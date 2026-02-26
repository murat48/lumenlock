import logging
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

logger = logging.getLogger(__name__)

_scheduler = None


def execute_due_transfers():
    """Check for pending scheduled transfers that are due and execute them."""
    import django
    from django.utils import timezone
    from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network
    import cryptocode

    from django.db import transaction as db_transaction
    from .models import ScheduledTransfer

    # Atomically claim each due transfer by transitioning it to 'processing'.
    # select_for_update(skip_locked=True) ensures that concurrent scheduler
    # instances skip rows already locked by another process, preventing
    # double-execution.
    with db_transaction.atomic():
        due = ScheduledTransfer.objects.select_for_update(skip_locked=True).filter(
            status='pending',
            scheduled_at__lte=timezone.now(),
        )
        claimed_ids = list(due.values_list('id', flat=True))
        if claimed_ids:
            ScheduledTransfer.objects.filter(id__in=claimed_ids).update(status='processing')

    if not claimed_ids:
        return

    for transfer in ScheduledTransfer.objects.filter(id__in=claimed_ids):
        try:
            raw_seed = cryptocode.decrypt(transfer.encrypted_seed, settings.SECRET_KEY[:32])
            if not raw_seed:
                raise ValueError('Failed to decrypt seed')

            source_keypair = Keypair.from_secret(raw_seed)
            server = Server("https://horizon-testnet.stellar.org")

            builder = TransactionBuilder(
                source_account=server.load_account(source_keypair.public_key),
                network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
                base_fee=100,
            ).append_payment_op(
                destination=transfer.recipient,
                amount=transfer.amount,
                asset=Asset.native(),
            ).set_timeout(30)

            if transfer.memo:
                builder.add_text_memo(transfer.memo)

            transaction = builder.build()
            transaction.sign(source_keypair)
            server.submit_transaction(transaction)

            transfer.status = 'completed'
            logger.info(f'Scheduled transfer {transfer.id} completed successfully.')
        except Exception as e:
            transfer.status = 'failed'
            logger.error(f'Scheduled transfer {transfer.id} failed: {e}')
        finally:
            transfer.save(update_fields=['status'])


def start():
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone='UTC')
    _scheduler.add_job(
        execute_due_transfers,
        trigger='interval',
        seconds=30,
        id='execute_due_transfers',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info('Scheduled transfer background scheduler started (runs every 30s).')

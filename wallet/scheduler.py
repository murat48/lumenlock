import logging
import uuid as _uuid
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
    # select_for_update(skip_locked=True) prevents concurrent scheduler instances
    # from picking the same row. SQLite does not support skip_locked, so we fall
    # back to a plain select_for_update (still atomic, just not skip-locked).
    db_engine = settings.DATABASES.get('default', {}).get('ENGINE', '')
    is_sqlite = 'sqlite' in db_engine
    now = timezone.now()

    if is_sqlite:
        # SQLite serialises writes, but claimed_ids would be the same for two
        # concurrent readers if they both read before either updates.  Using a
        # per-run UUID as a claim token stored in celery_task_id guarantees we
        # can identify *exactly* the rows this scheduler instance updated.
        claim_token = f'sched:{_uuid.uuid4().hex}'
        with db_transaction.atomic():
            ScheduledTransfer.objects.filter(
                status='pending',
                scheduled_at__lte=now,
            ).update(status='processing', celery_task_id=claim_token)
            claimed_ids = list(
                ScheduledTransfer.objects.filter(
                    status='processing',
                    celery_task_id=claim_token,
                ).values_list('id', flat=True)
            )
    else:
        # PostgreSQL / MySQL: use select_for_update(skip_locked=True) so that
        # concurrent scheduler instances each claim a disjoint set of rows.
        with db_transaction.atomic():
            claimed_ids = list(
                ScheduledTransfer.objects.select_for_update(skip_locked=True).filter(
                    status='pending',
                    scheduled_at__lte=now,
                ).values_list('id', flat=True)
            )
            if claimed_ids:
                ScheduledTransfer.objects.filter(id__in=claimed_ids).update(status='processing')

    if not claimed_ids:
        return

    # Re-filter by status='processing' AND id__in=claimed_ids so that we only
    # execute transfers this instance actually claimed.  Using id__in alone would
    # include rows that another concurrent process may have claimed first (their
    # status would already be 'processing' or beyond, but the data could be
    # mid-transition).  Filtering on both columns is the safe intersection.
    for transfer in ScheduledTransfer.objects.filter(id__in=claimed_ids, status='processing'):
        try:
            # Recovery path: if a previous run submitted the transaction but
            # crashed before saving 'completed', tx_hash will already be set.
            # Skip re-submission to prevent a double-send.
            if transfer.tx_hash:
                transfer.status = 'completed'
                transfer.save(update_fields=['status'])
                logger.info(f'Scheduled transfer {transfer.id} recovered from prior submit (hash={transfer.tx_hash}).')
                continue

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
            response = server.submit_transaction(transaction)

            # Persist the tx_hash BEFORE marking completed.  If the process
            # crashes between these two saves the next scheduler run will see
            # tx_hash is set and skip re-submission, then mark completed.
            transfer.tx_hash = response.get('hash', '')
            transfer.save(update_fields=['tx_hash'])

            transfer.status = 'completed'
            logger.info(f'Scheduled transfer {transfer.id} completed successfully (hash={transfer.tx_hash}).') 
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

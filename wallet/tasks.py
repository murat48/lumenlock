from celery import shared_task
from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network
import cryptocode
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def _server_encrypt(seed):
    """Re-encrypt the plaintext seed with the server secret key for safe DB storage."""
    return cryptocode.encrypt(seed, settings.SECRET_KEY[:32])


def _server_decrypt(enc):
    """Decrypt a server-encrypted seed."""
    return cryptocode.decrypt(enc, settings.SECRET_KEY[:32])


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def execute_scheduled_transfer(self, transfer_id):
    from .models import ScheduledTransfer

    try:
        transfer = ScheduledTransfer.objects.get(id=transfer_id)
    except ScheduledTransfer.DoesNotExist:
        return {'status': 'error', 'message': 'Transfer not found'}

    # Guard: do not execute if the scheduled time is in the future.
    # This handles early enqueue (clock skew, misconfigured ETA/beat).
    from django.utils import timezone
    if transfer.scheduled_at > timezone.now():
        # Reschedule: re-queue this task for the actual scheduled time.
        raise self.retry(eta=transfer.scheduled_at)

    try:
        raw_seed = _server_decrypt(transfer.encrypted_seed)
        if not raw_seed:
            raise ValueError('Failed to decrypt stored seed')

        source_keypair = Keypair.from_secret(raw_seed)
        server = Server("https://horizon-testnet.stellar.org")

        # Mark as 'processing' before the network call so that if the worker
        # crashes after submit_transaction but before the status update, the
        # retry will still proceed (status is not 'pending' but 'processing').
        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            updated = type(transfer).objects.filter(
                id=transfer.id, status__in=('pending', 'processing')
            ).update(status='processing')
        if not updated:
            return {'status': 'skipped', 'message': 'Concurrent worker already claimed this transfer'}
        transfer.status = 'processing'

        # Idempotency guard: if tx_hash is already set the payment was submitted
        # on a previous attempt (worker crashed after submit but before the
        # 'completed' save).  Skip re-submission to prevent a double-send.
        if transfer.tx_hash:
            transfer.status = 'completed'
            transfer.save(update_fields=['status'])
            return {'status': 'success', 'message': 'Already submitted on a previous attempt'}

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

        # Persist the hash BEFORE marking completed.  If the worker crashes
        # between these two saves, the next retry will see the hash and skip
        # re-submission, then mark the transfer completed cleanly.
        transfer.tx_hash = response.get('hash', '')
        transfer.save(update_fields=['tx_hash'])

        transfer.status = 'completed'
        transfer.save(update_fields=['status'])
        return {'status': 'success'}

    except Exception as exc:
        # Only mark as failed once all retries are exhausted; otherwise leave
        # the status as 'processing' so subsequent retries can proceed.
        if self.request.retries >= self.max_retries:
            transfer.status = 'failed'
            transfer.save()
        raise self.retry(exc=exc, countdown=60)

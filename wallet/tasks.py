from celery import shared_task
from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network
import cryptocode
from django.conf import settings


def _server_encrypt(seed):
    """Re-encrypt the plaintext seed with the server secret key for safe DB storage."""
    return cryptocode.encrypt(seed, settings.SECRET_KEY[:32])


def _server_decrypt(enc):
    """Decrypt a server-encrypted seed."""
    return cryptocode.decrypt(enc, settings.SECRET_KEY[:32])


@shared_task(bind=True, max_retries=3)
def execute_scheduled_transfer(self, transfer_id):
    from .models import ScheduledTransfer

    try:
        transfer = ScheduledTransfer.objects.get(id=transfer_id)
    except ScheduledTransfer.DoesNotExist:
        return {'status': 'error', 'message': 'Transfer not found'}

    if transfer.status != 'pending':
        return {'status': 'skipped', 'message': f'Transfer already {transfer.status}'}

    try:
        raw_seed = _server_decrypt(transfer.encrypted_seed)
        if not raw_seed:
            raise ValueError('Failed to decrypt stored seed')

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
        transfer.save()
        return {'status': 'success'}

    except Exception as exc:
        # Only mark as failed once all retries are exhausted; otherwise the
        # status != 'pending' guard above would prevent the retry from running.
        if self.request.retries >= self.max_retries:
            transfer.status = 'failed'
            transfer.save()
        raise self.retry(exc=exc, countdown=60)

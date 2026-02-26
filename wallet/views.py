from django.shortcuts import render
from django.http import JsonResponse
from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network, TextMemo, StrKey
from .models import Wallet, ScheduledTransfer
import cryptocode
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
import requests
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
import json
from django.utils.dateparse import parse_datetime
from django.utils import timezone
import datetime

def home(request):
    return render(request, 'home.html')

@login_required
def create_wallet(request):
    if Wallet.objects.filter(user=request.user).exists():
        return redirect('dashboard')
    keypair = Keypair.random()
    encryption_key = request.POST.get('password')
    encrypted_secret_seed = keypair.secret
    encrypted_secret_seed = cryptocode.encrypt(keypair.secret, encryption_key)
    wallet = Wallet.objects.create(
        user=User.objects.first(),
        public_key=keypair.public_key,
        secret_seed=encrypted_secret_seed
    )
    url = "https://friendbot.stellar.org"
    response = requests.get(url, params={"addr": keypair.public_key})
    return redirect('dashboard')


def check_balance(request):
    public_key = request.POST.get('public_key')
    if not public_key:
        wallet = Wallet.objects.filter(user=request.user)[0]
        public_key = wallet.public_key
    server = Server("https://horizon-testnet.stellar.org")
    account = server.accounts().account_id(public_key).call()
    return JsonResponse({'balance': account['balances'][0]['balance']})


@login_required
def send_money(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        destination_public_key = data.get('recipient')
        amount = data.get('amount')
        encryption_key = data.get('transaction_password')
        memo = data.get('memo', '').strip()
        wallet = Wallet.objects.filter(user=request.user)[0]
        server = Server("https://horizon-testnet.stellar.org")
        source_keypair = Keypair.from_secret(cryptocode.decrypt(wallet.secret_seed, encryption_key))
        builder = TransactionBuilder(
            source_account=server.load_account(source_keypair.public_key),
            network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
            base_fee=100
        ).append_payment_op(
            destination=destination_public_key,
            amount=amount,
            asset=Asset.native()
        ).set_timeout(30)
        if memo:
            builder.add_text_memo(memo[:28])
        transaction = builder.build()
        transaction.sign(source_keypair)
        response = server.submit_transaction(transaction)
        return JsonResponse({'message': 'Payment sent successfully', 'status': 'success'})
    else:
        pass


@login_required
def bulk_send(request):
    """Send XLM to multiple recipients in a single transaction."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    recipients = data.get('recipients', [])  # [{"address": "...", "amount": "..."}]
    encryption_key = data.get('transaction_password')
    memo = data.get('memo', '').strip()

    if not recipients:
        return JsonResponse({'status': 'error', 'message': 'No recipients provided'}, status=400)

    # Stellar transactions are capped at 100 operations
    MAX_RECIPIENTS = 100
    if len(recipients) > MAX_RECIPIENTS:
        return JsonResponse(
            {'status': 'error', 'message': f'Too many recipients: max {MAX_RECIPIENTS} per transaction'},
            status=400,
        )

    # Validate each recipient object before hitting the network
    for i, r in enumerate(recipients):
        if not isinstance(r, dict) or 'address' not in r or 'amount' not in r:
            return JsonResponse(
                {'status': 'error', 'message': f'Recipient {i} is missing required fields: address, amount'},
                status=400,
            )
        if not r['address'] or not str(r['amount']).strip():
            return JsonResponse(
                {'status': 'error', 'message': f'Recipient {i} has empty address or amount'},
                status=400,
            )
        if not StrKey.is_valid_ed25519_public_key(str(r['address'])):
            return JsonResponse(
                {'status': 'error', 'message': f'Recipient {i} has an invalid Stellar address'},
                status=400,
            )
        try:
            if float(r['amount']) <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            return JsonResponse(
                {'status': 'error', 'message': f'Recipient {i} amount must be a positive number'},
                status=400,
            )

    wallet = Wallet.objects.filter(user=request.user).first()
    if not wallet:
        return JsonResponse({'status': 'error', 'message': 'Wallet not found'})

    try:
        server = Server("https://horizon-testnet.stellar.org")
        source_keypair = Keypair.from_secret(cryptocode.decrypt(wallet.secret_seed, encryption_key))
        # base_fee is a per-operation fee; do NOT multiply by recipient count.
        builder = TransactionBuilder(
            source_account=server.load_account(source_keypair.public_key),
            network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
            base_fee=100,
        ).set_timeout(30)

        for r in recipients:
            builder.append_payment_op(
                destination=r['address'],
                amount=str(r['amount']),
                asset=Asset.native(),
            )

        if memo:
            builder.add_text_memo(memo[:28])

        transaction = builder.build()
        transaction.sign(source_keypair)
        server.submit_transaction(transaction)
        return JsonResponse({'status': 'success', 'message': f'{len(recipients)} payments sent successfully'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})


@login_required
def schedule_transfer(request):
    """Schedule an XLM transfer for a future date/time via Celery."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    recipient = data.get('recipient')
    amount = data.get('amount')
    memo = data.get('memo', '').strip()
    scheduled_at_str = data.get('scheduled_at')
    encryption_key = data.get('transaction_password')

    # Validate required fields before any DB work
    if not recipient or not str(recipient).strip():
        return JsonResponse({'status': 'error', 'message': 'recipient is required'}, status=400)
    if not StrKey.is_valid_ed25519_public_key(str(recipient)):
        return JsonResponse({'status': 'error', 'message': 'Invalid Stellar recipient address'}, status=400)
    if not amount and amount != 0:
        return JsonResponse({'status': 'error', 'message': 'amount is required'}, status=400)
    try:
        if float(amount) <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'amount must be a positive number'}, status=400)
    if not scheduled_at_str:
        return JsonResponse({'status': 'error', 'message': 'scheduled_at is required'}, status=400)

    wallet = Wallet.objects.filter(user=request.user).first()
    if not wallet:
        return JsonResponse({'status': 'error', 'message': 'Wallet not found'})

    decrypted_seed = cryptocode.decrypt(wallet.secret_seed, encryption_key)
    if not decrypted_seed:
        return JsonResponse({'status': 'error', 'message': 'Wrong transaction password'})

    scheduled_at = parse_datetime(scheduled_at_str)
    if not scheduled_at:
        return JsonResponse({'status': 'error', 'message': 'Invalid date/time format'})
    if timezone.is_naive(scheduled_at):
        # The UI label says "Send At (UTC)"; always interpret naive input as UTC
        # regardless of the server's TIME_ZONE setting.
        scheduled_at = timezone.make_aware(scheduled_at, datetime.timezone.utc)

    if scheduled_at <= timezone.now():
        return JsonResponse({'status': 'error', 'message': 'Scheduled time must be in the future'})

    from .tasks import _server_encrypt
    server_encrypted_seed = _server_encrypt(decrypted_seed)

    transfer = ScheduledTransfer.objects.create(
        user=request.user,
        recipient=recipient,
        amount=str(amount),
        memo=memo[:28],
        scheduled_at=scheduled_at,
        encrypted_seed=server_encrypted_seed,
    )
    # The in-process APScheduler (wallet/scheduler.py) polls every 30s
    # and will execute this transfer automatically when its time is due.
    return JsonResponse({
        'status': 'success',
        'message': f'Transfer scheduled for {scheduled_at.strftime("%Y-%m-%d %H:%M UTC")}',
    })


@login_required
def list_scheduled_transfers(request):
    transfers = ScheduledTransfer.objects.filter(user=request.user).order_by('-scheduled_at')
    data = []
    for t in transfers:
        data.append({
            'id': t.id,
            'recipient': t.recipient,
            'amount': t.amount,
            'memo': t.memo,
            'scheduled_at': t.scheduled_at.strftime('%Y-%m-%d %H:%M UTC'),
            'status': t.status,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M UTC'),
        })
    return JsonResponse({'transfers': data})


@login_required
def cancel_scheduled_transfer(request, transfer_id):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

    try:
        transfer = ScheduledTransfer.objects.get(id=transfer_id, user=request.user)
    except ScheduledTransfer.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Transfer not found'})

    if transfer.status != 'pending':
        return JsonResponse({'status': 'error', 'message': f'Cannot cancel a transfer with status: {transfer.status}'})

    transfer.status = 'cancelled'
    transfer.save(update_fields=['status'])
    return JsonResponse({'status': 'success', 'message': 'Transfer cancelled'})


@login_required
def transaction_history(request):
    wallet = Wallet.objects.filter(user=request.user).first()
    if not wallet:
        return JsonResponse({'transactions': []})
    server = Server("https://horizon-testnet.stellar.org")
    try:
        payments = (
            server.payments()
            .for_account(wallet.public_key)
            .order(desc=True)
            .limit(50)
            .call()
        )
    except Exception:
        return JsonResponse({'transactions': []})

    transactions = []
    for payment in payments.get('_embedded', {}).get('records', []):
        ptype = payment.get('type')
        if ptype == 'payment':
            is_sent = payment.get('from') == wallet.public_key
            transactions.append({
                'type': 'sent' if is_sent else 'received',
                'amount': payment.get('amount'),
                'asset': payment.get('asset_type', 'native'),
                'from': payment.get('from'),
                'to': payment.get('to'),
                'hash': payment.get('transaction_hash'),
                'date': payment.get('created_at'),
            })
        elif ptype == 'create_account':
            is_sent = payment.get('funder') == wallet.public_key
            transactions.append({
                'type': 'sent' if is_sent else 'received',
                'amount': payment.get('starting_balance'),
                'asset': 'native',
                'from': payment.get('funder'),
                'to': payment.get('account'),
                'hash': payment.get('transaction_hash'),
                'date': payment.get('created_at'),
            })
    return JsonResponse({'transactions': transactions})


@login_required
def dashboard(request):
    wallet_exists = Wallet.objects.filter(user=request.user).exists()
    if not wallet_exists:
        return render(request, 'dashboard.html', {'wallet_exists': wallet_exists})
    wallet = Wallet.objects.filter(user=request.user)[0]
    server = Server("https://horizon-testnet.stellar.org")
    account = server.accounts().account_id(wallet.public_key).call()
    balance = account['balances'][0]['balance']
    context = {
        'wallet_exists': wallet_exists,
        'balance': balance,
        'public_key': wallet.public_key
    }
    return render(request, 'dashboard.html', context)
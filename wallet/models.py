from django.db import models

# Create your models here.
class Wallet(models.Model):
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    public_key = models.CharField(max_length=56)
    secret_seed = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username + self.public_key


class ScheduledTransfer(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    recipient = models.CharField(max_length=56)
    amount = models.CharField(max_length=20)
    memo = models.CharField(max_length=28, blank=True)
    scheduled_at = models.DateTimeField()
    encrypted_seed = models.CharField(max_length=512)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    celery_task_id = models.CharField(max_length=255, blank=True)
    tx_hash = models.CharField(max_length=64, blank=True,
        help_text='Stellar transaction hash once submitted; used to prevent duplicate sends.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} -> {self.recipient} ({self.amount} XLM) @ {self.scheduled_at}'
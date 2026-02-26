from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0004_scheduledtransfer_celery_task_id_and_more'),
    ]

    operations = [
        # Add tx_hash field for idempotent retry detection
        migrations.AddField(
            model_name='scheduledtransfer',
            name='tx_hash',
            field=models.CharField(
                blank=True,
                max_length=64,
                help_text='Stellar transaction hash once submitted; used to prevent duplicate sends.',
            ),
        ),
        # Extend the status field to include the 'processing' intermediate state
        migrations.AlterField(
            model_name='scheduledtransfer',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]

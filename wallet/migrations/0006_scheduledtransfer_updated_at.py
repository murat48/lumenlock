from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0005_scheduledtransfer_tx_hash_processing_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduledtransfer',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]

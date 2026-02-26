from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wallet'

    def ready(self):
        import os
        # Only start the scheduler once: in the parent process (autoreload monitor)
        # or in a non-autoreload environment (e.g. production/gunicorn).
        # When autoreload is active Django sets RUN_MAIN='true' only in the child;
        # the parent never sets it, so `not RUN_MAIN` is True only there.
        if not os.environ.get('RUN_MAIN'):
            from . import scheduler
            scheduler.start()

from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wallet'

    def ready(self):
        import os
        # Avoid running twice in Django's autoreload (RUN_MAIN is set by the reloader child process)
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('RUN_MAIN'):
            from . import scheduler
            scheduler.start()

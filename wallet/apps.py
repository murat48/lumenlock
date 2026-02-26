from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wallet'

    def ready(self):
        import os
        # LUMENLOCK_RUN_SCHEDULER controls the in-process APScheduler:
        #   '1'  – always start (use on exactly ONE process in production)
        #   '0'  – never start
        #  unset – dev-autoreload heuristic: start only in the parent process
        #          (RUN_MAIN is set only in the reloader child, not the parent)
        #
        # In production set LUMENLOCK_RUN_SCHEDULER=0 on all worker processes
        # and run the scheduler as a separate single process
        # (or use Celery Beat instead).
        scheduler_flag = os.environ.get('LUMENLOCK_RUN_SCHEDULER')
        if scheduler_flag == '1' or (scheduler_flag is None and not os.environ.get('RUN_MAIN')):
            from . import scheduler
            scheduler.start()

from django.apps import AppConfig
from django.conf import settings


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'
    def ready(self):
        import os
        if os.environ.get('RUN_MAIN'):
            from .scheduler import start_scheduler
            start_scheduler()

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    # Планировщик намеренно не запускается из web-процесса: он живёт в отдельном
    # процессе (`manage.py run_scheduler`, сервис scheduler в docker-compose).
    # Иначе задача либо не стартует вовсе под WSGI/ASGI, либо дублируется по
    # числу воркеров.

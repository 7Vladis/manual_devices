"""
Планировщик периодических задач в отдельном процессе.

Запускается сервисом `scheduler` из docker-compose:

    python manage.py run_scheduler

Живёт вне web-процесса намеренно: под WSGI/ASGI `AppConfig.ready()` либо не
выполняется в нужный момент, либо выполняется в каждом воркере, порождая
несколько планировщиков. Один контейнер — ровно один экземпляр.
"""

import logging
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand
from django_apscheduler.jobstores import DjangoJobStore

from notifications.tasks import run_daily_maintenance_check

logger = logging.getLogger('notifications')

JOB_ID = 'maintenance_digest'


class Command(BaseCommand):
    help = "Запускает планировщик периодических задач (блокирующий процесс)."

    def add_arguments(self, parser):
        parser.add_argument('--hour', type=int, default=9, help="Час запуска рассылки (по умолчанию 9).")
        parser.add_argument('--minute', type=int, default=0, help="Минута запуска рассылки (по умолчанию 0).")

    def handle(self, *args, **options):
        hour, minute = options['hour'], options['minute']

        # Таймзона задаётся явно: иначе APScheduler берёт зону хоста, которая
        # в контейнере обычно UTC, и «9 утра» оказывается не тем временем.
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        scheduler.add_job(
            run_daily_maintenance_check,
            trigger='cron',
            hour=hour,
            minute=minute,
            id=JOB_ID,
            name="Сводка по приближающимся ТО",
            replace_existing=True,
            max_instances=1,
            # Если процесс лежал, пропущенные запуски схлопываются в один
            # и выполняются только в пределах часа после планового времени.
            coalesce=True,
            misfire_grace_time=3600,
        )

        def shutdown(signum, _frame):
            logger.info("Получен сигнал %s — останавливаю планировщик", signum)
            scheduler.shutdown(wait=False)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        self.stdout.write(self.style.SUCCESS(
            f"Планировщик запущен: ежедневно в {hour:02d}:{minute:02d} ({settings.TIME_ZONE}). "
            f"Остановка — Ctrl+C или SIGTERM."
        ))
        logger.info("Планировщик запущен: %02d:%02d %s", hour, minute, settings.TIME_ZONE)

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")

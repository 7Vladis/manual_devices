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

from data.services.youtrack_queue import process_jobs
from notifications.tasks import run_daily_maintenance_check

logger = logging.getLogger('notifications')

JOB_ID = 'maintenance_digest'
YOUTRACK_JOB_ID = 'youtrack_queue'


class Command(BaseCommand):
    help = "Запускает планировщик периодических задач (блокирующий процесс)."

    def add_arguments(self, parser):
        parser.add_argument('--hour', type=int, default=9, help="Час запуска рассылки (по умолчанию 9).")
        parser.add_argument('--minute', type=int, default=0, help="Минута запуска рассылки (по умолчанию 0).")
        parser.add_argument('--youtrack-interval', type=int, default=5,
                            help="Как часто разбирать очередь заданий YouTrack, секунд (по умолчанию 5).")

    def handle(self, *args, **options):
        hour, minute = options['hour'], options['minute']
        youtrack_interval = options['youtrack_interval']

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

        # Очередь обмена с YouTrack. Живёт здесь же: отдельный процесс ради
        # одного цикла опроса — лишняя деталь в развёртывании, а гарантия
        # «один контейнер = один экземпляр» у планировщика уже есть.
        scheduler.add_job(
            process_jobs,
            trigger='interval',
            seconds=youtrack_interval,
            id=YOUTRACK_JOB_ID,
            name="Очередь заданий YouTrack",
            replace_existing=True,
            # Задания не должны наслаиваться: следующий проход стартует
            # только после того, как закончился предыдущий.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

        def shutdown(signum, _frame):
            logger.info("Получен сигнал %s — останавливаю планировщик", signum)
            scheduler.shutdown(wait=False)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        self.stdout.write(self.style.SUCCESS(
            f"Планировщик запущен: сводка ежедневно в {hour:02d}:{minute:02d} ({settings.TIME_ZONE}), "
            f"очередь YouTrack каждые {youtrack_interval} с. Остановка — Ctrl+C или SIGTERM."
        ))
        logger.info("Планировщик запущен: сводка %02d:%02d %s, очередь YouTrack каждые %s с",
                    hour, minute, settings.TIME_ZONE, youtrack_interval)

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")

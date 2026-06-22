from apscheduler.schedulers.background import BackgroundScheduler
from django_apscheduler.jobstores import DjangoJobStore
from .tasks import run_daily_maintenance_check

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_jobstore(DjangoJobStore(), "default")
    scheduler.add_job(
        run_daily_maintenance_check,
        trigger="cron",
        hour=9,
        minute=0,
        id="maintenance_check",
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
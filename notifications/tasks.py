from django.utils import timezone
from datetime import timedelta
from data.models import DataObject
from .services import send_mattermost_notification

def run_daily_maintenance_check():
    now = timezone.now()
    today_end = now.replace(hour=23, minute=59, second=59)
    in_7_days = (now + timedelta(days=7)).date()
    in_30_days = (now + timedelta(days=30)).date()
    to_today = DataObject.objects.filter(next_maintenance_date__range=[now, today_end])
    to_week = DataObject.objects.filter(next_maintenance_date__range=[now, in_7_days])
    to_month = DataObject.objects.filter(next_maintenance_date__range=[now, in_30_days])
    if to_today.exists():
        msg = "Список ТО объектов на сегодня:\n" + "\n".join([f"- {obj.name or obj.model.name} (ID: {obj.id})" for obj in to_today])
        send_mattermost_notification(msg)
    if to_week.exists():
        msg = "Список ТО объектов через неделю:\n" + "\n".join([f"- {obj.name or obj.model.name} (ID: {obj.id})" for obj in to_week])
        send_mattermost_notification(msg)
    if to_month.exists():
        msg = "Список ТО объектов через месяц:\n" + "\n".join([f"- {obj.name or obj.model.name} (ID: {obj.id})" for obj in to_month])
        send_mattermost_notification(msg)
from django.utils import timezone
from datetime import timedelta
from data.models import DataObject
from .services import send_mattermost_notification

def format_object_list(queryset):
        lines = []
        for obj in queryset:
            name = obj.name or "Без имени"
            model_name = obj.model.name if obj.model else "Модель не указана"
            inv_num = obj.inventory_number or "нет"
            lines.append(f"- **{name}** [Модель: {model_name}] (Инв. №: {inv_num})")
        return "\n".join(lines)

def run_daily_maintenance_check():
    now = timezone.now()
    str_today = now.strftime('%d.%m.%Y')
    str_7_days = (now + timedelta(days=7)).strftime('%d.%m.%Y')
    str_30_days = (now + timedelta(days=30)).strftime('%d.%m.%Y')
    today_start = now.replace(hour=0, minute=0, second=0)
    today_end = now.replace(hour=23, minute=59, second=59)
    day_7_start = today_start + timedelta(days=7)
    day_7_end = today_end + timedelta(days=7)
    day_30_start = today_start + timedelta(days=30)
    day_30_end = today_end + timedelta(days=30)
    to_today = DataObject.objects.filter(
        next_maintenance_date__range=[today_start, today_end]
    ).select_related('model')
    
    to_week = DataObject.objects.filter(
        next_maintenance_date__range=[day_7_start, day_7_end]
    ).select_related('model')
    
    to_month = DataObject.objects.filter(
        next_maintenance_date__range=[day_30_start, day_30_end]
    ).select_related('model')

    if to_today.exists():
        msg = f"⚠️ Список объектов на обслуживание {str_today}:\n" + format_object_list(to_today)
        send_mattermost_notification(msg)
        
    if to_week.exists():
        msg = f"📅 Список объектов на обслуживание {str_7_days} (через 7 дней):\n" + format_object_list(to_week)
        send_mattermost_notification(msg)
        
    if to_month.exists():
        msg = f"🛠️ Список объектов на обслуживание {str_30_days} (через 30 дней):\n" + format_object_list(to_month)
        send_mattermost_notification(msg)

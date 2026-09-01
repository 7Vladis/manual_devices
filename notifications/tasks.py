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
    today = timezone.localdate()
    day_7 = today + timedelta(days=7)
    day_30 = today + timedelta(days=30)
    
    str_today = today.strftime('%d.%m.%Y')
    str_7_days = day_7.strftime('%d.%m.%Y')
    str_30_days = day_30.strftime('%d.%m.%Y')

    # Точные сравнения по DateField
    to_today = DataObject.objects.filter(
        next_maintenance_date=today
    ).select_related('model')
    
    to_week = DataObject.objects.filter(
        next_maintenance_date=day_7
    ).select_related('model')
    
    to_month = DataObject.objects.filter(
        next_maintenance_date=day_30
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
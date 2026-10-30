import logging
from datetime import timedelta

from django.utils import timezone
from data.models import MaintenancePlan
from .services import send_mattermost_notification

logger = logging.getLogger('notifications')


def format_plan_list(plans):
    """
    Строки сводки: что за работа и над каким объектом.

    Рассылка идёт по работам, а не по объектам: у насоса осмотр и смазка
    подшипников — разные работы с разными интервалами, и человеку, который
    читает сводку в чате, нужно знать, что именно подошло по сроку. Без
    названия работы две строки про один насос выглядели бы повтором.
    """
    lines = []
    for plan in plans:
        obj = plan.data_object
        name = obj.name or "Без имени"
        model_name = obj.model.name if obj.model else "Модель не указана"
        inv_num = obj.inventory_number or "нет"
        lines.append(f"- **{plan.title}** — {name} [Модель: {model_name}] (Инв. №: {inv_num})")
    return "\n".join(lines)


def run_daily_maintenance_check(today=None, dry_run=False):
    """
    Рассылает сводку по работам, назначенным на сегодня, через 7 и через 30
    дней. Возвращает список отправленных (или подготовленных при dry_run)
    сообщений — это делает задачу проверяемой и пригодной для ручного
    запуска из management-команды.
    """
    today = today or timezone.localdate()
    messages = []

    plans = MaintenancePlan.objects.select_related(
        'rule', 'data_object', 'data_object__model'
    ).order_by('data_object__name', 'created_at')

    periods = [
        (today, '\u26a0\ufe0f', 'Работы на {date}'),
        (today + timedelta(days=7), '\U0001f4c5', 'Работы на {date} (через 7 дней)'),
        (today + timedelta(days=30), '\U0001f6e0\ufe0f', 'Работы на {date} (через 30 дней)'),
    ]

    for day, mark, title in periods:
        due = list(plans.filter(next_date=day))
        if not due:
            continue
        heading = title.format(date=day.strftime('%d.%m.%Y'))
        messages.append(f"{mark} {heading}:\n" + format_plan_list(due))

    if dry_run:
        return messages

    for msg in messages:
        success, detail = send_mattermost_notification(msg)
        if not success:
            logger.error("Уведомление не отправлено в Mattermost: %s", detail)

    return messages

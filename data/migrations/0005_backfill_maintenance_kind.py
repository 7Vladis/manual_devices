"""
Заполняет вид обслуживания для уже накопленной истории.

Раньше плановое и внеплановое ТО различались только текстом описания,
поэтому здесь он и используется — единственный доступный источник.
`planned_for` для старых записей проставляется по дате выполнения: точную
плановую дату восстановить неоткуда, но без неё статистика за прошедшие
периоды обнулилась бы.

Записи, пришедшие из YouTrack (есть youtrack_id), считаем внеплановыми:
списание времени в задаче не закрывает локальное плановое событие.
"""

from django.db import migrations

UNPLANNED_MARKERS = ('[Внеплановое ТО]', 'Внеплановое техническое обслуживание')


def backfill(apps, schema_editor):
    ActionHistory = apps.get_model('data', 'ActionHistory')
    maintenance = ActionHistory.objects.filter(action_type='maintenance')

    for entry in maintenance.iterator(chunk_size=500):
        text = entry.action or ''
        if entry.youtrack_id or any(marker in text for marker in UNPLANNED_MARKERS):
            entry.maintenance_kind = 'unplanned'
            entry.planned_for = None
        else:
            entry.maintenance_kind = 'planned'
            entry.planned_for = entry.created_at.date()
        entry.save(update_fields=['maintenance_kind', 'planned_for'])


def unset(apps, schema_editor):
    ActionHistory = apps.get_model('data', 'ActionHistory')
    ActionHistory.objects.filter(action_type='maintenance').update(
        maintenance_kind=None, planned_for=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ('data', '0004_maintenance_plan_event'),
    ]

    operations = [
        migrations.RunPython(backfill, unset),
    ]

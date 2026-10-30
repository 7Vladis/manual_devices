"""
Сроки ТО переезжают из объекта в планы.

У объекта было одно правило и одна дата, а работ у него бывает несколько:
плановый осмотр и смазка подшипников идут с разными интервалами, и сроки
у них расходятся. Поэтому срок стал отдельной записью `MaintenancePlan`
(объект + правило + дата), а `DataObject.next_maintenance_date` остался
сводкой — ближайшим из сроков.

Порядок операций важен: модель создаётся до переноса, а колонка
`date_update_rule` убирается только после него — иначе читать было бы
неоткуда.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


def move_schedules_to_plans(apps, schema_editor):
    """
    Переносит привязку объекта к правилу и его дату в план.

    План заводится и объекту без правила, но с назначенной датой: это срок,
    поставленный вручную, и терять его нельзя. Объект без того и другого
    планов не получает — обслуживание ему не назначено.
    """
    DataObject = apps.get_model('data', 'DataObject')
    MaintenancePlan = apps.get_model('data', 'MaintenancePlan')

    plans = [
        MaintenancePlan(
            uuid=uuid.uuid4(),
            data_object_id=obj.pk,
            rule_id=obj.date_update_rule_id,
            next_date=obj.next_maintenance_date,
        )
        for obj in DataObject.objects.exclude(
            date_update_rule__isnull=True, next_maintenance_date__isnull=True
        ).only('pk', 'date_update_rule_id', 'next_maintenance_date')
    ]
    MaintenancePlan.objects.bulk_create(plans, batch_size=500)


def move_plans_back(apps, schema_editor):
    """
    Возвращает срок в объект — для отката.

    Планов к этому моменту может оказаться несколько, а поле одно: берём
    ближайший срок, как и делает сводка. Остальные при откате теряются —
    поле их вместить не может, и это цена возврата к одной дате.
    """
    DataObject = apps.get_model('data', 'DataObject')
    MaintenancePlan = apps.get_model('data', 'MaintenancePlan')

    for obj in DataObject.objects.all().only('pk'):
        plans = list(MaintenancePlan.objects.filter(data_object_id=obj.pk))
        if not plans:
            continue
        dated = [p for p in plans if p.next_date]
        nearest = min(dated, key=lambda p: p.next_date) if dated else plans[0]
        obj.date_update_rule_id = nearest.rule_id
        obj.next_maintenance_date = nearest.next_date
        obj.save(update_fields=['date_update_rule', 'next_maintenance_date'])


class Migration(migrations.Migration):

    dependencies = [
        ('data', '0004_youtrackjob_per_user'),
    ]

    operations = [
        migrations.CreateModel(
            name='MaintenancePlan',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('next_date', models.DateField(blank=True, null=True, verbose_name='Дата следующего ТО')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Добавлен')),
                ('data_object', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='plans', to='data.dataobject', verbose_name='Объект')),
                ('rule', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='plans', to='data.dateupdaterule', verbose_name='Правило расчёта')),
            ],
            options={
                'verbose_name': 'План обслуживания',
                'verbose_name_plural': 'Планы обслуживания',
                'db_table': 'maintenance_plan',
                'ordering': ['created_at'],
                'constraints': [
                    models.UniqueConstraint(fields=('data_object', 'rule'), name='unique_plan_per_rule', violation_error_message='Это правило уже назначено объекту.'),
                    models.UniqueConstraint(condition=models.Q(('rule__isnull', True)), fields=('data_object',), name='unique_manual_plan_per_object', violation_error_message='Срок вручную у объекта может быть только один.'),
                ],
            },
        ),
        migrations.AddField(
            model_name='actionhistory',
            name='maintenance_rule',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='history_entries', to='data.dateupdaterule', verbose_name='Правило обслуживания'),
        ),
        migrations.RunPython(move_schedules_to_plans, move_plans_back),
        migrations.RemoveField(
            model_name='dataobject',
            name='date_update_rule',
        ),
    ]

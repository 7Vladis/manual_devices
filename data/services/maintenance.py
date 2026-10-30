"""Расчёт плановых дат ТО по правилу."""

import calendar
import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from dateutil.relativedelta import relativedelta

logger = logging.getLogger('data')


def calculate_plan_date(plan, base_date=None, use_anchor=True):
    """
    Рассчитывает следующую дату плана (datetime.date).

    Считает по одному плану, а не по объекту: планов у объекта может быть
    несколько, и у каждого свой срок и своя точка отсчёта. Прежняя дата для
    привязки «от планового срока» берётся из самого плана — у соседнего она
    другая, и перепутать их значило бы сдвинуть не тот график.

    use_anchor=False отключает привязку к прежнему плановому сроку. Это нужно,
    когда правило меняют или только что привязывают к объекту: прежняя дата
    рассчитана по другому правилу, и отталкиваться от неё некорректно —
    срок считается заново от base_date.
    """
    if plan is None or plan.rule_id is None:
        return None

    rule_data = plan.rule.rule or {}
    strategy = rule_data.get('strategy', 'relative')
    anchor_type = rule_data.get('anchor', 'actual')
    value = rule_data.get('value', {})

    if not base_date:
        base_date = timezone.localdate()
    elif isinstance(base_date, datetime):
        base_date = base_date.date()

    if use_anchor and anchor_type == 'scheduled' and plan.next_date:
        base_date = plan.next_date

    if strategy == 'relative':
        if not isinstance(value, dict):
            return None
        try:
            delta = relativedelta(
                years=int(value.get('years', 0) or 0),
                months=int(value.get('months', 0) or 0),
                days=int(value.get('days', 0) or 0),
            )
        except (TypeError, ValueError):
            logger.warning("Правило %s содержит нечисловой интервал: %r", plan.rule_id, value)
            return None
        return base_date + delta
        
    elif strategy == 'fixed':
        if not isinstance(value, list) or not value:
            return None
            
        dates_in_year = []
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                m = int(item.get('month'))
                d = int(item.get('day'))
            except (TypeError, ValueError):
                continue
            if not 1 <= m <= 12:
                continue
            # 29 февраля в невисокосный год — берём последний день месяца,
            # а не фиксированное 28-е для любого месяца, как было раньше.
            d = min(max(d, 1), calendar.monthrange(base_date.year, m)[1])
            dates_in_year.append(base_date.replace(month=m, day=d))

        if not dates_in_year:
            return None

        dates_in_year.sort()
        
        for candidate in dates_in_year:
            if candidate > base_date:
                return candidate
                
        first_candidate = dates_in_year[0]
        return first_candidate + relativedelta(years=1)
        
    return None


def refresh_object_schedule(data_object):
    """
    Пересчитывает сводку объекта: `next_maintenance_date` — ближайший из сроков.

    Единственное место, которое пишет это поле. Сводка нужна там, где
    спрашивать планы отдельным запросом дорого или незачем: строка дерева,
    выборки плана ТО, рассылка, опись. Поэтому её не вычисляют на лету, а
    держат в объекте и обновляют после любой правки планов.

    План без правила и без даты смысла не имеет — такой убираем: срок вручную
    существует ровно пока у него есть дата.
    """
    data_object.plans.filter(rule__isnull=True, next_date__isnull=True).delete()

    dates = [plan.next_date for plan in data_object.plans.all() if plan.next_date]
    nearest = min(dates) if dates else None
    if data_object.next_maintenance_date != nearest:
        data_object.next_maintenance_date = nearest
        data_object.save(update_fields=['next_maintenance_date'])
    return nearest


def get_single_plan(data_object):
    """
    План по умолчанию — когда выбирать не из чего или выбор не сделан.

    Нужен там, где план не назван явно: объект создают с одним сроком, а окно
    фиксации ТО присылает выбранный план только при нескольких. Предпочитаем
    план с правилом; при нескольких берём первый по времени добавления, как
    их и упорядочивает модель.
    """
    plans = list(data_object.plans.select_related('rule').all())
    if not plans:
        return None
    with_rule = [plan for plan in plans if plan.rule_id]
    return with_rule[0] if with_rule else plans[0]


def set_object_rule(data_object, rule, base_date=None):
    """
    Назначает объекту правило и считает срок заново.

    Срок считается от сегодняшнего дня (`use_anchor=False`): прежняя дата
    рассчитана по другому правилу, и отталкиваться от неё неверно.

    Прежний план переиспользуется, а не создаётся заново, чтобы у объекта не
    появился второй план с тем же правилом и чтобы ручной срок, если он был,
    превратился в плановый, а не остался лишней строкой.
    """
    from ..models import MaintenancePlan

    plan = get_single_plan(data_object)
    if plan is None:
        plan = MaintenancePlan(data_object=data_object)
    plan.rule = rule
    plan.next_date = calculate_plan_date(
        plan, base_date=base_date or timezone.localdate(), use_anchor=False
    )
    plan.save()
    refresh_object_schedule(data_object)
    return plan


def set_manual_date(data_object, next_date):
    """Назначает срок вручную: без правила, дату двигает человек."""
    from ..models import MaintenancePlan

    plan = get_single_plan(data_object)
    if plan is None:
        if next_date is None:
            return None
        plan = MaintenancePlan(data_object=data_object)
    plan.next_date = next_date
    plan.save()
    refresh_object_schedule(data_object)
    return plan


def add_plan(data_object, rule=None, next_date=None):
    """
    Заводит объекту ещё один план обслуживания.

    У плана с правилом срок считается сразу и от сегодняшнего дня: план
    только что заведён, отталкиваться не от чего. У плана без правила дату
    задаёт человек.

    Повтор ловит база, а не проверка в коде: два запроса могли бы пройти
    проверку одновременно. `IntegrityError` поднимается наружу, и вызывающий
    показывает его текст — он записан прямо в ограничении.
    """
    from ..models import MaintenancePlan

    plan = MaintenancePlan(data_object=data_object, rule=rule, next_date=next_date)
    if rule is not None:
        plan.next_date = calculate_plan_date(
            plan, base_date=timezone.localdate(), use_anchor=False
        )
    with transaction.atomic():
        plan.save()
    refresh_object_schedule(data_object)
    return plan


def remove_plan(plan):
    """Убирает план и пересчитывает сводку объекта."""
    data_object = plan.data_object
    plan.delete()
    refresh_object_schedule(data_object)
    return data_object

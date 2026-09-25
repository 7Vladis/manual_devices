"""Расчёт плановых дат ТО по правилу."""

import calendar
import logging
from datetime import datetime

from django.utils import timezone

from dateutil.relativedelta import relativedelta

logger = logging.getLogger('data')


def calculate_next_maintenance_date(data_object, base_date=None, use_anchor=True):
    """
    Рассчитывает следующую дату ТО (datetime.date).

    use_anchor=False отключает привязку к прежнему плановому сроку. Это нужно,
    когда правило меняют или только что привязывают к объекту: прежняя дата
    рассчитана по другому правилу, и отталкиваться от неё некорректно —
    срок считается заново от base_date.
    """
    if not data_object.date_update_rule:
        return None
        
    rule_data = data_object.date_update_rule.rule or {}
    strategy = rule_data.get('strategy', 'relative')
    anchor_type = rule_data.get('anchor', 'actual')
    value = rule_data.get('value', {})
    
    if not base_date:
        base_date = timezone.localdate()
    elif isinstance(base_date, datetime):
        base_date = base_date.date()
        
    if use_anchor and anchor_type == 'scheduled' and data_object.next_maintenance_date:
        base_date = data_object.next_maintenance_date

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
            logger.warning("Правило %s содержит нечисловой интервал: %r", data_object.date_update_rule_id, value)
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

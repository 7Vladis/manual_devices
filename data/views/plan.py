"""План ТО: показатели, фильтры и сгруппированный список работ."""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.shortcuts import render
from django.utils import timezone

from ..models import ActionHistory, DataObject

from .common import page_of


def get_period_limits(period_type):
    """Возвращает даты начала и конца периода (типа date)"""
    today = timezone.localdate()
    if period_type == 'week':
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period_type == 'month':
        start = today.replace(day=1)
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    else:
        start = end = today
    return start, end


def group_by_urgency(objects, today):
    """
    Раскладывает объекты по срочности: просроченные, сегодня, ближайшая
    неделя, позже, без срока. Пустые группы не возвращаются.

    Каждому объекту добавляется days_left — расстояние до планового ТО
    в днях. Шаблон рисует по нему датовую рейку и не пересчитывает даты
    сам: разница дат в шаблонах Django выражается только через timeuntil,
    а это уже готовая фраза, а не число.
    """
    buckets = [
        ('overdue', 'Просрочено', []),
        ('today', 'Сегодня', []),
        ('week', 'Ближайшие 7 дней', []),
        ('later', 'Позже', []),
        ('none', 'Срок не назначен', []),
    ]
    by_key = {key: items for key, _, items in buckets}

    for obj in objects:
        date = obj.next_maintenance_date
        if date is None:
            obj.days_left = None
            by_key['none'].append(obj)
            continue

        delta = (date - today).days
        obj.days_left = delta
        # Подпись рейки считаем здесь: в шаблоне знак минуса и слово «дн»
        # пришлось бы собирать условиями вокруг каждой строки.
        if delta == 0:
            obj.days_label = 'сегодня'
        elif delta < 0:
            obj.days_label = f'\u2212{-delta} дн'
        else:
            obj.days_label = f'+{delta} дн'

        if delta < 0:
            by_key['overdue'].append(obj)
        elif delta == 0:
            by_key['today'].append(obj)
        elif delta <= 7:
            by_key['week'].append(obj)
        else:
            by_key['later'].append(obj)

    return [
        {'key': key, 'title': title, 'objects': items}
        for key, title, items in buckets if items
    ]


def plan_counters(request=None):
    """
    Показатели плана ТО: просрочка, выполнение недели и месяца, всего
    объектов. Считаются и для страницы плана, и для OOB-обновления
    полосы фильтров после фиксации ТО — иначе цифры расходятся со списком.
    """
    today = timezone.localdate()

    def get_stats(period):
        """
        Выполнение плана ТО за период.

        Считаем плановые СОБЫТИЯ, а не записи истории:
        - выполненные — различные пары (объект, плановая дата), закрытые
          плановым ТО; несколько работ по одному событию дают одну единицу,
          внеплановые работы и списания из YouTrack не учитываются вовсе;
        - всего — выполненные плюс ещё не закрытые события периода
          (объекты, у которых срок ТО приходится на этот период).
        """
        start, end = get_period_limits(period)

        completed_events = set(
            ActionHistory.objects.filter(
                action_type='maintenance',
                maintenance_kind='planned',
                planned_for__range=(start, end),
            ).values_list('data_object_id', 'planned_for')
        )
        completed = len(completed_events)

        open_events = set(
            DataObject.objects.filter(
                next_maintenance_date__range=(start, end)
            ).values_list('uuid', 'next_maintenance_date')
        )
        # Объект могли обслужить и тут же снова запланировать на этот период —
        # закрытое и открытое события считаем отдельно, дубли исключаем.
        planned = len(completed_events | open_events)

        return completed, planned

    week_done, week_all = get_stats('week')
    month_done, month_all = get_stats('month')

    return {
        'today': today,
        'total_objects': DataObject.objects.count(),
        'overdue_count': DataObject.objects.filter(next_maintenance_date__lt=today).count(),
        'week_done': week_done,
        'week_all': week_all,
        'week_percent': (week_done / week_all * 100) if week_all > 0 else 0,
        'month_done': month_done,
        'month_all': month_all,
        'month_percent': (month_done / month_all * 100) if month_all > 0 else 0,
    }


@login_required
def dashboard(request):
    context = plan_counters(request)
    context['active_period'] = 'week'
    return render(request, 'data/dashboard.html', context)


@login_required
def maintenance_list(request):
    period = request.GET.get('period', 'week')
    today = timezone.localdate()
    
    if period == 'overdue':
        objects = DataObject.objects.filter(next_maintenance_date__lt=today)
        label = "Просроченные ТО"
    elif period == 'all':
        objects = DataObject.objects.all()
        label = "Все объекты системы"
    elif period in ('week', 'month'):
        # Границы периода те же, что в счётчиках дашборда: раньше список брал
        # всё до конца периода (включая просроченное), и число на плитке
        # не совпадало с длиной списка. Просроченные — отдельная плитка.
        start, end = get_period_limits(period)
        objects = DataObject.objects.filter(next_maintenance_date__range=(start, end))
        label = "План на неделю" if period == 'week' else "План на месяц"
    else:
        # Неизвестное значение приводим к неделе целиком, а не только список:
        # иначе фильтр в полосе показателей оставался бы без подсветки,
        # а перезагрузка списка ушла бы по несуществующему периоду.
        period = 'week'
        start, end = get_period_limits(period)
        objects = DataObject.objects.filter(next_maintenance_date__range=(start, end))
        label = "План на неделю"
        
    objects = objects.select_related('model', 'model__object_type').order_by(
        F('next_maintenance_date').asc(nulls_last=True)
    )

    # Весь парк в одном ответе — это сотни килобайт разметки, поэтому список
    # отдаётся страницами. Группы по срочности строятся уже по странице:
    # сортировка по дате держит их сплошными.
    page = page_of(objects, request.GET.get('page'))

    context = plan_counters(request)
    context.update({
        'active_period': period,
        'oob_filters': True,
        'page': page,
        'groups': group_by_urgency(page.object_list, today),
        # Полный набор остаётся в контексте: тесты и любой будущий потребитель
        # ждут привычный ключ, а шаблон рисует группы.
        'objects': objects,
        'total': page.paginator.count,
        'period': period,
        'period_label': label,
    })
    return render(request, 'data/includes/maintenance_table.html', context)

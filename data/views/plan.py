"""План ТО: показатели, фильтры и сгруппированный список работ."""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.shortcuts import render
from django.utils import timezone

from ..models import ActionHistory, DataObject, MaintenancePlan

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


def group_by_urgency(plans, today):
    """
    Раскладывает работы по срочности: просроченные, сегодня, ближайшая
    неделя, позже, без срока. Пустые группы не возвращаются.

    Раскладываются именно работы, а не объекты: у насоса осмотр может быть
    просрочен, а смазка — только на будущей неделе, и одной строкой это
    не выразить.

    Каждой работе добавляется days_left — расстояние до срока в днях.
    Шаблон рисует по нему датовую рейку и не пересчитывает даты сам: разница
    дат в шаблонах Django выражается только через timeuntil, а это уже
    готовая фраза, а не число.
    """
    buckets = [
        ('overdue', 'Просрочено', []),
        ('today', 'Сегодня', []),
        ('week', 'Ближайшие 7 дней', []),
        ('later', 'Позже', []),
        ('none', 'Срок не назначен', []),
    ]
    by_key = {key: items for key, _, items in buckets}

    for plan in plans:
        date = plan.next_date
        if date is None:
            plan.days_left = None
            by_key['none'].append(plan)
            continue

        delta = (date - today).days
        plan.days_left = delta
        # Подпись рейки считаем здесь: в шаблоне знак минуса и слово «дн»
        # пришлось бы собирать условиями вокруг каждой строки.
        if delta == 0:
            plan.days_label = 'сегодня'
        elif delta < 0:
            plan.days_label = f'\u2212{-delta} дн'
        else:
            plan.days_label = f'+{delta} дн'

        if delta < 0:
            by_key['overdue'].append(plan)
        elif delta == 0:
            by_key['today'].append(plan)
        elif delta <= 7:
            by_key['week'].append(plan)
        else:
            by_key['later'].append(plan)

    return [
        {'key': key, 'title': title, 'plans': items}
        for key, title, items in buckets if items
    ]


def unplanned_row(obj):
    """
    Строка списка для объекта, которому ТО не назначено.

    Это не сохранённый план, а способ показать такой объект в одном списке
    с работами: в группе «Срок не назначен» он и нужен. Признак `no_plan`
    отличает его от плана со сроком вручную — у того работа назначена,
    просто дату двигает человек.
    """
    row = MaintenancePlan(data_object=obj)
    row.no_plan = True
    return row


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

        Считаем плановые СОБЫТИЯ, а не записи истории. Событие — это тройка
        «объект, правило, дата»: у насоса осмотр и смазка в один день это две
        работы, и пара «объект + дата» склеила бы их в одну.
        - выполненные — различные тройки, закрытые плановым ТО; несколько
          работ по одному событию дают одну единицу, внеплановые работы и
          списания из YouTrack не учитываются вовсе;
        - всего — выполненные плюс ещё не закрытые события периода (планы,
          срок которых приходится на этот период).

        У записей, сделанных до появления планов, правило пустое — как и у
        плана со сроком вручную, так что старая история продолжает совпадать
        со своим планом.
        """
        start, end = get_period_limits(period)

        completed_events = set(
            ActionHistory.objects.filter(
                action_type='maintenance',
                maintenance_kind='planned',
                planned_for__range=(start, end),
            ).values_list('data_object_id', 'maintenance_rule_id', 'planned_for')
        )
        completed = len(completed_events)

        open_events = set(
            MaintenancePlan.objects.filter(
                next_date__range=(start, end)
            ).values_list('data_object_id', 'rule_id', 'next_date')
        )
        # Работу могли выполнить и тут же снова запланировать на этот период —
        # закрытое и открытое события считаем отдельно, дубли исключаем.
        planned = len(completed_events | open_events)

        return completed, planned

    week_done, week_all = get_stats('week')
    month_done, month_all = get_stats('month')

    return {
        'today': today,
        'total_objects': DataObject.objects.count(),
        # Просрочены работы, а не объекты: у одного объекта их может быть
        # просрочено сразу несколько.
        'overdue_count': MaintenancePlan.objects.filter(next_date__lt=today).count(),
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
    """
    Список работ, а не объектов.

    Строка — это план: у насоса осмотр может быть просрочен, а смазка стоять
    на будущей неделе, и одной строкой на объект такое не показать.
    """
    period = request.GET.get('period', 'week')
    today = timezone.localdate()
    plans = MaintenancePlan.objects.all()
    planless = []

    if period == 'overdue':
        plans = plans.filter(next_date__lt=today)
        label = "Просроченные работы"
    elif period == 'all':
        label = "Все работы и объекты"
        # Объекты без планов тоже показываем: «кому ТО не назначено» —
        # ровно тот вопрос, с которым сюда и приходят. Планом такой объект
        # не представлен, поэтому строку собираем вручную.
        planless = [
            unplanned_row(obj)
            for obj in DataObject.objects.filter(plans__isnull=True)
            .select_related('model', 'model__object_type').order_by('name')
        ]
    elif period in ('week', 'month'):
        # Границы периода те же, что в счётчиках дашборда: раньше список брал
        # всё до конца периода (включая просроченное), и число на плитке
        # не совпадало с длиной списка. Просроченные — отдельная плитка.
        start, end = get_period_limits(period)
        plans = plans.filter(next_date__range=(start, end))
        label = "План на неделю" if period == 'week' else "План на месяц"
    else:
        # Неизвестное значение приводим к неделе целиком, а не только список:
        # иначе фильтр в полосе показателей оставался бы без подсветки,
        # а перезагрузка списка ушла бы по несуществующему периоду.
        period = 'week'
        start, end = get_period_limits(period)
        plans = plans.filter(next_date__range=(start, end))
        label = "План на неделю"

    plans = plans.select_related(
        'rule', 'data_object', 'data_object__model', 'data_object__model__object_type'
    ).order_by(F('next_date').asc(nulls_last=True))

    # Весь парк в одном ответе — это сотни килобайт разметки, поэтому список
    # отдаётся страницами. Группы по срочности строятся уже по странице:
    # сортировка по дате держит их сплошными. Объекты без планов — хвост
    # списка: у них срока нет, и в группу «Срок не назначен» они и попадут.
    rows = list(plans) + planless if planless else plans
    page = page_of(rows, request.GET.get('page'))

    context = plan_counters(request)
    context.update({
        'active_period': period,
        'oob_filters': True,
        'page': page,
        'groups': group_by_urgency(page.object_list, today),
        'total': page.paginator.count,
        'period': period,
        'period_label': label,
    })
    return render(request, 'data/includes/maintenance_table.html', context)

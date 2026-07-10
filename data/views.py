from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import HttpResponse
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta, datetime
from .models import DataObject, ActionHistory

def get_period_limits(period_type):
    """Вспомогательная функция для получения границ дат"""
    now = timezone.now()
    today = now.date()
    
    if period_type == 'week':
        # Текущая неделя: Пн - Вс
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period_type == 'month':
        # Текущий месяц: 1-е число - конец месяца
        start = today.replace(day=1)
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    else: # today
        start = end = today
        
    # Превращаем даты в datetime (начало и конец дня) для фильтрации
    start_dt = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    return start_dt, end_dt

@login_required
def dashboard(request):
    now = timezone.now()
    # 1. Общее количество
    total_objects = DataObject.objects.count()
    
    # 2. Количество просроченных
    overdue_count = DataObject.objects.filter(next_maintenance_date__lt=now).count()
    
    # 3. Статистика планов (как была)
    def get_stats(period):
        start, end = get_period_limits(period)
        planned = DataObject.objects.filter(next_maintenance_date__range=(start, end)).count()
        completed = ActionHistory.objects.filter(
            created_at__range=(start, end),
            action__icontains="Техническое обслуживание выполнено"
        ).count()
        return completed, planned

    week_done, week_all = get_stats('week')
    month_done, month_all = get_stats('month')

    context = {
        'total_objects': total_objects,
        'overdue_count': overdue_count, # Новое поле
        'week_done': week_done,
        'week_all': week_all,
        'week_percent': (week_done / week_all * 100) if week_all > 0 else 0,
        'month_done': month_done,
        'month_all': month_all,
        'month_percent': (month_done / month_all * 100) if month_all > 0 else 0,
    }
    return render(request, 'data/dashboard.html', context)

@login_required
def maintenance_list(request):
    period = request.GET.get('period', 'week')
    now = timezone.now()
    
    # Логика фильтрации
    if period == 'overdue':
        objects = DataObject.objects.filter(next_maintenance_date__lt=now)
        label = "Просроченные ТО"
    elif period == 'all':
        objects = DataObject.objects.all()
        label = "Все объекты системы"
    elif period == 'month':
        _, end = get_period_limits('month')
        objects = DataObject.objects.filter(next_maintenance_date__lte=end)
        label = "План на месяц"
    else: # week
        _, end = get_period_limits('week')
        objects = DataObject.objects.filter(next_maintenance_date__lte=end)
        label = "План на неделю"
        
    objects = objects.select_related('model', 'model__object_type').order_by('next_maintenance_date')

    return render(request, 'data/includes/maintenance_table.html', {
        'objects': objects,
        'now': now,
        'period_label': label
    })

@login_required
def search_view(request):
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return HttpResponse('')

    # Умный поиск: 
    # 1. Сначала ищем полное совпадение фразы
    base_filters = (
        Q(name__icontains=query) |
        Q(inventory_number__icontains=query) |
        Q(model__name__icontains=query) |
        Q(model__specifications__icontains=query) |
        Q(comments__text__icontains=query) |
        Q(actions__action__icontains=query)
    )
    
    results = DataObject.objects.filter(base_filters).distinct()
    
    # 2. Если результатов мало, ищем по отдельным словам
    if results.count() < 3:
        words = query.split()
        if len(words) > 1:
            word_filters = Q()
            for word in words:
                word_filters |= (
                    Q(name__icontains=word) | 
                    Q(model__name__icontains=word) |
                    Q(model__specifications__icontains=word)
                )
            results = (results | DataObject.objects.filter(word_filters)).distinct()

    return render(request, 'data/includes/search_results_list.html', {'results': results[:10]})

@login_required
def dict_view(request):
    """Представление для страницы справочника"""
    return render(request, 'data/dict.html')
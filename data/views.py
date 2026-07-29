from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.http import HttpResponse, HttpResponseForbidden
from django.db.models import Q, Prefetch, Count
from django.utils import timezone
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta
from users.decorators import role_required
from .models import DateUpdateRule, DataObject, ActionHistory, ObjectModel, ObjectType, Relation, DependencyType, Attachment, Comment

@login_required
@role_required(['senior', 'admin', 'superuser'])  # Младший инженер не имеет доступа к настройкам вообще
def settings_page(request):
    """Единый интерактивный центр управления системой (без JS, горизонтальные вкладки)"""
    active_tab = request.GET.get('tab', 'rules')
    
    # Защита вкладок на уровне бэкенда для Старшего инженера
    if active_tab in ['notifications', 'users'] and not request.user.is_admin_or_higher:
        active_tab = 'rules'  # Старшего инженера сбрасываем на доступную ему вкладку правил

    context = {'active_tab': active_tab}

    # 1. ВКЛАДКА: Правила планирования ТО
    if active_tab == 'rules':
        rules_query = DateUpdateRule.objects.prefetch_related('data_objects').annotate(
            objects_count=Count('data_objects')
        ).order_by('name')
        
        month_names = {
            1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
            5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
            9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря'
        }
        
        for r in rules_query:
            rule_data = r.rule or {}
            if rule_data.get('strategy') == 'fixed':
                dates_list = rule_data.get('value', [])
                formatted_dates = []
                for d in dates_list:
                    day = d.get('day', 1)
                    month_num = d.get('month', 1)
                    month_name = month_names.get(month_num, '')
                    formatted_dates.append(f"{day} {month_name}")
                r.formatted_fixed_dates = ", ".join(formatted_dates)
                
        context['rules'] = rules_query

    # 2. ВКЛАДКА: Типы объектов
    elif active_tab == 'object_types':
        context['object_types'] = ObjectType.objects.annotate(
            models_count=Count('models')
        ).order_by('type')

    # 3. ВКЛАДКА: Типы связей (взаимодействия)
    elif active_tab == 'dependency_types':
        context['dependency_types'] = DependencyType.objects.annotate(
            relations_count=Count('relations')
        ).order_by('type')

    # 4. ВКЛАДКА: Уведомления Mattermost (Только для Админов и Суперпользователей)
    elif active_tab == 'notifications' and request.user.is_admin_or_higher:
        from notifications.models import MattermostSetting
        context['settings'] = MattermostSetting.objects.all().order_by('-updated_at')

    # 5. ВКЛАДКА: Пользователи (Только для Админов и Суперпользователей)
    elif active_tab == 'users' and request.user.is_admin_or_higher:
        User = get_user_model()
        context['users_list'] = User.objects.all().order_by('username')

    if request.headers.get('HX-Request'):
        return render(request, 'data/settings/settings_layout_inner.html', context)
    return render(request, 'data/settings.html', context)

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_object_type_view(request):
    """Создание нового типа оборудования из настроек"""
    if request.method == 'POST':
        name = request.POST.get('type', '').strip()
        if name:
            ObjectType.objects.get_or_create(type=name)
            
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=object_types'
    return response

@login_required
@role_required(['senior', 'admin', 'superuser'])
def delete_object_type_view(request, pk):
    """Удаление типа оборудования с проверкой на использование"""
    obj_type = get_object_or_404(ObjectType, pk=pk)
    if obj_type.models.exists():
        return HttpResponse(
            '<div class="alert alert-danger py-2 px-3 m-0 rounded-3 small animate-fade">'
            '<i class="bi bi-exclamation-triangle-fill me-1"></i> '
            'Нельзя удалить тип: он привязан к существующим моделям оборудования!'
            '</div>', 
            status=400
        )
    obj_type.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=object_types'
    return response

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_dependency_type_view(request):
    """Создание нового типа связи из настроек"""
    if request.method == 'POST':
        name = request.POST.get('type', '').strip()
        if name:
            DependencyType.objects.get_or_create(type=name)
            
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=dependency_types'
    return response

@login_required
@role_required(['senior', 'admin', 'superuser'])
def delete_dependency_type_view(request, pk):
    """Удаление типа связи с защитой целостности данных"""
    dep_type = get_object_or_404(DependencyType, pk=pk)
    if dep_type.relations.exists():
        return HttpResponse(
            '<div class="alert alert-danger py-2 px-3 m-0 rounded-3 small animate-fade">'
            '<i class="bi bi-exclamation-triangle-fill me-1"></i> '
            'Нельзя удалить связь: она используется в активных отношениях между объектами!'
            '</div>', 
            status=400
        )
    dep_type.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=dependency_types'
    return response

def parse_rule_from_request(request):
    """Вспомогательная функция для разбора параметров правила планирования из POST-запроса"""
    strategy = request.POST.get('new_rule_strategy', 'relative')
    if strategy == 'relative':
        anchor = request.POST.get('new_rule_anchor', 'actual')
        try:
            years = int(request.POST.get('new_rule_years', 0))
        except (ValueError, TypeError):
            years = 0
        try:
            months = int(request.POST.get('new_rule_months', 6))
        except (ValueError, TypeError):
            months = 6
        try:
            days = int(request.POST.get('new_rule_days', 0))
        except (ValueError, TypeError):
            days = 0
            
        return {
            "strategy": "relative",
            "anchor": anchor,
            "value": {
                "years": years,
                "months": months,
                "days": days
            }
        }
    elif strategy == 'fixed':
        fixed_months = request.POST.getlist('fixed_months')
        fixed_days = request.POST.getlist('fixed_days')
        
        dates_list = []
        for m, d in zip(fixed_months, fixed_days):
            try:
                dates_list.append({"month": int(m), "day": int(d)})
            except (ValueError, TypeError):
                pass
                
        return {
            "strategy": "fixed",
            "anchor": "yearly",
            "value": dates_list
        }
    return {"strategy": "relative", "anchor": "actual", "value": {"years": 0, "months": 6, "days": 0}}

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_rule_settings_view(request):
    """Создание нового правила планирования ТО из настроек"""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            rule_json = parse_rule_from_request(request)
            DateUpdateRule.objects.get_or_create(
                name=name,
                defaults={"rule": rule_json}
            )
            
        response = HttpResponse()
        response['HX-Redirect'] = '/settings/?tab=rules'
        return response

    strategy = 'relative'
    anchor = 'actual'
    years = 0
    months = 6
    days = 0
    fixed_dates = []
    
    month_names = {
        1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
        5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
        9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря'
    }
    
    return render(request, 'data/settings/create_rule_modal.html', {
        'strategy': strategy,
        'anchor': anchor,
        'years': years,
        'months': months,
        'days': days,
        'fixed_dates': fixed_dates,
        'month_names': month_names,
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_rule_settings_view(request, pk):
    """Редактирование параметров правила планирования ТО"""
    rule_obj = get_object_or_404(DateUpdateRule, pk=pk)
    
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        strategy = request.POST.get('new_rule_strategy', 'relative')
        
        if name:
            rule_obj.name = name
            
        if strategy == 'relative':
            anchor = request.POST.get('new_rule_anchor', 'actual')
            years = int(request.POST.get('new_rule_years', 0))
            months = int(request.POST.get('new_rule_months', 6))
            days = int(request.POST.get('new_rule_days', 0))
            
            rule_json = {
                "strategy": "relative",
                "anchor": anchor,
                "value": {
                    "years": years,
                    "months": months,
                    "days": days
                }
            }
        elif strategy == 'fixed':
            fixed_months = request.POST.getlist('fixed_months')
            fixed_days = request.POST.getlist('fixed_days')
            
            dates_list = []
            for m, d in zip(fixed_months, fixed_days):
                dates_list.append({"month": int(m), "day": int(d)})
                
            rule_json = {
                "strategy": "fixed",
                "anchor": "yearly",
                "value": dates_list
            }
            
        rule_obj.rule = rule_json
        rule_obj.save()
        
        for obj in rule_obj.data_objects.all():
            obj.next_maintenance_date = calculate_next_maintenance_date(obj, base_date=timezone.now())
            obj.save()
            
        response = HttpResponse()
        response['HX-Redirect'] = '/settings/?tab=rules'
        return response

    rule_data = rule_obj.rule or {}
    strategy = rule_data.get('strategy', 'relative')
    anchor = rule_data.get('anchor', 'actual')
    val = rule_data.get('value', {})
    
    years = val.get('years', 0) if strategy == 'relative' else 0
    months = val.get('months', 6) if strategy == 'relative' else 0
    days = val.get('days', 0) if strategy == 'relative' else 0
    
    fixed_dates = val if strategy == 'fixed' else []
    
    month_names = {
        1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
        5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
        9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря'
    }
    
    return render(request, 'data/settings/edit_rule_modal.html', {
        'rule_obj': rule_obj,
        'strategy': strategy,
        'anchor': anchor,
        'years': years,
        'months': months,
        'days': days,
        'fixed_dates': fixed_dates,
        'month_names': month_names,
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])
def delete_rule_view(request, pk):
    """Удаление правила планирования"""
    rule = get_object_or_404(DateUpdateRule, pk=pk)
    if rule.data_objects.exists():
        return HttpResponse(
            '<div class="alert alert-danger py-2 px-3 m-0 rounded-3 small animate-fade">'
            '<i class="bi bi-exclamation-triangle-fill me-1"></i> '
            'Нельзя удалить: правило используется в активных объектах!'
            '</div>', 
            status=400
        )
    rule.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=rules'
    return response

@login_required
@role_required(['admin', 'superuser'])
def export_modal_view(request):
    """Модальное окно выбора опции экспорта данных в XLSX"""
    export_url = reverse('export_xlsx')
    return HttpResponse(f'''
        <div class="modal fade" id="exportModal" tabindex="-1" aria-labelledby="exportModalLabel" aria-hidden="true">
            <div class="modal-dialog">
                <div class="modal-content border-0 shadow-lg rounded-3">
                    <div class="modal-header bg-dark text-white">
                        <h5 class="modal-title fs-6 fw-bold" id="exportModalLabel">
                            <i class="bi bi-file-earmark-excel-fill text-success me-2"></i> Экспорт данных в XLSX
                        </h5>
                        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Закрыть"></button>
                    </div>
                    <form action="{export_url}" method="get">
                        <div class="modal-body p-4">
                            <p class="text-muted small mb-3">
                                Выберите режим экспорта объектов системы. Отчет будет сформирован и скачан в формате Excel.
                            </p>
                            <div class="mb-3">
                                <label class="form-label fw-semibold small text-dark mb-2">Режим отбора объектов:</label>
                                <div class="form-check mb-2">
                                    <input class="form-check-input" type="radio" name="export_mode" id="modeAll" value="all" checked>
                                    <label class="form-check-label small" for="modeAll" style="cursor: pointer;">
                                        Экспортировать все объекты (включая без инвентарного номера)
                                    </label>
                                </div>
                                <div class="form-check">
                                    <input class="form-check-input" type="radio" name="export_mode" id="modeWithInv" value="with_inventory">
                                    <label class="form-check-label small" for="modeWithInv" style="cursor: pointer;">
                                        Только с заполненным инвентарным номером
                                    </label>
                                </div>
                            </div>
                        </div>
                        <div class="modal-footer bg-light px-4 py-3">
                            <button type="button" class="btn btn-secondary btn-sm px-3" data-bs-dismiss="modal">Отмена</button>
                            <button type="submit" class="btn btn-success btn-sm px-4 fw-bold" onclick="bootstrap.Modal.getInstance(document.getElementById('exportModal')).hide();">
                                <i class="bi bi-download me-1"></i> Скачать XLSX
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    ''')

@login_required
@role_required(['admin', 'superuser'])
def export_xlsx_view(request):
    """Генерация и отдача XLSX файла с данными оборудования"""
    import openpyxl
    from django.http import HttpResponse

    export_mode = request.GET.get('export_mode', 'all')
    
    queryset = DataObject.objects.select_related('model', 'model__object_type').all()
    if export_mode == 'with_inventory':
        queryset = queryset.filter(inventory_number__isnull=False).exclude(inventory_number='')
    
    queryset = queryset.order_by('inventory_number', 'name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Оборудование"

    # Заголовки столбцов
    headers = [
        "Инвентарный номер",
        "Название объекта (DataObject.name)",
        "Название модели (ObjectModel.name)",
        "Характеристики спецификации (JSON)"
    ]
    ws.append(headers)

    # Настройка стилей шапки
    for col_num in range(1, 5):
        cell = ws.cell(row=1, column=col_num)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    for obj in queryset:
        inv_num = obj.inventory_number or ""
        obj_name = obj.name or ""
        model_name = obj.model.name if obj.model else ""
        
        # Парсим JSON specifications в строку вида "ключ: значение; ключ2: значение2"
        specs = obj.model.specifications if obj.model and obj.model.specifications else {}
        specs_str_list = []
        if isinstance(specs, dict):
            for k, v in specs.items():
                specs_str_list.append(f"{k}: {v}")
        specs_formatted = "; ".join(specs_str_list)

        ws.append([inv_num, obj_name, model_name, specs_formatted])

    # Автоподгонка ширины столбцов
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 15)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"manual_devices_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    wb.save(response)
    return response


# --- ДАШБОРД (Доступен всем авторизованным пользователям) ---

def get_period_limits(period_type):
    now = timezone.now()
    today = now.date()
    if period_type == 'week':
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period_type == 'month':
        start = today.replace(day=1)
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    else:
        start = end = today
    start_dt = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    return start_dt, end_dt

@login_required
def dashboard(request):
    now = timezone.now()
    total_objects = DataObject.objects.count()
    overdue_count = DataObject.objects.filter(next_maintenance_date__lt=now).count()
    
    def get_stats(period):
        start, end = get_period_limits(period)
        # Выполненные ТО за период
        completed = ActionHistory.objects.filter(
            created_at__range=(start, end),
            action__icontains="Техническое обслуживание выполнено"
        ).count()
        current_planned = DataObject.objects.filter(next_maintenance_date__range=(start, end)).count()
        planned = current_planned + completed
        return completed, planned

    week_done, week_all = get_stats('week')
    month_done, month_all = get_stats('month')

    context = {
        'total_objects': total_objects,
        'overdue_count': overdue_count,
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
    else:
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

    base_filters = (
        Q(name__icontains=query) |
        Q(inventory_number__icontains=query) |
        Q(descriotion__icontains=query) |
        Q(model__name__icontains=query) |
        Q(model__specifications__icontains=query) |
        Q(comments__text__icontains=query) |
        Q(actions__action__icontains=query)
    )
    
    results = DataObject.objects.filter(base_filters).distinct()
    
    if results.count() < 3:
        words = query.split()
        if len(words) > 1:
            word_filters = Q()
            for word in words:
                word_filters |= (
                    Q(name__icontains=word) | 
                    Q(description__icontains=word) | 
                    Q(model__name__icontains=word) |
                    Q(model__specifications__icontains=word)
                )
            results = (results | DataObject.objects.filter(word_filters)).distinct()

    return render(request, 'data/includes/search_results_list.html', {'results': results[:10]})


# --- СПРАВОЧНИК И ПРОВОДНИК (Доступен всем на чтение) ---

@login_required
def dict_view(request):
    active_tab = request.GET.get('tab', 'objects')
    
    selected_object_id = request.GET.get('object')
    selected_model_id = request.GET.get('model')
    
    active_object = None
    active_model = None
    parent_uuids = []
    
    if selected_object_id:
        active_tab = 'objects'
        active_object = get_object_or_404(DataObject, pk=selected_object_id)
        
        current = active_object
        while True:
            relation = Relation.objects.filter(subject=current).select_related('main').first()
            if relation:
                parent_uuids.append(str(relation.main.pk))
                current = relation.main
            else:
                break
                
    elif selected_model_id:
        active_tab = 'models'
        active_model = get_object_or_404(ObjectModel, pk=selected_model_id)
    
    models = ObjectModel.objects.all().order_by('name')
    object_types = ObjectType.objects.all().order_by('type')
    
    context = {
        'models': models,
        'object_types': object_types,
        'active_tab': active_tab,
        'active_object': active_object,
        'active_model': active_model,
        'parent_uuids': parent_uuids,
    }
    
    if active_tab == 'models':
        context['object_types_list'] = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
    else:
        context['initial_objects'] = DataObject.objects.exclude(
            main_relations__isnull=False
        ).prefetch_related('subject_relations').order_by('name')
        
    if request.headers.get('HX-Request') and request.GET.get('sidebar'):
        return render(request, 'data/tree/dict_sidebar.html', context)
        
    return render(request, 'data/dict.html', context)

@login_required
def object_tree_view(request):
    roots = DataObject.objects.exclude(
        main_relations__isnull=False
    ).prefetch_related('subject_relations').order_by('name')
    return render(request, 'data/tree/object_tree_list.html', {'objects': roots})

@login_required
def object_children_view(request, parent_uuid):
    parent = get_object_or_404(DataObject, pk=parent_uuid)
    children = DataObject.objects.filter(
        main_relations__main=parent
    ).prefetch_related('subject_relations').order_by('name')
    
    active_object_id = request.GET.get('active_object')
    active_object = None
    parent_uuids = []
    
    if active_object_id:
        try:
            active_object = DataObject.objects.get(pk=active_object_id)
            current = active_object
            while True:
                relation = Relation.objects.filter(subject=current).select_related('main').first()
                if relation:
                    parent_uuids.append(str(relation.main.pk))
                    current = relation.main
                else:
                    break
        except DataObject.DoesNotExist:
            pass

    return render(request, 'data/tree/object_tree_list_nodes.html', {
        'objects': children,
        'parent': parent,
        'active_object': active_object,
        'parent_uuids': parent_uuids,
    })

@login_required
def model_tree_view(request):
    object_types = ObjectType.objects.prefetch_related(
        Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
    ).order_by('type')
    return render(request, 'data/tree/model_tree_list.html', {'object_types': object_types})


# --- ОБСЛУЖИВАНИЕ (Доступно всем ролям, включая Младшего инженера) ---

@login_required
def service_object_view(request, pk):
    """Обслуживание объекта: вывод формы и обработка сохранения"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        date_str = request.POST.get('maintenance_date')
        if date_str:
            maintenance_date = timezone.make_aware(datetime.strptime(date_str, '%Y-%m-%d').replace(hour=13, minute=0))
            obj.next_maintenance_date = maintenance_date
            obj.save()
            
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action=f"Техническое обслуживание выполнено. Следующее ТО запланировано на {maintenance_date.strftime('%d.%m.%Y')}."
            )
        
        node_html = render_to_string('data/tree/object_tree_node.html', {'node': obj}, request=request)
        
        date_display_str = obj.next_maintenance_date.strftime('%d.%m.%Y') if obj.next_maintenance_date else "Не запланировано"
        oob_date_html = f'<strong id="maintenance-date-display" class="text-danger" hx-swap-oob="innerHTML">{date_display_str}</strong>'
        
        response = HttpResponse(node_html + "\n" + oob_date_html)
        response['HX-Trigger'] = 'objectServiced'
        return response

    proposed_date = calculate_next_maintenance_date(obj)
    
    return render(request, 'data/tree/service_modal_body.html', {
        'obj': obj,
        'proposed_date': proposed_date
    })


# --- УДАЛЕНИЕ ОБЪЕКТОВ И МОДЕЛЕЙ (Только Senior, Admin, Superuser) ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def delete_object_view(request, pk):
    if request.method in ['POST', 'DELETE']:
        obj = get_object_or_404(DataObject, pk=pk)
        obj.delete()
        return HttpResponse("", status=200)
    return HttpResponse("Метод не разрешен", status=405)

@login_required
@role_required(['senior', 'admin', 'superuser'])
def delete_model_view(request, pk):
    if request.method in ['POST', 'DELETE']:
        model_obj = get_object_or_404(ObjectModel, pk=pk)
        model_obj.delete()
        return HttpResponse("", status=200)
    return HttpResponse("Метод не разрешен", status=405)


# --- СОЗДАНИЕ ОБЪЕКТОВ И МОДЕЛЕЙ (Только Senior, Admin, Superuser) ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_object_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        model_uuid = request.POST.get('model')
        inventory_number = request.POST.get('inventory_number')
        parent_uuid = request.POST.get('parent')
        maintenance_str = request.POST.get('next_maintenance_date')
        
        dep_type_uuid = request.POST.get('dependency_type')
        new_dep_type_name = request.POST.get('new_dependency_type')
        
        rule_uuid = request.POST.get('date_update_rule')
        new_rule_name = request.POST.get('new_date_update_rule')
        
        model_obj = get_object_or_404(ObjectModel, pk=model_uuid)
        
        next_maintenance_date = None
        if maintenance_str:
            try:
                next_maintenance_date = timezone.make_aware(
                    datetime.strptime(maintenance_str, '%Y-%m-%d').replace(hour=13, minute=0)
                )
            except ValueError:
                pass

        selected_rule = None
        if rule_uuid:
            selected_rule = get_object_or_404(DateUpdateRule, pk=rule_uuid)
        elif new_rule_name:
            strategy = request.POST.get('new_rule_strategy', 'relative')
            
            if strategy == 'relative':
                anchor = request.POST.get('new_rule_anchor', 'actual')
                years = int(request.POST.get('new_rule_years', 0))
                months = int(request.POST.get('new_rule_months', 6))
                days = int(request.POST.get('new_rule_days', 0))
                
                rule_json = {
                    "strategy": "relative",
                    "anchor": anchor,
                    "value": {
                        "years": years,
                        "months": months,
                        "days": days
                    }
                }
            elif strategy == 'fixed':
                fixed_months = request.POST.getlist('fixed_months')
                fixed_days = request.POST.getlist('fixed_days')
                
                dates_list = []
                for m, d in zip(fixed_months, fixed_days):
                    dates_list.append({"month": int(m), "day": int(d)})
                    
                rule_json = {
                    "strategy": "fixed",
                    "anchor": "yearly",
                    "value": dates_list
                }
                
            selected_rule, _ = DateUpdateRule.objects.get_or_create(
                name=new_rule_name,
                defaults={"rule": rule_json}
            )
                
        new_obj = DataObject.objects.create(
            name=name,
            model=model_obj,
            inventory_number=inventory_number if inventory_number else None,
            next_maintenance_date=next_maintenance_date,
            date_update_rule=selected_rule,
            user=request.user
        )

        scheduling_mode = request.POST.get('maintenance_scheduling_mode', 'manual')
        if scheduling_mode == 'auto' and selected_rule:
            first_date = calculate_next_maintenance_date(new_obj, base_date=timezone.now())
            new_obj.next_maintenance_date = first_date
            new_obj.save()
        
        if parent_uuid:
            parent_obj = get_object_or_404(DataObject, pk=parent_uuid)
            
            if new_dep_type_name:
                dep_type, _ = DependencyType.objects.get_or_create(type=new_dep_type_name)
            elif dep_type_uuid:
                dep_type = get_object_or_404(DependencyType, pk=dep_type_uuid)
            else:
                dep_type, _ = DependencyType.objects.get_or_create(type="Входит в состав")
                
            Relation.objects.create(
                main=parent_obj,
                subject=new_obj,
                dependency_type=dep_type
            )
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=new_obj,
            action="Объект зарегистрирован в системе через форму быстрого добавления."
        )
        
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return render(request, 'data/includes/create_object_modal_body.html')

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_model_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        type_uuid = request.POST.get('object_type')
        new_type_name = request.POST.get('new_object_type')
        
        spec_keys = request.POST.getlist('spec_keys')
        spec_values = request.POST.getlist('spec_values')
        specifications = dict(zip(spec_keys, spec_values))
        
        if new_type_name:
            object_type, _ = ObjectType.objects.get_or_create(type=new_type_name)
        else:
            object_type = get_object_or_404(ObjectType, pk=type_uuid)
            
        ObjectModel.objects.create(
            name=name,
            object_type=object_type,
            specifications=specifications
        )
        
        object_types = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
        context = {
            'object_types_list': object_types,
            'active_tab': 'models',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return render(request, 'data/includes/create_model_modal_body.html')


# --- ДЕТАЛИ ОБЪЕКТА (Доступ на чтение) ---

@login_required
def object_detail_view(request, pk):
    obj = get_object_or_404(DataObject.objects.select_related('model', 'model__object_type'), pk=pk)
    prev_active_id = request.session.get('active_object_id')
    request.session['active_object_id'] = str(pk)
    
    parent_relation = Relation.objects.filter(subject=obj).select_related('main').first()
    parent = parent_relation.main if parent_relation else None
    
    context = {
        'obj': obj,
        'parent': parent,
        'active_tab': 'short_info',
    }
    
    response_content = render_to_string('data/object/object_details.html', context, request=request)
    oob_elements = []
    
    new_node_html = render_to_string('data/tree/object_tree_node_label.html', {
        'node': obj,
        'is_active': True,
        'oob': True
    }, request=request)
    oob_elements.append(new_node_html)
    
    if prev_active_id and prev_active_id != str(pk):
        try:
            prev_obj = DataObject.objects.get(pk=prev_active_id)
            old_node_html = render_to_string('data/tree/object_tree_node_label.html', {
                'node': prev_obj,
                'is_active': False,
                'oob': True
            }, request=request)
            oob_elements.append(old_node_html)
        except DataObject.DoesNotExist:
            pass
            
    combined_content = response_content + "\n" + "\n".join(oob_elements)
    
    if request.GET.get('sidebar'):
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        combined_content = combined_content + f'\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        
    return HttpResponse(combined_content)

@login_required
@role_required(['senior', 'admin', 'superuser'])  # Отвязка правила — только для старших
def unlink_rule_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    obj.date_update_rule = None
    obj.save()
    
    ActionHistory.objects.create(
        user=request.user,
        data_object=obj,
        action="Правило автоматического расчета ТО отвязано от объекта."
    )
    
    return render(request, 'data/object/edit_rule_modal_body.html', {
        'obj': obj,
        'current_mode': 'auto',
        'rule_details': None
    })

@login_required
def object_tab_view(request, pk, tab_name):
    obj = get_object_or_404(DataObject, pk=pk)
    context = {'obj': obj}
    
    if tab_name == 'short_info':
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        context['preview'] = preview
        template = 'data/object/object_tab_short_info.html'
        
    elif tab_name == 'specs':
        context['specifications'] = obj.model.specifications or {}
        template = 'data/object/object_tab_specs.html'
        
    elif tab_name == 'comments':
        comments = obj.comments.select_related('user').order_by('-created_at')
        context['comments'] = comments
        template = 'data/object/object_tab_comments.html'
        
    elif tab_name == 'files':
        files = obj.attachments.select_related('user').order_by('-created_at')
        context['files'] = files
        template = 'data/object/object_tab_files.html'
        
    elif tab_name == 'history':
        history = obj.actions.select_related('user').order_by('-created_at')
        context['history'] = history
        template = 'data/object/object_tab_history.html'
        
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)


# --- Inline-редактирование в мини-шапке и описании (Только Senior, Admin, Superuser) ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_inventory_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        new_inv = request.POST.get('inventory_number', '').strip()
        obj.inventory_number = new_inv if new_inv else None
        obj.save()
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Изменен инвентарный номер объекта на: {new_inv or 'отсутствует'}."
        )
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': True})

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_parent_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        current_relation = Relation.objects.filter(subject=obj).select_related('main__model').first()
        parent = current_relation.main if current_relation else None
        parent_name = parent.name or parent.model.name if parent else "отсутствует"
        
        return HttpResponse(
            f'<div id="parent-display-container" hx-get="{request.path}" hx-target="#parent-display-container" hx-swap="outerHTML" style="cursor: pointer;" class="text-primary fw-semibold d-inline-block animate-fade">'
            f'{parent_name if parent else "<span class=\'text-muted italic small\'>указать родителя <i class=\'bi bi-pencil-square ms-1\'></i></span>"}'
            f'{f" <i class=\'bi bi-pencil-square ms-1 text-muted small\'></i>" if parent else ""}'
            f'</div>'
        )
        
    if request.method == 'POST':
        parent_uuid = request.POST.get('parent') or request.POST.get('parent_uuid')
        Relation.objects.filter(subject=obj).delete()
        
        parent_name = "отсутствует"
        if parent_uuid:
            parent_obj = get_object_or_404(DataObject, pk=parent_uuid)
            parent_name = parent_obj.name or parent_obj.model.name
            dep_type, _ = DependencyType.objects.get_or_create(type="Входит в состав")
            Relation.objects.create(
                main=parent_obj,
                subject=obj,
                dependency_type=dep_type
            )
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Связь изменена: назначен новый родительский объект '{parent_name}'."
        )
        
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render(request, 'data/tree/dict_sidebar.html', sidebar_context).content.decode('utf-8')
        
        response_html = f"""
            <div id="parent-display-container" hx-get="{request.path}" hx-target="#parent-display-container" hx-swap="outerHTML" style="cursor: pointer;" class="text-primary fw-semibold d-inline-block animate-fade">
                {parent_name} <i class="bi bi-pencil-square ms-1 text-muted small"></i>
            </div>
            <div id="sidebar-container" hx-swap-oob="innerHTML">
                {sidebar_html}
            </div>
        """
        return HttpResponse(response_html)

    current_relation = Relation.objects.filter(subject=obj).select_related('main__model').first()
    current_parent = current_relation.main if current_relation else None
    
    return render(request, 'data/object/inline_parent.html', {
        'obj': obj,
        'current_parent': current_parent
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])  # Запрет доступа для младших инженеров
def edit_name_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_name.html', {'obj': obj, 'editing': False})
        
    if request.method == 'POST':
        old_name = obj.name or obj.model.name
        new_name = request.POST.get('name', '').strip()
        
        if new_name and old_name != new_name:
            obj.name = new_name
            obj.save()
        
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action=f"Имя объекта изменено с '{old_name}' на '{new_name}'."
            )
            
        name_html = render_to_string('data/object/inline_name.html', {'obj': obj, 'editing': False}, request=request)
        sidebar_node_html = render_to_string('data/tree/object_tree_node_label.html', {
            'node': obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(name_html + "\n" + sidebar_node_html)

    return render(request, 'data/object/inline_name.html', {'obj': obj, 'editing': True})

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_description_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        desc = request.POST.get('description', '').strip()
        obj.description = desc if desc else None
        obj.save()
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action="Обновлено краткое описание объекта."
        )
        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': True})


# --- Вкладка: Комментарии (Доступно всем авторизованным пользователям) ---

@login_required
def add_comment_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    text = request.POST.get('text', '').strip()
    if text:
        Comment.objects.create(
            user=request.user,
            data_object=obj,
            text=text
        )
    comments = obj.comments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_comments.html', {'obj': obj, 'comments': comments})

@login_required
def edit_comment_view(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    
    # Редактировать комментарий может только его автор или администратор
    if comment.user != request.user and not request.user.is_admin_or_higher:
        return HttpResponseForbidden("Вы не можете редактировать чужие комментарии.")
        
    if request.method == 'POST':
        text = request.POST.get('text', '').strip()
        if text:
            comment.text = text
            comment.save()
        return render(request, 'data/object/comment_item.html', {'comment': comment})
        
    return render(request, 'data/object/comment_item_edit.html', {'comment': comment})

@login_required
def delete_comments_bulk(request):
    comment_ids = request.POST.getlist('comment_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    if comment_ids:
        queryset = Comment.objects.filter(uuid__in=comment_ids, data_object=obj)
        # Старший инженер, админ или суперпользователь могут удалять любые комментарии, младший — только свои
        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)
        queryset.delete()
        
    comments = obj.comments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_comments.html', {'obj': obj, 'comments': comments})


# --- Вкладка: Файлы (Доступно всем авторизованным пользователям) ---

@login_required
def add_attachment_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    file = request.FILES.get('file')
    is_preview_upload = request.POST.get('is_preview') == 'true'
    
    if file:
        if is_preview_upload:
            # Проверяем, что загружаемый файл является картинкой (изображением)
            content_type = getattr(file, 'content_type', '')
            filename_lower = file.name.lower()
            image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg')
            is_image_mime = content_type.startswith('image/')
            is_image_ext = filename_lower.endswith(image_extensions)
            
            if not (is_image_mime or is_image_ext):
                # Возвращаем ошибку в виде HTMX-фрагмента или HttpResponse с кодом 400
                return HttpResponse(
                    '<div class="alert alert-danger py-2 px-3 mb-3 rounded-3 small animate-fade">'
                    '<i class="bi bi-exclamation-triangle-fill me-1"></i> '
                    'Ошибка: файл превью должен быть изображением (картинкой)!'
                    '</div>',
                    status=400
                )
            
            Attachment.objects.filter(data_object=obj, is_preview=True).update(is_preview=False)
            
        Attachment.objects.create(
            user=request.user,
            data_object=obj,
            path=file,
            is_preview=is_preview_upload
        )
        
    if is_preview_upload:
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        return render(request, 'data/object/object_tab_short_info.html', {'obj': obj, 'preview': preview})
        
    files = obj.attachments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_files.html', {'obj': obj, 'files': files})

@login_required
def delete_attachments_bulk(request):
    file_ids = request.POST.getlist('file_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    if file_ids:
        queryset = Attachment.objects.filter(uuid__in=file_ids, data_object=obj)
        # Старший инженер, админ или суперпользователь могут удалять любые файлы, младший — только свои
        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)
        queryset.delete()
        
    files = obj.attachments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_files.html', {'obj': obj, 'files': files})


# --- ДЕТАЛИ МОДЕЛИ (Доступ на чтение) ---

@login_required
def model_detail_view(request, pk):
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    prev_active_id = request.session.get('active_model_id')
    request.session['active_model_id'] = str(pk)
    
    context = {
        'model_obj': model_obj,
        'active_tab': 'specs',
    }
    
    response_content = render_to_string('data/model/model_details.html', context, request=request)
    oob_elements = []
    
    new_node_html = render_to_string('data/tree/model_tree_node_label.html', {
        'model': model_obj,
        'is_active': True,
        'oob': True
    }, request=request)
    oob_elements.append(new_node_html)
    
    if prev_active_id and prev_active_id != str(pk):
        try:
            prev_model = ObjectModel.objects.get(pk=prev_active_id)
            old_node_html = render_to_string('data/tree/model_tree_node_label.html', {
                'model': prev_model,
                'is_active': False,
                'oob': True
            }, request=request)
            oob_elements.append(old_node_html)
        except ObjectModel.DoesNotExist:
            pass
            
    combined_content = response_content + "\n" + "\n".join(oob_elements)
    
    if request.GET.get('sidebar'):
        object_types = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
        sidebar_context = {
            'object_types_list': object_types,
            'active_tab': 'models',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        combined_content = combined_content + f'\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        
    return HttpResponse(combined_content)

@login_required
def model_tab_view(request, pk, tab_name):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    context = {'model_obj': model_obj}
    
    if tab_name == 'specs':
        context['specifications'] = model_obj.specifications or {}
        template = 'data/model/model_tab_specs.html'
    elif tab_name == 'objects':
        objects = model_obj.data_objects.all().select_related('model__object_type').order_by('name')
        context['objects'] = objects
        template = 'data/model/model_tab_objects.html'
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)


# --- JSON ХАРАКТЕРИСТИКИ МОДЕЛИ (Только Senior, Admin, Superuser) ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def model_spec_add_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    if request.method == 'POST':
        key = request.POST.get('key', '').strip()
        value = request.POST.get('value', '').strip()
        if key and value:
            specs = model_obj.specifications or {}
            specs[key] = value
            model_obj.specifications = specs
            model_obj.save()
            
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)

@login_required
@role_required(['senior', 'admin', 'superuser'])
def model_spec_edit_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.GET.get('key') or request.POST.get('key')
    
    if request.method == 'POST':
        old_key = request.POST.get('old_key')
        new_key = request.POST.get('key', '').strip()
        value = request.POST.get('value', '').strip()
        specs = model_obj.specifications or {}
        
        if old_key and new_key and value:
            if old_key != new_key:
                specs.pop(old_key, None)
            specs[new_key] = value
            model_obj.specifications = specs
            model_obj.save()
            
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)
        
    value = model_obj.specifications.get(key, '')
    return render(request, 'data/model/inline_spec_row.html', {
        'model_obj': model_obj,
        'key': key,
        'value': value,
        'editing': True
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])
def model_spec_delete_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key')
    specs = model_obj.specifications or {}
    
    if key in specs:
        specs.pop(key)
        model_obj.specifications = specs
        model_obj.save()
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications
    }
    return render(request, 'data/model/model_tab_specs.html', context)


# --- ВСПОМОГАТЕЛЬНЫЕ ПРОВЕРКИ И ПОДСКАЗКИ (Доступно всем авторизованным) ---

@login_required
def check_model_name_view(request):
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    exact_match = ObjectModel.objects.filter(name__iexact=name).first()
    if exact_match:
        return HttpResponse(
            f'<div class="alert alert-danger py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold mb-1"><i class="bi bi-x-circle-fill me-1"></i> Модель с таким названием уже существует!</div>'
            f'<a href="#" class="text-danger fw-bold" '
            f'hx-get="/dict/models/{exact_match.uuid}/?sidebar=1" '
            f'hx-target="#detail-container" '
            f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createModelModal\')).hide();">'
            f'Перейти к существующей модели: {exact_match.name} ({exact_match.object_type.type})'
            f'</a>'
            f'</div>'
        )

    stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по', 'of', 'and', 'the'}
    words = [
        w.lower() for w in name.split() 
        if len(w) >= 2 and w.lower() not in stop_words
    ]

    similar_models = []
    if words:
        query = Q()
        for word in words:
            query |= Q(name__icontains=word) | Q(object_type__type__icontains=word)
            similar_models = ObjectModel.objects.filter(query).select_related('object_type').distinct()[:5]

    if similar_models:
        links = []
        for model in similar_models:
            links.append(
                f'<li class="mb-1">'
                f'<a href="#" class="alert-link text-primary fw-semibold" '
                f'hx-get="/dict/models/{model.uuid}/?sidebar=1" '
                f'hx-target="#detail-container" '
                f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createModelModal\')).hide();">'
                f'{model.name} <span class="text-muted fw-normal">({model.object_type.type})</span>'
                f'</a>'
                f'</li>'
            )

        return HttpResponse(
            f'<div class="alert alert-warning py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold text-dark mb-1">'
            f'<i class="bi bi-exclamation-triangle-fill text-warning me-1"></i> '
            f'Обнаружены похожие модели ({len(similar_models)} шт.):'
            f'</div>'
            f'<ul class="ps-3 mb-1" style="max-height: 80px; overflow-y: auto;">'
            f'{"".join(links)}'
            f'</ul>'
            f'<div class="text-muted" style="font-size: 0.75rem;">'
            f'Возможно, нужная модель уже заведена. Кликните для быстрого перехода.'
            f'</div>'
            f'</div>'
        )

    return HttpResponse(
        '<div class="text-success small mt-1">'
        '<i class="bi bi-check-circle-fill me-1"></i>'
        'Название модели свободно и уникально'
        '</div>'
    )

@login_required
def check_object_name_view(request):
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    exact_match = DataObject.objects.filter(name__iexact=name).first()
    if exact_match:
        exact_name = exact_match.name or exact_match.model.name
        return HttpResponse(
            f'<div class="alert alert-danger py-2 px-3 mt-2 mb-0 rounded-3 small">'
            f'<div class="fw-bold mb-1"><i class="bi bi-x-circle-fill me-1"></i> Объект с таким именем уже существует!</div>'
            f'<a href="#" class="text-danger fw-bold" '
            f'hx-get="/dict/objects/{exact_match.uuid}/?sidebar=1" '
            f'hx-target="#detail-container" '
            f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createObjectModal\')).hide();">'
            f'Перейти к существующему объекту: {exact_name} ({exact_match.model.name})'
            f'</a>'
            f'</div>'
        )

    stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по'}
    words = [
        w.lower() for w in name.split() 
        if len(w) >= 3 and w.lower() not in stop_words
    ]

    similar_objects = []
    if words:
        query = Q()
        for word in words:
            query |= Q(name__icontains=word) | Q(model__name__icontains=word)
            similar_objects = DataObject.objects.filter(query).select_related('model').distinct()[:5]

    if similar_objects:
        links = []
        for obj in similar_objects:
            obj_name = obj.name or obj.model.name
            links.append(
                f'<li class="mb-1">'
                f'<a href="#" class="alert-link text-primary fw-semibold" '
                f'hx-get="/dict/objects/{obj.uuid}/?sidebar=1" '
                f'hx-target="#detail-container" '
                f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createObjectModal\')).hide();">'
                f'{obj_name} <span class="text-muted fw-normal">({obj.model.name})</span>'
                f'</a>'
                f'</li>'
            )

        return HttpResponse(
            f'<div class="alert alert-warning py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold text-dark mb-1">'
            f'<i class="bi bi-exclamation-triangle-fill text-warning me-1"></i> '
            f'Обнаружены похожие объекты ({len(similar_objects)} шт.):'
            f'</div>'
            f'<ul class="ps-3 mb-1" style="max-height: 80px; overflow-y: auto;">'
            f'{"".join(links)}'
            f'</ul>'
            f'<div class="text-muted" style="font-size: 0.75rem;">'
            f'Возможно, нужный объект уже зарегистрирован. Кликните на него для перехода.'
            f'</div>'
            f'</div>'
        )

    return HttpResponse(
        '<div class="text-success small mt-1">'
        '<i class="bi bi-check-circle-fill me-1"></i>'
        'Имя свободно и уникально'
        '</div>'
    )

@login_required
def suggest_view(request):
    field = request.GET.get('field')
    q = request.GET.get('q', '').strip()
    
    if not q or len(q) < 1:
        return HttpResponse('')
        
    results = []
    words = q.split()
    
    if field == 'object_type':
        exact = ObjectType.objects.filter(type__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= Q(type__icontains=w)
        partial = ObjectType.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'model':
        exact = ObjectModel.objects.filter(name__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= (Q(name__icontains=w) | Q(object_type__type__icontains=w))
        partial = ObjectModel.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'parent':
        exact = DataObject.objects.filter(Q(name__iexact=q) | Q(inventory_number__iexact=q))
        word_filter = Q()
        for w in words:
            word_filter &= (Q(name__icontains=w) | Q(inventory_number__icontains=w) | Q(model__name__icontains=w))
        partial = DataObject.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'dependency_type':
        exact = DependencyType.objects.filter(type__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= Q(type__icontains=w)
        partial = DependencyType.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)

    elif field == 'date_update_rule':
        exact = DateUpdateRule.objects.filter(name__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= Q(name__icontains=w)
        partial = DateUpdateRule.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)

    show_create_option = False
    if field in ['object_type', 'dependency_type', 'date_update_rule']:
        has_exact_match = any(
            (getattr(item, 'type' if field != 'date_update_rule' else 'name', '').lower() == q.lower()) for item in results
        )
        if not has_exact_match:
            show_create_option = True

    return render(request, 'data/includes/suggestions_list.html', {
        'results': results[:10],
        'field': field,
        'q': q,
        'show_create_option': show_create_option
    })

@login_required
def select_suggestion_view(request):
    field = request.GET.get('field')
    uuid_val = request.GET.get('uuid')
    name_val = request.GET.get('name')
    
    display_name = ""
    hidden_name = field
    hidden_value = ""
    is_new_rule = False
    
    if uuid_val:
        hidden_value = uuid_val
        if field == 'object_type':
            display_name = get_object_or_404(ObjectType, pk=uuid_val).type
        elif field == 'model':
            model_obj = get_object_or_404(ObjectModel, pk=uuid_val)
            display_name = f"{model_obj.name} ({model_obj.object_type.type})"
        elif field == 'parent':
            parent_obj = get_object_or_404(DataObject, pk=uuid_val)
            display_name = parent_obj.name or parent_obj.model.name
        elif field == 'dependency_type':
            display_name = get_object_or_404(DependencyType, pk=uuid_val).type
        elif field == 'date_update_rule':
            display_name = get_object_or_404(DateUpdateRule, pk=uuid_val).name
    elif name_val:
        display_name = f"{name_val} (Создать новое)"
        hidden_name = f"new_{field}"
        hidden_value = name_val
        if field == 'date_update_rule':
            is_new_rule = True
        
    return render(request, 'data/includes/suggestion_selected.html', {
        'field': field,
        'display_name': display_name,
        'hidden_name': hidden_name,
        'hidden_value': hidden_value,
        'is_new_rule': is_new_rule
    })

@login_required
def reset_suggestion_view(request):
    field = request.GET.get('field')
    placeholders = {
        'object_type': 'Введите тип оборудования...',
        'model': 'Введите модель оборудования...',
        'parent': 'Поиск родительского объекта...',
        'dependency_type': 'Введите тип связи...',
        'date_update_rule': 'Введите правило обновления...'
    }
    return render(request, 'data/includes/suggestion_input.html', {
        'field': field,
        'placeholder': placeholders.get(field, 'Начните вводить...')
    })

@login_required
def specs_builder_view(request):
    keys = request.POST.getlist('spec_keys')
    values = request.POST.getlist('spec_values')
    specs = dict(zip(keys, values))
    
    new_key = request.POST.get('new_key', '').strip()
    new_value = request.POST.get('new_value', '').strip()
    if new_key and new_value:
        specs[new_key] = new_value
        
    remove_key = request.POST.get('remove_key')
    if remove_key:
        specs.pop(remove_key, None)
        
    return render(request, 'data/includes/specs_builder.html', {
        'specifications': specs
    })


# --- КОНСТРУКТОР ОБСЛУЖИВАНИЯ ---

def calculate_next_maintenance_date(data_object, base_date=None):
    if not data_object.date_update_rule:
        return None
        
    rule_data = data_object.date_update_rule.rule or {}
    strategy = rule_data.get('strategy', 'relative')
    anchor_type = rule_data.get('anchor', 'actual')
    value = rule_data.get('value', {})
    
    if not base_date:
        base_date = timezone.now()
        
    if anchor_type == 'scheduled' and data_object.next_maintenance_date:
        base_date = data_object.next_maintenance_date

    if strategy == 'relative':
        delta = relativedelta(
            years=int(value.get('years', 0)),
            months=int(value.get('months', 0)),
            days=int(value.get('days', 0))
        )
        return base_date + delta
        
    elif strategy == 'fixed':
        if not isinstance(value, list) or not value:
            return None
            
        dates_in_year = []
        for item in value:
            m = int(item.get('month', 1))
            d = int(item.get('day', 1))
            try:
                dates_in_year.append(base_date.replace(month=m, day=d))
            except ValueError:
                dates_in_year.append(base_date.replace(month=m, day=28))
                
        dates_in_year.sort()
        
        for candidate in dates_in_year:
            if candidate > base_date:
                return candidate
                
        first_candidate = dates_in_year[0]
        return first_candidate + relativedelta(years=1)
        
    return None

@login_required
@role_required(['senior', 'admin', 'superuser'])
def rules_dates_builder_view(request):
    months = request.POST.getlist('fixed_months')
    days = request.POST.getlist('fixed_days')
    
    dates = []
    for m, d in zip(months, days):
        dates.append({'month': int(m), 'day': int(d)})
        
    new_month = request.POST.get('new_fixed_month')
    new_day = request.POST.get('new_fixed_day')
    if new_month and new_day:
        new_date = {'month': int(new_month), 'day': int(new_day)}
        if new_date not in dates:
            dates.append(new_date)
            
    remove_idx = request.POST.get('remove_idx')
    if remove_idx is not None:
        try:
            dates.pop(int(remove_idx))
        except IndexError:
            pass
            
    dates.sort(key=lambda x: (x['month'], x['day']))
    
    month_names = {
        1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
        5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
        9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря'
    }
    
    return render(request, 'data/includes/rules_dates_builder.html', {
        'dates': dates,
        'month_names': month_names
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])
def rule_constructor_view(request):
    strategy = request.GET.get('new_rule_strategy', 'relative')
    context = {'strategy': strategy}
    
    if strategy == 'fixed':
        context['dates'] = []
        context['month_names'] = {
            1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
            5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
            9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря'
        }
        
    return render(request, 'data/includes/rule_constructor_fields.html', context)

@login_required
@role_required(['senior', 'admin', 'superuser'])
def toggle_scheduling_mode_view(request):
    mode = request.GET.get('maintenance_scheduling_mode', 'manual')
    is_inline = request.GET.get('inline', '0') == '1'
    template = 'data/includes/scheduling_mode_fields_inline.html' if is_inline else 'data/includes/scheduling_mode_fields.html'
    return render(request, template, {
        'mode': mode
    })

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_rule_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        mode = request.POST.get('maintenance_scheduling_mode', 'manual')
        
        if mode == 'manual':
            obj.date_update_rule = None
            obj.save()
        else:
            rule_uuid = request.POST.get('date_update_rule')
            new_rule_name = request.POST.get('new_date_update_rule')
            
            selected_rule = None
            if rule_uuid:
                selected_rule = get_object_or_404(DateUpdateRule, pk=rule_uuid)
            elif new_rule_name:
                strategy = request.POST.get('new_rule_strategy', 'relative')
                
                if strategy == 'relative':
                    anchor = request.POST.get('new_rule_anchor', 'actual')
                    years = int(request.POST.get('new_rule_years', 0))
                    months = int(request.POST.get('new_rule_months', 6))
                    days = int(request.POST.get('new_rule_days', 0))
                    
                    rule_json = {
                        "strategy": "relative",
                        "anchor": anchor,
                        "value": {
                            "years": years,
                            "months": months,
                            "days": days
                        }
                    }
                elif strategy == 'fixed':
                    fixed_months = request.POST.getlist('fixed_months')
                    fixed_days = request.POST.getlist('fixed_days')
                    
                    dates_list = []
                    for m, d in zip(fixed_months, fixed_days):
                        dates_list.append({"month": int(m), "day": int(d)})
                        
                    rule_json = {
                        "strategy": "fixed",
                        "anchor": "yearly",
                        "value": dates_list
                    }
                    
                selected_rule, _ = DateUpdateRule.objects.get_or_create(
                    name=new_rule_name,
                    defaults={"rule": rule_json}
                )
                
            if selected_rule:
                obj.date_update_rule = selected_rule
                obj.next_maintenance_date = calculate_next_maintenance_date(obj, base_date=timezone.now())
                obj.save()
                
        rule_name = obj.date_update_rule.name if obj.date_update_rule else 'ручной ввод'
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Изменено правило планирования ТО на: {rule_name}."
        )
        
        rule_name_display = f"{obj.date_update_rule.name} <i class='bi bi-pencil-square ms-1 text-muted small'></i>" if obj.date_update_rule else "<span class='text-muted italic small'>ручной ввод <i class='bi bi-pencil-square ms-1'></i></span>"
        
        oob_rule_html = f"""
        <span id="rule-display-container" 
              data-bs-toggle="modal" 
              data-bs-target="#editRuleModal"
              hx-get="/dict/objects/{obj.uuid}/edit-rule/" 
              hx-target="#edit-rule-modal-content"
              style="cursor: pointer;" 
              class="fw-semibold text-primary transition-all"
              hx-swap-oob="outerHTML">
            {rule_name_display}
        </span>
        """
        
        date_str = obj.next_maintenance_date.strftime('%d.%m.%Y') if obj.next_maintenance_date else "Не запланировано"
        oob_date_html = f'<strong id="maintenance-date-display" class="text-danger" hx-swap-oob="innerHTML">{date_str}</strong>'
        
        return HttpResponse(f"{oob_rule_html}\n{oob_date_html}")

    current_mode = 'auto' if obj.date_update_rule else 'manual'
    rule_details = None
    
    if obj.date_update_rule:
        rule_data = obj.date_update_rule.rule or {}
        strategy = rule_data.get('strategy', 'relative')
        
        if strategy == 'relative':
            anchor_text = 'фактического выполнения' if rule_data.get('anchor') == 'actual' else 'планового срока'
            val = rule_data.get('value', {})
            rule_details = f"Интервал от {anchor_text}: {val.get('years', 0)}г. {val.get('months', 0)}мес. {val.get('days', 0)}дн."
        elif strategy == 'fixed':
            month_names = {
                1: 'Янв', 2: 'Фев', 3: 'Мар', 4: 'Апр', 5: 'Май', 6: 'Июн',
                7: 'Июл', 8: 'Авг', 9: 'Сен', 10: 'Окт', 11: 'Ноя', 12: 'Дек'
            }
            dates = rule_data.get('value', [])
            formatted_dates = [f"{d.get('day')} {month_names.get(d.get('month'))}" for d in dates]
            rule_details = f"Сезонные даты: {', '.join(formatted_dates)}"

    return render(request, 'data/object/edit_rule_modal_body.html', {
        'obj': obj,
        'current_mode': current_mode,
        'rule_details': rule_details
    })
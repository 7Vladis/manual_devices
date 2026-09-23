import calendar
import logging

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib.auth import get_user_model
from django.db.models.functions import Cast
from django.db.models import TextField
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.http import HttpResponse, HttpResponseForbidden
from django.db.models import Q, Prefetch, Count, ProtectedError
from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.html import format_html
from datetime import timedelta, datetime, date
from dateutil.relativedelta import relativedelta
from users.decorators import role_required
from .youtrack_services import (
    send_comment_to_youtrack,
    add_work_item_to_youtrack,
    upload_attachment_to_youtrack,
    sync_issue_from_youtrack,
    update_issue_description_in_youtrack,
    delete_comment_from_youtrack,
    delete_attachment_from_youtrack,
    update_comment_in_youtrack
)
from .models import DateUpdateRule, DataObject, ActionHistory, ObjectModel, ObjectType, Attachment, Comment
from .forms import MONTH_NAMES, DateUpdateRuleForm, clean_fixed_dates, parse_maintenance_date
from .validators import validate_attachment

logger = logging.getLogger('data')


def htmx_error(message, status=400, retarget=None):
    """
    Ответ с текстом ошибки для HTMX-запроса. Тело — готовый алерт, при необходимости
    перенаправляется в другой контейнер заголовком HX-Retarget (например, в блок
    .form-feedback модального окна, чтобы не затирать основную цель формы).
    """
    response = render_to_string('data/includes/htmx_error.html', {'message': message})
    response = HttpResponse(response, status=status)
    if retarget:
        response['HX-Retarget'] = retarget
        response['HX-Reswap'] = 'innerHTML'
    return response


def form_errors_text(form):
    """Плоский текст ошибок формы — для компактных HTMX-ответов."""
    parts = list(form.non_field_errors())
    for field in form:
        for error in field.errors:
            label = field.label or field.name
            parts.append(f"{label}: {error}")
    return " ".join(parts) or "Проверьте заполнение формы."


def yt_toast(messages, level='warning', request=None):
    """
    HTML всплывающих уведомлений о проблемах синхронизации с YouTrack.
    Приклеивается OOB-свопом к обычному ответу: локальная операция уже
    выполнена, но пользователь должен узнать о расхождении с внешней системой.
    """
    if isinstance(messages, str):
        messages = [messages]
    unique = list(dict.fromkeys(m for m in messages if m))
    return "\n".join(
        render_to_string('data/includes/yt_toast.html', {'message': m, 'level': level}, request=request)
        for m in unique
    )


# --- НАСТРОЙКИ СИСТЕМЫ ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def settings_page(request):
    """Единый центр управления системой"""
    active_tab = request.GET.get('tab', 'rules')
    
    # Защита вкладок на уровне бэкенда для Старшего инженера
    if active_tab in ['notifications', 'users'] and not request.user.is_admin_or_higher:
        active_tab = 'rules'

    context = {'active_tab': active_tab}

    # 1. ВКЛАДКА: Правила планирования ТО
    if active_tab == 'rules':
        rules_query = DateUpdateRule.objects.prefetch_related('data_objects').annotate(
            objects_count=Count('data_objects')
        ).order_by('name')
        

        for r in rules_query:
            rule_data = r.rule or {}
            if rule_data.get('strategy') == 'fixed':
                dates_list = rule_data.get('value', [])
                formatted_dates = []
                for d in dates_list:
                    day = d.get('day', 1)
                    month_num = d.get('month', 1)
                    month_name = MONTH_NAMES.get(month_num, '')
                    formatted_dates.append(f"{day} {month_name}")
                r.formatted_fixed_dates = ", ".join(formatted_dates)
                
        context['rules'] = rules_query

    # 2. ВКЛАДКА: Типы объектов
    elif active_tab == 'object_types':
        object_types_qs = ObjectType.objects.prefetch_related(
            Prefetch(
                'models',
                queryset=ObjectModel.objects.prefetch_related(
                    Prefetch('data_objects', queryset=DataObject.objects.all().order_by('name'))
                ).order_by('name')
            )
        ).annotate(
            models_count=Count('models', distinct=True),
            objects_count=Count('models__data_objects', distinct=True)
        ).order_by('type')
        
        context['object_types'] = object_types_qs

    # 3. ВКЛАДКА: Уведомления Mattermost (Только для Админов и Суперпользователей)
    elif active_tab == 'notifications' and request.user.is_admin_or_higher:
        from notifications.models import MattermostSetting
        context['settings'] = MattermostSetting.objects.all().order_by('-updated_at')

    # 4. ВКЛАДКА: Пользователи (Только для Админов и Суперпользователей)
    elif active_tab == 'users' and request.user.is_admin_or_higher:
        from users.forms import UserCreateForm

        User = get_user_model()
        context['users_list'] = User.objects.all().order_by('username')
        context['user_create_form'] = UserCreateForm()

    if request.headers.get('HX-Request'):
        return render(request, 'data/settings/settings_layout_inner.html', context)
    return render(request, 'data/settings.html', context)


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def create_object_type_view(request):
    """Создание нового типа оборудования из настроек"""
    name = request.POST.get('type', '').strip()
    if name:
        ObjectType.objects.get_or_create(type=name)
            
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=object_types'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def delete_object_type_view(request, pk):
    """Удаление типа оборудования с проверкой на использование"""
    obj_type = get_object_or_404(ObjectType, pk=pk)
    if obj_type.models.exists():
        return htmx_error(
            'Нельзя удалить тип: он привязан к существующим моделям оборудования.',
            retarget=f'#error-container-type-{obj_type.uuid}'
        )
    obj_type.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=object_types'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_rule_settings_view(request):
    """Создание нового правила планирования ТО из настроек"""
    if request.method == 'POST':
        form = DateUpdateRuleForm(
            request.POST,
            fixed_months=request.POST.getlist('fixed_months'),
            fixed_days=request.POST.getlist('fixed_days'),
        )
        if not form.is_valid():
            return htmx_error(form_errors_text(form), retarget='#create-rule-error')

        form.save()
        response = HttpResponse()
        response['HX-Redirect'] = '/settings/?tab=rules'
        return response

    return render(request, 'data/settings/create_rule_modal.html', {
        'strategy': 'relative',
        'anchor': 'actual',
        'years': 0,
        'months': 6,
        'days': 0,
        'fixed_dates': [],
        'month_names': MONTH_NAMES,
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_rule_settings_view(request, pk):
    """Редактирование параметров правила планирования ТО"""
    rule_obj = get_object_or_404(DateUpdateRule, pk=pk)

    if request.method == 'POST':
        form = DateUpdateRuleForm(
            request.POST,
            instance=rule_obj,
            fixed_months=request.POST.getlist('fixed_months'),
            fixed_days=request.POST.getlist('fixed_days'),
        )
        if not form.is_valid():
            return htmx_error(form_errors_text(form), retarget=f'#rule-error-{rule_obj.uuid}')

        rule_obj = form.save()

        # Пересчитываем даты для объектов с этим правилом
        for obj in rule_obj.data_objects.all():
            obj.next_maintenance_date = calculate_next_maintenance_date(obj, base_date=timezone.localdate())
            obj.save(update_fields=['next_maintenance_date'])

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
    fixed_dates = val if strategy == 'fixed' and isinstance(val, list) else []

    return render(request, 'data/settings/edit_rule_modal.html', {
        'rule_obj': rule_obj,
        'strategy': strategy,
        'anchor': anchor,
        'years': years,
        'months': months,
        'days': days,
        'fixed_dates': fixed_dates,
        'month_names': MONTH_NAMES,
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def delete_rule_view(request, pk):
    """Удаление правила планирования"""
    rule = get_object_or_404(DateUpdateRule, pk=pk)
    if rule.data_objects.exists():
        return htmx_error(
            'Нельзя удалить: правило используется в активных объектах.',
            retarget=f'#rule-error-{rule.uuid}'
        )
    rule.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=rules'
    return response


# --- ЭКСПОРТ ДАННЫХ ---

@login_required
@role_required(['admin', 'superuser'])
def export_modal_view(request):
    """Модальное окно выбора опции экспорта данных в XLSX"""
    return render(request, 'data/export_modal.html')


@login_required
@role_required(['admin', 'superuser'])
def export_xlsx_view(request):
    """Генерация XLSX файла с прямой иерархией через parent"""
    import openpyxl

    export_mode = request.GET.get('export_mode', 'all')
    
    queryset = DataObject.objects.select_related('model', 'model__object_type', 'parent', 'parent__model').all()
    if export_mode == 'with_inventory':
        queryset = queryset.filter(inventory_number__isnull=False).exclude(inventory_number='')
    
    queryset = queryset.order_by('inventory_number', 'name')

    # Excel/LibreOffice трактуют значение, начинающееся с =, +, -, @ (а также
    # с управляющих символов табуляции и возврата каретки) как формулу.
    # Данные приходят от пользователей и из YouTrack, поэтому обезвреживаем их.
    FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

    def escape_formula(value):
        text = "" if value is None else str(value)
        if text.startswith(FORMULA_PREFIXES):
            return "'" + text
        return text

    def get_object_hierarchy_path(obj):
        """Сборка пути от корня до текущего объекта через цепочку parent"""
        path_segments = []
        current = obj
        visited = set()
        
        while current and current.uuid not in visited:
            visited.add(current.uuid)
            name = current.name or (current.model.name if current.model else "Без имени")
            path_segments.append(name)
            current = current.parent
            
        path_segments.reverse()
        return " → ".join(path_segments)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Оборудование"

    headers = [
        "Инвентарный номер",
        "Название объекта",
        "Иерархический путь (от корня)",
        "Описание объекта",
        "Название модели",
        "Характеристики спецификации"
    ]
    ws.append(headers)

    for col_num in range(1, 7):
        cell = ws.cell(row=1, column=col_num)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    for obj in queryset:
        inv_num = obj.inventory_number or ""
        obj_name = obj.name or ""
        hierarchy_path = get_object_hierarchy_path(obj)
        description = obj.description or ""
        model_name = obj.model.name if obj.model else ""
        
        specs = obj.model.specifications if obj.model and obj.model.specifications else {}
        specs_str_list = []
        if isinstance(specs, dict):
            for k, v in specs.items():
                specs_str_list.append(f"{k}: {v}")
        specs_formatted = "; ".join(specs_str_list)

        row = [inv_num, obj_name, hierarchy_path, description, model_name, specs_formatted]
        ws.append([escape_formula(value) for value in row])

        # Явно фиксируем текстовый тип, чтобы Excel не переинтерпретировал строку
        for cell in ws[ws.max_row]:
            cell.data_type = 's'

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 15), 50)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"manual_devices_export_{timezone.localdate().strftime('%Y%m%d')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    wb.save(response)
    return response


# --- ДАШБОРД ---

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


@login_required
def dashboard(request):
    today = timezone.localdate()
    total_objects = DataObject.objects.count()
    overdue_count = DataObject.objects.filter(next_maintenance_date__lt=today).count()
    
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
        start, end = get_period_limits('week')
        objects = DataObject.objects.filter(next_maintenance_date__range=(start, end))
        label = "План на неделю"
        
    objects = objects.select_related('model', 'model__object_type').order_by('next_maintenance_date')

    return render(request, 'data/includes/maintenance_table.html', {
        'objects': objects,
        'today': today,
        'period_label': label
    })


@login_required
def search_view(request):
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return HttpResponse('')

    words = [w for w in query.split() if w]

    def term_filter(term):
        """Все поля, по которым ищем один терм (см. подсказку в шапке поиска)."""
        return (
            Q(name__icontains=term) |
            Q(inventory_number__icontains=term) |
            Q(description__icontains=term) |
            Q(model__name__icontains=term) |
            Q(youtrack_issue_id__icontains=term) |
            Q(comments__text__icontains=term) |
            Q(actions__action__icontains=term)
        )

    # 1. Поиск по точному совпадению всей фразы (в любом поле)
    search_filter = term_filter(query)

    # 2. Если введено несколько слов (например, "4 стол" или "стол 4"),
    # дополнительно ищем объекты, где встречаются ВСЕ эти слова одновременно
    if len(words) > 1:
        words_q = Q()
        for word in words:
            words_q &= term_filter(word)
        search_filter |= words_q

    results = DataObject.objects.filter(
        search_filter
    ).select_related('model', 'model__object_type').distinct()[:10]

    return render(request, 'data/includes/search_results_list.html', {'results': results})


# --- СПРАВОЧНИК И ПРОВОДНИК ---

@login_required
def dict_view(request):
    active_tab = request.GET.get('tab', 'objects')
    
    selected_object_id = request.GET.get('object') or request.session.get('active_object_id')
    selected_model_id = request.GET.get('model') or request.session.get('active_model_id')
    
    active_object = None
    active_model = None
    parent_uuids = []
    
    explorer_mode = request.session.get('explorer_mode', 'tree')
    explorer_parent_uuid = request.session.get('explorer_parent_uuid')
    explorer_parent = None
    
    if selected_object_id and active_tab == 'objects':
        try:
            active_object = DataObject.objects.select_related('parent').prefetch_related('children').get(pk=selected_object_id)
            request.session['active_object_id'] = str(active_object.pk)
            
            # Строим цепочку родителей для разворачивания дерева
            parent_uuids = [str(anc.pk) for anc in get_ancestors_chain(active_object)]
        except DataObject.DoesNotExist:
            request.session['active_object_id'] = None
                
    elif selected_model_id and active_tab == 'models':
        try:
            active_model = ObjectModel.objects.get(pk=selected_model_id)
            request.session['active_model_id'] = str(active_model.pk)
        except ObjectModel.DoesNotExist:
            request.session['active_model_id'] = None
        
    # Ищем текущую папку проводника
    if explorer_mode == 'flat' and explorer_parent_uuid:
        try:
            explorer_parent = DataObject.objects.select_related('parent').get(pk=explorer_parent_uuid)
            for node in get_ancestors_chain(explorer_parent) + [explorer_parent]:
                if str(node.pk) not in parent_uuids:
                    parent_uuids.append(str(node.pk))
        except DataObject.DoesNotExist:
            explorer_parent_uuid = None
            request.session['explorer_parent_uuid'] = None
    
    request.session.modified = True

    models = ObjectModel.objects.all().order_by('name')
    object_types = ObjectType.objects.all().order_by('type')
    
    context = {
        'models': models,
        'object_types': object_types,
        'active_tab': active_tab,
        'active_object': active_object,
        'active_model': active_model,
        'parent_uuids': parent_uuids,
        'explorer_mode': explorer_mode,
        'explorer_parent': explorer_parent,
    }
    
    if active_tab == 'models':
        context['object_types_list'] = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
    else:
        if explorer_mode == 'flat' and explorer_parent:
            context['initial_objects'] = explorer_parent.children.all().prefetch_related('children').order_by('name')
        else:
            context['initial_objects'] = DataObject.objects.filter(
                parent__isnull=True
            ).prefetch_related('children').order_by('name')
        
    if request.headers.get('HX-Request') and request.GET.get('sidebar'):
        return render(request, 'data/tree/dict_sidebar.html', context)
        
    return render(request, 'data/dict.html', context)

@login_required
@require_POST
def toggle_explorer_mode_view(request):
    """Переключает режим отображения с умной синхронизацией позиции"""
    if request.method == 'POST':
        current_mode = request.session.get('explorer_mode', 'tree')
        new_mode = 'flat' if current_mode == 'tree' else 'tree'
        request.session['explorer_mode'] = new_mode
        
        if new_mode == 'flat':
            active_id = request.session.get('active_object_id')
            if active_id:
                try:
                    obj = DataObject.objects.prefetch_related('children').select_related('parent').get(pk=active_id)
                    if obj.children.exists():
                        request.session['explorer_parent_uuid'] = str(obj.pk)
                    elif obj.parent:
                        request.session['explorer_parent_uuid'] = str(obj.parent.pk)
                    else:
                        request.session['explorer_parent_uuid'] = None
                except DataObject.DoesNotExist:
                    request.session['explorer_parent_uuid'] = None
            else:
                request.session['explorer_parent_uuid'] = None

        request.session.modified = True
        
        icon_class = "bi-folder2-open text-warning" if new_mode == 'flat' else "bi-diagram-3-fill text-info"
        title_text = "Проводник (кликните для перехода в Дерево)" if new_mode == 'flat' else "Дерево (кликните для перехода в Проводник)"
        
        html = f"""
        <button type="button"
                id="explorer-toggle-btn"
                class="header-nav-btn"
                hx-post="/dict/toggle-explorer-mode/"
                hx-target="#explorer-toggle-btn"
                hx-swap="outerHTML"
                style="width: 34px; height: 34px; border-radius: 8px; background-color: rgba(255, 255, 255, 0.06);"
                title="Режим справочника: {title_text}">
            <i class="bi {icon_class} fs-6" style="line-height: 1;"></i>
        </button>
        """
        
        response = HttpResponse(html)
        response['HX-Trigger'] = 'explorerModeChanged'
        return response
        
    return HttpResponse("Метод не разрешен", status=405)


@login_required
def explorer_navigate_view(request, pk):
    """Переход внутрь папки (или в корень, если pk='root')"""
    if str(pk) == 'root':
        request.session['explorer_parent_uuid'] = None
    else:
        request.session['explorer_parent_uuid'] = str(pk)
        
    request.session.modified = True
    response = HttpResponse()
    response['HX-Trigger'] = 'explorerModeChanged'
    return response


@login_required
def explorer_up_view(request):
    """Переход на один уровень вверх в плоском режиме (вплоть до корня)"""
    parent_uuid = request.session.get('explorer_parent_uuid')
    if parent_uuid:
        try:
            current_parent = DataObject.objects.select_related('parent').get(pk=parent_uuid)
            request.session['explorer_parent_uuid'] = str(current_parent.parent.pk) if current_parent.parent else None
        except DataObject.DoesNotExist:
            request.session['explorer_parent_uuid'] = None
    else:
        request.session['explorer_parent_uuid'] = None
            
    request.session.modified = True
    response = HttpResponse()
    response['HX-Trigger'] = 'explorerModeChanged'
    return response


@login_required
def object_tree_view(request):
    roots = DataObject.objects.filter(parent__isnull=True).prefetch_related('children').order_by('name')
    return render(request, 'data/tree/object_tree_list.html', {'objects': roots})


@login_required
def object_children_view(request, parent_uuid):
    parent = get_object_or_404(DataObject, pk=parent_uuid)
    children = parent.children.all().prefetch_related('children').order_by('name')
    
    active_object_id = request.GET.get('active_object')
    active_object = None
    parent_uuids = []
    
    if active_object_id:
        try:
            active_object = DataObject.objects.select_related('parent').get(pk=active_object_id)
            parent_uuids = [str(anc.pk) for anc in get_ancestors_chain(active_object)]
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


# --- ОБСЛУЖИВАНИЕ ОБЪЕКТА ---

@login_required
def service_object_view(request, pk):
    """Выполнение ТО объекта (плановое или внеплановое)"""
    obj = get_object_or_404(DataObject, pk=pk)
    effective_yt_id, target_yt_obj = obj.get_effective_youtrack_issue()
    
    if request.method == 'POST':
        yt_errors = []
        is_unplanned = request.POST.get('is_unplanned') == 'on'
        date_str = request.POST.get('maintenance_date')
        spent_time = request.POST.get('spent_time', '').strip()
        custom_comment = request.POST.get('comment', '').strip()
        
        # Плановое событие, которое закрывает эта работа, — это срок ТО,
        # назначенный ДО обновления. Запоминаем его до перезаписи.
        closed_plan_date = None if is_unplanned else obj.next_maintenance_date

        # 1. Если ТО ПЛАНОВОЕ — обновляем дату следующего обслуживания
        if not is_unplanned and date_str:
            try:
                obj.next_maintenance_date = parse_maintenance_date(date_str, "Дата следующего ТО")
            except ValidationError as exc:
                return htmx_error(" ".join(exc.messages),
                                  retarget='#service-modal-content .form-feedback')
            obj.save(update_fields=['next_maintenance_date'])
        
        # 2. Формируем текст записи для истории и YouTrack
        if is_unplanned:
            prefix_type = "Внеплановое ТО"
            date_info = f"(след. ТО по графику: {obj.next_maintenance_date.strftime('%d.%m.%Y')})" if obj.next_maintenance_date else ""
            default_desc = f"Внеплановое техническое обслуживание {date_info}".strip()
        else:
            prefix_type = "Плановое ТО"
            date_info = f"(след. ТО: {obj.next_maintenance_date.strftime('%d.%m.%Y')})" if obj.next_maintenance_date else ""
            default_desc = f"Плановое техническое обслуживание выполнено {date_info}".strip()

        action_text = f"[{prefix_type}] {custom_comment}" if custom_comment else default_desc

        # 3. Фиксируем запись в истории объекта
        history_entry = ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='maintenance',
            action=action_text,
            maintenance_kind='unplanned' if is_unplanned else 'planned',
            planned_for=closed_plan_date,
        )

        # 4. Списываем время в YouTrack (свою задачу или родительскую)
        if effective_yt_id and spent_time:
            comp_prefix = f"[{obj.name or obj.model.name}] " if obj != target_yt_obj else ""
            work_item_desc = f"{comp_prefix}{action_text}"
            
            success_work, work_id_or_err = add_work_item_to_youtrack(
                issue_id=effective_yt_id,
                duration_str=spent_time,
                text=work_item_desc,
                user=request.user
            )
            
            if success_work and work_id_or_err:
                history_entry.youtrack_id = work_id_or_err
                history_entry.save(update_fields=['youtrack_id'])
            elif not success_work:
                # ТО зафиксировано локально, но время в задачу не списалось —
                # молчать об этом нельзя, иначе учёт разойдётся незаметно.
                logger.warning("Не удалось списать время в YouTrack %s: %s", effective_yt_id, work_id_or_err)
                yt_errors.append(f"Время не списано в задачу {effective_yt_id}: {work_id_or_err}")
        
        node_html = render_to_string('data/tree/object_tree_node.html', {'node': obj}, request=request)
        oob_pill_html = render_maintenance_pill(obj, request=request)
        # Счетчик вкладки «История» в карточке объекта обновляем тем же ответом
        oob_count_html = format_html(
            '<span id="tab-count-history" class="tab-count" hx-swap-oob="true">{}</span>',
            obj.actions.count()
        )
        
        parts = [node_html, oob_pill_html, oob_count_html]
        if yt_errors:
            parts.append(yt_toast(yt_errors, request=request))
        response = HttpResponse("\n".join(parts))
        response['HX-Trigger'] = 'objectServiced'
        return response

    proposed_date = calculate_next_maintenance_date(obj)
    
    return render(request, 'data/tree/service_modal_body.html', {
        'obj': obj,
        'proposed_date': proposed_date,
        'effective_yt_id': effective_yt_id,
        'target_yt_obj': target_yt_obj
    })

# --- УДАЛЕНИЕ ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_http_methods(["GET", "POST", "DELETE"])
def delete_object_view(request, pk):
    """
    GET  — модальное окно подтверждения с точным объёмом каскада
           (потомки, комментарии, вложения, история).
    POST — удаление поддерева. Удаление непустого узла требует явного
           подтверждения флагом confirm_subtree.
    """
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    stats = obj.get_subtree_stats()

    if request.method == 'GET':
        return render(request, 'data/includes/confirm_delete_object.html', {
            'obj': obj,
            'stats': stats,
        })

    if request.method not in ['POST', 'DELETE']:
        return HttpResponse("Метод не разрешен", status=405)

    if stats['descendants'] and request.POST.get('confirm_subtree') != 'yes':
        return htmx_error(
            'Объект содержит дочерние компоненты: подтвердите удаление всего поддерева.',
            retarget='#confirm-modal-content .form-feedback'
        )

    was_active = request.session.get('active_object_id') == str(obj.pk)
    obj_uuid = obj.uuid
    obj.delete()
    if was_active:
        request.session['active_object_id'] = None
        request.session.modified = True

    # Узел убираем из дерева OOB-свопом: он может отсутствовать в DOM
    # (свёрнутая ветка, режим проводника) — тогда инструкция просто игнорируется.
    parts = [format_html('<li id="node-{}" hx-swap-oob="delete"></li>', obj_uuid)]
    if was_active:
        parts.append(render_to_string('data/includes/detail_placeholder.html', {'oob': True}, request=request))
    response = HttpResponse("\n".join(parts), status=200)
    response['HX-Trigger'] = 'objectDeleted'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_http_methods(["GET", "POST", "DELETE"])
def delete_model_view(request, pk):
    """
    GET  — окно подтверждения; если модель используется объектами, удаление
           недоступно (FK защищён PROTECT), окно объясняет, что делать.
    POST — удаление свободной модели.
    """
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    objects_count = model_obj.data_objects.count()

    if request.method == 'GET':
        return render(request, 'data/includes/confirm_delete_model.html', {
            'model_obj': model_obj,
            'objects_count': objects_count,
        })

    if request.method not in ['POST', 'DELETE']:
        return HttpResponse("Метод не разрешен", status=405)

    model_uuid = model_obj.uuid
    try:
        model_obj.delete()
    except ProtectedError:
        return htmx_error(
            f'Модель используется объектами ({objects_count} шт.) — сначала переназначьте им другую модель.',
            status=409,
            retarget='#confirm-modal-content .form-feedback'
        )

    was_active = request.session.get('active_model_id') == str(model_uuid)
    parts = [format_html('<li id="model-node-{}" hx-swap-oob="delete"></li>', model_uuid)]
    if was_active:
        request.session['active_model_id'] = None
        request.session.modified = True
        parts.append(render_to_string('data/includes/detail_placeholder.html', {'oob': True}, request=request))
    response = HttpResponse("\n".join(parts), status=200)
    response['HX-Trigger'] = 'modelDeleted'
    return response


# --- СОЗДАНИЕ ОБЪЕКТОВ И МОДЕЛЕЙ ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_object_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        model_uuid = request.POST.get('model')
        inventory_number = request.POST.get('inventory_number')
        youtrack_issue_id = request.POST.get('youtrack_issue_id', '').strip()
        parent_uuid = request.POST.get('parent')
        maintenance_str = request.POST.get('next_maintenance_date')
        rule_uuid = request.POST.get('date_update_rule')
        
        model_obj = ObjectModel.objects.filter(pk=model_uuid).first() if model_uuid else None
        if model_obj is None:
            return htmx_error(
                'Не выбрана модель оборудования. Выберите её из списка подсказок.',
                retarget='#createObjectModal .form-feedback'
            )
        
        try:
            next_maintenance_date = parse_maintenance_date(maintenance_str, "Дата следующего ТО")
        except ValidationError as exc:
            return htmx_error(" ".join(exc.messages),
                              retarget='#createObjectModal .form-feedback')

        selected_rule = get_object_or_404(DateUpdateRule, pk=rule_uuid) if rule_uuid else None
        parent_obj = get_object_or_404(DataObject, pk=parent_uuid) if parent_uuid else None
                
        new_obj = DataObject.objects.create(
            name=name,
            model=model_obj,
            parent=parent_obj,
            inventory_number=inventory_number if inventory_number else None,
            youtrack_issue_id=youtrack_issue_id if youtrack_issue_id else None,
            next_maintenance_date=next_maintenance_date,
            date_update_rule=selected_rule
        )

        scheduling_mode = request.POST.get('maintenance_scheduling_mode', 'manual')
        if scheduling_mode == 'auto' and selected_rule:
            first_date = calculate_next_maintenance_date(new_obj, base_date=timezone.localdate())
            new_obj.next_maintenance_date = first_date
            new_obj.save(update_fields=['next_maintenance_date'])
            
        if parent_obj:
            parent_name = parent_obj.name or parent_obj.model.name
            action_desc = f"Объект зарегистрирован в системе в составе родительского объекта '{parent_name}'."
        else:
            action_desc = "Объект зарегистрирован в системе как корневой объект."

        ActionHistory.objects.create(
            user=request.user,
            data_object=new_obj,
            action_type='create',
            action=action_desc
        )
        
        roots = DataObject.objects.filter(parent__isnull=True).order_by('name')
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
            object_type = ObjectType.objects.filter(pk=type_uuid).first() if type_uuid else None

        if object_type is None:
            return htmx_error(
                'Не выбран тип оборудования. Выберите существующий тип или создайте новый.',
                retarget='#createModelModal .form-feedback'
            )
            
        try:
            # atomic() ставит savepoint: после IntegrityError транзакция
            # остаётся рабочей и мы можем отрендерить ответ с ошибкой.
            with transaction.atomic():
                ObjectModel.objects.create(
                    name=name,
                    object_type=object_type,
                    specifications=specifications
                )
        except IntegrityError:
            # Ограничение в БД — последний рубеж: подсказка при вводе имени
            # не спасает от гонки двух одновременных запросов.
            return htmx_error(
                f'Модель «{name}» уже существует для типа «{object_type.type}». '
                'Откройте существующую модель вместо создания дубликата.',
                status=409,
                retarget='#createModelModal .form-feedback'
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


def get_comments_for(obj, user):
    """
    Лента комментариев объекта с проставленным признаком can_edit —
    шаблон не может вызвать метод с аргументом, поэтому считаем здесь.
    """
    comments = list(
        obj.comments.select_related('user').prefetch_related('attachments').order_by('-created_at')
    )
    for comment in comments:
        comment.can_edit = comment.can_be_edited_by(user)
    return comments


# --- ДЕТАЛИ ОБЪЕКТА ---

def render_maintenance_pill(obj, request=None, oob=True):
    """Рендерит пилюлю срока ТО (для OOB-обновления шапки карточки объекта)."""
    today = timezone.localdate()
    return render_to_string('data/object/maintenance_pill.html', {
        'obj': obj,
        'today': today,
        'soon_date': today + timedelta(days=14),
        'oob': oob,
    }, request=request)


def get_ancestors_chain(obj, max_depth=64):
    """
    Возвращает цепочку родителей от корня до непосредственного родителя объекта.
    Защищена от зацикливания (visited) и от чрезмерной глубины.
    """
    chain = []
    visited = {obj.uuid}
    current = obj.parent
    while current and current.uuid not in visited and len(chain) < max_depth:
        visited.add(current.uuid)
        chain.append(current)
        current = current.parent
    chain.reverse()
    return chain


@login_required
def object_detail_view(request, pk):
    """
    Карточка объекта. Это чистый GET: никаких обращений к YouTrack и записи
    в БД (кроме курсора активного объекта в сессии). Синхронизацию запускает
    сама карточка сразу после загрузки — фоновым POST на sync_youtrack_view,
    так что пользователь видит актуальные данные без ручных действий.
    """
    return render_object_detail(request, pk)


def render_object_detail(request, pk):
    """Рендер карточки с OOB-обновлением подсветки узлов дерева."""
    obj = get_object_or_404(
        DataObject.objects.select_related('model', 'model__object_type', 'parent', 'parent__model').prefetch_related('children'), 
        pk=pk
    )
    prev_active_id = request.session.get('active_object_id')
    request.session['active_object_id'] = str(pk)
    request.session.modified = True

    context = {
        'obj': obj,
        'parent': obj.parent,
        'active_tab': 'short_info',
        'today': timezone.localdate(),
        'soon_date': timezone.localdate() + timedelta(days=14),
        'comments_count': obj.comments.count(),
        'files_count': obj.attachments.count(),
        'history_count': obj.actions.count(),
        'specs_count': len(obj.model.specifications or {}) if obj.model else 0,
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
        roots = DataObject.objects.filter(parent__isnull=True).order_by('name')
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
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def unlink_rule_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    obj.date_update_rule = None
    obj.save(update_fields=['date_update_rule'])
    
    ActionHistory.objects.create(
        user=request.user,
        data_object=obj,
        action_type='rule_change',
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
    context = {'obj': obj, 'today': timezone.localdate()}
    
    if tab_name == 'short_info':
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        context['preview'] = preview
        template = 'data/object/object_tab_short_info.html'
        
    elif tab_name == 'specs':
        context['specifications'] = obj.model.specifications or {}
        template = 'data/object/object_tab_specs.html'
        
    elif tab_name == 'comments':
        comments = get_comments_for(obj, request.user)
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


# --- INLINE-РЕДАКТИРОВАНИЕ ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_inventory_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        new_inv = request.POST.get('inventory_number', '').strip()
        obj.inventory_number = new_inv if new_inv else None
        obj.save(update_fields=['inventory_number'])
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='update',
            action=f"Изменен инвентарный номер объекта на: {new_inv or 'отсутствует'}."
        )
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_youtrack_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        new_yt = request.POST.get('youtrack_issue_id', '').strip()
        obj.youtrack_issue_id = new_yt if new_yt else None
        obj.save(update_fields=['youtrack_issue_id'])
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='update',
            action=f"Изменен ID задачи Youtrack на: {new_yt or 'отсутствует'}."
        )

        sync_status = None
        sync_message = ""
        if obj.youtrack_issue_id:
            sync_success, sync_message = sync_issue_from_youtrack(obj, request.user)
            sync_status = 'success' if sync_success else 'error'

        return render(request, 'data/object/inline_youtrack.html', {
            'obj': obj,
            'editing': False,
            'sync_status': sync_status,
            'sync_message': sync_message
        })
        
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_youtrack.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_youtrack.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_parent_view(request, pk):
    """Смена родительского объекта напрямую через ForeignKey parent"""
    obj = get_object_or_404(DataObject.objects.select_related('parent', 'parent__model'), pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_parent.html', {
            'obj': obj, 
            'current_parent': obj.parent,
            'editing': False
        })
        
    if request.method == 'POST':
        parent_uuid = request.POST.get('parent') or request.POST.get('parent_uuid')
        
        if parent_uuid:
            new_parent = get_object_or_404(DataObject, pk=parent_uuid)
            # Защита от циклов: нельзя назначить родителем себя или своего потомка
            try:
                obj.validate_parent(new_parent)
            except ValidationError as exc:
                return render(request, 'data/object/inline_parent.html', {
                    'obj': obj,
                    'current_parent': obj.parent,
                    'editing': False,
                    'error': " ".join(exc.messages),
                }, status=400)
            obj.parent = new_parent
            parent_name = new_parent.name or new_parent.model.name
        else:
            obj.parent = None
            parent_name = "отсутствует"
            
        obj.save(update_fields=['parent'])
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='link_change',
            action=f"Назначен новый родительский объект: '{parent_name}'."
        )
        
        roots = DataObject.objects.filter(parent__isnull=True).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render(request, 'data/tree/dict_sidebar.html', sidebar_context).content.decode('utf-8')
        parent_html = render_to_string('data/object/inline_parent.html', {
            'obj': obj, 
            'current_parent': obj.parent, 
            'editing': False
        }, request=request)
            
        response_html = f"""
            {parent_html}
            <div id="sidebar-container" hx-swap-oob="innerHTML">
                {sidebar_html}
            </div>
        """
        return HttpResponse(response_html)

    return render(request, 'data/object/inline_parent.html', {
        'obj': obj,
        'current_parent': obj.parent,
        'editing': True
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_name_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_name.html', {'obj': obj, 'editing': False})
        
    if request.method == 'POST':
        old_name = obj.name or obj.model.name
        new_name = request.POST.get('name', '').strip()
        
        if new_name and old_name != new_name:
            obj.name = new_name
            obj.save(update_fields=['name'])
        
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action_type='update',
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

    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})

    if request.method == 'POST':
        old_desc = (obj.description or '').strip()
        new_desc = request.POST.get('description', '').strip()
        
        if old_desc != new_desc:
            obj.description = new_desc if new_desc else None
            obj.save(update_fields=['description'])
            
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action_type='update',
                action="Обновлено описание объекта."
            )

            if obj.youtrack_issue_id:
                ok, detail = update_issue_description_in_youtrack(
                    issue_id=obj.youtrack_issue_id,
                    description=new_desc,
                    user=request.user
                )
                if not ok:
                    logger.warning("Описание не отправлено в YouTrack %s: %s", obj.youtrack_issue_id, detail)
                    html = render_to_string('data/object/inline_description.html',
                                            {'obj': obj, 'editing': False}, request=request)
                    return HttpResponse(html + "\n" + yt_toast(
                        f"Описание сохранено локально, но не обновлено в задаче {obj.youtrack_issue_id}: {detail}",
                        request=request))

        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': True})


# --- КОММЕНТАРИИ И ВЛОЖЕНИЯ ---

@login_required
@require_POST
def add_comment_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    text = request.POST.get('text', '').strip()
    file = request.FILES.get('file')
    yt_errors = []
    form_error = None
    
    # Ищем эффективную задачу YouTrack (свою или первого родителя)
    effective_yt_id, target_yt_obj = obj.get_effective_youtrack_issue()

    if not text and not file:
        form_error = "Добавьте текст комментария или прикрепите файл."

    if file:
        try:
            validate_attachment(file)
        except ValidationError as exc:
            form_error = " ".join(exc.messages)
            file = None
            if not text:
                text = ''

    if (text or file) and not form_error:
        comment = Comment.objects.create(
            user=request.user,
            data_object=obj,
            text=text or "Вложение к объекту"
        )
        
        attachment_obj = None
        if file:
            attachment_obj = Attachment.objects.create(
                user=request.user,
                data_object=obj,
                comment=comment,
                path=file,
                is_preview=False
            )
            
        if effective_yt_id:
            # Загружаем файл в YouTrack карточку
            if attachment_obj and attachment_obj.path:
                try:
                    with open(attachment_obj.path.path, 'rb') as f:
                        success_file, result_file = upload_attachment_to_youtrack(
                            issue_id=effective_yt_id,
                            file_obj=f,
                            user=request.user
                        )
                        if success_file and result_file:
                            attachment_obj.youtrack_id = result_file
                            attachment_obj.save(update_fields=['youtrack_id'])
                        elif not success_file:
                            yt_errors.append(result_file)
                except OSError as exc:
                    logger.exception("Не удалось прочитать файл комментария для YouTrack")
                    yt_errors.append(f"Файл сохранён локально, но не прочитан для отправки в YouTrack: {exc}")

            # Формируем текст комментария с указанием компонента (если пишем в родителя)
            component_prefix = f"**[{obj.name or obj.model.name}]** " if obj != target_yt_obj else ""
            
            yt_text = text
            if attachment_obj and attachment_obj.is_image:
                image_md = f"\n\n![]({attachment_obj.filename})"
                yt_text = (yt_text + image_md) if yt_text else f"![]({attachment_obj.filename})"
            elif not yt_text and attachment_obj:
                yt_text = f"Прикреплен файл: {attachment_obj.filename}"

            full_yt_text = f"{component_prefix}{yt_text}" if yt_text else ""

            if full_yt_text:
                success_txt, result_txt = send_comment_to_youtrack(
                    issue_id=effective_yt_id,
                    text=full_yt_text,
                    user=request.user
                )
                if success_txt and result_txt:
                    comment.youtrack_id = result_txt
                    comment.save(update_fields=['youtrack_id'])
                elif not success_txt:
                    yt_errors.append(result_txt)

    comments = get_comments_for(obj, request.user)
    return render(request, 'data/object/object_tab_comments.html', {
        'obj': obj, 
        'comments': comments,
        'yt_error': " | ".join(dict.fromkeys(yt_errors)) if yt_errors else None,
        'form_error': form_error
    })


@login_required
@require_http_methods(["GET", "POST"])
def edit_comment_view(request, pk):
    """
    Встроенное редактирование комментария.
    GET  — форма правки (или возврат к просмотру при ?cancel=1);
    POST — сохранение с попыткой обновить текст в YouTrack.
    """
    comment = get_object_or_404(
        Comment.objects.select_related('data_object', 'data_object__model', 'user').prefetch_related('attachments'),
        pk=pk
    )
    obj = comment.data_object

    if not comment.can_be_edited_by(request.user):
        return htmx_error("Редактировать можно только собственные комментарии.", status=403)

    if request.method == 'GET':
        return render(request, 'data/object/comment_item.html', {
            'comment': comment,
            'editing': request.GET.get('cancel') != '1',
            'can_edit': True,
        })

    text = request.POST.get('text', '').strip()
    if not text:
        return render(request, 'data/object/comment_item.html', {
            'comment': comment,
            'editing': True,
            'can_edit': True,
            'error_html': render_to_string('data/includes/htmx_error.html',
                                           {'message': 'Текст комментария не может быть пустым.'}),
        }, status=400)

    yt_errors = []
    if comment.text != text:
        comment.text = text
        comment.save(update_fields=['text'])

        # Локальная правка должна уехать в YouTrack, иначе следующая
        # синхронизация перезапишет её прежним текстом.
        effective_yt_id, target_yt_obj = obj.get_effective_youtrack_issue()
        if effective_yt_id and comment.youtrack_id:
            component_prefix = f"**[{obj.name or obj.model.name}]**\n" if obj != target_yt_obj else ""
            ok, detail = update_comment_in_youtrack(
                issue_id=effective_yt_id,
                comment_yt_id=comment.youtrack_id,
                text=f"{component_prefix}{text}",
                user=request.user
            )
            if not ok:
                logger.warning("Комментарий не обновлён в YouTrack %s: %s", effective_yt_id, detail)
                yt_errors.append(
                    f"Комментарий изменён локально, но не в YouTrack: {detail}. "
                    "Следующая синхронизация вернёт прежний текст."
                )

    html = render_to_string('data/object/object_tab_comments.html', {
        'obj': obj,
        'comments': get_comments_for(obj, request.user),
    }, request=request)
    if yt_errors:
        html += "\n" + yt_toast(yt_errors, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def delete_comments_bulk(request):
    comment_ids = request.POST.getlist('comment_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    effective_yt_id, _ = obj.get_effective_youtrack_issue()
    yt_errors = []
    
    if comment_ids:
        queryset = Comment.objects.filter(uuid__in=comment_ids, data_object=obj).prefetch_related('attachments')
        
        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)
            
        deletable_ids = []
        for comment in queryset:
            remote_ok = True

            if effective_yt_id:
                for att in comment.attachments.all():
                    if att.youtrack_id:
                        ok, detail = delete_attachment_from_youtrack(
                            issue_id=effective_yt_id,
                            attachment_yt_id=att.youtrack_id,
                            user=request.user
                        )
                        if not ok:
                            remote_ok = False
                            yt_errors.append(f"Вложение «{att.filename}» не удалено в YouTrack: {detail}")

                if remote_ok and comment.youtrack_id:
                    ok, detail = delete_comment_from_youtrack(
                        issue_id=effective_yt_id,
                        comment_yt_id=comment.youtrack_id,
                        user=request.user
                    )
                    if not ok:
                        remote_ok = False
                        yt_errors.append(f"Комментарий не удалён в YouTrack: {detail}")

            # Локальную запись удаляем только после подтверждённого удаления
            # в YouTrack, иначе следующая синхронизация вернёт её обратно.
            if remote_ok:
                deletable_ids.append(comment.uuid)

        if deletable_ids:
            Comment.objects.filter(uuid__in=deletable_ids).delete()
        
    comments = get_comments_for(obj, request.user)
    html = render_to_string('data/object/object_tab_comments.html',
                            {'obj': obj, 'comments': comments}, request=request)
    if yt_errors:
        html += "\n" + yt_toast(
            yt_errors + ["Записи оставлены локально, чтобы данные систем не разошлись."],
            request=request
        )
    return HttpResponse(html)


@login_required
@require_POST
def add_attachment_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    file = request.FILES.get('file')
    is_preview_upload = request.POST.get('is_preview') == 'true'
    yt_errors = []

    if file:
        # Проверяем размер, расширение и (для превью) реальную сигнатуру изображения.
        try:
            validate_attachment(file, require_image=is_preview_upload)
        except ValidationError as exc:
            target = '#tab-pane-short-info' if is_preview_upload else '#tab-pane-files'
            return htmx_error(" ".join(exc.messages), retarget=target)

        if is_preview_upload:
            # Снимаем отметку со старого превью
            Attachment.objects.filter(data_object=obj, is_preview=True).update(is_preview=False)

        attachment_obj = Attachment.objects.create(
            user=request.user,
            data_object=obj,
            path=file,
            is_preview=is_preview_upload
        )

        # Отправляем файл в YouTrack (в карточку объекта или первого родителя)
        effective_yt_id, target_yt_obj = obj.get_effective_youtrack_issue()
        if effective_yt_id and attachment_obj.path:
            try:
                with open(attachment_obj.path.path, 'rb') as f:
                    success_file, result_file = upload_attachment_to_youtrack(
                        issue_id=effective_yt_id,
                        file_obj=f,
                        user=request.user
                    )
            except OSError as exc:
                logger.exception("Не удалось прочитать файл вложения для YouTrack")
                success_file, result_file = False, f"файл недоступен для чтения ({exc})"

            if success_file and result_file:
                attachment_obj.youtrack_id = result_file
                attachment_obj.save(update_fields=['youtrack_id'])

                comp_name = obj.name or obj.model.name

                # 1. Превью — прикрепляем файл и публикуем пост с картинкой
                if is_preview_upload:
                    msg_prefix = f"**[{comp_name}]** " if obj != target_yt_obj else ""
                    comment_text = (
                        f"{msg_prefix}Прикреплено фото (превью): {attachment_obj.filename}"
                        f"\n\n![]({attachment_obj.filename})"
                    )
                    ok, detail = send_comment_to_youtrack(
                        issue_id=effective_yt_id, text=comment_text, user=request.user
                    )
                # 2. Документ дочернего компонента — оставляем понятную заметку
                elif obj != target_yt_obj:
                    ok, detail = send_comment_to_youtrack(
                        issue_id=effective_yt_id,
                        text=f"**[{comp_name}]** Прикреплён новый документ: {attachment_obj.filename}",
                        user=request.user
                    )
                else:
                    ok, detail = True, None

                if not ok:
                    yt_errors.append(f"Заметка о файле не добавлена в задачу {effective_yt_id}: {detail}")
            else:
                logger.warning("Вложение не загружено в YouTrack %s: %s", effective_yt_id, result_file)
                yt_errors.append(
                    f"Файл «{attachment_obj.filename}» сохранён локально, "
                    f"но не загружен в задачу {effective_yt_id}: {result_file}"
                )

    if is_preview_upload:
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        html = render_to_string('data/object/object_tab_short_info.html',
                                {'obj': obj, 'preview': preview}, request=request)
    else:
        files = obj.attachments.select_related('user').order_by('-created_at')
        html = render_to_string('data/object/object_tab_files.html',
                                {'obj': obj, 'files': files}, request=request)

    if yt_errors:
        html += "\n" + yt_toast(yt_errors, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def delete_attachments_bulk(request):
    file_ids = request.POST.getlist('file_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    effective_yt_id, _ = obj.get_effective_youtrack_issue()
    yt_errors = []
    
    if file_ids:
        queryset = Attachment.objects.filter(uuid__in=file_ids, data_object=obj)
        
        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)
            
        deletable_ids = []
        for att in queryset:
            remote_ok = True
            if effective_yt_id and att.youtrack_id:
                ok, detail = delete_attachment_from_youtrack(
                    issue_id=effective_yt_id,
                    attachment_yt_id=att.youtrack_id,
                    user=request.user
                )
                if not ok:
                    remote_ok = False
                    yt_errors.append(f"Файл «{att.filename}» не удалён в YouTrack: {detail}")
            if remote_ok:
                deletable_ids.append(att.uuid)

        if deletable_ids:
            # Удаляем поштучно: post_delete стирает файл с диска.
            for att in Attachment.objects.filter(uuid__in=deletable_ids):
                att.delete()
        
    files = obj.attachments.select_related('user').order_by('-created_at')
    html = render_to_string('data/object/object_tab_files.html',
                            {'obj': obj, 'files': files}, request=request)
    if yt_errors:
        html += "\n" + yt_toast(
            yt_errors + ["Файлы оставлены локально, чтобы данные систем не разошлись."],
            request=request
        )
    return HttpResponse(html)


# --- ДЕТАЛИ МОДЕЛИ ---

@login_required
def model_detail_view(request, pk):
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    prev_active_id = request.session.get('active_model_id')
    request.session['active_model_id'] = str(pk)
    
    context = {
        'model_obj': model_obj,
        'active_tab': 'specs',
        'specs_count': len(model_obj.specifications or {}),
        'objects_count': model_obj.data_objects.count(),
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


# --- СПЕЦИФИКАЦИИ МОДЕЛИ ---

@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def model_spec_add_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key', '').strip()
    value = request.POST.get('value', '').strip()
    if key and value:
        specs = model_obj.specifications or {}
        specs[key] = value
        model_obj.specifications = specs
        model_obj.save(update_fields=['specifications'])
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications or {}
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
            model_obj.save(update_fields=['specifications'])
            
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)
        
    # specifications допускает NULL — без защиты это AttributeError и 500
    value = (model_obj.specifications or {}).get(key, '')
    return render(request, 'data/model/inline_spec_row.html', {
        'model_obj': model_obj,
        'key': key,
        'value': value,
        'editing': True
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def model_spec_delete_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key')
    specs = model_obj.specifications or {}
    
    if key in specs:
        specs.pop(key)
        model_obj.specifications = specs
        model_obj.save(update_fields=['specifications'])
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications
    }
    return render(request, 'data/model/model_tab_specs.html', context)


# --- ПОДСКАЗКИ И ПРОВЕРКИ ИМЕН ---

@login_required
def check_model_name_view(request):
    """Подсказка при вводе названия модели: точный дубликат или похожие записи"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    # Уникальность действует в пределах типа оборудования, поэтому при
    # известном типе сужаем проверку до него.
    exact_qs = ObjectModel.objects.select_related('object_type').filter(name__iexact=name)
    type_uuid = request.GET.get('object_type')
    if type_uuid:
        exact_qs = exact_qs.filter(object_type=type_uuid)
    exact_match = exact_qs.first()
    similar_models = []
    if not exact_match:
        stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по', 'of', 'and', 'the'}
        words = [w.lower() for w in name.split() if len(w) >= 2 and w.lower() not in stop_words]
        if words:
            query = Q()
            for word in words:
                query |= Q(name__icontains=word) | Q(object_type__type__icontains=word)
            similar_models = ObjectModel.objects.filter(query).select_related('object_type').distinct()[:5]

    return render(request, 'data/includes/name_check_result.html', {
        'kind': 'model',
        'exact': exact_match,
        'similar': similar_models,
        'modal_id': 'createModelModal',
    })


@login_required
def check_object_name_view(request):
    """Подсказка при вводе имени объекта: точный дубликат или похожие записи"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    exact_match = DataObject.objects.select_related('model').filter(name__iexact=name).first()
    similar_objects = []
    if not exact_match:
        stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по'}
        words = [w.lower() for w in name.split() if len(w) >= 3 and w.lower() not in stop_words]
        if words:
            query = Q()
            for word in words:
                query |= Q(name__icontains=word) | Q(model__name__icontains=word)
            similar_objects = DataObject.objects.filter(query).select_related('model').distinct()[:5]

    return render(request, 'data/includes/name_check_result.html', {
        'kind': 'object',
        'exact': exact_match,
        'similar': similar_objects,
        'modal_id': 'createObjectModal',
    })


@login_required
def suggest_view(request):
    field = request.GET.get('field', '').strip()
    q = request.GET.get('q', '').strip()
    exclude_uuid = request.GET.get('exclude_uuid')
    target_obj_uuid = request.GET.get('target_obj_uuid') or exclude_uuid
    
    results = []
    words = q.split() if q else []
    
    # 1. Типы оборудования
    if field in ['object_type']:
        if q:
            exact = ObjectType.objects.filter(type__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= Q(type__icontains=w)
            partial = ObjectType.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(ObjectType.objects.all().order_by('type')[:8])
        
    # 2. Модели оборудования (включая model_inline)
    elif field in ['model', 'model_inline']:
        if q:
            exact = ObjectModel.objects.filter(name__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= (Q(name__icontains=w) | Q(object_type__type__icontains=w))
            partial = ObjectModel.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(ObjectModel.objects.select_related('object_type').all().order_by('name')[:8])
        
    # 3. Родительские объекты (включая parent_inline и source_object)
    elif field in ['parent', 'parent_inline', 'source_object']:
        qs = DataObject.objects.select_related('model', 'model__object_type').all()
        
        # Защита от зацикливания: исключаем сам объект и всё его поддерево
        if exclude_uuid:
            excluded = {exclude_uuid}
            source = DataObject.objects.filter(pk=exclude_uuid).first()
            if source:
                excluded |= {str(u) for u in source.get_descendant_uuids()}
            qs = qs.exclude(pk__in=excluded)
            
        if q:
            exact = qs.filter(Q(name__iexact=q) | Q(inventory_number__iexact=q))
            word_filter = Q()
            for w in words:
                word_filter &= (Q(name__icontains=w) | Q(inventory_number__icontains=w) | Q(model__name__icontains=w))
            partial = qs.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            # Если поле пустое, показываем первые объекты для быстрого выбора
            results = list(qs.order_by('name')[:8])

    # 4. Правила расчета ТО
    elif field in ['date_update_rule']:
        if q:
            exact = DateUpdateRule.objects.filter(name__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= Q(name__icontains=w)
            partial = DateUpdateRule.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(DateUpdateRule.objects.all().order_by('name')[:8])

    # Создание значения «на лету» поддерживается только для типов оборудования:
    # для правил ТО нужен конструктор параметров, которого нет в подсказках.
    show_create_option = False
    if field == 'object_type' and q:
        has_exact_match = any(item.type.lower() == q.lower() for item in results)
        if not has_exact_match:
            show_create_option = True

    return render(request, 'data/includes/suggestions_list.html', {
        'results': results[:10],
        'field': field,
        'q': q,
        'target_obj_uuid': target_obj_uuid,
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
    
    if uuid_val:
        hidden_value = uuid_val
        if field == 'object_type':
            display_name = get_object_or_404(ObjectType, pk=uuid_val).type
        elif field == 'model':
            model_obj = get_object_or_404(ObjectModel, pk=uuid_val)
            display_name = f"{model_obj.name} ({model_obj.object_type.type})"
        elif field in ['parent', 'source_object']:
            obj = get_object_or_404(DataObject, pk=uuid_val)
            display_name = obj.name or obj.model.name
        elif field == 'date_update_rule':
            display_name = get_object_or_404(DateUpdateRule, pk=uuid_val).name
    elif name_val:
        display_name = f"{name_val} (Создать новое)"
        hidden_name = f"new_{field}"
        hidden_value = name_val
        
    return render(request, 'data/includes/suggestion_selected.html', {
        'field': field,
        'display_name': display_name,
        'hidden_name': hidden_name,
        'hidden_value': hidden_value
    })


@login_required
def reset_suggestion_view(request):
    field = request.GET.get('field')
    placeholders = {
        'object_type': 'Введите тип оборудования...',
        'model': 'Введите модель оборудования...',
        'parent': 'Поиск родительского объекта...',
        'source_object': 'Поиск объекта для копирования...',
        'date_update_rule': 'Введите правило обновления...'
    }
    return render(request, 'data/includes/suggestion_input.html', {
        'field': field,
        'placeholder': placeholders.get(field, 'Начните вводить...')
    })


@login_required
@require_POST
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


# --- КОНСТРУКТОР ПРАВИЛ И РАСЧЕТ СРОКОВ ТО ---

def calculate_next_maintenance_date(data_object, base_date=None):
    """Рассчитывает следующую дату ТО типа datetime.date"""
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
        
    if anchor_type == 'scheduled' and data_object.next_maintenance_date:
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


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def rules_dates_builder_view(request):
    months = request.POST.getlist('fixed_months')
    days = request.POST.getlist('fixed_days')
    
    dates = clean_fixed_dates(months, days)

    new_month = request.POST.get('new_fixed_month')
    new_day = request.POST.get('new_fixed_day')
    if new_month and new_day:
        # Проверяем календарную корректность: 31 февраля отбрасывается,
        # а не подменяется 28-м числом, как раньше.
        dates = clean_fixed_dates(
            [d['month'] for d in dates] + [new_month],
            [d['day'] for d in dates] + [new_day],
        )

    remove_idx = request.POST.get('remove_idx')
    if remove_idx is not None:
        try:
            dates.pop(int(remove_idx))
        except (IndexError, ValueError, TypeError):
            pass
    
    return render(request, 'data/includes/rules_dates_builder.html', {
        'dates': dates,
        'month_names': MONTH_NAMES
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def rule_constructor_view(request):
    strategy = request.GET.get('new_rule_strategy', 'relative')
    anchor = request.GET.get('new_rule_anchor', 'actual')
    context = {
        'strategy': strategy,
        'anchor': anchor,
        'years': 0,
        'months': 6,
        'days': 0,
        'fixed_dates': [],
    }
    if strategy == 'fixed':
        context['month_names'] = MONTH_NAMES
    return render(request, 'data/includes/rule_constructor_fields.html', context)


@login_required
@role_required(['senior', 'admin', 'superuser'])
def toggle_scheduling_mode_view(request):
    mode = request.GET.get('maintenance_scheduling_mode', 'manual')
    is_inline = request.GET.get('inline', '0') == '1'
    template = 'data/includes/scheduling_mode_fields_inline.html' if is_inline else 'data/includes/scheduling_mode_fields.html'
    return render(request, template, {'mode': mode})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_rule_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        mode = request.POST.get('maintenance_scheduling_mode', 'manual')
        
        if mode == 'manual':
            obj.date_update_rule = None
            obj.save(update_fields=['date_update_rule'])
        else:
            rule_uuid = request.POST.get('date_update_rule')
            new_rule_name = request.POST.get('new_date_update_rule')
            
            selected_rule = None
            if rule_uuid:
                selected_rule = DateUpdateRule.objects.filter(pk=rule_uuid).first()
                if selected_rule is None:
                    return htmx_error("Выбранное правило не найдено — обновите страницу.", status=404)
            elif new_rule_name:
                # Создание правила «на лету»: те же проверки, что и в настройках.
                form = DateUpdateRuleForm(
                    {
                        'name': new_rule_name,
                        'new_rule_strategy': request.POST.get('new_rule_strategy', 'relative'),
                        'new_rule_anchor': request.POST.get('new_rule_anchor', 'actual'),
                        'new_rule_years': request.POST.get('new_rule_years') or 0,
                        'new_rule_months': request.POST.get('new_rule_months') or 0,
                        'new_rule_days': request.POST.get('new_rule_days') or 0,
                    },
                    fixed_months=request.POST.getlist('fixed_months'),
                    fixed_days=request.POST.getlist('fixed_days'),
                )
                if form.is_valid():
                    selected_rule = form.save()
                else:
                    # Правило с таким названием уже есть — используем его,
                    # в остальных случаях показываем ошибку.
                    existing = DateUpdateRule.objects.filter(name__iexact=new_rule_name.strip()).first()
                    if existing is None:
                        return htmx_error(form_errors_text(form),
                                          retarget='#edit-rule-modal-content .form-feedback')
                    selected_rule = existing
                
            if selected_rule:
                obj.date_update_rule = selected_rule
                obj.next_maintenance_date = calculate_next_maintenance_date(obj, base_date=timezone.localdate())
                obj.save(update_fields=['date_update_rule', 'next_maintenance_date'])
                
        rule_name = obj.date_update_rule.name if obj.date_update_rule else 'ручной ввод'
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='rule_change', 
            action=f"Изменено правило планирования ТО на: {rule_name}."
        )
        
        oob_rule_html = render_to_string('data/object/inline_rule_value.html', {
            'obj': obj,
            'oob': True,
        }, request=request)
        
        oob_pill_html = render_maintenance_pill(obj, request=request)
        
        return HttpResponse(f"{oob_rule_html}\n{oob_pill_html}")

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
            if not isinstance(dates, list):
                dates = []
            formatted_dates = [
                f"{d.get('day')} {month_names.get(d.get('month'), '')}"
                for d in dates if isinstance(d, dict)
            ]
            rule_details = f"Сезонные даты: {', '.join(formatted_dates)}"

    return render(request, 'data/object/edit_rule_modal_body.html', {
        'obj': obj,
        'current_mode': current_mode,
        'rule_details': rule_details
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_object_model_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_model.html', {'obj': obj, 'editing': False})
        
    if request.method == 'POST':
        model_uuid = request.POST.get('model')
        
        if model_uuid:
            new_model = get_object_or_404(ObjectModel, pk=model_uuid)
            old_model = obj.model
            
            if old_model != new_model:
                obj.model = new_model
                obj.save(update_fields=['model'])
                
                ActionHistory.objects.create(
                    user=request.user,
                    data_object=obj,
                    action_type='update',
                    action=f"Модель оборудования изменена с '{old_model.name}' на '{new_model.name}'."
                )
                
        model_html = render_to_string('data/object/inline_model.html', {'obj': obj, 'editing': False}, request=request)
        sidebar_node_html = render_to_string('data/tree/object_tree_node_label.html', {
            'node': obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(model_html + "\n" + sidebar_node_html)
        
    return render(request, 'data/object/inline_model.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_model_name_view(request, pk):
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': False})
        
    if request.method == 'POST':
        old_name = model_obj.name
        new_name = request.POST.get('name', '').strip()
        
        if new_name and old_name != new_name:
            model_obj.name = new_name
            try:
                with transaction.atomic():
                    model_obj.save(update_fields=['name'])
            except IntegrityError:
                model_obj.name = old_name
                return htmx_error(
                    f'Модель «{new_name}» уже существует для типа «{model_obj.object_type.type}».',
                    status=409,
                    retarget='#model-name-error'
                )
            
        model_name_html = render_to_string('data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': False}, request=request)
        sidebar_model_node_html = render_to_string('data/tree/model_tree_node_label.html', {
            'model': model_obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(model_name_html + "\n" + sidebar_model_node_html)
        
    return render(request, 'data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def set_preview_attachment_view(request, pk):
    attachment = get_object_or_404(Attachment.objects.select_related('data_object'), pk=pk)
    obj = attachment.data_object
    
    if not attachment.is_image:
        return HttpResponse("Только изображение может быть превью", status=400)
    
    if attachment.is_preview:
        attachment.is_preview = False
        attachment.save(update_fields=['is_preview'])
    else:
        Attachment.objects.filter(data_object=obj, is_preview=True).update(is_preview=False)
        attachment.is_preview = True
        attachment.save(update_fields=['is_preview'])

    files = obj.attachments.select_related('user', 'comment').order_by('-created_at')
    return render(request, 'data/object/object_tab_files.html', {'obj': obj, 'files': files})


@login_required
@require_POST
def sync_youtrack_view(request, pk):
    """Явный запуск синхронизации с YouTrack; карточка перерисовывается с результатом"""
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    
    sync_success, sync_message = sync_issue_from_youtrack(obj, request.user)
    if sync_success:
        obj.refresh_from_db()

    return render(request, 'data/object/sync_status.html', {
        'obj': obj,
        'sync_status': 'success' if sync_success else 'error',
        'sync_message': sync_message,
        'comments_count': obj.comments.count(),
        'files_count': obj.attachments.count(),
        'history_count': obj.actions.count(),
    })


# --- СЕРВИС И ПРЕДСТАВЛЕНИЯ КЛОНИРОВАНИЯ ОБЪЕКТОВ ---

def deep_clone_object(source_obj, new_root_name, new_parent=None, source_root_name=None, user=None,
                      clone_children=True, _visited=None, _depth=0):
    """
    Рекурсивно клонирует объект и всю его дочернюю иерархию с умным суффиксированием:
    - Если в названии детали было имя старого родителя -> подменяем на новое имя.
    - Если название детали общее (например, "Блок питания") -> добавляем суффикс "(НовоеИмя)".

    Защищено от циклов в дереве (visited) и от чрезмерной глубины. Атомарность
    всей операции обеспечивает вызывающий код (clone_object_view).
    """
    if _visited is None:
        _visited = set()
    if source_obj.uuid in _visited or _depth > DataObject.MAX_TREE_DEPTH:
        return None
    _visited.add(source_obj.uuid)

    is_root = (source_root_name is None)
    if is_root:
        source_root_name = source_obj.name or (source_obj.model.name if source_obj.model else "")
    
    # 1. Формируем имя для текущего узла
    if is_root:
        # Это сам корневой объект
        obj_name = new_root_name
    else:
        # Это дочерняя деталь
        orig_name = source_obj.name or (source_obj.model.name if source_obj.model else "Компонент")
        if source_root_name and source_root_name in orig_name:
            # Заменяем старое имя родителя на новое
            obj_name = orig_name.replace(source_root_name, new_root_name)
        else:
            # Если имя общее — приписываем суффикс нового родителя
            obj_name = f"{orig_name} ({new_root_name})"

    # 2. Создаем копию объекта
    cloned_obj = DataObject.objects.create(
        name=obj_name,
        model=source_obj.model,
        parent=new_parent,
        inventory_number=None,       # Очищаем инвентарник
        youtrack_issue_id=None,      # Очищаем задачу YouTrack
        next_maintenance_date=source_obj.next_maintenance_date,
        date_update_rule=source_obj.date_update_rule,
        description=source_obj.description
    )
    
    # 3. Фиксируем создание в истории объекта
    ActionHistory.objects.create(
        user=user,
        data_object=cloned_obj,
        action_type='create',
        action=f"Объект создан копированием из '{source_obj.name or source_obj.model.name}'."
    )
    
    # 4. Рекурсивно клонируем всех потомков
    if clone_children:
        for child in source_obj.children.all().order_by('name'):
            deep_clone_object(
                source_obj=child,
                new_root_name=new_root_name,
                new_parent=cloned_obj,
                source_root_name=source_root_name,
                user=user,
                clone_children=True,
                _visited=_visited,
                _depth=_depth + 1,
            )
            
    return cloned_obj



@login_required
@role_required(['senior', 'admin', 'superuser'])
def clone_object_modal_view(request):
    """Отображение модального окна тиражирования объекта"""
    active_object_id = request.GET.get('active_object_id') or request.session.get('active_object_id')
    preselected_object = None
    if active_object_id:
        try:
            preselected_object = DataObject.objects.get(pk=active_object_id)
        except DataObject.DoesNotExist:
            pass

    return render(request, 'data/includes/clone_object_modal_body.html', {
        'preselected_object': preselected_object
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def clone_object_view(request):
    """Создание копии объекта (обычной или с дочерними элементами)"""
    if request.method == 'POST':
        # Поддерживаем оба имени параметра: source_object и parent (из подсказок поиска)
        source_uuid = request.POST.get('source_object') or request.POST.get('parent')
        new_name = request.POST.get('new_name', '').strip()
        clone_children = request.POST.get('clone_children') == 'on'
        keep_parent = request.POST.get('keep_parent') == 'on'
        
        if source_uuid:
            source_obj = get_object_or_404(DataObject.objects.select_related('parent'), pk=source_uuid)
            
            if not new_name:
                new_name = f"{source_obj.name or source_obj.model.name} (копия)"
                
            new_parent = source_obj.parent if keep_parent else None
            
            # Вся копия создаётся одной транзакцией: при ошибке в середине
            # не остаётся наполовину склонированного поддерева.
            with transaction.atomic():
                deep_clone_object(
                    source_obj=source_obj,
                    new_root_name=new_name,
                    new_parent=new_parent,
                    user=request.user,
                    clone_children=clone_children
                )

        roots = DataObject.objects.filter(parent__isnull=True).prefetch_related('children').order_by('name')
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return HttpResponse("Метод не разрешен", status=405)
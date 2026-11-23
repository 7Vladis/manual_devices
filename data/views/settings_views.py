"""Панель настроек: типы оборудования, правила планирования."""

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import DateUpdateRuleForm, RuleObjectLinksForm, MONTH_NAMES
from ..models import ActionHistory, DataObject, DateUpdateRule, MaintenancePlan, ObjectModel, ObjectType
from users.decorators import role_required

from ..services.maintenance import add_plan, calculate_plan_date, refresh_object_schedule, remove_plan
from .common import form_errors_text, htmx_error


def rules_queryset():
    return DateUpdateRule.objects.prefetch_related(
        Prefetch(
            'plans',
            queryset=MaintenancePlan.objects.select_related('data_object__model')
            .order_by('data_object__name'),
        )
    ).annotate(objects_count=Count('plans')).order_by('name')


def format_fixed_rule_dates(rule):
    rule_data = rule.rule or {}
    if rule_data.get('strategy') == 'fixed':
        rule.formatted_fixed_dates = ', '.join(
            f"{item.get('day', 1)} {MONTH_NAMES.get(item.get('month', 1), '')}"
            for item in rule_data.get('value', [])
        )
    return rule


def rules_with_objects():
    queryset = rules_queryset()
    for rule in queryset:
        format_fixed_rule_dates(rule)
    return queryset


def rule_with_objects(pk):
    return format_fixed_rule_dates(get_object_or_404(rules_queryset(), pk=pk))


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
        context['rules'] = rules_with_objects()

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
    if not name:
        return htmx_error("Укажите название типа оборудования.",
                          retarget='#addObjectTypeModal .form-feedback')

    if ObjectType.objects.filter(type__iexact=name).exists():
        return htmx_error(f"Тип оборудования «{name}» уже существует.",
                          status=409, retarget='#addObjectTypeModal .form-feedback')

    try:
        with transaction.atomic():
            ObjectType.objects.create(type=name)
    except IntegrityError:
        return htmx_error(f"Тип оборудования «{name}» уже существует.",
                          status=409, retarget='#addObjectTypeModal .form-feedback')

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

        try:
            with transaction.atomic():
                rule = form.save()
                for obj in form.cleaned_data['object_ids']:
                    add_plan(obj, rule=rule)
                    ActionHistory.objects.create(
                        user=request.user,
                        data_object=obj,
                        action_type='rule_change',
                        action=f'Добавлен план обслуживания: {rule.name}.',
                    )
        except IntegrityError:
            return htmx_error(
                'Не удалось привязать один из объектов. Обновите форму и попробуйте снова.',
                status=409, retarget='#create-rule-error',
            )

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

        # Правило изменилось — прежние даты рассчитаны по старым параметрам,
        # поэтому считаем заново от сегодняшнего дня, а не от старого срока.
        # Пересчитывается каждый план этого правила; у соседних планов объекта
        # сроки свои и остаются нетронутыми, а сводка объекта обновляется по
        # всем его планам сразу.
        for plan in rule_obj.plans.select_related('rule', 'data_object'):
            plan.next_date = calculate_plan_date(
                plan, base_date=timezone.localdate(), use_anchor=False
            )
            plan.save(update_fields=['next_date'])
            refresh_object_schedule(plan.data_object)

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
        'rule_error_id': f'rule-error-{rule_obj.uuid}',
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
    if rule.plans.exists():
        return htmx_error(
            'Нельзя удалить: правило используется в активных объектах.',
            retarget=f'#rule-error-{rule.uuid}'
        )
    rule.delete()
    
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=rules'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
def rule_objects_view(request, pk):
    rule = rule_with_objects(pk)
    return render(request, 'data/settings/rule_objects.html', {'item': rule})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_rule_objects_view(request, pk):
    rule = rule_with_objects(pk)
    return render(request, 'data/settings/rule_objects_editor.html', {'item': rule})


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def update_rule_objects_view(request, pk):
    rule = get_object_or_404(DateUpdateRule, pk=pk)
    form = RuleObjectLinksForm(request.POST, rule=rule)
    error_target = f'#rule-objects-error-{rule.uuid}'
    if not form.is_valid():
        return htmx_error(form_errors_text(form), retarget=error_target)

    try:
        with transaction.atomic():
            for plan in form.cleaned_data['remove_plan_ids'].select_related('data_object'):
                obj = plan.data_object
                remove_plan(plan)
                ActionHistory.objects.create(
                    user=request.user,
                    data_object=obj,
                    action_type='rule_change',
                    action=f'Убран план обслуживания: {rule.name}.',
                )

            linked_ids = set(rule.plans.values_list('data_object_id', flat=True))
            for obj in form.cleaned_data['object_ids']:
                if obj.pk in linked_ids:
                    continue
                add_plan(obj, rule=rule)
                linked_ids.add(obj.pk)
                ActionHistory.objects.create(
                    user=request.user,
                    data_object=obj,
                    action_type='rule_change',
                    action=f'Добавлен план обслуживания: {rule.name}.',
                )
    except IntegrityError:
        return htmx_error(
            'Не удалось изменить привязки. Обновите список и попробуйте снова.',
            status=409, retarget=error_target,
        )

    rule = rule_with_objects(pk)
    return render(request, 'data/settings/rule_card.html', {
        'item': rule,
        'objects_expanded': True,
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def select_rule_object_view(request, pk):
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    return render(request, 'data/settings/rule_object_selected.html', {
        'obj': obj,
        'selection_id': request.GET.get('selection_id', 'create'),
        'rule_id': request.GET.get('rule_id', ''),
    })

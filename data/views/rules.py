"""Конструктор правил расчёта дат ТО."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import DateUpdateRuleForm, MONTH_NAMES, clean_fixed_dates, parse_maintenance_date
from ..models import ActionHistory, DataObject, DateUpdateRule, MaintenancePlan
from users.decorators import role_required

# Короткие имена для расшифровки сезонного правила в списке планов.
MONTH_SHORT = {
    1: 'янв', 2: 'фев', 3: 'мар', 4: 'апр', 5: 'май', 6: 'июн',
    7: 'июл', 8: 'авг', 9: 'сен', 10: 'окт', 11: 'ноя', 12: 'дек',
}

from ..services.maintenance import add_plan, remove_plan
from .common import form_errors_text, htmx_error, render_maintenance_pill


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
        'month_names': MONTH_NAMES,
        'mode': 'edit' if request.POST.get('mode') == 'edit' else 'create',
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def rule_constructor_view(request):
    strategy = request.GET.get('new_rule_strategy', 'relative')
    anchor = request.GET.get('new_rule_anchor', 'actual')
    context = {
        'strategy': strategy,
        'anchor': anchor,
        # Окна создания и редактирования правила живут на одной странице,
        # поэтому идентификаторы полей разделяются по режиму.
        'mode': 'edit' if request.GET.get('mode') == 'edit' else 'create',
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


def describe_rule(rule):
    """
    Короткая расшифровка правила для списка планов.

    Нужна по правилу, а не по объекту: планов у объекта несколько, и у
    каждого своя строка с пояснением, откуда взялся срок.
    """
    if rule is None:
        return 'Срок двигает инженер вручную.'

    rule_data = rule.rule or {}
    strategy = rule_data.get('strategy', 'relative')

    if strategy == 'relative':
        anchor_text = ('фактического выполнения'
                       if rule_data.get('anchor') == 'actual' else 'планового срока')
        val = rule_data.get('value', {})
        return (f"Интервал от {anchor_text}: {val.get('years', 0)}г. "
                f"{val.get('months', 0)}мес. {val.get('days', 0)}дн.")

    if strategy == 'fixed':
        dates = rule_data.get('value', [])
        if not isinstance(dates, list):
            dates = []
        formatted = [f"{d.get('day')} {MONTH_SHORT.get(d.get('month'), '')}"
                     for d in dates if isinstance(d, dict)]
        return f"Сезонные даты: {', '.join(formatted)}"

    return None


def plans_context(obj):
    """Состояние окна планирования: что уже назначено объекту."""
    plans = list(obj.plans.select_related('rule').all())
    for plan in plans:
        plan.details = describe_rule(plan.rule)
    return {
        'obj': obj,
        'plans': plans,
        # Срок вручную у объекта один — ограничение БД то же самое. Пока он
        # есть, форму второго такого показывать незачем.
        'has_manual_plan': any(plan.rule_id is None for plan in plans),
    }


def pick_rule(request):
    """
    Правило из формы: выбранное подсказкой или созданное на лету.

    Возвращает пару «правило, ответ об ошибке». Создание на лету оставлено
    здесь же, как было у прежнего окна: правило нередко придумывают ровно
    в тот момент, когда назначают его объекту.
    """
    rule_uuid = request.POST.get('date_update_rule')
    new_rule_name = request.POST.get('new_date_update_rule')

    if rule_uuid:
        rule = DateUpdateRule.objects.filter(pk=rule_uuid).first()
        if rule is None:
            return None, htmx_error('Выбранное правило не найдено — обновите страницу.', status=404)
        return rule, None

    if not new_rule_name:
        return None, htmx_error('Выберите правило или создайте новое.',
                                retarget='#plans-modal-content .form-feedback')

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
        return form.save(), None

    # Правило с таким названием уже есть — берём его; остальные ошибки показываем.
    existing = DateUpdateRule.objects.filter(name__iexact=new_rule_name.strip()).first()
    if existing is None:
        return None, htmx_error(form_errors_text(form),
                                retarget='#plans-modal-content .form-feedback')
    return existing, None


def plans_response(request, obj):
    """
    Ответ после правки планов: само окно плюс OOB-части карточки.

    Окно остаётся открытым — планов обычно назначают несколько подряд, и
    закрывать его после каждого значило бы заставлять открывать заново.
    Ячейка «Правило ТО» и пилюля срока уходят OOB-свопом: они показывают
    сводку, а она изменилась.
    """
    html = render_to_string('data/object/object_plans_modal_body.html',
                            plans_context(obj), request=request)
    oob_rule = render_to_string('data/object/inline_rule_value.html',
                                {'obj': obj, 'oob': True}, request=request)
    oob_pill = render_maintenance_pill(obj, request=request)
    return HttpResponse(f"{html}\n{oob_rule}\n{oob_pill}")


@login_required
@role_required(['senior', 'admin', 'superuser'])
def object_plans_view(request, pk):
    """Окно планирования: список планов объекта."""
    obj = get_object_or_404(DataObject, pk=pk)
    return render(request, 'data/object/object_plans_modal_body.html', plans_context(obj))


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def add_object_plan_view(request, pk):
    """Добавляет объекту план — по правилу или со сроком вручную."""
    obj = get_object_or_404(DataObject, pk=pk)

    if request.POST.get('plan_kind') == 'manual':
        try:
            next_date = parse_maintenance_date(request.POST.get('next_date'), "Дата следующего ТО")
        except ValidationError as exc:
            return htmx_error(" ".join(exc.messages),
                              retarget='#plans-modal-content .form-feedback')
        if next_date is None:
            return htmx_error('Укажите дату следующего ТО.',
                              retarget='#plans-modal-content .form-feedback')
        rule = None
    else:
        rule, error = pick_rule(request)
        if error:
            return error
        next_date = None

    try:
        add_plan(obj, rule=rule, next_date=next_date)
    except IntegrityError:
        # Текст записан в самом ограничении: одно правило объекту дважды
        # не назначить, срок вручную у него один.
        return htmx_error('Такой план у объекта уже есть.', status=409,
                          retarget='#plans-modal-content .form-feedback')

    ActionHistory.objects.create(
        user=request.user,
        data_object=obj,
        action_type='rule_change',
        action=f"Добавлен план обслуживания: {rule.name if rule else 'срок вручную'}.",
    )
    return plans_response(request, obj)


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def delete_object_plan_view(request, pk):
    """Убирает план объекта."""
    plan = get_object_or_404(MaintenancePlan.objects.select_related('rule', 'data_object'), pk=pk)
    obj = plan.data_object
    title = plan.title

    remove_plan(plan)

    ActionHistory.objects.create(
        user=request.user,
        data_object=obj,
        action_type='rule_change',
        action=f"Убран план обслуживания: {title}.",
    )
    return plans_response(request, obj)

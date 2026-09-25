"""Конструктор правил расчёта дат ТО."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import DateUpdateRuleForm, MONTH_NAMES, clean_fixed_dates
from ..models import ActionHistory, DataObject, DateUpdateRule
from users.decorators import role_required

from ..services.maintenance import calculate_next_maintenance_date
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
                # Правило только что назначено — стартуем от сегодняшнего дня.
                obj.next_maintenance_date = calculate_next_maintenance_date(
                    obj, base_date=timezone.localdate(), use_anchor=False
                )
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

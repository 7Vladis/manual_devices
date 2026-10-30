"""Фиксация обслуживания и синхронизация карточки с YouTrack."""

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from ..forms import parse_maintenance_date
from ..models import ActionHistory, DataObject, YouTrackJob
from .. import youtrack_services
from ..services import youtrack_queue
from ..services.maintenance import (calculate_plan_date, get_single_plan,
                                    refresh_object_schedule, set_manual_date)
from ..models import MaintenancePlan
from .common import htmx_error, render_maintenance_pill, yt_toast

logger = logging.getLogger('data')


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
        
        # Работу закрывают по плану: он говорит, какой срок закрыт и какое
        # правило выполнено. Какой именно — выбирают в окне, когда планов
        # несколько; при одном выбирать не из чего, и он приходит скрытым
        # полем. Чужой план к объекту не привяжешь: выборка сужена по нему.
        plan_uuid = request.POST.get('plan')
        plan = None
        if plan_uuid:
            plan = MaintenancePlan.objects.filter(pk=plan_uuid, data_object=obj).first()
        if plan is None:
            plan = get_single_plan(obj)

        # Плановое событие, которое закрывает эта работа, — это срок,
        # назначенный ДО обновления. Запоминаем его до перезаписи.
        closed_plan_date = None if is_unplanned else (plan.next_date if plan else None)
        closed_rule = plan.rule if (plan and not is_unplanned) else None

        # 1. Если ТО ПЛАНОВОЕ — обновляем дату следующего обслуживания
        if not is_unplanned and date_str:
            try:
                new_date = parse_maintenance_date(date_str, "Дата следующего ТО")
            except ValidationError as exc:
                return htmx_error(" ".join(exc.messages),
                                  retarget='#service-modal-content .form-feedback')
            if plan is None:
                plan = set_manual_date(obj, new_date)
            else:
                plan.next_date = new_date
                plan.save(update_fields=['next_date'])
                refresh_object_schedule(obj)
        
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
            maintenance_rule=closed_rule,
        )

        # 4. Списание времени уходит в очередь: ТО уже зафиксировано локально,
        #    и ждать ответа YouTrack в этом запросе незачем.
        if effective_yt_id and spent_time:
            youtrack_queue.enqueue(
                YouTrackJob.KIND_WORK_ITEM, obj, request.user,
                {'history': str(history_entry.pk), 'spent_time': spent_time},
            )

        if request.GET.get('ctx') == 'plan':
            # Окно открыли из плана ТО: там нет ни строки дерева, ни карточки
            # объекта. Список перезагрузит себя сам по событию objectServiced,
            # а лишние OOB-части ушли бы в никуда и сорили бы ошибками в консоли.
            parts = []
        else:
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

    # Каждому плану — свой предложенный срок: интервалы у них разные, и
    # подставлять один на все значило бы сдвинуть не тот график. Окно кладёт
    # дату в `data-next-date`, а выбор плана переносит её в поле.
    plans = list(obj.plans.select_related('rule').all())
    for plan in plans:
        plan.proposed_date = calculate_plan_date(plan)

    return render(request, 'data/tree/service_modal_body.html', {
        'obj': obj,
        'plans': plans,
        'proposed_date': plans[0].proposed_date if plans else None,
        'effective_yt_id': effective_yt_id,
        'target_yt_obj': target_yt_obj
    })


# Сколько раз карточка спросит о состоянии, прежде чем признать, что
# отвечать некому. При опросе раз в две секунды это около минуты — дольше
# ждать нечего: либо воркер не запущен, либо он не справляется.
SYNC_POLL_LIMIT = 30


@login_required
@require_POST
def sync_youtrack_view(request, pk):
    """
    Ставит синхронизацию в очередь и отдаёт пилюлю ожидания.

    Сам запрос в YouTrack идёт из воркера: держать ради него HTTP-соединение
    незачем, а карточка всё равно уже показана.
    """
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)

    # Своя задача не обязательна: карточка дочернего объекта читает задачу
    # предка — там лежат и её записи.
    if not obj.effective_youtrack_issue_id:
        return render(request, 'data/object/sync_status.html',
                      {'obj': obj, 'sync_state': 'none'})

    # Без токена обмен человеку недоступен: ходить в YouTrack нечем.
    # Пилюли у него нет вовсе — ни запроса, ни индикатора, который всё равно
    # нечем погасить. Про сам токен говорит кнопка «YouTrack» в шапке и
    # предупреждение в ленте комментариев.
    if youtrack_queue.enqueue(YouTrackJob.KIND_SYNC, obj, request.user) is None:
        return render(request, 'data/object/sync_pill_empty.html', {})

    return render(request, 'data/object/sync_status.html',
                  {'obj': obj, 'sync_state': 'pending', 'poll': 1})


@login_required
def sync_status_view(request, pk):
    """
    Состояние обмена с YouTrack по объекту: карточка опрашивает этот адрес,
    пока в очереди есть незавершённые задания.
    """
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    state, message = youtrack_queue.object_state(obj, request.user)

    try:
        poll = int(request.GET.get('poll', 1))
    except (TypeError, ValueError):
        poll = 1

    if state == 'pending' and poll >= SYNC_POLL_LIMIT:
        # Задания живы, но никто их не берёт — молчать об этом нельзя,
        # иначе пользователь будет считать данные свежими.
        state = 'stalled'

    context = {'obj': obj, 'sync_state': state, 'sync_message': message}

    if state == 'pending':
        context['poll'] = poll + 1
    elif state == 'done':
        # Счётчики и описание обновляются только здесь: до конца обмена
        # они показывали бы промежуточное состояние.
        context.update({
            'counts_ready': True,
            'comments_count': obj.comments.count(),
            'files_count': obj.attachments.count(),
            'history_count': obj.actions.count(),
        })

    return render(request, 'data/object/sync_status.html', context)


@login_required
@require_POST
def retry_youtrack_view(request, pk):
    """Повторяет неудавшиеся задания объекта по явному нажатию на пилюлю."""
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    restarted = youtrack_queue.retry_failed(obj, request.user)

    state = 'pending' if restarted else 'idle'
    return render(request, 'data/object/sync_status.html',
                  {'obj': obj, 'sync_state': state, 'poll': 1})

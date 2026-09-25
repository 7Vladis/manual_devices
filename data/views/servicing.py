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
from ..services import youtrack_queue
from ..services.maintenance import calculate_next_maintenance_date
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

    proposed_date = calculate_next_maintenance_date(obj)
    
    return render(request, 'data/tree/service_modal_body.html', {
        'obj': obj,
        'proposed_date': proposed_date,
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

    if not obj.youtrack_issue_id:
        return render(request, 'data/object/sync_status.html',
                      {'obj': obj, 'sync_state': 'none'})

    youtrack_queue.enqueue(YouTrackJob.KIND_SYNC, obj, request.user)
    return render(request, 'data/object/sync_status.html',
                  {'obj': obj, 'sync_state': 'pending', 'poll': 1})


@login_required
def sync_status_view(request, pk):
    """
    Состояние обмена с YouTrack по объекту: карточка опрашивает этот адрес,
    пока в очереди есть незавершённые задания.
    """
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    state, message = youtrack_queue.object_state(obj)

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
    restarted = youtrack_queue.retry_failed(obj)

    state = 'pending' if restarted else 'idle'
    return render(request, 'data/object/sync_status.html',
                  {'obj': obj, 'sync_state': state, 'poll': 1})

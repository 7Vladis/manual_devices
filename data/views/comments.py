"""Комментарии и вложения объекта."""

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_http_methods

from ..services import youtrack_queue, youtrack_sync
from ..models import Attachment, Comment, DataObject, YouTrackJob
from ..validators import validate_attachment
from users.decorators import role_required

from .common import get_comments_for, get_files_for, htmx_error, sync_pill_oob, yt_toast

logger = logging.getLogger('data')


@login_required
@require_POST
def add_comment_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    text = request.POST.get('text', '').strip()
    file = request.FILES.get('file')
    form_error = None

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

        # Отправка уходит в очередь: комментарий уже сохранён, и держать
        # запрос ради обращения к YouTrack незачем.
        if obj.effective_youtrack_issue_id:
            youtrack_queue.enqueue(YouTrackJob.KIND_COMMENT, obj, request.user, {
                'comment': str(comment.pk),
                'attachment': str(attachment_obj.pk) if attachment_obj else None,
                'raw_text': text,
            })

    # Новый комментарий всегда на первой странице: лента от новых к старым.
    html = render_to_string('data/object/object_tab_comments.html', {
        'obj': obj,
        'comments': get_comments_for(obj, request.user),
        'form_error': form_error,
    }, request=request)
    return HttpResponse(html + "\n" + sync_pill_oob(obj, request))


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

    queued = False
    if comment.text != text:
        comment.text = text
        comment.save(update_fields=['text'])
        # Правка обязана уехать в YouTrack, иначе следующая синхронизация
        # вернёт прежний текст. Отправит её воркер.
        if obj.effective_youtrack_issue_id and comment.youtrack_id:
            youtrack_queue.enqueue(YouTrackJob.KIND_COMMENT_EDIT, obj, request.user,
                                   {'comment': str(comment.pk)})
            queued = True

    html = render_to_string('data/object/object_tab_comments.html', {
        'obj': obj,
        'comments': get_comments_for(obj, request.user, request.GET.get('page')),
    }, request=request)
    if queued:
        html += "\n" + sync_pill_oob(obj, request)
    return HttpResponse(html)


@login_required
@require_POST
def delete_comments_bulk(request):
    comment_ids = request.POST.getlist('comment_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    yt_errors = []

    if comment_ids:
        queryset = Comment.objects.filter(uuid__in=comment_ids, data_object=obj).prefetch_related('attachments')

        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)

        deletable_ids, yt_errors = youtrack_sync.remove_comments(obj, queryset, request.user)

        if deletable_ids:
            Comment.objects.filter(uuid__in=deletable_ids).delete()
        
    comments = get_comments_for(obj, request.user, request.POST.get('page'))
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
    queued = False

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

        if obj.effective_youtrack_issue_id:
            youtrack_queue.enqueue(YouTrackJob.KIND_ATTACHMENT, obj, request.user,
                                   {'attachment': str(attachment_obj.pk)})
            queued = True

    if is_preview_upload:
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        html = render_to_string('data/object/object_tab_short_info.html',
                                {'obj': obj, 'preview': preview}, request=request)
    else:
        html = render_to_string('data/object/object_tab_files.html',
                                {'obj': obj, 'files': get_files_for(obj)}, request=request)

    if queued:
        html += "\n" + sync_pill_oob(obj, request)
    return HttpResponse(html)


@login_required
@require_POST
def delete_attachments_bulk(request):
    file_ids = request.POST.getlist('file_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    yt_errors = []

    if file_ids:
        queryset = Attachment.objects.filter(uuid__in=file_ids, data_object=obj)

        if not request.user.can_manage_content:
            queryset = queryset.filter(user=request.user)

        deletable_ids, yt_errors = youtrack_sync.remove_attachments(obj, queryset, request.user)

        if deletable_ids:
            # Удаляем поштучно: post_delete стирает файл с диска.
            for att in Attachment.objects.filter(uuid__in=deletable_ids):
                att.delete()
        
    files = get_files_for(obj, request.POST.get('page'))
    html = render_to_string('data/object/object_tab_files.html',
                            {'obj': obj, 'files': files}, request=request)
    if yt_errors:
        html += "\n" + yt_toast(
            yt_errors + ["Файлы оставлены локально, чтобы данные систем не разошлись."],
            request=request
        )
    return HttpResponse(html)


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

    return render(request, 'data/object/object_tab_files.html',
                  {'obj': obj, 'files': get_files_for(obj, request.GET.get('page'))})

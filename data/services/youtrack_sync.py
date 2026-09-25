"""
Оркестрация обмена с YouTrack: что сделать до и после обращения к клиенту.

Здесь нет ни HTTP, ни шаблонов. Функции принимают доменные объекты и
возвращают список человекочитаемых ошибок; представление само решает,
показать их тостом или врезкой в форму.

Источник истины — локальная БД, YouTrack зеркало. Отсюда два правила,
которые здесь и живут:

- запись, созданная локально, отправляется в YouTrack, и полученный id
  сохраняется рядом с ней;
- локальная запись удаляется только после подтверждённого удаления в
  YouTrack, иначе следующая синхронизация вернёт её обратно.

Сквозные вызовы (`sync_issue_from_youtrack`) сюда не вынесены: оборачивать
одну строку в функцию нечем.
"""

import logging

from data import youtrack_services

logger = logging.getLogger('data')


def _component_prefix(obj, target_obj, template='**[{}]** '):
    """
    Пометка компонента для записи, которая уходит в задачу родителя.

    Если у объекта нет своей задачи, запись попадает в задачу предка —
    без имени компонента в ней не разобрать, о какой единице речь.
    """
    if obj == target_obj:
        return ''
    return template.format(obj.name or obj.model.name)


def push_comment(comment, attachment, raw_text, user):
    """
    Отправляет в YouTrack только что созданный комментарий и его вложение.

    Возвращает список ошибок; id успешно отправленных записей проставляются
    в переданные объекты.
    """
    obj = comment.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id:
        return []

    errors = []

    if attachment and attachment.path:
        try:
            with open(attachment.path.path, 'rb') as f:
                ok, result = youtrack_services.upload_attachment_to_youtrack(
                    issue_id=issue_id,
                    file_obj=f,
                    user=user
                )
                if ok and result:
                    attachment.youtrack_id = result
                    attachment.save(update_fields=['youtrack_id'])
                elif not ok:
                    errors.append(result)
        except OSError as exc:
            logger.exception("Не удалось прочитать файл комментария для YouTrack")
            errors.append(f"Файл сохранён локально, но не прочитан для отправки в YouTrack: {exc}")

    yt_text = raw_text
    if attachment and attachment.is_image:
        image_md = f"\n\n![]({attachment.filename})"
        yt_text = (yt_text + image_md) if yt_text else f"![]({attachment.filename})"
    elif not yt_text and attachment:
        yt_text = f"Прикреплен файл: {attachment.filename}"

    full_text = f"{_component_prefix(obj, target_obj)}{yt_text}" if yt_text else ""
    if full_text:
        ok, result = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=full_text,
            user=user
        )
        if ok and result:
            comment.youtrack_id = result
            comment.save(update_fields=['youtrack_id'])
        elif not ok:
            errors.append(result)

    return errors


def push_comment_edit(comment, user):
    """
    Догоняет YouTrack новым текстом комментария.

    Локальная правка обязана уехать наружу: иначе следующая синхронизация
    перезапишет её прежним текстом.
    """
    obj = comment.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id or not comment.youtrack_id:
        return []

    prefix = _component_prefix(obj, target_obj, '**[{}]**\n')
    ok, detail = youtrack_services.update_comment_in_youtrack(
        issue_id=issue_id,
        comment_yt_id=comment.youtrack_id,
        text=f"{prefix}{comment.text}",
        user=user
    )
    if ok:
        return []

    logger.warning("Комментарий не обновлён в YouTrack %s: %s", issue_id, detail)
    return [
        f"Комментарий изменён локально, но не в YouTrack: {detail}. "
        "Следующая синхронизация вернёт прежний текст."
    ]


def remove_comments(obj, comments, user):
    """
    Удаляет комментарии и их вложения в YouTrack.

    Возвращает (uuid, которые можно удалить локально; ошибки). Комментарий,
    который не удалось убрать снаружи, остаётся и в локальной базе.
    """
    issue_id, _ = obj.get_effective_youtrack_issue()
    deletable, errors = [], []

    for comment in comments:
        remote_ok = True

        if issue_id:
            for att in comment.attachments.all():
                if att.youtrack_id:
                    ok, detail = youtrack_services.delete_attachment_from_youtrack(
                        issue_id=issue_id,
                        attachment_yt_id=att.youtrack_id,
                        user=user
                    )
                    if not ok:
                        remote_ok = False
                        errors.append(f"Вложение «{att.filename}» не удалено в YouTrack: {detail}")

            if remote_ok and comment.youtrack_id:
                ok, detail = youtrack_services.delete_comment_from_youtrack(
                    issue_id=issue_id,
                    comment_yt_id=comment.youtrack_id,
                    user=user
                )
                if not ok:
                    remote_ok = False
                    errors.append(f"Комментарий не удалён в YouTrack: {detail}")

        if remote_ok:
            deletable.append(comment.uuid)

    return deletable, errors


def push_attachment(attachment, user):
    """
    Загружает вложение в задачу и оставляет о нём заметку.

    Заметка нужна не всегда: для превью это пост с картинкой, для документа
    дочернего компонента — строка с именем файла, а для обычного файла своей
    задачи хватает самого вложения.
    """
    obj = attachment.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id or not attachment.path:
        return []

    errors = []
    try:
        with open(attachment.path.path, 'rb') as f:
            ok, result = youtrack_services.upload_attachment_to_youtrack(
                issue_id=issue_id,
                file_obj=f,
                user=user
            )
    except OSError as exc:
        logger.exception("Не удалось прочитать файл вложения для YouTrack")
        ok, result = False, f"файл недоступен для чтения ({exc})"

    if not (ok and result):
        logger.warning("Вложение не загружено в YouTrack %s: %s", issue_id, result)
        return [
            f"Файл «{attachment.filename}» сохранён локально, "
            f"но не загружен в задачу {issue_id}: {result}"
        ]

    attachment.youtrack_id = result
    attachment.save(update_fields=['youtrack_id'])

    comp_name = obj.name or obj.model.name
    if attachment.is_preview:
        prefix = _component_prefix(obj, target_obj)
        note_ok, detail = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=(f"{prefix}Прикреплено фото (превью): {attachment.filename}"
                  f"\n\n![]({attachment.filename})"),
            user=user
        )
    elif obj != target_obj:
        note_ok, detail = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=f"**[{comp_name}]** Прикреплён новый документ: {attachment.filename}",
            user=user
        )
    else:
        note_ok, detail = True, None

    if not note_ok:
        errors.append(f"Заметка о файле не добавлена в задачу {issue_id}: {detail}")
    return errors


def remove_attachments(obj, attachments, user):
    """
    Удаляет вложения в YouTrack. Возвращает (uuid к локальному удалению; ошибки).
    """
    issue_id, _ = obj.get_effective_youtrack_issue()
    deletable, errors = [], []

    for att in attachments:
        remote_ok = True
        if issue_id and att.youtrack_id:
            ok, detail = youtrack_services.delete_attachment_from_youtrack(
                issue_id=issue_id,
                attachment_yt_id=att.youtrack_id,
                user=user
            )
            if not ok:
                remote_ok = False
                errors.append(f"Файл «{att.filename}» не удалён в YouTrack: {detail}")
        if remote_ok:
            deletable.append(att.uuid)

    return deletable, errors


def push_work_item(history_entry, spent_time, user):
    """
    Списывает время выполненного ТО в задачу объекта или его предка.

    Молчать о неудаче нельзя: ТО зафиксировано локально, и без записи
    в задаче учёт времени незаметно разойдётся.
    """
    obj = history_entry.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id or not spent_time:
        return []

    prefix = _component_prefix(obj, target_obj, '[{}] ')
    ok, result = youtrack_services.add_work_item_to_youtrack(
        issue_id=issue_id,
        duration_str=spent_time,
        text=f"{prefix}{history_entry.action}",
        user=user
    )

    if ok and result:
        history_entry.youtrack_id = result
        history_entry.save(update_fields=['youtrack_id'])
        return []
    if not ok:
        logger.warning("Не удалось списать время в YouTrack %s: %s", issue_id, result)
        return [f"Время не списано в задачу {issue_id}: {result}"]
    return []


def push_description(obj, user):
    """Отправляет изменённое описание объекта в его собственную задачу."""
    if not obj.youtrack_issue_id:
        return []

    ok, detail = youtrack_services.update_issue_description_in_youtrack(
        issue_id=obj.youtrack_issue_id,
        description=obj.description or '',
        user=user
    )
    if ok:
        return []

    logger.warning("Описание не отправлено в YouTrack %s: %s", obj.youtrack_issue_id, detail)
    return [
        f"Описание сохранено локально, но не обновлено в задаче "
        f"{obj.youtrack_issue_id}: {detail}"
    ]

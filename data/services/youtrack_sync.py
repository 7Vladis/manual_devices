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
import time

from data import youtrack_services
from data.models import YouTrackObsoleteRecord

logger = logging.getLogger('data')

# Пометка компонента в тексте, который уезжает в задачу предка. Шаблон один на
# все операции: создание писало пометку через пробел, а правка через перенос
# строки, и один и тот же комментарий менял вид в YouTrack после первой правки.
COMPONENT_PREFIX = '**[{}]** '

# Пометка устаревшей записи идёт синхронно, внутри веб-запроса: локальная
# запись исчезает только после подтверждения, и показать это надо сразу. Но
# каждая запись — отдельное обращение к YouTrack, а gunicorn прибивает запрос
# на 60 секундах: выделенная страница из 20 записей при медленном YouTrack
# оборвала бы соединение на середине, оставив часть помеченной. Поэтому на всю
# операцию отведён бюджет времени, а что в него не уложилось — честно остаётся
# непомеченным, с сообщением пользователю.
MARK_TIME_BUDGET = 25.0


def _component_prefix(obj, target_obj, template=COMPONENT_PREFIX):
    """
    Пометка компонента для записи, которая уходит в задачу родителя.

    Если у объекта нет своей задачи, запись попадает в задачу предка —
    без имени компонента в ней не разобрать, о какой единице речь.
    """
    if obj == target_obj:
        return ''
    return template.format(obj.name or obj.model.name)


def mark_record_obsolete(issue_id, youtrack_id, filename, user):
    """Ставит надгробие. Повторная пометка той же записи ничего не меняет."""
    if not (issue_id and youtrack_id):
        return
    YouTrackObsoleteRecord.objects.get_or_create(
        issue_id=issue_id,
        youtrack_id=youtrack_id,
        defaults={'filename': filename or '', 'user': user},
    )


def record_issue(record, obj):
    """
    Задача, в которой лежит копия записи.

    Берём сохранённую, а не вычисленную по дереву: объект могли перенести
    к другому родителю или отвязать от задачи, и подъём по дереву показал бы
    уже другую задачу, а копия осталась там, куда её отправили. Для старых
    записей, у которых поле не заполнено, остаётся прежний путь.
    """
    if record.youtrack_issue_id:
        return record.youtrack_issue_id
    return obj.get_effective_youtrack_issue()[0]


def _prefix_for_issue(obj, issue_id, template=COMPONENT_PREFIX):
    """
    То же, что _component_prefix, но по номеру задачи: используется там,
    где задача взята из самой записи и target_obj неизвестен.
    """
    if obj.youtrack_issue_id and obj.youtrack_issue_id == issue_id:
        return ''
    return template.format(obj.name or obj.model.name)


def push_comment(comment, attachment, raw_text, user):
    """
    Отправляет в YouTrack только что созданный комментарий и его вложение.

    Возвращает список ошибок; id успешно отправленных записей проставляются
    в переданные объекты.

    Уже отправленное повторно не отправляется: задание может вернуться на
    повтор из-за второго шага (файл уехал, комментарий — нет), и без этой
    проверки в задаче появился бы дубликат. Признак — сохранённый id записи.
    """
    obj = comment.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id:
        return []

    errors = []

    if attachment and attachment.path and not attachment.youtrack_id:
        try:
            with open(attachment.path.path, 'rb') as f:
                ok, result = youtrack_services.upload_attachment_to_youtrack(
                    issue_id=issue_id,
                    file_obj=f,
                    user=user
                )
                if ok and result:
                    attachment.youtrack_id = result
                    attachment.youtrack_issue_id = issue_id
                    attachment.save(update_fields=['youtrack_id', 'youtrack_issue_id'])
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
    if full_text and not comment.youtrack_id:
        ok, result = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=full_text,
            user=user
        )
        if ok and result:
            comment.youtrack_id = result
            comment.youtrack_issue_id = issue_id
            comment.save(update_fields=['youtrack_id', 'youtrack_issue_id'])
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
    issue_id = record_issue(comment, obj)
    if not issue_id or not comment.youtrack_id:
        return []

    prefix = _prefix_for_issue(obj, issue_id)
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


def mark_comments_obsolete(obj, comments, user):
    """
    Помечает комментарии устаревшими в YouTrack и возвращает те, что можно
    убрать локально.

    Сервис в YouTrack ничего не удаляет: вместо `DELETE` в начало текста
    дописывается маркер. Локальная запись исчезает только после
    подтверждённой пометки — иначе следующая синхронизация вернёт её.

    Текст собирается из локального, а не вычитывается из YouTrack: ровно так
    же поступает обычная правка комментария (`push_comment_edit`), поэтому
    поведение не новое. Заодно маркер не может задвоиться — строку каждый раз
    собираем заново.

    Вложениям помеченного комментария ставятся надгробия: файл остаётся в
    задаче, и без отметки синхронизация скачала бы его снова.

    Время на всю операцию ограничено `MARK_TIME_BUDGET`: не уложившиеся записи
    остаются в справочнике непомеченными — лучше сказать об этом, чем оборвать
    запрос по таймауту gunicorn на середине списка.
    """
    deletable, errors = [], []
    deadline = time.monotonic() + MARK_TIME_BUDGET
    skipped = 0

    for comment in comments:
        issue_id = record_issue(comment, obj)

        if not (issue_id and comment.youtrack_id):
            # Запись наружу не уезжала — убираем молча.
            deletable.append(comment.uuid)
            continue

        if time.monotonic() > deadline:
            # Бюджет исчерпан: запись остаётся как есть, повторить можно руками.
            skipped += 1
            continue

        prefix = _prefix_for_issue(obj, issue_id)
        ok, detail = youtrack_services.update_comment_in_youtrack(
            issue_id=issue_id,
            comment_yt_id=comment.youtrack_id,
            text=f"{youtrack_services.OBSOLETE_MARK} {prefix}{comment.text}",
            user=user,
        )
        if not ok:
            logger.warning("Комментарий не помечен устаревшим в YouTrack %s: %s", issue_id, detail)
            errors.append(f"Комментарий не помечен устаревшим в YouTrack: {detail}")
            continue

        for att in comment.attachments.all():
            if att.youtrack_id:
                mark_record_obsolete(record_issue(att, obj), att.youtrack_id, att.filename, user)

        deletable.append(comment.uuid)

    if skipped:
        errors.append(
            f"YouTrack отвечает медленно: {skipped} комментариев не помечено, "
            "операция остановлена по времени"
        )

    return deletable, errors


def push_attachment(attachment, user):
    """
    Загружает вложение в задачу и оставляет о нём заметку.

    Заметка нужна не всегда: для превью это пост с картинкой, для документа
    дочернего компонента — строка с именем файла, а для обычного файла своей
    задачи хватает самого вложения.

    Уже загруженный файл повторно не грузится: если он уехал, а заметка о нём
    не ушла, задание вернётся на повтор — и в задаче появился бы второй такой
    же файл. Повтор в этом случае досылает только заметку.
    """
    obj = attachment.data_object
    issue_id, target_obj = obj.get_effective_youtrack_issue()
    if not issue_id or not attachment.path:
        return []

    errors = []
    if not attachment.youtrack_id:
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
        attachment.youtrack_issue_id = issue_id
        attachment.save(update_fields=['youtrack_id', 'youtrack_issue_id'])

    prefix = _component_prefix(obj, target_obj)
    if attachment.is_preview:
        note_ok, detail = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=(f"{prefix}Прикреплено фото (превью): {attachment.filename}"
                  f"\n\n![]({attachment.filename})"),
            user=user
        )
    elif obj != target_obj:
        note_ok, detail = youtrack_services.send_comment_to_youtrack(
            issue_id=issue_id,
            text=f"{prefix}Прикреплён новый документ: {attachment.filename}",
            user=user
        )
    else:
        note_ok, detail = True, None

    if not note_ok:
        errors.append(f"Заметка о файле не добавлена в задачу {issue_id}: {detail}")
    return errors


def mark_attachments_obsolete(obj, attachments, user):
    """
    Помечает вложения устаревшими и возвращает те, что можно убрать локально.

    У вложения нет текста, поэтому маркер ставить некуда: признак хранится
    надгробием в нашей базе, а в задачу уходит заметка — чтобы человек,
    открывший её, видел, что файл больше не в ходу.

    Заметка отправляется лучшим усилием: от неё не зависит, скачается ли файл
    заново, это решает надгробие. Поэтому локальная запись убирается всегда,
    а неудачу отправки показываем отдельно.

    По той же причине бюджет времени (`MARK_TIME_BUDGET`) обрывает только
    заметки: надгробие ставится локально и в бюджете не нуждается, так что
    выделенные файлы уходят из справочника все, сколько бы их ни было.
    """
    deletable, errors = [], []
    deadline = time.monotonic() + MARK_TIME_BUDGET
    skipped = 0

    for att in attachments:
        issue_id = record_issue(att, obj)

        if issue_id and att.youtrack_id:
            mark_record_obsolete(issue_id, att.youtrack_id, att.filename, user)
            if time.monotonic() > deadline:
                skipped += 1
            else:
                prefix = _prefix_for_issue(obj, issue_id)
                ok, detail = youtrack_services.send_comment_to_youtrack(
                    issue_id=issue_id,
                    text=f"{prefix}Файл «{att.filename}» помечен устаревшим и убран из справочника",
                    user=user,
                )
                if not ok:
                    logger.warning("Заметка о файле не добавлена в YouTrack %s: %s", issue_id, detail)
                    errors.append(f"Заметка о файле «{att.filename}» не добавлена в задачу {issue_id}: {detail}")

        deletable.append(att.uuid)

    if skipped:
        errors.append(
            f"YouTrack отвечает медленно: о {skipped} файлах заметка не отправлена, "
            "отправка остановлена по времени"
        )

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
        history_entry.youtrack_issue_id = issue_id
        history_entry.save(update_fields=['youtrack_id', 'youtrack_issue_id'])
        return []
    if not ok:
        logger.warning("Не удалось списать время в YouTrack %s: %s", issue_id, result)
        return [f"Время не списано в задачу {issue_id}: {result}"]
    return []


def push_move_note(obj, issue_id, destination, user):
    """
    Оставляет в прежней задаче след о том, что компонент уехал.

    Записи о прошлых работах в этой задаче не трогаем: они правдивы —
    работа действительно была сделана, пока деталь стояла здесь. Меняется
    настоящее, а не прошлое, поэтому вместо пометок одна честная строка.
    """
    if not issue_id:
        return []

    name = obj.name or obj.model.name
    ok, detail = youtrack_services.send_comment_to_youtrack(
        issue_id=issue_id,
        text=f"**[{name}]** Компонент перенесён: {destination}",
        user=user,
    )
    if ok:
        return []

    logger.warning("Заметка о переносе не добавлена в YouTrack %s: %s", issue_id, detail)
    return [f"Заметка о переносе не добавлена в задачу {issue_id}: {detail}"]


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

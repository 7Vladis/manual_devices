"""
Очередь заданий к YouTrack: постановка из веба и выполнение воркером.

Веб-запрос не ходит во внешнюю систему — он только записывает задание в БД
и отвечает. Разбирает очередь воркер в процессе планировщика (один контейнер
= один экземпляр), поэтому блокировок и отдельного брокера здесь нет.

Обработчики живут в HANDLERS: `kind` → функция(job) → (успех, сообщение).
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from data import youtrack_services
from data.models import ActionHistory, Attachment, Comment, YouTrackJob
from data.services import youtrack_sync

logger = logging.getLogger('data')

# Пауза перед повтором растёт с каждой неудачей: сеть чаще всего подводит
# ненадолго, а ломиться в недоступный сервер каждые пять секунд бессмысленно.
RETRY_DELAYS = (30, 180, 600)


class PermanentFailure(Exception):
    """
    Отказ, который повтором не лечится: не указан токен, объект не привязан
    к задаче. Такое задание закрывается сразу — иначе пользователь минутами
    смотрел бы на «синхронизируется» вместо внятной причины.
    """


def enqueue(kind, data_object, user, payload=None):
    """
    Ставит задание в очередь и возвращает его.

    Для синхронизации действует ограничение «одно незавершённое задание на
    объект»: карточку открывают часто, а обновляет она одно и то же состояние.
    Наткнувшись на него, возвращаем уже стоящее в очереди задание.
    """
    try:
        with transaction.atomic():
            return YouTrackJob.objects.create(
                kind=kind,
                data_object=data_object,
                user=user,
                payload=payload or {},
            )
    except IntegrityError:
        existing = pending_job(data_object, kind)
        if existing is None:
            # Ограничение сработало, но задания уже нет: воркер успел его
            # завершить между попыткой вставки и этим запросом.
            raise
        return existing


def pending_job(data_object, kind):
    """Незавершённое задание такого вида по объекту, если оно есть."""
    return (
        YouTrackJob.objects
        .filter(data_object=data_object, kind=kind, status__in=YouTrackJob.PENDING_STATUSES)
        .order_by('-created_at')
        .first()
    )


def latest_job(data_object, kind):
    """Последнее задание такого вида — в любом состоянии."""
    return (
        YouTrackJob.objects
        .filter(data_object=data_object, kind=kind)
        .order_by('-created_at')
        .first()
    )


def _claim(job):
    """
    Помечает задание выполняющимся. Возвращает False, если его уже забрали:
    условие по статусу проверяется той же командой UPDATE, что и меняет его.
    """
    claimed = YouTrackJob.objects.filter(
        pk=job.pk, status=YouTrackJob.QUEUED
    ).update(status=YouTrackJob.RUNNING, updated_at=timezone.now())
    return bool(claimed)


def _require_token(job):
    if not youtrack_services.get_auth_token(job.user):
        raise PermanentFailure("В вашем профиле не указан персональный токен YouTrack")


def _outcome(errors):
    """Переводит список ошибок оркестратора в ответ обработчика."""
    if errors:
        return False, " | ".join(dict.fromkeys(errors))
    return True, ''


def _gone(what):
    """Запись удалили раньше, чем воркер до неё дошёл — отправлять нечего."""
    return True, f"{what} удалён до отправки — задание снято"


def _handle_sync(job):
    """Входящая синхронизация: YouTrack → локальная база."""
    if not job.data_object.youtrack_issue_id:
        raise PermanentFailure("У объекта не задан ID задачи YouTrack")
    _require_token(job)
    return youtrack_services.sync_issue_from_youtrack(job.data_object, job.user)


def _handle_comment(job):
    """Отправка созданного комментария и приложенного к нему файла."""
    _require_token(job)
    comment = Comment.objects.filter(pk=job.payload.get('comment')).select_related('data_object').first()
    if comment is None:
        return _gone("Комментарий")

    attachment = None
    if job.payload.get('attachment'):
        attachment = Attachment.objects.filter(pk=job.payload['attachment']).first()

    return _outcome(youtrack_sync.push_comment(
        comment, attachment, job.payload.get('raw_text', ''), job.user
    ))


def _handle_comment_edit(job):
    """
    Догоняет YouTrack текстом комментария.

    Берём текст из базы, а не из задания: если комментарий правили дважды,
    оба задания отправят последнюю версию, и порядок их выполнения
    перестаёт иметь значение.
    """
    _require_token(job)
    comment = Comment.objects.filter(pk=job.payload.get('comment')).select_related('data_object').first()
    if comment is None:
        return _gone("Комментарий")
    return _outcome(youtrack_sync.push_comment_edit(comment, job.user))


def _handle_attachment(job):
    """Загрузка вложения и заметка о нём."""
    _require_token(job)
    attachment = Attachment.objects.filter(pk=job.payload.get('attachment')).select_related('data_object').first()
    if attachment is None:
        return _gone("Файл")
    return _outcome(youtrack_sync.push_attachment(attachment, job.user))


def _handle_work_item(job):
    """Списание времени по выполненному ТО."""
    _require_token(job)
    entry = ActionHistory.objects.filter(pk=job.payload.get('history')).select_related('data_object').first()
    if entry is None:
        return _gone("Запись о ТО")
    return _outcome(youtrack_sync.push_work_item(entry, job.payload.get('spent_time', ''), job.user))


def _handle_description(job):
    """Текущее описание объекта в его задачу."""
    _require_token(job)
    return _outcome(youtrack_sync.push_description(job.data_object, job.user))


HANDLERS = {
    YouTrackJob.KIND_SYNC: _handle_sync,
    YouTrackJob.KIND_COMMENT: _handle_comment,
    YouTrackJob.KIND_COMMENT_EDIT: _handle_comment_edit,
    YouTrackJob.KIND_ATTACHMENT: _handle_attachment,
    YouTrackJob.KIND_WORK_ITEM: _handle_work_item,
    YouTrackJob.KIND_DESCRIPTION: _handle_description,
}


def run_job(job):
    """
    Выполняет одно задание. Неудача — не исключение: клиент YouTrack сам
    возвращает (False, причина), и задание уходит на повтор, пока не кончатся
    попытки.
    """
    handler = HANDLERS.get(job.kind)
    if handler is None:
        job.status = YouTrackJob.FAILED
        job.last_error = f"Неизвестный вид задания: {job.kind}"
        job.save(update_fields=['status', 'last_error', 'updated_at'])
        logger.error("Задание %s неизвестного вида %s", job.pk, job.kind)
        return False

    job.attempts += 1
    permanent = False
    try:
        ok, message = handler(job)
    except PermanentFailure as exc:
        ok, message, permanent = False, str(exc), True
    except Exception as exc:  # noqa: BLE001 — воркер не должен падать из-за одного задания
        logger.exception("Задание %s (%s) завершилось исключением", job.pk, job.kind)
        ok, message = False, f"Внутренняя ошибка: {exc}"

    if ok:
        job.status = YouTrackJob.DONE
        job.result = message or ''
        job.last_error = ''
    elif job.can_retry and not permanent:
        delay = RETRY_DELAYS[min(job.attempts - 1, len(RETRY_DELAYS) - 1)]
        job.status = YouTrackJob.QUEUED
        job.run_after = timezone.now() + timedelta(seconds=delay)
        job.last_error = message or ''
        logger.warning("Задание %s (%s) не удалось, повтор через %s с: %s",
                       job.pk, job.kind, delay, message)
    else:
        job.status = YouTrackJob.FAILED
        job.last_error = message or ''
        logger.error("Задание %s (%s) исчерпало попытки: %s", job.pk, job.kind, message)

    job.save(update_fields=['status', 'attempts', 'run_after', 'last_error', 'result', 'updated_at'])
    return ok


def process_jobs(limit=20):
    """
    Разбирает порцию готовых заданий. Вызывается воркером по расписанию;
    возвращает, сколько заданий выполнено, — это удобно и в логе, и в тестах.
    """
    ready = YouTrackJob.objects.filter(
        status=YouTrackJob.QUEUED,
        run_after__lte=timezone.now(),
    ).select_related('data_object', 'user').order_by('run_after', 'created_at')[:limit]

    processed = 0
    for job in ready:
        if not _claim(job):
            continue
        run_job(job)
        processed += 1

    if processed:
        logger.info("Обработано заданий YouTrack: %s", processed)
    return processed

def object_state(data_object):
    """
    Состояние обмена с YouTrack по объекту — то, что показывает пилюля.

    Неудача важнее ожидания, ожидание важнее успеха: если хоть что-то не
    уехало, человек должен увидеть именно это, а не зелёную галочку от
    предыдущей удачной синхронизации.
    """
    jobs = YouTrackJob.objects.filter(data_object=data_object)

    failed = jobs.filter(status=YouTrackJob.FAILED).order_by('-updated_at').first()
    if failed is not None:
        return 'failed', f"{failed.get_kind_display()}: {failed.last_error}"

    if jobs.filter(status__in=YouTrackJob.PENDING_STATUSES).exists():
        return 'pending', ''

    done = jobs.filter(status=YouTrackJob.DONE).order_by('-updated_at').first()
    if done is not None:
        return 'done', done.result

    return 'idle', ''


def retry_failed(data_object):
    """
    Возвращает неудавшиеся задания объекта в очередь.

    Счётчик попыток сбрасывается: это осознанное решение человека повторить,
    а не автоматический ретрай. Возвращает, сколько заданий поставлено заново.
    """
    return YouTrackJob.objects.filter(
        data_object=data_object, status=YouTrackJob.FAILED
    ).update(
        status=YouTrackJob.QUEUED,
        attempts=0,
        run_after=timezone.now(),
        last_error='',
        updated_at=timezone.now(),
    )

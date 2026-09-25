"""Общие помощники представлений: ответы об ошибках, уведомления,
мелкие выборки, которыми пользуются сразу несколько модулей."""

from datetime import timedelta

from django.http import HttpResponse
from django.db.models import Count
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.template.loader import render_to_string
from django.utils import timezone


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


# Длинные ленты отдаются страницами: история объекта за годы эксплуатации
# и список работ по всему парку иначе уезжают в сотни килобайт разметки.
PAGE_SIZE = 20


def page_of(queryset, page_number, per_page=PAGE_SIZE):
    """
    Страница списка. Номер вне диапазона не ошибка, а край: ссылку могли
    сохранить, а записи с тех пор удалить или отметить выполненными.
    """
    paginator = Paginator(queryset, per_page)
    try:
        return paginator.page(page_number)
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def sync_pill_oob(obj, request=None):
    """
    OOB-пилюля состояния обмена: после действия она снова начинает опрос.

    Пустая строка, если у объекта нет своей задачи YouTrack: пилюли в шапке
    карточки тогда нет, и своп ушёл бы в никуда.
    """
    if not obj.youtrack_issue_id:
        return ''
    return render_to_string(
        'data/object/sync_status.html',
        {'obj': obj, 'sync_state': 'pending', 'poll': 1, 'oob': True},
        request=request,
    )


def tree_queryset(queryset):
    """
    Выборка для строк дерева и проводника.

    children_count вместо prefetch_related('children'): строке нужно знать
    только, есть ли вложенные объекты, а не какие именно — раньше ради
    стрелки раскрытия выгружались все потомки целиком.

    select_related('model') обязателен: подпись строки — это
    `node.name|default:node.model.name`, а фильтр default вычисляет аргумент
    всегда, даже когда имя задано. Без него каждый узел спрашивал модель
    отдельным запросом.
    """
    return (
        queryset
        .annotate(children_count=Count('children'))
        .select_related('model')
        .order_by('name')
    )


def get_comments_for(obj, user, page_number=1):
    """
    Страница ленты комментариев с проставленным признаком can_edit —
    шаблон не может вызвать метод с аргументом, поэтому считаем здесь.
    """
    page = page_of(
        obj.comments.select_related('user').prefetch_related('attachments').order_by('-created_at'),
        page_number,
    )
    for comment in page.object_list:
        comment.can_edit = comment.can_be_edited_by(user)
    return page


def get_files_for(obj, page_number=1):
    """
    Страница ленты вложений. select_related('comment') не роскошь: шаблон
    помечает файлы, пришедшие из комментария, и иначе спрашивал бы про
    каждый файл отдельно.
    """
    return page_of(
        obj.attachments.select_related('user', 'comment').order_by('-created_at'),
        page_number,
    )


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

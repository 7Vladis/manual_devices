"""
Раздача вложений с поддержкой частичных запросов.

Раньше медиафайлы отдавало стандартное представление Django, и только при
`DEBUG=True`. Оно не понимает заголовок `Range`: на запрос куска отвечает
файлом целиком со статусом 200 и не присылает `Accept-Ranges`. Картинке и
документу этого хватает, видео — нет: браузер не может ни перемотать, ни
дочитать конец файла, не скачав его заново целиком. Отсюда рывки на
проигрывании, особенно ближе к концу ролика.

Здесь тот же файл отдаётся правильно: с `Accept-Ranges`, ответом 206 на
запрос куска и 416 на невыполнимый запрос. Заодно раздача перестаёт зависеть
от `DEBUG` — с выключенной отладкой вложения не отдавались вовсе.

Это по-прежнему раздача силами приложения: на 2–5 пользователей её достаточно,
отдельный веб-сервер ради этого в развёртывание не добавляется.
"""

import mimetypes
import os
import re

from django.conf import settings
from django.http import (FileResponse, Http404, HttpResponse,
                         HttpResponseNotModified, StreamingHttpResponse)
from django.core.exceptions import SuspiciousFileOperation
from django.utils._os import safe_join
from django.utils.http import http_date
from django.views.static import was_modified_since

# Кусок, которым читается файл. У FileResponse по умолчанию 4 КиБ — для
# двадцатимегабайтного ролика это тысячи проходов через WSGI на ровном месте.
BLOCK_SIZE = 64 * 1024

# `bytes=0-499`, `bytes=500-` и `bytes=-500` (последние 500 байт).
_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


def _parse_range(header, size):
    """
    Границы запрошенного куска или None, если куска не просили.

    Диапазон, который сервер не умеет отдать (несколько кусков сразу,
    мусор в заголовке), трактуем как его отсутствие — так разрешает спецификация.
    `False` означает, что кусок запрошен, но выходит за пределы файла.
    """
    match = _RANGE_RE.match((header or '').strip())
    if not match:
        return None

    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None

    if not start_raw:                       # bytes=-500 — хвост файла
        start, end = max(size - int(end_raw), 0), size - 1
    else:
        start = int(start_raw)
        end = min(int(end_raw), size - 1) if end_raw else size - 1

    if start >= size or start > end:
        return False
    return start, end


def _chunks(handle, length):
    """Отдаёт ровно `length` байт и закрывает файл."""
    try:
        while length > 0:
            data = handle.read(min(BLOCK_SIZE, length))
            if not data:
                break
            length -= len(data)
            yield data
    finally:
        handle.close()


def serve_media(request, path):
    """Отдаёт файл из MEDIA_ROOT, понимая `Range` и условный GET."""
    try:
        full_path = safe_join(settings.MEDIA_ROOT, path)
    except SuspiciousFileOperation:
        raise Http404("Недопустимый путь к файлу")

    if not os.path.isfile(full_path):
        raise Http404("Файл не найден")

    stat = os.stat(full_path)
    content_type, encoding = mimetypes.guess_type(full_path)
    content_type = content_type or 'application/octet-stream'

    if not was_modified_since(request.META.get('HTTP_IF_MODIFIED_SINCE'), stat.st_mtime):
        return HttpResponseNotModified()

    requested = _parse_range(request.META.get('HTTP_RANGE'), stat.st_size)

    if requested is False:
        # Проигрыватель иногда просит кусок за концом файла; ответить надо
        # отказом с указанием настоящего размера, а не файлом целиком.
        response = HttpResponse(status=416)
        response['Content-Range'] = f'bytes */{stat.st_size}'
        response['Accept-Ranges'] = 'bytes'
        return response

    if requested is None:
        handle = open(full_path, 'rb')
        response = FileResponse(handle, content_type=content_type)
        response['Content-Length'] = str(stat.st_size)
    else:
        start, end = requested
        handle = open(full_path, 'rb')
        handle.seek(start)
        length = end - start + 1
        response = StreamingHttpResponse(_chunks(handle, length), status=206,
                                         content_type=content_type)
        response['Content-Length'] = str(length)
        response['Content-Range'] = f'bytes {start}-{end}/{stat.st_size}'

    response['Accept-Ranges'] = 'bytes'
    response['Last-Modified'] = http_date(stat.st_mtime)
    if encoding:
        response['Content-Encoding'] = encoding
    return response

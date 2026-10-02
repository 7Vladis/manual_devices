# data/templatetags/markdown_extras.py

import os
import re
from urllib.parse import unquote

import markdown as md
import nh3
from django import template
from django.utils.safestring import mark_safe
from django.utils.text import get_valid_filename

register = template.Library()

# Разрешённый HTML на выходе Markdown. Всё, чего здесь нет (script, iframe, svg,
# on*-атрибуты, style, javascript:-ссылки), вырезается nh3 до попадания в шаблон.
ALLOWED_TAGS = {
    'p', 'br', 'hr',
    'strong', 'b', 'em', 'i', 'u', 's', 'del', 'code', 'pre', 'kbd', 'sub', 'sup',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li',
    'blockquote',
    'a', 'img',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'dl', 'dt', 'dd',
}

ALLOWED_ATTRIBUTES = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title'},
    'th': {'align'},
    'td': {'align'},
    'code': {'class'},
    'pre': {'class'},
}

ALLOWED_URL_SCHEMES = {'http', 'https', 'mailto'}

# Вставка изображения в Markdown: ![подпись](адрес). Адресом бывает и ссылка,
# и просто имя файла — так пишет YouTrack, когда картинку вставили в текст.
_IMAGE_RE = re.compile(r'!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]*)\)')

# Адреса, которые подставлять не нужно: они уже указывают куда надо.
_READY_PREFIXES = ('http://', 'https://', '/media/')


def _attribute_filter(tag, attr, value):
    """Изображения — только из наших медиафайлов или по абсолютному http(s)-адресу."""
    if tag == 'img' and attr == 'src':
        value = value.strip()
        if value.startswith('/media/') or value.startswith(('http://', 'https://')):
            return value
        return None
    return value


def sanitize_html(html):
    """Прогоняет произвольный HTML через разрешающий список тегов и атрибутов."""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel='noopener noreferrer',
        strip_comments=True,
    )


def _match_key(name):
    """
    Ключ для сверки имени из текста с именем файла на диске.

    В тексте стоит имя, которое дал YouTrack, а на диске лежит то, что из него
    сделало хранилище Django: пробелы стали подчёркиваниями, посторонние
    символы выпали. Без приведения к общему виду совпадения почти не бывает.
    """
    name = unquote((name or '').strip()).replace('\\', '/').rsplit('/', 1)[-1]
    return get_valid_filename(name).lower() if name else ''


# Суффикс, которым хранилище разводит файлы с одинаковыми именами в одной
# папке: `doc.pdf` рядом с таким же превращается в `doc_3xUTXOj.pdf`.
_STORAGE_SUFFIX_RE = re.compile(r'^(?P<stem>.+)_[A-Za-z0-9]{7}$')


def _stored_stem(key):
    """Имя файла на диске без расширения и без суффикса хранилища."""
    stem, ext = os.path.splitext(key)
    match = _STORAGE_SUFFIX_RE.match(stem)
    return (match.group('stem') if match else stem), ext


def _find_attachment(src, attachments):
    """
    Вложение, на которое ссылается вставка в тексте, или None.

    Точного совпадения имён мало: файл на диске мог получить от хранилища
    суффикс `_XXXXXXX` (рядом уже лежал такой же) и мог укоротиться под предел
    длины пути. Поэтому суффикс снимаем, а укороченное имя принимаем как начало
    исходного — но только если от него осталось хотя бы восемь символов: иначе
    «photo» прилипло бы к «photo2».
    """
    key = _match_key(src)
    if not key:
        return None

    keyed = [(_match_key(att.filename), att) for att in attachments]
    for att_key, att in keyed:
        if att_key == key:
            return att

    ref_stem, ref_ext = os.path.splitext(key)
    for att_key, att in keyed:
        stem, ext = _stored_stem(att_key)
        if ext != ref_ext or not stem:
            continue
        if stem == ref_stem or (len(stem) >= 8 and ref_stem.startswith(stem)):
            return att
    return None


def _substitute_images(text, attachments, used):
    """
    Меняет имена файлов во вставках на ссылки наших копий.

    Вставку, которой файла не нашлось, убираем: битая картинка в ленте хуже её
    отсутствия. Так вёл себя фильтр и раньше — только тогда вырезались все
    относительные вставки без разбора, и порядок рассказа терялся.
    """
    def replace(match):
        src = (match.group('src') or '').strip()
        if src.startswith(_READY_PREFIXES):
            return match.group(0)

        att = _find_attachment(src, attachments)
        if att is None or not att.path:
            return ''

        used.append(att.uuid)
        return f"![{match.group('alt')}]({att.path.url})"

    return _IMAGE_RE.sub(replace, text)


def render_markdown(text, attachments=()):
    """
    Преобразует Markdown в безопасный HTML.

    Возвращает пару: готовый HTML и uuid вложений, показанных прямо в тексте —
    повторять их плиткой под комментарием не надо.
    """
    if not text:
        return mark_safe(''), []

    used = []
    prepared = _substitute_images(text, attachments, used).strip()
    html = md.markdown(prepared, extensions=['extra', 'nl2br', 'sane_lists'])
    return mark_safe(sanitize_html(html)), used


@register.filter(name='markdown')
def markdown_format(text):
    """
    Markdown текста, у которого своих вложений нет: описание объекта, записи
    истории. Относительные вставки изображений здесь убираются — подставлять
    вместо них нечего.
    """
    html, _ = render_markdown(text)
    return html


@register.simple_tag(name='comment_body')
def comment_body(comment):
    """
    Тело комментария: текст с картинками на своих местах и отдельно вложения,
    на которые в тексте не сослались.

    YouTrack вставляет картинку прямо в текст (`![](имя.png)`); раньше такая
    вставка вырезалась, а файл уезжал плиткой под комментарий, и рассказ
    рассыпался. Теперь она заменяется ссылкой на нашу копию, а под текстом
    остаётся только то, чего в нём нет.
    """
    attachments = list(comment.attachments.all())
    html, used = render_markdown(comment.text, attachments)
    shown = set(used)
    return {'html': html, 'extra': [att for att in attachments if att.uuid not in shown]}

# data/templatetags/markdown_extras.py

import re

import markdown as md
import nh3
from django import template
from django.utils.safestring import mark_safe

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

# Только относительные ссылки на наши медиафайлы и абсолютные http(s)/mailto.
_RELATIVE_IMAGE_RE = re.compile(r'!\[.*?\]\((?!https?://|/media/).*?\)')


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


@register.filter(name='markdown')
def markdown_format(text):
    """
    Преобразует Markdown в безопасный HTML.

    1. Удаляет относительные вставки изображений ![](имя_файла.jpg): вложения
       показываются интерактивными миниатюрами под комментарием.
    2. Рендерит Markdown.
    3. Санитизирует результат: пользовательский и пришедший из YouTrack текст
       не должен исполнять скрипты в браузере других пользователей.
    """
    if not text:
        return ""

    cleaned_text = _RELATIVE_IMAGE_RE.sub('', text).strip()
    html = md.markdown(cleaned_text, extensions=['extra', 'nl2br', 'sane_lists'])
    return mark_safe(sanitize_html(html))

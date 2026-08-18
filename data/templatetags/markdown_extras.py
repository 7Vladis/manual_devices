# data/templatetags/markdown_extras.py

from django import template
from django.utils.safestring import mark_safe
import markdown as md

register = template.Library()

@register.filter(name='markdown')
def markdown_format(text):
    """Преобразует Markdown в безопасный HTML"""
    if not text:
        return ""
    # Поддерживаем таблицы, списки, переносы строк и подсветку кода
    html = md.markdown(text, extensions=['extra', 'nl2br', 'sane_lists'])
    return mark_safe(html)
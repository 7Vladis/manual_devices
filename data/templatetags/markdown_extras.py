# data/templatetags/markdown_extras.py

import re
from django import template
from django.utils.safestring import mark_safe
import markdown as md

register = template.Library()

@register.filter(name='markdown')
def markdown_format(text):
    """
    Преобразует Markdown в безопасный HTML.
    Удаляет битые относительные теги изображений ![](имя_файла.jpg) из тела текста,
    так как они отображаются в виде интерактивных миниатюр под комментарием.
    """
    if not text:
        return ""
    
    # Удаляем Markdown-вставки вида ![](filename.jpg), если они не являются полными URL-ссылками
    cleaned_text = re.sub(r'!\[.*?\]\((?!http|/media/).*?\)', '', text).strip()
    
    html = md.markdown(cleaned_text, extensions=['extra', 'nl2br', 'sane_lists'])
    return mark_safe(html)
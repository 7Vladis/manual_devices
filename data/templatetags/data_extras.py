"""Небольшие фильтры для шаблонов приложения data."""

from django import template

from data.forms import MONTH_NAMES

register = template.Library()


@register.filter(name='month_name')
def month_name(value):
    """Номер месяца → название в родительном падеже («5» → «Мая»)."""
    try:
        return MONTH_NAMES.get(int(value), '')
    except (TypeError, ValueError):
        return ''

"""
Формы приложения data.

Правила планирования ТО раньше собирались напрямую из request.POST: голые
int()/strptime() роняли запрос на любом нечисловом значении, неизвестная
стратегия оставляла rule_json несвязанным, а день ограничивался диапазоном
1-31 без учёта месяца.
"""

import calendar
from datetime import date

from django import forms

from .models import DateUpdateRule

STRATEGY_CHOICES = [
    ('relative', 'Интервал от даты'),
    ('fixed', 'Фиксированные даты в году'),
]

ANCHOR_CHOICES = [
    ('actual', 'от фактического выполнения'),
    ('scheduled', 'от планового срока'),
]

MONTH_NAMES = {
    1: 'Января', 2: 'Февраля', 3: 'Марта', 4: 'Апреля',
    5: 'Мая', 6: 'Июня', 7: 'Июля', 8: 'Августа',
    9: 'Сентября', 10: 'Октября', 11: 'Ноября', 12: 'Декабря',
}

# Ограничение сверху: интервал длиннее ста лет — почти наверняка опечатка,
# а relativedelta на огромных значениях даёт бессмысленные даты.
MAX_YEARS = 100
MAX_MONTHS = 1200
MAX_DAYS = 36500


def days_in_month(month, year=None):
    """Сколько дней в месяце. Для февраля берём високосный максимум — 29."""
    if year is None:
        return 29 if month == 2 else calendar.monthrange(2001, month)[1]
    return calendar.monthrange(year, month)[1]


def clean_fixed_dates(months, days):
    """
    Превращает параллельные списки месяцев и дней в список {'month', 'day'}.
    Отбрасывает нечисловые значения и несуществующие календарные даты
    (например, 31 февраля), убирает дубликаты и сортирует.
    """
    result = []
    for raw_month, raw_day in zip(months, days):
        try:
            month = int(raw_month)
            day = int(raw_day)
        except (TypeError, ValueError):
            continue

        if not 1 <= month <= 12:
            continue
        if not 1 <= day <= days_in_month(month):
            continue

        item = {'month': month, 'day': day}
        if item not in result:
            result.append(item)

    result.sort(key=lambda d: (d['month'], d['day']))
    return result


class DateUpdateRuleForm(forms.ModelForm):
    """Создание и редактирование правила расчёта даты следующего ТО."""

    name = forms.CharField(
        label="Название правила",
        max_length=255,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Например: Раз в полгода'}),
    )
    new_rule_strategy = forms.ChoiceField(
        label="Стратегия", choices=STRATEGY_CHOICES, initial='relative', required=False,
    )
    new_rule_anchor = forms.ChoiceField(
        label="Отсчёт", choices=ANCHOR_CHOICES, initial='actual', required=False,
    )
    new_rule_years = forms.IntegerField(label="Лет", min_value=0, max_value=MAX_YEARS, required=False, initial=0)
    new_rule_months = forms.IntegerField(label="Месяцев", min_value=0, max_value=MAX_MONTHS, required=False, initial=6)
    new_rule_days = forms.IntegerField(label="Дней", min_value=0, max_value=MAX_DAYS, required=False, initial=0)

    class Meta:
        model = DateUpdateRule
        fields = ['name']

    def __init__(self, *args, **kwargs):
        self.fixed_months = kwargs.pop('fixed_months', None) or []
        self.fixed_days = kwargs.pop('fixed_days', None) or []
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        qs = DateUpdateRule.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Правило с таким названием уже существует.")
        return name

    def clean(self):
        cleaned = super().clean()
        strategy = cleaned.get('new_rule_strategy') or 'relative'

        if strategy == 'fixed':
            dates = clean_fixed_dates(self.fixed_months, self.fixed_days)
            if not dates:
                raise forms.ValidationError(
                    "Добавьте хотя бы одну корректную дату обслуживания в году."
                )
            cleaned['rule_json'] = {'strategy': 'fixed', 'anchor': 'yearly', 'value': dates}
            return cleaned

        years = cleaned.get('new_rule_years') or 0
        months = cleaned.get('new_rule_months') or 0
        days = cleaned.get('new_rule_days') or 0

        if years == 0 and months == 0 and days == 0:
            raise forms.ValidationError(
                "Интервал не может быть нулевым: укажите годы, месяцы или дни."
            )

        cleaned['rule_json'] = {
            'strategy': 'relative',
            'anchor': cleaned.get('new_rule_anchor') or 'actual',
            'value': {'years': years, 'months': months, 'days': days},
        }
        return cleaned

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.rule = self.cleaned_data['rule_json']
        if commit:
            rule.save()
        return rule


def parse_maintenance_date(raw, field_label="Дата"):
    """
    Разбирает дату из формы (ГГГГ-ММ-ДД). Возвращает date или None,
    бросает ValidationError на некорректном значении — вместо необработанного
    strptime, роняющего запрос.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise forms.ValidationError(f"{field_label}: ожидается формат ГГГГ-ММ-ДД.")

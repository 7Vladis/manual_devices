from django import forms

from .models import MattermostSetting


class MattermostSettingForm(forms.ModelForm):
    """Добавление webhook: проверяем формат URL и отсутствие дубликата."""

    class Meta:
        model = MattermostSetting
        fields = ['webhook_url']

    def clean_webhook_url(self):
        url = self.cleaned_data['webhook_url'].strip()
        if MattermostSetting.objects.filter(webhook_url=url).exists():
            raise forms.ValidationError("Этот webhook уже добавлен.")
        return url

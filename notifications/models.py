from django.db import models

class MattermostSetting(models.Model):
    webhook_url = models.URLField(verbose_name="Webhook URL")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mattermost_setting'
        verbose_name = "Настройка Mattermost"
        verbose_name_plural = "Настройки Mattermost"

    def __str__(self):
        return f"Конфигурация от {self.updated_at.strftime('%d.%m.%Y')}"

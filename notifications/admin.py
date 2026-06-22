from django.contrib import admin
from .models import MattermostSetting

@admin.register(MattermostSetting)
class MattermostSettingAdmin(admin.ModelAdmin):
    list_display = ('id', 'updated_at', 'is_active', 'webhook_url')

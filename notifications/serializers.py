from rest_framework import serializers
from .models import MattermostSetting

class MattermostSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = MattermostSetting
        fields = ['id', 'webhook_url', 'is_active', 'updated_at']
        read_only_fields = ['updated_at']
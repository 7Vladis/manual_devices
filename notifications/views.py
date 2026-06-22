from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import MattermostSetting
from .serializers import MattermostSettingSerializer
from .services import check_webhook_availability

class MattermostSettingViewSet(viewsets.ModelViewSet):
    queryset = MattermostSetting.objects.all()
    serializer_class = MattermostSettingSerializer

    @action(detail=True, methods=['get'])
    def check_connection(self, request, pk=None):
        success, message = check_webhook_availability()
        if success:
            return Response({"status": "connected", "message": message})
        return Response({"status": "error", "message": message}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get'])
    def system_status(self, request):
        config = MattermostSetting.objects.filter(is_active=True).last()
        is_configured = config is not None and len(config.webhook_url) > 10
        
        return Response({
            "is_configured": is_configured,
            "is_active": config.is_active if config else False,
            "webhook_url": config.webhook_url if config else None,
            "last_updated": config.updated_at if config else None
        })


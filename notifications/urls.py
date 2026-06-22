from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MattermostSettingViewSet

router = DefaultRouter()
router.register(r'settings', MattermostSettingViewSet, basename='notifications-settings')

urlpatterns = [
    path('', include(router.urls)),
]
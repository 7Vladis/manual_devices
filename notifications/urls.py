from django.urls import path
from . import views

urlpatterns = [
    path('settings/', views.notification_settings, name='notification_settings'),
    path('settings/add/', views.add_webhook, name='add_webhook'),
    path('settings/test/<uuid:pk>/', views.test_webhook, name='test_webhook'),
    path('settings/activate/<uuid:pk>/', views.activate_webhook, name='activate_webhook'),
    path('settings/delete/', views.delete_webhooks, name='delete_webhooks'),
]
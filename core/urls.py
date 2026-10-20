from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.contrib.auth import views as auth_views
from core.media import serve_media
from users.views import MyLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', MyLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('settings/users/', include('users.urls', namespace='users')), 
    path('notifications/', include('notifications.urls')), 
    path('', include('data.urls')),
]

# Вложения раздаёт своё представление, а не `static()` из Django:
# стандартное не понимает `Range` (видео нельзя перемотать) и подключалось
# только при DEBUG=True — с выключенной отладкой вложения пропадали совсем.
urlpatterns += [
    re_path(r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'), serve_media, name='media'),
]
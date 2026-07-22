from django.urls import path
from .views import create_user_view, update_user_role_view, toggle_user_status_view, delete_user_view

app_name = 'users'

urlpatterns = [
    path('create/', create_user_view, name='create_user'),
    path('<uuid:pk>/update-role/', update_user_role_view, name='update_role'),
    path('<uuid:pk>/toggle-status/', toggle_user_status_view, name='toggle_status'),
    path('<uuid:pk>/delete/', delete_user_view, name='delete_user'),
]
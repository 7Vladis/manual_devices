from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('maintenance-list/', views.maintenance_list, name='maintenance_list'), # Новый путь
    path('dict/', views.dict_view, name='dict'),
    path('search/', views.search_view, name='search'),
]
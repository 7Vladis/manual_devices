from django.urls import path
from . import views
import users.views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('maintenance-list/', views.maintenance_list, name='maintenance_list'),
    path('search/', views.search_view, name='search'),
    path('dict/', views.dict_view, name='dict'),

    # Натсройки
    path('settings/', views.settings_page, name='settings_page'),
    path('settings/object-types/create/', views.create_object_type_view, name='create_object_type'),
    path('settings/object-types/<uuid:pk>/delete/', views.delete_object_type_view, name='delete_object_type'),
    path('settings/dependency-types/create/', views.create_dependency_type_view, name='create_dependency_type'),
    path('settings/dependency-types/<uuid:pk>/delete/', views.delete_dependency_type_view, name='delete_dependency_type'),
    path('settings/rules/create/', views.create_rule_settings_view, name='create_rule_settings'),
    path('settings/rules/<uuid:pk>/delete/', views.delete_rule_view, name='delete_rule'),
    path('settings/rules/<uuid:pk>/edit/', views.edit_rule_settings_view, name='edit_rule_settings'),
    path('settings/rules/<uuid:pk>/edit/', views.edit_rule_settings_view, name='edit_rule_settings'),
    path('settings/users/<uuid:pk>/toggle/', users.views.toggle_user_status_view, name='toggle_user_status'),
    
    # Дерево объектов
    path('dict/objects/', views.object_tree_view, name='object_tree'),
    path('dict/objects/<uuid:parent_uuid>/children/', views.object_children_view, name='object_children'),
    path('dict/objects/<uuid:pk>/service/', views.service_object_view, name='service_object'),
    path('dict/objects/<uuid:pk>/delete/', views.delete_object_view, name='delete_object'),
    path('dict/toggle-explorer-mode/', views.toggle_explorer_mode_view, name='toggle_explorer_mode'),
    path('dict/explorer/navigate/<uuid:pk>/', views.explorer_navigate_view, name='explorer_navigate'),
    path('dict/explorer/up/', views.explorer_up_view, name='explorer_up'),

    # Дерево моделей
    path('dict/models/', views.model_tree_view, name='model_tree'),
    path('dict/models/<uuid:pk>/delete/', views.delete_model_view, name='delete_model'),
    
    # Создание моделей и объектов
    path('dict/objects/create/', views.create_object_view, name='create_object'),
    path('dict/models/create/', views.create_model_view, name='create_model'),

    # Детализация объектов
    path('dict/objects/<uuid:pk>/', views.object_detail_view, name='object_detail'),
    path('dict/objects/<uuid:pk>/edit-name/', views.edit_name_view, name='edit_name'),
    path('dict/objects/<uuid:pk>/edit-model/', views.edit_object_model_view, name='edit_object_model'),
    path('dict/objects/<uuid:pk>/tab/<str:tab_name>/', views.object_tab_view, name='object_tab'),
    path('dict/objects/<uuid:pk>/edit-rule/', views.edit_rule_view, name='edit_rule'),
    path('dict/objects/<uuid:pk>/edit-inventory/', views.edit_inventory_view, name='edit_inventory'),
    path('dict/objects/<uuid:pk>/edit-parent/', views.edit_parent_view, name='edit_parent'),
    path('dict/objects/<uuid:pk>/edit-description/', views.edit_description_view, name='edit_description'),
    path('dict/objects/<uuid:pk>/comments/add/', views.add_comment_view, name='add_comment'),
    path('dict/comments/<uuid:pk>/edit/', views.edit_comment_view, name='edit_comment'),
    path('dict/comments/delete-bulk/', views.delete_comments_bulk, name='delete_comments_bulk'),
    path('dict/objects/<uuid:pk>/attachments/add/', views.add_attachment_view, name='add_attachment'),
    path('dict/attachments/delete-bulk/', views.delete_attachments_bulk, name='delete_attachments_bulk'),
    path('dict/objects/<uuid:pk>/unlink-rule/', views.unlink_rule_view, name='unlink_rule'),
    path('dict/objects/<uuid:pk>/edit-youtrack/', views.edit_youtrack_view, name='edit_youtrack'),
    path('dict/attachments/<uuid:pk>/set-preview/', views.set_preview_attachment_view, name='set_preview_attachment'),
    path('dict/objects/<uuid:pk>/sync-youtrack/', views.sync_youtrack_view, name='sync_youtrack'),
    
    # Детализация моделей
    path('dict/models/<uuid:pk>/', views.model_detail_view, name='model_detail'),
    path('dict/models/<uuid:pk>/edit-name/', views.edit_model_name_view, name='edit_model_name'),
    path('dict/models/<uuid:pk>/tab/<str:tab_name>/', views.model_tab_view, name='model_tab'),
    path('dict/models/<uuid:pk>/specs/add/', views.model_spec_add_view, name='model_spec_add'),
    path('dict/models/<uuid:pk>/specs/edit/', views.model_spec_edit_view, name='model_spec_edit'),
    path('dict/models/<uuid:pk>/specs/delete/', views.model_spec_delete_view, name='model_spec_delete'),

    # Маршруты для умных подсказок и интерактивных форм
    path('dict/suggest/', views.suggest_view, name='suggest'),
    path('dict/suggest/select/', views.select_suggestion_view, name='select_suggestion'),
    path('dict/suggest/reset/', views.reset_suggestion_view, name='reset_suggestion'),
    path('dict/objects/check-name/', views.check_object_name_view, name='check_object_name'),
    path('dict/models/check-name/', views.check_model_name_view, name='check_model_name'),
    path('dict/models/specs-builder/', views.specs_builder_view, name='specs_builder'),
    path('dict/rules/constructor/', views.rule_constructor_view, name='rule_constructor'), 
    path('dict/rules/dates-builder/', views.rules_dates_builder_view, name='rules_dates_builder'),
    path('dict/rules/toggle-mode/', views.toggle_scheduling_mode_view, name='toggle_scheduling_mode'),
    path('export/xlsx/', views.export_xlsx_view, name='export_xlsx'),
    path('export/modal/', views.export_modal_view, name='export_modal'),
]
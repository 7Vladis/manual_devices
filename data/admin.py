from django.contrib import admin
from .models import ObjectType, DependencyType, DateUpdateRule, ObjectModel, DataObject, Relation, ActionHistory, Comment, Attachment

class CommentInline(admin.TabularInline):
    model = Comment
    extra = 0
    fields = ('user', 'text', 'created_at')
    readonly_fields = ('created_at',)

class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ('user', 'path', 'is_preview', 'created_at')
    readonly_fields = ('created_at',)

class SubsRelationsInline(admin.TabularInline):
    model = Relation
    fk_name = "main"
    extra = 0
    verbose_name = "Зависимый объект (Подчиненный)"
    verbose_name_plural = "Зависимые объекты (Составные части / Подчиненные)"

class MainRelationsInline(admin.TabularInline):
    model = Relation
    fk_name = "subject"
    extra = 0
    verbose_name = "Главный объект (Родитель)"
    verbose_name_plural = "Главные объекты (В состав чего входит)"

@admin.register(ObjectType)
class ObjectTypeAdmin(admin.ModelAdmin):
    list_display = ('type',)
    search_fields = ('type',)

@admin.register(DependencyType)
class DependencyTypeAdmin(admin.ModelAdmin):
    list_display = ('type',)
    search_fields = ('type',)

@admin.register(DateUpdateRule)
class DateUpdateRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'rule')
    search_fields = ('name',)

@admin.register(ObjectModel)
class ObjectModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'object_type')
    list_filter = ('object_type',)
    search_fields = ('name',)

@admin.register(DataObject)
class DataObjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'inventory_number', 'model', 'user', 'next_maintenance_date')
    list_filter = ('model__object_type', 'next_maintenance_date')
    search_fields = ('name', 'inventory_number', 'model__name')
    fields = ('name', 'inventory_number', 'model', 'user','date_update_rule', 'next_maintenance_date', 'description') 
    
    inlines = [
        MainRelationsInline, 
        SubsRelationsInline, 
        CommentInline, 
        AttachmentInline
    ]

@admin.register(ActionHistory)
class ActionHistoryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'data_object', 'user', 'action')
    list_filter = ('created_at', 'user')
    readonly_fields = ('created_at',)
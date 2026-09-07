from django.contrib import admin
from .models import ObjectType, DateUpdateRule, ObjectModel, DataObject, ActionHistory, Comment, Attachment


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


@admin.register(ObjectType)
class ObjectTypeAdmin(admin.ModelAdmin):
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
    list_display = ('name', 'inventory_number', 'model', 'parent', 'next_maintenance_date')
    list_filter = ('model__object_type', 'next_maintenance_date')
    search_fields = ('name', 'inventory_number', 'youtrack_issue_id', 'model__name')
    fields = ('name', 'inventory_number', 'youtrack_issue_id', 'model', 'parent', 'date_update_rule', 'next_maintenance_date', 'description')
    
    inlines = [
        CommentInline, 
        AttachmentInline
    ]


@admin.register(ActionHistory)
class ActionHistoryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'data_object', 'user', 'action')
    list_filter = ('created_at', 'user')
    readonly_fields = ('created_at',)
from django.contrib import admin
from .models import ObjectType, DependencyType, ObjectModel, DataObject, Relation, ActionHistory, Comment, Attachment

class CommentInline(admin.TabularInline):
    model = Comment
    extra = 1


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 1


class ActionHistoryInline(admin.TabularInline):
    model = ActionHistory
    extra = 1


class RelationInline(admin.TabularInline):
    model = Relation
    fk_name = "main"
    extra = 1


@admin.register(ObjectType)
class ObjectTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'type')
    search_fields = ('type',)

@admin.register(DependencyType)
class DependencyTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'type')

@admin.register(ObjectModel)
class ObjectModelAdmin(admin.ModelAdmin):
    list_display = ('id', 'object_type', 'name')
    list_filter = ('object_type',)
    search_fields = ('name',)

@admin.register(DataObject)
class DataObjectAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'model', 'next_maintenance_date')
    list_filter = ('model__object_type','next_maintenance_date')
    inlines = [RelationInline, ActionHistoryInline, CommentInline, AttachmentInline]
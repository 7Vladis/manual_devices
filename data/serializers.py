from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from .models import ObjectType, DependencyType, ObjectModel, DataObject, Relation, ActionHistory, Comment, Attachment

class ObjectTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ObjectType
        fields = ['id', 'type']

    
class ObjectModelSerializer(serializers.ModelSerializer):
    object_type_name = serializers.CharField(source='object_type.type', read_only=True)
    new_type_name = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = ObjectModel
        fields = ['id', 'object_type', 'object_type_name', 'new_type_name','name', 'specifications']
        extra_kwargs = {'object_type': {'required':False, 'allow_null':True}}

    def create(self, validated_data):
        new_type_name = validated_data.pop('new_type_name', None)
        object_type = validated_data.get('object_type')
        if not object_type and new_type_name:
            object_type, created = ObjectType.objects.get_or_create(type=new_type_name.strip())
            validated_data['object_type'] = object_type
        if not validated_data.get('object_type'):
            raise serializers.ValidationError(
                {"object_type": "Выберите тип из списка или укажите название нового."}
            )
        return super().create(validated_data)


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ['id', 'data_object', 'text']

class AttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = ['id', 'data_object', 'path']


class ActionHistorySerializer(serializers.ModelSerializer):
    object_name = serializers.CharField(source='data_object.model.name', read_only=True)
    action_date = serializers.DateTimeField(format="%d.%m.%Y %H:%M:%S")

    class Meta:
        model = ActionHistory
        fields = ['data_object', 'object_name', 'action_date', 'action']


class DataObjectSerializer(serializers.ModelSerializer):
    model_details = ObjectModelSerializer(source='models', read_only=True)
    object_history = ActionHistorySerializer(source='actions', many=True, read_only=True)
    object_attachments = AttachmentSerializer(source='attachments',many=True, read_only=True)
    object_comments = CommentSerializer(source='comments', many=True, read_only=True)
    parent_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    dependency_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    new_dependency_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    initial_comment = serializers.CharField(write_only=True, required=False, allow_blank=True)
    uploaded_files = serializers.ListField(child=serializers.FileField(), write_only=True, required=False)

    class Meta:
        model = DataObject
        fields = ['id', 'name', 'model', 'model_details', 'next_maintenance_date', 'object_history', 'object_attachments', 'object_comments', 'parent_id', 'dependency_id', 'new_dependency_name', 'initial_comment', 'uploaded_files']
    
    def validate_name(self, value):
        if value and DataObject.objects.filter(name__iexact=value).exists():
            raise serializers.ValidationError("Объект с таким именем уже существует")
        return value
    
    def create(self, validated_data):
        parent_id = validated_data.pop('parent_id', None)
        dependency_id = validated_data.pop('dependency_id', None)
        new_dependency_name = validated_data.pop('new_dependency_name', None)
        initial_comment = validated_data.pop('initial_comment', None)
        uploaded_files = validated_data.pop('uploaded_files', [])
        with transaction.atomic():
            instance = super().create(validated_data)
            ActionHistory.objects.create(
                data_object=instance,
                action_date=timezone.now(),
                action=f"Объект '{instance.name or instance.model.name}' успешно создан."
            )
            if parent_id:
                dep_type = None
                if dependency_id:
                    dep_type = DependencyType.objects.get(id=dependency_id)
                elif new_dependency_name:
                    dep_type, _ = DependencyType.objects.get_or_create(type=new_dependency_name.strip())
                if dep_type:
                    Relation.objects.create(
                        main_id=parent_id,
                        subject=instance,
                        dependency_type=dep_type
                    )
            if initial_comment:
                Comment.objects.create(
                    data_object=instance,
                    text=initial_comment
                )
            for file in uploaded_files:
                Attachment.objects.create(
                    data_object=instance,
                    path=file
                )
            return instance



class DependencyTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DependencyType
        fields = ['id', 'type']


class RelationSerializer(serializers.ModelSerializer):
    main_name = serializers.CharField(source='main.model.name', read_only=True)
    subject_name = serializers.CharField(source='subject.model.name', read_only=True)
    dependency_name = serializers.CharField(source='dependency_type.type', read_only=True)

    class Meta:
        model = Relation
        fields = ['main', 'main_name', 'subject', 'subject_name', 'dependency_type', 'dependency_name']

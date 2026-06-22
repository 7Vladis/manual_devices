from notifications.services import send_mattermost_notification
from rest_framework import viewsets, filters
from django.db import transaction
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from .models import (
    ObjectType, DependencyType, ObjectModel, DataObject, Relation, ActionHistory, Comment, Attachment
)

from .serializers import (
    ObjectTypeSerializer, DependencyTypeSerializer, ObjectModelSerializer, DataObjectSerializer, RelationSerializer, ActionHistorySerializer, CommentSerializer, AttachmentSerializer
)

class DataObjectViewSet(viewsets.ModelViewSet):
    queryset = DataObject.objects.all().prefetch_related(
        'actions', 'attachments', 'comments', 'model__object_type'
    )
    serializer_class = DataObjectSerializer


    @action(detail=False, methods=['get'])
    def search(self, request):
        query = request.query_params.get('q', '').strip()
        if not query:
            return Response([])
        results = self.queryset.filter(
            Q(name__icontains=query)|
            Q(model__name__icontains=query) |
            Q(model__object_type__type__icontains=query) | 
            Q(model__specifications__icontains=query) |
            Q(comments__text__icontains=query) |
            Q(actions__action__icontains=query)
        ).distinct()
        if not results.exists():
            words = query.split()
            q_objects = Q()
            for word in words:
                q_objects |= Q(name__icontains=query)|\
                    Q(model__name__icontains=word) |\
                    Q(model__object_type__type__icontains=word) |\
                    Q(model__specifications__icontains=word) |\
                    Q(comments__text__icontains=word) |\
                    Q(actions__action__icontains=word)
            results = self.queryset.filter(q_objects).distinct()
        serializer = self.get_serializer(results, many=True)
        return Response(serializer.data)
    

    @action(detail=False, methods=['get'])
    def maintenance(self, request):
        period = request.query_params.get('period', 'week')
        now = timezone.now()
        if period == 'today':
            end_date = now.replace(hour=23, minute=59, second=59)
        elif period == 'month':
            end_date = now + timedelta(days=30)
        else:
            end_date = now + timedelta(days=7)
        results = self.queryset.filter(
            next_maintenance_date__range=[now, end_date]
        ).order_by('next_maintenance_date')
        serializer = self.get_serializer(results, many=True)
        return Response(serializer.data)
    

    @action(detail=False, methods=['get'])
    def roots(self, request):
        subjects_ids = Relation.objects.values_list('subject_id', flat=True)
        roots = self.queryset.exclude(id__in=subjects_ids)
        serializer = self.get_serializer(roots, many=True)
        return Response(serializer.data)
    
    
    @action(detail=True, methods=['get'])
    def children(self, request, pk=None):
        relations = Relation.objects.filter(main_id=pk).select_related('subject', 'dependency_type')
        data = []
        for rel in relations:
            data.append({
                "relation_type": rel.dependency_type.type,
                "object": DataObjectSerializer(rel.subject).data
            })
        return Response(data)
    
    @action(detail=True, methods=['post'])
    def chenge_parent(self, request, pk=None):
        obj = self.get_object()
        new_parent_id = request.data.get('new_parent_id')
        dependency_id = request.data.get('dependency_id')
        new_dependency_name = request.data.get('new_dependency_name')
        with transaction.atomic():
            old_relation = Relation.objects.filter(subject=obj).first()
            old_parent_name = old_relation.main.name if old_relation else "корня справочника"
            if old_relation:
                old_relation.delete()
            if new_parent_id:
                new_parent = DataObject.objects.get(id=new_parent_id)
                dep_type = None
                if dependency_id:
                    dep_type = DependencyType.objects.get(id=dependency_id)
                elif new_dependency_name:
                    dep_type, _ = DependencyType.objects.get_or_create(type=new_dependency_name.strip())
                if not dep_type:
                    return Response(
                        {"error": "Необходимо указать тип связи (ID или название)."}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                Relation.objects.create(
                    main=new_parent,
                    subject=obj,
                    dependency_type=dep_type
                )
                new_parent_name = new_parent.name
                action_text = f"Объект перемещен из '{old_parent_name} в '{new_parent_name}'."
            else:
                action_text = f"Объекть перемещен из '{old_parent_name}' в корень справочника."
            ActionHistory.objects.create(
                data_object=obj,
                action_date=timezone.now(),
                action=action_text
            )
            return Response({"status":"перемещено", "details": action_text})
        
    @action(detail=True, methods=['post'])
    def perform_maintenance(self, request, pk=None):
        obj = self.get_object()
        new_date = request.data.get('next_maintenance_date')
        comment = request.data.get('comment', 'Техническое обслуживание выполнено.')
        if not new_date:
            return Response({"error": "Укажите дату следующего ТО"}, status=400)
        with transaction.atomic():
            obj.next_maintenance_date = new_date
            obj.save()
            ActionHistory.objects.create(
                data_object=obj,
                action_date=timezone.now(),
                action=f"Выполнено ТО. Следующее обслуживания назначено на: {new_date}. Комментарий: {comment}"
            )
        send_mattermost_notification(f"ТО объекта {obj.name or obj.model.name} выполенено\nСледующая дата: {new_date}\nКомментарий: {comment}")
        return Response({"status":"обслужено", "next_date":new_date})
        

class ActionHistoryViewSet(viewsets.ModelViewSet):
    queryset = ActionHistory.objects.all().order_by('-action_date')
    serializer_class = ActionHistorySerializer

    @action(detail=False, methods=['get'])
    def recent(self, request):
        limit = int(request.query_params.get('limit', 10))
        recent_actions = self.queryset[:limit]
        serializer = self.get_serializer(recent_actions, many=True)
        return Response(serializer.data)
    

class ObjectTypeViewSet(viewsets.ModelViewSet):
    queryset = ObjectType.objects.all()
    serializer_class = ObjectTypeSerializer
    filter_backends = [filters.SearchFilter]
    search_fileds = ['type']


class ObjectModelViewSet(viewsets.ModelViewSet):
    queryset = ObjectModel.objects.all()
    serializer_class = ObjectModelSerializer


class RelationViewSet(viewsets.ModelViewSet):
    queryset = Relation.objects.all()
    serializer_class = RelationSerializer


class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all()
    serializer_class = CommentSerializer


class AttachmentViewSet(viewsets.ModelViewSet):
    queryset = Attachment.objects.all()
    serializer_class = AttachmentSerializer


class DependencyTypeViewSet(viewsets.ModelViewSet):
    queryset = DependencyType.objects.all()
    serializer_class = DependencyTypeSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['type']
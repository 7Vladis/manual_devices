from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DataObjectViewSet, ActionHistoryViewSet, ObjectTypeViewSet,
    ObjectModelViewSet, RelationViewSet, CommentViewSet,
    AttachmentViewSet, DependencyTypeViewSet
)

router = DefaultRouter()
router.register(r'objects', DataObjectViewSet, basename='objects')
router.register(r'history', ActionHistoryViewSet, basename='history')
router.register(r'object-types', ObjectTypeViewSet, basename='object-types')
router.register(r'models', ObjectModelViewSet, basename='models')
router.register(r'relations', RelationViewSet, basename='relations')
router.register(r'comments', CommentViewSet, basename='comments')
router.register(r'attachments', AttachmentViewSet, basename='attachments')
router.register(r'dependency-types', DependencyTypeViewSet, basename='dependency-types')

urlpatterns = [
    path('', include(router.urls)),
]
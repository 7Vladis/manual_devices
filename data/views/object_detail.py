"""Карточка объекта и её вкладки."""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import Attachment, DataObject, ObjectModel, ObjectType

from .common import get_comments_for, get_files_for, page_of, tree_queryset


@login_required
def object_detail_view(request, pk):
    """
    Карточка объекта. Это чистый GET: никаких обращений к YouTrack и записи
    в БД (кроме курсора активного объекта в сессии). Синхронизацию запускает
    сама карточка сразу после загрузки — фоновым POST на sync_youtrack_view,
    так что пользователь видит актуальные данные без ручных действий.
    """
    return render_object_detail(request, pk)


def render_object_detail(request, pk):
    """Рендер карточки с OOB-обновлением подсветки узлов дерева."""
    obj = get_object_or_404(
        DataObject.objects.select_related('model', 'model__object_type', 'parent', 'parent__model').prefetch_related('children'), 
        pk=pk
    )
    prev_active_id = request.session.get('active_object_id')
    request.session['active_object_id'] = str(pk)
    request.session.modified = True

    context = {
        'obj': obj,
        'parent': obj.parent,
        'active_tab': 'short_info',
        'today': timezone.localdate(),
        'soon_date': timezone.localdate() + timedelta(days=14),
        'comments_count': obj.comments.count(),
        'files_count': obj.attachments.count(),
        'history_count': obj.actions.count(),
        'specs_count': len(obj.model.specifications or {}) if obj.model else 0,
    }
    
    response_content = render_to_string('data/object/object_details.html', context, request=request)
    oob_elements = []
    
    new_node_html = render_to_string('data/tree/object_tree_node_label.html', {
        'node': obj,
        'is_active': True,
        'oob': True
    }, request=request)
    oob_elements.append(new_node_html)
    
    if prev_active_id and prev_active_id != str(pk):
        try:
            prev_obj = DataObject.objects.get(pk=prev_active_id)
            old_node_html = render_to_string('data/tree/object_tree_node_label.html', {
                'node': prev_obj,
                'is_active': False,
                'oob': True
            }, request=request)
            oob_elements.append(old_node_html)
        except DataObject.DoesNotExist:
            pass
            
    combined_content = response_content + "\n" + "\n".join(oob_elements)
    
    if request.GET.get('sidebar'):
        roots = tree_queryset(DataObject.objects.filter(parent__isnull=True))
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        combined_content = combined_content + f'\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        
    return HttpResponse(combined_content)


@login_required
def object_tab_view(request, pk, tab_name):
    obj = get_object_or_404(DataObject, pk=pk)
    context = {'obj': obj, 'today': timezone.localdate()}
    
    if tab_name == 'short_info':
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        context['preview'] = preview
        template = 'data/object/object_tab_short_info.html'
        
    elif tab_name == 'specs':
        context['specifications'] = obj.model.specifications or {}
        template = 'data/object/object_tab_specs.html'
        
    elif tab_name == 'comments':
        context['comments'] = get_comments_for(obj, request.user, request.GET.get('page'))
        template = 'data/object/object_tab_comments.html'

    elif tab_name == 'files':
        context['files'] = get_files_for(obj, request.GET.get('page'))
        template = 'data/object/object_tab_files.html'

    elif tab_name == 'history':
        context['history'] = page_of(
            obj.actions.select_related('user').order_by('-created_at'),
            request.GET.get('page'),
        )
        template = 'data/object/object_tab_history.html'
        
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)

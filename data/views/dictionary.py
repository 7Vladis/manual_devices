"""Справочник: поиск, дерево, проводник."""

from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from ..models import DataObject, ObjectModel, ObjectType

from .common import get_ancestors_chain, tree_queryset


@login_required
def search_view(request):
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return HttpResponse('')

    words = [w for w in query.split() if w]

    def term_filter(term):
        """Все поля, по которым ищем один терм (см. подсказку в шапке поиска)."""
        return (
            Q(name__icontains=term) |
            Q(inventory_number__icontains=term) |
            Q(description__icontains=term) |
            Q(model__name__icontains=term) |
            Q(youtrack_issue_id__icontains=term) |
            Q(comments__text__icontains=term) |
            Q(actions__action__icontains=term)
        )

    # 1. Поиск по точному совпадению всей фразы (в любом поле)
    search_filter = term_filter(query)

    # 2. Если введено несколько слов (например, "4 стол" или "стол 4"),
    # дополнительно ищем объекты, где встречаются ВСЕ эти слова одновременно
    if len(words) > 1:
        words_q = Q()
        for word in words:
            words_q &= term_filter(word)
        search_filter |= words_q

    results = DataObject.objects.filter(
        search_filter
    ).select_related('model', 'model__object_type').distinct()[:10]

    return render(request, 'data/includes/search_results_list.html', {'results': results})


@login_required
def dict_view(request):
    active_tab = request.GET.get('tab', 'objects')
    
    selected_object_id = request.GET.get('object') or request.session.get('active_object_id')
    selected_model_id = request.GET.get('model') or request.session.get('active_model_id')
    
    active_object = None
    active_model = None
    parent_uuids = []
    
    explorer_mode = request.session.get('explorer_mode', 'tree')
    explorer_parent_uuid = request.session.get('explorer_parent_uuid')
    explorer_parent = None
    
    if selected_object_id and active_tab == 'objects':
        try:
            active_object = DataObject.objects.select_related('parent').prefetch_related('children').get(pk=selected_object_id)
            request.session['active_object_id'] = str(active_object.pk)
            
            # Строим цепочку родителей для разворачивания дерева
            parent_uuids = [str(anc.pk) for anc in get_ancestors_chain(active_object)]
        except DataObject.DoesNotExist:
            request.session['active_object_id'] = None
                
    elif selected_model_id and active_tab == 'models':
        try:
            active_model = ObjectModel.objects.get(pk=selected_model_id)
            request.session['active_model_id'] = str(active_model.pk)
        except ObjectModel.DoesNotExist:
            request.session['active_model_id'] = None
        
    # Ищем текущую папку проводника
    if explorer_mode == 'flat' and explorer_parent_uuid:
        try:
            explorer_parent = DataObject.objects.select_related('parent').get(pk=explorer_parent_uuid)
            for node in get_ancestors_chain(explorer_parent) + [explorer_parent]:
                if str(node.pk) not in parent_uuids:
                    parent_uuids.append(str(node.pk))
        except DataObject.DoesNotExist:
            explorer_parent_uuid = None
            request.session['explorer_parent_uuid'] = None
    
    request.session.modified = True

    models = ObjectModel.objects.all().order_by('name')
    object_types = ObjectType.objects.all().order_by('type')
    
    context = {
        'models': models,
        'object_types': object_types,
        'active_tab': active_tab,
        'active_object': active_object,
        'active_model': active_model,
        'parent_uuids': parent_uuids,
        'explorer_mode': explorer_mode,
        'explorer_parent': explorer_parent,
    }
    
    if active_tab == 'models':
        context['object_types_list'] = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
    else:
        if explorer_mode == 'flat' and explorer_parent:
            context['initial_objects'] = tree_queryset(explorer_parent.children.all())
        else:
            context['initial_objects'] = tree_queryset(DataObject.objects.filter(
                parent__isnull=True
            ))
        
    if request.headers.get('HX-Request') and request.GET.get('sidebar'):
        return render(request, 'data/tree/dict_sidebar.html', context)
        
    return render(request, 'data/dict.html', context)


@login_required
@require_POST
def toggle_explorer_mode_view(request):
    """Переключает режим отображения с умной синхронизацией позиции"""
    if request.method == 'POST':
        current_mode = request.session.get('explorer_mode', 'tree')
        new_mode = 'flat' if current_mode == 'tree' else 'tree'
        request.session['explorer_mode'] = new_mode
        
        if new_mode == 'flat':
            active_id = request.session.get('active_object_id')
            if active_id:
                try:
                    obj = DataObject.objects.prefetch_related('children').select_related('parent').get(pk=active_id)
                    if obj.children.exists():
                        request.session['explorer_parent_uuid'] = str(obj.pk)
                    elif obj.parent:
                        request.session['explorer_parent_uuid'] = str(obj.parent.pk)
                    else:
                        request.session['explorer_parent_uuid'] = None
                except DataObject.DoesNotExist:
                    request.session['explorer_parent_uuid'] = None
            else:
                request.session['explorer_parent_uuid'] = None

        request.session.modified = True
        
        html = render_to_string('includes/explorer_toggle_btn.html',
                                {'explorer_mode': new_mode}, request=request)
        
        response = HttpResponse(html)
        response['HX-Trigger'] = 'explorerModeChanged'
        return response
        
    return HttpResponse("Метод не разрешен", status=405)


@login_required
def explorer_navigate_view(request, pk):
    """Переход внутрь папки (или в корень, если pk='root')"""
    if str(pk) == 'root':
        request.session['explorer_parent_uuid'] = None
    else:
        request.session['explorer_parent_uuid'] = str(pk)
        
    request.session.modified = True
    response = HttpResponse()
    response['HX-Trigger'] = 'explorerModeChanged'
    return response


@login_required
def explorer_up_view(request):
    """Переход на один уровень вверх в плоском режиме (вплоть до корня)"""
    parent_uuid = request.session.get('explorer_parent_uuid')
    if parent_uuid:
        try:
            current_parent = DataObject.objects.select_related('parent').get(pk=parent_uuid)
            request.session['explorer_parent_uuid'] = str(current_parent.parent.pk) if current_parent.parent else None
        except DataObject.DoesNotExist:
            request.session['explorer_parent_uuid'] = None
    else:
        request.session['explorer_parent_uuid'] = None
            
    request.session.modified = True
    response = HttpResponse()
    response['HX-Trigger'] = 'explorerModeChanged'
    return response


@login_required
def object_children_view(request, parent_uuid):
    parent = get_object_or_404(DataObject, pk=parent_uuid)
    children = tree_queryset(parent.children.all())
    
    active_object_id = request.GET.get('active_object')
    active_object = None
    parent_uuids = []
    
    if active_object_id:
        try:
            active_object = DataObject.objects.select_related('parent').get(pk=active_object_id)
            parent_uuids = [str(anc.pk) for anc in get_ancestors_chain(active_object)]
        except DataObject.DoesNotExist:
            pass

    return render(request, 'data/tree/object_tree_list_nodes.html', {
        'objects': children,
        'parent': parent,
        'active_object': active_object,
        'parent_uuids': parent_uuids,
    })

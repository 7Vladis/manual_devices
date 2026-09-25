"""Копирование объекта вместе с поддеревом."""

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .common import tree_queryset
from ..models import DataObject, ObjectModel, ObjectType
from users.decorators import role_required

from ..services.cloning import deep_clone_object


@login_required
@role_required(['senior', 'admin', 'superuser'])
def clone_object_modal_view(request):
    """Отображение модального окна тиражирования объекта"""
    active_object_id = request.GET.get('active_object_id') or request.session.get('active_object_id')
    preselected_object = None
    if active_object_id:
        try:
            preselected_object = DataObject.objects.get(pk=active_object_id)
        except DataObject.DoesNotExist:
            pass

    return render(request, 'data/includes/clone_object_modal_body.html', {
        'preselected_object': preselected_object
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def clone_object_view(request):
    """Создание копии объекта (обычной или с дочерними элементами)"""
    if request.method == 'POST':
        # Поддерживаем оба имени параметра: source_object и parent (из подсказок поиска)
        source_uuid = request.POST.get('source_object') or request.POST.get('parent')
        new_name = request.POST.get('new_name', '').strip()
        clone_children = request.POST.get('clone_children') == 'on'
        keep_parent = request.POST.get('keep_parent') == 'on'
        
        if source_uuid:
            source_obj = get_object_or_404(DataObject.objects.select_related('parent'), pk=source_uuid)
            
            if not new_name:
                new_name = f"{source_obj.name or source_obj.model.name} (копия)"
                
            new_parent = source_obj.parent if keep_parent else None
            
            # Вся копия создаётся одной транзакцией: при ошибке в середине
            # не остаётся наполовину склонированного поддерева.
            with transaction.atomic():
                deep_clone_object(
                    source_obj=source_obj,
                    new_root_name=new_name,
                    new_parent=new_parent,
                    user=request.user,
                    clone_children=clone_children
                )

        roots = tree_queryset(DataObject.objects.filter(parent__isnull=True))
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return HttpResponse("Метод не разрешен", status=405)

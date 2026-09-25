"""Модели оборудования: карточка, вкладки, характеристики."""

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, ProtectedError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.views.decorators.http import require_POST, require_http_methods

from ..models import ObjectModel, ObjectType
from users.decorators import role_required

from .common import htmx_error


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_http_methods(["GET", "POST", "DELETE"])
def delete_model_view(request, pk):
    """
    GET  — окно подтверждения; если модель используется объектами, удаление
           недоступно (FK защищён PROTECT), окно объясняет, что делать.
    POST — удаление свободной модели.
    """
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    objects_count = model_obj.data_objects.count()

    if request.method == 'GET':
        return render(request, 'data/includes/confirm_delete_model.html', {
            'model_obj': model_obj,
            'objects_count': objects_count,
        })

    if request.method not in ['POST', 'DELETE']:
        return HttpResponse("Метод не разрешен", status=405)

    model_uuid = model_obj.uuid
    try:
        model_obj.delete()
    except ProtectedError:
        return htmx_error(
            f'Модель используется объектами ({objects_count} шт.) — сначала переназначьте им другую модель.',
            status=409,
            retarget='#confirm-modal-content .form-feedback'
        )

    was_active = request.session.get('active_model_id') == str(model_uuid)
    parts = [format_html('<li id="model-node-{}" hx-swap-oob="delete"></li>', model_uuid)]
    if was_active:
        request.session['active_model_id'] = None
        request.session.modified = True
        parts.append(render_to_string('data/includes/detail_placeholder.html', {'oob': True}, request=request))
    response = HttpResponse("\n".join(parts), status=200)
    response['HX-Trigger'] = 'modelDeleted'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_model_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        type_uuid = request.POST.get('object_type')
        new_type_name = request.POST.get('new_object_type')
        
        spec_keys = request.POST.getlist('spec_keys')
        spec_values = request.POST.getlist('spec_values')
        specifications = dict(zip(spec_keys, spec_values))
        
        if new_type_name:
            object_type, _ = ObjectType.objects.get_or_create(type=new_type_name)
        else:
            object_type = ObjectType.objects.filter(pk=type_uuid).first() if type_uuid else None

        if object_type is None:
            return htmx_error(
                'Не выбран тип оборудования. Выберите существующий тип или создайте новый.',
                retarget='#createModelModal .form-feedback'
            )
            
        try:
            # atomic() ставит savepoint: после IntegrityError транзакция
            # остаётся рабочей и мы можем отрендерить ответ с ошибкой.
            with transaction.atomic():
                ObjectModel.objects.create(
                    name=name,
                    object_type=object_type,
                    specifications=specifications
                )
        except IntegrityError:
            # Ограничение в БД — последний рубеж: подсказка при вводе имени
            # не спасает от гонки двух одновременных запросов.
            return htmx_error(
                f'Модель «{name}» уже существует для типа «{object_type.type}». '
                'Откройте существующую модель вместо создания дубликата.',
                status=409,
                retarget='#createModelModal .form-feedback'
            )
        
        object_types = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
        context = {
            'object_types_list': object_types,
            'active_tab': 'models',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return render(request, 'data/includes/create_model_modal_body.html')


@login_required
def model_detail_view(request, pk):
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    prev_active_id = request.session.get('active_model_id')
    request.session['active_model_id'] = str(pk)
    
    context = {
        'model_obj': model_obj,
        'active_tab': 'specs',
        'specs_count': len(model_obj.specifications or {}),
        'objects_count': model_obj.data_objects.count(),
    }
    
    response_content = render_to_string('data/model/model_details.html', context, request=request)
    oob_elements = []
    
    new_node_html = render_to_string('data/tree/model_tree_node_label.html', {
        'model': model_obj,
        'is_active': True,
        'oob': True
    }, request=request)
    oob_elements.append(new_node_html)
    
    if prev_active_id and prev_active_id != str(pk):
        try:
            prev_model = ObjectModel.objects.get(pk=prev_active_id)
            old_node_html = render_to_string('data/tree/model_tree_node_label.html', {
                'model': prev_model,
                'is_active': False,
                'oob': True
            }, request=request)
            oob_elements.append(old_node_html)
        except ObjectModel.DoesNotExist:
            pass
            
    combined_content = response_content + "\n" + "\n".join(oob_elements)
    
    if request.GET.get('sidebar'):
        object_types = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
        sidebar_context = {
            'object_types_list': object_types,
            'active_tab': 'models',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        combined_content = combined_content + f'\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        
    return HttpResponse(combined_content)


@login_required
def model_tab_view(request, pk, tab_name):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    context = {'model_obj': model_obj}
    
    if tab_name == 'specs':
        context['specifications'] = model_obj.specifications or {}
        template = 'data/model/model_tab_specs.html'
    elif tab_name == 'objects':
        objects = model_obj.data_objects.all().select_related('model__object_type').order_by('name')
        context['objects'] = objects
        template = 'data/model/model_tab_objects.html'
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def model_spec_add_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key', '').strip()
    value = request.POST.get('value', '').strip()
    if key and value:
        specs = model_obj.specifications or {}
        specs[key] = value
        model_obj.specifications = specs
        model_obj.save(update_fields=['specifications'])
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications or {}
    }
    return render(request, 'data/model/model_tab_specs.html', context)


@login_required
@role_required(['senior', 'admin', 'superuser'])
def model_spec_edit_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.GET.get('key') or request.POST.get('key')
    
    if request.method == 'POST':
        old_key = request.POST.get('old_key')
        new_key = request.POST.get('key', '').strip()
        value = request.POST.get('value', '').strip()
        specs = model_obj.specifications or {}
        
        if old_key and new_key and value:
            if old_key != new_key:
                specs.pop(old_key, None)
            specs[new_key] = value
            model_obj.specifications = specs
            model_obj.save(update_fields=['specifications'])
            
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)
        
    # specifications допускает NULL — без защиты это AttributeError и 500
    value = (model_obj.specifications or {}).get(key, '')
    return render(request, 'data/model/inline_spec_row.html', {
        'model_obj': model_obj,
        'key': key,
        'value': value,
        'editing': True
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def model_spec_delete_view(request, pk):
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key')
    specs = model_obj.specifications or {}
    
    if key in specs:
        specs.pop(key)
        model_obj.specifications = specs
        model_obj.save(update_fields=['specifications'])
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications
    }
    return render(request, 'data/model/model_tab_specs.html', context)


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_model_name_view(request, pk):
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': False})
        
    if request.method == 'POST':
        old_name = model_obj.name
        new_name = request.POST.get('name', '').strip()
        
        if new_name and old_name != new_name:
            model_obj.name = new_name
            try:
                with transaction.atomic():
                    model_obj.save(update_fields=['name'])
            except IntegrityError:
                model_obj.name = old_name
                return htmx_error(
                    f'Модель «{new_name}» уже существует для типа «{model_obj.object_type.type}».',
                    status=409,
                    retarget='#model-name-error'
                )
            
        model_name_html = render_to_string('data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': False}, request=request)
        sidebar_model_node_html = render_to_string('data/tree/model_tree_node_label.html', {
            'model': model_obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(model_name_html + "\n" + sidebar_model_node_html)
        
    return render(request, 'data/model/inline_model_name.html', {'model_obj': model_obj, 'editing': True})

"""Подсказки, проверка имён и конструктор характеристик."""

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from ..models import DataObject, DateUpdateRule, ObjectModel, ObjectType


@login_required
def check_model_name_view(request):
    """Подсказка при вводе названия модели: точный дубликат или похожие записи"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    # Уникальность действует в пределах типа оборудования, поэтому при
    # известном типе сужаем проверку до него.
    exact_qs = ObjectModel.objects.select_related('object_type').filter(name__iexact=name)
    type_uuid = request.GET.get('object_type')
    if type_uuid:
        exact_qs = exact_qs.filter(object_type=type_uuid)
    exact_match = exact_qs.first()
    similar_models = []
    if not exact_match:
        stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по', 'of', 'and', 'the'}
        words = [w.lower() for w in name.split() if len(w) >= 2 and w.lower() not in stop_words]
        if words:
            query = Q()
            for word in words:
                query |= Q(name__icontains=word) | Q(object_type__type__icontains=word)
            similar_models = ObjectModel.objects.filter(query).select_related('object_type').distinct()[:5]

    return render(request, 'data/includes/name_check_result.html', {
        'kind': 'model',
        'exact': exact_match,
        'similar': similar_models,
        'modal_id': 'createModelModal',
    })


@login_required
def check_object_name_view(request):
    """Подсказка при вводе имени объекта: точный дубликат или похожие записи"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    exact_match = DataObject.objects.select_related('model').filter(name__iexact=name).first()
    similar_objects = []
    if not exact_match:
        stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по'}
        words = [w.lower() for w in name.split() if len(w) >= 3 and w.lower() not in stop_words]
        if words:
            query = Q()
            for word in words:
                query |= Q(name__icontains=word) | Q(model__name__icontains=word)
            similar_objects = DataObject.objects.filter(query).select_related('model').distinct()[:5]

    return render(request, 'data/includes/name_check_result.html', {
        'kind': 'object',
        'exact': exact_match,
        'similar': similar_objects,
        'modal_id': 'createObjectModal',
    })


@login_required
def suggest_view(request):
    field = request.GET.get('field', '').strip()
    q = request.GET.get('q', '').strip()
    exclude_uuid = request.GET.get('exclude_uuid')
    target_obj_uuid = request.GET.get('target_obj_uuid') or exclude_uuid
    
    results = []
    words = q.split() if q else []
    
    # 1. Типы оборудования
    if field in ['object_type']:
        if q:
            exact = ObjectType.objects.filter(type__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= Q(type__icontains=w)
            partial = ObjectType.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(ObjectType.objects.all().order_by('type')[:8])
        
    # 2. Модели оборудования (включая model_inline)
    elif field in ['model', 'model_inline']:
        if q:
            exact = ObjectModel.objects.filter(name__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= (Q(name__icontains=w) | Q(object_type__type__icontains=w))
            partial = ObjectModel.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(ObjectModel.objects.select_related('object_type').all().order_by('name')[:8])
        
    # 3. Родительские объекты (включая parent_inline и source_object)
    elif field in ['parent', 'parent_inline', 'source_object']:
        qs = DataObject.objects.select_related('model', 'model__object_type').all()
        
        # Защита от зацикливания: исключаем сам объект и всё его поддерево
        if exclude_uuid:
            excluded = {exclude_uuid}
            source = DataObject.objects.filter(pk=exclude_uuid).first()
            if source:
                excluded |= {str(u) for u in source.get_descendant_uuids()}
            qs = qs.exclude(pk__in=excluded)
            
        if q:
            exact = qs.filter(Q(name__iexact=q) | Q(inventory_number__iexact=q))
            word_filter = Q()
            for w in words:
                word_filter &= (Q(name__icontains=w) | Q(inventory_number__icontains=w) | Q(model__name__icontains=w))
            partial = qs.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            # Если поле пустое, показываем первые объекты для быстрого выбора
            results = list(qs.order_by('name')[:8])

    # 4. Правила расчета ТО
    elif field in ['date_update_rule']:
        if q:
            exact = DateUpdateRule.objects.filter(name__iexact=q)
            word_filter = Q()
            for w in words:
                word_filter &= Q(name__icontains=w)
            partial = DateUpdateRule.objects.filter(word_filter).exclude(pk__in=exact)
            results = list(exact) + list(partial)
        else:
            results = list(DateUpdateRule.objects.all().order_by('name')[:8])

    # Создание значения «на лету» поддерживается только для типов оборудования:
    # для правил ТО нужен конструктор параметров, которого нет в подсказках.
    show_create_option = False
    if field == 'object_type' and q:
        has_exact_match = any(item.type.lower() == q.lower() for item in results)
        if not has_exact_match:
            show_create_option = True

    return render(request, 'data/includes/suggestions_list.html', {
        'results': results[:10],
        'field': field,
        'q': q,
        'target_obj_uuid': target_obj_uuid,
        'show_create_option': show_create_option
    })


@login_required
def select_suggestion_view(request):
    field = request.GET.get('field')
    uuid_val = request.GET.get('uuid')
    name_val = request.GET.get('name')
    
    display_name = ""
    hidden_name = field
    hidden_value = ""
    
    if uuid_val:
        hidden_value = uuid_val
        if field == 'object_type':
            display_name = get_object_or_404(ObjectType, pk=uuid_val).type
        elif field == 'model':
            model_obj = get_object_or_404(ObjectModel, pk=uuid_val)
            display_name = f"{model_obj.name} ({model_obj.object_type.type})"
        elif field in ['parent', 'source_object']:
            obj = get_object_or_404(DataObject, pk=uuid_val)
            display_name = obj.name or obj.model.name
        elif field == 'date_update_rule':
            display_name = get_object_or_404(DateUpdateRule, pk=uuid_val).name
    elif name_val:
        display_name = f"{name_val} (Создать новое)"
        hidden_name = f"new_{field}"
        hidden_value = name_val
        
    return render(request, 'data/includes/suggestion_selected.html', {
        'field': field,
        'display_name': display_name,
        'hidden_name': hidden_name,
        'hidden_value': hidden_value
    })


@login_required
def reset_suggestion_view(request):
    field = request.GET.get('field')
    placeholders = {
        'object_type': 'Введите тип оборудования...',
        'model': 'Введите модель оборудования...',
        'parent': 'Поиск родительского объекта...',
        'source_object': 'Поиск объекта для копирования...',
        'date_update_rule': 'Введите правило обновления...'
    }
    return render(request, 'data/includes/suggestion_input.html', {
        'field': field,
        'placeholder': placeholders.get(field, 'Начните вводить...')
    })


@login_required
@require_http_methods(["GET", "POST"])
def specs_builder_view(request):
    """
    Конструктор характеристик модели. Ничего не сохраняет: собирает состояние
    из переданных полей и возвращает разметку, поэтому GET здесь безопасен —
    именно им модальное окно загружает пустой конструктор при открытии.
    """
    data = request.POST if request.method == 'POST' else request.GET
    keys = data.getlist('spec_keys')
    values = data.getlist('spec_values')
    specs = dict(zip(keys, values))
    
    new_key = data.get('new_key', '').strip()
    new_value = data.get('new_value', '').strip()
    if new_key and new_value:
        specs[new_key] = new_value
        
    remove_key = data.get('remove_key')
    if remove_key:
        specs.pop(remove_key, None)
        
    return render(request, 'data/includes/specs_builder.html', {
        'specifications': specs
    })

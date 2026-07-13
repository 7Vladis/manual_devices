from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.db.models import Q, Prefetch
from django.utils import timezone
from datetime import timedelta, datetime
from .models import DataObject, ActionHistory, ObjectModel, ObjectType, Relation, DependencyType, Attachment, Comment

# (Существующие вспомогательные функции для дашборда)
def get_period_limits(period_type):
    now = timezone.now()
    today = now.date()
    if period_type == 'week':
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period_type == 'month':
        start = today.replace(day=1)
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
    else:
        start = end = today
    start_dt = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end, datetime.max.time()))
    return start_dt, end_dt

@login_required
def dashboard(request):
    now = timezone.now()
    total_objects = DataObject.objects.count()
    overdue_count = DataObject.objects.filter(next_maintenance_date__lt=now).count()
    
    def get_stats(period):
        start, end = get_period_limits(period)
        planned = DataObject.objects.filter(next_maintenance_date__range=(start, end)).count()
        completed = ActionHistory.objects.filter(
            created_at__range=(start, end),
            action__icontains="Техническое обслуживание выполнено"
        ).count()
        return completed, planned

    week_done, week_all = get_stats('week')
    month_done, month_all = get_stats('month')

    context = {
        'total_objects': total_objects,
        'overdue_count': overdue_count,
        'week_done': week_done,
        'week_all': week_all,
        'week_percent': (week_done / week_all * 100) if week_all > 0 else 0,
        'month_done': month_done,
        'month_all': month_all,
        'month_percent': (month_done / month_all * 100) if month_all > 0 else 0,
    }
    return render(request, 'data/dashboard.html', context)

@login_required
def maintenance_list(request):
    period = request.GET.get('period', 'week')
    now = timezone.now()
    
    if period == 'overdue':
        objects = DataObject.objects.filter(next_maintenance_date__lt=now)
        label = "Просроченные ТО"
    elif period == 'all':
        objects = DataObject.objects.all()
        label = "Все объекты системы"
    elif period == 'month':
        _, end = get_period_limits('month')
        objects = DataObject.objects.filter(next_maintenance_date__lte=end)
        label = "План на месяц"
    else:
        _, end = get_period_limits('week')
        objects = DataObject.objects.filter(next_maintenance_date__lte=end)
        label = "План на неделю"
        
    objects = objects.select_related('model', 'model__object_type').order_by('next_maintenance_date')

    return render(request, 'data/includes/maintenance_table.html', {
        'objects': objects,
        'now': now,
        'period_label': label
    })

@login_required
def search_view(request):
    query = request.GET.get('q', '').strip()
    if not query or len(query) < 2:
        return HttpResponse('')

    base_filters = (
        Q(name__icontains=query) |
        Q(inventory_number__icontains=query) |
        Q(model__name__icontains=query) |
        Q(model__specifications__icontains=query) |
        Q(comments__text__icontains=query) |
        Q(actions__action__icontains=query)
    )
    
    results = DataObject.objects.filter(base_filters).distinct()
    
    if results.count() < 3:
        words = query.split()
        if len(words) > 1:
            word_filters = Q()
            for word in words:
                word_filters |= (
                    Q(name__icontains=word) | 
                    Q(model__name__icontains=word) |
                    Q(model__specifications__icontains=word)
                )
            results = (results | DataObject.objects.filter(word_filters)).distinct()

    return render(request, 'data/includes/search_results_list.html', {'results': results[:10]})


# ==========================================
# ЭТАП 1: СПРАВОЧНИК И ПРОВОДНИК
# ==========================================

@login_required
def dict_view(request):
    """Главная страница справочника (содержит разметку левого и правого окон)"""
    active_tab = request.GET.get('tab', 'objects')
    
    # Общие данные для модальных окон создания
    models = ObjectModel.objects.all().order_by('name')
    object_types = ObjectType.objects.all().order_by('type')
    
    context = {
        'models': models,
        'object_types': object_types,
        'active_tab': active_tab,
    }
    
    # Наполнение контекста в зависимости от активной вкладки
    if active_tab == 'models':
        context['object_types_list'] = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
    else:
        context['initial_objects'] = DataObject.objects.exclude(
            main_relations__isnull=False
        ).order_by('name')
        
    # Если это HTMX-запрос на обновление левой панели (sidebar)
    if request.headers.get('HX-Request') and request.GET.get('sidebar'):
        return render(request, 'data/tree/dict_sidebar.html', context)
        
    return render(request, 'data/dict.html', context)


@login_required
def object_tree_view(request):
    """Возвращает только дерево объектов (для вкладки 'Объекты')"""
    roots = DataObject.objects.exclude(
        main_relations__isnull=False
    ).order_by('name')
    return render(request, 'data/tree/object_tree_list.html', {'objects': roots})


@login_required
def object_children_view(request, parent_uuid):
    """Возвращает дочерние объекты конкретного родителя"""
    parent = get_object_or_404(DataObject, pk=parent_uuid)
    # Находим все DataObject, которые связаны отношением, где parent является главным (main)
    children = DataObject.objects.filter(
        main_relations__main=parent
    ).order_by('name')
    
    return render(request, 'data/tree/object_tree_list_nodes.html', {
        'objects': children,
        'parent': parent
    })


@login_required
def model_tree_view(request):
    """Возвращает дерево моделей, сгруппированных по типам объектов"""
    # Выбираем типы объектов и подгружаем связанные модели, отсортированные по алфавиту
    object_types = ObjectType.objects.prefetch_related(
        Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
    ).order_by('type')
    
    return render(request, 'data/tree/model_tree_list.html', {'object_types': object_types})


@login_required
def service_object_view(request, pk):
    """Обслуживание объекта: вывод формы и обработка сохранения"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        date_str = request.POST.get('maintenance_date')
        if date_str:
            maintenance_date = timezone.make_aware(datetime.strptime(date_str, '%Y-%m-%d'))
            obj.next_maintenance_date = maintenance_date
            obj.save()
            
            # Запись в историю действий
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action=f"Техническое обслуживание выполнено. Следующее ТО запланировано на {maintenance_date.strftime('%d.%m.%Y')}."
            )
        
        # Перерендерим только этот узел в дереве, чтобы обновить дату или состояние
        # Нам нужно понять, является ли он корневым или дочерним, но шаблон отображения узла одинаков
        # Для обновления возвращаем обновленный элемент дерева
        # Так как модалка закрывается с помощью data-bs-dismiss на кнопке отправки, мы просто возвращаем узел
        # Дополнительно передадим заголовок HX-Trigger, чтобы обновить правое окно, если этот объект был там открыт
        response = render(request, 'data/tree/object_tree_node.html', {'node': obj})
        response['HX-Trigger'] = 'objectServiced'
        return response

    # GET запрос возвращает HTML-код формы для модального окна
    return render(request, 'data/tree/service_modal_body.html', {'obj': obj})


@login_required
def delete_object_view(request, pk):
    """Удаление объекта. При вызове через HTMX возвращает пустой ответ, тем самым удаляя узел из дерева"""
    if request.method in ['POST', 'DELETE']:
        obj = get_object_or_404(DataObject, pk=pk)
        obj.delete()
        return HttpResponse("", status=200) # HTMX удалит элемент благодаря hx-target="this" и swap="delete"
    return HttpResponse("Метод не разрешен", status=405)


@login_required
def delete_model_view(request, pk):
    """Удаление модели"""
    if request.method in ['POST', 'DELETE']:
        model_obj = get_object_or_404(ObjectModel, pk=pk)
        model_obj.delete()
        return HttpResponse("", status=200)
    return HttpResponse("Метод не разрешен", status=405)


@login_required
def create_object_view(request):
    """Быстрое создание объекта из левого меню"""
    if request.method == 'POST':
        name = request.POST.get('name')
        model_uuid = request.POST.get('model')
        inventory_number = request.POST.get('inventory_number')
        parent_uuid = request.POST.get('parent')
        
        model_obj = get_object_or_404(ObjectModel, pk=model_uuid)
        
        # Создаем объект
        new_obj = DataObject.objects.create(
            name=name,
            model=model_obj,
            inventory_number=inventory_number,
            user=request.user
        )
        
        # Если указан родительский объект, создаем связь в Relation
        if parent_uuid:
            parent_obj = get_object_or_404(DataObject, pk=parent_uuid)
            # Ищем или создаем базовый тип зависимости "Состоит из"
            dep_type, _ = DependencyType.objects.get_or_create(type="Входит в состав")
            Relation.objects.create(
                main=parent_obj,
                subject=new_obj,
                dependency_type=dep_type
            )
            
        # Записываем действие в историю
        ActionHistory.objects.create(
            user=request.user,
            data_object=new_obj,
            action="Объект успешно зарегистрирован в системе."
        )
        
        # ОТДАЕМ ОБНОВЛЕННЫЙ САЙДБАР ЦЕЛИКОМ
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)


@login_required
def create_model_view(request):
    """Быстрое создание модели из левого меню"""
    if request.method == 'POST':
        name = request.POST.get('name')
        type_uuid = request.POST.get('object_type')
        
        object_type = get_object_or_404(ObjectType, pk=type_uuid)
        
        ObjectModel.objects.create(
            name=name,
            object_type=object_type,
            specifications={} # Пустые характеристики по умолчанию
        )
        
        # ОТДАЕМ ОБНОВЛЕННЫЙ САЙДБАР ЦЕЛИКОМ С АКТИВНОЙ ВКЛАДКОЙ "МОДЕЛИ"
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
    
    # ==========================================
# ЭТАП 2: ДЕТАЛИ ОБЪЕКТА (ПРАВОЕ ОКНО)
# ==========================================

@login_required
def object_detail_view(request, pk):
    """Отображает правое окно объекта с вкладками"""
    obj = get_object_or_404(DataObject.objects.select_related('model', 'model__object_type'), pk=pk)
    
    parent_relation = Relation.objects.filter(subject=obj).select_related('main').first()
    parent = parent_relation.main if parent_relation else None
    
    context = {
        'obj': obj,
        'parent': parent,
        'active_tab': 'short_info',
    }
    
    response = render(request, 'data/object/object_details.html', context)
    
    # Если перешли по ссылке из модели — переключим левый сайдбар на "Объекты" на лету
    if request.GET.get('sidebar'):
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        from django.template.loader import render_to_string
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        
        content = response.content.decode('utf-8')
        combined_content = f'{content}\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        return HttpResponse(combined_content)
        
    return response


@login_required
def object_tab_view(request, pk, tab_name):
    """Возвращает содержимое конкретной вкладки объекта"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    context = {'obj': obj}
    
    if tab_name == 'short_info':
        # Находим превью-изображение
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        context['preview'] = preview
        template = 'data/object/object_tab_short_info.html'
        
    elif tab_name == 'specs':
        # Характеристики берутся из JSON модели
        context['specifications'] = obj.model.specifications or {}
        template = 'data/object/object_tab_specs.html'
        
    elif tab_name == 'comments':
        comments = obj.comments.select_related('user').order_by('-created_at')
        context['comments'] = comments
        template = 'data/object/object_tab_comments.html'
        
    elif tab_name == 'files':
        # Выводим все файлы, кроме тех, что помечены как превью (или вообще все, но превью отдельно)
        # Удобнее показать все файлы списком для скачивания
        files = obj.attachments.select_related('user').order_by('-created_at')
        context['files'] = files
        template = 'data/object/object_tab_files.html'
        
    elif tab_name == 'history':
        history = obj.actions.select_related('user').order_by('-created_at')
        context['history'] = history
        template = 'data/object/object_tab_history.html'
        
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)


# --- Inline-редактирование в мини-шапке и описании ---

@login_required
def edit_inventory_view(request, pk):
    """Возвращает поле ввода для изменения инвентарного номера"""
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        new_inv = request.POST.get('inventory_number', '').strip()
        obj.inventory_number = new_inv if new_inv else None
        obj.save()
        
        # Запишем изменение в лог
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Изменен инвентарный номер объекта на: {new_inv or 'отсутствует'}."
        )
        
        # Возвращаем обычное текстовое представление номера
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': True})


@login_required
def edit_parent_view(request, pk):
    """Возвращает выпадающий список для смены родительского объекта"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        parent_uuid = request.POST.get('parent_uuid')
        
        # Удаляем существующую родительскую связь
        Relation.objects.filter(subject=obj).delete()
        
        parent_name = "отсутствует"
        if parent_uuid:
            parent_obj = get_object_or_404(DataObject, pk=parent_uuid)
            parent_name = parent_obj.name or parent_obj.model.name
            dep_type, _ = DependencyType.objects.get_or_create(type="Входит в состав")
            Relation.objects.create(
                main=parent_obj,
                subject=obj,
                dependency_type=dep_type
            )
            
        # Логируем смену родителя
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Связь изменена: назначен новый родительский объект '{parent_name}'."
        )
        
        # Возвращаем текстовый вид и инициируем обновление левой панели проводника через OOB Swap
        # Это мгновенно перерисует дерево с правильной вложенностью
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render(request, 'data/tree/dict_sidebar.html', sidebar_context).content.decode('utf-8')
        
        parent_obj = DataObject.objects.filter(subject_relations__subject=obj).first()
        response_html = f"""
            <span id="parent-display-container" hx-get="{request.path}" hx-target="#parent-display-container" hx-swap="outerHTML" style="cursor: pointer;" class="text-primary fw-semibold">
                {parent_name} <i class="bi bi-pencil-square ms-1 small text-muted"></i>
            </span>
            <div id="sidebar-container" hx-swap-oob="innerHTML">
                {sidebar_html}
            </div>
        """
        return HttpResponse(response_html)

    # GET запрос: отдаем селектор
    # Исключаем самого себя из кандидатов в родители, чтобы избежать рекурсии
    candidates = DataObject.objects.exclude(pk=obj.pk).order_by('name')
    current_relation = Relation.objects.filter(subject=obj).first()
    current_parent_pk = current_relation.main.pk if current_relation else None
    
    return render(request, 'data/object/inline_parent.html', {
        'obj': obj,
        'candidates': candidates,
        'current_parent_pk': current_parent_pk
    })


@login_required
def edit_description_view(request, pk):
    """Редактирование описания объекта во вкладке 'Краткая информация'"""
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        desc = request.POST.get('description', '').strip()
        obj.description = desc if desc else None
        obj.save()
        
        # Логируем изменение описания
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action="Обновлено краткое описание объекта."
        )
        
        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': True})


# --- Вкладка: Комментарии (Комментарии CRUD + Массовое удаление) ---

@login_required
def add_comment_view(request, pk):
    """Добавление нового комментария к объекту"""
    obj = get_object_or_404(DataObject, pk=pk)
    text = request.POST.get('text', '').strip()
    if text:
        from .models import Comment
        Comment.objects.create(
            user=request.user,
            data_object=obj,
            text=text
        )
    # Возвращаем обновленный список комментариев для этой вкладки
    comments = obj.comments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_comments.html', {'obj': obj, 'comments': comments})


@login_required
def edit_comment_view(request, pk):
    """Поштучное редактирование комментария"""
    from .models import Comment
    comment = get_object_or_404(Comment, pk=pk)
    
    if request.method == 'POST':
        text = request.POST.get('text', '').strip()
        if text:
            comment.text = text
            comment.save()
        return render(request, 'data/object/comment_item.html', {'comment': comment})
        
    return render(request, 'data/object/comment_item_edit.html', {'comment': comment})


@login_required
def delete_comments_bulk(request):
    """Массовое и одиночное удаление комментариев"""
    from .models import Comment
    comment_ids = request.POST.getlist('comment_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    if comment_ids:
        Comment.objects.filter(uuid__in=comment_ids, data_object=obj).delete()
        
    comments = obj.comments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_comments.html', {'obj': obj, 'comments': comments})


# --- Вкладка: Файлы (Вложения CRUD + Загрузка Превью) ---

@login_required
def add_attachment_view(request, pk):
    """Добавление нового файла/превью изображения"""
    obj = get_object_or_404(DataObject, pk=pk)
    file = request.FILES.get('file')
    is_preview_upload = request.POST.get('is_preview') == 'true'
    
    if file:
        from .models import Attachment
        if is_preview_upload:
            # Снимаем флаг превью со всех старых вложений объекта
            Attachment.objects.filter(data_object=obj, is_preview=True).update(is_preview=False)
            
        Attachment.objects.create(
            user=request.user,
            data_object=obj,
            path=file,
            is_preview=is_preview_upload
        )
        
    # Если загружали превью, перерендерим Краткую информацию, иначе вкладку файлов
    if is_preview_upload:
        preview = Attachment.objects.filter(data_object=obj, is_preview=True).first()
        return render(request, 'data/object/object_tab_short_info.html', {'obj': obj, 'preview': preview})
        
    files = obj.attachments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_files.html', {'obj': obj, 'files': files})


@login_required
def delete_attachments_bulk(request):
    """Массовое удаление файлов"""
    from .models import Attachment
    file_ids = request.POST.getlist('file_ids')
    obj_pk = request.POST.get('object_uuid')
    obj = get_object_or_404(DataObject, pk=obj_pk)
    
    if file_ids:
        Attachment.objects.filter(uuid__in=file_ids, data_object=obj).delete()
        
    files = obj.attachments.select_related('user').order_by('-created_at')
    return render(request, 'data/object/object_tab_files.html', {'obj': obj, 'files': files})

# ==========================================
# ЭТАП 3: ДЕТАЛИ МОДЕЛИ И JSON-РЕДАКТОР
# ==========================================

@login_required
def model_detail_view(request, pk):
    """Отображает правое окно детализации модели"""
    model_obj = get_object_or_404(ObjectModel.objects.select_related('object_type'), pk=pk)
    
    context = {
        'model_obj': model_obj,
        'active_tab': 'specs',
    }
    
    response = render(request, 'data/model/model_details.html', context)
    
    # Если запрашиваем деталь модели с флагом sidebar=1 (при переходе из объекта)
    if request.GET.get('sidebar'):
        object_types = ObjectType.objects.prefetch_related(
            Prefetch('models', queryset=ObjectModel.objects.all().order_by('name'))
        ).order_by('type')
        sidebar_context = {
            'object_types_list': object_types,
            'active_tab': 'models', # Устанавливаем вкладку "Модели" активной
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        from django.template.loader import render_to_string
        sidebar_html = render_to_string('data/tree/dict_sidebar.html', sidebar_context, request=request)
        
        content = response.content.decode('utf-8')
        combined_content = f'{content}\n<div id="sidebar-container" hx-swap-oob="innerHTML">{sidebar_html}</div>'
        return HttpResponse(combined_content)
        
    return response


@login_required
def model_tab_view(request, pk, tab_name):
    """Возвращает контент вкладок модели"""
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    
    context = {'model_obj': model_obj}
    
    if tab_name == 'specs':
        context['specifications'] = model_obj.specifications or {}
        template = 'data/model/model_tab_specs.html'
        
    elif tab_name == 'objects':
        # Находим все объекты, созданные на основе этой модели
        objects = model_obj.data_objects.all().select_related('model__object_type').order_by('name')
        context['objects'] = objects
        template = 'data/model/model_tab_objects.html'
        
    else:
        return HttpResponse("Вкладка не найдена", status=404)
        
    return render(request, template, context)


# --- JSON Характеристики CRUD ---

@login_required
def model_spec_add_view(request, pk):
    """Добавление новой пары ключ-значение в характеристики модели"""
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    
    if request.method == 'POST':
        key = request.POST.get('key', '').strip()
        value = request.POST.get('value', '').strip()
        
        if key and value:
            # Получаем текущий словарь, мержим и сохраняем
            specs = model_obj.specifications or {}
            specs[key] = value
            model_obj.specifications = specs
            model_obj.save()
            
        # Возвращаем обновленный шаблон вкладки характеристик
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)


@login_required
def model_spec_edit_view(request, pk):
    """Inline-редактирование строки характеристики"""
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.GET.get('key') or request.POST.get('key')
    
    if request.method == 'POST':
        old_key = request.POST.get('old_key')
        new_key = request.POST.get('key', '').strip()
        value = request.POST.get('value', '').strip()
        
        specs = model_obj.specifications or {}
        
        if old_key and new_key and value:
            # Если ключ изменился, удаляем старый
            if old_key != new_key:
                specs.pop(old_key, None)
            specs[new_key] = value
            model_obj.specifications = specs
            model_obj.save()
            
        context = {
            'model_obj': model_obj,
            'specifications': model_obj.specifications
        }
        return render(request, 'data/model/model_tab_specs.html', context)
        
    # GET запрос: возвращаем строку таблицы в режиме редактирования
    value = model_obj.specifications.get(key, '')
    return render(request, 'data/model/inline_spec_row.html', {
        'model_obj': model_obj,
        'key': key,
        'value': value,
        'editing': True
    })


@login_required
def model_spec_delete_view(request, pk):
    """Удаление ключа из характеристик"""
    model_obj = get_object_or_404(ObjectModel, pk=pk)
    key = request.POST.get('key')
    
    specs = model_obj.specifications or {}
    if key in specs:
        specs.pop(key)
        model_obj.specifications = specs
        model_obj.save()
        
    context = {
        'model_obj': model_obj,
        'specifications': model_obj.specifications
    }
    return render(request, 'data/model/model_tab_specs.html', context)
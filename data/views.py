from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.db.models import Q, Prefetch
from django.utils import timezone
from datetime import timedelta, datetime
from django.views.decorators.http import require_GET, require_POST
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
    """Быстрое создание объекта / Загрузка чистой формы"""
    if request.method == 'POST':
        name = request.POST.get('name')
        model_uuid = request.POST.get('model')
        inventory_number = request.POST.get('inventory_number')
        parent_uuid = request.POST.get('parent')
        maintenance_str = request.POST.get('next_maintenance_date')
        
        dep_type_uuid = request.POST.get('dependency_type')
        new_dep_type_name = request.POST.get('new_dependency_type')
        
        model_obj = get_object_or_404(ObjectModel, pk=model_uuid)
        
        next_maintenance_date = None
        if maintenance_str:
            try:
                next_maintenance_date = timezone.make_aware(
                    datetime.strptime(maintenance_str, '%Y-%m-%d')
                )
            except ValueError:
                pass
                
        new_obj = DataObject.objects.create(
            name=name,
            model=model_obj,
            inventory_number=inventory_number if inventory_number else None,
            next_maintenance_date=next_maintenance_date,
            user=request.user
        )
        
        if parent_uuid:
            parent_obj = get_object_or_404(DataObject, pk=parent_uuid)
            
            if new_dep_type_name:
                dep_type, _ = DependencyType.objects.get_or_create(type=new_dep_type_name)
            elif dep_type_uuid:
                dep_type = get_object_or_404(DependencyType, pk=dep_type_uuid)
            else:
                dep_type, _ = DependencyType.objects.get_or_create(type="Входит в состав")
                
            Relation.objects.create(
                main=parent_obj,
                subject=new_obj,
                dependency_type=dep_type
            )
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=new_obj,
            action="Объект зарегистрирован в системе через форму быстрого добавления."
        )
        
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    # ЕСЛИ GET-ЗАПРОС: Возвращаем чистый бланк формы
    return render(request, 'data/includes/create_object_modal_body.html')

@login_required
def create_model_view(request):
    """Быстрое создание модели / Загрузка чистой формы"""
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
            object_type = get_object_or_404(ObjectType, pk=type_uuid)
            
        ObjectModel.objects.create(
            name=name,
            object_type=object_type,
            specifications=specifications
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

    # ЕСЛИ GET-ЗАПРОС: Возвращаем чистый бланк формы
    return render(request, 'data/includes/create_model_modal_body.html')
    
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
    """Редактирование инвентарного номера объекта"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        new_inv = request.POST.get('inventory_number', '').strip()
        obj.inventory_number = new_inv if new_inv else None
        obj.save()
        
        # Логируем изменение инвентарного номера
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Изменен инвентарный номер объекта на: {new_inv or 'отсутствует'}."
        )
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    # GET-запрос: если нажата кнопка отмены, возвращаем обычный режим просмотра
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': True})


@login_required
def edit_parent_view(request, pk):
    """Выбор родительского объекта с использованием умных подсказок поиска"""
    obj = get_object_or_404(DataObject, pk=pk)
    
    # Легкий обработчик отмены редактирования (возвращает только текстовый вид)
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        current_relation = Relation.objects.filter(subject=obj).select_related('main__model').first()
        parent = current_relation.main if current_relation else None
        parent_name = parent.name or parent.model.name if parent else "отсутствует"
        
        return HttpResponse(
            f'<div id="parent-display-container" hx-get="{request.path}" hx-target="#parent-display-container" hx-swap="outerHTML" style="cursor: pointer;" class="text-primary fw-semibold d-inline-block animate-fade">'
            f'{parent_name if parent else "<span class=\'text-muted italic small\'>указать родителя <i class=\'bi bi-pencil-square ms-1\'></i></span>"}'
            f'{f" <i class=\'bi bi-pencil-square ms-1 text-muted small\'></i>" if parent else ""}'
            f'</div>'
        )
        
    if request.method == 'POST':
        parent_uuid = request.POST.get('parent') or request.POST.get('parent_uuid')
        
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
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action=f"Связь изменена: назначен новый родительский объект '{parent_name}'."
        )
        
        roots = DataObject.objects.exclude(main_relations__isnull=False).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render(request, 'data/tree/dict_sidebar.html', sidebar_context).content.decode('utf-8')
        
        response_html = f"""
            <div id="parent-display-container" hx-get="{request.path}" hx-target="#parent-display-container" hx-swap="outerHTML" style="cursor: pointer;" class="text-primary fw-semibold d-inline-block animate-fade">
                {parent_name} <i class="bi bi-pencil-square ms-1 text-muted small"></i>
            </div>
            <div id="sidebar-container" hx-swap-oob="innerHTML">
                {sidebar_html}
            </div>
        """
        return HttpResponse(response_html)

    current_relation = Relation.objects.filter(subject=obj).select_related('main__model').first()
    current_parent = current_relation.main if current_relation else None
    
    return render(request, 'data/object/inline_parent.html', {
        'obj': obj,
        'current_parent': current_parent
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

@login_required
def check_model_name_view(request):
    """Умная проверка имени модели на точные совпадения и схожесть"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    # 1. Сначала жесткая проверка на точное совпадение
    exact_match = ObjectModel.objects.filter(name__iexact=name).first()
    if exact_match:
        return HttpResponse(
            f'<div class="alert alert-danger py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold mb-1"><i class="bi bi-x-circle-fill me-1"></i> Модель с таким названием уже существует!</div>'
            f'<a href="#" class="text-danger fw-bold" '
            f'hx-get="/dict/models/{exact_match.uuid}/?sidebar=1" '
            f'hx-target="#detail-container" '
            f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createModelModal\')).hide();">'
            f'Перейти к существующей модели: {exact_match.name} ({exact_match.object_type.type})'
            f'</a>'
            f'</div>'
        )

    # 2. Умный поиск похожих названий моделей (исключая предлоги и короткие слова)
    stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по', 'of', 'and', 'the'}
    words = [
        w.lower() for w in name.split() 
        if len(w) >= 2 and w.lower() not in stop_words
    ]

    similar_models = []
    if words:
        # Ищем совпадения по словам в названии модели или в типе оборудования
        query = Q()
        for word in words:
            query |= Q(name__icontains=word) | Q(object_type__type__icontains=word)
            
        similar_models = ObjectModel.objects.filter(query).select_related('object_type').distinct()[:5]

    if similar_models:
        links = []
        for model in similar_models:
            links.append(
                f'<li class="mb-1">'
                f'<a href="#" class="alert-link text-primary fw-semibold" '
                f'hx-get="/dict/models/{model.uuid}/?sidebar=1" '
                f'hx-target="#detail-container" '
                f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createModelModal\')).hide();">'
                f'{model.name} <span class="text-muted fw-normal">({model.object_type.type})</span>'
                f'</a>'
                f'</li>'
            )

        return HttpResponse(
            f'<div class="alert alert-warning py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold text-dark mb-1">'
            f'<i class="bi bi-exclamation-triangle-fill text-warning me-1"></i> '
            f'Обнаружены похожие модели ({len(similar_models)} шт.):'
            f'</div>'
            f'<ul class="ps-3 mb-1" style="max-height: 80px; overflow-y: auto;">'  # Добавлен скролл
            f'{"".join(links)}'
            f'</ul>'
            f'<div class="text-muted" style="font-size: 0.75rem;">'
            f'Возможно, нужная модель уже заведена. Кликните для быстрого перехода.'
            f'</div>'
            f'</div>'
        )

    # 3. Если всё свободно
    return HttpResponse(
        '<div class="text-success small mt-1">'
        '<i class="bi bi-check-circle-fill me-1"></i>'
        'Название модели свободно и уникально'
        '</div>'
    )


@login_required
def check_object_name_view(request):
    """Умная проверка имени объекта на точные совпадения и схожесть"""
    name = request.GET.get('name', '').strip()
    if not name or len(name) < 2:
        return HttpResponse('')

    # 1. Сначала жесткая проверка на точное совпадение
    exact_match = DataObject.objects.filter(name__iexact=name).first()
    if exact_match:
        exact_name = exact_match.name or exact_match.model.name
        return HttpResponse(
            f'<div class="alert alert-danger py-2 px-3 mt-2 mb-0 rounded-3 small">'
            f'<div class="fw-bold mb-1"><i class="bi bi-x-circle-fill me-1"></i> Объект с таким именем уже существует!</div>'
            f'<a href="#" class="text-danger fw-bold" '
            f'hx-get="/dict/objects/{exact_match.uuid}/?sidebar=1" '
            f'hx-target="#detail-container" '
            f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createObjectModal\')).hide();">'
            f'Перейти к существующему объекту: {exact_name} ({exact_match.model.name})'
            f'</a>'
            f'</div>'
        )

    # 2. Умный поиск похожих названий (исключая предлоги и короткие слова)
    stop_words = {'в', 'на', 'под', 'над', 'для', 'из', 'со', 'и', 'или', 'а', 'но', 'с', 'по'}
    words = [
        w.lower() for w in name.split() 
        if len(w) >= 3 and w.lower() not in stop_words
    ]

    similar_objects = []
    if words:
        # Строим Q-запрос для поиска вхождений любых из значимых слов в имя объекта или имя его модели
        query = Q()
        for word in words:
            query |= Q(name__icontains=word) | Q(model__name__icontains=word)
            
        similar_objects = DataObject.objects.filter(query).select_related('model').distinct()[:5]

    if similar_objects:
        links = []
        for obj in similar_objects:
            obj_name = obj.name or obj.model.name
            # Каждая ссылка при клике загружает детали в правое окно и закрывает модальное окно создания
            links.append(
                f'<li class="mb-1">'
                f'<a href="#" class="alert-link text-primary fw-semibold" '
                f'hx-get="/dict/objects/{obj.uuid}/?sidebar=1" '
                f'hx-target="#detail-container" '
                f'hx-on:click="bootstrap.Modal.getInstance(document.getElementById(\'createObjectModal\')).hide();">'
                f'{obj_name} <span class="text-muted fw-normal">({obj.model.name})</span>'
                f'</a>'
                f'</li>'
            )

        return HttpResponse(
            f'<div class="alert alert-warning py-2 px-3 mt-2 mb-0 rounded-3 small animate-fade">'
            f'<div class="fw-bold text-dark mb-1">'
            f'<i class="bi bi-exclamation-triangle-fill text-warning me-1"></i> '
            f'Обнаружены похожие объекты ({len(similar_objects)} шт.):'
            f'</div>'
            f'<ul class="ps-3 mb-1" style="max-height: 80px; overflow-y: auto;">'  # Добавлен скролл
            f'{"".join(links)}'
            f'</ul>'
            f'<div class="text-muted" style="font-size: 0.75rem;">'
            f'Возможно, нужный объект уже зарегистрирован. Кликните на него для перехода.'
            f'</div>'
            f'</div>'
        )

    # 3. Если всё чисто
    return HttpResponse(
        '<div class="text-success small mt-1">'
        '<i class="bi bi-check-circle-fill me-1"></i>'
        'Имя свободно и уникально'
        '</div>'
    )


@login_required
def suggest_view(request):
    """Единый эндпоинт для умных подсказок (с сужением результатов через AND)"""
    field = request.GET.get('field')
    q = request.GET.get('q', '').strip()
    
    if not q or len(q) < 1:
        return HttpResponse('')
        
    results = []
    words = q.split()
    
    if field == 'object_type':
        exact = ObjectType.objects.filter(type__iexact=q)
        # Сужение поиска через оператор &= (И)
        word_filter = Q()
        for w in words:
            word_filter &= Q(type__icontains=w)
        partial = ObjectType.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'model':
        exact = ObjectModel.objects.filter(name__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= (Q(name__icontains=w) | Q(object_type__type__icontains=w))
        partial = ObjectModel.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'parent':
        exact = DataObject.objects.filter(Q(name__iexact=q) | Q(inventory_number__iexact=q))
        word_filter = Q()
        for w in words:
            word_filter &= (Q(name__icontains=w) | Q(inventory_number__icontains=w) | Q(model__name__icontains=w))
        partial = DataObject.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)
        
    elif field == 'dependency_type':
        exact = DependencyType.objects.filter(type__iexact=q)
        word_filter = Q()
        for w in words:
            word_filter &= Q(type__icontains=w)
        partial = DependencyType.objects.filter(word_filter).exclude(pk__in=exact)
        results = list(exact) + list(partial)

    show_create_option = False
    if field in ['object_type', 'dependency_type']:
        has_exact_match = any(
            (getattr(item, 'type', '').lower() == q.lower()) for item in results
        )
        if not has_exact_match:
            show_create_option = True

    return render(request, 'data/includes/suggestions_list.html', {
        'results': results[:10],
        'field': field,
        'q': q,
        'show_create_option': show_create_option
    })


@login_required
def select_suggestion_view(request):
    """Рендеринг состояния выбранного элемента (Badge с кнопкой Сбросить)"""
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
        elif field == 'parent':
            parent_obj = get_object_or_404(DataObject, pk=uuid_val)
            display_name = parent_obj.name or parent_obj.model.name
        elif field == 'dependency_type':
            display_name = get_object_or_404(DependencyType, pk=uuid_val).type
    elif name_val:
        # Режим создания нового типа inline
        display_name = f"{name_val} (Создать новый)"
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
    """Сброс выбранного значения обратно к строке поиска"""
    field = request.GET.get('field')
    placeholders = {
        'object_type': 'Введите тип оборудования...',
        'model': 'Введите модель оборудования...',
        'parent': 'Поиск родительского объекта...',
        'dependency_type': 'Введите тип связи...'
    }
    return render(request, 'data/includes/suggestion_input.html', {
        'field': field,
        'placeholder': placeholders.get(field, 'Начните вводить...')
    })


@login_required
def specs_builder_view(request):
    """Обработка добавления и удаления временных спецификаций модели на стороне HTML"""
    # Собираем текущие списки ключей и значений из скрытых полей
    keys = request.POST.getlist('spec_keys')
    values = request.POST.getlist('spec_values')
    
    # Объединяем их в один словарь
    specs = dict(zip(keys, values))
    
    # Обработка добавления нового элемента
    new_key = request.POST.get('new_key', '').strip()
    new_value = request.POST.get('new_value', '').strip()
    if new_key and new_value:
        specs[new_key] = new_value
        
    # Обработка удаления
    remove_key = request.POST.get('remove_key')
    if remove_key:
        specs.pop(remove_key, None)
        
    return render(request, 'data/includes/specs_builder.html', {
        'specifications': specs
    })
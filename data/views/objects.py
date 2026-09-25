"""Объекты справочника: создание, удаление и инлайн-правка полей."""

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST, require_http_methods

from ..forms import parse_maintenance_date
from ..models import ActionHistory, DataObject, DateUpdateRule, ObjectModel, ObjectType, YouTrackJob
from users.decorators import role_required

from ..services import youtrack_queue
from ..services.maintenance import calculate_next_maintenance_date
from .common import htmx_error, sync_pill_oob

logger = logging.getLogger('data')


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_http_methods(["GET", "POST", "DELETE"])
def delete_object_view(request, pk):
    """
    GET  — модальное окно подтверждения с точным объёмом каскада
           (потомки, комментарии, вложения, история).
    POST — удаление поддерева. Удаление непустого узла требует явного
           подтверждения флагом confirm_subtree.
    """
    obj = get_object_or_404(DataObject.objects.select_related('model'), pk=pk)
    stats = obj.get_subtree_stats()

    if request.method == 'GET':
        return render(request, 'data/includes/confirm_delete_object.html', {
            'obj': obj,
            'stats': stats,
        })

    if request.method not in ['POST', 'DELETE']:
        return HttpResponse("Метод не разрешен", status=405)

    if stats['descendants'] and request.POST.get('confirm_subtree') != 'yes':
        return htmx_error(
            'Объект содержит дочерние компоненты: подтвердите удаление всего поддерева.',
            retarget='#confirm-modal-content .form-feedback'
        )

    was_active = request.session.get('active_object_id') == str(obj.pk)
    obj_uuid = obj.uuid
    obj.delete()
    if was_active:
        request.session['active_object_id'] = None
        request.session.modified = True

    # Узел убираем из дерева OOB-свопом: он может отсутствовать в DOM
    # (свёрнутая ветка, режим проводника) — тогда инструкция просто игнорируется.
    parts = [format_html('<li id="node-{}" hx-swap-oob="delete"></li>', obj_uuid)]
    if was_active:
        parts.append(render_to_string('data/includes/detail_placeholder.html', {'oob': True}, request=request))
    response = HttpResponse("\n".join(parts), status=200)
    response['HX-Trigger'] = 'objectDeleted'
    return response


@login_required
@role_required(['senior', 'admin', 'superuser'])
def create_object_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        model_uuid = request.POST.get('model')
        inventory_number = request.POST.get('inventory_number')
        youtrack_issue_id = request.POST.get('youtrack_issue_id', '').strip()
        parent_uuid = request.POST.get('parent')
        maintenance_str = request.POST.get('next_maintenance_date')
        rule_uuid = request.POST.get('date_update_rule')
        
        model_obj = ObjectModel.objects.filter(pk=model_uuid).first() if model_uuid else None
        if model_obj is None:
            return htmx_error(
                'Не выбрана модель оборудования. Выберите её из списка подсказок.',
                retarget='#createObjectModal .form-feedback'
            )
        
        try:
            next_maintenance_date = parse_maintenance_date(maintenance_str, "Дата следующего ТО")
        except ValidationError as exc:
            return htmx_error(" ".join(exc.messages),
                              retarget='#createObjectModal .form-feedback')

        selected_rule = get_object_or_404(DateUpdateRule, pk=rule_uuid) if rule_uuid else None
        parent_obj = get_object_or_404(DataObject, pk=parent_uuid) if parent_uuid else None
                
        new_obj = DataObject.objects.create(
            name=name,
            model=model_obj,
            parent=parent_obj,
            inventory_number=inventory_number if inventory_number else None,
            youtrack_issue_id=youtrack_issue_id if youtrack_issue_id else None,
            next_maintenance_date=next_maintenance_date,
            date_update_rule=selected_rule
        )

        scheduling_mode = request.POST.get('maintenance_scheduling_mode', 'manual')
        if scheduling_mode == 'auto' and selected_rule:
            first_date = calculate_next_maintenance_date(
                new_obj, base_date=timezone.localdate(), use_anchor=False
            )
            new_obj.next_maintenance_date = first_date
            new_obj.save(update_fields=['next_maintenance_date'])
            
        if parent_obj:
            parent_name = parent_obj.name or parent_obj.model.name
            action_desc = f"Объект зарегистрирован в системе в составе родительского объекта '{parent_name}'."
        else:
            action_desc = "Объект зарегистрирован в системе как корневой объект."

        ActionHistory.objects.create(
            user=request.user,
            data_object=new_obj,
            action_type='create',
            action=action_desc
        )
        
        roots = DataObject.objects.filter(parent__isnull=True).order_by('name')
        context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        return render(request, 'data/tree/dict_sidebar.html', context)

    return render(request, 'data/includes/create_object_modal_body.html')


@login_required
@role_required(['senior', 'admin', 'superuser'])
@require_POST
def unlink_rule_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    obj.date_update_rule = None
    obj.save(update_fields=['date_update_rule'])
    
    ActionHistory.objects.create(
        user=request.user,
        data_object=obj,
        action_type='rule_change',
        action="Правило автоматического расчета ТО отвязано от объекта."
    )
    
    return render(request, 'data/object/edit_rule_modal_body.html', {
        'obj': obj,
        'current_mode': 'auto',
        'rule_details': None
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_inventory_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    if request.method == 'POST':
        new_inv = request.POST.get('inventory_number', '').strip()
        obj.inventory_number = new_inv if new_inv else None
        obj.save(update_fields=['inventory_number'])
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='update',
            action=f"Изменен инвентарный номер объекта на: {new_inv or 'отсутствует'}."
        )
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_inventory.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_youtrack_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'POST':
        new_yt = request.POST.get('youtrack_issue_id', '').strip()
        obj.youtrack_issue_id = new_yt if new_yt else None
        obj.save(update_fields=['youtrack_issue_id'])
        
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='update',
            action=f"Изменен ID задачи Youtrack на: {new_yt or 'отсутствует'}."
        )

        # Привязали задачу — забрать из неё данные должен воркер: держать
        # запрос ради обращения к YouTrack незачем, пилюля в шапке карточки
        # сама покажет, чем всё кончилось.
        queued = False
        if obj.youtrack_issue_id:
            youtrack_queue.enqueue(YouTrackJob.KIND_SYNC, obj, request.user)
            queued = True

        html = render_to_string('data/object/inline_youtrack.html', {
            'obj': obj,
            'editing': False,
            'sync_queued': queued,
        }, request=request)
        if queued:
            # Пилюля возвращается к ожиданию и снова начинает опрос.
            html += "\n" + render_to_string('data/object/sync_status.html',
                                            {'obj': obj, 'sync_state': 'pending', 'poll': 1},
                                            request=request)
        return HttpResponse(html)
        
    if request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_youtrack.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_youtrack.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_parent_view(request, pk):
    """Смена родительского объекта напрямую через ForeignKey parent"""
    obj = get_object_or_404(DataObject.objects.select_related('parent', 'parent__model'), pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_parent.html', {
            'obj': obj, 
            'current_parent': obj.parent,
            'editing': False
        })
        
    if request.method == 'POST':
        parent_uuid = request.POST.get('parent') or request.POST.get('parent_uuid')
        
        if parent_uuid:
            new_parent = get_object_or_404(DataObject, pk=parent_uuid)
            # Защита от циклов: нельзя назначить родителем себя или своего потомка
            try:
                obj.validate_parent(new_parent)
            except ValidationError as exc:
                return render(request, 'data/object/inline_parent.html', {
                    'obj': obj,
                    'current_parent': obj.parent,
                    'editing': False,
                    'error': " ".join(exc.messages),
                }, status=400)
            obj.parent = new_parent
            parent_name = new_parent.name or new_parent.model.name
        else:
            obj.parent = None
            parent_name = "отсутствует"
            
        obj.save(update_fields=['parent'])
            
        ActionHistory.objects.create(
            user=request.user,
            data_object=obj,
            action_type='link_change',
            action=f"Назначен новый родительский объект: '{parent_name}'."
        )
        
        roots = DataObject.objects.filter(parent__isnull=True).order_by('name')
        sidebar_context = {
            'initial_objects': roots,
            'active_tab': 'objects',
            'models': ObjectModel.objects.all().order_by('name'),
            'object_types': ObjectType.objects.all().order_by('type')
        }
        sidebar_html = render(request, 'data/tree/dict_sidebar.html', sidebar_context).content.decode('utf-8')
        parent_html = render_to_string('data/object/inline_parent.html', {
            'obj': obj, 
            'current_parent': obj.parent, 
            'editing': False
        }, request=request)
            
        response_html = f"""
            {parent_html}
            <div id="sidebar-container" hx-swap-oob="innerHTML">
                {sidebar_html}
            </div>
        """
        return HttpResponse(response_html)

    return render(request, 'data/object/inline_parent.html', {
        'obj': obj,
        'current_parent': obj.parent,
        'editing': True
    })


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_name_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_name.html', {'obj': obj, 'editing': False})
        
    if request.method == 'POST':
        old_name = obj.name or obj.model.name
        new_name = request.POST.get('name', '').strip()
        
        if new_name and old_name != new_name:
            obj.name = new_name
            obj.save(update_fields=['name'])
        
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action_type='update',
                action=f"Имя объекта изменено с '{old_name}' на '{new_name}'."
            )
            
        name_html = render_to_string('data/object/inline_name.html', {'obj': obj, 'editing': False}, request=request)
        sidebar_node_html = render_to_string('data/tree/object_tree_node_label.html', {
            'node': obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(name_html + "\n" + sidebar_node_html)

    return render(request, 'data/object/inline_name.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_description_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)

    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})

    if request.method == 'POST':
        old_desc = (obj.description or '').strip()
        new_desc = request.POST.get('description', '').strip()
        
        if old_desc != new_desc:
            obj.description = new_desc if new_desc else None
            obj.save(update_fields=['description'])
            
            ActionHistory.objects.create(
                user=request.user,
                data_object=obj,
                action_type='update',
                action="Обновлено описание объекта."
            )

            # Описание уезжает в YouTrack из очереди. Результат покажет
            # пилюля в шапке карточки — ответ на правку поля её и запустит.
            if obj.youtrack_issue_id:
                youtrack_queue.enqueue(YouTrackJob.KIND_DESCRIPTION, obj, request.user)
                html = render_to_string('data/object/inline_description.html',
                                        {'obj': obj, 'editing': False}, request=request)
                return HttpResponse(html + "\n" + sync_pill_oob(obj, request))

        return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': False})
        
    return render(request, 'data/object/inline_description.html', {'obj': obj, 'editing': True})


@login_required
@role_required(['senior', 'admin', 'superuser'])
def edit_object_model_view(request, pk):
    obj = get_object_or_404(DataObject, pk=pk)
    
    if request.method == 'GET' and request.GET.get('cancel') == '1':
        return render(request, 'data/object/inline_model.html', {'obj': obj, 'editing': False})
        
    if request.method == 'POST':
        model_uuid = request.POST.get('model')
        
        if model_uuid:
            new_model = get_object_or_404(ObjectModel, pk=model_uuid)
            old_model = obj.model
            
            if old_model != new_model:
                obj.model = new_model
                obj.save(update_fields=['model'])
                
                ActionHistory.objects.create(
                    user=request.user,
                    data_object=obj,
                    action_type='update',
                    action=f"Модель оборудования изменена с '{old_model.name}' на '{new_model.name}'."
                )
                
        model_html = render_to_string('data/object/inline_model.html', {'obj': obj, 'editing': False}, request=request)
        sidebar_node_html = render_to_string('data/tree/object_tree_node_label.html', {
            'node': obj,
            'is_active': True,
            'oob': True
        }, request=request)
        
        return HttpResponse(model_html + "\n" + sidebar_node_html)
        
    return render(request, 'data/object/inline_model.html', {'obj': obj, 'editing': True})

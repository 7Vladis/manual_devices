"""Глубокое копирование объекта с потомками."""

from data.models import ActionHistory, DataObject


def deep_clone_object(source_obj, new_root_name, new_parent=None, source_root_name=None, user=None,
                      clone_children=True, _visited=None, _depth=0):
    """
    Рекурсивно клонирует объект и всю его дочернюю иерархию с умным суффиксированием:
    - Если в названии детали было имя старого родителя -> подменяем на новое имя.
    - Если название детали общее (например, "Блок питания") -> добавляем суффикс "(НовоеИмя)".

    Защищено от циклов в дереве (visited) и от чрезмерной глубины. Атомарность
    всей операции обеспечивает вызывающий код (clone_object_view).
    """
    if _visited is None:
        _visited = set()
    if source_obj.uuid in _visited or _depth > DataObject.MAX_TREE_DEPTH:
        return None
    _visited.add(source_obj.uuid)

    is_root = (source_root_name is None)
    if is_root:
        source_root_name = source_obj.name or (source_obj.model.name if source_obj.model else "")
    
    # 1. Формируем имя для текущего узла
    if is_root:
        # Это сам корневой объект
        obj_name = new_root_name
    else:
        # Это дочерняя деталь
        orig_name = source_obj.name or (source_obj.model.name if source_obj.model else "Компонент")
        if source_root_name and source_root_name in orig_name:
            # Заменяем старое имя родителя на новое
            obj_name = orig_name.replace(source_root_name, new_root_name)
        else:
            # Если имя общее — приписываем суффикс нового родителя
            obj_name = f"{orig_name} ({new_root_name})"

    # 2. Создаем копию объекта
    cloned_obj = DataObject.objects.create(
        name=obj_name,
        model=source_obj.model,
        parent=new_parent,
        inventory_number=None,       # Очищаем инвентарник
        youtrack_issue_id=None,      # Очищаем задачу YouTrack
        next_maintenance_date=source_obj.next_maintenance_date,
        date_update_rule=source_obj.date_update_rule,
        description=source_obj.description
    )
    
    # 3. Фиксируем создание в истории объекта.
    # Факт копирования намеренно не упоминаем: в истории объекта важно, что
    # он появился в системе, а не каким способом его завели.
    if new_parent:
        parent_name = new_parent.name or new_parent.model.name
        action_desc = f"Объект зарегистрирован в системе в составе родительского объекта '{parent_name}'."
    else:
        action_desc = "Объект зарегистрирован в системе как корневой объект."

    ActionHistory.objects.create(
        user=user,
        data_object=cloned_obj,
        action_type='create',
        action=action_desc
    )
    
    # 4. Рекурсивно клонируем всех потомков
    if clone_children:
        for child in source_obj.children.all().order_by('name'):
            deep_clone_object(
                source_obj=child,
                new_root_name=new_root_name,
                new_parent=cloned_obj,
                source_root_name=source_root_name,
                user=user,
                clone_children=True,
                _visited=_visited,
                _depth=_depth + 1,
            )
            
    return cloned_obj

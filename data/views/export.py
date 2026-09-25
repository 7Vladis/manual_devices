"""Выгрузка справочника в XLSX."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils import timezone

from ..models import DataObject
from users.decorators import role_required


@login_required
@role_required(['admin', 'superuser'])
def export_xlsx_view(request):
    """Генерация XLSX файла с прямой иерархией через parent"""
    import openpyxl

    export_mode = request.GET.get('export_mode', 'all')
    
    queryset = DataObject.objects.select_related('model', 'model__object_type', 'parent', 'parent__model').all()
    if export_mode == 'with_inventory':
        queryset = queryset.filter(inventory_number__isnull=False).exclude(inventory_number='')
    
    queryset = queryset.order_by('inventory_number', 'name')

    # Excel/LibreOffice трактуют значение, начинающееся с =, +, -, @ (а также
    # с управляющих символов табуляции и возврата каретки) как формулу.
    # Данные приходят от пользователей и из YouTrack, поэтому обезвреживаем их.
    FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

    def escape_formula(value):
        text = "" if value is None else str(value)
        if text.startswith(FORMULA_PREFIXES):
            return "'" + text
        return text

    def get_object_hierarchy_path(obj):
        """Сборка пути от корня до текущего объекта через цепочку parent"""
        path_segments = []
        current = obj
        visited = set()
        
        while current and current.uuid not in visited:
            visited.add(current.uuid)
            name = current.name or (current.model.name if current.model else "Без имени")
            path_segments.append(name)
            current = current.parent
            
        path_segments.reverse()
        return " → ".join(path_segments)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Оборудование"

    headers = [
        "Инвентарный номер",
        "Название объекта",
        "Иерархический путь (от корня)",
        "Описание объекта",
        "Название модели",
        "Характеристики спецификации"
    ]
    ws.append(headers)

    for col_num in range(1, 7):
        cell = ws.cell(row=1, column=col_num)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    for obj in queryset:
        inv_num = obj.inventory_number or ""
        obj_name = obj.name or ""
        hierarchy_path = get_object_hierarchy_path(obj)
        description = obj.description or ""
        model_name = obj.model.name if obj.model else ""
        
        specs = obj.model.specifications if obj.model and obj.model.specifications else {}
        specs_str_list = []
        if isinstance(specs, dict):
            for k, v in specs.items():
                specs_str_list.append(f"{k}: {v}")
        specs_formatted = "; ".join(specs_str_list)

        row = [inv_num, obj_name, hierarchy_path, description, model_name, specs_formatted]
        ws.append([escape_formula(value) for value in row])

        # Явно фиксируем текстовый тип, чтобы Excel не переинтерпретировал строку
        for cell in ws[ws.max_row]:
            cell.data_type = 's'

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 15), 50)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"manual_devices_export_{timezone.localdate().strftime('%Y%m%d')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    wb.save(response)
    return response

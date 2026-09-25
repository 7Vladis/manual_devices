"""
Представления справочника.

Пакет собран из бывшего data/views.py. Здесь только реэкспорт: urls.py
и тесты продолжают обращаться к data.views.<имя>, не зная о разбиении.
"""

from .common import (  # noqa: F401
    htmx_error,
    form_errors_text,
    yt_toast,
    get_comments_for,
    render_maintenance_pill,
    get_ancestors_chain,
)
from .settings_views import (  # noqa: F401
    settings_page,
    create_object_type_view,
    delete_object_type_view,
    create_rule_settings_view,
    edit_rule_settings_view,
    delete_rule_view,
)
from .export import (  # noqa: F401
    export_xlsx_view,
)
from .plan import (  # noqa: F401
    get_period_limits,
    group_by_urgency,
    plan_counters,
    dashboard,
    maintenance_list,
)
from .dictionary import (  # noqa: F401
    search_view,
    dict_view,
    toggle_explorer_mode_view,
    explorer_navigate_view,
    explorer_up_view,
    object_children_view,
)
from .servicing import (  # noqa: F401
    service_object_view,
    sync_youtrack_view,
    sync_status_view,
    retry_youtrack_view,
)
from .objects import (  # noqa: F401
    delete_object_view,
    create_object_view,
    unlink_rule_view,
    edit_inventory_view,
    edit_youtrack_view,
    edit_parent_view,
    edit_name_view,
    edit_description_view,
    edit_object_model_view,
)
from .object_detail import (  # noqa: F401
    object_detail_view,
    render_object_detail,
    object_tab_view,
)
from .comments import (  # noqa: F401
    add_comment_view,
    edit_comment_view,
    delete_comments_bulk,
    add_attachment_view,
    delete_attachments_bulk,
    set_preview_attachment_view,
)
from .object_models import (  # noqa: F401
    delete_model_view,
    create_model_view,
    model_detail_view,
    model_tab_view,
    model_spec_add_view,
    model_spec_edit_view,
    model_spec_delete_view,
    edit_model_name_view,
)
from .suggestions import (  # noqa: F401
    check_model_name_view,
    check_object_name_view,
    suggest_view,
    select_suggestion_view,
    reset_suggestion_view,
    specs_builder_view,
)
from .rules import (  # noqa: F401
    rules_dates_builder_view,
    rule_constructor_view,
    toggle_scheduling_mode_view,
    edit_rule_view,
)
from .clone import (  # noqa: F401
    clone_object_modal_view,
    clone_object_view,
)

# Доменные функции остаются доступными под прежними именами:
# на них опираются тесты и вызовы из соседних модулей.
from ..services.maintenance import calculate_next_maintenance_date  # noqa: F401
from ..services.cloning import deep_clone_object  # noqa: F401

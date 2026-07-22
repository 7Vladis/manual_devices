from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from functools import wraps

def role_required(allowed_roles=[]):
    """
    Декоратор для ограничения доступа к представлениям на основе ролей.
    Использование: @role_required(['senior', 'admin', 'superuser'])
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            # 1. Проверяем, авторизован ли пользователь
            if not request.user.is_authenticated:
                return redirect('login')  # Перенаправляем на твою страницу входа

            # 2. Если пользователь встроенный суперюзер Django — разрешаем доступ ко всему
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            # 3. Проверяем, входит ли роль пользователя в список разрешенных
            if request.user.role in allowed_roles:
                return view_func(request, *args, **kwargs)

            # 4. Если роль не подходит, обрабатываем запрет доступа:
            
            # Вариант А: Запрос пришел через HTMX (например, клик по кнопке внутри страницы)
            if request.headers.get('HX-Request'):
                # Возвращаем аккуратную плашку с предупреждением, которая встроится в интерфейс
                # без разрушения структуры страницы
                return HttpResponseForbidden(
                    '<div class="alert alert-danger m-2" role="alert">'
                    'У вас нет прав доступа для выполнения этой операции.'
                    '</div>'
                )

            # Вариант Б: Обычный переход по ссылке (полная перезагрузка страницы)
            return HttpResponseForbidden("Доступ запрещен. У вашей учетной записи недостаточно прав.")

        return _wrapped_view
    return decorator
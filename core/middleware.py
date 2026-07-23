from django.contrib.auth import logout
from django.shortcuts import redirect

class ValidateUserActiveMiddleware:
    """
    Middleware проверяет, существует ли аутентифицированный пользователь в базе данных
    и активен ли он (is_active=True). Если пользователь был удален или заблокирован,
    его сеанс мгновенно завершается и происходит перенаправление на страницу входа.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            # Проверяем наличие пользователя в базе и флаг is_active
            User = request.user.__class__
            exists_and_active = User.objects.filter(pk=request.user.pk, is_active=True).exists()
            if not exists_and_active:
                logout(request)
                return redirect('login')
                
        response = self.get_response(request)
        return response

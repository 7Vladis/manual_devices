from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden

from .forms import LoginForm
from .decorators import role_required

User = get_user_model()

# Твой существующий класс авторизации
class MyLoginView(LoginView):
    form_class = LoginForm
    template_name = 'authorization/login.html'

    def form_valid(self, form):
        remember_me = form.cleaned_data.get('remember_me')
        if not remember_me:
            self.request.session.set_expiry(0)
        return super().form_valid(form)


# --- НОВЫЙ АДМИНИСТРАТИВНЫЙ ФУНКЦИОНАЛ ---

@login_required
@role_required(['admin', 'superuser'])
def create_user_view(request):
    """Создание локального пользователя через форму"""
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        username = request.POST.get('username', '').strip() # Получаем имя/FIO из формы
        password = request.POST.get('password', '').strip()
        
        if email and password:
            # Если имя пользователя не заполнено, берем часть email до собаки
            final_username = username if username else email.split('@')[0]
            
            User.objects.create_user(
                email=email,
                username=final_username, # Записываем имя в поле username
                password=password,
                role='junior',
                auth_source='django'
            )
            
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=users'
    return response


@login_required
@role_required(['admin', 'superuser'])
def update_user_role_view(request, pk):
    """Мгновенное изменение роли пользователя через HTMX dropdown"""
    target_user = get_object_or_404(User, pk=pk)
    
    # Не позволяем суперпользователю или администратору случайно понизить самого себя
    if target_user == request.user:
        return HttpResponseForbidden("Вы не можете изменить роль собственной учетной записи.")
        
    if request.method == 'POST':
        new_role = request.POST.get('role')
        if new_role in dict(User.ROLE_CHOICES):
            target_user.role = new_role
            target_user.save()
            
            # Возвращаем статус успешного выполнения
            return HttpResponse(
                '<span class="text-success small d-block animate-fade"><i class="bi bi-check-circle-fill"></i> Сохранено</span>'
            )
            
    return HttpResponse("Ошибка", status=400)


@login_required
@role_required(['admin', 'superuser'])
def toggle_user_status_view(request, pk):
    """Блокировка / Активация учетной записи"""
    target_user = get_object_or_404(User, pk=pk)
    
    if target_user != request.user:
        target_user.is_active = not target_user.is_active
        target_user.save()
        
    response = HttpResponse()
    response['HX-Redirect'] = '/settings/?tab=users'
    return response


@login_required
@role_required(['admin', 'superuser'])
def delete_user_view(request, pk):
    """Безвозвратное удаление пользователя из системы"""
    target_user = get_object_or_404(User, pk=pk)
    
    # Не позволяем удалять себя
    if target_user == request.user:
        return HttpResponseForbidden("Вы не можете удалить свою учетную запись.")
        
    if request.method in ['POST', 'DELETE']:
        target_user.delete()
        # Возвращаем пустой ответ, чтобы HTMX удалил строку таблицы из DOM
        return HttpResponse("", status=200)
        
    return HttpResponse("Метод не разрешен", status=405)
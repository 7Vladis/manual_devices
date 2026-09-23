from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from django.utils.html import format_html
from users.decorators import role_required
from .forms import MattermostSettingForm
from .models import MattermostSetting
from .services import test_specific_webhook

@login_required
@role_required(['admin', 'superuser'])  # Доступ только Администраторам и Суперюзерам
def notification_settings(request):
    return redirect('/settings/?tab=notifications')

@login_required
@role_required(['admin', 'superuser'])
@require_POST
def activate_webhook(request, pk):
    webhook = get_object_or_404(MattermostSetting, pk=pk)
    # Если мы хотим, чтобы активным был только один, раскомментируй строку ниже:
    # MattermostSetting.objects.all().update(is_active=False)
    webhook.is_active = not webhook.is_active
    webhook.save()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all()})

@login_required
@role_required(['admin', 'superuser'])
@require_POST
def test_webhook(request, pk):
    success, message = test_specific_webhook(pk)
    color = "success" if success else "danger"
    # Текст ошибки может содержать URL/тело ответа стороннего сервиса — экранируем
    return HttpResponse(format_html('<small class="text-{} ms-2">{}</small>', color, message))

@login_required
@role_required(['admin', 'superuser'])
@require_POST
def add_webhook(request):
    url = (request.POST.get('webhook_url') or '').strip()
    error = None

    if not url:
        error = "Укажите адрес webhook."
    else:
        form = MattermostSettingForm({'webhook_url': url})
        if form.is_valid():
            form.save()
        else:
            error = " ".join(form.errors.get('webhook_url', ["Некорректный адрес webhook."]))

    return render(request, 'notifications/includes/webhook_list.html', {
        'settings': MattermostSetting.objects.all(),
        'error': error,
    })

@login_required
@role_required(['admin', 'superuser'])
@require_POST
def delete_webhooks(request):
    ids = request.POST.getlist('webhook_ids')
    MattermostSetting.objects.filter(uuid__in=ids).delete()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all()})
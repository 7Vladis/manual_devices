from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils.html import format_html
from users.decorators import role_required
from .models import MattermostSetting
from .services import test_specific_webhook

@login_required
@role_required(['admin', 'superuser'])  # Доступ только Администраторам и Суперюзерам
def notification_settings(request):
    return redirect('/settings/?tab=notifications')

@login_required
@role_required(['admin', 'superuser'])
def activate_webhook(request, pk):
    webhook = get_object_or_404(MattermostSetting, pk=pk)
    # Если мы хотим, чтобы активным был только один, раскомментируй строку ниже:
    # MattermostSetting.objects.all().update(is_active=False)
    webhook.is_active = not webhook.is_active
    webhook.save()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})

@login_required
@role_required(['admin', 'superuser'])
def test_webhook(request, pk):
    success, message = test_specific_webhook(pk)
    color = "success" if success else "danger"
    # Текст ошибки может содержать URL/тело ответа стороннего сервиса — экранируем
    return HttpResponse(format_html('<small class="text-{} ms-2">{}</small>', color, message))

@login_required
@role_required(['admin', 'superuser'])
def add_webhook(request):
    url = request.POST.get('webhook_url')
    if url:
        MattermostSetting.objects.create(webhook_url=url)
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})

@login_required
@role_required(['admin', 'superuser'])
def delete_webhooks(request):
    ids = request.POST.getlist('webhook_ids')
    MattermostSetting.objects.filter(uuid__in=ids).delete()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})
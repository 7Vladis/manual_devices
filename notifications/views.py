from django.shortcuts import render, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from .models import MattermostSetting
from .services import test_specific_webhook

@staff_member_required
def notification_settings(request):
    settings = MattermostSetting.objects.all().order_by('-updated_at')
    return render(request, 'notifications/settings.html', {'settings': settings})

@staff_member_required
def activate_webhook(request, pk):
    webhook = get_object_or_404(MattermostSetting, pk=pk)
    # Если мы хотим, чтобы активным был только один, раскомментируй строку ниже:
    # MattermostSetting.objects.all().update(is_active=False)
    webhook.is_active = not webhook.is_active
    webhook.save()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})

@staff_member_required
def test_webhook(request, pk):
    success, message = test_specific_webhook(pk)
    color = "success" if success else "danger"
    return HttpResponse(f'<small class="text-{color} ms-2">{message}</small>')

@staff_member_required
def add_webhook(request):
    url = request.POST.get('webhook_url')
    if url:
        MattermostSetting.objects.create(webhook_url=url)
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})

@staff_member_required
def delete_webhooks(request):
    ids = request.POST.getlist('webhook_ids')
    MattermostSetting.objects.filter(uuid__in=ids).delete()
    return render(request, 'notifications/includes/webhook_list.html', 
                  {'settings': MattermostSetting.objects.all().order_by('-updated_at')})
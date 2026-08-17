# core/context_processors.py

from django.conf import settings

def youtrack_settings(request):
    """
    Добавляет базовый URL Youtrack во все шаблоны Django
    """
    return {
        'YOUTRACK_BASE_URL': getattr(settings, 'YOUTRACK_BASE_URL', 'https://youtrack.company.com')
    }
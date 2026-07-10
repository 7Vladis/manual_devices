import requests
import json
from .models import MattermostSetting

def send_mattermost_notification(text):
    config = MattermostSetting.objects.filter(is_active=True).last()
    if not config:
        return False, "Настройка webhook на найдена или деактивирована."
    payload = {"text": text}
    try:
        response = requests.post(
            config.webhook_url,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        if response.status_code == 200:
            return True, "Успешно"
        return False, f"Ошибка Mattermost: {response.status_code}"
    except Exception as e:
        return False, str(e)
    
def test_specific_webhook(webhook_id):
    config = MattermostSetting.objects.filter(uuid=webhook_id).first()
    if not config:
        return False, "Конфигурация не найдена."
    
    payload = {"text": f"Проверка связи: Система уведомлений Django активна."}
    try:
        response = requests.post(config.webhook_url, json=payload, timeout=5)
        if response.status_code == 200:
            return True, "Успешно отправлено!"
        return False, f"Ошибка: {response.status_code}"
    except Exception as e:
        return False, str(e)

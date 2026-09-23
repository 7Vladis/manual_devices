import requests
import json
from .models import MattermostSetting

def send_mattermost_notification(text, username="Диспетчер", emoji=":wrench:"):
    """
    Отправляет уведомление во ВСЕ активные webhook.

    Раньше бралась одна запись через .last() без ordering: при нескольких
    активных конфигурациях адресат выбирался произвольно, а остальные команды
    уведомлений молча ничего не получали.
    """
    configs = list(MattermostSetting.objects.filter(is_active=True))
    if not configs:
        return False, "Нет активных webhook: добавьте или включите конфигурацию в настройках."

    payload = {
        "text": text,
        "username": username,
        "icon_emoji": emoji,
    }

    delivered = 0
    errors = []
    for config in configs:
        try:
            response = requests.post(
                config.webhook_url,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            if response.status_code == 200:
                delivered += 1
            else:
                errors.append(f"{config.webhook_url}: код {response.status_code}")
        except requests.RequestException as exc:
            errors.append(f"{config.webhook_url}: {exc}")

    if delivered and not errors:
        return True, f"Успешно ({delivered} из {len(configs)})"
    if delivered:
        return True, f"Доставлено {delivered} из {len(configs)}. Ошибки: {'; '.join(errors)}"
    return False, "; ".join(errors)
    
def test_specific_webhook(webhook_id):
    config = MattermostSetting.objects.filter(uuid=webhook_id).first()
    if not config:
        return False, "Конфигурация не найдена."
    
    payload = {"text": "Проверка связи: система уведомлений активна."}
    try:
        response = requests.post(config.webhook_url, json=payload, timeout=5)
        if response.status_code == 200:
            return True, "Успешно отправлено!"
        return False, f"Ошибка: {response.status_code}"
    except requests.RequestException as exc:
        return False, str(exc)

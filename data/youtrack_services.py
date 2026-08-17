# data/youtrack_services.py

from django.conf import settings 
import requests
import logging

logger = logging.getLogger('data')

def get_auth_token(user) -> str | None:
    """Извлекает персональный токен пользователя."""
    if user and getattr(user, 'youtrack_token', None):
        token = user.youtrack_token.strip()
        return token if token else None
    return None


def send_comment_to_youtrack(issue_id: str, text: str, user) -> tuple[bool, str]:
    """
    Отправляет комментарий в задачу YouTrack от имени пользователя.
    Возвращает (True, "Успешно") или (False, "Текст ошибки").
    """
    token = get_auth_token(user)
    if not token:
        msg = "У вас не указан персональный токен YouTrack в профиле."
        logger.warning(f"YouTrack: {msg} (User: {user})")
        return False, msg

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        msg = "Базовый URL YouTrack не настроен на сервере."
        logger.error(msg)
        return False, msg

    url = f"{base_url}/api/issues/{issue_id}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {"text": text}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        
        if response.status_code in [200, 201]:
            logger.info(f"Комментарий успешно отправлен в YouTrack ({issue_id}) пользователем {user}")
            return True, "Комментарий отправлен в YouTrack"
        elif response.status_code in [401, 403]:
            msg = "Ошибка доступа YouTrack: неверный токен или нет прав на комментирование."
            logger.error(f"{msg} ({issue_id}, User: {user})")
            return False, msg
        else:
            msg = f"Ошибка YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg
            
    except requests.exceptions.RequestException as e:
        msg = f"Не удалось подключиться к серверу YouTrack: {e}"
        logger.error(msg)
        return False, msg


def add_work_item_to_youtrack(issue_id: str, duration_str: str, text: str, user) -> tuple[bool, str]:
    """
    Списывает время в задачу YouTrack от имени пользователя.
    Возвращает (True, "Успешно") или (False, "Текст ошибки").
    """
    token = get_auth_token(user)
    if not token:
        msg = "У вас не указан персональный токен YouTrack в профиле."
        logger.warning(f"YouTrack: {msg} (User: {user})")
        return False, msg

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        msg = "Базовый URL YouTrack не настроен на сервере."
        logger.error(msg)
        return False, msg

    url = f"{base_url}/api/issues/{issue_id}/timeTracking/workItems"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "duration": {"presentation": duration_str},
        "text": text
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        
        if response.status_code in [200, 201]:
            logger.info(f"Время ({duration_str}) успешно списано в YouTrack ({issue_id}) от {user}")
            return True, "Время успешно списано в YouTrack"
        elif response.status_code in [401, 403]:
            msg = "Ошибка доступа YouTrack: неверный токен или нет прав на списание времени."
            logger.error(f"{msg} ({issue_id}, User: {user})")
            return False, msg
        else:
            msg = f"Ошибка списания времени в YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg
            
    except requests.exceptions.RequestException as e:
        msg = f"Не удалось подключиться к серверу YouTrack: {e}"
        logger.error(msg)
        return False, msg
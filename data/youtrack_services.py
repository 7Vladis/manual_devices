# data/youtrack_services.py

from django.conf import settings 
import requests
import logging

logger = logging.getLogger('data')

def send_comment_to_youtrack(issue_id: str, text: str, author_name: str = None) -> bool:
    """
    Отправляет комментарий в Youtrack-задачу от имени системного бота.
    Если передан author_name, он добавляется в качестве подписи в текст комментария.
    """
    base_url = settings.YOUTRACK_BASE_URL
    token = settings.YOUTRACK_API_TOKEN

    # Если интеграция не настроена в .env, просто игнорируем отправку
    if not base_url or not token:
        logger.warning("Интеграция с Youtrack не настроена. Проверьте YOUTRACK_BASE_URL и YOUTRACK_API_TOKEN в .env")
        return False

    # Формируем красивую подпись автора
    if author_name:
        formatted_text = f"📝 **[Инженер: {author_name}]**:\n{text}"
    else:
        formatted_text = f"⚙️ **[Системное уведомление]**:\n{text}"

    # Удаляем лишние слэши в конце URL, если они есть
    base_url = base_url.rstrip('/')
    url = f"{base_url}/api/issues/{issue_id}/comments"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "text": formatted_text
    }

    try:
        # Устанавливаем тайм-аут 5 секунд, чтобы не блокировать интерфейс Django
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        
        if response.status_code in [200, 201]:
            logger.info(f"Комментарий успешно отправлен в Youtrack для задачи {issue_id}")
            return True
        else:
            logger.error(f"Не удалось отправить комментарий в Youtrack ({issue_id}). Код ответа: {response.status_code}, Тело: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка соединения с Youtrack при отправке комментария для {issue_id}: {e}")
        return False


def add_work_item_to_youtrack(issue_id: str, duration_str: str, text: str, author_name: str = None) -> bool:
    """
    Списывает потраченное время (Work Item) в задачу Youtrack.
    duration_str принимает формат времени Youtrack (например: "1h 30m", "45m")
    """
    base_url = settings.YOUTRACK_BASE_URL
    token = settings.YOUTRACK_API_TOKEN

    if not base_url or not token:
        return False

    url = f"{base_url}/api/issues/{issue_id}/timeTracking/workItems"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Подпись к списанию времени
    payload = {
        "duration": {
            "presentation": duration_str  # Youtrack сам распарсит строку вида "1h 30m"
        },
        "text": text
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        
        if response.status_code in [200, 201]:
            logger.info(f"Время ({duration_str}) успешно списано в Youtrack для задачи {issue_id}")
            return True
        else:
            logger.error(f"Не удалось списать время в Youtrack ({issue_id}). Код ответа: {response.status_code}, Тело: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка соединения с Youtrack при списании времени для {issue_id}: {e}")
        return False
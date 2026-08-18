# data/youtrack_services.py

from datetime import datetime
import logging
import requests
from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model

logger = logging.getLogger('data')


def get_auth_token(user) -> str | None:
    """Извлекает персональный токен пользователя."""
    if user and getattr(user, 'youtrack_token', None):
        token = user.youtrack_token.strip()
        return token if token else None
    return None


def send_comment_to_youtrack(issue_id: str, text: str, user) -> tuple[bool, str]:
    """Отправляет комментарий в задачу YouTrack от имени пользователя."""
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
    """Списывает время в задачу YouTrack от имени пользователя."""
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


def upload_attachment_to_youtrack(issue_id: str, file_obj, user) -> tuple[bool, str]:
    """Загружает файл/фотографию в карточку задачи YouTrack от имени пользователя."""
    token = get_auth_token(user)
    if not token:
        return False, "У вас не указан персональный токен YouTrack в профиле."

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    url = f"{base_url}/api/issues/{issue_id}/attachments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    try:
        file_obj.seek(0)
        files = {
            'file': (file_obj.name, file_obj.read(), getattr(file_obj, 'content_type', 'application/octet-stream'))
        }
        response = requests.post(url, headers=headers, files=files, timeout=15)
        
        if response.status_code in [200, 201]:
            logger.info(f"Файл {file_obj.name} успешно загружен в YouTrack ({issue_id}) пользователем {user}")
            return True, "Файл успешно загружен в YouTrack"
        else:
            msg = f"Ошибка загрузки файла в YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg

    except requests.exceptions.RequestException as e:
        msg = f"Не удалось подключиться к YouTrack для отправки файла: {e}"
        logger.error(msg)
        return False, msg


def sync_issue_from_youtrack(data_object, user) -> tuple[bool, str]:
    """
    Синхронизирует описание, комментарии и ВСЕ вложения (как из комментариев, 
    так и прикрепленные напрямую к карточке задачи) из YouTrack в DataObject.
    """
    token = get_auth_token(user)
    if not token:
        return False, "У вас не указан персональный токен YouTrack в профиле."

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    issue_id = data_object.youtrack_issue_id
    if not issue_id:
        return False, "У объекта не задан ID задачи YouTrack."

    url = f"{base_url}/api/issues/{issue_id}"
    fields_query = (
        "id,summary,description,created,updated,"
        "comments(id,text,created,updated,author(id,name,login,email),"
        "attachments(id,name,url,size,mimeType,created,author(id,name,email))),"
        "attachments(id,name,url,size,created,author(id,name,email))"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, params={"fields": fields_query}, timeout=15)
        if response.status_code != 200:
            return False, f"Ошибка YouTrack ({response.status_code}): {response.text}"

        data = response.json()
        User = get_user_model()
        from .models import Comment, Attachment, ActionHistory

        # 1. Синхронизируем описание объекта (Markdown)
        yt_description = data.get('description', '')
        if yt_description and yt_description != data_object.description:
            data_object.description = yt_description
            data_object.save(update_fields=['description'])

        new_comments_count = 0
        new_files_count = 0

        # 2. Синхронизируем комментарии и их вложения
        yt_comments = data.get('comments', [])
        for c_data in yt_comments:
            c_id = c_data.get('id')
            c_text = c_data.get('text', '') or "Вложение из YouTrack"
            
            comment_obj = Comment.objects.filter(data_object=data_object, youtrack_id=c_id).first()
            if not comment_obj:
                author_email = c_data.get('author', {}).get('email')
                author_user = None
                if author_email:
                    author_user = User.objects.filter(email__iexact=author_email).first()
                if not author_user:
                    author_user = user

                created_ts = c_data.get('created')
                created_dt = timezone.now()
                if created_ts:
                    created_dt = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.get_current_timezone())

                comment_obj = Comment.objects.create(
                    user=author_user,
                    data_object=data_object,
                    text=c_text,
                    created_at=created_dt,
                    youtrack_id=c_id
                )
                new_comments_count += 1

            # Вложения этого конкретного комментария
            c_attachments = c_data.get('attachments', [])
            for att_data in c_attachments:
                att_id = att_data.get('id')
                att_name = att_data.get('name')
                att_url = att_data.get('url')

                if att_id and att_url and not Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).exists():
                    full_att_url = f"{base_url}{att_url}" if att_url.startswith('/') else att_url
                    file_res = requests.get(full_att_url, headers=headers, timeout=20)
                    
                    if file_res.status_code == 200:
                        file_content = ContentFile(file_res.content, name=att_name)
                        Attachment.objects.create(
                            user=comment_obj.user,
                            data_object=data_object,
                            comment=comment_obj,
                            path=file_content,
                            is_preview=False,
                            youtrack_id=att_id
                        )
                        new_files_count += 1

        # 3. Синхронизируем ОБЩИЕ вложения карточки задачи (прикрепленные к самой задаче)
        yt_issue_attachments = data.get('attachments', [])
        for att_data in yt_issue_attachments:
            att_id = att_data.get('id')
            att_name = att_data.get('name')
            att_url = att_data.get('url')

            if att_id and att_url and not Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).exists():
                full_att_url = f"{base_url}{att_url}" if att_url.startswith('/') else att_url
                file_res = requests.get(full_att_url, headers=headers, timeout=20)
                
                if file_res.status_code == 200:
                    author_email = att_data.get('author', {}).get('email')
                    author_user = User.objects.filter(email__iexact=author_email).first() if author_email else user
                    
                    created_ts = att_data.get('created')
                    created_dt = timezone.now()
                    if created_ts:
                        created_dt = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.get_current_timezone())

                    file_content = ContentFile(file_res.content, name=att_name)
                    Attachment.objects.create(
                        user=author_user or user,
                        data_object=data_object,
                        comment=None,  # Общее вложение к объекту
                        path=file_content,
                        is_preview=False,
                        youtrack_id=att_id,
                        created_at=created_dt
                    )
                    new_files_count += 1

        if new_comments_count > 0 or new_files_count > 0:
            ActionHistory.objects.create(
                user=user,
                data_object=data_object,
                action=f"Синхронизация с YouTrack: добавлено комментариев ({new_comments_count}), файлов ({new_files_count})."
            )

        return True, f"Синхронизировано: комментариев +{new_comments_count}, файлов +{new_files_count}."

    except requests.exceptions.RequestException as e:
        msg = f"Ошибка сети при синхронизации с YouTrack: {e}"
        logger.error(msg)
        return False, msg
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


def send_comment_to_youtrack(issue_id: str, text: str, user) -> tuple[bool, str | None]:
    """Отправляет комментарий и возвращает (True, comment_id) при успехе"""
    token = get_auth_token(user)
    if not token:
        return False, "У вас не указан персональный токен YouTrack в профиле."

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

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
            data = response.json()
            comment_id = data.get('id')  # Получаем реальный ID комментария в YouTrack
            logger.info(f"Комментарий успешно отправлен в YouTrack ({issue_id}) с ID {comment_id}")
            return True, comment_id
        elif response.status_code in [401, 403]:
            return False, "Ошибка доступа YouTrack: неверный токен или нет прав."
        else:
            return False, f"Ошибка YouTrack ({response.status_code}): {response.text}"
            
    except requests.exceptions.RequestException as e:
        return False, f"Не удалось подключиться к YouTrack: {e}"


def add_work_item_to_youtrack(issue_id: str, duration_str: str, text: str, user) -> tuple[bool, str | None]:
    """Списывает время и возвращает (True, work_item_id) при успехе"""
    token = get_auth_token(user)
    if not token:
        return False, "У вас не указан персональный токен YouTrack в профиле."

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

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
            data = response.json()
            work_id = data.get('id')  # Получаем реальный ID списания времени
            logger.info(f"Время успешно списано в YouTrack ({issue_id}) с ID {work_id}")
            return True, work_id
        elif response.status_code in [401, 403]:
            return False, "Ошибка доступа YouTrack: неверный токен или нет прав."
        else:
            return False, f"Ошибка YouTrack ({response.status_code}): {response.text}"
            
    except requests.exceptions.RequestException as e:
        return False, f"Не удалось подключиться к YouTrack: {e}"


def upload_attachment_to_youtrack(issue_id: str, file_obj, user) -> tuple[bool, str | None]:
    """Загружает файл и возвращает (True, attachment_id) при успехе"""
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
            data = response.json()
            # YouTrack API возвращает массив загруженных файлов: [{"id": "..."}, ...]
            att_id = data[0].get('id') if isinstance(data, list) and data else None
            return True, att_id
        else:
            return False, f"Ошибка загрузки файла в YouTrack ({response.status_code}): {response.text}"

    except requests.exceptions.RequestException as e:
        return False, f"Не удалось подключиться к YouTrack: {e}"


def update_issue_description_in_youtrack(issue_id: str, description: str, user) -> tuple[bool, str]:
    """Обновляет текст описания задачи в YouTrack от имени пользователя"""
    token = get_auth_token(user)
    if not token:
        return False, "У вас не указан персональный токен YouTrack в профиле."

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    url = f"{base_url}/api/issues/{issue_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "description": description or ""
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            logger.info(f"Описание задачи YouTrack ({issue_id}) успешно обновлено пользователем {user}")
            return True, "Описание обновлено в YouTrack"
        else:
            msg = f"Ошибка обновления описания в YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg

    except requests.exceptions.RequestException as e:
        msg = f"Не удалось подключиться к YouTrack для обновления описания: {e}"
        logger.error(msg)
        return False, msg


def delete_comment_from_youtrack(issue_id: str, comment_yt_id: str, user) -> tuple[bool, str]:
    """Удаляет комментарий из YouTrack от имени пользователя"""
    token = get_auth_token(user)
    if not token or not issue_id or not comment_yt_id:
        return False, "Недостаточно данных для удаления комментария в YouTrack"

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    url = f"{base_url}/api/issues/{issue_id}/comments/{comment_yt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    try:
        response = requests.delete(url, headers=headers, timeout=10)
        if response.status_code in [200, 204]:
            logger.info(f"Комментарий YouTrack ({comment_yt_id}) удален пользователем {user}")
            return True, "Комментарий удален из YouTrack"
        else:
            msg = f"Ошибка удаления комментария из YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg

    except requests.exceptions.RequestException as e:
        msg = f"Ошибка сети при удалении комментария из YouTrack: {e}"
        logger.error(msg)
        return False, msg


def delete_attachment_from_youtrack(issue_id: str, attachment_yt_id: str, user) -> tuple[bool, str]:
    """Удаляет вложение из карточки задачи YouTrack от имени пользователя"""
    token = get_auth_token(user)
    if not token or not issue_id or not attachment_yt_id:
        return False, "Недостаточно данных для удаления вложения в YouTrack"

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    url = f"{base_url}/api/issues/{issue_id}/attachments/{attachment_yt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    try:
        response = requests.delete(url, headers=headers, timeout=10)
        if response.status_code in [200, 204]:
            logger.info(f"Вложение YouTrack ({attachment_yt_id}) удалено пользователем {user}")
            return True, "Вложение удалено из YouTrack"
        else:
            msg = f"Ошибка удаления вложения из YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg

    except requests.exceptions.RequestException as e:
        msg = f"Ошибка сети при удалении вложения из YouTrack: {e}"
        logger.error(msg)
        return False, msg


def sync_issue_from_youtrack(data_object, user) -> tuple[bool, str]:
    """
    Полная двусторонняя синхронизация:
    1. Обновляет описание.
    2. Добавляет новые комментарии, файлы и workItems из YouTrack.
    3. УДАЛЯЕТ из Django те комментарии, файлы и workItems, которые были удалены в YouTrack.
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

        # Наборы актуальных ID из YouTrack для очистки удаленных
        active_yt_comment_ids = set()
        active_yt_attachment_ids = set()
        active_yt_work_item_ids = set()

        new_comments_count = 0
        new_files_count = 0
        new_work_items_count = 0

        # 2. Синхронизируем комментарии и вложения к ним
        yt_comments = data.get('comments', [])
        for c_data in yt_comments:
            c_id = c_data.get('id')
            if not c_id:
                continue
            active_yt_comment_ids.add(c_id)

            c_text = c_data.get('text', '') or "Вложение из YouTrack"
            comment_obj = Comment.objects.filter(data_object=data_object, youtrack_id=c_id).first()
            
            if not comment_obj:
                author_email = c_data.get('author', {}).get('email')
                author_user = User.objects.filter(email__iexact=author_email).first() if author_email else None

                created_ts = c_data.get('created')
                created_dt = timezone.now()
                if created_ts:
                    created_dt = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.get_current_timezone())

                comment_obj = Comment.objects.create(
                    user=author_user or user,
                    data_object=data_object,
                    text=c_text,
                    created_at=created_dt,
                    youtrack_id=c_id
                )
                new_comments_count += 1

            # Вложения этого комментария
            for att_data in c_data.get('attachments', []):
                att_id = att_data.get('id')
                if not att_id:
                    continue
                active_yt_attachment_ids.add(att_id)

                if not Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).exists():
                    att_name = att_data.get('name')
                    att_url = att_data.get('url')
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

        # 3. Синхронизируем общие вложения карточки задачи
        yt_issue_attachments = data.get('attachments', [])
        for att_data in yt_issue_attachments:
            att_id = att_data.get('id')
            if not att_id:
                continue
            active_yt_attachment_ids.add(att_id)

            if not Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).exists():
                att_name = att_data.get('name')
                att_url = att_data.get('url')
                full_att_url = f"{base_url}{att_url}" if att_url.startswith('/') else att_url
                file_res = requests.get(full_att_url, headers=headers, timeout=20)
                
                if file_res.status_code == 200:
                    author_email = att_data.get('author', {}).get('email')
                    author_user = User.objects.filter(email__iexact=author_email).first() if author_email else None
                    
                    created_ts = att_data.get('created')
                    created_dt = timezone.now()
                    if created_ts:
                        created_dt = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.get_current_timezone())

                    file_content = ContentFile(file_res.content, name=att_name)
                    Attachment.objects.create(
                        user=author_user or user,
                        data_object=data_object,
                        comment=None,
                        path=file_content,
                        is_preview=False,
                        youtrack_id=att_id,
                        created_at=created_dt
                    )
                    new_files_count += 1

        # 4. Синхронизируем WorkItems (списания времени / работы)
        work_items_url = f"{base_url}/api/issues/{issue_id}/timeTracking/workItems"
        work_items_res = requests.get(
            work_items_url,
            headers=headers,
            params={"fields": "id,text,created,date,author(id,name,email)"},
            timeout=15
        )

        if work_items_res.status_code == 200:
            yt_work_items = work_items_res.json()
            for w_data in yt_work_items:
                w_id = w_data.get('id')
                if not w_id:
                    continue
                active_yt_work_item_ids.add(w_id)

                w_text = (w_data.get('text') or '').strip()
                if not w_text:
                    continue

                if not ActionHistory.objects.filter(data_object=data_object, youtrack_id=w_id).exists():
                    author_email = w_data.get('author', {}).get('email')
                    author_user = User.objects.filter(email__iexact=author_email).first() if author_email else None

                    created_ts = w_data.get('date') or w_data.get('created')
                    created_dt = timezone.now()
                    if created_ts:
                        created_dt = datetime.fromtimestamp(created_ts / 1000.0, tz=timezone.get_current_timezone())

                    ActionHistory.objects.create(
                        user=author_user or user,
                        data_object=data_object,
                        action_type='maintenance',
                        action=w_text,
                        youtrack_id=w_id,
                        created_at=created_dt
                    )
                    new_work_items_count += 1

        # 5. ОЧИСТКА: Удаляем из Django то, что было удалено в YouTrack
        # Удаляем удаленные в YouTrack комментарии
        deleted_comments_count, _ = Comment.objects.filter(
            data_object=data_object, 
            youtrack_id__isnull=False
        ).exclude(youtrack_id__in=active_yt_comment_ids).delete()

        # Удаляем удаленные в YouTrack файлы
        deleted_files_count, _ = Attachment.objects.filter(
            data_object=data_object, 
            youtrack_id__isnull=False
        ).exclude(youtrack_id__in=active_yt_attachment_ids).delete()

        # Удаляем удаленные в YouTrack записи истории (workItems)
        deleted_works_count, _ = ActionHistory.objects.filter(
            data_object=data_object, 
            youtrack_id__isnull=False
        ).exclude(youtrack_id__in=active_yt_work_item_ids).delete()

        total_deleted = deleted_comments_count + deleted_files_count + deleted_works_count
        total_added = new_comments_count + new_files_count + new_work_items_count

        if total_added > 0 or total_deleted > 0:
            ActionHistory.objects.create(
                user=user,
                data_object=data_object,
                action_type='sync',
                action=f"Синхронизация с YouTrack: добавлено ({total_added}), удалено ({total_deleted})."
            )

        return True, f"Синхронизировано: добавлено +{total_added}, удалено -{total_deleted}."

    except requests.exceptions.RequestException as e:
        msg = f"Ошибка сети при синхронизации с YouTrack: {e}"
        logger.error(msg)
        return False, msg
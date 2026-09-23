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


def update_comment_in_youtrack(issue_id: str, comment_yt_id: str, text: str, user) -> tuple[bool, str]:
    """Обновляет текст комментария в YouTrack от имени пользователя"""
    token = get_auth_token(user)
    if not token or not issue_id or not comment_yt_id:
        return False, "Недостаточно данных для обновления комментария в YouTrack"

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен на сервере."

    url = f"{base_url}/api/issues/{issue_id}/comments/{comment_yt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {"text": text}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            logger.info(f"Комментарий YouTrack ({comment_yt_id}) успешно обновлен пользователем {user}")
            return True, "Комментарий обновлен в YouTrack"
        else:
            msg = f"Ошибка обновления комментария в YouTrack ({response.status_code}): {response.text}"
            logger.error(msg)
            return False, msg

    except requests.exceptions.RequestException as e:
        msg = f"Не удалось подключиться к YouTrack для обновления комментария: {e}"
        logger.error(msg)
        return False, msg


WORK_ITEMS_PAGE_SIZE = 100
WORK_ITEMS_MAX_PAGES = 50


def fetch_all_work_items(base_url: str, issue_id: str, headers: dict) -> list | None:
    """
    Возвращает все списания времени по задаче, обходя пагинацию YouTrack.
    None — если хотя бы одна страница не получена: в этом случае локальные
    записи трогать нельзя.
    """
    url = f"{base_url}/api/issues/{issue_id}/timeTracking/workItems"
    items = []
    for page in range(WORK_ITEMS_MAX_PAGES):
        res = requests.get(
            url,
            headers=headers,
            params={
                "fields": "id,text,created,date,author(id,name,email)",
                "$top": WORK_ITEMS_PAGE_SIZE,
                "$skip": page * WORK_ITEMS_PAGE_SIZE,
            },
            timeout=10,
        )
        if res.status_code != 200:
            logger.warning("YouTrack %s: страница списаний %s не получена (%s)", issue_id, page, res.status_code)
            return None
        chunk = res.json()
        if not isinstance(chunk, list):
            return None
        items.extend(chunk)
        if len(chunk) < WORK_ITEMS_PAGE_SIZE:
            return items
    logger.warning("YouTrack %s: превышен лимит страниц списаний, синхронизация удалений пропущена", issue_id)
    return None


def _log_stale(kind: str, issue_id: str, youtrack_ids) -> None:
    ids = list(youtrack_ids)
    if ids:
        logger.info("YouTrack %s: локально удаляются %s, отсутствующие в задаче: %s", issue_id, kind, ids)


def sync_issue_from_youtrack(data_object, user) -> tuple[bool, str]:
    """
    Автоматическая двусторонняя синхронизация с YouTrack:
    Возвращает (True, "сообщение об успехе") или (False, "краткая причина ошибки").
    """
    token = get_auth_token(user)
    if not token:
        return False, "В вашем профиле не указан персональный токен YouTrack"

    base_url = getattr(settings, 'YOUTRACK_BASE_URL', '').rstrip('/')
    if not base_url:
        return False, "Базовый URL YouTrack не настроен в конфигурации сервера"

    issue_id = data_object.youtrack_issue_id
    if not issue_id:
        return False, "У объекта не задан ID задачи YouTrack"

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
        response = requests.get(url, headers=headers, params={"fields": fields_query}, timeout=5)
        
        if response.status_code in [401, 403]:
            return False, "Неверный токен YouTrack или недостаточно прав доступа"
        elif response.status_code == 404:
            return False, f"Задача {issue_id} не найдена в YouTrack"
        elif response.status_code != 200:
            return False, f"Ошибка YouTrack ({response.status_code})"

        data = response.json()
        User = get_user_model()
        from .models import Comment, Attachment, ActionHistory

        # 1. Синхронизируем описание объекта (пустое описание — тоже валидное состояние)
        if 'description' in data:
            yt_description = (data.get('description') or '').strip() or None
            if yt_description != ((data_object.description or '').strip() or None):
                data_object.description = yt_description
                data_object.save(update_fields=['description'])

        active_yt_comment_ids = set()
        active_yt_attachment_ids = set()
        active_yt_work_item_ids = set()

        new_comments_count = 0
        updated_comments_count = 0
        new_files_count = 0
        new_work_items_count = 0

        # Удалять локальные копии можно только по коллекциям, которые сервер
        # действительно вернул: частичный ответ не должен трактоваться как удаление.
        comments_received = 'comments' in data
        attachments_received = 'attachments' in data
        work_items_received = False

        # 2. Синхронизируем комментарии и их вложения
        yt_comments = data.get('comments', [])
        for c_data in yt_comments:
            c_id = c_data.get('id')
            if not c_id:
                continue
            active_yt_comment_ids.add(c_id)

            c_text = c_data.get('text', '') or "Вложение из YouTrack"
            comment_obj = Comment.objects.filter(data_object=data_object, youtrack_id=c_id).first()
            
            if comment_obj and comment_obj.text != c_text:
                comment_obj.text = c_text
                comment_obj.save(update_fields=['text'])
                updated_comments_count += 1

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

            for att_data in c_data.get('attachments', []):
                att_id = att_data.get('id')
                if not att_id:
                    continue
                active_yt_attachment_ids.add(att_id)

                if not Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).exists():
                    att_name = att_data.get('name')
                    att_url = att_data.get('url')
                    full_att_url = f"{base_url}{att_url}" if att_url.startswith('/') else att_url
                    file_res = requests.get(full_att_url, headers=headers, timeout=10)
                    
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

        # 3. Синхронизируем общие файлы карточки
        yt_issue_attachments = data.get('attachments', [])
        for att_data in yt_issue_attachments:
            att_id = att_data.get('id')
            if not att_id:
                continue
            active_yt_attachment_ids.add(att_id)

            att_name = att_data.get('name') or ''
            
            # Проверяем, существует ли уже это вложение
            att_obj = Attachment.objects.filter(data_object=data_object, youtrack_id=att_id).first()

            # Ищем, не принадлежит ли файл какому-либо комментарию (по имени файла в тексте Markdown)
            matched_comment = None
            for c in data_object.comments.all():
                if att_name and att_name in c.text:
                    matched_comment = c
                    break

            if not att_obj:
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
                        comment=matched_comment,  # Привязываем к комментарию, если имя совпало
                        path=file_content,
                        is_preview=False,
                        youtrack_id=att_id,
                        created_at=created_dt
                    )
                    new_files_count += 1
            else:
                # Если файл уже скачан, но ранее не был привязан к комментарию — привязываем его
                if matched_comment and not att_obj.comment:
                    att_obj.comment = matched_comment
                    att_obj.save(update_fields=['comment'])

        # 4. Синхронизируем списания времени (WorkItems).
        # Эндпоинт постраничный ($top по умолчанию 42): выбираем все страницы,
        # иначе старые списания были бы приняты за удалённые.
        yt_work_items = fetch_all_work_items(base_url, issue_id, headers)
        work_items_received = yt_work_items is not None

        if work_items_received:
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
                        created_at=created_dt,
                        # Списание времени в задаче не закрывает локальное
                        # плановое событие — в статистику плана не попадает.
                        maintenance_kind='unplanned',
                    )
                    new_work_items_count += 1

        # 5. Удаляем локальные записи, удалённые в YouTrack — только по тем
        # коллекциям, которые сервер вернул целиком.
        deleted_comments_count = deleted_files_count = deleted_works_count = 0

        if comments_received:
            stale_comments = Comment.objects.filter(
                data_object=data_object, youtrack_id__isnull=False
            ).exclude(youtrack_id__in=active_yt_comment_ids)
            _log_stale('комментарии', issue_id, stale_comments.values_list('youtrack_id', flat=True))
            deleted_comments_count, _ = stale_comments.delete()

        if comments_received and attachments_received:
            stale_files = Attachment.objects.filter(
                data_object=data_object, youtrack_id__isnull=False
            ).exclude(youtrack_id__in=active_yt_attachment_ids)
            _log_stale('вложения', issue_id, stale_files.values_list('youtrack_id', flat=True))
            deleted_files_count, _ = stale_files.delete()

        if work_items_received:
            stale_works = ActionHistory.objects.filter(
                data_object=data_object, youtrack_id__isnull=False
            ).exclude(youtrack_id__in=active_yt_work_item_ids)
            _log_stale('списания времени', issue_id, stale_works.values_list('youtrack_id', flat=True))
            deleted_works_count, _ = stale_works.delete()

        total_deleted = deleted_comments_count + deleted_files_count + deleted_works_count
        total_added = new_comments_count + new_files_count + new_work_items_count

        if total_added or total_deleted or updated_comments_count:
            ActionHistory.objects.create(
                user=user,
                data_object=data_object,
                action_type='sync',
                action=(
                    f"Синхронизация с YouTrack: добавлено {total_added}, "
                    f"обновлено {updated_comments_count}, удалено {total_deleted}."
                )
            )

        return True, "Данные успешно синхронизированы с YouTrack"

    except requests.exceptions.ConnectionError:
        return False, "Не удалось подключиться к серверу YouTrack (сервер недоступен)"
    except requests.exceptions.Timeout:
        return False, "Превышено время ожидания ответа от YouTrack"
    except requests.exceptions.RequestException as e:
        return False, f"Сетевая ошибка при обращении к YouTrack: {e}"
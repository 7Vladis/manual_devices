import uuid
import os
import re
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings


def get_attachment_upload_path(instance, filename):
    """
    Формирует структурированный путь для сохранения вложений:
    attachments/<имя_объекта>_<uuid_объекта>/<исходное_имя_файла>
    """
    obj = instance.data_object
    raw_name = obj.name or (obj.model.name if obj.model else "object")
    
    # Очищаем имя объекта от недопустимых символов для файловой системы
    safe_name = re.sub(r'[^\w\-\. ]', '_', raw_name).strip()
    safe_name = safe_name[:50] or "object"  # Ограничиваем длину имени папки
    
    folder_name = f"{safe_name}_{obj.uuid}"
    return os.path.join('attachments', folder_name, filename)


class ObjectType(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4,  editable=False)
    type = models.CharField(max_length=100, unique=True, verbose_name="Название объекта")

    class Meta:
        db_table = 'object_type'
        verbose_name = 'Тип объекта'
        verbose_name_plural = 'Типы объектов'

    def __str__(self):
        return self.type


class DependencyType(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=100, unique=True, verbose_name="Тип зависимости")

    class Meta:
        db_table = 'dependency_type'
        verbose_name = 'Тип зависимости'
        verbose_name_plural = 'Типы зависимостей'

    def __str__(self):
        return self.type  


class DateUpdateRule(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True, verbose_name="Название правила")
    rule = models.JSONField(default=dict, verbose_name="Правило расчета (JSON)")

    class Meta:
        db_table = 'date_update_rule'
        verbose_name = 'Правило обновления даты'
        verbose_name_plural = 'Правила обновления дат'

    def __str__(self):
        return self.name


class ObjectModel(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    object_type = models.ForeignKey(ObjectType, on_delete=models.PROTECT, related_name='models', verbose_name="Тип объектов")
    name = models.CharField(max_length=255, verbose_name="Название модели")
    specifications = models.JSONField(default=dict,blank=True, null=True, verbose_name="Характеристики модели")

    class Meta:
        db_table = 'object_model'
        verbose_name = 'Модель объекта'
        verbose_name_plural = 'Модели объектов'

    def __str__(self):
        return f"{self.object_type.type} {self.name}"
     

class DataObject(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,blank = True, null=True, related_name='data_objects', verbose_name="Пользователь")
    model = models.ForeignKey(ObjectModel, on_delete=models.CASCADE, related_name='data_objects', verbose_name="Модель")
    name = models.CharField(max_length=255, verbose_name="Имя объекта", blank=True, null=True)
    inventory_number = models.CharField(max_length=100, verbose_name="Инвентарный номер", blank=True, null=True)
    youtrack_issue_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="ID задачи в Youtrack")
    next_maintenance_date = models.DateTimeField(blank=True, null=True, verbose_name="Дата следующего обслуживания")
    description = models.TextField(blank=True, null=True, verbose_name="Описание")
    date_update_rule = models.ForeignKey(DateUpdateRule, on_delete=models.SET_NULL, blank=True, null=True, related_name='data_objects', verbose_name="Правило расчета ТО")

    class Meta:
        db_table = 'data_object'
        verbose_name = 'Объект данных'
        verbose_name_plural = 'Объекты данных'

    def __str__(self):
        return self.name or f"{self.model.name} ({self.inventory_number})"
         
    
class Relation(models.Model):
    main = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name="subject_relations", verbose_name="Главный объект")
    subject = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='main_relations', verbose_name="Зависимый объект")
    dependency_type = models.ForeignKey(DependencyType, on_delete=models.PROTECT, related_name='relations', verbose_name="Тип связи")
    pk = models.CompositePrimaryKey('main', 'subject')

    class Meta:
        db_table = 'relation'
        verbose_name = 'Связь объектов'
        verbose_name_plural = 'Связи объектов'

    def __str__(self):
        return f"{self.main} -> {self.dependency_type.type} -> {self.subject}"
    

class ActionHistory(models.Model):
    ACTION_TYPE_CHOICES = [
        ('maintenance', 'Работа с оборудованием'),
        ('create', 'Создание объекта'),
        ('update', 'Редактирование данных'),
        ('rule_change', 'Изменение правила ТО'),
        ('link_change', 'Изменение связей'),
        ('sync', 'Синхронизация с YouTrack'),
        ('other', 'Прочее действие'),
    ]
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Исполнитель")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='actions', verbose_name = "Объект")
    action = models.TextField(verbose_name="Описание действия")
    action_type = models.CharField(max_length=30, choices=ACTION_TYPE_CHOICES, default='other', db_index=True, verbose_name="Тип действия")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата создания")
    youtrack_id = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="ID записи в YouTrack")

    class Meta:
        db_table = 'action_history'
        verbose_name = 'История действия'
        verbose_name_plural = 'История объектов'

    
class Comment(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Автор")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='comments', verbose_name="Объект")
    text = models.TextField(verbose_name="Текст комментария")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата создания")
    youtrack_id = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="ID комментария в YouTrack")

    class Meta:
        db_table = 'comment'
        verbose_name = 'Комментарий'
        verbose_name_plural = 'Комментарии'

    
class Attachment(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Загрузил")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='attachments', verbose_name="Объект")
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments', verbose_name="Комментарий")
    path = models.FileField(upload_to=get_attachment_upload_path, verbose_name="Файл")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата загрузки")
    is_preview = models.BooleanField(default=False, verbose_name="Превью (фото)")
    youtrack_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="ID вложения в YouTrack")

    class Meta:
        db_table = 'attachment'
        verbose_name = 'Вложение'
        verbose_name_plural = 'Вложения'


    @property
    def filename(self):
        """Возвращает чистое имя файла без пути"""
        if self.path:
            return os.path.basename(self.path.name)
        return ""
    

    @property
    def is_image(self):
        """Проверяет по расширению, является ли вложение изображением"""
        if not self.path:
            return False
        ext = os.path.splitext(self.path.name)[1].lower()
        return ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']


@receiver(post_delete, sender=Attachment)
def delete_attachment_file(sender, instance, **kwargs):
    """Автоматически удаляет физический файл с диска при удалении записи Attachment из БД"""
    if instance.path:
        if os.path.isfile(instance.path.path):
            try:
                os.remove(instance.path.path)
            except Exception:
                pass
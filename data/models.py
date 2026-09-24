import uuid
import os
import re
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower, Trim
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
    safe_name = safe_name[:50] or "object"
    
    folder_name = f"{safe_name}_{obj.uuid}"
    return os.path.join('attachments', folder_name, filename)


class ObjectType(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=100, unique=True, verbose_name="Название объекта")

    class Meta:
        db_table = 'object_type'
        verbose_name = 'Тип объекта'
        verbose_name_plural = 'Типы объектов'
        constraints = [
            # unique=True ловит только точное совпадение, поэтому «Насосы» и
            # «насосы» создавались как два разных типа справочника.
            models.UniqueConstraint(
                Lower(Trim('type')),
                name='unique_object_type_name',
                violation_error_message='Тип оборудования с таким названием уже существует.',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.type:
            self.type = self.type.strip()
        return super().save(*args, **kwargs)

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
    specifications = models.JSONField(default=dict, blank=True, null=True, verbose_name="Характеристики модели")

    class Meta:
        db_table = 'object_model'
        verbose_name = 'Модель объекта'
        verbose_name_plural = 'Модели объектов'
        constraints = [
            # Внутри одного типа оборудования название модели уникально без
            # учёта регистра и краевых пробелов. Проверка в представлении
            # предупреждала о дубликате, но не мешала его создать — в том
            # числе при гонке двух одновременных запросов.
            models.UniqueConstraint(
                Lower(Trim('name')),
                'object_type',
                name='unique_model_name_per_object_type',
                violation_error_message='Модель с таким названием уже существует для этого типа оборудования.',
            ),
        ]

    def save(self, *args, **kwargs):
        # Нормализуем название, чтобы ограничение уникальности не обходилось
        # лишними пробелами по краям.
        if self.name:
            self.name = self.name.strip()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.object_type.type} {self.name}"


class DataObject(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, blank=True, null=True, related_name='children', verbose_name="Родительский объект")
    # PROTECT: модель с привязанными объектами удалить нельзя — иначе вместе с ней
    # каскадом исчезли бы объекты, их история, комментарии и вложения.
    model = models.ForeignKey(ObjectModel, on_delete=models.PROTECT, related_name='data_objects', verbose_name="Модель")
    name = models.CharField(max_length=255, verbose_name="Имя объекта", blank=True, null=True)
    inventory_number = models.CharField(max_length=100, verbose_name="Инвентарный номер", blank=True, null=True)
    youtrack_issue_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="ID задачи в Youtrack")
    next_maintenance_date = models.DateField(blank=True, null=True, verbose_name="Дата следующего обслуживания")
    description = models.TextField(blank=True, null=True, verbose_name="Описание")
    date_update_rule = models.ForeignKey(DateUpdateRule, on_delete=models.SET_NULL, blank=True, null=True, related_name='data_objects', verbose_name="Правило расчета ТО")

    class Meta:
        db_table = 'data_object'
        verbose_name = 'Объект данных'
        verbose_name_plural = 'Объекты данных'

    def __str__(self):
        return self.name or f"{self.model.name} ({self.inventory_number})"

    def get_effective_youtrack_issue(self):
        """
        Возвращает кортеж (youtrack_issue_id, target_object):
        1. Если у объекта есть свой youtrack_issue_id -> возвращает (youtrack_issue_id, self)
        2. Иначе поднимается по цепочке parent и берет ID у первого встреченного родителя.
        3. Если ни у кого в ветке нет задачи -> (None, None)
        """
        curr = self
        visited = set()
        while curr and curr.uuid not in visited:
            if curr.youtrack_issue_id:
                return curr.youtrack_issue_id, curr
            visited.add(curr.uuid)
            curr = curr.parent
        return None, None

    @property
    def effective_youtrack_issue_id(self):
        issue_id, _ = self.get_effective_youtrack_issue()
        return issue_id

    # --- Целостность иерархии ---

    MAX_TREE_DEPTH = 64

    def get_descendant_uuids(self):
        """
        UUID всех потомков объекта (обход в ширину, защищён от циклов).
        Используется, чтобы запретить назначать потомка родителем.
        """
        visited = {self.uuid}
        frontier = [self.uuid]
        while frontier:
            children = DataObject.objects.filter(parent__in=frontier).values_list('uuid', flat=True)
            frontier = [uuid for uuid in children if uuid not in visited]
            visited.update(frontier)
        visited.discard(self.uuid)
        return visited

    def get_subtree_stats(self):
        """Сколько записей исчезнет при удалении объекта вместе с поддеревом."""
        uuids = self.get_descendant_uuids() | {self.uuid}
        return {
            'descendants': len(uuids) - 1,
            'comments': Comment.objects.filter(data_object__in=uuids).count(),
            'attachments': Attachment.objects.filter(data_object__in=uuids).count(),
            'history': ActionHistory.objects.filter(data_object__in=uuids).count(),
        }

    def validate_parent(self, new_parent):
        """
        Проверяет, что new_parent можно назначить родителем без образования цикла.
        Бросает ValidationError; вызывается из представлений и clean().
        """
        if new_parent is None:
            return
        if new_parent.pk == self.pk:
            raise ValidationError("Объект не может быть родителем самого себя.")
        if new_parent.pk in self.get_descendant_uuids():
            raise ValidationError("Нельзя назначить родителем собственный дочерний объект: образуется цикл.")

    def clean(self):
        super().clean()
        if self.pk:
            self.validate_parent(self.parent)


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
    MAINTENANCE_KIND_CHOICES = [
        ('planned', 'Плановое обслуживание'),
        ('unplanned', 'Внеплановое обслуживание / ремонт'),
    ]

    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Исполнитель")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='actions', verbose_name="Объект")
    action = models.TextField(verbose_name="Описание действия")
    action_type = models.CharField(max_length=30, choices=ACTION_TYPE_CHOICES, default='other', db_index=True, verbose_name="Тип действия")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата создания")
    youtrack_id = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="ID записи в YouTrack")

    # Тип обслуживания хранится отдельным полем, а не выводится из текста
    # описания: статистика плана не должна зависеть от формулировок.
    maintenance_kind = models.CharField(
        max_length=20, choices=MAINTENANCE_KIND_CHOICES, blank=True, null=True, db_index=True,
        verbose_name="Вид обслуживания",
    )
    # Плановая дата, которую закрыла эта запись. Пара (объект, planned_for) —
    # идентификатор планового события: повторные работы по одному событию
    # не превращаются в несколько выполненных планов.
    planned_for = models.DateField(
        blank=True, null=True, db_index=True,
        verbose_name="Закрытая плановая дата",
    )

    class Meta:
        db_table = 'action_history'
        verbose_name = 'История действия'
        verbose_name_plural = 'История объектов'
        indexes = [
            models.Index(fields=['maintenance_kind', 'planned_for'], name='ah_plan_event_idx'),
        ]

    @property
    def is_planned_maintenance(self):
        return self.action_type == 'maintenance' and self.maintenance_kind == 'planned'


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

    def can_be_edited_by(self, user):
        """Править комментарий может его автор или администратор."""
        if not user or not user.is_authenticated:
            return False
        return self.user_id == user.pk or user.is_admin_or_higher


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
        # SVG намеренно отсутствует: такой файл отдаётся с нашего origin и
        # может выполнить скрипт. Загрузка SVG запрещена в data.validators.
        return ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']


@receiver(post_delete, sender=Attachment)
def delete_attachment_file(sender, instance, **kwargs):
    """Автоматически удаляет физический файл с диска при удалении записи Attachment из БД"""
    if instance.path and os.path.isfile(instance.path.path):
        try:
            os.remove(instance.path.path)
        except Exception:
            pass
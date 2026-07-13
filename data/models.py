import uuid
from django.db import models
from django.utils import timezone
from django.conf import settings

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
    next_maintenance_date = models.DateTimeField(blank=True, null=True, verbose_name="Дата следующего обслуживания")
    description = models.TextField(blank=True, null=True, verbose_name="Описание")

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
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Исполнитель")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='actions', verbose_name = "Объект")
    action = models.TextField(verbose_name="Описание действия")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата создания")

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

    class Meta:
        db_table = 'comment'
        verbose_name = 'Комментарий'
        verbose_name_plural = 'Комментарии'

    
class Attachment(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Загрузил")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='attachments', verbose_name="Объект")
    path = models.FileField(upload_to='attachments/', verbose_name="Файл")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата загрузки")
    is_preview = models.BooleanField(default=False, verbose_name="Превью (фото)")

    class Meta:
        db_table = 'attachment'
        verbose_name = 'Вложение'
        verbose_name_plural = 'Вложения'
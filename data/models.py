from django.db import models
from django.utils import timezone

class ObjectType(models.Model):
    id = models.AutoField(primary_key=True, verbose_name="ID Типа объекта")
    type = models.CharField(max_length=100, unique=True, verbose_name="Название объекта")

    class Meta:
        db_table = 'object_type'
        verbose_name = 'Тип объекта'
        verbose_name_plural = 'Типы объектов'

    def __str__(self):
        return self.type


class DependencyType(models.Model):
    id = models.AutoField(primary_key=True, verbose_name="ID Типа связи")
    type = models.CharField(max_length=100, unique=True, verbose_name="Тип зависимости")

    class Meta:
        db_table = 'dependency_type'
        verbose_name = 'Тип зависимости'
        verbose_name_plural = 'Типы зависимостей'

    def __str__(self):
        return self.type  


class ObjectModel(models.Model):
    id = models.AutoField(primary_key=True, verbose_name="ID Модели")
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
    id = models.AutoField(primary_key=True, verbose_name="ID Объекта")
    model = models.ForeignKey(ObjectModel, on_delete=models.CASCADE, related_name='data_objects', verbose_name="Модель")
    name = models.CharField(max_length=255, verbose_name="Имя объекта", blank=True, null=True)
    next_maintenance_date = models.DateTimeField(blank=True, null=True, verbose_name="Дата следующего обслуживания")

    class Meta:
        db_table = 'data_object'
        verbose_name = 'Объект данных'
        verbose_name_plural = 'Объекты данных'

    def __str__(self):
        display_name = self.name if self.name else self.model.name
        return f"ID: {self.id} | {display_name}"
         
    
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
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='actions', verbose_name = "Объект")
    action_date = models.DateTimeField(default=timezone.now, verbose_name="Дата и время действия")
    action = models.TextField(verbose_name="Описание действия")
    pk = models.CompositePrimaryKey('data_object', 'action_date')

    class Meta:
        db_table = 'action_history'
        verbose_name = 'История действия'
        verbose_name_plural = 'История объектов'

    def __str__(self):
        return f"{self.action_date.strftime('%d.%m.%Y %H:%M')} - {self.data_object}"    

    
class Comment(models.Model):
    id = models.AutoField(primary_key=True, verbose_name="ID Комментария")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='comments', verbose_name="Объект")
    text = models.TextField(verbose_name="Текст комментария")

    class Meta:
        db_table = 'comment'
        verbose_name = 'Комментарий'
        verbose_name_plural = 'Комментарии'

    def __str__(self):
        return f"Комментарий к {self.data_object.id}"
    
class Attachment(models.Model):
    id = models.AutoField(primary_key=True, verbose_name="ID Вложения")
    data_object = models.ForeignKey(DataObject, on_delete=models.CASCADE, related_name='attachments', verbose_name="Объект")
    path = models.FileField(upload_to='data_objects/attachments/', verbose_name="Файл")

    class Meta:
        db_table = 'attachment'
        verbose_name = 'Вложение'
        verbose_name_plural = 'Вложения'

    def __str__(self):
        return f"Вложение для {self.data_object.id}"
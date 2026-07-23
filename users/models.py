import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email обязателен')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'superuser')
        return self.create_user(email, password, **extra_fields)

class User(AbstractUser):
    ROLE_CHOICES = [
        ('junior', 'Младший инженер'),
        ('senior', 'Старший инженер'),
        ('admin', 'Администратор'),
        ('superuser', 'Суперпользователь'),
    ]

    AUTH_SOURCE_CHOICES = [
        ('django', 'Система'),
        ('ldap', 'LDAP'),
    ]

    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, verbose_name="Email адрес")
    username = models.CharField(max_length=150, blank=True, null=True)

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='junior',
        verbose_name="Роль в системе"
    )
    auth_source = models.CharField(
        max_length=10,
        choices=AUTH_SOURCE_CHOICES,
        default='django',
        verbose_name="Способ авторизации"
    )

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        db_table = 'app_user'
        verbose_name = 'Пользователь'
        verbose_name_plural = 'Пользователи'

    def __str__(self):
        return self.email

    @property
    def is_junior(self):
        return self.role == 'junior'

    @property
    def is_senior(self):
        return self.role == 'senior'

    @property
    def is_admin_or_higher(self):
        return self.role in ['admin', 'superuser'] or self.is_superuser

    @property
    def can_manage_content(self):
        return self.role in ['senior', 'admin', 'superuser'] or self.is_superuser
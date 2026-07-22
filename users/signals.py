from django_auth_ldap.backend import populate_user
from django.dispatch import receiver

@receiver(populate_user)
def set_user_ldap_defaults(sender, user, ldap_user, **kwargs):
    """
    Вызывается каждый раз, когда пользователь успешно авторизуется через LDAP.
    """
    # Всегда принудительно ставим источник авторизации LDAP
    user.auth_source = 'ldap'
    
    # Флаг user._state.adding равен True, только если запись пользователя СЕЙЧАС создается в БД.
    # Это гарантирует, что дефолтная роль 'junior' назначится один раз при первой авторизации,
    # а последующие изменения роли администратором не будут затёрты.
    if user._state.adding:
        user.role = 'junior'
        # По желанию можно сгенерировать случайное имя пользователя (username), 
        # если оно требуется моделью AbstractUser, но не пришло из LDAP:
        if not user.username:
            user.username = user.email.split('@')[0]
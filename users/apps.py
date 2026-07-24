from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'users'

    def ready(self):
        from django.conf import settings
        if getattr(settings, 'USE_LDAP', False):
            try:
                import users.signals
            except ImportError:
                pass
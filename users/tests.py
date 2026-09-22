"""
Автотесты приложения users: модель пользователя, вход в систему и
администрирование учётных записей.

Запуск:
    python manage.py test --settings=core.settings_test
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class UserModelTests(TestCase):
    """Кастомная модель пользователя и её менеджер."""

    def test_create_user_normalizes_email_and_hashes_password(self):
        user = User.objects.create_user(email='Ivan@Example.COM', username='ivan', password='pass12345')

        self.assertEqual(user.email, 'Ivan@example.com')
        self.assertNotEqual(user.password, 'pass12345')
        self.assertTrue(user.check_password('pass12345'))

    def test_create_user_without_email_is_rejected(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email='', password='pass12345')

    def test_new_user_is_junior_by_default(self):
        user = User.objects.create_user(email='new@test.local', username='new', password='pass12345')

        self.assertEqual(user.role, 'junior')
        self.assertEqual(user.auth_source, 'django')
        self.assertTrue(user.is_junior)
        self.assertFalse(user.can_manage_content)
        self.assertFalse(user.is_admin_or_higher)

    def test_create_superuser_gets_elevated_role(self):
        root = User.objects.create_superuser(email='root@test.local', username='root', password='pass12345')

        self.assertEqual(root.role, 'superuser')
        self.assertTrue(root.is_staff)
        self.assertTrue(root.is_superuser)
        self.assertTrue(root.is_admin_or_higher)

    def test_role_capability_flags(self):
        senior = User.objects.create_user(email='s@test.local', username='s', password='pass12345', role='senior')
        admin = User.objects.create_user(email='a@test.local', username='a', password='pass12345', role='admin')

        self.assertTrue(senior.can_manage_content)
        self.assertFalse(senior.is_admin_or_higher)
        self.assertTrue(admin.can_manage_content)
        self.assertTrue(admin.is_admin_or_higher)

    def test_email_is_the_login_field(self):
        self.assertEqual(User.USERNAME_FIELD, 'email')
        self.assertEqual(str(User(email='x@test.local')), 'x@test.local')


class LoginTests(TestCase):
    """Вход в систему."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email='user@test.local', username='user', password='pass12345'
        )

    def test_login_with_email_succeeds(self):
        response = self.client.post(reverse('login'), {
            'username': 'user@test.local', 'password': 'pass12345',
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_login_with_wrong_password_fails(self):
        response = self.client.post(reverse('login'), {
            'username': 'user@test.local', 'password': 'wrong',
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_session_expires_with_browser_when_remember_me_is_off(self):
        self.client.post(reverse('login'), {
            'username': 'user@test.local', 'password': 'pass12345',
        })
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_session_persists_when_remember_me_is_on(self):
        self.client.post(reverse('login'), {
            'username': 'user@test.local', 'password': 'pass12345', 'remember_me': 'on',
        })
        self.assertFalse(self.client.session.get_expire_at_browser_close())


class SessionInvalidationTests(TestCase):
    """Активная сессия не должна переживать блокировку или удаление аккаунта."""

    def test_blocked_user_loses_access_immediately(self):
        user = User.objects.create_user(email='b@test.local', username='b', password='pass12345')
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

        User.objects.filter(pk=user.pk).update(is_active=False)

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_deleted_user_loses_access_immediately(self):
        user = User.objects.create_user(email='d@test.local', username='d', password='pass12345')
        self.client.force_login(user)
        User.objects.filter(pk=user.pk).delete()

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_blocked_user_cannot_log_in_again(self):
        User.objects.create_user(
            email='blocked@test.local', username='blocked', password='pass12345', is_active=False
        )
        self.client.post(reverse('login'), {
            'username': 'blocked@test.local', 'password': 'pass12345',
        })
        self.assertNotIn('_auth_user_id', self.client.session)


class UserAdministrationTests(TestCase):
    """Управление учётными записями из раздела настроек."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            email='admin@test.local', username='admin', password='pass12345', role='admin'
        )
        cls.senior = User.objects.create_user(
            email='senior@test.local', username='senior', password='pass12345', role='senior'
        )
        cls.target = User.objects.create_user(
            email='target@test.local', username='target', password='pass12345', role='junior'
        )

    def test_admin_creates_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:create_user'), {
            'email': 'fresh@test.local', 'username': 'Фёдоров Ф.Ф.', 'password': 'StrongPass!42',
        })

        self.assertEqual(response.status_code, 200)
        created = User.objects.get(email='fresh@test.local')
        self.assertEqual(created.username, 'Фёдоров Ф.Ф.')
        self.assertEqual(created.role, 'junior')

    def test_username_defaults_to_email_local_part(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('users:create_user'), {
            'email': 'noname@test.local', 'password': 'StrongPass!42',
        })

        self.assertEqual(User.objects.get(email='noname@test.local').username, 'noname')

    def test_senior_cannot_create_user(self):
        self.client.force_login(self.senior)
        response = self.client.post(reverse('users:create_user'), {
            'email': 'hack@test.local', 'password': 'StrongPass!42',
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email='hack@test.local').exists())

    def test_admin_changes_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:update_role', args=[self.target.uuid]), {'role': 'senior'})

        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, 'senior')

    def test_unknown_role_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:update_role', args=[self.target.uuid]), {'role': 'god'})

        self.assertEqual(response.status_code, 400)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, 'junior')

    def test_admin_cannot_change_own_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:update_role', args=[self.admin.uuid]), {'role': 'junior'})

        self.assertEqual(response.status_code, 403)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'admin')

    def test_admin_blocks_and_unblocks_user(self):
        self.client.force_login(self.admin)

        self.client.post(reverse('users:toggle_status', args=[self.target.uuid]))
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)

        self.client.post(reverse('users:toggle_status', args=[self.target.uuid]))
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_admin_cannot_block_self(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('users:toggle_status', args=[self.admin.uuid]))

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_delete_user_requires_post(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('users:delete_user', args=[self.target.uuid]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())

    def test_admin_deletes_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:delete_user', args=[self.target.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.target.pk).exists())

    def test_admin_cannot_delete_self(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('users:delete_user', args=[self.admin.uuid]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())


class YoutrackTokenTests(TestCase):
    """Персональный токен YouTrack в профиле."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='u@test.local', username='u', password='pass12345')

    def test_token_is_saved(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('users:profile_youtrack_token'), {'youtrack_token': 'perm:abc'})

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.youtrack_token, 'perm:abc')

    def test_empty_token_clears_the_field(self):
        self.user.youtrack_token = 'perm:abc'
        self.user.save(update_fields=['youtrack_token'])

        self.client.force_login(self.user)
        self.client.post(reverse('users:profile_youtrack_token'), {'youtrack_token': '  '})

        self.user.refresh_from_db()
        self.assertIsNone(self.user.youtrack_token)

    def test_anonymous_cannot_open_token_modal(self):
        response = self.client.get(reverse('users:profile_youtrack_token'))
        self.assertEqual(response.status_code, 302)

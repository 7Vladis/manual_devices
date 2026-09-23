"""
Автотесты приложения notifications: выбор webhook, отправка уведомлений
и ежедневная проверка сроков ТО.

Запуск:
    python manage.py test
"""

import datetime
import json

import requests
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from data.models import DataObject, ObjectModel, ObjectType

from .models import MattermostSetting
from .services import send_mattermost_notification, test_specific_webhook
from .tasks import format_object_list, run_daily_maintenance_check

User = get_user_model()


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class WebhookServiceTests(TestCase):
    """Отправка сообщений в Mattermost."""

    def test_no_active_webhook_reports_failure(self):
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a', is_active=False)

        success, message = send_mattermost_notification('текст')

        self.assertFalse(success)
        self.assertIn('webhook', message.lower())

    @patch('notifications.services.requests.post', return_value=FakeResponse(200))
    def test_active_webhook_receives_payload(self, mock_post):
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')

        success, message = send_mattermost_notification('Проверка')

        self.assertTrue(success)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.args[0], 'https://mm.test/hook/a')
        payload = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(payload['text'], 'Проверка')
        self.assertEqual(payload['username'], 'Диспетчер')

    @patch('notifications.services.requests.post', return_value=FakeResponse(503))
    def test_non_200_response_is_reported(self, mock_post):
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')

        success, message = send_mattermost_notification('Проверка')

        self.assertFalse(success)
        self.assertIn('503', message)

    @patch('notifications.services.requests.post',
           side_effect=requests.ConnectionError('нет сети'))
    def test_network_error_is_caught(self, mock_post):
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')

        success, message = send_mattermost_notification('Проверка')

        self.assertFalse(success)
        self.assertIn('нет сети', message)

    @patch('notifications.services.requests.post', return_value=FakeResponse(200))
    def test_specific_webhook_is_tested_by_id(self, mock_post):
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/b')

        success, _ = test_specific_webhook(config.uuid)

        self.assertTrue(success)
        self.assertEqual(mock_post.call_args.args[0], 'https://mm.test/hook/b')

    def test_missing_webhook_id_is_reported(self):
        success, message = test_specific_webhook('00000000-0000-0000-0000-000000000000')

        self.assertFalse(success)
        self.assertIn('не найдена', message)


class DailyCheckTests(TestCase):
    """Ежедневная проверка приближающихся сроков ТО."""

    @classmethod
    def setUpTestData(cls):
        object_type = ObjectType.objects.create(type='Насос')
        cls.model = ObjectModel.objects.create(object_type=object_type, name='НМ-125')

    def _make_object(self, name, days_ahead, inventory_number=None):
        return DataObject.objects.create(
            model=self.model,
            name=name,
            inventory_number=inventory_number,
            next_maintenance_date=timezone.localdate() + datetime.timedelta(days=days_ahead),
        )

    def test_format_object_list_includes_model_and_inventory(self):
        obj = self._make_object('Насос А', 0, inventory_number='ИНВ-1')

        line = format_object_list([obj])

        self.assertIn('Насос А', line)
        self.assertIn('НМ-125', line)
        self.assertIn('ИНВ-1', line)

    def test_object_without_inventory_number_is_marked(self):
        obj = self._make_object('Безномерной', 0)
        self.assertIn('Инв. №: нет', format_object_list([obj]))

    @patch('notifications.tasks.send_mattermost_notification', return_value=(True, 'Успешно'))
    def test_notifications_are_sent_for_today_week_and_month(self, mock_send):
        self._make_object('Сегодня', 0)
        self._make_object('Через неделю', 7)
        self._make_object('Через месяц', 30)

        run_daily_maintenance_check()

        self.assertEqual(mock_send.call_count, 3)
        messages = ' '.join(call.args[0] for call in mock_send.call_args_list)
        self.assertIn('Сегодня', messages)
        self.assertIn('Через неделю', messages)
        self.assertIn('Через месяц', messages)

    @patch('notifications.tasks.send_mattermost_notification', return_value=(True, 'Успешно'))
    def test_nothing_is_sent_without_due_objects(self, mock_send):
        self._make_object('Далеко', 3)

        run_daily_maintenance_check()

        mock_send.assert_not_called()

    @patch('notifications.tasks.send_mattermost_notification', return_value=(True, 'Успешно'))
    def test_overdue_objects_do_not_trigger_notifications(self, mock_send):
        """Задача уведомляет о точных датах: сегодня, +7 и +30 дней."""
        self._make_object('Просрочен', -5)

        run_daily_maintenance_check()

        mock_send.assert_not_called()


class MaintenanceDigestCommandTests(TestCase):
    """BUG-010: задача доступна как management-команда и не зависит от web-процесса."""

    @classmethod
    def setUpTestData(cls):
        object_type = ObjectType.objects.create(type='Насос')
        cls.model = ObjectModel.objects.create(object_type=object_type, name='НМ-125')

    def _make_object(self, name, days_ahead):
        return DataObject.objects.create(
            model=self.model, name=name,
            next_maintenance_date=timezone.localdate() + datetime.timedelta(days=days_ahead),
        )

    def test_dry_run_prints_messages_without_sending(self):
        self._make_object('Сегодня', 0)
        out = StringIO()

        with patch('notifications.tasks.send_mattermost_notification') as mock_send:
            call_command('send_maintenance_digest', '--dry-run', stdout=out)

        mock_send.assert_not_called()
        output = out.getvalue()
        self.assertIn('--dry-run', output)
        self.assertIn('Сегодня', output)

    def test_command_sends_messages(self):
        self._make_object('Сегодня', 0)

        with patch('notifications.tasks.send_mattermost_notification', return_value=(True, 'ok')) as mock_send:
            call_command('send_maintenance_digest', stdout=StringIO())

        self.assertEqual(mock_send.call_count, 1)

    def test_custom_date_shifts_the_window(self):
        target = timezone.localdate() + datetime.timedelta(days=40)
        DataObject.objects.create(model=self.model, name='Далёкий', next_maintenance_date=target)
        out = StringIO()

        # Для даты «за 30 дней до» объект попадает в сводку
        call_command('send_maintenance_digest', '--dry-run',
                     '--date', (target - datetime.timedelta(days=30)).isoformat(), stdout=out)

        self.assertIn('Далёкий', out.getvalue())

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command('send_maintenance_digest', '--date', '01.10.2026', stdout=StringIO())

    def test_nothing_to_send_is_reported(self):
        out = StringIO()
        with patch('notifications.tasks.send_mattermost_notification') as mock_send:
            call_command('send_maintenance_digest', stdout=out)

        mock_send.assert_not_called()
        self.assertIn('не найдено', out.getvalue())

    def test_send_failure_is_logged_not_swallowed(self):
        self._make_object('Сегодня', 0)

        with patch('notifications.tasks.send_mattermost_notification', return_value=(False, 'webhook отвалился')):
            with self.assertLogs('notifications', level='ERROR') as logs:
                call_command('send_maintenance_digest', stdout=StringIO())

        self.assertTrue(any('webhook отвалился' in line for line in logs.output))


class SchedulerProcessTests(TestCase):
    """BUG-010/023: планировщик вынесен из web-процесса и имеет явную таймзону."""

    def test_app_config_does_not_start_scheduler(self):
        import notifications.apps as apps_module

        source = Path(apps_module.__file__).read_text(encoding='utf-8')
        self.assertNotIn('start_scheduler', source)
        self.assertNotIn('RUN_MAIN', source)

    def test_run_scheduler_command_exists(self):
        from notifications.management.commands import run_scheduler

        self.assertTrue(hasattr(run_scheduler, 'Command'))

    def test_scheduler_uses_project_timezone_and_guards(self):
        from notifications.management.commands.run_scheduler import Command

        created = {}

        class FakeScheduler:
            def __init__(self, timezone=None):
                created['timezone'] = timezone
                self.running = False

            def add_jobstore(self, *a, **kw):
                pass

            def add_job(self, func, **kwargs):
                created['job'] = kwargs
                created['func'] = func

            def start(self):
                created['started'] = True

            def shutdown(self, wait=True):
                pass

        with patch('notifications.management.commands.run_scheduler.BlockingScheduler', FakeScheduler):
            with patch('notifications.management.commands.run_scheduler.DjangoJobStore'):
                call_command('run_scheduler', stdout=StringIO())

        self.assertEqual(created['timezone'], settings.TIME_ZONE)
        self.assertTrue(created['started'])
        self.assertEqual(created['job']['max_instances'], 1)
        self.assertTrue(created['job']['coalesce'])
        self.assertEqual(created['func'], run_daily_maintenance_check)


class WebhookViewTests(TestCase):
    """Управление webhook из настроек."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            email='admin@test.local', username='admin', password='pass12345', role='admin'
        )
        cls.senior = User.objects.create_user(
            email='senior@test.local', username='senior', password='pass12345', role='senior'
        )

    @patch('notifications.views.test_specific_webhook', return_value=(False, '<script>alert(1)</script>'))
    def test_webhook_test_result_is_escaped(self, _mock):
        """BUG-003: текст ошибки стороннего сервиса не должен исполняться как HTML."""
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(self.admin)

        response = self.client.post(reverse('test_webhook', args=[config.uuid]))

        body = response.content.decode()
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)

    def test_get_does_not_toggle_webhook(self):
        """BUG-005: активность webhook не переключается GET-запросом."""
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(self.admin)

        response = self.client.get(reverse('activate_webhook', args=[config.uuid]))

        self.assertEqual(response.status_code, 405)
        config.refresh_from_db()
        self.assertTrue(config.is_active)

    def test_get_does_not_send_test_message(self):
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(self.admin)
        with patch('notifications.views.test_specific_webhook') as mock_test:
            response = self.client.get(reverse('test_webhook', args=[config.uuid]))
        self.assertEqual(response.status_code, 405)
        mock_test.assert_not_called()

    def test_admin_adds_webhook(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('add_webhook'), {'webhook_url': 'https://mm.test/hook/new'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(MattermostSetting.objects.filter(webhook_url='https://mm.test/hook/new').exists())

    def test_senior_cannot_add_webhook(self):
        self.client.force_login(self.senior)
        response = self.client.post(reverse('add_webhook'), {'webhook_url': 'https://mm.test/hook/x'})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(MattermostSetting.objects.exists())

    def test_admin_toggles_webhook_activity(self):
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(self.admin)

        self.client.post(reverse('activate_webhook', args=[config.uuid]))
        config.refresh_from_db()
        self.assertFalse(config.is_active)

    def test_admin_deletes_webhooks(self):
        config = MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(self.admin)

        self.client.post(reverse('delete_webhooks'), {'webhook_ids': [str(config.uuid)]})

        self.assertFalse(MattermostSetting.objects.exists())


class MultipleWebhookTests(TestCase):
    """BUG-022: при нескольких активных webhook адресат выбирался произвольно."""

    def test_message_goes_to_every_active_webhook(self):
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/b')
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/off', is_active=False)

        with patch('notifications.services.requests.post', return_value=FakeResponse(200)) as mock_post:
            success, message = send_mattermost_notification('Проверка')

        self.assertTrue(success)
        self.assertEqual(mock_post.call_count, 2)
        urls = {call.args[0] for call in mock_post.call_args_list}
        self.assertEqual(urls, {'https://mm.test/hook/a', 'https://mm.test/hook/b'})
        self.assertIn('2 из 2', message)

    def test_partial_failure_is_reported(self):
        MattermostSetting.objects.create(webhook_url='https://mm.test/ok')
        MattermostSetting.objects.create(webhook_url='https://mm.test/bad')

        def post(url, **kwargs):
            return FakeResponse(200 if url.endswith('/ok') else 500)

        with patch('notifications.services.requests.post', side_effect=post):
            success, message = send_mattermost_notification('Проверка')

        self.assertTrue(success)
        self.assertIn('Доставлено 1 из 2', message)
        self.assertIn('500', message)

    def test_all_failures_report_error(self):
        MattermostSetting.objects.create(webhook_url='https://mm.test/bad')

        with patch('notifications.services.requests.post', return_value=FakeResponse(503)):
            success, message = send_mattermost_notification('Проверка')

        self.assertFalse(success)
        self.assertIn('503', message)

    def test_newest_configuration_comes_first(self):
        old = MattermostSetting.objects.create(webhook_url='https://mm.test/old')
        new = MattermostSetting.objects.create(webhook_url='https://mm.test/new')

        self.assertEqual(list(MattermostSetting.objects.all()), [new, old])

    def test_duplicate_webhook_is_rejected(self):
        admin = User.objects.create_user(
            email='admin2@test.local', username='admin2', password='pass12345', role='admin'
        )
        MattermostSetting.objects.create(webhook_url='https://mm.test/hook/a')
        self.client.force_login(admin)

        response = self.client.post(reverse('add_webhook'), {'webhook_url': 'https://mm.test/hook/a'})

        self.assertIn('уже добавлен', response.content.decode())
        self.assertEqual(MattermostSetting.objects.count(), 1)

    def test_invalid_url_is_rejected(self):
        admin = User.objects.create_user(
            email='admin3@test.local', username='admin3', password='pass12345', role='admin'
        )
        self.client.force_login(admin)

        response = self.client.post(reverse('add_webhook'), {'webhook_url': 'не-адрес'})

        self.assertEqual(MattermostSetting.objects.count(), 0)
        self.assertIn('alert-danger', response.content.decode())

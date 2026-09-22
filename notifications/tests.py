"""
Автотесты приложения notifications: выбор webhook, отправка уведомлений
и ежедневная проверка сроков ТО.

Запуск:
    python manage.py test --settings=core.settings_test
"""

import datetime
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
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

    @patch('notifications.services.requests.post', side_effect=OSError('нет сети'))
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

    @patch('notifications.tasks.send_mattermost_notification')
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

    @patch('notifications.tasks.send_mattermost_notification')
    def test_nothing_is_sent_without_due_objects(self, mock_send):
        self._make_object('Далеко', 3)

        run_daily_maintenance_check()

        mock_send.assert_not_called()

    @patch('notifications.tasks.send_mattermost_notification')
    def test_overdue_objects_do_not_trigger_notifications(self, mock_send):
        """Задача уведомляет о точных датах: сегодня, +7 и +30 дней."""
        self._make_object('Просрочен', -5)

        run_daily_maintenance_check()

        mock_send.assert_not_called()


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

        response = self.client.get(reverse('test_webhook', args=[config.uuid]))

        body = response.content.decode()
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)

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

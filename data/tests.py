"""
Автотесты приложения data.

Запуск:
    python manage.py test

Набор покрывает то, что сейчас считается корректным поведением: модели и их
связи, разграничение доступа по ролям, расчёт срока ТО, дерево объектов,
вкладки карточки, комментарии и вложения, клонирование, поиск и экспорт.
"""

import datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActionHistory,
    Attachment,
    Comment,
    DataObject,
    DateUpdateRule,
    ObjectModel,
    ObjectType,
    YouTrackJob,
)
from .services.youtrack_queue import process_jobs
from .templatetags.markdown_extras import markdown_format
from .views import calculate_next_maintenance_date, get_ancestors_chain

User = get_user_model()

# Минимальные байты с корректной сигнатурой PNG — валидатор проверяет содержимое файла.
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class BaseDataTestCase(TestCase):
    """Общая фикстура: пользователи всех ролей и минимальное дерево объектов."""

    @classmethod
    def setUpTestData(cls):
        cls.junior = User.objects.create_user(
            email='junior@test.local', username='junior', password='pass12345', role='junior'
        )
        cls.senior = User.objects.create_user(
            email='senior@test.local', username='senior', password='pass12345', role='senior'
        )
        cls.admin = User.objects.create_user(
            email='admin@test.local', username='admin', password='pass12345', role='admin'
        )

        cls.object_type = ObjectType.objects.create(type='Насос')
        cls.model = ObjectModel.objects.create(
            object_type=cls.object_type,
            name='НМ-125',
            specifications={'Мощность': '5 кВт', 'Вес': '80 кг'},
        )
        cls.root = DataObject.objects.create(
            model=cls.model, name='Насосная станция №1', inventory_number='ИНВ-001'
        )
        cls.child = DataObject.objects.create(
            model=cls.model, name='Насос А', parent=cls.root, inventory_number='ИНВ-002'
        )

    def login(self, user):
        self.client.force_login(user)


class ModelLayerTests(BaseDataTestCase):
    """Поведение моделей предметной области."""

    def test_object_str_falls_back_to_model_and_inventory(self):
        nameless = DataObject.objects.create(model=self.model, inventory_number='ИНВ-777')
        self.assertEqual(str(nameless), 'НМ-125 (ИНВ-777)')
        self.assertEqual(str(self.root), 'Насосная станция №1')

    def test_effective_youtrack_issue_is_inherited_from_parent(self):
        self.root.youtrack_issue_id = 'MNT-1'
        self.root.save(update_fields=['youtrack_issue_id'])

        issue_id, target = self.child.get_effective_youtrack_issue()
        self.assertEqual(issue_id, 'MNT-1')
        self.assertEqual(target, self.root)

    def test_effective_youtrack_issue_is_none_without_task_in_branch(self):
        self.assertEqual(self.child.get_effective_youtrack_issue(), (None, None))

    def test_effective_youtrack_issue_survives_parent_cycle(self):
        """Обход цепочки родителей не должен зацикливаться при битых данных."""
        DataObject.objects.filter(pk=self.root.pk).update(parent=self.child)
        broken_child = DataObject.objects.get(pk=self.child.pk)

        self.assertEqual(broken_child.get_effective_youtrack_issue(), (None, None))

    def test_attachment_is_image_detection(self):
        att = Attachment(data_object=self.root)
        att.path.name = 'attachments/x/photo.PNG'
        self.assertTrue(att.is_image)

        att.path.name = 'attachments/x/passport.pdf'
        self.assertFalse(att.is_image)

    def test_attachment_filename_strips_directories(self):
        att = Attachment(data_object=self.root)
        att.path.name = 'attachments/station_1/scheme.pdf'
        self.assertEqual(att.filename, 'scheme.pdf')


class AncestorsChainTests(BaseDataTestCase):
    """Хлебные крошки карточки объекта."""

    def test_chain_is_ordered_from_root(self):
        grandchild = DataObject.objects.create(model=self.model, name='Подшипник', parent=self.child)
        self.assertEqual(get_ancestors_chain(grandchild), [self.root, self.child])

    def test_root_object_has_empty_chain(self):
        self.assertEqual(get_ancestors_chain(self.root), [])

    def test_chain_does_not_hang_on_cycle(self):
        DataObject.objects.filter(pk=self.root.pk).update(parent=self.child)
        obj = DataObject.objects.get(pk=self.child.pk)

        chain = get_ancestors_chain(obj)
        self.assertLessEqual(len(chain), 2)


class MaintenanceCalculationTests(BaseDataTestCase):
    """Расчёт даты следующего ТО по правилам."""

    def test_no_rule_returns_none(self):
        self.assertIsNone(calculate_next_maintenance_date(self.root))

    def test_relative_rule_adds_interval_to_base_date(self):
        rule = DateUpdateRule.objects.create(
            name='Раз в полгода',
            rule={'strategy': 'relative', 'anchor': 'actual', 'value': {'years': 0, 'months': 6, 'days': 0}},
        )
        self.root.date_update_rule = rule

        result = calculate_next_maintenance_date(self.root, base_date=datetime.date(2026, 1, 31))
        self.assertEqual(result, datetime.date(2026, 7, 31))

    def test_relative_rule_anchored_to_scheduled_date(self):
        rule = DateUpdateRule.objects.create(
            name='Год от плана',
            rule={'strategy': 'relative', 'anchor': 'scheduled', 'value': {'years': 1, 'months': 0, 'days': 0}},
        )
        self.root.date_update_rule = rule
        self.root.next_maintenance_date = datetime.date(2026, 3, 10)

        result = calculate_next_maintenance_date(self.root, base_date=datetime.date(2026, 9, 1))
        self.assertEqual(result, datetime.date(2027, 3, 10))

    def test_fixed_rule_picks_next_date_in_year(self):
        rule = DateUpdateRule.objects.create(
            name='Сезонное',
            rule={'strategy': 'fixed', 'anchor': 'yearly', 'value': [{'month': 4, 'day': 15}, {'month': 10, 'day': 15}]},
        )
        self.root.date_update_rule = rule

        result = calculate_next_maintenance_date(self.root, base_date=datetime.date(2026, 5, 1))
        self.assertEqual(result, datetime.date(2026, 10, 15))

    def test_fixed_rule_rolls_over_to_next_year(self):
        rule = DateUpdateRule.objects.create(
            name='Одна дата',
            rule={'strategy': 'fixed', 'anchor': 'yearly', 'value': [{'month': 2, 'day': 1}]},
        )
        self.root.date_update_rule = rule

        result = calculate_next_maintenance_date(self.root, base_date=datetime.date(2026, 6, 1))
        self.assertEqual(result, datetime.date(2027, 2, 1))


class MarkdownFilterTests(TestCase):
    """Фильтр рендера Markdown в комментариях и описаниях."""

    def test_script_tag_is_stripped(self):
        """BUG-001: stored XSS через Markdown."""
        rendered = markdown_format('<script>alert(1)</script>привет')
        self.assertNotIn('<script', rendered)
        self.assertIn('привет', rendered)

    def test_event_handler_attributes_are_stripped(self):
        rendered = markdown_format('<img src="/media/a.png" onerror="alert(1)">')
        self.assertNotIn('onerror', rendered)

    def test_javascript_links_are_neutralized(self):
        rendered = markdown_format('[клик](javascript:alert(1))')
        self.assertNotIn('javascript:', rendered)

    def test_svg_and_iframe_are_removed(self):
        rendered = markdown_format('<svg onload="alert(1)"></svg><iframe src="//evil"></iframe>')
        self.assertNotIn('<svg', rendered)
        self.assertNotIn('<iframe', rendered)

    def test_external_links_get_noopener(self):
        rendered = markdown_format('[сайт](https://example.com)')
        self.assertIn('href="https://example.com"', rendered)
        self.assertIn('noopener', rendered)

    def test_image_from_foreign_relative_path_is_dropped(self):
        rendered = markdown_format('<img src="/settings/users/">')
        self.assertNotIn('src=', rendered)

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(markdown_format(''), '')
        self.assertEqual(markdown_format(None), '')

    def test_basic_markdown_is_rendered(self):
        self.assertIn('<strong>важно</strong>', markdown_format('**важно**'))

    def test_relative_image_tags_are_stripped(self):
        rendered = markdown_format('Текст ![](photo.jpg) дальше')
        self.assertNotIn('<img', rendered)

    def test_absolute_media_images_are_kept(self):
        rendered = markdown_format('![](/media/attachments/photo.jpg)')
        self.assertIn('<img', rendered)


class AccessControlTests(BaseDataTestCase):
    """Разграничение доступа по ролям."""

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('dict'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_junior_cannot_open_settings(self):
        self.login(self.junior)
        response = self.client.get(reverse('settings_page'))
        self.assertEqual(response.status_code, 403)

    def test_senior_can_open_settings(self):
        self.login(self.senior)
        response = self.client.get(reverse('settings_page'))
        self.assertEqual(response.status_code, 200)

    def test_senior_cannot_open_users_tab(self):
        """Вкладка пользователей доступна только администраторам."""
        self.login(self.senior)
        response = self.client.get(reverse('settings_page'), {'tab': 'users'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_tab'], 'rules')

    def test_junior_cannot_create_object(self):
        self.login(self.junior)
        response = self.client.post(reverse('create_object'), {'name': 'X'})
        self.assertEqual(response.status_code, 403)

    def test_junior_cannot_delete_object(self):
        self.login(self.junior)
        response = self.client.post(reverse('delete_object', args=[self.child.uuid]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(DataObject.objects.filter(pk=self.child.pk).exists())

    def test_junior_cannot_export_xlsx(self):
        self.login(self.junior)
        response = self.client.get(reverse('export_xlsx'))
        self.assertEqual(response.status_code, 403)

    def test_htmx_forbidden_response_is_an_inline_banner(self):
        self.login(self.junior)
        response = self.client.post(
            reverse('delete_object', args=[self.child.uuid]), HTTP_HX_REQUEST='true'
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('alert-danger', response.content.decode())


class ObjectCreationTests(BaseDataTestCase):
    """Создание объектов через модальное окно."""

    def test_object_is_created_with_history_record(self):
        self.login(self.senior)
        response = self.client.post(reverse('create_object'), {
            'name': 'Новый насос',
            'model': str(self.model.uuid),
            'inventory_number': 'ИНВ-100',
            'parent': str(self.root.uuid),
            'maintenance_scheduling_mode': 'manual',
            'next_maintenance_date': '2026-12-01',
        })

        self.assertEqual(response.status_code, 200)
        created = DataObject.objects.get(name='Новый насос')
        self.assertEqual(created.parent, self.root)
        self.assertEqual(created.next_maintenance_date, datetime.date(2026, 12, 1))
        self.assertTrue(created.actions.filter(action_type='create').exists())

    def test_auto_mode_computes_first_maintenance_date(self):
        rule = DateUpdateRule.objects.create(
            name='Каждые 30 дней',
            rule={'strategy': 'relative', 'anchor': 'actual', 'value': {'years': 0, 'months': 0, 'days': 30}},
        )
        self.login(self.senior)
        self.client.post(reverse('create_object'), {
            'name': 'Авто-объект',
            'model': str(self.model.uuid),
            'maintenance_scheduling_mode': 'auto',
            'date_update_rule': str(rule.uuid),
        })

        created = DataObject.objects.get(name='Авто-объект')
        expected = timezone.localdate() + datetime.timedelta(days=30)
        self.assertEqual(created.next_maintenance_date, expected)

    def test_missing_model_returns_readable_error_instead_of_crash(self):
        self.login(self.senior)
        response = self.client.post(reverse('create_object'), {'name': 'Без модели'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('модель', response.content.decode().lower())
        self.assertFalse(DataObject.objects.filter(name='Без модели').exists())

    def test_invalid_date_is_reported_instead_of_being_swallowed(self):
        """BUG-020: некорректная дата теперь сообщается, а не теряется молча."""
        self.login(self.senior)
        response = self.client.post(reverse('create_object'), {
            'name': 'Кривая дата',
            'model': str(self.model.uuid),
            'maintenance_scheduling_mode': 'manual',
            'next_maintenance_date': 'не-дата',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('ГГГГ-ММ-ДД', response.content.decode())
        self.assertFalse(DataObject.objects.filter(name='Кривая дата').exists())

    def test_empty_date_is_accepted(self):
        self.login(self.senior)
        self.client.post(reverse('create_object'), {
            'name': 'Без даты',
            'model': str(self.model.uuid),
            'maintenance_scheduling_mode': 'manual',
            'next_maintenance_date': '',
        })

        self.assertIsNone(DataObject.objects.get(name='Без даты').next_maintenance_date)


class ModelCreationTests(BaseDataTestCase):
    """Создание моделей оборудования."""

    def test_model_created_with_new_object_type_and_specs(self):
        self.login(self.senior)
        response = self.client.post(reverse('create_model'), {
            'name': 'ВК-25',
            'new_object_type': 'Вентилятор',
            'spec_keys': ['Мощность', 'Вес'],
            'spec_values': ['2 кВт', '30 кг'],
        })

        self.assertEqual(response.status_code, 200)
        created = ObjectModel.objects.get(name='ВК-25')
        self.assertEqual(created.object_type.type, 'Вентилятор')
        self.assertEqual(created.specifications, {'Мощность': '2 кВт', 'Вес': '30 кг'})

    def test_missing_object_type_returns_readable_error(self):
        self.login(self.senior)
        response = self.client.post(reverse('create_model'), {'name': 'Без типа'})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ObjectModel.objects.filter(name='Без типа').exists())


class ObjectCardTests(BaseDataTestCase):
    """Карточка объекта и её вкладки."""

    def test_card_renders_parent_pill_and_counters(self):
        self.login(self.senior)
        response = self.client.get(reverse('object_detail', args=[self.child.uuid]))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Насосная станция №1', body)  # поле «Входит в состав»
        self.assertIn('id="maintenance-pill"', body)
        self.assertIn('tab-count-comments', body)

    def test_card_has_no_duplicate_tree_actions(self):
        """Обслужить/копировать/удалить есть в дереве — в карточке их не дублируем."""
        self.login(self.senior)
        body = self.client.get(reverse('object_detail', args=[self.root.uuid])).content.decode()
        self.assertNotIn('obj-breadcrumb', body)
        self.assertNotIn('#confirmModal', body)
        self.assertNotIn('#cloneObjectModal', body)
        self.assertNotIn('#serviceModal', body)

    def test_card_with_youtrack_issue_schedules_background_sync(self):
        self.root.youtrack_issue_id = 'MNT-7'
        self.root.save(update_fields=['youtrack_issue_id'])

        self.login(self.senior)
        body = self.client.get(reverse('object_detail', args=[self.root.uuid])).content.decode()
        self.assertIn('id="yt-sync-status"', body)
        self.assertIn('hx-trigger="load"', body)

    def test_card_without_youtrack_issue_has_no_sync_trigger(self):
        self.login(self.senior)
        body = self.client.get(reverse('object_detail', args=[self.root.uuid])).content.decode()
        self.assertNotIn('yt-sync-status', body)

    def test_info_tab_does_not_duplicate_tree_children(self):
        self.login(self.junior)
        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'short_info'])).content.decode()
        self.assertNotIn('Состав объекта', body)

    def test_card_marks_overdue_maintenance_date(self):
        """BUG-018: просроченный срок ТО должен подсвечиваться."""
        self.root.next_maintenance_date = timezone.localdate() - datetime.timedelta(days=1)
        self.root.save(update_fields=['next_maintenance_date'])

        self.login(self.senior)
        response = self.client.get(reverse('object_detail', args=[self.root.uuid]))
        self.assertIn('pill-danger', response.content.decode())

    def test_card_marks_future_maintenance_date_as_ok(self):
        self.root.next_maintenance_date = timezone.localdate() + datetime.timedelta(days=200)
        self.root.save(update_fields=['next_maintenance_date'])

        self.login(self.senior)
        response = self.client.get(reverse('object_detail', args=[self.root.uuid]))
        body = response.content.decode()
        self.assertIn('pill-success', body)
        self.assertNotIn('pill-danger', body)

    def test_specs_tab_shows_model_specifications(self):
        self.login(self.junior)
        response = self.client.get(reverse('object_tab', args=[self.root.uuid, 'specs']))

        self.assertEqual(response.status_code, 200)
        self.assertIn('Мощность', response.content.decode())

    def test_specs_tab_survives_null_specifications(self):
        self.model.specifications = None
        self.model.save(update_fields=['specifications'])

        self.login(self.junior)
        response = self.client.get(reverse('object_tab', args=[self.root.uuid, 'specs']))
        self.assertEqual(response.status_code, 200)

    def test_unknown_tab_returns_404(self):
        self.login(self.junior)
        response = self.client.get(reverse('object_tab', args=[self.root.uuid, 'nope']))
        self.assertEqual(response.status_code, 404)


class InlineEditTests(BaseDataTestCase):
    """Инлайн-редактирование полей карточки."""

    def test_senior_can_rename_object_and_history_is_written(self):
        self.login(self.senior)
        response = self.client.post(reverse('edit_name', args=[self.child.uuid]), {'name': 'Насос Б'})

        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, 'Насос Б')
        self.assertTrue(self.child.actions.filter(action_type='update').exists())

    def test_inventory_number_can_be_cleared(self):
        self.login(self.senior)
        self.client.post(reverse('edit_inventory', args=[self.child.uuid]), {'inventory_number': '   '})

        self.child.refresh_from_db()
        self.assertIsNone(self.child.inventory_number)

    def test_object_cannot_become_its_own_parent(self):
        self.login(self.senior)
        response = self.client.post(
            reverse('edit_parent', args=[self.child.uuid]), {'parent': str(self.child.uuid)}
        )

        self.assertEqual(response.status_code, 400)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent, self.root)

    def test_descendant_cannot_become_parent(self):
        """BUG-004: назначение потомка родителем создало бы цикл."""
        grandchild = DataObject.objects.create(model=self.model, name='Внук', parent=self.child)

        self.login(self.senior)
        response = self.client.post(
            reverse('edit_parent', args=[self.root.uuid]), {'parent': str(grandchild.uuid)}
        )

        self.assertEqual(response.status_code, 400)
        self.root.refresh_from_db()
        self.assertIsNone(self.root.parent)

    def test_model_validation_rejects_cycle(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.root.validate_parent(self.child)
        with self.assertRaises(ValidationError):
            self.root.validate_parent(self.root)
        self.child.validate_parent(self.root)  # штатный случай не бросает

    def test_descendant_uuids_are_collected_across_levels(self):
        grandchild = DataObject.objects.create(model=self.model, name='Внук', parent=self.child)
        self.assertEqual(self.root.get_descendant_uuids(), {self.child.uuid, grandchild.uuid})
        self.assertEqual(grandchild.get_descendant_uuids(), set())

    def test_parent_can_be_cleared_to_make_object_root(self):
        self.login(self.senior)
        self.client.post(reverse('edit_parent', args=[self.child.uuid]), {'parent': ''})

        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent)
        self.assertTrue(self.child.actions.filter(action_type='link_change').exists())

    def test_junior_cannot_edit_name(self):
        self.login(self.junior)
        response = self.client.post(reverse('edit_name', args=[self.child.uuid]), {'name': 'Взлом'})

        self.assertEqual(response.status_code, 403)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, 'Насос А')


class CommentTests(BaseDataTestCase):
    """Комментарии к объекту, включая комментарий-вложение."""

    def test_text_comment_is_created(self):
        self.login(self.junior)
        response = self.client.post(
            reverse('add_comment', args=[self.root.uuid]), {'text': 'Проверено'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.root.comments.count(), 1)
        self.assertEqual(self.root.comments.first().text, 'Проверено')

    def test_comment_with_only_a_file_is_accepted(self):
        """BUG-028: вложение без текста — штатный сценарий."""
        self.login(self.junior)
        upload = SimpleUploadedFile('scheme.pdf', b'%PDF-1.4 fake', content_type='application/pdf')

        response = self.client.post(reverse('add_comment', args=[self.root.uuid]), {'file': upload})

        self.assertEqual(response.status_code, 200)
        comment = self.root.comments.get()
        self.assertEqual(comment.attachments.count(), 1)
        self.assertEqual(comment.attachments.get().filename, 'scheme.pdf')

    def test_comment_textarea_is_not_required_in_markup(self):
        """BUG-028: интерфейс не должен требовать текст при наличии файла."""
        self.login(self.junior)
        response = self.client.get(reverse('object_tab', args=[self.root.uuid, 'comments']))

        body = response.content.decode()
        textarea = body[body.index('<textarea'):body.index('</textarea>')]
        self.assertNotIn('required', textarea)

    def test_empty_comment_is_rejected_with_message(self):
        self.login(self.junior)
        response = self.client.post(reverse('add_comment', args=[self.root.uuid]), {'text': '   '})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.root.comments.count(), 0)
        self.assertIn('прикрепите файл', response.content.decode())

    def test_junior_can_delete_only_own_comments(self):
        mine = Comment.objects.create(user=self.junior, data_object=self.root, text='моё')
        foreign = Comment.objects.create(user=self.senior, data_object=self.root, text='чужое')

        self.login(self.junior)
        self.client.post(reverse('delete_comments_bulk'), {
            'object_uuid': str(self.root.uuid),
            'comment_ids': [str(mine.uuid), str(foreign.uuid)],
        })

        self.assertFalse(Comment.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Comment.objects.filter(pk=foreign.pk).exists())

    def test_senior_can_delete_any_comment(self):
        foreign = Comment.objects.create(user=self.junior, data_object=self.root, text='чужое')

        self.login(self.senior)
        self.client.post(reverse('delete_comments_bulk'), {
            'object_uuid': str(self.root.uuid),
            'comment_ids': [str(foreign.uuid)],
        })

        self.assertFalse(Comment.objects.filter(pk=foreign.pk).exists())


class AttachmentTests(BaseDataTestCase):
    """Загрузка файлов и превью объекта."""

    def test_document_upload_creates_attachment(self):
        self.login(self.junior)
        upload = SimpleUploadedFile('passport.pdf', b'data', content_type='application/pdf')

        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': upload, 'is_preview': 'false',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.root.attachments.count(), 1)
        self.assertFalse(self.root.attachments.get().is_preview)

    def test_non_image_cannot_be_uploaded_as_preview(self):
        self.login(self.junior)
        upload = SimpleUploadedFile('passport.pdf', b'data', content_type='application/pdf')

        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': upload, 'is_preview': 'true',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.root.attachments.count(), 0)

    def test_new_preview_replaces_the_previous_one(self):
        self.login(self.senior)
        first = Attachment.objects.create(
            user=self.senior, data_object=self.root, is_preview=True,
            path=SimpleUploadedFile('a.png', PNG_BYTES, content_type='image/png'),
        )
        self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('b.png', PNG_BYTES, content_type='image/png'),
            'is_preview': 'true',
        })

        first.refresh_from_db()
        self.assertFalse(first.is_preview)
        self.assertEqual(Attachment.objects.filter(data_object=self.root, is_preview=True).count(), 1)

    def test_set_preview_toggles_flag(self):
        att = Attachment.objects.create(
            user=self.senior, data_object=self.root,
            path=SimpleUploadedFile('c.png', PNG_BYTES, content_type='image/png'),
        )
        self.login(self.senior)

        self.client.post(reverse('set_preview_attachment', args=[att.uuid]))
        att.refresh_from_db()
        self.assertTrue(att.is_preview)

        self.client.post(reverse('set_preview_attachment', args=[att.uuid]))
        att.refresh_from_db()
        self.assertFalse(att.is_preview)


class ServiceObjectTests(BaseDataTestCase):
    """Фиксация выполненного ТО."""

    def test_planned_service_updates_next_date_and_writes_history(self):
        self.login(self.junior)
        response = self.client.post(reverse('service_object', args=[self.root.uuid]), {
            'maintenance_date': '2027-01-15',
            'comment': 'Замена подшипника',
        })

        self.assertEqual(response.status_code, 200)
        self.root.refresh_from_db()
        self.assertEqual(self.root.next_maintenance_date, datetime.date(2027, 1, 15))

        entry = ActionHistory.objects.get(data_object=self.root, action_type='maintenance')
        self.assertIn('Плановое ТО', entry.action)
        self.assertIn('Замена подшипника', entry.action)

    def test_unplanned_service_keeps_scheduled_date(self):
        self.root.next_maintenance_date = datetime.date(2027, 5, 5)
        self.root.save(update_fields=['next_maintenance_date'])

        self.login(self.junior)
        self.client.post(reverse('service_object', args=[self.root.uuid]), {
            'is_unplanned': 'on',
            'maintenance_date': '2030-01-01',
        })

        self.root.refresh_from_db()
        self.assertEqual(self.root.next_maintenance_date, datetime.date(2027, 5, 5))
        self.assertIn(
            'Внеплановое',
            ActionHistory.objects.get(data_object=self.root, action_type='maintenance').action,
        )

    def test_service_response_refreshes_maintenance_pill(self):
        self.login(self.junior)
        response = self.client.post(reverse('service_object', args=[self.root.uuid]), {
            'maintenance_date': '2027-01-15',
        })
        self.assertIn('id="maintenance-pill"', response.content.decode())


class DeletionTests(BaseDataTestCase):
    """Удаление объектов и моделей."""

    def test_get_shows_confirmation_and_does_not_delete_object(self):
        """BUG-005/011: GET не изменяет данные, а показывает объём каскада."""
        Comment.objects.create(user=self.senior, data_object=self.child, text='x')

        self.login(self.senior)
        response = self.client.get(reverse('delete_object', args=[self.root.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(DataObject.objects.filter(pk=self.root.pk).exists())
        self.assertEqual(response.context['stats'], {
            'descendants': 1, 'comments': 1, 'attachments': 0, 'history': 0,
        })
        self.assertIn('confirm_subtree', response.content.decode())

    def test_put_is_rejected_for_object_deletion(self):
        self.login(self.senior)
        response = self.client.put(reverse('delete_object', args=[self.child.uuid]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(DataObject.objects.filter(pk=self.child.pk).exists())

    def test_non_empty_object_requires_subtree_confirmation(self):
        """BUG-011: поддерево не удаляется молча."""
        self.login(self.senior)
        response = self.client.post(reverse('delete_object', args=[self.root.uuid]))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(DataObject.objects.filter(pk=self.root.pk).exists())
        self.assertTrue(DataObject.objects.filter(pk=self.child.pk).exists())

    def test_confirmed_subtree_deletion_removes_everything(self):
        self.login(self.senior)
        response = self.client.post(reverse('delete_object', args=[self.root.uuid]), {'confirm_subtree': 'yes'})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DataObject.objects.filter(pk__in=[self.root.pk, self.child.pk]).exists())
        self.assertIn(f'id="node-{self.root.uuid}" hx-swap-oob="delete"', response.content.decode())

    def test_leaf_object_is_deleted_without_extra_confirmation(self):
        self.login(self.senior)
        response = self.client.post(reverse('delete_object', args=[self.child.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DataObject.objects.filter(pk=self.child.pk).exists())

    def test_deleting_open_object_resets_detail_pane(self):
        self.login(self.senior)
        self.client.get(reverse('object_detail', args=[self.child.uuid]))  # делает объект активным

        response = self.client.post(reverse('delete_object', args=[self.child.uuid]))

        self.assertIn('id="detail-container" hx-swap-oob="innerHTML"', response.content.decode())
        self.assertIsNone(self.client.session.get('active_object_id'))

    def test_model_in_use_cannot_be_deleted(self):
        """BUG-002: удаление модели не должно каскадом уносить объекты."""
        self.login(self.senior)
        response = self.client.post(reverse('delete_model', args=[self.model.uuid]))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(ObjectModel.objects.filter(pk=self.model.pk).exists())
        self.assertEqual(DataObject.objects.count(), 2)

    def test_model_confirmation_explains_protection(self):
        self.login(self.senior)
        response = self.client.get(reverse('delete_model', args=[self.model.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['objects_count'], 2)
        self.assertNotIn('hx-post', response.content.decode())

    def test_unused_model_is_deleted(self):
        free_model = ObjectModel.objects.create(object_type=self.object_type, name='Свободная')

        self.login(self.senior)
        response = self.client.post(reverse('delete_model', args=[free_model.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ObjectModel.objects.filter(pk=free_model.pk).exists())

    def test_model_fk_is_protected_at_orm_level(self):
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.model.delete()

    def test_get_requests_do_not_mutate_state(self):
        """BUG-005: все изменяющие эндпоинты отвечают 405 на GET."""
        rule = DateUpdateRule.objects.create(name='Свободное', rule={})
        free_type = ObjectType.objects.create(type='Никем не используется')
        att = Attachment.objects.create(
            user=self.senior, data_object=self.root,
            path=SimpleUploadedFile('p.png', PNG_BYTES, content_type='image/png'),
        )
        self.root.date_update_rule = rule
        self.root.save(update_fields=['date_update_rule'])

        self.login(self.admin)
        urls = [
            reverse('delete_rule', args=[rule.uuid]),
            reverse('delete_object_type', args=[free_type.uuid]),
            reverse('unlink_rule', args=[self.root.uuid]),
            reverse('set_preview_attachment', args=[att.uuid]),
            reverse('add_comment', args=[self.root.uuid]) + '?text=x',
            reverse('delete_comments_bulk'),
            reverse('delete_attachments_bulk'),
            reverse('model_spec_delete', args=[self.model.uuid]) + '?key=Мощность',
            reverse('sync_youtrack', args=[self.root.uuid]),
            reverse('clone_object'),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

        self.assertTrue(DateUpdateRule.objects.filter(pk=rule.pk).exists())
        self.assertTrue(ObjectType.objects.filter(pk=free_type.pk).exists())
        self.root.refresh_from_db()
        self.assertEqual(self.root.date_update_rule, rule)
        att.refresh_from_db()
        self.assertFalse(att.is_preview)
        self.assertEqual(self.root.comments.count(), 0)
        self.model.refresh_from_db()
        self.assertIn('Мощность', self.model.specifications)

    def test_object_type_in_use_cannot_be_deleted(self):
        self.login(self.senior)
        response = self.client.post(reverse('delete_object_type', args=[self.object_type.uuid]))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(ObjectType.objects.filter(pk=self.object_type.uuid).exists())

    def test_rule_in_use_cannot_be_deleted(self):
        rule = DateUpdateRule.objects.create(name='Используемое', rule={})
        self.root.date_update_rule = rule
        self.root.save(update_fields=['date_update_rule'])

        self.login(self.senior)
        response = self.client.post(reverse('delete_rule', args=[rule.uuid]))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(DateUpdateRule.objects.filter(pk=rule.pk).exists())


class CloneTests(BaseDataTestCase):
    """Тиражирование объектов."""

    def test_clone_copies_subtree_and_clears_identifiers(self):
        self.root.youtrack_issue_id = 'MNT-9'
        self.root.save(update_fields=['youtrack_issue_id'])

        self.login(self.senior)
        self.client.post(reverse('clone_object'), {
            'source_object': str(self.root.uuid),
            'new_name': 'Насосная станция №2',
            'clone_children': 'on',
        })

        clone = DataObject.objects.get(name='Насосная станция №2')
        self.assertIsNone(clone.inventory_number)
        self.assertIsNone(clone.youtrack_issue_id)
        self.assertIsNone(clone.parent)
        self.assertEqual(clone.children.count(), 1)

    def test_clone_without_children_creates_single_object(self):
        self.login(self.senior)
        self.client.post(reverse('clone_object'), {
            'source_object': str(self.root.uuid),
            'new_name': 'Одиночная копия',
        })

        clone = DataObject.objects.get(name='Одиночная копия')
        self.assertEqual(clone.children.count(), 0)

    def test_clone_is_atomic(self):
        """BUG-027: ошибка в середине не оставляет частично склонированное дерево."""
        from unittest.mock import patch

        real_create = ActionHistory.objects.create
        calls = {'n': 0}

        def failing_create(**kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('сбой на втором объекте')
            return real_create(**kwargs)

        before = DataObject.objects.count()
        self.login(self.senior)
        with patch.object(ActionHistory.objects, 'create', side_effect=failing_create):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('clone_object'), {
                    'source_object': str(self.root.uuid),
                    'new_name': 'Половинчатая копия',
                    'clone_children': 'on',
                })

        self.assertEqual(DataObject.objects.count(), before)

    def test_clone_does_not_hang_on_cycle(self):
        DataObject.objects.filter(pk=self.root.pk).update(parent=self.child)

        self.login(self.senior)
        self.client.post(reverse('clone_object'), {
            'source_object': str(self.root.uuid),
            'new_name': 'Копия из цикла',
            'clone_children': 'on',
            'keep_parent': '',
        })

        self.assertTrue(DataObject.objects.filter(name='Копия из цикла').exists())

    def test_clone_keeps_parent_when_requested(self):
        self.login(self.senior)
        self.client.post(reverse('clone_object'), {
            'source_object': str(self.child.uuid),
            'new_name': 'Насос А (дубль)',
            'keep_parent': 'on',
        })

        self.assertEqual(DataObject.objects.get(name='Насос А (дубль)').parent, self.root)


class SearchAndSuggestTests(BaseDataTestCase):
    """Глобальный поиск и подсказки."""

    def test_short_query_returns_nothing(self):
        self.login(self.junior)
        response = self.client.get(reverse('search'), {'q': 'а'})
        self.assertEqual(response.content.decode().strip(), '')

    def test_search_finds_object_by_inventory_number(self):
        self.login(self.junior)
        response = self.client.get(reverse('search'), {'q': 'ИНВ-002'})
        self.assertIn('Насос А', response.content.decode())

    def test_search_matches_all_words_in_any_order(self):
        self.login(self.junior)
        response = self.client.get(reverse('search'), {'q': 'станция насосная'})
        self.assertIn('Насосная станция №1', response.content.decode())

    def test_name_check_escapes_html_in_names(self):
        """BUG-003: имена вставлялись в HTML-ответ без экранирования."""
        ObjectModel.objects.create(object_type=self.object_type, name='<img src=x onerror=alert(1)>')

        self.login(self.senior)
        response = self.client.get(reverse('check_model_name'), {'name': '<img src=x onerror=alert(1)>'})

        body = response.content.decode()
        self.assertNotIn('<img src=x', body)
        self.assertIn('&lt;img', body)

    def test_object_name_check_escapes_similar_matches(self):
        DataObject.objects.create(model=self.model, name='Насос <b>жирный</b>')

        self.login(self.senior)
        response = self.client.get(reverse('check_object_name'), {'name': 'Насос жирный'})

        self.assertNotIn('<b>жирный</b>', response.content.decode())

    def test_suggest_offers_creating_a_new_object_type(self):
        self.login(self.senior)
        response = self.client.get(reverse('suggest'), {'field': 'object_type', 'q': 'Компрессор'})

        self.assertTrue(response.context['show_create_option'])
        self.assertIn('Создать', response.content.decode())

    def test_suggest_does_not_offer_creating_rules(self):
        """Правило нельзя создать из подсказки: нет конструктора параметров."""
        self.login(self.senior)
        response = self.client.get(reverse('suggest'), {'field': 'date_update_rule', 'q': 'Новое'})
        self.assertFalse(response.context['show_create_option'])

    def test_parent_suggestions_exclude_descendants(self):
        """BUG-004: потомки не предлагаются в качестве родителя."""
        grandchild = DataObject.objects.create(model=self.model, name='Насос-внук', parent=self.child)

        self.login(self.senior)
        response = self.client.get(reverse('suggest'), {
            'field': 'parent_inline', 'q': 'Насос', 'exclude_uuid': str(self.root.uuid),
        })

        uuids = {item.uuid for item in response.context['results']}
        self.assertNotIn(self.child.uuid, uuids)
        self.assertNotIn(grandchild.uuid, uuids)

    def test_parent_suggestions_exclude_the_object_itself(self):
        self.login(self.senior)
        response = self.client.get(reverse('suggest'), {
            'field': 'parent_inline', 'q': 'Насос', 'exclude_uuid': str(self.child.uuid),
        })

        uuids = [item.uuid for item in response.context['results']]
        self.assertNotIn(self.child.uuid, uuids)


class DashboardAndExportTests(BaseDataTestCase):
    """Дашборд, список ТО и выгрузка XLSX."""

    def test_dashboard_counts_overdue_objects(self):
        self.root.next_maintenance_date = timezone.localdate() - datetime.timedelta(days=3)
        self.root.save(update_fields=['next_maintenance_date'])

        self.login(self.junior)
        response = self.client.get(reverse('dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['overdue_count'], 1)
        self.assertEqual(response.context['total_objects'], 2)

    def test_maintenance_list_filters_overdue_period(self):
        self.root.next_maintenance_date = timezone.localdate() - datetime.timedelta(days=3)
        self.root.save(update_fields=['next_maintenance_date'])

        self.login(self.junior)
        response = self.client.get(reverse('maintenance_list'), {'period': 'overdue'})

        self.assertEqual(list(response.context['objects']), [self.root])

    def test_export_returns_xlsx_attachment(self):
        self.login(self.admin)
        response = self.client.get(reverse('export_xlsx'))

        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])
        self.assertIn('attachment;', response['Content-Disposition'])

    def test_export_with_inventory_filter_skips_objects_without_number(self):
        DataObject.objects.create(model=self.model, name='Без инвентарника')

        self.login(self.admin)
        response = self.client.get(reverse('export_xlsx'), {'export_mode': 'with_inventory'})
        self.assertEqual(response.status_code, 200)


class ExplorerModeTests(BaseDataTestCase):
    """Переключение режимов справочника."""

    def test_toggle_requires_post(self):
        self.login(self.junior)
        response = self.client.get(reverse('toggle_explorer_mode'))
        self.assertEqual(response.status_code, 405)

    def test_toggle_switches_between_tree_and_flat(self):
        self.login(self.junior)

        self.client.post(reverse('toggle_explorer_mode'))
        self.assertEqual(self.client.session['explorer_mode'], 'flat')

        self.client.post(reverse('toggle_explorer_mode'))
        self.assertEqual(self.client.session['explorer_mode'], 'tree')

    def test_explorer_navigate_sets_current_folder(self):
        self.login(self.junior)
        self.client.post(reverse('explorer_navigate', args=[str(self.root.uuid)]))

        self.assertEqual(self.client.session['explorer_parent_uuid'], str(self.root.uuid))


class FakeYtResponse:
    def __init__(self, status_code=200, payload=None, content=b''):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = ''

    def json(self):
        return self._payload


class YoutrackSyncTests(BaseDataTestCase):
    """Синхронизация с YouTrack: только по явному запросу и без потери данных."""

    def setUp(self):
        self.root.youtrack_issue_id = 'MNT-1'
        self.root.save(update_fields=['youtrack_issue_id'])
        self.senior.youtrack_token = 'perm:token'
        self.senior.save(update_fields=['youtrack_token'])

    def _issue_payload(self, **overrides):
        payload = {
            'id': 'MNT-1',
            'description': 'Описание из YouTrack',
            'comments': [
                {'id': 'c-1', 'text': 'Комментарий из YT', 'created': 1_700_000_000_000,
                 'author': {'email': 'senior@test.local'}, 'attachments': []},
            ],
            'attachments': [],
        }
        payload.update(overrides)
        return payload

    def _mock_get(self, issue_payload, work_items_pages=None, work_items_status=200):
        """Фабрика подмены requests.get: задача + постраничные списания."""
        pages = list(work_items_pages or [[]])

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith('/timeTracking/workItems'):
                if work_items_status != 200:
                    return FakeYtResponse(work_items_status, [])
                page = (params or {}).get('$skip', 0) // 100
                return FakeYtResponse(200, pages[page] if page < len(pages) else [])
            return FakeYtResponse(200, issue_payload)
        return fake_get

    def _sync(self):
        """
        Ставит синхронизацию в очередь и тут же выполняет её воркером.

        В бою эти два шага разнесены: веб только записывает задание, а в
        YouTrack ходит процесс планировщика. В тесте разносить незачем.
        """
        response = self.client.post(reverse('sync_youtrack', args=[self.root.uuid]))
        process_jobs()
        return response

    def _sync_status(self):
        """Разметка пилюли состояния — то, что увидит пользователь."""
        return self.client.get(reverse('sync_status', args=[self.root.uuid])).content.decode()

    def test_opening_card_does_not_call_youtrack(self):
        """BUG-006: GET карточки — чистый просмотр без обращений наружу."""
        self.login(self.senior)
        with patch('data.youtrack_services.requests.get') as mock_get:
            response = self.client.get(reverse('object_detail', args=[self.root.uuid]))

        self.assertEqual(response.status_code, 200)
        mock_get.assert_not_called()

    def test_explicit_sync_runs_exactly_once(self):
        """BUG-013: ручная синхронизация не дублируется рендером карточки."""
        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(self._issue_payload())) as mock_get:
            response = self._sync()

        self.assertEqual(response.status_code, 200)
        issue_calls = [c for c in mock_get.call_args_list if '/api/issues/MNT-1' == c.args[0].split('?')[0][-len('/api/issues/MNT-1'):]]
        self.assertEqual(len(issue_calls), 1)
        body = self._sync_status()
        self.assertIn('pill-success', body)
        self.assertIn('id="tab-count-comments" class="tab-count" hx-swap-oob="true">1<', body)
        self.assertIn('id="description-container"', body)
        self.assertEqual(self.root.comments.filter(youtrack_id='c-1').count(), 1)

    def test_sync_error_is_shown_to_user(self):
        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', return_value=FakeYtResponse(404)):
            # 404 — ошибка сетевая по форме, поэтому задание отрабатывает
            # все попытки, прежде чем признать себя неудачным.
            self._sync()
            for _ in range(YouTrackJob.MAX_ATTEMPTS):
                YouTrackJob.objects.filter(status=YouTrackJob.QUEUED).update(run_after=timezone.now())
                process_jobs()

        body = self._sync_status()
        self.assertIn('pill-danger', body)
        self.assertIn('не найдена', body)
        self.assertNotIn('id="description-container"', body)  # при ошибке описание не трогаем

    def test_missing_collections_do_not_delete_local_records(self):
        """BUG-006: частичный ответ не трактуется как удаление."""
        Comment.objects.create(user=self.senior, data_object=self.root, text='старый', youtrack_id='c-old')
        ActionHistory.objects.create(user=self.senior, data_object=self.root, action='ТО', youtrack_id='w-old')
        payload = {'id': 'MNT-1', 'description': 'x'}  # без comments/attachments

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(payload, work_items_status=500)):
            self._sync()

        self.assertTrue(Comment.objects.filter(youtrack_id='c-old').exists())
        self.assertTrue(ActionHistory.objects.filter(youtrack_id='w-old').exists())

    def test_complete_response_removes_records_deleted_remotely(self):
        Comment.objects.create(user=self.senior, data_object=self.root, text='удалён в YT', youtrack_id='c-gone')

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(self._issue_payload())):
            self._sync()

        self.assertFalse(Comment.objects.filter(youtrack_id='c-gone').exists())
        self.assertTrue(Comment.objects.filter(youtrack_id='c-1').exists())

    def test_work_items_are_fetched_across_pages(self):
        """Списания старше первой страницы не должны считаться удалёнными."""
        ActionHistory.objects.create(user=self.senior, data_object=self.root, action='старое', youtrack_id='w-150')
        page1 = [{'id': f'w-{i}', 'text': f'работа {i}', 'date': 1_700_000_000_000} for i in range(100)]
        page2 = [{'id': 'w-150', 'text': 'старое', 'date': 1_700_000_000_000}]

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(self._issue_payload(), [page1, page2])):
            self._sync()

        self.assertTrue(ActionHistory.objects.filter(youtrack_id='w-150').exists())
        self.assertEqual(ActionHistory.objects.filter(data_object=self.root, youtrack_id__startswith='w-').count(), 101)

    def test_empty_remote_description_clears_local_one(self):
        """BUG-014: пустое описание в YouTrack — валидное значение."""
        self.root.description = 'локальный текст'
        self.root.save(update_fields=['description'])

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(self._issue_payload(description=''))):
            self._sync()

        self.root.refresh_from_db()
        self.assertIsNone(self.root.description)

    def test_changed_remote_comment_updates_local_text(self):
        """BUG-014: правка комментария в YouTrack применяется локально."""
        Comment.objects.create(user=self.senior, data_object=self.root, text='старая версия', youtrack_id='c-1')

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get', side_effect=self._mock_get(self._issue_payload())):
            self._sync()

        self.assertEqual(Comment.objects.get(youtrack_id='c-1').text, 'Комментарий из YT')

    def test_sync_requires_token(self):
        self.senior.youtrack_token = None
        self.senior.save(update_fields=['youtrack_token'])

        self.login(self.senior)
        with patch('data.youtrack_services.requests.get') as mock_get:
            self._sync()

        mock_get.assert_not_called()
        # Отсутствие токена повтором не лечится: задание закрывается сразу.
        job = YouTrackJob.objects.get(data_object=self.root, kind=YouTrackJob.KIND_SYNC)
        self.assertEqual(job.status, YouTrackJob.FAILED)
        self.assertIn('токен', job.last_error)
        self.assertIn('токен', self._sync_status())


class AttachmentValidationTests(BaseDataTestCase):
    """BUG-008: ограничения на загружаемые файлы."""

    def test_oversized_file_is_rejected(self):
        self.login(self.senior)
        big = SimpleUploadedFile('big.pdf', b'x' * (settings.MAX_ATTACHMENT_SIZE + 1), content_type='application/pdf')

        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': big, 'is_preview': 'false',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('слишком большой', response.content.decode())
        self.assertEqual(self.root.attachments.count(), 0)

    def test_empty_file_is_rejected(self):
        self.login(self.senior)
        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('empty.pdf', b'', content_type='application/pdf'),
            'is_preview': 'false',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.root.attachments.count(), 0)

    def test_svg_upload_is_blocked(self):
        """SVG отдаётся с нашего origin и может выполнить скрипт."""
        self.login(self.senior)
        payload = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'

        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('logo.svg', payload, content_type='image/svg+xml'),
            'is_preview': 'false',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.root.attachments.count(), 0)

    def test_html_upload_is_blocked(self):
        self.login(self.senior)
        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('page.html', b'<h1>hi</h1>', content_type='text/html'),
            'is_preview': 'false',
        })
        self.assertEqual(response.status_code, 400)

    def test_preview_checks_real_signature_not_mime(self):
        """MIME и расширение подконтрольны клиенту — решает содержимое файла."""
        self.login(self.senior)
        fake = SimpleUploadedFile('shell.png', b'MZ\x90\x00 not an image', content_type='image/png')

        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': fake, 'is_preview': 'true',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('не распознано как изображение', response.content.decode())
        self.assertEqual(self.root.attachments.count(), 0)

    def test_real_png_is_accepted_as_preview(self):
        self.login(self.senior)
        response = self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('photo.png', PNG_BYTES, content_type='image/png'),
            'is_preview': 'true',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.root.attachments.get().is_preview)

    def test_comment_attachment_is_validated(self):
        self.login(self.junior)
        response = self.client.post(reverse('add_comment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('x.svg', b'<svg></svg>', content_type='image/svg+xml'),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.root.comments.count(), 0)
        self.assertIn('svg', response.content.decode().lower())

    def test_svg_is_not_treated_as_image(self):
        att = Attachment(data_object=self.root)
        att.path.name = 'attachments/x/logo.svg'
        self.assertFalse(att.is_image)

    def test_signature_detection(self):
        from io import BytesIO
        from data.validators import detect_image_format

        self.assertEqual(detect_image_format(BytesIO(PNG_BYTES)), 'png')
        self.assertEqual(detect_image_format(BytesIO(b'\xff\xd8\xff\xe0jfif')), 'jpeg')
        self.assertEqual(detect_image_format(BytesIO(b'RIFF\x00\x00\x00\x00WEBPVP8 ')), 'webp')
        self.assertIsNone(detect_image_format(BytesIO(b'%PDF-1.4')))


class XlsxExportSafetyTests(BaseDataTestCase):
    """BUG-009: значения не должны становиться формулами Excel."""

    def _export_rows(self):
        import openpyxl
        from io import BytesIO

        response = self.client.get(reverse('export_xlsx'))
        self.assertEqual(response.status_code, 200)
        wb = openpyxl.load_workbook(BytesIO(response.content))
        return list(wb.active.iter_rows(min_row=2, values_only=True))

    def test_formula_like_values_are_escaped(self):
        DataObject.objects.create(
            model=self.model,
            name='=HYPERLINK("http://evil","click")',
            inventory_number='+1234',
            description='@SUM(A1:A2)',
        )
        self.login(self.admin)

        rows = self._export_rows()
        dangerous = [c for row in rows for c in row if isinstance(c, str) and c[:1] in '=+-@']
        self.assertEqual(dangerous, [])

        exported = {row[1] for row in rows}
        self.assertIn('\'=HYPERLINK("http://evil","click")', exported)

    def test_normal_values_are_not_mangled(self):
        self.login(self.admin)
        rows = self._export_rows()
        names = {row[1] for row in rows}
        self.assertIn('Насосная станция №1', names)


class YoutrackFailureReportingTests(BaseDataTestCase):
    """BUG-007: ошибки YouTrack не должны проглатываться."""

    def setUp(self):
        self.root.youtrack_issue_id = 'MNT-1'
        self.root.save(update_fields=['youtrack_issue_id'])
        self.senior.youtrack_token = 'perm:token'
        self.senior.save(update_fields=['youtrack_token'])
        self.login(self.senior)

    def _drain(self):
        """
        Прогоняет очередь до конца, включая повторы.

        В бою между попытками проходят минуты; здесь достаточно сдвинуть
        «не раньше» и позвать воркера снова.
        """
        for _ in range(YouTrackJob.MAX_ATTEMPTS):
            YouTrackJob.objects.filter(status=YouTrackJob.QUEUED).update(run_after=timezone.now())
            if not process_jobs():
                break

    def _pill(self, obj=None):
        """Разметка пилюли состояния — там пользователь и видит итог обмена."""
        target = obj or self.root
        return self.client.get(reverse('sync_status', args=[target.uuid])).content.decode()

    def test_failed_work_item_is_reported_to_user(self):
        with patch('data.youtrack_services.add_work_item_to_youtrack', return_value=(False, 'нет прав')):
            self.client.post(reverse('service_object', args=[self.root.uuid]), {
                'maintenance_date': '2027-03-01',
                'spent_time': '1h',
            })
            self._drain()

        # Списание уходит в очередь, поэтому о неудаче сообщает пилюля
        body = self._pill()
        self.assertIn('pill-danger', body)
        self.assertIn('нет прав', body)
        # Локальная запись о ТО всё равно сохраняется — источник истины у нас
        self.assertTrue(ActionHistory.objects.filter(data_object=self.root, action_type='maintenance').exists())

    def test_successful_work_item_produces_no_warning(self):
        with patch('data.youtrack_services.add_work_item_to_youtrack', return_value=(True, 'w-1')):
            response = self.client.post(reverse('service_object', args=[self.root.uuid]), {
                'maintenance_date': '2027-03-01',
                'spent_time': '1h',
            })
            self._drain()

        self.assertNotIn('toast-item', response.content.decode())
        self.assertNotIn('pill-danger', self._pill())
        self.assertEqual(ActionHistory.objects.get(data_object=self.root, action_type='maintenance').youtrack_id, 'w-1')

    def test_failed_description_update_is_reported(self):
        with patch('data.youtrack_services.update_issue_description_in_youtrack', return_value=(False, 'таймаут')):
            self.client.post(reverse('edit_description', args=[self.root.uuid]), {
                'description': 'новое описание',
            })
            self._drain()

        body = self._pill()
        self.assertIn('pill-danger', body)
        self.assertIn('таймаут', body)
        self.root.refresh_from_db()
        self.assertEqual(self.root.description, 'новое описание')

    def test_comment_is_kept_when_remote_delete_fails(self):
        """Локальная запись не удаляется, пока она жива в YouTrack."""
        comment = Comment.objects.create(
            user=self.senior, data_object=self.root, text='важное', youtrack_id='c-1'
        )

        with patch('data.youtrack_services.delete_comment_from_youtrack', return_value=(False, 'сервер недоступен')):
            response = self.client.post(reverse('delete_comments_bulk'), {
                'object_uuid': str(self.root.uuid),
                'comment_ids': [str(comment.uuid)],
            })

        self.assertTrue(Comment.objects.filter(pk=comment.pk).exists())
        body = response.content.decode()
        self.assertIn('сервер недоступен', body)
        self.assertIn('оставлены локально', body)

    def test_comment_is_deleted_when_remote_delete_succeeds(self):
        comment = Comment.objects.create(
            user=self.senior, data_object=self.root, text='важное', youtrack_id='c-1'
        )

        with patch('data.youtrack_services.delete_comment_from_youtrack', return_value=(True, 'ok')):
            response = self.client.post(reverse('delete_comments_bulk'), {
                'object_uuid': str(self.root.uuid),
                'comment_ids': [str(comment.uuid)],
            })

        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
        self.assertNotIn('toast-item', response.content.decode())

    def test_local_only_comment_is_deleted_without_remote_call(self):
        comment = Comment.objects.create(user=self.senior, data_object=self.root, text='локальный')

        with patch('data.youtrack_services.delete_comment_from_youtrack') as mock_delete:
            self.client.post(reverse('delete_comments_bulk'), {
                'object_uuid': str(self.root.uuid),
                'comment_ids': [str(comment.uuid)],
            })

        mock_delete.assert_not_called()
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())

    def test_attachment_is_kept_when_remote_delete_fails(self):
        att = Attachment.objects.create(
            user=self.senior, data_object=self.root, youtrack_id='a-1',
            path=SimpleUploadedFile('doc.pdf', b'data', content_type='application/pdf'),
        )

        with patch('data.youtrack_services.delete_attachment_from_youtrack', return_value=(False, '503')):
            response = self.client.post(reverse('delete_attachments_bulk'), {
                'object_uuid': str(self.root.uuid),
                'file_ids': [str(att.uuid)],
            })

        self.assertTrue(Attachment.objects.filter(pk=att.pk).exists())
        self.assertIn('toast-item', response.content.decode())

    def test_failed_attachment_upload_is_reported(self):
        with patch('data.youtrack_services.upload_attachment_to_youtrack', return_value=(False, 'диск переполнен')):
            self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
                'file': SimpleUploadedFile('doc.pdf', b'data', content_type='application/pdf'),
                'is_preview': 'false',
            })
            self._drain()

        body = self._pill()
        self.assertIn('pill-danger', body)
        self.assertIn('диск переполнен', body)
        # Файл сохранён локально — пользователь не теряет работу
        self.assertEqual(self.root.attachments.count(), 1)

    def test_failed_comment_update_is_reported(self):
        comment = Comment.objects.create(
            user=self.senior, data_object=self.root, text='старое', youtrack_id='c-1'
        )

        with patch('data.youtrack_services.update_comment_in_youtrack', return_value=(False, 'конфликт версий')):
            self.client.post(reverse('edit_comment', args=[comment.uuid]), {'text': 'новое'})
            self._drain()

        self.assertIn('конфликт версий', self._pill())
        comment.refresh_from_db()
        self.assertEqual(comment.text, 'новое')


class SearchByYoutrackIdTests(BaseDataTestCase):
    """BUG-016: поиск обещал YouTrack в подсказке, но по ID задачи не искал."""

    def setUp(self):
        self.root.youtrack_issue_id = 'MNT-412'
        self.root.save(update_fields=['youtrack_issue_id'])
        self.login(self.junior)

    def test_search_finds_object_by_full_issue_id(self):
        response = self.client.get(reverse('search'), {'q': 'MNT-412'})
        self.assertIn('Насосная станция №1', response.content.decode())

    def test_search_finds_object_by_issue_id_fragment(self):
        response = self.client.get(reverse('search'), {'q': '412'})
        self.assertIn('Насосная станция №1', response.content.decode())

    def test_issue_id_works_in_multi_word_query(self):
        response = self.client.get(reverse('search'), {'q': 'MNT-412 Насосная'})
        self.assertIn('Насосная станция №1', response.content.decode())

    def test_issue_id_is_shown_in_results(self):
        response = self.client.get(reverse('search'), {'q': 'MNT-412'})
        self.assertIn('MNT-412', response.content.decode())

    def test_unrelated_issue_id_finds_nothing(self):
        response = self.client.get(reverse('search'), {'q': 'ZZZ-999'})
        self.assertIn('Ничего не найдено', response.content.decode())


class CommentEditingTests(BaseDataTestCase):
    """BUG-017: редактирование комментария было недоступно и падало на GET."""

    def setUp(self):
        self.comment = Comment.objects.create(
            user=self.junior, data_object=self.root, text='исходный текст'
        )

    def test_edit_button_is_shown_for_own_comment(self):
        self.login(self.junior)
        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'comments'])).content.decode()

        self.assertIn(reverse('edit_comment', args=[self.comment.uuid]), body)
        self.assertIn('Изменить', body)

    def test_edit_button_is_hidden_for_foreign_comment(self):
        other = User.objects.create_user(
            email='other@test.local', username='other', password='pass12345', role='senior'
        )
        self.client.force_login(other)
        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'comments'])).content.decode()

        self.assertNotIn(reverse('edit_comment', args=[self.comment.uuid]), body)

    def test_admin_sees_edit_button_for_foreign_comment(self):
        self.login(self.admin)
        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'comments'])).content.decode()
        self.assertIn(reverse('edit_comment', args=[self.comment.uuid]), body)

    def test_get_renders_edit_form(self):
        """Раньше здесь был TemplateDoesNotExist: шаблона не существовало."""
        self.login(self.junior)
        response = self.client.get(reverse('edit_comment', args=[self.comment.uuid]))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('<textarea', body)
        self.assertIn('исходный текст', body)

    def test_cancel_returns_display_mode(self):
        self.login(self.junior)
        response = self.client.get(reverse('edit_comment', args=[self.comment.uuid]), {'cancel': '1'})

        self.assertNotIn('<textarea', response.content.decode())

    def test_edit_form_is_not_nested_in_another_form(self):
        """Лента лежит внутри формы массового удаления — вложенных <form> быть не должно."""
        self.login(self.junior)
        body = self.client.get(reverse('edit_comment', args=[self.comment.uuid])).content.decode()
        self.assertNotIn('<form', body)

    def test_author_saves_new_text(self):
        self.login(self.junior)
        response = self.client.post(reverse('edit_comment', args=[self.comment.uuid]), {'text': 'исправленный текст'})

        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'исправленный текст')
        self.assertIn('исправленный текст', response.content.decode())

    def test_empty_text_is_rejected(self):
        self.login(self.junior)
        response = self.client.post(reverse('edit_comment', args=[self.comment.uuid]), {'text': '   '})

        self.assertEqual(response.status_code, 400)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'исходный текст')
        self.assertIn('не может быть пустым', response.content.decode())

    def test_foreign_comment_cannot_be_edited(self):
        other = User.objects.create_user(
            email='other2@test.local', username='other2', password='pass12345', role='senior'
        )
        self.client.force_login(other)
        response = self.client.post(reverse('edit_comment', args=[self.comment.uuid]), {'text': 'взлом'})

        self.assertEqual(response.status_code, 403)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'исходный текст')

    def test_admin_can_edit_foreign_comment(self):
        self.login(self.admin)
        self.client.post(reverse('edit_comment', args=[self.comment.uuid]), {'text': 'правка администратора'})

        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'правка администратора')


class ModelUniquenessTests(BaseDataTestCase):
    """BUG-025: проверка дубликата предупреждала, но не защищала данные."""

    def test_duplicate_name_is_rejected_by_database(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ObjectModel.objects.create(object_type=self.object_type, name='НМ-125')

    def test_uniqueness_ignores_case_and_edge_spaces(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ObjectModel.objects.create(object_type=self.object_type, name='  нм-125 ')

    def test_same_name_is_allowed_in_another_object_type(self):
        other_type = ObjectType.objects.create(type='Компрессор')
        created = ObjectModel.objects.create(object_type=other_type, name='НМ-125')
        self.assertIsNotNone(created.pk)

    def test_name_is_normalized_on_save(self):
        created = ObjectModel.objects.create(object_type=self.object_type, name='  ВК-25  ')
        self.assertEqual(created.name, 'ВК-25')

    def test_create_view_reports_duplicate_instead_of_crashing(self):
        self.login(self.senior)
        response = self.client.post(reverse('create_model'), {
            'name': 'нм-125',
            'object_type': str(self.object_type.uuid),
        })

        self.assertEqual(response.status_code, 409)
        self.assertIn('уже существует', response.content.decode())
        self.assertEqual(ObjectModel.objects.filter(object_type=self.object_type).count(), 1)

    def test_rename_to_existing_name_is_rejected(self):
        other = ObjectModel.objects.create(object_type=self.object_type, name='ВК-25')

        self.login(self.senior)
        response = self.client.post(reverse('edit_model_name', args=[other.uuid]), {'name': 'НМ-125'})

        self.assertEqual(response.status_code, 409)
        other.refresh_from_db()
        self.assertEqual(other.name, 'ВК-25')

    def test_name_check_is_scoped_to_object_type(self):
        other_type = ObjectType.objects.create(type='Компрессор')

        self.login(self.senior)
        response = self.client.get(reverse('check_model_name'), {
            'name': 'НМ-125', 'object_type': str(other_type.uuid),
        })

        self.assertIsNone(response.context['exact'])


class NullSpecificationsTests(BaseDataTestCase):
    """BUG-019: specifications допускает NULL — это не должно давать 500."""

    def setUp(self):
        self.model.specifications = None
        self.model.save(update_fields=['specifications'])
        self.login(self.senior)

    def test_spec_edit_form_survives_null(self):
        response = self.client.get(reverse('model_spec_edit', args=[self.model.uuid]), {'key': 'Мощность'})
        self.assertEqual(response.status_code, 200)

    def test_spec_add_works_on_null(self):
        response = self.client.post(reverse('model_spec_add', args=[self.model.uuid]), {
            'key': 'Мощность', 'value': '5 кВт',
        })

        self.assertEqual(response.status_code, 200)
        self.model.refresh_from_db()
        self.assertEqual(self.model.specifications, {'Мощность': '5 кВт'})

    def test_spec_delete_works_on_null(self):
        response = self.client.post(reverse('model_spec_delete', args=[self.model.uuid]), {'key': 'Мощность'})
        self.assertEqual(response.status_code, 200)

    def test_model_tabs_render_with_null(self):
        for tab in ('specs', 'objects'):
            with self.subTest(tab=tab):
                self.assertEqual(
                    self.client.get(reverse('model_tab', args=[self.model.uuid, tab])).status_code, 200
                )

    def test_object_card_renders_with_null_specs(self):
        self.assertEqual(self.client.get(reverse('object_detail', args=[self.root.uuid])).status_code, 200)

    def test_spec_add_rejects_get(self):
        self.assertEqual(self.client.get(reverse('model_spec_add', args=[self.model.uuid])).status_code, 405)


class RuleValidationTests(BaseDataTestCase):
    """BUG-020: серверная валидация правил ТО и дат."""

    def setUp(self):
        self.login(self.admin)

    def _create(self, **overrides):
        payload = {
            'name': 'Тестовое правило',
            'new_rule_strategy': 'relative',
            'new_rule_anchor': 'actual',
            'new_rule_years': '0',
            'new_rule_months': '6',
            'new_rule_days': '0',
        }
        payload.update(overrides)
        return self.client.post(reverse('create_rule_settings'), payload)

    def test_valid_relative_rule_is_created(self):
        response = self._create()

        self.assertEqual(response.status_code, 200)
        rule = DateUpdateRule.objects.get(name='Тестовое правило')
        self.assertEqual(rule.rule['value'], {'years': 0, 'months': 6, 'days': 0})

    def test_non_numeric_interval_is_rejected(self):
        """Раньше голый int() ронял запрос с 500."""
        response = self._create(new_rule_months='шесть')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DateUpdateRule.objects.filter(name='Тестовое правило').exists())

    def test_negative_interval_is_rejected(self):
        response = self._create(new_rule_months='-3')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DateUpdateRule.objects.filter(name='Тестовое правило').exists())

    def test_zero_interval_is_rejected(self):
        response = self._create(new_rule_years='0', new_rule_months='0', new_rule_days='0')

        self.assertEqual(response.status_code, 400)
        self.assertIn('нулевым', response.content.decode())

    def test_absurd_interval_is_rejected(self):
        response = self._create(new_rule_years='99999')
        self.assertEqual(response.status_code, 400)

    def test_unknown_strategy_does_not_crash(self):
        """Раньше rule_json оставался несвязанным → UnboundLocalError."""
        response = self._create(new_rule_strategy='телепатия')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DateUpdateRule.objects.filter(name='Тестовое правило').exists())

    def test_duplicate_rule_name_is_rejected(self):
        DateUpdateRule.objects.create(name='Тестовое правило', rule={})
        response = self._create()

        self.assertEqual(response.status_code, 400)
        self.assertIn('уже существует', response.content.decode())
        self.assertEqual(DateUpdateRule.objects.filter(name__iexact='Тестовое правило').count(), 1)

    def test_fixed_rule_requires_at_least_one_date(self):
        response = self._create(new_rule_strategy='fixed')

        self.assertEqual(response.status_code, 400)
        self.assertIn('дату', response.content.decode())

    def test_fixed_rule_drops_impossible_calendar_dates(self):
        response = self.client.post(reverse('create_rule_settings'), {
            'name': 'Сезонное',
            'new_rule_strategy': 'fixed',
            'fixed_months': ['2', '4'],
            'fixed_days': ['31', '15'],  # 31 февраля не существует
        })

        self.assertEqual(response.status_code, 200)
        rule = DateUpdateRule.objects.get(name='Сезонное')
        self.assertEqual(rule.rule['value'], [{'month': 4, 'day': 15}])

    def test_february_29_is_allowed(self):
        self.client.post(reverse('create_rule_settings'), {
            'name': 'Високосное',
            'new_rule_strategy': 'fixed',
            'fixed_months': ['2'],
            'fixed_days': ['29'],
        })

        self.assertEqual(DateUpdateRule.objects.get(name='Високосное').rule['value'], [{'month': 2, 'day': 29}])

    def test_edit_rejects_invalid_payload_and_keeps_old_rule(self):
        rule = DateUpdateRule.objects.create(
            name='Полгода',
            rule={'strategy': 'relative', 'anchor': 'actual', 'value': {'years': 0, 'months': 6, 'days': 0}},
        )

        response = self.client.post(reverse('edit_rule_settings', args=[rule.uuid]), {
            'name': 'Полгода',
            'new_rule_strategy': 'relative',
            'new_rule_months': 'много',
        })

        self.assertEqual(response.status_code, 400)
        rule.refresh_from_db()
        self.assertEqual(rule.rule['value']['months'], 6)

    def test_service_rejects_malformed_date(self):
        response = self.client.post(reverse('service_object', args=[self.root.uuid]), {
            'maintenance_date': '31-02-2027',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ActionHistory.objects.filter(data_object=self.root, action_type='maintenance').exists())

    def test_dates_builder_ignores_impossible_date(self):
        response = self.client.post(reverse('rules_dates_builder'), {
            'fixed_months': [], 'fixed_days': [],
            'new_fixed_month': '2', 'new_fixed_day': '31',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dates'], [])

    def test_dates_builder_ignores_out_of_range_month(self):
        response = self.client.post(reverse('rules_dates_builder'), {
            'fixed_months': [], 'fixed_days': [],
            'new_fixed_month': '13', 'new_fixed_day': '1',
        })

        self.assertEqual(response.context['dates'], [])

    def test_dates_builder_accepts_valid_date(self):
        response = self.client.post(reverse('rules_dates_builder'), {
            'fixed_months': [], 'fixed_days': [],
            'new_fixed_month': '4', 'new_fixed_day': '15',
        })

        self.assertEqual(response.context['dates'], [{'month': 4, 'day': 15}])


class BrokenRuleDataTests(BaseDataTestCase):
    """Расчёт даты не должен падать на повреждённых данных правила."""

    def _calc(self, rule_data):
        rule = DateUpdateRule.objects.create(name=f'Правило {id(rule_data)}', rule=rule_data)
        self.root.date_update_rule = rule
        return calculate_next_maintenance_date(self.root, base_date=datetime.date(2026, 5, 1))

    def test_non_numeric_relative_value_returns_none(self):
        self.assertIsNone(self._calc({'strategy': 'relative', 'value': {'months': 'шесть'}}))

    def test_garbage_fixed_value_returns_none(self):
        self.assertIsNone(self._calc({'strategy': 'fixed', 'value': ['мусор', 42]}))

    def test_invalid_month_is_skipped(self):
        result = self._calc({'strategy': 'fixed', 'value': [
            {'month': 99, 'day': 1}, {'month': 10, 'day': 15},
        ]})
        self.assertEqual(result, datetime.date(2026, 10, 15))

    def test_day_beyond_month_length_is_clamped(self):
        """31 июня превращается в 30 июня, а не в 28-е, как было раньше."""
        result = self._calc({'strategy': 'fixed', 'value': [{'month': 6, 'day': 31}]})
        self.assertEqual(result, datetime.date(2026, 6, 30))

    def test_february_29_in_common_year_is_clamped_to_28(self):
        result = self._calc({'strategy': 'fixed', 'value': [{'month': 2, 'day': 29}]})
        self.assertEqual(result, datetime.date(2027, 2, 28))


class MaintenancePlanStatsTests(BaseDataTestCase):
    """BUG-015: статистика плана ТО считала внеплановые и повторные работы."""

    def setUp(self):
        self.login(self.junior)
        self.monday = timezone.localdate() - datetime.timedelta(days=timezone.localdate().weekday())
        self.wednesday = self.monday + datetime.timedelta(days=2)

    def _plan(self, obj, day):
        obj.next_maintenance_date = day
        obj.save(update_fields=['next_maintenance_date'])

    def _service(self, obj, next_date=None, unplanned=False):
        payload = {}
        if unplanned:
            payload['is_unplanned'] = 'on'
        payload['maintenance_date'] = (next_date or (self.monday + datetime.timedelta(days=200))).isoformat()
        return self.client.post(reverse('service_object', args=[obj.uuid]), payload)

    def _week(self):
        response = self.client.get(reverse('dashboard'))
        return response.context['week_done'], response.context['week_all']

    def test_service_records_kind_and_closed_plan_date(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root)

        entry = ActionHistory.objects.get(data_object=self.root, action_type='maintenance')
        self.assertEqual(entry.maintenance_kind, 'planned')
        self.assertEqual(entry.planned_for, self.wednesday)

    def test_unplanned_service_is_marked_and_closes_nothing(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root, unplanned=True)

        entry = ActionHistory.objects.get(data_object=self.root, action_type='maintenance')
        self.assertEqual(entry.maintenance_kind, 'unplanned')
        self.assertIsNone(entry.planned_for)

    def test_unplanned_work_does_not_count_as_completed_plan(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root, unplanned=True)

        done, total = self._week()
        self.assertEqual(done, 0)
        self.assertEqual(total, 1)  # плановое событие всё ещё открыто

    def test_repeated_work_on_same_event_counts_once(self):
        """Две работы по одному плановому событию — одно выполнение."""
        self._plan(self.root, self.wednesday)
        self._service(self.root)
        # Повторная работа: возвращаем прежнюю плановую дату и обслуживаем снова
        self._plan(self.root, self.wednesday)
        self._service(self.root)

        self.assertEqual(
            ActionHistory.objects.filter(data_object=self.root, action_type='maintenance').count(), 2
        )
        done, total = self._week()
        self.assertEqual(done, 1)
        self.assertEqual(total, 1)

    def test_completed_and_open_events_are_summed(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root)
        self._plan(self.child, self.wednesday)

        done, total = self._week()
        self.assertEqual(done, 1)
        self.assertEqual(total, 2)

    def test_youtrack_work_item_does_not_count(self):
        """Списание времени из YouTrack — не выполнение локального плана."""
        ActionHistory.objects.create(
            data_object=self.root, action_type='maintenance',
            action='2h работы', youtrack_id='w-1', maintenance_kind='unplanned',
        )
        self._plan(self.root, self.wednesday)

        done, total = self._week()
        self.assertEqual(done, 0)
        self.assertEqual(total, 1)

    def test_service_outside_period_is_not_counted(self):
        far_past = self.monday - datetime.timedelta(days=60)
        self._plan(self.root, far_past)
        self._service(self.root)

        done, total = self._week()
        self.assertEqual(done, 0)
        self.assertEqual(total, 0)

    def test_percent_never_exceeds_one_hundred(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root)
        self._plan(self.root, self.wednesday)
        self._service(self.root)

        response = self.client.get(reverse('dashboard'))
        self.assertLessEqual(response.context['week_percent'], 100)

    def test_service_without_prior_plan_closes_no_event(self):
        self.root.next_maintenance_date = None
        self.root.save(update_fields=['next_maintenance_date'])
        self._service(self.root)

        entry = ActionHistory.objects.get(data_object=self.root, action_type='maintenance')
        self.assertEqual(entry.maintenance_kind, 'planned')
        self.assertIsNone(entry.planned_for)

        done, _ = self._week()
        self.assertEqual(done, 0)

    def test_empty_plan_gives_zero_percent_without_division_error(self):
        response = self.client.get(reverse('dashboard'))

        self.assertEqual(response.context['week_all'], 0)
        self.assertEqual(response.context['week_percent'], 0)

    def test_history_tab_shows_maintenance_kind(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root, unplanned=True)

        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'history'])).content.decode()
        self.assertIn('Внеплановое обслуживание', body)

    def test_history_tab_shows_closed_plan_date(self):
        self._plan(self.root, self.wednesday)
        self._service(self.root)

        body = self.client.get(reverse('object_tab', args=[self.root.uuid, 'history'])).content.decode()
        self.assertIn('план на', body)


class MaintenanceBackfillMigrationTests(TestCase):
    """Обратное заполнение вида обслуживания для накопленной истории."""

    def test_backfill_classifies_existing_records(self):
        from data.migrations import __name__ as _  # noqa: F401
        from importlib import import_module

        migration = import_module('data.migrations.0005_backfill_maintenance_kind')

        object_type = ObjectType.objects.create(type='Насос')
        model = ObjectModel.objects.create(object_type=object_type, name='НМ-1')
        obj = DataObject.objects.create(model=model, name='Объект')

        planned = ActionHistory.objects.create(
            data_object=obj, action_type='maintenance',
            action='[Плановое ТО] Замена масла',
        )
        unplanned = ActionHistory.objects.create(
            data_object=obj, action_type='maintenance',
            action='Внеплановое техническое обслуживание (след. ТО по графику: 01.01.2027)',
        )
        from_youtrack = ActionHistory.objects.create(
            data_object=obj, action_type='maintenance',
            action='Списание 2ч', youtrack_id='w-1',
        )
        other = ActionHistory.objects.create(
            data_object=obj, action_type='update', action='Изменено имя',
        )

        ActionHistory.objects.update(maintenance_kind=None, planned_for=None)

        class FakeApps:
            @staticmethod
            def get_model(app_label, model_name):
                return ActionHistory

        migration.backfill(FakeApps, None)

        planned.refresh_from_db()
        unplanned.refresh_from_db()
        from_youtrack.refresh_from_db()
        other.refresh_from_db()

        self.assertEqual(planned.maintenance_kind, 'planned')
        self.assertEqual(planned.planned_for, planned.created_at.date())
        self.assertEqual(unplanned.maintenance_kind, 'unplanned')
        self.assertIsNone(unplanned.planned_for)
        self.assertEqual(from_youtrack.maintenance_kind, 'unplanned')
        self.assertIsNone(other.maintenance_kind)


class MaintenanceListPeriodTests(BaseDataTestCase):
    """Список работ должен совпадать со счётчиком на плитке дашборда."""

    def setUp(self):
        self.login(self.junior)
        today = timezone.localdate()
        self.monday = today - datetime.timedelta(days=today.weekday())
        self.sunday = self.monday + datetime.timedelta(days=6)

        # Именно сегодня: начало недели уже может быть в прошлом,
        # и тогда объект попал бы заодно в просроченные.
        self.this_week = DataObject.objects.create(
            model=self.model, name='На этой неделе', next_maintenance_date=today
        )
        self.overdue = DataObject.objects.create(
            model=self.model, name='Просрочен', next_maintenance_date=self.monday - datetime.timedelta(days=30)
        )
        self.far_future = DataObject.objects.create(
            model=self.model, name='Далеко', next_maintenance_date=self.sunday + datetime.timedelta(days=60)
        )

    def _list(self, period):
        response = self.client.get(reverse('maintenance_list'), {'period': period})
        return list(response.context['objects'])

    def test_week_list_excludes_overdue(self):
        """Раньше список брал всё до конца недели и тянул просроченные."""
        objects = self._list('week')

        self.assertIn(self.this_week, objects)
        self.assertNotIn(self.overdue, objects)
        self.assertNotIn(self.far_future, objects)

    def test_week_list_length_matches_dashboard_counter(self):
        dashboard = self.client.get(reverse('dashboard'))

        self.assertEqual(len(self._list('week')), dashboard.context['week_all'])

    def test_month_list_length_matches_dashboard_counter(self):
        dashboard = self.client.get(reverse('dashboard'))

        self.assertEqual(len(self._list('month')), dashboard.context['month_all'])

    def test_overdue_stays_available_as_its_own_filter(self):
        objects = self._list('overdue')

        self.assertEqual(objects, [self.overdue])

    def test_all_period_returns_everything(self):
        self.assertEqual(len(self._list('all')), DataObject.objects.count())

    def test_unknown_period_falls_back_to_week(self):
        response = self.client.get(reverse('maintenance_list'), {'period': 'квартал'})

        self.assertEqual(response.context['period_label'], "План на неделю")
        self.assertNotIn(self.overdue, list(response.context['objects']))


class DictMobileLayoutTests(BaseDataTestCase):
    """Справочник на узком экране: видна либо ветка, либо карточка."""

    def test_layout_has_switchable_panes(self):
        self.login(self.junior)
        body = self.client.get(reverse('dict')).content.decode()

        self.assertIn('id="dict-layout"', body)
        self.assertIn('dict-pane-list', body)
        self.assertIn('dict-pane-detail', body)

    def test_back_button_is_present(self):
        self.login(self.junior)
        body = self.client.get(reverse('dict')).content.decode()

        self.assertIn('dict-back-btn', body)
        self.assertIn('mdShowDictList', body)

"""
Автотесты приложения data.

Запуск:
    python manage.py test --settings=core.settings_test

Набор покрывает то, что сейчас считается корректным поведением: модели и их
связи, разграничение доступа по ролям, расчёт срока ТО, дерево объектов,
вкладки карточки, комментарии и вложения, клонирование, поиск и экспорт.
"""

import datetime

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
)
from .templatetags.markdown_extras import markdown_format
from .views import calculate_next_maintenance_date, get_ancestors_chain

User = get_user_model()


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

    def test_invalid_date_is_ignored_and_does_not_break_creation(self):
        self.login(self.senior)
        self.client.post(reverse('create_object'), {
            'name': 'Кривая дата',
            'model': str(self.model.uuid),
            'maintenance_scheduling_mode': 'manual',
            'next_maintenance_date': 'не-дата',
        })

        created = DataObject.objects.get(name='Кривая дата')
        self.assertIsNone(created.next_maintenance_date)


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

    def test_card_renders_with_breadcrumbs_and_counters(self):
        self.login(self.senior)
        response = self.client.get(reverse('object_detail', args=[self.child.uuid]))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Насосная станция №1', body)
        self.assertIn('id="maintenance-pill"', body)
        self.assertIn('tab-count-comments', body)

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

    def test_info_tab_lists_child_components(self):
        self.login(self.junior)
        response = self.client.get(reverse('object_tab', args=[self.root.uuid, 'short_info']))

        self.assertEqual(response.status_code, 200)
        self.assertIn('Насос А', response.content.decode())

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
            path=SimpleUploadedFile('a.png', b'\x89PNG', content_type='image/png'),
        )
        self.client.post(reverse('add_attachment', args=[self.root.uuid]), {
            'file': SimpleUploadedFile('b.png', b'\x89PNG', content_type='image/png'),
            'is_preview': 'true',
        })

        first.refresh_from_db()
        self.assertFalse(first.is_preview)
        self.assertEqual(Attachment.objects.filter(data_object=self.root, is_preview=True).count(), 1)

    def test_set_preview_toggles_flag(self):
        att = Attachment.objects.create(
            user=self.senior, data_object=self.root,
            path=SimpleUploadedFile('c.png', b'\x89PNG', content_type='image/png'),
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
        # Регистр слов сохранён намеренно: SQLite (тестовая БД) выполняет LIKE
        # без учёта регистра только для ASCII, поэтому кириллицу здесь не меняем.
        self.login(self.junior)
        response = self.client.get(reverse('search'), {'q': 'станция Насосная'})
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

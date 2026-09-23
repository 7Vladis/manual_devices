"""
Разовая рассылка сводки по приближающимся ТО.

Команда идемпотентна по побочным эффектам в БД (ничего не пишет) и пригодна
как для планировщика, так и для ручного запуска:

    python manage.py send_maintenance_digest
    python manage.py send_maintenance_digest --dry-run
    python manage.py send_maintenance_digest --date 2026-10-01
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from notifications.tasks import run_daily_maintenance_check


class Command(BaseCommand):
    help = "Отправляет в Mattermost сводку по объектам с ТО сегодня, через 7 и через 30 дней."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Показать сообщения, но не отправлять их в Mattermost.",
        )
        parser.add_argument(
            '--date',
            help="Дата отсчёта в формате ГГГГ-ММ-ДД (по умолчанию — сегодня).",
        )

    def handle(self, *args, **options):
        base_date = None
        if options['date']:
            try:
                base_date = datetime.strptime(options['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError("Неверная дата: ожидается формат ГГГГ-ММ-ДД, например 2026-10-01.")

        messages = run_daily_maintenance_check(today=base_date, dry_run=options['dry_run'])

        if not messages:
            self.stdout.write("Объектов с приближающимся ТО не найдено — рассылка не требуется.")
            return

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(f"Режим --dry-run: подготовлено сообщений — {len(messages)}"))
            for msg in messages:
                self.stdout.write("-" * 60)
                self.stdout.write(msg)
        else:
            self.stdout.write(self.style.SUCCESS(f"Отправлено сообщений: {len(messages)}"))

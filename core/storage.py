"""Хранилище статики, устойчивое к отсутствию собранных файлов."""

import logging

from whitenoise.storage import CompressedManifestStaticFilesStorage

logger = logging.getLogger('django')


class ResilientManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """
    В production даёт хешированные имена файлов (кеш браузера сбрасывается сам
    при обновлении стилей). Но если collectstatic ещё не выполнялся — в
    разработке и в автотестах, — возвращает обычное имя вместо исключения,
    иначе ломается рендер любого шаблона с тегом {% static %}.
    """

    manifest_strict = False

    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content, filename)
        except ValueError:
            logger.debug("Статика не собрана, отдаю '%s' без хеша", name)
            return name

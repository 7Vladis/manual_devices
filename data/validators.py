"""
Проверки пользовательских файлов перед сохранением.

Файлы вложений отдаются с того же origin, что и приложение, поэтому активный
контент (SVG, HTML) загружать нельзя: он выполнится в браузере как часть сайта.
Изображения проверяются по реальной сигнатуре, а не по MIME-типу и расширению —
и то и другое полностью контролируется клиентом.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.defaultfilters import filesizeformat

# Сигнатуры растровых форматов, которые допускаем в качестве превью.
IMAGE_SIGNATURES = (
    (b'\xff\xd8\xff', 'jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'BM', 'bmp'),
)

# Расширения, которые браузер может исполнить при открытии по прямой ссылке.
BLOCKED_EXTENSIONS = {
    '.svg', '.svgz', '.html', '.htm', '.xhtml', '.xht', '.shtml',
    '.mhtml', '.mht', '.js', '.mjs', '.xml', '.xsl', '.xslt', '.swf',
}


def _max_size():
    return getattr(settings, 'MAX_ATTACHMENT_SIZE', 25 * 1024 * 1024)


def detect_image_format(file_obj):
    """Возвращает формат изображения по сигнатуре файла или None."""
    pos = file_obj.tell()
    try:
        file_obj.seek(0)
        header = file_obj.read(16)
    finally:
        file_obj.seek(pos)

    for signature, fmt in IMAGE_SIGNATURES:
        if header.startswith(signature):
            return fmt
    # WEBP: 'RIFF' .... 'WEBP'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    return None


def validate_attachment(file_obj, require_image=False):
    """
    Проверяет размер, расширение и (для превью) реальный формат изображения.
    Бросает ValidationError с понятным пользователю текстом.
    """
    if file_obj is None:
        raise ValidationError("Файл не выбран.")

    max_size = _max_size()
    if file_obj.size == 0:
        raise ValidationError("Файл пустой.")
    if file_obj.size > max_size:
        raise ValidationError(
            f"Файл слишком большой ({filesizeformat(file_obj.size)}). "
            f"Максимальный размер — {filesizeformat(max_size)}."
        )

    name = (file_obj.name or '').lower()
    for ext in BLOCKED_EXTENSIONS:
        if name.endswith(ext):
            raise ValidationError(
                f"Файлы {ext} загружать нельзя: такой файл может выполнить код в браузере. "
                "Для схем используйте PNG, JPEG или PDF."
            )

    if require_image:
        fmt = detect_image_format(file_obj)
        if fmt is None:
            raise ValidationError(
                "Файл превью должен быть изображением (PNG, JPEG, GIF, BMP или WEBP). "
                "Содержимое файла не распознано как изображение."
            )
        return fmt

    return None

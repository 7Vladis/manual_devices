# data/templatetags/markdown_extras.py

import os
import re
from urllib.parse import unquote

import markdown as md
import nh3
from django import template
from django.utils.safestring import mark_safe
from django.utils.text import get_valid_filename

register = template.Library()

# Разрешённый HTML на выходе Markdown. Всё, чего здесь нет (script, iframe, svg,
# on*-атрибуты, style, javascript:-ссылки), вырезается nh3 до попадания в шаблон.
ALLOWED_TAGS = {
    'p', 'br', 'hr',
    'strong', 'b', 'em', 'i', 'u', 's', 'del', 'code', 'pre', 'kbd', 'sub', 'sup',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li',
    'blockquote',
    'a', 'img', 'video',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'dl', 'dt', 'dd',
}

ALLOWED_ATTRIBUTES = {
    'a': {'href', 'title'},
    # width/height приходят из разметки YouTrack `![](имя.png){width=70%}`:
    # markdown разбирает её сам и кладёт размер в атрибут.
    'img': {'src', 'alt', 'title', 'width', 'height'},
    # Видео YouTrack вставляет той же разметкой, что и картинку, — отсюда
    # проигрыватель в разрешённых тегах. Источник проверяется так же строго.
    'video': {'src', 'controls', 'preload', 'width', 'height', 'class'},
    'th': {'align'},
    'td': {'align'},
    'code': {'class'},
    'pre': {'class'},
}

ALLOWED_URL_SCHEMES = {'http', 'https', 'mailto'}

# Вставка изображения в Markdown: ![подпись](адрес). Адресом бывает и ссылка,
# и просто имя файла — так пишет YouTrack, когда картинку вставили в текст.
# Следом он дописывает размер — `{width=70%}`; его markdown разбирает сам, но
# в выражение он включён: убирая картинку, надо убрать и размер, иначе на
# экране остаётся осиротевшее `{width=70%}`.
_IMAGE_RE = re.compile(r'!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]*)\)(?P<attrs>\{[^}]*\})?')

# Адреса, которые подставлять не нужно: они уже указывают куда надо.
_READY_PREFIXES = ('http://', 'https://', '/media/')

# Ссылки, которые ведут не на файл вложения и правки не требуют.
_READY_LINK_PREFIXES = _READY_PREFIXES + ('/', 'mailto:', '#')

# Размер картинки: только число с необязательным процентом. Всё остальное
# в атрибут не пускаем — он попадает в разметку как есть.
_SIZE_RE = re.compile(r'^\d{1,4}%?$')


def _attribute_filter_for(attachments):
    """
    Фильтр атрибутов nh3: вызывается на каждый атрибут уцелевшего тега.

    Здесь же чинятся ссылки на файлы. YouTrack пишет их именем файла
    (`[паспорт.pdf](паспорт.pdf)`), и без подстановки ссылка ведёт в никуда.
    Разобрать их выражением до markdown нельзя: в именах попадаются скобки
    (`...торнадо(104).pdf`), а их markdown считает сам — здесь же приходит
    готовый адрес.
    """
    def attribute_filter(tag, attr, value):
        value = (value or '').strip()

        # Изображения и видео — только из наших медиафайлов или по
        # абсолютному адресу.
        if tag in ('img', 'video') and attr == 'src':
            return value if value.startswith(_READY_PREFIXES) else None

        if attr in ('width', 'height'):
            return value if _SIZE_RE.match(value) else None

        if tag == 'a' and attr == 'href':
            if value.startswith(_READY_LINK_PREFIXES):
                return value
            attachment = _find_attachment(value, attachments)
            if attachment is not None and attachment.path:
                return attachment.path.url
            # Файла у нас нет: оставляем подпись текстом. Ссылка всё равно
            # вела бы в никуда, а подпись сама по себе осмысленна.
            return None

        return value

    return attribute_filter


# Голый адрес в тексте: человек пишет его без разметки, а ссылкой он должен
# стать сам. Хвостовую пунктуацию не забираем — точка в конце предложения к
# адресу не относится; закрывающую скобку берём только если открывающая была
# внутри адреса.
_BARE_URL_RE = re.compile(r'(?<![\w@/])(https?://[^\s<>"\']+)')
_URL_TAIL = '.,;:!?»”\'"'

# Теги, внутри которых ссылку делать нельзя: внутри `a` она уже есть,
# в `code` и `pre` адрес — часть примера, а не ссылка.
_NO_LINK_TAGS = ('a', 'code', 'pre')
_TAG_SPLIT_RE = re.compile(r'(<[^>]+>)')


def _linkify(html):
    """
    Превращает голые адреса в ссылки.

    Работает по готовой разметке, а не по тексту Markdown: так не нужно
    отличать адрес в тексте от адреса внутри `[подписи](ссылки)` — там он уже
    стал атрибутом тега и в текст не попадает. Разметку разбираем на теги и
    текст между ними и трогаем только текст вне `a`, `code` и `pre`.
    """
    parts = _TAG_SPLIT_RE.split(html)
    depth = 0
    for i, part in enumerate(parts):
        if part.startswith('<'):
            name = part[1:].lstrip('/').split(' ', 1)[0].rstrip('>/').lower()
            if name in _NO_LINK_TAGS:
                depth += -1 if part.startswith('</') else 1
                depth = max(depth, 0)
            continue
        if depth or 'http' not in part:
            continue

        def wrap(match):
            url = match.group(1).rstrip(_URL_TAIL)
            if url.endswith(')') and '(' not in url:
                url = url[:-1]
            tail = match.group(1)[len(url):]
            return f'<a href="{url}">{url}</a>{tail}'

        parts[i] = _BARE_URL_RE.sub(wrap, part)
    return ''.join(parts)


# Ссылка, у которой фильтр убрал адрес. Снимаем сам тег: иначе подпись
# выглядит ссылкой (стили применяются и к `a` без href), а нажатие ничего
# не делает. Выражение работает по уже очищенной разметке.
_EMPTY_LINK_RE = re.compile(r'<a(?![^>]*\shref=)[^>]*>(.*?)</a>', re.DOTALL)


def sanitize_html(html, attachments=()):
    """Прогоняет произвольный HTML через разрешающий список тегов и атрибутов."""
    cleaned = nh3.clean(
        _linkify(html),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter_for(attachments),
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel='noopener noreferrer',
        strip_comments=True,
    )
    return _EMPTY_LINK_RE.sub(r'\1', cleaned)


def _match_key(name):
    """
    Ключ для сверки имени из текста с именем файла на диске.

    В тексте стоит имя, которое дал YouTrack, а на диске лежит то, что из него
    сделало хранилище Django: пробелы стали подчёркиваниями, посторонние
    символы выпали. Без приведения к общему виду совпадения почти не бывает.
    """
    name = unquote((name or '').strip()).replace('\\', '/').rsplit('/', 1)[-1]
    return get_valid_filename(name).lower() if name else ''


# Суффикс, которым хранилище разводит файлы с одинаковыми именами в одной
# папке: `doc.pdf` рядом с таким же превращается в `doc_3xUTXOj.pdf`.
_STORAGE_SUFFIX_RE = re.compile(r'^(?P<stem>.+)_[A-Za-z0-9]{7}$')


def _stored_stem(key):
    """Имя файла на диске без расширения и без суффикса хранилища."""
    stem, ext = os.path.splitext(key)
    match = _STORAGE_SUFFIX_RE.match(stem)
    return (match.group('stem') if match else stem), ext


def _find_attachment(src, attachments):
    """
    Вложение, на которое ссылается вставка в тексте, или None.

    Точного совпадения имён мало: файл на диске мог получить от хранилища
    суффикс `_XXXXXXX` (рядом уже лежал такой же) и мог укоротиться под предел
    длины пути. Поэтому суффикс снимаем, а укороченное имя принимаем как начало
    исходного — но только если от него осталось хотя бы восемь символов: иначе
    «photo» прилипло бы к «photo2».
    """
    key = _match_key(src)
    if not key:
        return None

    keyed = [(_match_key(att.filename), att) for att in attachments]
    for att_key, att in keyed:
        if att_key == key:
            return att

    ref_stem, ref_ext = os.path.splitext(key)
    for att_key, att in keyed:
        stem, ext = _stored_stem(att_key)
        if ext != ref_ext or not stem:
            continue
        if stem == ref_stem or (len(stem) >= 8 and ref_stem.startswith(stem)):
            return att
    return None


# Размер из `{width=70%}`: markdown кладёт его в атрибут сам, но только для
# картинки. Для проигрывателя разбираем сами — там мы пишем готовый тег.
_WIDTH_IN_ATTRS_RE = re.compile(r'width\s*=\s*["\']?(\d{1,4}%?)')


def _substitute_images(text, attachments, used):
    """
    Меняет имена файлов во вставках на ссылки наших копий.

    Видео YouTrack вставляет той же разметкой, что и картинку: `![](ролик.mp4)`.
    Картинкой его не покажешь — браузер рисует битый значок, — поэтому на месте
    такой вставки появляется проигрыватель. Разметку пишем готовым тегом:
    markdown из `![]()` умеет делать только `img`.

    Вставку, которой файла не нашлось, убираем: битая картинка в ленте хуже её
    отсутствия. Так вёл себя фильтр и раньше — только тогда вырезались все
    относительные вставки без разбора, и порядок рассказа терялся.
    """
    def replace(match):
        src = (match.group('src') or '').strip()
        if src.startswith(_READY_PREFIXES):
            return match.group(0)

        att = _find_attachment(src, attachments)
        if att is None or not att.path:
            return ''

        used.append(att.uuid)
        attrs = match.group('attrs') or ''

        if getattr(att, 'is_video', False):
            size = _WIDTH_IN_ATTRS_RE.search(attrs)
            width = f' width="{size.group(1)}"' if size else ''
            # preload="metadata" тянет заголовок, а не весь ролик.
            return (f'<video src="{att.path.url}" controls preload="metadata"'
                    f' class="markdown-video"{width}></video>')

        return f"![{match.group('alt')}]({att.path.url}){attrs}"

    return _IMAGE_RE.sub(replace, text)


# Начало пункта списка. Отдельно — те, которыми список имеет право прервать
# абзац: маркированный любым знаком и нумерованный, начинающийся с единицы.
# Так же решает и CommonMark, по которому разбирает текст YouTrack: «2.» в
# середине абзаца остаётся текстом, а не превращается в список с другого номера.
_LIST_LINE_RE = re.compile(r'^ {0,3}(?:[-*+]\s+|\d{1,9}[.)]\s+)\S')
_LIST_INTERRUPT_RE = re.compile(r'^ {0,3}(?:[-*+]\s+|1[.)]\s+)\S')
_FENCE_RE = re.compile(r'^\s*(?:```|~~~)')


def _loosen_lists(text):
    """
    Вставляет пустую строку перед списком, начатым сразу после абзаца.

    python-markdown требует пустую строку между абзацем и списком, а разборщик
    YouTrack — нет. Из-за этого один и тот же текст выглядел по-разному: там
    пункты, здесь строка «- пункт» как есть. Текст не меняется — добавляется
    только перенос, которого ждёт разборщик.

    Внутрь блоков кода не лезем: там дефис в начале строки — часть примера.
    """
    result = []
    in_fence = False
    in_list = False

    for line in text.split('\n'):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            result.append(line)
            continue

        if in_fence:
            result.append(line)
            continue

        if not line.strip():
            in_list = False
            result.append(line)
            continue

        if _LIST_LINE_RE.match(line):
            if not in_list and result and result[-1].strip() and _LIST_INTERRUPT_RE.match(line):
                result.append('')
            in_list = True
            result.append(line)
            continue

        # Продолжение пункта с отступом списком быть не перестаёт.
        if in_list and line[:1] in (' ', '\t'):
            result.append(line)
            continue

        in_list = False
        result.append(line)

    return '\n'.join(result)


def render_markdown(text, attachments=()):
    """
    Преобразует Markdown в безопасный HTML.

    Возвращает пару: готовый HTML и uuid вложений, показанных прямо в тексте —
    повторять их плиткой под комментарием не надо.
    """
    if not text:
        return mark_safe(''), []

    attachments = list(attachments)
    used = []
    prepared = _loosen_lists(_substitute_images(text, attachments, used).strip())
    html = md.markdown(prepared, extensions=['extra', 'nl2br', 'sane_lists'])
    return mark_safe(sanitize_html(html, attachments)), used


@register.filter(name='markdown')
def markdown_format(text):
    """
    Markdown текста, у которого своих вложений нет: описание объекта, записи
    истории. Относительные вставки изображений здесь убираются — подставлять
    вместо них нечего.
    """
    html, _ = render_markdown(text)
    return html


@register.simple_tag(name='description_body')
def description_body(obj):
    """
    Описание объекта с картинками на своих местах.

    Описание приходит из YouTrack вместе с разметкой `![](имя.png){width=70%}`,
    а сами файлы лежат у объекта вложениями. Подставляем ссылки на наши копии;
    размер markdown кладёт в атрибут `width` сам. Раньше описание рендерилось
    фильтром без вложений, вставка вырезалась целиком — и на экране оставался
    осиротевший `{width=70%}`.
    """
    html, _ = render_markdown(obj.description, obj.attachments.all())
    return html


@register.simple_tag(name='comment_body')
def comment_body(comment):
    """
    Тело комментария: текст с картинками на своих местах и отдельно вложения,
    на которые в тексте не сослались.

    YouTrack вставляет картинку прямо в текст (`![](имя.png)`); раньше такая
    вставка вырезалась, а файл уезжал плиткой под комментарий, и рассказ
    рассыпался. Теперь она заменяется ссылкой на нашу копию, а под текстом
    остаётся только то, чего в нём нет.
    """
    attachments = list(comment.attachments.all())
    html, used = render_markdown(comment.text, attachments)
    shown = set(used)
    return {'html': html, 'extra': [att for att in attachments if att.uuid not in shown]}

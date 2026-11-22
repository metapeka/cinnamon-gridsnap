#!/usr/bin/env python3
"""zone_i18n — выбор языка интерфейса и каталоги переводов GridSnap.

Чистый модуль: ни X, ни GTK, ни `gi`. Его импортируют редактор, пикер, полоса
вкладок, горячие края, раскладчик, демон, клиент сокета, хоткеи и конфиг-слой,
поэтому правило выбора языка живёт ровно в одном месте и не расходится.

Язык исходника — русский: строки в коде русские, а поверх них ложатся каталоги
`en` и `zh_CN`. Каталога `ru` нет нарочно: русский — это `msgid` как есть.

Порядок выбора языка (первое непустое значение выигрывает):

1. `GRIDSNAP_LANG` — явное перекрытие (для тестов и для «хочу английский сейчас»);
2. `LC_ALL`;
3. `LC_MESSAGES`;
4. `LANG`.

Значение переводится в код языка по префиксу (`ru_RU.UTF-8` → `ru`, `zh-Hans` →
`zh_CN`); всё остальное (`de`, `fr`, `C`, `POSIX`, мусор) — английский, он же
язык по умолчанию. Переменная `LANGUAGE` нарочно не читается: правило выбора
должно быть одно и явное, а `GRIDSNAP_LANG` перекрывает всё.

Каталог ищется так: сначала собранный `.mo` (в `locale/` рядом с кодом, в
`share/locale` префикса или там, куда указал `GRIDSNAP_LOCALEDIR`), затем — если
`.mo` нет — `po/<язык>.po` рядом с модулем. Так владелец работает прямо из
чекаута, ничего не собирая, а пакет пользуется стандартным `.mo`. Ни `.mo`, ни
`.po` — не ошибка: работают исходные (русские) строки.
"""

import gettext
import os
import struct

# Домен Python-инструментов и отдельный домен апплета Cinnamon: xlet'ы берут
# перевод через `Gettext.bindtextdomain(uuid, ...)`, поэтому у них своё имя.
DOMAIN = "gridsnap"
APPLET_DOMAIN = "gridsnap@metapeka"

# Язык исходника (`msgid` как есть) и язык по умолчанию для всех прочих локалей.
SOURCE_LANGUAGE = "ru"
DEFAULT_LANGUAGE = "en"
# Языки, для которых есть каталог. `ru` тут нет: это исходные строки.
LANGUAGES = ("en", "zh_CN")

# Переопределение из `set_language` (None — «решать по окружению»).
_override = None
# Кэш каталогов: язык → {msgid: msgstr}.
_catalogs = {}


# --------------------------------------------------------------- выбор языка


def normalize_language(value):
    """Код языка по значению локали; пусто — `None`.

    `ru_RU.UTF-8@modifier` → `ru`, `zh-Hans` → `zh_CN`, `C`/`POSIX`/`de`/мусор
    → `en` (язык по умолчанию). Разбор — по префиксу до `_`/`-`, как принято у
    локалей: `zh_CN` и `zh_TW` у нас оба попадают в упрощённый каталог.
    """
    text = (value or "").strip().lower()
    # Кодировка и модификатор к языку не относятся: ru_RU.UTF-8@euro -> ru_ru.
    text = text.split(".", 1)[0].split("@", 1)[0]
    if not text:
        return None
    base = text.replace("-", "_").split("_", 1)[0]
    if base == SOURCE_LANGUAGE:
        return SOURCE_LANGUAGE
    if base == "zh":
        return "zh_CN"
    return DEFAULT_LANGUAGE


def resolve_language(*values):
    """Первый непустой код языка из переданных значений, иначе язык по умолчанию."""
    for value in values:
        code = normalize_language(value)
        if code is not None:
            return code
    return DEFAULT_LANGUAGE


def language():
    """Выбранный язык интерфейса: `ru`, `zh_CN` или `en`.

    Переопределение из :func:`set_language` важнее окружения; иначе смотрим
    `GRIDSNAP_LANG`, затем локаль в порядке gettext.
    """
    if _override is not None:
        return _override
    return resolve_language(
        os.environ.get("GRIDSNAP_LANG"),
        os.environ.get("LC_ALL"),
        os.environ.get("LC_MESSAGES"),
        os.environ.get("LANG"),
    )


def set_language(code=None):
    """Переопределить язык; `None`/`""`/`"auto"` возвращают выбор по окружению.

    Возвращает получившийся код языка. Нужен тестам и тем, кто хочет сменить
    язык, не трогая переменные окружения процесса.
    """
    global _override
    if code is None or (isinstance(code, str) and code.strip().lower() in ("", "auto")):
        _override = None
    else:
        _override = normalize_language(code) or DEFAULT_LANGUAGE
    return language()


# ------------------------------------------------------------------- каталоги


def module_dir():
    """Каталог, в котором лежит этот модуль (рядом — код и `po/`)."""
    return os.path.dirname(os.path.abspath(__file__))


def localedirs():
    """Каталоги-кандидаты для собранных `.mo`, в порядке приоритета.

    `$GRIDSNAP_LOCALEDIR` — явное перекрытие (тесты, ручная сборка); `locale/`
    рядом с кодом — сборка из чекаута; `../../share/locale` от каталога кода —
    префикс установки (`/usr/lib/gridsnap` → `/usr/share/locale`,
    `~/.local/lib/gridsnap` → `~/.local/share/locale`).
    """
    dirs = []
    env = (os.environ.get("GRIDSNAP_LOCALEDIR") or "").strip()
    if env:
        dirs.append(env)
    here = module_dir()
    dirs.append(os.path.join(here, "locale"))
    dirs.append(os.path.normpath(os.path.join(here, "..", "..", "share", "locale")))
    return dirs


def po_dir():
    """Каталог с `.po`-файлами рядом с модулем (`<чекаут>/po`)."""
    return os.path.join(module_dir(), "po")


def mo_path(lang, domain=DOMAIN):
    """Путь к собранному `.mo` для языка, или `None` — если его нигде нет."""
    for directory in localedirs():
        path = os.path.join(directory, lang, "LC_MESSAGES", domain + ".mo")
        if os.path.isfile(path):
            return path
    return None


def po_path(lang, domain=DOMAIN):
    """Путь к `.po` для языка, или `None`.

    Основной домен лежит как `po/<язык>.po`; отдельные домены (апплет) — в
    подкаталоге своего имени, `po/<домен>/<язык>.po`.
    """
    base = po_dir() if domain == DOMAIN else os.path.join(po_dir(), domain)
    path = os.path.join(base, lang + ".po")
    return path if os.path.isfile(path) else None


# --------------------------------------------------------------- разбор `.po`

# Эскейпы, которые GNU gettext пишет в `.po` (см. `po-gram-gen.y`).
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}


def unescape_po(text):
    """Снять эскейпы со строки `.po` (`\\n`, `\\t`, `\\"`, `\\\\` и прочие)."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            # Незнакомый эскейп оставляем как есть: лучше видимый текст, чем потеря.
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unquote(line):
    """Содержимое строки `.po`, если она начинается с кавычки; иначе `None`."""
    text = line.strip()
    if not text.startswith('"'):
        return None
    end = text.rfind('"')
    if end <= 0:
        return None
    return unescape_po(text[1:end])


def parse_po(text):
    """`{msgid: msgstr}` из текста `.po` (включая пустые `msgstr`).

    Понимает многострочные строки (соседние `"..."`), комментарии, `#, fuzzy`
    (запись пропускается) и заголовок файла (`msgid ""`). Множественные формы
    (`msgid_plural`) нам не нужны, поэтому `msgstr[n]` и `msgctxt` пропускаются:
    их в наших каталогах нет, и молча терять обычные записи из-за них нельзя.
    """
    catalog = {}
    msgid = None
    msgstr = None
    fuzzy = False
    # Запись не наша (`msgctxt`/`msgid_plural`) — пропускаем её целиком.
    skip = False

    def flush():
        nonlocal msgid, msgstr, fuzzy, skip
        if (msgid is not None and msgstr is not None and msgid != ""
                and not fuzzy and not skip):
            catalog[msgid] = msgstr
        msgid = msgstr = None
        fuzzy = False
        skip = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith("msgctxt"):
            skip = True
            continue
        if line.startswith("msgid_plural"):
            skip = True
            continue
        if line.startswith("msgstr["):
            continue
        if line.startswith("msgid"):
            # Новая запись без пустой строки-разделителя: предыдущую дописываем.
            if msgid is not None or msgstr is not None:
                flush()
            msgid = _unquote(line[len("msgid"):]) or ""
            continue
        if line.startswith("msgstr"):
            msgstr = _unquote(line[len("msgstr"):]) or ""
            continue
        if line.startswith('"'):
            part = _unquote(line)
            if part is None:
                continue
            if msgstr is not None:
                msgstr += part
            elif msgid is not None:
                msgid += part
            continue
        # Неизвестная строка: файл не наш, но и падать из-за неё незачем.
    flush()
    return catalog


# ------------------------------------------------------------------ переводы


def _read_catalog(lang):
    """`{msgid: msgstr}` языка из `.mo`, иначе из `.po`, иначе `{}`."""
    path = mo_path(lang)
    if path:
        try:
            with open(path, "rb") as handle:
                return dict(gettext.GNUTranslations(handle)._catalog)
        except (OSError, ValueError, struct.error, EOFError, UnicodeDecodeError):
            pass  # Битый .mo — не повод остаться без перевода: пробуем .po.
    path = po_path(lang)
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return parse_po(handle.read())
        except OSError:
            pass
    return {}


def catalog(lang=None):
    """`{msgid: msgstr}` выбранного языка; нет каталога — пустой словарь."""
    lang = lang or language()
    if lang not in _catalogs:
        _catalogs[lang] = {} if lang == SOURCE_LANGUAGE else _read_catalog(lang)
    return _catalogs[lang]


def reset_cache():
    """Забыть прочитанные каталоги (нужно тестам после подмены файлов)."""
    _catalogs.clear()


def translate(message):
    """Перевод строки; нет каталога или записи — исходный (русский) текст."""
    lang = language()
    if lang == SOURCE_LANGUAGE:
        return message
    return catalog(lang).get(message, message)


def _(message):
    """Короткий синоним :func:`translate` — под `_("строка")` в коде."""
    return translate(message)


if __name__ == "__main__":  # pragma: no cover - ручная проверка
    print(language())

#!/usr/bin/env python3
"""zone_i18n — choosing the interface language and the GridSnap translation catalogs.

A pure module: no X, no GTK, no `gi`. It is imported by the editor, the picker, the tab
strip, hot corners, the tiler, the daemon, the socket client, hotkeys and the config layer,
so the rule of choosing the language lives in exactly one place and does not diverge.

The source language is English: the strings in the code are English, and the `ru` and
`zh_CN` catalogs are laid on top of them. There is deliberately no `en` catalog: English
is the `msgid` as it is.

The order of choosing the language (the first non-empty value wins):

1. `GRIDSNAP_LANG` — an explicit override (for the tests and for "I want Russian now");
2. `LC_ALL`;
3. `LC_MESSAGES`;
4. `LANG`.

The value is turned into a language code by its prefix (`ru_RU.UTF-8` → `ru`, `zh-Hans`
→ `zh_CN`); everything else (`en`, `de`, `fr`, `C`, `POSIX`, garbage) is English, which
is both the source language and the default one. The `LANGUAGE` variable is deliberately
not read: the rule of choosing must be one and explicit, and `GRIDSNAP_LANG` overrides
everything.

The catalog is looked up like this: first the built `.mo` (in `locale/` next to the code, in
the prefix's `share/locale` or wherever `GRIDSNAP_LOCALEDIR` points it), then — if the `.mo`
is missing — `po/<language>.po` next to the module. That way the owner works straight
from the checkout without building anything, while the package uses the standard `.mo`.
Neither the `.mo` nor the `.po` is an error: the source (English) strings work.
"""

import gettext
import os
import struct

# The domain of the Python tools and a separate domain of the Cinnamon applet: the xlets get
# their translations through `Gettext.bindtextdomain(uuid, ...)`, hence a name of their own.
DOMAIN = "gridsnap"
APPLET_DOMAIN = "gridsnap@metapeka"

# The source language (`msgid` as it is) and the default language for every other locale.
SOURCE_LANGUAGE = "en"
DEFAULT_LANGUAGE = "en"
# The languages that have a catalog. There is no `en` here: those are the source strings.
LANGUAGES = ("ru", "zh_CN")

# Locale → catalog: `zh` covers both `zh_CN` and `zh_TW`; `ru` has a catalog of its own,
# everything else is the default language (the source strings).
LANGUAGE_ALIASES = {"zh": "zh_CN"}

# The override from `set_language` (None — "decide from the environment").
_override = None
# The catalog cache: language → {msgid: msgstr}.
_catalogs = {}


# ----------------------------------------------------- choosing the language


def normalize_language(value):
    """The language code for a locale value; empty — `None`.

    `ru_RU.UTF-8@modifier` → `ru`, `zh-Hans` → `zh_CN`, `en`/`C`/`POSIX`/`de`/
    garbage → `en` (the source language and the default one). Parsing is by the prefix
    before `_`/`-`, as is customary for locales: `zh_CN` and `zh_TW` both land in our
    simplified catalog.
    """
    text = (value or "").strip().lower()
    # The encoding and the modifier are not part of the language: ru_RU.UTF-8@euro -> ru_ru.
    text = text.split(".", 1)[0].split("@", 1)[0]
    if not text:
        return None
    base = text.replace("-", "_").split("_", 1)[0]
    if base in LANGUAGES:
        return base
    if base in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[base]
    return DEFAULT_LANGUAGE


def resolve_language(*values):
    """The first non-empty language code of those passed, otherwise the default."""
    for value in values:
        code = normalize_language(value)
        if code is not None:
            return code
    return DEFAULT_LANGUAGE


def language():
    """The chosen interface language: `ru`, `zh_CN` or `en`.

    The override from :func:`set_language` matters more than the environment; otherwise we
    look at `GRIDSNAP_LANG`, then at the locale in gettext order.
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
    """Override the language; `None`/`""`/`"auto"` return the choice from the environment.

    Returns the resulting language code. It is needed by the tests and by those who want to
    change the language without touching the environment variables of the process.
    """
    global _override
    if code is None or (isinstance(code, str) and code.strip().lower() in ("", "auto")):
        _override = None
    else:
        _override = normalize_language(code) or DEFAULT_LANGUAGE
    return language()


# ------------------------------------------------------------------- catalogs


def module_dir():
    """The directory this module lives in (the code and `po/` are next to it)."""
    return os.path.dirname(os.path.abspath(__file__))


def localedirs():
    """The candidate directories for the built `.mo` files, in order of priority.

    `$GRIDSNAP_LOCALEDIR` is an explicit override (tests, a manual build); `locale/`
    next to the code is a build from the checkout; `../../share/locale` from the code
    directory is the installation prefix (`/usr/lib/gridsnap` → `/usr/share/locale`,
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
    """The directory with the `.po` files next to the module (`<checkout>/po`)."""
    return os.path.join(module_dir(), "po")


def mo_path(lang, domain=DOMAIN):
    """The path to the built `.mo` for a language, or `None` if there is none anywhere."""
    for directory in localedirs():
        path = os.path.join(directory, lang, "LC_MESSAGES", domain + ".mo")
        if os.path.isfile(path):
            return path
    return None


def po_path(lang, domain=DOMAIN):
    """The path to the `.po` for a language, or `None`.

    The main domain lives as `po/<language>.po`; separate domains (the applet) live in
    a subdirectory of their own name, `po/<domain>/<language>.po`.
    """
    base = po_dir() if domain == DOMAIN else os.path.join(po_dir(), domain)
    path = os.path.join(base, lang + ".po")
    return path if os.path.isfile(path) else None


# -------------------------------------------------------------- parsing `.po`

# The escapes GNU gettext writes into `.po` (see `po-gram-gen.y`).
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
    """Strip the escapes from a `.po` string (`\\n`, `\\t`, `\\"`, `\\\\` and the rest)."""
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
            # An unknown escape is left as it is: visible text is better than a loss.
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unquote(line):
    """The content of a `.po` line if it starts with a quote; otherwise `None`."""
    text = line.strip()
    if not text.startswith('"'):
        return None
    end = text.rfind('"')
    if end <= 0:
        return None
    return unescape_po(text[1:end])


def parse_po(text):
    """`{msgid: msgstr}` from the text of a `.po` (including empty `msgstr`).

    It understands multi-line strings (adjacent `"..."`), comments, `#, fuzzy`
    (the entry is skipped) and the file header (`msgid ""`). We do not need plural
    forms (`msgid_plural`), so `msgstr[n]` and `msgctxt` are skipped: our catalogs have
    none of them, and ordinary entries must not be lost silently because of them.
    """
    catalog = {}
    msgid = None
    msgstr = None
    fuzzy = False
    # Not our entry (`msgctxt`/`msgid_plural`) — skip it entirely.
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
            # A new entry with no blank separator line: finish off the previous one first.
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
        # An unknown line: the file is not ours, but there is no reason to fall over it.
    flush()
    return catalog


# -------------------------------------------------------------- translations


def _read_catalog(lang):
    """`{msgid: msgstr}` of a language, from `.mo`, otherwise from `.po`, otherwise `{}`."""
    path = mo_path(lang)
    if path:
        try:
            with open(path, "rb") as handle:
                return dict(gettext.GNUTranslations(handle)._catalog)
        except (OSError, ValueError, struct.error, EOFError, UnicodeDecodeError):
            pass  # A corrupt .mo is no reason to be left without a translation: try .po.
    path = po_path(lang)
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return parse_po(handle.read())
        except OSError:
            pass
    return {}


def catalog(lang=None):
    """`{msgid: msgstr}` of the chosen language; no catalog — an empty dictionary."""
    lang = lang or language()
    if lang not in _catalogs:
        _catalogs[lang] = {} if lang == SOURCE_LANGUAGE else _read_catalog(lang)
    return _catalogs[lang]


def reset_cache():
    """Forget the catalogs that have been read (the tests need it after swapping files)."""
    _catalogs.clear()


def translate(message):
    """Translate a string; no catalog or no entry — the source (English) text."""
    lang = language()
    if lang == SOURCE_LANGUAGE:
        return message
    return catalog(lang).get(message, message)


def _(message):
    """A short synonym of :func:`translate` — for `_("string")` in the code."""
    return translate(message)


if __name__ == "__main__":  # pragma: no cover - a manual check
    print(language())

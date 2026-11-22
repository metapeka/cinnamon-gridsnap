#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""man-страницы: полнота каталогов po4a, сверка с CLI и то, что видит пользователь.

Man-страницы — не gettext-домен, а локализованные страницы: po4a делает `.po` из
roff, собранные страницы ставятся в `/usr/share/man/<язык>/man1/`. Этот тест
держит то, что иначе тихо гниёт:

* каждый `msgid` шаблона `po/man/gridsnap.1.pot` переведён в `en.po` и
  `zh_CN.po`, лишних записей нет и `fuzzy` нет — читается своим
  `zone_i18n.parse_po`, поэтому ни `gettext`, ни `po4a` тесту не нужны;
* **страница не отстала от кода**: множества длинных ключей `--…` в
  `man/gridsnap.1` и в `--help` семи команд совпадают; все секции `zones.conf`,
  которые знает `zone_config`, имеют свой `.SS`; команды и подкоманды из раздела
  ПРИМЕРЫ настоящие;
* **собранные страницы** (если `make man` уже прогнан) ведут себя как у
  пользователя: кладутся во временный `MANPATH`, и `man gridsnap` под нужной
  локалью показывает нужный язык, укладывается в колонку и не оставляет строк,
  которые troff не смог разорвать.

Про `man путь/zh_CN/man1/gridsnap.1`: так проверять нельзя — man-db ведёт себя
иначе, когда ему дают файл из каталога локали, и замер врёт (на этом уже ловили
ложную тревогу). Поэтому временный `MANPATH` и локаль, как в жизни, причём путь
берётся вида `<tmp>/usr/share/man`, а не короткий `<tmp>`: man-db включает
языковые макросы groff только для такой раскладки. Разница измеримая и решает
всё: без точек переноса страница даёт 254 колонки под коротким путём и 193 под
настоящим, а с точками — 79 в обоих.

Локаль `zh_CN.UTF-8` нужна, чтобы страница легла в колонку: без неё groff
считает иероглиф одной колонкой вместо двух. Если локали в системе нет, языковые
проверки пропускаются (`make check-man-i18n` в CI ставит их заранее);
ширина и «cannot break line» проверяются всё равно.

Запуск:
    python3 tests/test_man_i18n.py
    python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402
import zone_i18n as i18n  # noqa: E402

MAN = os.path.join(ROOT, "man")
SOURCE = os.path.join(MAN, "gridsnap.1")
BUILD = os.path.join(MAN, "build")
POT = os.path.join(ROOT, "po", "man", "gridsnap.1.pot")
LANGUAGES = ("en", "zh_CN")
# Язык страницы в man1/ — английский (`MAN_DEFAULT` в Makefile); `ru` собирается
# копией источника, поэтому в каталогах po4a его нет.
BUILT_LANGS = ("en", "ru", "zh_CN")
# Язык → локаль, под которой man-db обязан выбрать именно его страницу.
LOCALES = {"en": "C", "ru": "ru_RU.UTF-8", "zh_CN": "zh_CN.UTF-8"}
ALIASES = (
    "gridsnap-tiler.1",
    "gridsnap-daemon.1",
    "gridsnap-editor.1",
    "gridsnap-picker.1",
    "gridsnap-ctl.1",
    "setup-hotkeys.1",
)

# Команда-запускалка → файл в репозитории. `--help` у всех работает без дисплея,
# но троим для импорта нужен `python3-gi`.
COMMANDS = (
    ("gridsnap-tiler", "zone-tiler.py"),
    ("gridsnap-daemon", "zone-daemon.py"),
    ("gridsnap-editor", "zone-editor.py"),
    ("gridsnap-picker", "zone-picker.py"),
    ("gridsnap-hotcorners", "zone-hotcorners.py"),
    ("gridsnap-ctl", "gridsnap-ctl"),
    ("setup-hotkeys", "setup-hotkeys.py"),
)
FILE_OF = dict(COMMANDS)

OPTION_RE = re.compile(r"--([a-z][a-z-]*)")
SECTION_RE = re.compile(r'^\.SS "?\[([^\]]+)\]"?', re.M)
# Скелет страницы: макросы структуры, а не шрифта. `.B x` po4a превращает в
# `\fBx\fP`, поэтому шрифтовые макросы в сверке не участвуют.
STRUCT_RE = re.compile(r"^\.(SH|SS|TP|PP|IP|RS|RE|nf|fi|TH|EX|EE)\b")
BUILD_NOTICE_RE = re.compile(r'^\.\\"')
# Ширина страницы: man-db верстает в 80 колонок, текст — 78 минус отступ.
MAX_WIDTH = 90
# Единственное предупреждение, которое может остаться у CJK-страницы: troff не
# может растянуть строку без пробелов, поэтому она остаётся неполной. Пользователь
# этого не видит (обычный `man` предупреждений не печатает), а вот неразрывная
# строка — видит: она уезжает за ширину терминала.
TOLERATED_WARNING = "cannot adjust line"
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
CJK_RE = re.compile(r"[\u2e80-\u9fff\u3040-\u30ff\uff00-\uffef]")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def po_entries(path):
    """Записи каталога без шапки; `fuzzy`, `msgctxt` и `msgid_plural` пропускает."""
    return {key: value for key, value in i18n.parse_po(read(path)).items() if key}


def source_body():
    """Исходная страница без комментариев `.\\"` (в них есть `--warnings`)."""
    return "\n".join(
        line for line in read(SOURCE).splitlines()
        if not line.startswith('.\\"')
    )


def option_names(text):
    """Длинные ключи `--…`; `\\-` в roff — экранированный дефис."""
    return set(OPTION_RE.findall(text.replace("\\-", "-")))


def help_text(binary):
    """Вывод `--help` команды или `None`, если она не запускается без GTK."""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env["GRIDSNAP_LANG"] = "ru"
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, binary), "--help"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def built_help():
    """`--help` всех запустившихся команд; пропущенные — отдельным списком."""
    texts, skipped = {}, []
    for name, binary in COMMANDS:
        text = help_text(binary)
        if text is None:
            skipped.append(name)
        else:
            texts[name] = text
    return texts, skipped


def skeleton(text):
    """Последовательность структурных макросов страницы (без аргументов)."""
    found = []
    for line in text.splitlines():
        if BUILD_NOTICE_RE.match(line):
            continue
        match = STRUCT_RE.match(line)
        if match:
            found.append(match.group(1))
    return found


def example_lines():
    """Строки блока `.nf` … `.fi` из раздела ПРИМЕРЫ."""
    lines = read(SOURCE).splitlines()
    try:
        start = lines.index(".nf")
        end = lines.index(".fi", start)
    except ValueError:  # pragma: no cover — страница сломана
        return []
    return [
        line.strip().replace("\\-", "-")
        for line in lines[start + 1:end]
        if line.strip()
    ]


def display_width(line):
    """Ширина строки в колонках: иероглиф занимает две."""
    return sum(
        2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line
    )


def locale_available(name):
    """Есть ли локаль в системе (`locale -a` знает её под своим написанием)."""
    proc = subprocess.run(["locale", "-a"], stdout=subprocess.PIPE, text=True)
    normalize = lambda text: text.lower().replace("-", "").replace("_", "").replace(".", "")
    wanted = normalize(name)
    return any(normalize(line) == wanted for line in proc.stdout.splitlines())


class ManCatalogsTest(unittest.TestCase):
    """Каталоги po4a полны и не содержат мусора."""

    def test_pot_matches_source(self):
        self.assertTrue(os.path.exists(POT), "нет po/man/gridsnap.1.pot: make man-pot")
        entries = po_entries(POT)
        self.assertGreater(len(entries), 100, "в шаблоне подозрительно мало записей")

    def test_catalogs_cover_pot(self):
        pot = po_entries(POT)
        for lang in LANGUAGES:
            path = os.path.join(ROOT, "po", "man", f"{lang}.po")
            self.assertTrue(os.path.exists(path), f"нет {path}")
            catalog = po_entries(path)
            missing = sorted(set(pot) - set(catalog))
            empty = sorted(k for k, v in catalog.items() if not v.strip())
            extra = sorted(set(catalog) - set(pot))
            self.assertEqual(missing, [], f"{lang}.po: нет перевода этих строк")
            self.assertEqual(empty, [], f"{lang}.po: пустой msgstr у этих строк")
            self.assertEqual(extra, [], f"{lang}.po: лишние строки, их нет в шаблоне")

    def test_no_fuzzy(self):
        for lang in LANGUAGES:
            path = os.path.join(ROOT, "po", "man", f"{lang}.po")
            fuzzy = [
                n for n, line in enumerate(read(path).splitlines(), 1)
                if line.startswith("#,") and "fuzzy" in line
            ]
            self.assertEqual(fuzzy, [], f"{lang}.po: fuzzy на строках {fuzzy}")


class ManMatchesCliTest(unittest.TestCase):
    """Страница не отстала от кода — ни в ключах, ни в секциях, ни в примерах."""

    @classmethod
    def setUpClass(cls):
        cls.help_texts, cls.skipped = built_help()

    def test_options_match_cli_both_ways(self):
        """Ключи страницы и ключи `--help` — одно и то же множество.

        Сверка двусторонняя, поэтому командам с GTK нужен `python3-gi`: без
        него (юнит-job CI) обе стороны множества неполны и тест пропускается,
        а полный прогон делает `make check-man-i18n`.
        """
        if self.skipped:
            self.skipTest(f"без GTK не запускаются: {', '.join(self.skipped)}")
        man = option_names(source_body())
        real = set()
        for text in self.help_texts.values():
            real |= option_names(text)
        self.assertGreater(len(real), 20, "ни одна команда не ответила на --help")
        self.assertEqual(sorted(man - real), [],
                         "в man-странице есть ключи, которых нет у команд")
        self.assertEqual(sorted(real - man), [],
                         "у команд есть ключи, не описанные в man-странице")

    def test_config_sections_are_documented(self):
        known = set(zc.RESERVED_SECTIONS)
        known |= {
            zc.MOVE_SECTION, zc.FOCUS_SECTION, zc.TABS_SECTION,
            zc.WORKSPACES_SECTION, zc.MONITORS_SECTION,
            zc.ACTION_SECTION_PREFIX + "<имя>",
        }
        known |= set(zc.BEFORE_KEYS) | set(zc.AFTER_KEYS)
        documented = set(SECTION_RE.findall(read(SOURCE)))
        self.assertEqual(sorted(known - documented), [],
                         "секции zones.conf нет в man-странице")

    def test_example_commands_are_real(self):
        actions = set(zc.load_zonetiler().PROFILE_ACTIONS)
        for line in example_lines():
            tokens = line.split()
            binary = FILE_OF.get(tokens[0])
            self.assertIsNotNone(binary, f"пример на неизвестную команду: {line!r}")
            text = self.help_texts.get(tokens[0])
            if text is None:
                continue  # команда без GTK не запускается — пропускаем
            # Подкоманда — второе слово; у `profile` она третья, и её набор
            # задаёт не argparse, а PROFILE_ACTIONS в zone-tiler.py.
            if len(tokens) > 1 and tokens[1] == "profile":
                if len(tokens) <= 2:
                    continue
                self.assertIn(tokens[2], actions,
                              f"нет действия profile {tokens[2]!r} (пример {line!r})")
                continue
            if len(tokens) <= 1 or tokens[1].startswith("-"):
                continue
            self.assertIn(tokens[1], text,
                          f"в --help {tokens[0]} нет подкоманды {tokens[1]!r} "
                          f"(пример {line!r})")

    def test_source_renders_without_warnings(self):
        if shutil.which("man") is None:
            self.skipTest("нет man-db")
        proc = subprocess.run(
            ["man", "--warnings", "-E", "UTF-8", "-l", SOURCE],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.strip(), "", proc.stderr)


class BuiltPagesTest(unittest.TestCase):
    """Собранные `make man` страницы так, как их видит пользователь.

    Без `make man` (юнит-job CI, где нет po4a) тесты пропускаются: полноту
    каталогов они всё равно проверяют выше, а сборку гоняет `make check-man-i18n`.
    """

    manpath = None

    @classmethod
    def setUpClass(cls):
        cls.built = os.path.isdir(BUILD)
        if cls.built:
            # Путь именно `<tmp>/usr/share/man`: под коротким `<tmp>` man-db не
            # включает языковые макросы groff и страница форматируется иначе,
            # чем у пользователя (проверено: без точек переноса 254 колонки
            # против 193 под настоящим путём).
            cls.root = tempfile.mkdtemp(prefix="gridsnap-man-")
            cls.manpath = os.path.join(cls.root, "usr", "share", "man")
            for lang in BUILT_LANGS:
                sub = "" if lang == "en" else os.path.join(lang, "")
                target = os.path.join(cls.manpath, sub, "man1")
                os.makedirs(target, exist_ok=True)
                for name in os.listdir(os.path.join(BUILD, lang, "man1")):
                    shutil.copy(
                        os.path.join(BUILD, lang, "man1", name),
                        os.path.join(target, name),
                    )

    @classmethod
    def tearDownClass(cls):
        if cls.root:
            shutil.rmtree(cls.root, ignore_errors=True)

    def page(self, lang, name="gridsnap.1"):
        return os.path.join(BUILD, lang, "man1", name)

    def render_man(self, locale, *args):
        env = dict(os.environ)
        env["LC_ALL"] = locale
        env["MANPATH"] = self.manpath
        for key in ("LANG", "LANGUAGE", "LC_MESSAGES"):
            env.pop(key, None)
        return subprocess.run(
            ["man", *args], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60,
        )

    def test_all_languages_and_aliases_exist(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        for lang in BUILT_LANGS:
            self.assertTrue(os.path.exists(self.page(lang)), lang)
            for alias in ALIASES:
                path = self.page(lang, alias)
                self.assertTrue(os.path.exists(path), path)
                self.assertEqual(read(path).strip(), ".so man1/gridsnap.1", path)

    def test_structure_survives_translation(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        expected = skeleton(read(SOURCE))
        self.assertGreater(len(expected), 50, "в источнике мало структурных макросов")
        for lang in BUILT_LANGS:
            self.assertEqual(skeleton(read(self.page(lang))), expected,
                             f"скелет {lang} разошёлся с источником")

    def test_russian_page_is_the_source(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        with open(SOURCE, "rb") as src, open(self.page("ru"), "rb") as ru:
            self.assertEqual(ru.read(), src.read(),
                             "русская страница должна быть копией man/gridsnap.1")

    def test_locale_chooses_the_page(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        expected = {
            "C": lambda text: not CYRILLIC_RE.search(text) and not CJK_RE.search(text),
            "ru_RU.UTF-8": lambda text: bool(CYRILLIC_RE.search(text)),
            "zh_CN.UTF-8": lambda text: bool(CJK_RE.search(text)),
        }
        for locale, ok in expected.items():
            if locale != "C" and not locale_available(locale):
                continue  # локаль не сгенерирована — проверять нечего
            proc = self.render_man(locale, "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            self.assertTrue(ok(proc.stdout), f"{locale}: man gridsnap показал не ту страницу")

    def test_pages_fit_the_column(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        for locale in LOCALES.values():
            if locale != "C" and not locale_available(locale):
                continue
            proc = self.render_man(locale, "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            width = max((display_width(line) for line in proc.stdout.splitlines()),
                        default=0)
            self.assertLessEqual(
                width, MAX_WIDTH,
                f"{locale}: строка шириной {width} колонок — troff не разорвал её")

    def test_no_unbreakable_lines(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        for locale in LOCALES.values():
            if locale != "C" and not locale_available(locale):
                continue
            proc = self.render_man(locale, "--warnings", "-E", "UTF-8", "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            bad = [
                line for line in proc.stderr.splitlines()
                if "warning" in line and "cannot break line" in line
            ]
            self.assertEqual(bad, [], f"{locale}: неразрывные строки: {bad[:3]}")


if __name__ == "__main__":
    unittest.main()

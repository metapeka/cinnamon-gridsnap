#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""man-страницы: полнота каталогов po4a, сверка с реальным CLI и сборка.

Man-страницы — не gettext-домен, а локализованные страницы: po4a делает `.po` из
roff, собранные страницы ставятся в `/usr/share/man/<язык>/man1/`. Этот тест
держит то, что иначе тихо гниёт:

* каждый `msgid` шаблона `po/man/gridsnap.1.pot` переведён в `en.po` и
  `zh_CN.po`, лишних записей нет и `fuzzy` нет — читается своим
  `zone_i18n.parse_po`, поэтому ни `gettext`, ни `po4a` тесту не нужны;
* **страница не отстала от кода**: каждый длинный ключ `--…` из страницы
  принимается `--help` какой-нибудь команды, и наоборот — каждый ключ из
  `--help` описан в странице; все секции `zones.conf`, которые знает
  `zone_config`, имеют свой `.SS`; команды и подкоманды из раздела ПРИМЕРЫ —
  настоящие (слово обязано встречаться в `--help`);
* **собранные страницы** (если `make man` уже прогнан) не потеряли ни одного
  структурного макроса, а русская страница байт-в-байт равна источнику.

Команды, которым нужен `python3-gi` (редактор, пикер, горячие края), в
юнит-job'е CI пропускаются — там GTK нет; полный прогон делает `make
check-man-i18n` и job «Каталоги переводов».

Запуск:
    python3 tests/test_man_i18n.py
    python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import sys
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


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def po_entries(path):
    """Записи каталога без шапки; `fuzzy`, `msgctxt` и `msgid_plural` пропускает."""
    return {key: value for key, value in i18n.parse_po(read(path)).items() if key}


def source_body():
    """Исходная страница без комментариев `\\.\\"` (в них есть `--warnings`)."""
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

    def test_man_options_exist_in_cli(self):
        man = option_names(source_body())
        real = set()
        for text in self.help_texts.values():
            real |= option_names(text)
        self.assertTrue(real, "ни одна команда не ответила на --help")
        self.assertEqual(sorted(man - real), [],
                         "в man-странице есть ключи, которых нет у команд")

    def test_cli_options_are_documented(self):
        man = option_names(source_body())
        real = set()
        for text in self.help_texts.values():
            real |= option_names(text)
        if self.skipped:
            self.skipTest(f"без GTK не запускаются: {', '.join(self.skipped)}")
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
    """Собранные `make man` страницы: скелет, языки, псевдонимы.

    Без `make man` (юнит-job CI, где нет po4a) тесты пропускаются: полноту
    каталогов они всё равно проверяют выше, а сборку гоняет `make check-man-i18n`.
    """

    @classmethod
    def setUpClass(cls):
        cls.built = os.path.isdir(BUILD)

    def page(self, lang):
        return os.path.join(BUILD, lang, "man1", "gridsnap.1")

    def test_all_languages_and_aliases_exist(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        for lang in BUILT_LANGS:
            self.assertTrue(os.path.exists(self.page(lang)), lang)
            for alias in ALIASES:
                path = os.path.join(BUILD, lang, "man1", alias)
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

    def test_built_pages_render_without_warnings(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        for lang in BUILT_LANGS:
            proc = subprocess.run(
                ["man", "--warnings", "-E", "UTF-8", "-l", self.page(lang)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(proc.returncode, 0, f"{lang}: {proc.stderr}")
            self.assertEqual(proc.stderr.strip(), "", f"{lang}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Полнота каталогов переводов — держит тест, а не надежда.

Три обещания волны локализации:

* каждый `msgid` из шаблона (`.pot`) имеет непустой `msgstr` в `en.po` и
  `zh_CN.po` — и наоборот, в каталоге нет строк, которых нет в коде (иначе
  перевод тихо гниёт);
* то же для отдельного домена апплета `gridsnap@metapeka`;
* под `GRIDSNAP_LANG=en` CLI действительно печатает по-английски, а под
  `GRIDSNAP_LANG=ru` — по-русски: каталог не просто лежит на диске, а работает.

Шаблон и каталоги читаются собственным разбором `zone_i18n.parse_po`, так что
ни `xgettext`, ни `msgfmt` тестам не нужны (в CI их ставит только job каталогов).
Сверку шаблона с кодом делает `make check-i18n`.

Запуск:
    python3 tests/test_i18n_catalogs.py
    python3 -m unittest discover -s tests -v
"""

import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_i18n as i18n  # noqa: E402

# Языки, у которых обязан быть полный каталог. `en` тут нет: английский — язык
# исходника, каталога у него нет и быть не должно.
LANGUAGES = ("ru", "zh_CN")

CYRILLIC = re.compile(r"[\u0400-\u04ff]")

# Команды, которые обязаны говорить на выбранном языке и запускаются без X:
# раскладчик подтягивает `gi` только внутри `monitors()`, клиент сокета и
# установщик сочетаний вообще без GTK.
ENGLISH_COMMANDS = (
    ["zone-tiler.py", "--help"],
    ["zone-daemon.py", "--help"],
    ["setup-hotkeys.py", "--help"],
    ["gridsnap-ctl", "--help"],
)


def read_po(path):
    with open(path, encoding="utf-8") as handle:
        return i18n.parse_po(handle.read())


def entries_only(table):
    """Записи каталога без служебной шапки (`msgid ""`)."""
    return {key: value for key, value in table.items() if key}


def run(argv, lang):
    """Запустить команду с заданным языком интерфейса и вернуть текст вывода."""
    env = dict(os.environ)
    env["GRIDSNAP_LANG"] = lang
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, argv[0])] + list(argv[1:]),
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.stdout


class TestPotMatchesCode(unittest.TestCase):
    """Шаблон собран из кода: он и есть список того, что надо перевести."""

    def test_main_pot_has_entries_and_utf8(self):
        path = os.path.join(ROOT, "po", "gridsnap.pot")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertTrue(entries_only(read_po(path)),
                        "po/gridsnap.pot пуст — make pot не собрал строки")
        self.assertIn("charset=UTF-8", raw, "в шапке пустой или неверный charset")

    def test_applet_pot_has_entries(self):
        table = read_po(os.path.join(ROOT, "po", i18n.APPLET_DOMAIN,
                                     i18n.APPLET_DOMAIN + ".pot"))
        self.assertTrue(entries_only(table), "шаблон апплета пуст")


class CatalogChecks:
    """Сверка одного домена: шаблон против каталогов языков (миксин)."""

    domain = None

    @property
    def base(self):
        if self.domain == i18n.DOMAIN:
            return os.path.join(ROOT, "po")
        return os.path.join(ROOT, "po", self.domain)

    def pot(self):
        return entries_only(read_po(os.path.join(self.base, self.domain + ".pot")))

    def po(self, lang):
        return entries_only(read_po(os.path.join(self.base, lang + ".po")))

    def test_every_string_is_translated(self):
        expected = set(self.pot())
        for lang in LANGUAGES:
            table = self.po(lang)
            missing = sorted(expected - set(table))
            self.assertEqual(missing, [], "%s: нет перевода для %s" % (lang, missing))
            empty = sorted(key for key in expected if not table.get(key))
            self.assertEqual(empty, [], "%s: пустой перевод у %s" % (lang, empty))

    def test_catalog_has_no_extra_strings(self):
        expected = set(self.pot())
        for lang in LANGUAGES:
            extra = sorted(set(self.po(lang)) - expected)
            self.assertEqual(extra, [], "%s: перевода нет в коде: %s" % (lang, extra))

    def test_languages_differ(self):
        # Не тот же язык под другим именем: хоть одна строка должна отличаться.
        ru, zh = self.po("ru"), self.po("zh_CN")
        self.assertNotEqual(ru, zh, "ru и zh_CN совпали — перевод не сделан")


class TestMainCatalog(CatalogChecks, unittest.TestCase):
    domain = i18n.DOMAIN


class TestAppletCatalog(CatalogChecks, unittest.TestCase):
    domain = i18n.APPLET_DOMAIN


class TestInterfaceLanguage(unittest.TestCase):
    """Каталог не просто лежит на диске: под ru интерфейс русский."""

    def test_english_has_no_cyrillic(self):
        # Английский — язык исходника: кириллицы в выводе быть не должно.
        for argv in ENGLISH_COMMANDS:
            out = run(argv, "en")
            left = sorted(set(CYRILLIC.findall(out)))
            self.assertEqual(left, [], "%s: под en осталась кириллица %s" % (" ".join(argv), left))

    def test_russian_has_cyrillic(self):
        for argv in ENGLISH_COMMANDS:
            out = run(argv, "ru")
            self.assertTrue(CYRILLIC.search(out), "%s: под ru нет русских строк" % " ".join(argv))

    def test_languages_differ_for_help(self):
        argv = ["zone-tiler.py", "--help"]
        self.assertNotEqual(run(argv, "en"), run(argv, "ru"))


if __name__ == "__main__":
    unittest.main()

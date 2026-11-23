#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The completeness of the translation catalogs — a test holds it, not hope.

Three promises of the localization wave:

* every `msgid` from the template (`.pot`) has a non-empty `msgstr` in `en.po` and
  `zh_CN.po` — and conversely, the catalog has no strings that are not in the code (otherwise
  the translation quietly rots);
* the same for the separate applet domain `gridsnap@metapeka`;
* under `GRIDSNAP_LANG=en` the CLI really prints in English, and under
  `GRIDSNAP_LANG=ru` — in Russian: the catalog does not just lie on disk, it works.

The template and the catalogs are read by our own `zone_i18n.parse_po` parser, so
the tests need neither `xgettext` nor `msgfmt` (in CI only the catalogs job installs them).
The template is cross-checked against the code by `make check-i18n`.

Running:
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

# The languages that must have a complete catalog. `en` is not here: English is the
# source language, it has no catalog and must not have one.
LANGUAGES = ("ru", "zh_CN")

CYRILLIC = re.compile(r"[\u0400-\u04ff]")

# The commands that must speak the chosen language and run without X:
# the tiler pulls `gi` in only inside `monitors()`, the socket client and the
# keybinding installer need no GTK at all.
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
    """The catalog entries without the service header (`msgid ""`)."""
    return {key: value for key, value in table.items() if key}


def run(argv, lang):
    """Run a command with the given interface language and return the output text."""
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
    """The template is built from the code: it is the list of what has to be translated."""

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
    """Cross-check of one domain: the template against the language catalogs (a mixin)."""

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
        # Not the same language under another name: at least one string must differ.
        ru, zh = self.po("ru"), self.po("zh_CN")
        self.assertNotEqual(ru, zh, "ru и zh_CN совпали — перевод не сделан")


class TestMainCatalog(CatalogChecks, unittest.TestCase):
    domain = i18n.DOMAIN


class TestAppletCatalog(CatalogChecks, unittest.TestCase):
    domain = i18n.APPLET_DOMAIN


class TestInterfaceLanguage(unittest.TestCase):
    """The catalog does not just lie on disk: under ru the interface is Russian."""

    def test_english_has_no_cyrillic(self):
        # English is the source language: there must be no Cyrillic in the output.
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the three language versions of the README: the structure and the links do not drift apart.

The README exists in English (`README.md` — the main page of the repository),
simplified Chinese (`README.zh.md`) and Russian (`README.ru.md`). The translations
are made separately from each other, so what diverges first is checked:

* the file `README.en.md` is removed: `README.md` took over its role, and a second English
  version next to it would fall behind again (as happened with the former `README.en.md`);
* the section keys (`<!-- section: <slug> -->`) are the same in all three and go in
  one order — they show that a translation neither lost nor added a section;
* every version has a language switcher with links to the two others;
* the images that the READMEs link to exist.

Running: `python3 -m unittest discover -s tests` or `python3 tests/test_docs_languages.py`.
"""

import os
import re
import unittest


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # a run of the form `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The main one is English: the language order is en → zh → ru.
SOURCES = ("README.md", "README.zh.md", "README.ru.md")
SECTION_RE = re.compile(r"<!--\s*section:\s*([a-z0-9-]+)\s*-->")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class ReadmeLanguagesTest(unittest.TestCase):
    def test_three_files_and_no_legacy_english(self):
        for name in SOURCES:
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), f"нет {name}")
        self.assertFalse(
            os.path.exists(os.path.join(ROOT, "README.en.md")),
            "README.en.md остался: английская версия должна быть одна (README.md)",
        )

    def test_section_keys_are_the_same_everywhere(self):
        keys = {name: SECTION_RE.findall(read(name)) for name in SOURCES}
        main = keys[SOURCES[0]]
        self.assertGreater(len(main), 20, "в README почти нет ключей разделов")
        for name, found in keys.items():
            self.assertEqual(found, main, f"разделы {name} разошлись с README.md")
            self.assertEqual(
                len(set(found)), len(found), f"в {name} ключ раздела повторяется"
            )

    def test_language_switcher_links_the_other_two(self):
        for name in SOURCES:
            text = read(name)
            for other in SOURCES:
                if other == name:
                    continue
                self.assertIn(
                    f"]({other})", text, f"в {name} нет ссылки на {other}"
                )

    def test_images_referenced_by_readme_exist(self):
        for name in SOURCES:
            for target in IMAGE_RE.findall(read(name)):
                if target.startswith(("http://", "https://")):
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(ROOT, target)),
                    f"{name} ссылается на несуществующий файл {target}",
                )


if __name__ == "__main__":
    unittest.main()

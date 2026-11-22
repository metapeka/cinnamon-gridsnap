#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты трёх языковых версий README: структура и ссылки не расходятся.

README есть на английском (`README.md` — главная страница репозитория),
упрощённом китайском (`README.zh.md`) и русском (`README.ru.md`). Переводы
делаются отдельно друг от друга, поэтому проверяется то, что расходится первым:

* файл `README.en.md` удалён: его роль взял `README.md`, а вторая английская
  версия рядом снова бы отстала (так и случилось с прежним `README.en.md`);
* ключи разделов (`<!-- section: <слаг> -->`) у всех трёх совпадают и идут в
  одном порядке — по ним видно, что перевод не потерял и не добавил раздел;
* в каждой версии есть переключатель языков со ссылками на две другие;
* картинки, на которые ссылаются README, существуют.

Запуск: `python3 -m unittest discover -s tests` или `python3 tests/test_docs_languages.py`.
"""

import os
import re
import unittest

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Главная — английская: порядок языков en → zh → ru.
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

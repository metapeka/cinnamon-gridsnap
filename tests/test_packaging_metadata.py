#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Метаданные пакетов: одно описание на всех путях сборки и на английском.

Описание пакета — это то, что пользователь видит в `apt show gridsnap` и
`pacman -Si gridsnap`, а не служебный комментарий. Поэтому:

* `debian/control` и цель `make deb` (она пишет своё `DEBIAN/control`) обязаны
  говорить **одно и то же** — раньше текст был продублирован, и после перевода
  `debian/control` на английский русская копия в `Makefile` осталась (нашлось
  проверкой при сборке `.deb`, а не глазами);
* `pkgdesc` в `PKGBUILD` обязан совпадать с коротким описанием Debian: это одна
  и та же строка про продукт;
* в описаниях не должно быть кириллицы: Debian Policy §3.4 требует английского,
  а ради архива Debian эта волна и делалась;
* короткое описание — строчными и без точки на конце, как принято в Debian.

Запуск:
    python3 tests/test_packaging_metadata.py
    python3 -m unittest discover -s tests
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CONTROL = os.path.join(ROOT, "debian", "control")
MAKEFILE = os.path.join(ROOT, "Makefile")
PKGBUILD = os.path.join(ROOT, "PKGBUILD")

CYRILLIC = re.compile(r"[\u0400-\u04ff]")
ECHO_RE = re.compile(r"^\s*@echo '(?P<text>.*?)'\s*>>\s*\"?\$\(DEBROOT\)/DEBIAN/control\"?\s*$")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def debian_description():
    """Короткое и длинное описание из бинарной секции ``debian/control``."""
    lines = read(CONTROL).splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("Description:"))
    synopsis = lines[start][len("Description:"):].strip()
    body = []
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        body.append(line[1:].rstrip())
    return synopsis, body


def makefile_description():
    """Описание, которое цель `make deb` пишет в DEBIAN/control."""
    synopsis = None
    body = []
    for line in read(MAKEFILE).splitlines():
        match = ECHO_RE.match(line)
        if not match:
            continue
        text = match.group("text")
        if synopsis is None:
            if text.startswith("Description:"):
                synopsis = text[len("Description:"):].strip()
            continue
        if text.startswith(" "):
            body.append(text[1:].rstrip())
            continue
        break
    return synopsis, body


def pkgbuild_pkgdesc():
    for line in read(PKGBUILD).splitlines():
        if line.startswith("pkgdesc="):
            return line[len("pkgdesc="):].strip().strip('"')
    return None


class PackageDescriptionTest(unittest.TestCase):
    def setUp(self):
        self.synopsis, self.body = debian_description()

    def test_debian_description_is_not_empty(self):
        self.assertTrue(self.synopsis, "в debian/control нет короткого описания")
        self.assertGreaterEqual(len(self.body), 3, "длинное описание слишком короткое")

    def test_make_deb_writes_the_same_description(self):
        """Текст в Makefile — дословно тот же (иначе одно из двух устаревает)."""
        synopsis, body = makefile_description()
        self.assertIsNotNone(synopsis, "в Makefile нет строки Description для DEBIAN/control")
        self.assertEqual(synopsis, self.synopsis)
        self.assertEqual(body, self.body)

    def test_pkgbuild_synopsis_matches_debian(self):
        desc = pkgbuild_pkgdesc()
        self.assertIsNotNone(desc, "в PKGBUILD нет pkgdesc")
        self.assertEqual(desc, self.synopsis)

    def test_descriptions_are_english(self):
        for where, text in (("debian/control", "\n".join([self.synopsis] + self.body)),
                            ("PKGBUILD", pkgbuild_pkgdesc() or "")):
            self.assertFalse(CYRILLIC.search(text),
                             f"{where}: описание пакета должно быть на английском")

    def test_synopsis_follows_debian_style(self):
        self.assertFalse(self.synopsis.endswith("."),
                         "у короткого описания не должно быть точки на конце")
        self.assertEqual(self.synopsis[0], self.synopsis[0].lower(),
                         "короткое описание начинается со строчной буквы")


if __name__ == "__main__":
    unittest.main()

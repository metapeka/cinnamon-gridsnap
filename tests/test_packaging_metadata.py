#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Package metadata: one description on every build path, and in English.

The package description is what the user sees in `apt show gridsnap` and
`pacman -Si gridsnap`, not a service comment. Therefore:

* `debian/control` and the `make deb` target (it writes its own `DEBIAN/control`) must
  say **the same thing** — the text used to be duplicated, and after translating
  `debian/control` to English the Russian copy in the `Makefile` stayed behind (it was found
  by a check while building the `.deb`, not by eye);
* `pkgdesc` in `PKGBUILD` must match the short Debian description: it is one
  and the same line about the product;
* the descriptions must not contain Cyrillic: Debian Policy §3.4 requires English,
  and that wave was done for the sake of the Debian archive;
* the short description is lowercase and without a full stop at the end, as is usual in Debian.

Running:
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
    """The short and long description from the binary section of ``debian/control``."""
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
    """The description that the `make deb` target writes into DEBIAN/control."""
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
        """The text in the Makefile — literally the same (otherwise one of the two goes stale)."""
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

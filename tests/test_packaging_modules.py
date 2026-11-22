#!/usr/bin/env python3
"""Списки модулей в упаковке не должны отставать от кода.

Модуль, добавленный в корне (`zone_units.py`), обязан появиться **во всех** путях
установки: `Makefile` (`make tarball`/`make deb`), `install.sh` (`--prefix`),
`debian/install` (штатный пакет) и `PKGBUILD` (AUR). Пропущенный модуль не видно ни
юнит-тестам, ни дымовым сценариям: код работает из чекаута, где файл лежит рядом, а
падает только установленный пакет — `import zone_units` из `zone-tiler.py` даёт
`ModuleNotFoundError`. Именно это поймал CI на волне DPI, и стоило это прогона CI,
потому что локально такой проверки не было.

Тест читает сами списки (текстом), а не повторяет их: разойтись с упаковкой он не
может, а новый модуль в корне заставит дописать его везде.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Где перечислены модули поставки. Значение — как вытащить имена из файла.
SOURCES = {
    "Makefile": "MODULES",
    "install.sh": "MODULES",
    "debian/install": "debian",
    "PKGBUILD": "pkgbuild",
}

MODULE_RE = re.compile(r"[A-Za-z0-9_-]+\.py")


def root_modules():
    """Модули в корне репозитория — то, что должно быть в поставке."""
    return {
        name
        for name in os.listdir(ROOT)
        if name.endswith(".py") and os.path.isfile(os.path.join(ROOT, name))
    }


def listed_modules(path):
    """Имена модулей, перечисленные в файле упаковки."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith("Makefile"):
        # Берём только строку(и) переменной MODULES до следующего заголовка файла.
        start = text.index("MODULES :=")
        end = text.index("\n\n", start)
        text = text[start:end]
    elif path.endswith("install.sh"):
        start = text.index("MODULES=")
        end = text.index("\n\n", start)
        text = text[start:end]
    return set(MODULE_RE.findall(text))


class TestPackagingLists(unittest.TestCase):
    def test_every_root_module_is_listed_everywhere(self):
        modules = root_modules()
        self.assertIn("zone_units.py", modules)  # сам тест не должен ослепнуть
        for name in SOURCES:
            listed = listed_modules(os.path.join(ROOT, name))
            missing = sorted(modules - listed)
            self.assertEqual(
                missing,
                [],
                f"{name}: в поставке нет модулей {missing} — установленный пакет "
                "упадёт с ModuleNotFoundError",
            )

    def test_lists_do_not_invent_modules(self):
        modules = root_modules()
        for name in SOURCES:
            listed = listed_modules(os.path.join(ROOT, name))
            extra = sorted(listed - modules)
            self.assertEqual(
                extra, [], f"{name}: перечислены несуществующие модули {extra}"
            )


if __name__ == "__main__":
    unittest.main()

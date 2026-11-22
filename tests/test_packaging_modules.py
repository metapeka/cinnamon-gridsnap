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

Тем же тестом стережётся и каталог `gallery/`: готовые раскладки — это данные, а
не модули, но цена забытой строки та же — в установленном пакете меню «Из
галереи…» пустое (тесты из чекаута этого не видят: в чекауте каталог лежит рядом).
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

GALLERY_DIR = os.path.join(ROOT, "gallery")

# Готовые раскладки ищутся в `<префикс>/share/gridsnap/gallery` (см.
# `zone_config.gallery_dir`), а в tar.gz едет каталог `gallery/` целиком. Что
# именно проверить в каждом файле: путь установки и то, что копируются сами
# `*.conf`, а не пустой каталог (добавишь раскладку — новые файлы доедут без
# правки списков, но только если каталог вообще есть в поставке).
GALLERY_MARKERS = {
    "install.sh": ("share/gridsnap/gallery", "gallery/*.conf"),
    "debian/install": ("gallery usr/share/gridsnap",),
    "PKGBUILD": ("usr/share/gridsnap/gallery", "gallery/*.conf"),
}


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

class TestGalleryPackaging(unittest.TestCase):
    """Каталог `gallery/` доезжает до пользователя всеми четырьмя путями.

    Раскладки — данные, а не модули: забыть их в списке так же легко, а цена
    другая — не `ModuleNotFoundError`, а пустое меню «Из галереи…» в
    установленном пакете. Из чекаута такой провал не виден: там каталог лежит
    рядом с кодом, и тест `test_gallery` его находит.
    """

    def test_gallery_has_layouts(self):
        layouts = sorted(
            name for name in os.listdir(GALLERY_DIR) if name.endswith(".conf")
        )
        self.assertTrue(
            layouts,
            "нет gallery/*.conf — поставке нечего ставить, а тест проверяет пустое",
        )

    def test_tarball_ships_gallery_directory(self):
        with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("RELEASE_DIRS :=")
        end = text.index("\n\n", start)
        self.assertIn(
            "gallery",
            text[start:end].split(),
            "Makefile: gallery/ не попал в RELEASE_DIRS — в tar.gz раскладок не будет",
        )
        self.assertIn(
            "usr/share/gridsnap/gallery",
            text,
            "Makefile: `make deb` не ставит раскладки в /usr/share/gridsnap/gallery",
        )
        self.assertIn(
            "gallery/*.conf",
            text,
            "Makefile: `make deb` не копирует сами *.conf — каталог уедет пустым",
        )

    def test_every_install_path_mentions_gallery(self):
        for name, markers in GALLERY_MARKERS.items():
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
            for marker in markers:
                self.assertIn(
                    marker,
                    text,
                    f"{name}: нет «{marker}» — раскладки не доедут до пользователя "
                    "этим путём (см. zone_config.gallery_dir)",
                )

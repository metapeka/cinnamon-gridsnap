#!/usr/bin/env python3
"""The module lists in the packaging must not fall behind the code.

A module added at the root (`zone_units.py`) must appear **in every** installation
path: `Makefile` (`make tarball`/`make deb`), `install.sh` (`--prefix`),
`debian/install` (the distribution package) and `PKGBUILD` (AUR). A missing module is
visible neither to the unit tests nor to the smoke scenarios: the code runs from the
checkout, where the file lies next to it, and only the installed package fails —
`import zone_units` from `zone-tiler.py` raises `ModuleNotFoundError`. Exactly what CI
caught in the DPI wave, and it cost a CI run, because there was no such check locally.

The test reads the lists themselves (as text) instead of repeating them: it cannot
drift away from the packaging, and a new module at the root forces adding it everywhere.

The same test also guards the `gallery/` directory: ready-made layouts are data, not modules,
but the price of a forgotten line is the same — in the installed package the "From the
gallery…" menu is empty (the checkout tests miss it: the directory lies next to the code).
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Where the distribution modules are listed. The value says how to pull the names out.
SOURCES = {
    "Makefile": "MODULES",
    "install.sh": "MODULES",
    "debian/install": "debian",
    "PKGBUILD": "pkgbuild",
}

MODULE_RE = re.compile(r"[A-Za-z0-9_-]+\.py")

GALLERY_DIR = os.path.join(ROOT, "gallery")

# The ready-made layouts are looked up in `<prefix>/share/gridsnap/gallery` (see
# `zone_config.gallery_dir`), and the tarball ships the whole `gallery/` directory. What
# exactly to check in each file: the install path, and that the files are the
# `*.conf` ones themselves, not an empty directory (add a layout and the new files
# arrive without touching the lists, but only if the directory is in the distribution at all).
GALLERY_MARKERS = {
    "install.sh": ("share/gridsnap/gallery", "gallery/*.conf"),
    "debian/install": ("gallery usr/share/gridsnap",),
    "PKGBUILD": ("usr/share/gridsnap/gallery", "gallery/*.conf"),
}


def root_modules():
    """Modules at the root of the repository — what must be in the distribution."""
    return {
        name
        for name in os.listdir(ROOT)
        if name.endswith(".py") and os.path.isfile(os.path.join(ROOT, name))
    }


def listed_modules(path):
    """The module names listed in a packaging file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith("Makefile"):
        # Take only the line(s) of the MODULES variable up to the next heading of the file.
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
        self.assertIn("zone_units.py", modules)  # the test itself must not go blind
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
    """The `gallery/` directory reaches the user by all four paths.

    Layouts are data, not modules: forgetting them in the list is just as easy, but the price
    is different — not `ModuleNotFoundError` but an empty "From the gallery…" menu in the
    installed package. From the checkout such a failure is invisible: there the directory lies
    next to the code, and the `test_gallery` test finds it.
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

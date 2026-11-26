#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the still screenshots the READMEs and the site show.

The product's face on GitHub and on the Pages site is two images in `site/assets/`: the editor
with a zone set and the picker overlay. The docs wave that added them to the three READMEs and
the three site pages left them hand-made, and a hand-made image drifts silently: the palette
changes, the surface moves, and the picture keeps showing last month's product.

So what is checked is the pair of files and the script that produces them:

* `site/assets/editor.png` and `site/assets/picker.png` exist, start with the PNG signature and are
  decoded to their real size in the header — a screenshot, not a placeholder of the right name;
* the editor is at least 1000×600 and the picker exactly the 1280×800 work area it renders: a
  picture shrunk below that stops being readable in a README;
* both are referenced by all three READMEs and all three site pages: an image nothing shows is a
  file that rots unnoticed (the link checks in `test_docs_languages`/`test_site_pages` prove the
  references resolve, this proves the images are the ones those references carry);
* `tools/shoot-screens.sh` is executable and names both files: re-rendering is one command, not
  "there was a script somewhere", the same contract `tests/test_demo_gif.py` holds for the GIF.

The test needs no X, GTK or ImageMagick — only the bytes of the files, so it runs everywhere.

Running: `python3 -m unittest tests.test_screenshots -v` or
`python3 -m unittest discover -s tests`.
"""

import os
import re
import struct
import unittest


try:
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "site", "assets")
SCRIPT = os.path.join(ROOT, "tools", "shoot-screens.sh")

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The editor's offscreen window is 1140×720 (`zone-editor.py --shot`); the picker draws the whole
# work area of the pinned Xvfb screen. Both limits sit at "readable in a README", not at the
# current pixel values, so a deliberate size change is not a failure but a crop is.
MIN_WIDTH = 1000
MIN_HEIGHT = 600
PICKER_SIZE = (1280, 800)

IMAGES = ("editor.png", "picker.png")
SOURCES = ("README.md", "README.zh.md", "README.ru.md")
PAGES = ("index.html", "index.zh.html", "index.ru.html")


def png_size(path):
    """`(width, height)` from the IHDR chunk; raises for a file that is not a PNG."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    if not header.startswith(SIGNATURE):
        raise ValueError(f"{path} does not start with the PNG signature")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class ScreenshotsTest(unittest.TestCase):
    def test_files_are_real_pngs(self):
        for name in IMAGES:
            path = os.path.join(ASSETS, name)
            self.assertTrue(os.path.isfile(path), f"no site/assets/{name}")
            self.assertGreater(os.path.getsize(path), 1000, f"site/assets/{name} is empty")
            try:
                width, height = png_size(path)
            except ValueError as exc:
                self.fail(str(exc))
            self.assertGreater(width, 0, f"site/assets/{name} has a zero width")
            self.assertGreater(height, 0, f"site/assets/{name} has a zero height")

    def test_editor_is_large_enough_to_read(self):
        width, height = png_size(os.path.join(ASSETS, "editor.png"))
        self.assertGreaterEqual(width, MIN_WIDTH, f"editor.png is {width} px wide")
        self.assertGreaterEqual(height, MIN_HEIGHT, f"editor.png is {height} px tall")

    def test_picker_covers_the_work_area_it_renders(self):
        # The picker draws the monitor's work area, and the script pins that screen at 1280×800:
        # a different size means the image came from another display, not from the script.
        self.assertEqual(
            png_size(os.path.join(ASSETS, "picker.png")),
            PICKER_SIZE,
            "picker.png is not the 1280×800 work area tools/shoot-screens.sh renders",
        )

    def test_every_readme_shows_both_images(self):
        for name in SOURCES:
            text = read(name)
            for image in IMAGES:
                self.assertIn(
                    f"site/assets/{image}", text, f"{name} does not show site/assets/{image}"
                )

    def test_every_page_shows_both_images(self):
        for name in PAGES:
            text = read(os.path.join("site", name))
            for image in IMAGES:
                self.assertIn(
                    f"assets/{image}", text, f"site/{name} does not show assets/{image}"
                )

    def test_script_renders_both_images(self):
        self.assertTrue(os.path.isfile(SCRIPT), f"no script: {SCRIPT}")
        self.assertTrue(
            os.access(SCRIPT, os.X_OK), "tools/shoot-screens.sh is not executable: chmod +x it"
        )
        with open(SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("site/assets", text, "tools/shoot-screens.sh does not name site/assets")
        for image in IMAGES:
            self.assertTrue(
                re.search(rf"\b{re.escape(image)}\b", text),
                f"tools/shoot-screens.sh never mentions {image}",
            )

    def test_script_keeps_the_palette_distinct_from_the_default(self):
        # The pictures exist to show where the colours come from: a scene rendered in the default
        # palette would be a regression nobody notices, because the files would simply differ.
        with open(SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(
            "scene-palette.py", text,
            "the shots no longer build their palette with tools/scene-palette.py",
        )
        self.assertIn("GRIDSNAP_LANG=en", text, "the shots are not pinned to the scene language")


if __name__ == "__main__":
    unittest.main()

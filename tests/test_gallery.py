#!/usr/bin/env python3
"""A gallery of ready-made layouts: the contract (a failing test before the implementation).

The "layout gallery" wave went to a worker, and this file is the interface fixed in
advance: the pure functions live in `zone_config.py`, the data lives in the `gallery/`
directory next to the code (and is installed by the package). The test deliberately
**fails** before the implementation and checks exactly what was agreed with the owner:

* a layout from the gallery is a set of zones **plus** keys **plus** rules for
  applications (a ready-made "scenario", not just geometry);
* picking one copies the layout into the **user profile** — from then on it is edited as one's own;
* the gallery is visible both in the editor and in the CLI (the CLI part is checked as the functions of
  `zone_config`, because the editor and the command line call the same ones);
* the gallery files are ordinary `.conf` in the profile format, with a `[gallery]` section
  (a human title and a description — both the menu and `profile gallery` show them).

The bodies are not hinted at: what matters is that the worker picks a reasonable
implementation itself, while the test keeps the promises of the interface.
"""

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402

GALLERY = os.path.join(ROOT, "gallery")

# How many layouts the first release must have: fewer — a "gallery" of one
# entry, more — a mess in the menu. The owner asked for ready-made scenarios, not a catalogue.
MIN_LAYOUTS = 6


class TestGalleryData(unittest.TestCase):
    def test_gallery_directory_is_shipped_with_code(self):
        self.assertTrue(
            os.path.isdir(GALLERY),
            "нет каталога gallery/ рядом с кодом — раскладкам неоткуда взяться",
        )

    def test_every_layout_is_a_profile_with_title_and_description(self):
        files = sorted(f for f in os.listdir(GALLERY) if f.endswith(".conf"))
        self.assertGreaterEqual(
            len(files), MIN_LAYOUTS, f"в галерее {len(files)} раскладок, мало"
        )
        for name in files:
            with open(os.path.join(GALLERY, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("[gallery]", text, f"{name}: нет секции [gallery]")
            self.assertIn("[zones]", text, f"{name}: нет зон")
            self.assertIn("title", text, f"{name}: нет человеческого названия")
            self.assertIn("description", text, f"{name}: нет описания")

    def test_layouts_bring_keys_and_rules_not_only_geometry(self):
        # The owner chose the "sets + keys + rules" option: at least one
        # layout must bring rules for applications.
        texts = []
        for name in sorted(os.listdir(GALLERY)):
            if name.endswith(".conf"):
                with open(os.path.join(GALLERY, name), encoding="utf-8") as fh:
                    texts.append((name, fh.read()))
        self.assertTrue(any("[rules]" in t for _n, t in texts), "ни одной [rules]")
        self.assertTrue(any("[keys]" in t for _n, t in texts), "ни одной [keys]")


class TestGalleryApi(unittest.TestCase):
    """The gallery interface in `zone_config` — used by both the CLI and the editor."""

    def test_gallery_layouts_lists_all_with_titles(self):
        layouts = zc.gallery_layouts()
        self.assertGreaterEqual(len(layouts), MIN_LAYOUTS)
        for item in layouts:
            self.assertTrue(item["id"], "у раскладки нет id")
            self.assertTrue(item["title"].strip(), f"{item['id']}: пустое название")
            self.assertTrue(
                item["description"].strip(), f"{item['id']}: пустое описание"
            )
            self.assertTrue(item["zones"], f"{item['id']}: нет зон")

    def test_load_gallery_layout_gives_zones_keys_and_rules(self):
        gid = zc.gallery_layouts()[0]["id"]
        layout = zc.load_gallery_layout(gid)
        self.assertTrue(layout.get("zones"), f"{gid}: нечего раскладывать")
        # keys and rules may be empty for a particular layout, but the keys in the
        # dictionary must be there — the editor and the CLI use them to decide what to carry over.
        self.assertIn("keys", layout)
        self.assertIn("rules", layout)

    def test_load_unknown_layout_is_a_clear_error(self):
        with self.assertRaises(zc.GalleryError):
            zc.load_gallery_layout("нет-такой-раскладки")

    def test_install_copies_into_user_profile(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write("[zones]\nmain = 0, 0, 1, 1\n")
            gid = zc.gallery_layouts()[0]["id"]
            path = zc.install_gallery_layout(gid, conf_path=conf)
            self.assertTrue(os.path.exists(path), "профиль не создан")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("[zones]", text, "в профиль не перенеслись зоны")
            # Picking the same name again without force is an error, not a silent
            # overwrite of somebody else's profile.
            with self.assertRaises(zc.GalleryError):
                zc.install_gallery_layout(gid, conf_path=conf)

    def test_install_can_write_under_a_chosen_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write("[zones]\nmain = 0, 0, 1, 1\n")
            gid = zc.gallery_layouts()[0]["id"]
            path = zc.install_gallery_layout(gid, name="моя-раскладка", conf_path=conf)
            self.assertEqual(os.path.basename(path), "моя-раскладка.conf")
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()

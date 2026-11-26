#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for `zone_theme` — the palette behind every GridSnap surface.

Three things are checked:

* the defaults are exactly the colours the surfaces drew before the theme existed, so an
  installation without `theme.conf` does not change its look (the tab strip and the editor
  take their shades from the same tokens as the picker, and those shades must match the ones
  that were hard-coded in the three modules);
* a `theme.conf` next to `zones.conf` overrides what it names and leaves the rest alone —
  a preset first, then the `[colors]` section, then `zones`/`radius`/`font`;
* a broken theme degrades instead of failing: a malformed colour, an unknown preset, a bad
  radius and a file that is not INI at all keep the base and print one warning. The tools that
  draw have to survive a typo in a colour.

Everything here is pure: no X, no GTK, no display.

Running:
    python3 tests/test_theme.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # a run of the form `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_theme  # noqa: E402


@contextlib.contextmanager
def warnings():
    """Collect what the module wrote to stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


class TestDefaults(unittest.TestCase):
    def test_default_palette_is_the_old_look(self):
        # The promise of "no theme.conf - nothing changes": these are the exact colours the
        # picker backdrop, the accent and the first zone palette entry had as floats.
        theme = zone_theme.default_theme()
        self.assertEqual(theme.rgb("bg"), (0x12 / 255, 0x14 / 255, 0x1A / 255))
        self.assertEqual(theme.rgb("accent"), (0xFF / 255, 0xB5 / 255, 0x54 / 255))
        self.assertEqual(theme.zone_rgb(0), (0x5C / 255, 0x9E / 255, 0xEB / 255))
        self.assertEqual(theme.font_desc(11, bold=True), "Sans Bold 11")
        self.assertEqual(theme.radius, 10)

    def test_default_zones_are_eight_distinct_colours(self):
        theme = zone_theme.default_theme()
        self.assertEqual(len(theme.zones), 8)
        self.assertEqual(len(set(theme.zones)), 8)

    def test_zone_index_wraps(self):
        theme = zone_theme.default_theme()
        self.assertEqual(theme.zone_rgb(8), theme.zone_rgb(0))
        self.assertEqual(theme.zone_rgba(8, 0.5), (*theme.zone_rgb(0), 0.5))

    def test_hex_and_rgb_are_inverse(self):
        for value in ("#000000", "#ffffff", "#5c9eeb", "#8e8e93"):
            self.assertEqual(zone_theme.hex_of(zone_theme.rgb(value)), value)

    def test_contrast_text_suits_the_accent(self):
        self.assertEqual(zone_theme.contrast_text("#ffffff"), "#000000")
        self.assertEqual(zone_theme.contrast_text("#000000"), "#ffffff")


class TestDerivation(unittest.TestCase):
    def test_derives_eight_distinct_colours(self):
        zones = zone_theme.derive_zones("#7aa2f7")
        self.assertEqual(len(zones), 8)
        self.assertEqual(len(set(zones)), 8)
        for value in zones:
            self.assertRegex(value, r"^#[0-9a-f]{6}$")

    def test_monochrome_accent_does_not_collapse_into_greys(self):
        # The 43PR presets carry accent = #ffffff; eight identical greys would make a grid unreadable.
        zones = zone_theme.derive_zones("#ffffff")
        self.assertEqual(len(set(zones)), 8)

    def test_derivation_is_deterministic(self):
        self.assertEqual(zone_theme.derive_zones("#7aa2f7"), zone_theme.derive_zones("#7aa2f7"))


class TestLoading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conf = os.path.join(self.dir, "zones.conf")
        self.path = os.path.join(self.dir, "theme.conf")
        with open(self.conf, "w", encoding="utf-8"):
            pass

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_theme_sits_next_to_the_config(self):
        self.assertEqual(zone_theme.theme_path(self.conf), self.path)

    def test_no_config_path_means_no_theme(self):
        self.assertIsNone(zone_theme.theme_path(None))
        self.assertEqual(zone_theme.load(None).name, "default")

    def test_missing_file_keeps_the_defaults(self):
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.colors, zone_theme.DEFAULT_COLORS)
        self.assertIsNone(theme.path)

    def test_colors_section_overrides_only_what_it_names(self):
        self.write("[colors]\nbg = #000000\naccent = #7aa2f7\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.colors["bg"], "#000000")
        self.assertEqual(theme.colors["accent"], "#7aa2f7")
        self.assertEqual(theme.colors["fg"], zone_theme.DEFAULT_COLORS["fg"])
        self.assertEqual(theme.path, self.path)

    def test_a_preset_is_a_base_and_the_section_wins(self):
        self.write("[theme]\npreset = nord\n[colors]\naccent = #ff0000\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.colors["bg"], zone_theme.PRESETS["nord"]["bg"])
        self.assertEqual(theme.colors["accent"], "#ff0000")

    def test_unknown_preset_warns_and_keeps_the_default(self):
        self.write("[theme]\npreset = no-such-look\n")
        with warnings() as stderr:
            theme = zone_theme.load(self.conf)
        self.assertIn("unknown preset", stderr.getvalue())
        self.assertEqual(theme.colors["bg"], zone_theme.DEFAULT_COLORS["bg"])

    def test_malformed_colour_warns_and_keeps_the_base(self):
        self.write("[theme]\npreset = nord\n[colors]\nbg = #000\n")
        with warnings() as stderr:
            theme = zone_theme.load(self.conf)
        self.assertIn("bg", stderr.getvalue())
        self.assertEqual(theme.colors["bg"], zone_theme.PRESETS["nord"]["bg"])

    def test_a_file_that_is_not_ini_still_gives_a_theme(self):
        self.write("[colors\nbg = #000000\n")
        with warnings() as stderr:
            theme = zone_theme.load(self.conf)
        self.assertNotEqual(stderr.getvalue(), "")
        self.assertEqual(theme.colors, zone_theme.DEFAULT_COLORS)

    def test_radius_and_font(self):
        self.write("[theme]\nradius = 4\nfont = JetBrains Mono\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.radius, 4)
        self.assertEqual(theme.font_desc(9), "JetBrains Mono 9")
        self.assertEqual(theme.font_desc(9, bold=True), "JetBrains Mono Bold 9")

    def test_out_of_range_radius_and_font_keep_the_defaults(self):
        self.write("[theme]\nradius = 999\nfont = Sans; Mono\n")
        with warnings() as stderr:
            theme = zone_theme.load(self.conf)
        self.assertEqual(theme.radius, zone_theme.DEFAULT_RADIUS)
        self.assertEqual(theme.font, zone_theme.DEFAULT_FONT)
        self.assertEqual(len(stderr.getvalue().splitlines()), 2)

    def test_zone_list_is_read_and_wrapped(self):
        self.write("[colors]\nzones = #112233, #445566\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.zones, ("#112233", "#445566"))
        self.assertEqual(theme.zone_rgb(2), zone_theme.rgb("#112233"))

    def test_zone_list_drops_malformed_entries(self):
        self.write("[colors]\nzones = #112233, red, #445566\n")
        with warnings() as stderr:
            theme = zone_theme.load(self.conf)
        self.assertIn("red", stderr.getvalue())
        self.assertEqual(theme.zones, ("#112233", "#445566"))

    def test_zones_are_derived_from_the_accent_without_a_list(self):
        self.write("[theme]\npreset = nord\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.zones, zone_theme.derive_zones(zone_theme.PRESETS["nord"]["accent"]))

    def test_name_defaults_to_the_preset(self):
        self.write("[theme]\npreset = graphite\n")
        self.assertEqual(zone_theme.load(self.conf).name, "graphite")
        self.write("[theme]\npreset = graphite\nname = mine\n")
        self.assertEqual(zone_theme.load(self.conf).name, "mine")

    def test_keys_in_the_default_section_are_accepted(self):
        # configparser has no way to put a key before the first section; [DEFAULT] is the nearest
        # thing, and a file that uses it must still be understood.
        self.write("[DEFAULT]\npreset = nord\n[colors]\naccent = #ff0000\n")
        theme = zone_theme.load(self.conf)
        self.assertEqual(theme.colors["bg"], zone_theme.PRESETS["nord"]["bg"])
        self.assertEqual(theme.colors["accent"], "#ff0000")


class TestGtkSheet(unittest.TestCase):
    def test_sheet_paints_the_editor_from_the_tokens(self):
        theme = zone_theme.default_theme()
        css = theme.gtk_css()
        self.assertIn(theme.colors["bg"], css)
        self.assertIn(theme.colors["bg_panel"], css)
        self.assertIn(theme.colors["accent"], css)
        self.assertIn(".dim-label", css)
        self.assertTrue(css.endswith("\n"))

    def test_text_on_the_accent_has_contrast(self):
        theme = zone_theme.Theme({**zone_theme.DEFAULT_COLORS, "accent": "#ffffff"})
        checked = [
            line
            for line in theme.gtk_css().splitlines()
            if line.startswith("button:checked")
        ]
        self.assertEqual(len(checked), 1)
        self.assertIn("background-color: #ffffff", checked[0])
        self.assertIn("color: #000000", checked[0])


if __name__ == "__main__":
    unittest.main()

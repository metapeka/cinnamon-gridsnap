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

The generated files are checked here too: the wallpaper resolver (a file, a `file://` URI, a GNOME
background descriptor), the palette sampled from an image made in the test, `theme.conf` written
and read back, and the GTK controls theme (the file GTK will actually load). The sampling needs
GdkPixbuf, so those tests skip where `gi` is absent; everything else is pure: no X, no display.

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
import urllib.parse

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


def make_wallpaper(path, blocks, width=64, height=64):
    """Write a PNG of vertical colour blocks: ``blocks`` is one ``(r, g, b)`` per column."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    stride = width * 3
    data = bytearray(stride * height)
    for row in range(height):
        for column in range(width):
            red, green, blue = blocks[column]
            index = row * stride + column * 3
            data[index : index + 3] = bytes((red, green, blue))
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        width,
        height,
        stride,
    )
    pixbuf.savev(path, "png", [], [])


def wallpaper_available():
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


class TestWallpaperImage(unittest.TestCase):
    """Resolving the setting into a file: a path, a `file://` URI, a GNOME descriptor, nothing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.image = os.path.join(self.dir, "paper.png")
        with open(self.image, "wb") as handle:
            handle.write(b"not really a png")  # the resolver only checks that it exists

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_plain_path_is_returned(self):
        self.assertEqual(zone_theme.wallpaper_image(self.image), self.image)

    def test_a_file_uri_is_decoded(self):
        uri = "file://" + urllib.parse.quote(self.image)
        self.assertEqual(zone_theme.wallpaper_image(uri), self.image)

    def test_a_gnome_descriptor_resolves_to_its_first_existing_file(self):
        descriptor = os.path.join(self.dir, "gnome-background.xml")
        with open(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                "<background><static><file>%s</file></static>"
                "<transition><file>/no/such/file.png</file></transition></background>"
                % self.image
            )
        self.assertEqual(zone_theme.wallpaper_image(descriptor), self.image)

    def test_a_descriptor_with_nested_sizes_resolves_its_first_image(self):
        # Debian's desktop-base descriptor: the path is the text of a `<size>` child, and the
        # `<file>` element carries only whitespace. This is what the live Cinnamon setting points at.
        descriptor = os.path.join(self.dir, "sizes.xml")
        with open(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                '<background><static><file><size width="1280" height="1024">%s</size>'
                '<size width="1920" height="1080">/no/such/file.svg</size>'
                "</file></static></background>" % self.image
            )
        self.assertEqual(zone_theme.wallpaper_image(descriptor), self.image)

    def test_a_missing_file_gives_nothing(self):
        self.assertIsNone(zone_theme.wallpaper_image(os.path.join(self.dir, "nope.png")))
        self.assertIsNone(zone_theme.wallpaper_image(""))

    def test_a_descriptor_without_existing_files_gives_nothing(self):
        descriptor = os.path.join(self.dir, "empty.xml")
        with open(descriptor, "w", encoding="utf-8") as handle:
            handle.write("<background><file>/no/such/file.png</file></background>")
        self.assertIsNone(zone_theme.wallpaper_image(descriptor))


class TestWallpaperPalette(unittest.TestCase):
    def setUp(self):
        if not wallpaper_available():
            self.skipTest("GdkPixbuf is not available")
        self.dir = tempfile.mkdtemp()
        self.image = os.path.join(self.dir, "sunset.png")
        make_wallpaper(
            self.image,
            [(12, 20, 48)] * 40 + [(230, 120, 30)] * 12 + [(30, 120, 80)] * 12,
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_samples_are_weighted_and_deterministic(self):
        first = zone_theme.samples_of(self.image)
        self.assertEqual(first, zone_theme.samples_of(self.image))
        self.assertEqual(len(first), 3)
        self.assertEqual(sum(weight for _colour, weight in first), 64 * 64)
        self.assertGreater(first[0][1], first[1][1])

    def test_the_palette_is_a_dark_chrome_with_a_vivid_accent(self):
        theme = zone_theme.from_wallpaper(self.image)
        for token in zone_theme.COLOR_TOKENS:
            self.assertRegex(theme.colors[token], r"^#[0-9a-f]{6}$", token)
        ladder = [
            zone_theme.luminance(theme.colors[token])
            for token in ("bg", "bg_panel", "bg_card", "bg_hover")
        ]
        self.assertEqual(ladder, sorted(ladder), ladder)
        self.assertLess(ladder[0], 0.05)
        self.assertGreater(zone_theme.luminance(theme.colors["fg"]), 0.75)
        red, _green, blue = zone_theme.rgb(theme.colors["accent"])
        self.assertGreater(red, blue)  # the orange block is the accent, not the blue field

    def test_the_name_comes_from_the_file(self):
        self.assertEqual(zone_theme.from_wallpaper(self.image).name, "wallpaper-sunset")

    def test_zones_are_derived_from_the_accent(self):
        theme = zone_theme.from_wallpaper(self.image)
        self.assertEqual(len(theme.zones), 8)
        self.assertEqual(len(set(theme.zones)), 8)
        self.assertEqual(theme.zones, zone_theme.derive_zones(theme.colors["accent"]))

    def test_a_monochrome_image_still_gives_a_readable_grid(self):
        grey = os.path.join(self.dir, "grey.png")
        make_wallpaper(grey, [(200, 200, 200)] * 64)
        theme = zone_theme.from_wallpaper(grey)
        self.assertEqual(len(set(theme.zones)), 8)

    def test_an_unreadable_file_raises_value_error(self):
        broken = os.path.join(self.dir, "broken.png")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("not a png")
        with self.assertRaises(ValueError):
            zone_theme.from_wallpaper(broken)


class TestSamplesToTheme(unittest.TestCase):
    """The pure part of the bridge: samples in, a consistent palette out."""

    def test_empty_samples_raise(self):
        with self.assertRaises(ValueError):
            zone_theme.theme_from_samples([])
        with self.assertRaises(ValueError):
            zone_theme.theme_from_samples([((0.1, 0.1, 0.1), 0)])

    def test_the_ramp_follows_the_dominant_colour_and_the_accent_the_vivid_one(self):
        theme = zone_theme.theme_from_samples(
            [((0.05, 0.08, 0.19), 1000), ((0.9, 0.47, 0.12), 100)]
        )
        red, _green, blue = zone_theme.rgb(theme.colors["bg"])
        self.assertGreater(blue, red)  # the blue field is the chrome
        red, _green, blue = zone_theme.rgb(theme.colors["accent"])
        self.assertGreater(red, blue)  # the orange cluster is the accent
        self.assertEqual(theme.name, "wallpaper")


class TestWriteTheme(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "theme.conf")
        self.theme = zone_theme.Theme(
            dict(zone_theme.DEFAULT_COLORS),
            zone_theme.DEFAULT_ZONES,
            radius=7,
            font="Mono",
            name="mine",
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_file_reads_back_as_the_same_theme(self):
        zone_theme.write_theme(self.theme, self.path, source="/tmp/paper.png")
        back = zone_theme.load(None, path=self.path)
        self.assertEqual(back.colors, self.theme.colors)
        self.assertEqual(back.zones, self.theme.zones)
        self.assertEqual((back.radius, back.font, back.name), (7, "Mono", "mine"))

    def test_the_file_is_marked_as_generated(self):
        zone_theme.write_theme(self.theme, self.path, source="/tmp/paper.png")
        with open(self.path, encoding="utf-8") as handle:
            first = handle.readline()
        self.assertTrue(first.startswith(zone_theme.GENERATED_MARK))
        self.assertIn("/tmp/paper.png", first)

    def test_no_temporary_file_is_left_behind(self):
        zone_theme.write_theme(self.theme, self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class TestGeneratedSource(unittest.TestCase):
    """The marker is the only claim on a file: a hand-written palette is never rewritten."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "theme.conf")
        self.theme = zone_theme.Theme(dict(zone_theme.DEFAULT_COLORS), zone_theme.DEFAULT_ZONES)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_generated_file_names_its_image(self):
        zone_theme.write_theme(self.theme, self.path, source="/tmp/paper.png")
        self.assertEqual(zone_theme.generated_source(self.path), "/tmp/paper.png")

    def test_a_marker_without_a_source_is_still_generated(self):
        zone_theme.write_theme(self.theme, self.path)
        self.assertEqual(zone_theme.generated_source(self.path), "")

    def test_a_hand_written_file_is_not_generated(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[colors]\naccent = #ff0000\n")
        self.assertIsNone(zone_theme.generated_source(self.path))

    def test_a_missing_file_is_not_generated(self):
        self.assertIsNone(zone_theme.generated_source(self.path))
        self.assertIsNone(zone_theme.generated_source(None))


class TestRefreshFromWallpaper(unittest.TestCase):
    """A generated palette follows the wallpaper; nothing else is ever touched."""

    def setUp(self):
        if not wallpaper_available():
            self.skipTest("GdkPixbuf is not available")
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "theme.conf")
        self.blue = os.path.join(self.dir, "blue.png")
        self.orange = os.path.join(self.dir, "orange.png")
        make_wallpaper(self.blue, [(12, 20, 96)] * 64)
        make_wallpaper(self.orange, [(230, 120, 30)] * 64)
        self.theme = zone_theme.Theme(dict(zone_theme.DEFAULT_COLORS), zone_theme.DEFAULT_ZONES)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def generated(self, source):
        return zone_theme.write_theme(self.theme, self.path, source=source)

    def test_a_new_wallpaper_rewrites_the_palette(self):
        self.generated(self.blue)
        written = zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=self.orange)
        self.assertEqual(written, self.path)
        self.assertEqual(zone_theme.generated_source(self.path), self.orange)
        self.assertEqual(
            zone_theme.load(None, path=self.path).colors,
            zone_theme.from_wallpaper(self.orange).colors,
        )

    def test_the_same_wallpaper_keeps_the_file(self):
        self.generated(self.blue)
        before = os.stat(self.path).st_mtime_ns
        self.assertIsNone(zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=self.blue))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_a_file_uri_counts_as_the_same_wallpaper(self):
        self.generated(self.blue)
        uri = "file://" + urllib.parse.quote(self.blue)
        self.assertIsNone(zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=uri))

    def test_a_marker_without_a_source_is_still_refreshed(self):
        self.generated(None)
        written = zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=self.orange)
        self.assertEqual(written, self.path)
        self.assertEqual(zone_theme.generated_source(self.path), self.orange)

    def test_a_hand_written_file_is_never_rewritten(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[colors]\naccent = #ff0000\n")
        self.assertIsNone(zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=self.orange))
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("accent = #ff0000", handle.read())

    def test_no_theme_file_and_no_wallpaper_write_nothing(self):
        self.assertIsNone(zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=self.orange))
        self.assertFalse(os.path.exists(self.path))
        self.generated(self.blue)
        before = os.stat(self.path).st_mtime_ns
        self.assertIsNone(zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=""))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_an_unreadable_image_keeps_the_old_palette(self):
        self.generated(self.blue)
        broken = os.path.join(self.dir, "broken.png")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("not a png")
        with self.assertRaises(ValueError):
            zone_theme.refresh_from_wallpaper(path=self.path, wallpaper=broken)
        self.assertEqual(zone_theme.generated_source(self.path), self.blue)


class TestGtkThemeFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.directory = os.path.join(self.dir, "Gridsnap")
        self.theme = zone_theme.Theme(
            dict(zone_theme.DEFAULT_COLORS), zone_theme.DEFAULT_ZONES, name="mine"
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def files(self):
        index = os.path.join(self.directory, "index.theme")
        css = os.path.join(self.directory, "gtk-3.0", "gtk.css")
        with open(index, encoding="utf-8") as handle:
            index_text = handle.read()
        with open(css, encoding="utf-8") as handle:
            css_text = handle.read()
        return index_text, css_text

    def test_the_theme_is_adwaita_dark_plus_the_palette(self):
        index, css = zone_theme.write_gtk_theme(self.directory, self.theme)
        self.assertEqual(index, os.path.join(self.directory, "index.theme"))
        css_text = self.files()[1]
        self.assertIn("gtk-contained-dark.css", css_text)
        self.assertIn(self.theme.colors["bg"], css_text)
        self.assertIn(self.theme.colors["accent"], css_text)
        self.assertIn("decoration, window.csd", css_text)

    def test_the_files_are_marked_as_generated(self):
        zone_theme.write_gtk_theme(self.directory, self.theme)
        index_text, css_text = self.files()
        self.assertTrue(index_text.startswith(zone_theme.GTK_MARK))
        self.assertTrue(css_text.startswith(zone_theme.GTK_CSS_MARK))
        self.assertIn("Name=Gridsnap", index_text)
        self.assertIn("Comment=Gridsnap controls (mine)", index_text)
        self.assertTrue(zone_theme.generated_gtk_theme(self.directory))


class TestGeneratedGtkTheme(unittest.TestCase):
    """Only a controls theme GridSnap wrote is ever refreshed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.directory = os.path.join(self.dir, "Gridsnap")
        self.theme = zone_theme.Theme(
            dict(zone_theme.DEFAULT_COLORS), zone_theme.DEFAULT_ZONES, name="mine"
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_index(self, text):
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, "index.theme")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_marked_theme_is_generated(self):
        zone_theme.write_gtk_theme(self.directory, self.theme)
        self.assertTrue(zone_theme.generated_gtk_theme(self.directory))

    def test_the_legacy_unmarked_theme_is_adopted(self):
        # The first version wrote no marker; its exact Name+Comment pair is the claim.
        self.write_index(
            "[Desktop Entry]\nType=X-GNOME-Metatheme\nName=Gridsnap\n"
            "Comment=Gridsnap controls (default)\nEncoding=UTF-8\n"
        )
        self.assertTrue(zone_theme.generated_gtk_theme(self.directory))

    def test_a_user_theme_is_not_generated(self):
        self.write_index("[Desktop Entry]\nType=X-GNOME-Metatheme\nName=Mine\n")
        self.assertFalse(zone_theme.generated_gtk_theme(self.directory))

    def test_a_name_without_the_comment_is_not_generated(self):
        self.write_index("[Desktop Entry]\nName=Gridsnap\n")
        self.assertFalse(zone_theme.generated_gtk_theme(self.directory))

    def test_an_absent_theme_is_not_generated(self):
        self.assertFalse(zone_theme.generated_gtk_theme(self.directory))

    def test_a_new_palette_rewrites_the_files(self):
        zone_theme.write_gtk_theme(self.directory, self.theme)
        other = zone_theme.Theme(
            {**zone_theme.DEFAULT_COLORS, "bg": "#000000"},
            zone_theme.DEFAULT_ZONES,
            name="other",
        )
        written = zone_theme.refresh_gtk_theme(directory=self.directory, theme=other)
        self.assertEqual(written, self.directory)
        with open(os.path.join(self.directory, "gtk-3.0", "gtk.css"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("#000000", text)
        self.assertIn('"other"', text)

    def test_the_same_palette_keeps_the_files(self):
        zone_theme.write_gtk_theme(self.directory, self.theme)
        index = os.path.join(self.directory, "index.theme")
        css = os.path.join(self.directory, "gtk-3.0", "gtk.css")
        before = (os.stat(index).st_mtime_ns, os.stat(css).st_mtime_ns)
        self.assertIsNone(
            zone_theme.refresh_gtk_theme(directory=self.directory, theme=self.theme)
        )
        self.assertEqual((os.stat(index).st_mtime_ns, os.stat(css).st_mtime_ns), before)

    def test_a_user_theme_is_never_rewritten(self):
        self.write_index("[Desktop Entry]\nType=X-GNOME-Metatheme\nName=Mine\n")
        self.assertIsNone(
            zone_theme.refresh_gtk_theme(directory=self.directory, theme=self.theme)
        )
        with open(os.path.join(self.directory, "index.theme"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "[Desktop Entry]\nType=X-GNOME-Metatheme\nName=Mine\n")

    def test_the_palette_comes_from_theme_conf(self):
        # No explicit theme: the file next to the config is what the surfaces see.
        conf = os.path.join(self.dir, "zones.conf")
        with open(conf, "w", encoding="utf-8") as handle:
            handle.write("[zones]\nmain = 0, 0, 1, 1\n")
        zone_theme.write_theme(self.theme, zone_theme.theme_path(conf))
        zone_theme.write_gtk_theme(self.directory, self.theme)
        other = zone_theme.Theme(
            {**zone_theme.DEFAULT_COLORS, "accent": "#00ff00"},
            zone_theme.DEFAULT_ZONES,
            name="other",
        )
        zone_theme.write_theme(other, zone_theme.theme_path(conf))
        self.assertEqual(zone_theme.refresh_gtk_theme(conf, self.directory), self.directory)
        with open(os.path.join(self.directory, "gtk-3.0", "gtk.css"), encoding="utf-8") as handle:
            self.assertIn("#00ff00", handle.read())


if __name__ == "__main__":
    unittest.main()

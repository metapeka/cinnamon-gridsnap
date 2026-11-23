#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the HUD mode of the picker (`zone-picker.py --hud`).

Three things are checked:

* the pure choice of a zone under a screen point — `zone_picker_nav.zone_at_point`
    (the same `zone_at`, only the screen pixels are translated into fractions of the work area);
    this class of tests runs without GTK and without X;
* parsing the arguments `--hud`/`--at`/`--follow`: without a point source — exit code 2,
    with the interactive flags — exit code 2, `--at` and `--follow` are mutually exclusive,
    `--at` understands both "X,Y" and "X Y";
* a shot `--hud --at X,Y --shot FILE.png` — the file is not empty, and different points
    (different zones) give different files, that is the highlight really is drawn.

The module needs `gi`/GTK and cairo (it imports them at file level), and the shot needs
a display as well (`Gdk.Display`), so in CI without GTK or without X these tests are
skipped. The pure part (point → zone) always works.

Running:
    python3 tests/test_picker_hud.py
    python3 -m unittest discover -s tests -v
    xvfb-run -a python3 tests/test_picker_hud.py   # together with the shot
"""

import collections
import contextlib
import importlib.util
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

import zone_picker_nav as nav  # noqa: E402


PICKER = os.path.join(ROOT, "zone-picker.py")

Rect = collections.namedtuple("Rect", "x y w h")

# Two zones over the full height: the border right in the middle of the work area.
LEFT, RIGHT = Rect(0, 0, 0.5, 1.0), Rect(0.5, 0, 0.5, 1.0)
TWO = [LEFT, RIGHT]
# The gap between the zones: 0.4..0.6 is covered by neither.
GAP = [Rect(0, 0, 0.4, 1.0), Rect(0.6, 0, 0.4, 1.0)]

AREA = (0, 0, 1000, 500)  # the work area of the monitor: screen pixels


def gui_available():
    """Are GTK and cairo available: without them `zone-picker.py` does not import."""
    try:
        import cairo  # noqa: F401
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except Exception:
        return False


def display_available():
    """Is there an X display: without it the monitors cannot be counted and the shot cannot be written."""
    if not HAS_GUI:
        return False
    try:
        from gi.repository import Gdk

        return Gdk.Display.get_default() is not None
    except Exception:
        return False


HAS_GUI = gui_available()
HAS_DISPLAY = display_available()

# The config for the shot: two zones, so that a point on the left and a point on the right land in different ones.
CONF2 = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    "[keys]\nleft = <Super><Alt>1\nright = <Super><Alt>2\n"
)


class TestZoneAtPoint(unittest.TestCase):
    """A point in screen pixels -> the zone index; without GTK and without X."""

    def test_point_inside_left_zone(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (100, 100)), 0)

    def test_point_inside_right_zone(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (700, 100)), 1)

    def test_point_at_bottom_right_corner(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (999, 499)), 1)

    def test_boundary_goes_to_later_zone(self):
        # Right on the seam the zone drawn later wins — as in zone_at.
        self.assertEqual(nav.zone_at_point(TWO, AREA, (500, 250)), 1)

    def test_gap_between_zones_has_no_zone(self):
        self.assertIsNone(nav.zone_at_point(GAP, AREA, (500, 250)))

    def test_point_outside_work_area(self):
        self.assertIsNone(nav.zone_at_point(TWO, AREA, (1500, 250)))
        self.assertIsNone(nav.zone_at_point(TWO, AREA, (-10, 250)))

    def test_work_area_offset_is_subtracted(self):
        # The monitor on the right: its work area starts at x=100, y=50.
        shifted = (100, 50, 1000, 500)
        self.assertEqual(nav.zone_at_point(TWO, shifted, (150, 60)), 0)
        self.assertEqual(nav.zone_at_point(TWO, shifted, (1050, 60)), 1)
        # the same point, but without the offset correction, would land in the left zone
        self.assertEqual(nav.zone_at_point(TWO, AREA, (150, 60)), 0)

    def test_empty_zones(self):
        self.assertIsNone(nav.zone_at_point([], AREA, (100, 100)))

    def test_accepts_objects_like_zone_config(self):
        class Zone:
            def __init__(self, x, y, w, h):
                self.x, self.y, self.w, self.h = x, y, w, h

        zones = [Zone(0, 0, 0.5, 1), Zone(0.5, 0, 0.5, 1)]
        self.assertEqual(nav.zone_at_point(zones, AREA, (900, 400)), 1)


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo: zone-picker.py не загрузить")
class PickerHudArgsCase(unittest.TestCase):
    """Parsing the arguments and the small HUD helpers; no window and no X needed."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("zone_picker_hud_under_test", PICKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.picker = module

    def parse(self, argv):
        """Parse the arguments; SystemExit and stderr go outside, to the test."""
        return self.picker.parse_args(argv)

    def parse_error(self, argv):
        """The return code and the parse error text for deliberately invalid arguments."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self.picker.parse_args(argv)
        return ctx.exception.code, err.getvalue()

    # ---- --hud needs a point source

    def test_hud_without_point_source_exits_2(self):
        code, err = self.parse_error(["--hud"])
        self.assertEqual(code, 2)
        self.assertIn("--hud needs a point source", err)

    def test_hud_with_at(self):
        args = self.parse(["--hud", "--at", "100,100"])
        self.assertTrue(args.hud)
        self.assertEqual(args.at, (100, 100))
        self.assertFalse(args.follow)

    def test_hud_with_follow(self):
        args = self.parse(["--hud", "--follow"])
        self.assertTrue(args.hud)
        self.assertTrue(args.follow)
        self.assertIsNone(args.at)

    def test_at_and_follow_are_mutually_exclusive(self):
        code, err = self.parse_error(["--hud", "--at", "1,2", "--follow"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_at_without_hud_exits_2(self):
        code, err = self.parse_error(["--at", "100,100"])
        self.assertEqual(code, 2)
        self.assertIn("only work together with --hud", err)

    def test_follow_without_hud_exits_2(self):
        code, err = self.parse_error(["--follow"])
        self.assertEqual(code, 2)
        self.assertIn("only work together with --hud", err)

    # ---- --hud is incompatible with the interactive mode

    def test_hud_with_interactive_flags_exits_2(self):
        for extra in (["--select", "left"], ["--window", "0x1"], ["--dry-run"], ["--verify"]):
            with self.subTest(extra=extra):
                code, err = self.parse_error(["--hud", "--at", "1,2"] + extra)
                self.assertEqual(code, 2)
                self.assertIn("--hud is incompatible", err)
                flag = extra[0]
                self.assertIn(flag, err)

    def test_shot_with_follow_exits_2(self):
        code, err = self.parse_error(["--hud", "--follow", "--shot", "/tmp/x.png"])
        self.assertEqual(code, 2)
        self.assertIn("--shot does not work with --follow", err)

    def test_hud_keeps_common_flags(self):
        # --conf/--monitor/--workspace/--shot are common: the HUD accepts them.
        args = self.parse(
            ["--hud", "--at", "5,6", "--shot", "/tmp/x.png", "--conf", "/tmp/z.conf",
             "--monitor", "1", "--workspace", "conf"]
        )
        self.assertEqual(args.shot, "/tmp/x.png")
        self.assertEqual(args.conf, "/tmp/z.conf")
        self.assertEqual(args.monitor, 1)
        self.assertEqual(args.workspace, self.picker.zt.WORKSPACE_CONF)

    # ---- the point: one parser for --at and for the stdin lines

    def test_parse_point_formats(self):
        parse = self.picker.parse_point
        self.assertEqual(parse("100,100"), (100, 100))
        self.assertEqual(parse("100 100"), (100, 100))
        self.assertEqual(parse("  -5, 7 "), (-5, 7))
        self.assertEqual(parse(b"640 512"), (640, 512))
        self.assertEqual(parse("100,100\n"), (100, 100))

    def test_parse_point_rejects_garbage(self):
        parse = self.picker.parse_point
        for text in ("", "12", "1,2,3", "abc", "1;2", "1,два", None):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_bad_at_value_exits_2(self):
        code, err = self.parse_error(["--hud", "--at", "12"])
        self.assertEqual(code, 2)
        self.assertIn("screen coordinates", err)

    # ---- the profile caption

    def test_hud_caption(self):
        caption = self.picker.hud_caption
        self.assertEqual(caption("cinema"), 'profile "cinema"')
        self.assertEqual(caption(None), "")
        self.assertEqual(caption(""), "")

    def test_hud_caption_names_screen_when_its_profile_gave_grid(self):
        """The screen profile takes precedence over the workspace in the HUD caption too, not only in the overlay."""
        caption = self.picker.hud_caption
        self.assertEqual(caption("cinema", "HDMI-1", "studio"),
                         'monitor HDMI-1 · profile "studio"')
        self.assertEqual(caption("cinema", "HDMI-1", None), 'profile "cinema"')
        self.assertEqual(caption(None, "HDMI-1", None), "")

    # ---- the HUD is drawn without the key hint

    def test_draw_zones_without_hint_differs_from_hint(self):
        """An empty hint removes the "arrows/Enter" hint: the HUD does not listen to them."""
        import cairo

        zone = self.picker.zc.Zone("left", 0.0, 0.0, 0.5, 1.0, "")

        def render(hint):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
            cr = cairo.Context(surface)
            cr.set_source_rgb(0, 0, 0)
            cr.paint()
            self.picker.draw_zones(cr, 400, 300, [zone], 0, 'profile "cinema"', hint)
            surface.flush()
            return bytes(surface.get_data())

        self.assertNotEqual(render(""), render(self.picker.HINT))


@unittest.skipUnless(
    HAS_GUI and HAS_DISPLAY, "нужны GTK, cairo и X-дисплей: снимок HUD считается по мониторам"
)
class PickerHudShotCase(unittest.TestCase):
    """`--hud --at X,Y --shot`: the PNG is not empty, different points — different files."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("zone_picker_hud_shot_under_test", PICKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.picker = module

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-picker-hud-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        with open(self.conf, "w", encoding="utf-8") as handle:
            handle.write(CONF2)

    def shot(self, name, point):
        """Run `main` in the HUD shot mode and return the path, the size and the output."""
        path = os.path.join(self.dir, name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.picker.main(
                ["--hud", "--at", f"{point[0]},{point[1]}", "--shot", path,
                 "--conf", self.conf, "--monitor", "0", "--workspace", "conf"]
            )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(path), out.getvalue())
        with open(path, "rb") as handle:
            return path, handle.read(), out.getvalue()

    def test_shot_is_not_empty_and_names_the_zone(self):
        _path, data, out = self.shot("hud.png", (100, 100))
        self.assertTrue(data.startswith(b"\x89PNG"), "снимок — не PNG")
        self.assertGreater(len(data), 1000, "PNG подозрительно пустой")
        self.assertIn("снимок HUD", out)
        self.assertIn("-> left", out)

    def test_different_points_highlight_different_zones(self):
        _p1, left_png, out_left = self.shot("left.png", (100, 100))
        _p2, right_png, out_right = self.shot("right.png", (700, 100))
        self.assertIn("-> left", out_left)
        self.assertIn("-> right", out_right)
        # different zones are highlighted — so the pictures differ too
        self.assertNotEqual(left_png, right_png, "подсветка не попала в снимок")

    def test_point_in_gap_highlights_nothing(self):
        # CONF2 has no gap, so we take a point beyond the right edge of the work area:
        # there is no highlight, but the grid is drawn anyway.
        _path, data, out = self.shot("gap.png", (10_000, 10_000))
        self.assertIn("мимо зон", out)
        self.assertGreater(len(data), 1000)


if __name__ == "__main__":
    unittest.main()

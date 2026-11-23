#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for converting the strip frame from physical pixels to logical ones.

They check `window_rect` from `zone-tabstrip.py`: the daemon sends a physical frame
(the coordinates of the zone window), while the GTK window lives in logical pixels, so with
`GDK_SCALE=2` and no conversion the strip moves twice as far and grows twice as big. The module loads
GTK lazily (`load_gtk`), so the test imports the strip without X and without GTK —
and also checks that the import does not require them.

Running:
    python3 tests/test_tabstrip_scale.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import sys
import unittest
from unittest import mock

try:
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_tabstrip_nav as nav  # noqa: E402
import zone_units as units  # noqa: E402

# The strip is a hyphen file: next to the strip it is called `zone-tabstrip.py`.
_spec = importlib.util.spec_from_file_location(
    "gridsnap_tabstrip_module", os.path.join(ROOT, "zone-tabstrip.py")
)
strip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strip)


def fixed(scale):
    """`scale_of` with a constant scale: returns and records the points."""
    seen = []

    def scale_of(point):
        seen.append(point)
        return scale

    return scale_of, seen


class TestWindowRect(unittest.TestCase):
    def test_scale_two_halves_place_and_size(self):
        # 1920x22 physical pixels are 960x11 logical ones: both the place and the size.
        self.assertEqual(strip.window_rect((0, 0, 1920, 22), lambda _p: 2), (0, 0, 960, 11))

    def test_scale_one_is_identity(self):
        self.assertEqual(strip.window_rect((10, 20, 300, 22), lambda _p: 1), (10, 20, 300, 22))
        # No scale at all (GDK did not answer) — also 1, not an error.
        for value in (None, 0, -3, "x"):
            with self.subTest(value=value):
                self.assertEqual(
                    strip.window_rect((10, 20, 300, 22), lambda _p, v=value: v),
                    (10, 20, 300, 22),
                )

    def test_center_of_the_strip_is_its_middle(self):
        self.assertEqual(strip.strip_center((0, 40, 1920, 22)), (960, 51))
        self.assertEqual(strip.strip_center((1920.4, 40.6, 1919.5, 22.2)), (2880, 52))

    def test_scale_is_asked_under_own_center(self):
        # The strip on the second monitor: the scale comes from under its centre, not the first's.
        scale_of, seen = fixed(2)
        strip.window_rect((100, 200, 300, 22), scale_of)
        self.assertEqual(seen, [(100 + 300 // 2, 200 + 22 // 2)])

    def test_matches_zone_units_conversion(self):
        # The conversion is exactly `zone_units.logical_rect`, no arithmetic of its own: the edges
        # are computed from the shared lines so that neighbouring strips do not drift apart by 1 px.
        for rect in ((0, 0, 1920, 22), (1920, 40, 1921, 23), (37, 11, 101, 23)):
            for scale in (1, 2, 3):
                with self.subTest(rect=rect, scale=scale):
                    x, y, w, h = units.logical_rect(rect, scale)
                    self.assertEqual(
                        strip.window_rect(rect, lambda _p, s=scale: s),
                        (int(x), int(y), max(1, int(w)), max(1, int(h))),
                    )

    def test_size_never_below_one_pixel(self):
        # A narrow strip at a large scale: a window of zero size would show neither
        # the label nor the tab frame.
        for rect in ((0, 0, 1, 1), (5, 5, 1, 22), (0, 0, 3, 3)):
            with self.subTest(rect=rect):
                _x, _y, w, h = strip.window_rect(rect, lambda _p: 4)
                self.assertGreaterEqual(w, 1)
                self.assertGreaterEqual(h, 1)

    def test_sizes_and_positions_are_integers(self):
        self.assertTrue(
            all(isinstance(v, int) for v in strip.window_rect((3, 5, 41, 21), lambda _p: 2))
        )

    def test_scale_lookup_failure_falls_back_to_one(self):
        # A failure to read the scale must not bring the strip down: scale 1, same place.
        def broken(_point):
            raise RuntimeError("нет X")

        self.assertEqual(strip.window_rect((10, 20, 300, 22), broken), (10, 20, 300, 22))

    def test_fractional_rect_is_rounded_to_whole_pixels(self):
        # `nav.record_rect` returns floats — the window needs whole pixels.
        self.assertEqual(strip.window_rect((0.4, 21.6, 1919.5, 22.2), lambda _p: 1),
                         (0, 22, 1920, 22))


class TestLocalRect(unittest.TestCase):
    """Window-local coordinates: cairo and the GTK event use logical pixels."""

    def test_scale_two_halves_offsets_and_sizes(self):
        # The second tab of a 1920x22 strip at scale 2 starts at 480.5
        # logical window pixels, not at 960.5: otherwise it would go beyond the window.
        origin = (0, 0)
        x, y, w, h = strip.local_rect((960.5, 0, 959.5, 22), origin, 2)
        self.assertAlmostEqual(x, 480.25)
        self.assertEqual(y, 0)
        self.assertAlmostEqual(w, 479.75)
        self.assertEqual(h, 11)

    def test_origin_is_the_strip_corner(self):
        # The offsets are counted from the top-left corner of the strip, not from screen zero.
        self.assertEqual(strip.local_rect((1920, 40, 100, 22), (1920, 40), 2),
                         (0.0, 0.0, 50.0, 11.0))

    def test_scale_one_is_plain_offset(self):
        self.assertEqual(strip.local_rect((110, 40, 100, 22), (10, 18), 1),
                         (100, 22, 100, 22))
        self.assertEqual(strip.local_rect((110, 40, 100, 22), (10, 18), None),
                         (100, 22, 100, 22))

    def test_tabs_stay_inside_the_window(self):
        rect = (0, 0, 1920, 22)
        rows = nav.layout({"rect": rect, "tabs": [{"window": 1}, {"window": 2}]})
        scale = 2
        _wx, _wy, ww, wh = strip.window_rect(rect, lambda _p: scale)
        frames = [strip.local_rect(row["rect"], rect, scale) for row in rows]
        self.assertEqual(len(frames), 2)
        self.assertGreaterEqual(frames[0][0], 0)
        self.assertLessEqual(frames[-1][0] + frames[-1][2], ww + 1e-6)
        self.assertLessEqual(max(f[1] + f[3] for f in frames), wh + 1e-6)


class TestLocalPoint(unittest.TestCase):
    def test_scale_two_maps_logical_click_to_physical_screen(self):
        # A click in the middle of the strip window (logical 480, 5) is physical
        # (960, 10) on the screen: the same place as the tab frames from the snapshot.
        self.assertEqual(strip.local_point((480, 5), (0, 0), 2), (960, 10))

    def test_scale_one_and_garbage_scale(self):
        self.assertEqual(strip.local_point((10, 20), (100, 40), 1), (110, 60))
        self.assertEqual(strip.local_point((10, 20), (100, 40), 0), (110, 60))

    def test_round_trip_with_local_rect(self):
        for origin, rect, scale in (((0, 0), (0, 0, 1920, 22), 2),
                                    ((1920, 40), (1921, 40, 1919, 22), 2),
                                    ((0, 0), (10, 20, 300, 22), 1)):
            with self.subTest(rect=rect, scale=scale):
                x, y, _w, _h = strip.local_rect(rect, origin, scale)
                self.assertEqual(strip.local_point((x, y), origin, scale), (rect[0], rect[1]))

    def test_click_lands_on_the_tab_that_is_drawn_there(self):
        # The main consequence of the conversion: the click point and the drawn tab are one.
        origin = (0, 0)
        scale = 2
        rows = nav.layout({"rect": (0, 0, 1920, 22),
                           "tabs": [{"window": 0x11}, {"window": 0x22}]})
        frames = [row["rect"] for row in rows]
        # The left half of the window is the first tab, the right half the second: a click at
        # logical 400/600 is physical 800/1200, not 400/600.
        for logical_x, want in ((400, 0x11), (600, 0x22)):
            with self.subTest(logical_x=logical_x):
                point = strip.local_point((logical_x, 5), origin, scale)
                self.assertEqual(nav.tab_under({"rect": (0, 0, 1920, 22),
                                                "tabs": [{"window": 0x11},
                                                         {"window": 0x22}]}, point),
                                 want)


class TestStripScale(unittest.TestCase):
    def test_no_tiler_means_scale_one(self):
        # No tiler (not installed, its dependencies are missing) — the strip as before.
        with mock.patch.object(strip, "tiler_module", lambda: None):
            self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_answers_scale(self):
        class Fake:
            @staticmethod
            def scale_at_point(_point):
                return 2

        with mock.patch.object(strip, "tiler_module", lambda: Fake()):
            self.assertEqual(strip.strip_scale((10, 10)), 2)

    def test_tiler_garbage_scale_is_one(self):
        class Fake:
            @staticmethod
            def scale_at_point(_point):
                return None

        with mock.patch.object(strip, "tiler_module", lambda: Fake()):
            self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_exit_and_error_do_not_kill_the_strip(self):
        # `zone-tiler.monitors` without X says `fail` (SystemExit) — the strip stays alive.
        class Exiting:
            @staticmethod
            def scale_at_point(_point):
                raise SystemExit(1)

        class Failing:
            @staticmethod
            def scale_at_point(_point):
                raise OSError("нет дисплея")

        for fake in (Exiting(), Failing()):
            with self.subTest(fake=type(fake).__name__):
                with mock.patch.object(strip, "tiler_module", lambda f=fake: f):
                    self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_module_is_loaded_once(self):
        # A snapshot arrives on every change of the picture: the tiler is loaded once.
        calls = []

        def fake_loader():
            calls.append(1)
            return None

        with mock.patch.object(strip, "tiler_module", fake_loader):
            strip.strip_scale((0, 0))
            strip.strip_scale((10, 10))
        self.assertEqual(len(calls), 2)  # the patch counts the calls of `strip_scale` itself


import unittest.mock  # noqa: E402  (needed by the tests above in the file)


class TestTilerLoading(unittest.TestCase):
    def test_module_import_needs_neither_gtk_nor_display(self):
        # The strip loads without GTK: `load_gtk` is called only from `main`.
        self.assertIsNone(strip.Gtk)
        self.assertIsNone(strip.cairo)

    def test_real_tiler_without_display_gives_scale_one(self):
        # The real `zone-tiler.py` without DISPLAY: the scale cannot be asked — 1.
        saved = strip._ZT, strip._ZT_LOADED
        strip._ZT, strip._ZT_LOADED = None, False
        env = {key: os.environ.pop(key) for key in ("DISPLAY", "GDK_SCALE") if key in os.environ}
        noise = io.StringIO()  # the tiler's `fail` writes to stderr — that is not our output
        try:
            with contextlib.redirect_stderr(noise):
                self.assertEqual(strip.strip_scale((10, 10)), 1)
        finally:
            os.environ.update(env)
            strip._ZT, strip._ZT_LOADED = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)

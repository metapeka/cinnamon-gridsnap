#!/usr/bin/env python3
"""Юнит-тесты чистой геометрии редактора — без X-сервера и без GTK.

Проверяют `zone_geom`: расчёт «экрана» на холсте, перевод долей в пиксели и
обратно, прилипание (направляющие, края зон, допуск, пропуск своей зоны),
перетаскивание (move/resize, границы, минимальный размер), сдвиг с клавиатуры,
колесо и попадание курсора.

Запуск:
    python3 tests/test_zone_geom.py
"""

import collections
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_geom as geom  # noqa: E402

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

Rect = collections.namedtuple("Rect", "x y w h")


class TestScreenRect(unittest.TestCase):
    def test_fits_workarea_into_canvas_with_margin(self):
        sx, sy, sw, sh = geom.screen_rect(1000, 800, (0, 0, 1920, 1080))
        self.assertAlmostEqual(sw, 948)
        self.assertAlmostEqual(sh, 1080 * 948 / 1920)
        self.assertAlmostEqual(sx, (1000 - sw) / 2)
        self.assertAlmostEqual(sy, (800 - sh) / 2)

    def test_respects_workarea_offset(self):
        # смещение рабочей области на размер «экрана» не влияет
        a = geom.screen_rect(1000, 800, (100, 40, 1000, 500))
        b = geom.screen_rect(1000, 800, (0, 0, 1000, 500))
        self.assertEqual(a, b)

    def test_tiny_canvas_clamps_to_minimum(self):
        _, _, sw, sh = geom.screen_rect(10, 10, (0, 0, 1000, 1000), margin=26)
        self.assertGreater(sw, 0)
        self.assertGreater(sh, 0)


class TestPxFrac(unittest.TestCase):
    def setUp(self):
        self.screen = geom.screen_rect(1000, 800, (0, 0, 1000, 500))

    def test_to_px_covers_screen(self):
        x, y, w, h = geom.to_px(self.screen, (0, 0, 1, 1))
        self.assertEqual((x, y, w, h), self.screen)

    def test_to_px_accepts_object(self):
        self.assertEqual(
            geom.to_px(self.screen, Rect(0, 0, 0.5, 0.5)),
            geom.to_px(self.screen, (0, 0, 0.5, 0.5)),
        )

    def test_roundtrip_center(self):
        sx, sy, sw, sh = self.screen
        fx, fy = geom.to_frac(self.screen, sx + sw / 2, sy + sh / 2)
        self.assertAlmostEqual(fx, 0.5)
        self.assertAlmostEqual(fy, 0.5)

    def test_roundtrip_corner(self):
        sx, sy, sw, sh = self.screen
        fx, fy = geom.to_frac(self.screen, sx, sy)
        self.assertAlmostEqual(fx, 0.0)
        self.assertAlmostEqual(fy, 0.0)


class TestSnap(unittest.TestCase):
    def test_snaps_to_guide(self):
        self.assertEqual(geom.snap_frac(0.251, "x", 0.01, []), 0.25)

    def test_snaps_to_zone_edge(self):
        zones = [Rect(0.3, 0, 0.2, 1)]
        self.assertEqual(geom.snap_frac(0.305, "x", 0.01, zones), 0.3)

    def test_skips_dragged_zone(self):
        zones = [Rect(0.3, 0, 0.2, 1)]
        self.assertEqual(geom.snap_frac(0.305, "x", 0.01, zones, skip=0), 0.305)

    def test_outside_tolerance_is_unchanged(self):
        self.assertEqual(geom.snap_frac(0.4, "x", 0.01, []), 0.4)

    def test_vertical_axis_uses_y_edges(self):
        zones = [Rect(0, 0.1, 1, 0.9)]
        self.assertAlmostEqual(geom.snap_frac(0.098, "y", 0.01, zones), 0.1)

    def test_snap_tol_in_fractions(self):
        screen = (0, 0, 1000, 500)
        self.assertAlmostEqual(geom.snap_tol(screen, "x", 10), 0.01)
        self.assertAlmostEqual(geom.snap_tol(screen, "y", 10), 0.02)


class TestSnapMove(unittest.TestCase):
    def test_left_edge_snaps(self):
        zones = [Rect(0.3, 0, 0.2, 1)]
        self.assertEqual(geom.snap_move(0.305, 0.2, "x", 0.01, zones), 0.3)

    def test_right_edge_snaps(self):
        zones = [Rect(0.3, 0, 0.5, 1)]
        # правый край 0.29 притягивается к 0.3 -> зона начинается с 0.1
        self.assertAlmostEqual(geom.snap_move(0.09, 0.2, "x", 0.011, zones), 0.1)

    def test_outside_any_magnet_is_unchanged(self):
        self.assertEqual(geom.snap_move(0.4, 0.2, "x", 0.0, []), 0.4)


class TestDragMove(unittest.TestCase):
    def test_moves_by_cursor_delta(self):
        x, y, w, h = geom.drag_rect((0.2, 0.2, 0.2, 0.2), "move", set(), (0.3, 0.3), (0.35, 0.35), [], 0, 0)
        self.assertAlmostEqual(x, 0.25)
        self.assertAlmostEqual(y, 0.25)
        self.assertAlmostEqual(w, 0.2)
        self.assertAlmostEqual(h, 0.2)

    def test_clamped_inside_screen(self):
        new = geom.drag_rect((0.0, 0.0, 0.3, 0.3), "move", set(), (0.0, 0.0), (-0.5, -0.5), [], 0, 0)
        self.assertEqual(new, (0.0, 0.0, 0.3, 0.3))

    def test_clamped_at_right_border(self):
        new = geom.drag_rect((0.7, 0.7, 0.3, 0.3), "move", set(), (0, 0), (0.5, 0.5), [], 0, 0)
        self.assertEqual(new, (0.7, 0.7, 0.3, 0.3))

    def test_snaps_to_neighbor_edge(self):
        zones = [Rect(0.5, 0, 0.3, 0.3)]
        new = geom.drag_rect((0.1, 0.0, 0.2, 0.2), "move", set(), (0.0, 0.0), (0.19, 0.0), zones, 0.02, 0.02)
        self.assertAlmostEqual(new[0], 0.3)

    def test_dragged_zone_not_used_as_magnet(self):
        # своя же зона не должна притягивать к собственным краям
        zones = [Rect(0.1, 0.0, 0.2, 0.2)]
        new = geom.drag_rect((0.1, 0.0, 0.2, 0.2), "move", set(), (0.0, 0.0), (0.005, 0.0), zones, 0.02, 0.02, skip=0)
        self.assertAlmostEqual(new[0], 0.105)


class TestDragResize(unittest.TestCase):
    def test_right_edge(self):
        new = geom.drag_rect((0, 0, 0.5, 0.5), "resize", {"r"}, (0, 0), (0.1, 0), [], 0, 0)
        self.assertAlmostEqual(new[2], 0.6)
        self.assertAlmostEqual(new[0], 0.0)

    def test_left_edge(self):
        new = geom.drag_rect((0.2, 0, 0.5, 0.5), "resize", {"l"}, (0, 0), (0.1, 0), [], 0, 0)
        self.assertAlmostEqual(new[0], 0.3)
        self.assertAlmostEqual(new[2], 0.4)

    def test_bottom_edge(self):
        new = geom.drag_rect((0, 0, 0.5, 0.5), "resize", {"b"}, (0, 0), (0, 0.2), [], 0, 0)
        self.assertAlmostEqual(new[3], 0.7)

    def test_minimum_size_is_kept(self):
        new = geom.drag_rect((0, 0, 0.1, 0.5), "resize", {"r"}, (0, 0), (-0.5, 0), [], 0, 0, min_size=0.05)
        self.assertAlmostEqual(new[2], 0.05)

    def test_clamped_to_one(self):
        new = geom.drag_rect((0, 0, 0.9, 0.5), "resize", {"r"}, (0, 0), (0.5, 0), [], 0, 0)
        self.assertAlmostEqual(new[0] + new[2], 1.0)

    def test_corner_resizes_both_axes(self):
        new = geom.drag_rect((0.1, 0.1, 0.3, 0.3), "resize", {"l", "t"}, (0, 0), (0.1, 0.1), [], 0, 0)
        self.assertAlmostEqual(new[0], 0.2)
        self.assertAlmostEqual(new[1], 0.2)
        self.assertAlmostEqual(new[2], 0.2)
        self.assertAlmostEqual(new[3], 0.2)

    def test_snaps_edge_to_guide(self):
        new = geom.drag_rect((0, 0, 0.4, 0.5), "resize", {"r"}, (0, 0), (0.099, 0), [], 0.01, 0.01)
        self.assertAlmostEqual(new[2], 0.5)


class TestNudgeAndScroll(unittest.TestCase):
    def test_nudge_moves(self):
        self.assertEqual(geom.nudge((0.5, 0.5, 0.2, 0.2), 0.1, 0.0), (0.6, 0.5, 0.2, 0.2))

    def test_nudge_clamps(self):
        self.assertEqual(geom.nudge((0.9, 0.9, 0.2, 0.2), 0.1, 0.1), (0.8, 0.8, 0.2, 0.2))

    def test_nudge_resizes(self):
        self.assertEqual(geom.nudge((0.5, 0.5, 0.2, 0.2), 0.5, 0.0, resize=True), (0.5, 0.5, 0.5, 0.2))

    def test_nudge_resize_keeps_minimum(self):
        self.assertEqual(geom.nudge((0.0, 0.0, 0.2, 0.2), -1.0, 0.0, resize=True), (0.0, 0.0, 0.05, 0.2))

    def test_scroll_resizes_width(self):
        x, y, w, h = geom.scroll_resize((0.5, 0, 0.2, 0.5), 0.1)
        self.assertAlmostEqual(w, 0.3)
        self.assertEqual((x, y, h), (0.5, 0, 0.5))

    def test_scroll_keeps_minimum(self):
        self.assertEqual(geom.scroll_resize((0.0, 0, 0.2, 0.5), -1.0), (0.0, 0, 0.05, 0.5))

    def test_scroll_clamped_to_screen(self):
        _, _, w, _ = geom.scroll_resize((0.9, 0, 0.05, 0.5), 0.1)
        self.assertAlmostEqual(w, 0.1)


class TestHitTest(unittest.TestCase):
    SCREEN = (0, 0, 1000, 1000)
    ZONES = (Rect(0.1, 0.1, 0.2, 0.2),)  # пиксели: x=100 y=100 w=200 h=200

    def test_inside_is_move(self):
        self.assertEqual(geom.hit_test(200, 200, self.SCREEN, self.ZONES), (0, "move", set()))

    def test_near_left_edge_is_resize(self):
        idx, mode, edges = geom.hit_test(103, 200, self.SCREEN, self.ZONES)
        self.assertEqual((idx, mode), (0, "resize"))
        self.assertEqual(edges, {"l"})

    def test_corner_has_two_edges(self):
        _, _, edges = geom.hit_test(103, 103, self.SCREEN, self.ZONES)
        self.assertEqual(edges, {"l", "t"})

    def test_just_outside_edge_is_move(self):
        self.assertEqual(geom.hit_test(97, 200, self.SCREEN, self.ZONES), (0, "move", set()))

    def test_far_outside_is_nothing(self):
        self.assertEqual(geom.hit_test(50, 50, self.SCREEN, self.ZONES), (None, "", set()))

    def test_topmost_zone_wins(self):
        zones = [Rect(0, 0, 0.5, 0.5), Rect(0.25, 0.25, 0.5, 0.5)]  # вторая нарисована поверх
        idx, _, _ = geom.hit_test(300, 300, (0, 0, 1000, 1000), zones)
        self.assertEqual(idx, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

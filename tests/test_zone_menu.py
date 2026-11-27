#!/usr/bin/env python3
"""The pure half of the layout menus: the thumbnail geometry and the compact labels.

The Templates and Profiles menus draw a layout instead of naming it, and the picture has
to be derived from the same fractions the canvas uses. That maths lives in `zone_menu`,
without GTK or X, so it is checked here — in CI, where `python3-gi` is missing, the menus
themselves cannot be built at all.

Run: `python3 tests/test_zone_menu.py` or through `make test`.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402
import zone_menu  # noqa: E402


def zones(*specs):
    """``Zone`` objects from ``(name, x, y, w, h)`` tuples — the model the canvas paints."""
    return [zc.Zone(name, x, y, w, h, "") for name, x, y, w, h in specs]


class ThumbRectsTest(unittest.TestCase):
    WIDTH, HEIGHT = 100.0, 60.0

    def test_two_columns_split_the_box(self):
        rects = zone_menu.thumb_rects(
            zones(("left", 0, 0, 0.5, 1), ("right", 0.5, 0, 0.5, 1)), self.WIDTH, self.HEIGHT
        )
        self.assertEqual(len(rects), 2)
        left, right = rects
        self.assertAlmostEqual(left[0], zone_menu.THUMB_INSET)
        self.assertAlmostEqual(left[1], zone_menu.THUMB_INSET)
        self.assertAlmostEqual(
            right[0], zone_menu.THUMB_INSET + 0.5 * (self.WIDTH - 2 * zone_menu.THUMB_INSET)
        )
        self.assertAlmostEqual(left[3], self.HEIGHT - 2 * zone_menu.THUMB_INSET - zone_menu.THUMB_GAP)

    def test_every_rect_stays_inside_the_box(self):
        rects = zone_menu.thumb_rects(
            zones(("a", 0, 0, 0.5, 0.5), ("b", 0.5, 0.5, 0.5, 0.5)), self.WIDTH, self.HEIGHT
        )
        for x, y, w, h in rects:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, self.WIDTH)
            self.assertLessEqual(y + h, self.HEIGHT)

    def test_neighbours_are_separated_by_the_gap(self):
        rects = zone_menu.thumb_rects(
            zones(("a", 0, 0, 0.5, 1), ("b", 0.5, 0, 0.5, 1)), self.WIDTH, self.HEIGHT
        )
        first_right = rects[0][0] + rects[0][2]
        self.assertGreaterEqual(rects[1][0] - first_right, zone_menu.THUMB_GAP - 0.5)

    def test_tiny_zones_stay_visible(self):
        rects = zone_menu.thumb_rects(zones(("dot", 0.1, 0.1, 0.001, 0.001)), 20, 16)
        x, y, w, h = rects[0]
        self.assertGreaterEqual(w, 1.0)
        self.assertGreaterEqual(h, 1.0)

    def test_smaller_box_than_the_inset_still_has_a_place(self):
        rects = zone_menu.thumb_rects(zones(("a", 0, 0, 1, 1)), 3, 3)
        x, y, w, h = rects[0]
        self.assertTrue(all(isinstance(value, float) for value in (x, y, w, h)))
        self.assertGreaterEqual(w, 1.0)
        self.assertGreaterEqual(h, 1.0)

    def test_empty_set_has_no_rectangles(self):
        self.assertEqual(zone_menu.thumb_rects([], self.WIDTH, self.HEIGHT), [])


class CompactRangesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(zone_menu.compact_ranges([]), "")

    def test_single(self):
        self.assertEqual(zone_menu.compact_ranges([3]), "3")

    def test_contiguous_run(self):
        self.assertEqual(zone_menu.compact_ranges([1, 2, 3, 4]), "1\u20134")

    def test_gaps_become_separate_parts(self):
        self.assertEqual(zone_menu.compact_ranges([1, 2, 4]), "1\u20132, 4")

    def test_order_and_duplicates_do_not_matter(self):
        self.assertEqual(zone_menu.compact_ranges([4, 1, 3, 2, 2]), "1\u20134")

    def test_two_runs(self):
        self.assertEqual(zone_menu.compact_ranges([1, 2, 3, 7, 8]), "1\u20133, 7\u20138")


if __name__ == "__main__":
    unittest.main()

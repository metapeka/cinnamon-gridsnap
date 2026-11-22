#!/usr/bin/env python3
"""Юнит-тесты чистой навигации выбора зоны — без X-сервера и без GTK.

Проверяют `zone_picker_nav`: начальное выделение, перемещение стрелками
(все четыре направления из центра, границы списка, зоны разной геометрии),
зону под курсором (в т.ч. перекрытие и «щель»), пустой список и нормализацию
индекса. Модуль не тянет за собой GTK, поэтому тесты идут в обычном CI.

Запуск:
    python3 tests/test_picker_nav.py
    python3 -m unittest discover -s tests -v
"""

import collections
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_picker_nav as nav  # noqa: E402

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

Rect = collections.namedtuple("Rect", "x y w h")

# Сетка 2×2 в порядке [A, B, C, D]; A — левый верх, D — правый низ.
A, B = Rect(0, 0, 0.5, 0.5), Rect(0.5, 0, 0.5, 0.5)
C, D = Rect(0, 0.5, 0.5, 0.5), Rect(0.5, 0.5, 0.5, 0.5)
GRID2 = [A, B, C, D]

# Сетка 3×3; индекс = row * 3 + col, центр — индекс 4.
GRID3 = [Rect(c / 3.0, r / 3.0, 1 / 3.0, 1 / 3.0) for r in range(3) for c in range(3)]


class TestRectCenter(unittest.TestCase):
    def test_rect_from_object_and_tuple(self):
        self.assertEqual(nav.rect(A), (0.0, 0.0, 0.5, 0.5))
        self.assertEqual(nav.rect((0, 0, 0.5, 0.5)), (0.0, 0.0, 0.5, 0.5))

    def test_center(self):
        self.assertEqual(nav.center(A), (0.25, 0.25))
        self.assertEqual(nav.center(D), (0.75, 0.75))


class TestInitialSelection(unittest.TestCase):
    def test_first_index_non_empty(self):
        self.assertEqual(nav.first_index(GRID2), 0)

    def test_first_index_empty(self):
        self.assertIsNone(nav.first_index([]))


class TestClampIndex(unittest.TestCase):
    def test_none_and_out_of_range(self):
        self.assertEqual(nav.clamp_index(GRID2, None), 0)
        self.assertEqual(nav.clamp_index(GRID2, -5), 0)
        self.assertEqual(nav.clamp_index(GRID2, 99), len(GRID2) - 1)
        self.assertEqual(nav.clamp_index(GRID2, 2), 2)

    def test_bad_value_falls_back_to_zero(self):
        self.assertEqual(nav.clamp_index(GRID2, "мусор"), 0)

    def test_empty_list_is_none(self):
        self.assertIsNone(nav.clamp_index([], 0))


class TestMoveGrid2(unittest.TestCase):
    def test_right_from_a_goes_to_b(self):
        self.assertEqual(nav.move_selection(GRID2, 0, "right"), 1)

    def test_left_from_b_goes_to_a(self):
        self.assertEqual(nav.move_selection(GRID2, 1, "left"), 0)

    def test_down_from_a_goes_to_c(self):
        self.assertEqual(nav.move_selection(GRID2, 0, "down"), 2)

    def test_up_from_c_goes_to_a(self):
        self.assertEqual(nav.move_selection(GRID2, 2, "up"), 0)

    def test_from_d_all_directions(self):
        self.assertEqual(nav.move_selection(GRID2, 3, "left"), 2)
        self.assertEqual(nav.move_selection(GRID2, 3, "up"), 1)


class TestMoveGrid3FromCenter(unittest.TestCase):
    def test_up(self):
        self.assertEqual(nav.move_selection(GRID3, 4, "up"), 1)

    def test_down(self):
        self.assertEqual(nav.move_selection(GRID3, 4, "down"), 7)

    def test_left(self):
        self.assertEqual(nav.move_selection(GRID3, 4, "left"), 3)

    def test_right(self):
        self.assertEqual(nav.move_selection(GRID3, 4, "right"), 5)


class TestMoveBoundaries(unittest.TestCase):
    def test_edges_stay_put(self):
        self.assertEqual(nav.move_selection(GRID3, 0, "left"), 0)
        self.assertEqual(nav.move_selection(GRID3, 0, "up"), 0)
        self.assertEqual(nav.move_selection(GRID3, 8, "right"), 8)
        self.assertEqual(nav.move_selection(GRID3, 8, "down"), 8)

    def test_grid2_corners_stay_put(self):
        self.assertEqual(nav.move_selection(GRID2, 0, "left"), 0)
        self.assertEqual(nav.move_selection(GRID2, 0, "up"), 0)
        self.assertEqual(nav.move_selection(GRID2, 3, "right"), 3)
        self.assertEqual(nav.move_selection(GRID2, 3, "down"), 3)

    def test_unknown_direction_changes_nothing(self):
        self.assertEqual(nav.move_selection(GRID2, 2, "вбок"), 2)

    def test_empty_list_returns_none(self):
        self.assertIsNone(nav.move_selection([], None, "right"))


class TestMoveDifferentGeometry(unittest.TestCase):
    def test_prefers_same_row_neighbour(self):
        # Слева — низкая зона; справа по той же строке сосед и дальше внизу ещё одна.
        left = Rect(0, 0, 0.3, 0.4)
        same_row = Rect(0.3, 0, 0.3, 0.4)
        lower = Rect(0.5, 0.6, 0.3, 0.4)
        zones = [left, same_row, lower]
        self.assertEqual(nav.move_selection(zones, 0, "right"), 1)

    def test_wide_zone_reaches_thin_strip(self):
        # Из широкой зоны «вправо» — тонкая полоса справа, а не широкая слева.
        wide = Rect(0, 0, 0.9, 1.0)
        strip = Rect(0.9, 0, 0.1, 1.0)
        zones = [wide, strip]
        self.assertEqual(nav.move_selection(zones, 0, "right"), 1)
        self.assertEqual(nav.move_selection(zones, 1, "left"), 0)

    def test_picks_only_candidate_in_direction_without_overlap(self):
        near = Rect(0, 0, 0.2, 0.2)
        far_down = Rect(0.5, 0.9, 0.2, 0.1)
        zones = [near, far_down]
        self.assertEqual(nav.move_selection(zones, 0, "right"), 1)
        self.assertEqual(nav.move_selection(zones, 1, "left"), 0)

    def test_large_zone_center_right_of_small_left_zone(self):
        # Сосед крупнее и его центр правее — движение вправо работает.
        small = Rect(0, 0, 0.2, 1.0)
        big = Rect(0.2, 0, 0.7, 1.0)
        self.assertEqual(nav.move_selection([small, big], 0, "right"), 1)


class TestMoveRealLayout(unittest.TestCase):
    """Раскладка из zones.conf: широкая main, полоса справа и зона bottom."""

    def setUp(self):
        self.main = Rect(0, 0, 0.8358, 1.0)
        self.top_right = Rect(0.8358, 0, 0.1642, 0.5)
        self.top_far = Rect(0.8358, 0.5, 0.1642, 0.5)
        self.bottom = Rect(0.7129, 0.8068, 0.2871, 0.1932)
        self.zones = [self.main, self.top_right, self.top_far, self.bottom]

    def test_right_from_main_is_the_right_strip_not_the_bottom(self):
        # Центр bottom ближе по X, но он снизу — «вправо» должно вести в полосу.
        self.assertEqual(nav.move_selection(self.zones, 0, "right"), 1)

    def test_down_from_main_is_bottom(self):
        self.assertEqual(nav.move_selection(self.zones, 0, "down"), 3)


class TestZoneAt(unittest.TestCase):
    def test_inside_zones(self):
        self.assertEqual(nav.zone_at(GRID2, 0.25, 0.25), 0)
        self.assertEqual(nav.zone_at(GRID2, 0.75, 0.75), 3)

    def test_gap_returns_none(self):
        zones = [Rect(0, 0, 0.4, 1.0), Rect(0.6, 0, 0.4, 1.0)]
        self.assertIsNone(nav.zone_at(zones, 0.5, 0.5))

    def test_overlap_returns_last_drawn(self):
        zones = [Rect(0, 0, 1.0, 1.0), Rect(0, 0, 0.5, 0.5)]
        self.assertEqual(nav.zone_at(zones, 0.25, 0.25), 1)

    def test_empty_list(self):
        self.assertIsNone(nav.zone_at([], 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()

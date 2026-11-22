#!/usr/bin/env python3
"""Тесты `zone_units` — перевода логических пикселей GTK в физические X.

Модуль чистый, поэтому X-сервер и GTK тут не нужны: проверяется арифметика,
которой пользуются раскладчик, демон, пикер, HUD и полоса вкладок. Главное
свойство — **соседние зоны остаются сомкнутыми** после перевода: расхождение на
пиксель между ними и было бы «съехавшей сеткой».
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zone_units as zu  # noqa: E402


class TestScaleValue(unittest.TestCase):
    def test_normalises_garbage_to_one(self):
        for value in (None, "", "два", 0, -2, 0.5):
            self.assertEqual(zu.scale_value(value), 1, value)

    def test_keeps_integer_scale(self):
        self.assertEqual(zu.scale_value(2), 2)
        self.assertEqual(zu.scale_value("3"), 3)


class TestPhysicalRect(unittest.TestCase):
    def test_scale_two_multiplies_the_workarea(self):
        # ровно случай из замера: GDK видит 960x540, X — 1920x1080
        self.assertEqual(zu.physical_rect((0, 0, 960, 540), 2), (0, 0, 1920, 1080))

    def test_scale_one_changes_nothing(self):
        self.assertEqual(zu.physical_rect((0, 40, 1920, 1040), 1), (0, 40, 1920, 1040))

    def test_monitor_offset_is_scaled_too(self):
        # второй экран справа: GDK 960 логических, X 1920 физических
        self.assertEqual(
            zu.physical_rect((960, 0, 960, 540), 2), (1920, 0, 1920, 1080)
        )

    def test_neighbours_stay_flush_when_scale_does_not_divide(self):
        # ширина 1001 при масштабе 2: сумма ширин равна ширине целого
        left = zu.physical_rect((0, 0, 500, 10), 2)
        right = zu.physical_rect((500, 0, 501, 10), 2)
        self.assertEqual(left[0] + left[2], right[0])
        self.assertEqual(left[2] + right[2], 2002)

    def test_gap_between_zones_does_not_appear(self):
        # доли соседних зон стыкуются: 0.5 и 0.5 от нечётной ширины
        a = zu.physical_rect((0, 0, 801, 600), 2)
        b = zu.physical_rect((801, 0, 799, 600), 2)
        self.assertEqual(a[0] + a[2], b[0])


class TestLogicalRect(unittest.TestCase):
    def test_round_trip_is_stable(self):
        for rect, scale in (
            ((0, 0, 960, 540), 2),
            ((960, 0, 960, 540), 2),
            ((0, 40, 1920, 1040), 1),
            ((-1920, 0, 1920, 1080), 2),
        ):
            self.assertEqual(zu.logical_rect(zu.physical_rect(rect, scale), scale), rect)

    def test_physical_is_divided(self):
        self.assertEqual(zu.logical_rect((1920, 0, 1920, 1080), 2), (960, 0, 960, 540))


class TestPoints(unittest.TestCase):
    def test_point_scales_both_ways(self):
        self.assertEqual(zu.physical_point((100, 50), 2), (200, 100))
        self.assertEqual(zu.logical_point((200, 100), 2), (100, 50))

    def test_scale_one_keeps_point(self):
        self.assertEqual(zu.physical_point((7, 9), 1), (7, 9))
        self.assertEqual(zu.logical_point((7, 9), 1), (7, 9))


class TestScaleAt(unittest.TestCase):
    def test_known_index(self):
        self.assertEqual(zu.scale_at([2, 3], 1), 3)

    def test_missing_index_is_one(self):
        for index in (-1, 5):
            self.assertEqual(zu.scale_at([2], index), 1)
        self.assertEqual(zu.scale_at([], 0), 1)
        self.assertEqual(zu.scale_at(None, 0), 1)


class TestScaleForPoint(unittest.TestCase):
    areas = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]

    def test_containing_monitor_wins(self):
        self.assertEqual(zu.scale_for_point(self.areas, [1, 2], (100, 100)), 1)
        self.assertEqual(zu.scale_for_point(self.areas, [1, 2], (2500, 500)), 2)

    def test_point_outside_every_area_takes_nearest_centre(self):
        # точка далеко справа: ближе центр правого монитора
        self.assertEqual(zu.scale_for_point(self.areas, [1, 2], (5000, 500)), 2)
        self.assertEqual(zu.scale_for_point(self.areas, [1, 2], (-5000, 500)), 1)

    def test_no_areas_means_no_scale(self):
        self.assertEqual(zu.scale_for_point([], [], (10, 10)), 1)

    def test_short_scale_list_falls_back_to_one(self):
        self.assertEqual(zu.scale_for_point(self.areas, [2], (2500, 500)), 1)


if __name__ == "__main__":
    unittest.main()

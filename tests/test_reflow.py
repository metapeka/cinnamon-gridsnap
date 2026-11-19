#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты reflow GridSnap — чистая геометрия, без X-сервера.

Проверяют `zone_reflow.reflow(zones, freed, occupied)`: расширение соседа на
освободившуюся зону, отказ при несовпадении по другой оси, занятый/неизвестный
`freed`, выбор кандидата по длине общего ребра и разрешение ничьей по имени.

Запуск:
    python3 tests/test_reflow.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_reflow  # noqa: E402


class TestReflow(unittest.TestCase):
    def test_right_neighbor_expands_left(self):
        zones = {
            "left": (0.0, 0.0, 0.5, 1.0),
            "right": (0.5, 0.0, 0.5, 1.0),
        }
        self.assertEqual(
            zone_reflow.reflow(zones, "left", {"right"}),
            {"right": (0.0, 0.0, 1.0, 1.0)},
        )

    def test_bottom_neighbor_expands_up(self):
        zones = {
            "top": (0.0, 0.0, 1.0, 0.5),
            "bottom": (0.0, 0.5, 1.0, 0.5),
        }
        self.assertEqual(
            zone_reflow.reflow(zones, "top", {"bottom"}),
            {"bottom": (0.0, 0.0, 1.0, 1.0)},
        )

    def test_top_neighbor_expands_down(self):
        zones = {
            "top": (0.0, 0.0, 1.0, 0.5),
            "bottom": (0.0, 0.5, 1.0, 0.5),
        }
        self.assertEqual(
            zone_reflow.reflow(zones, "bottom", {"top"}),
            {"top": (0.0, 0.0, 1.0, 1.0)},
        )

    def test_mismatch_on_other_axis_keeps_nothing(self):
        # x и w совпадают, но y-диапазоны не стыкуются: объединение не прямоугольник.
        zones = {
            "freed": (0.0, 0.0, 0.5, 0.5),
            "cand": (0.0, 0.25, 0.5, 0.5),
        }
        self.assertEqual(zone_reflow.reflow(zones, "freed", {"cand"}), {})

    def test_partial_edge_mismatch_h_keeps_nothing(self):
        # Общая вертикальная сторона есть, но h различаются -> не прямоугольник.
        zones = {
            "freed": (0.0, 0.0, 0.5, 0.5),
            "cand": (0.5, 0.0, 0.5, 0.25),
        }
        self.assertEqual(zone_reflow.reflow(zones, "freed", {"cand"}), {})

    def test_freed_occupied_is_noop(self):
        zones = {
            "freed": (0.0, 0.0, 0.5, 1.0),
            "right": (0.5, 0.0, 0.5, 1.0),
        }
        self.assertEqual(zone_reflow.reflow(zones, "freed", {"freed", "right"}), {})

    def test_freed_unknown_is_noop(self):
        zones = {"right": (0.5, 0.0, 0.5, 1.0)}
        self.assertEqual(zone_reflow.reflow(zones, "ghost", {"right"}), {})

    def test_no_candidates_is_noop(self):
        zones = {
            "freed": (0.0, 0.0, 0.5, 0.5),
            "far": (0.6, 0.6, 0.4, 0.4),
        }
        self.assertEqual(zone_reflow.reflow(zones, "freed", {"far"}), {})

    def test_longest_shared_edge_wins(self):
        # freed w=0.5, h=0.25. Снизу сосед с общим ребром 0.5 (по w), справа —
        # 0.25 (по h). Побеждает нижний.
        zones = {
            "freed": (0.0, 0.0, 0.5, 0.25),
            "below": (0.0, 0.25, 0.5, 0.25),
            "right": (0.5, 0.0, 0.5, 0.25),
        }
        self.assertEqual(
            zone_reflow.reflow(zones, "freed", {"below", "right"}),
            {"below": (0.0, 0.0, 0.5, 0.5)},
        )

    def test_tie_broken_by_name(self):
        # Оба кандидата делят ребро длины 0.5; побеждает "alpha".
        zones = {
            "mid": (0.0, 0.0, 0.5, 0.5),
            "zeta": (0.5, 0.0, 0.5, 0.5),
            "alpha": (0.0, 0.5, 0.5, 0.5),
        }
        self.assertEqual(
            zone_reflow.reflow(zones, "mid", {"zeta", "alpha"}),
            {"alpha": (0.0, 0.0, 0.5, 1.0)},
        )

    def test_tie_is_deterministic_for_set_order(self):
        # Итерируем occupied в обоих порядках — результат один и тот же.
        zones = {
            "mid": (0.0, 0.0, 0.5, 0.5),
            "beta": (0.5, 0.0, 0.5, 0.5),
            "alpha": (0.0, 0.5, 0.5, 0.5),
        }
        expected = {"alpha": (0.0, 0.0, 0.5, 1.0)}
        self.assertEqual(zone_reflow.reflow(zones, "mid", {"alpha", "beta"}), expected)
        self.assertEqual(zone_reflow.reflow(zones, "mid", {"beta", "alpha"}), expected)

    def test_tolerance_accepts_near_touching_edges(self):
        # Разрыв 5e-10 < EPS=1e-9: соседи всё ещё стыкуются.
        eps = 5e-10
        zones = {
            "left": (0.0, 0.0, 0.5, 1.0),
            "right": (0.5 + eps, 0.0, 0.5, 1.0),
        }
        result = zone_reflow.reflow(zones, "left", {"right"})
        self.assertIn("right", result)
        x, y, w, h = result["right"]
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0)
        self.assertAlmostEqual(w, 1.0 + eps)
        self.assertAlmostEqual(h, 1.0)

    def test_occupied_zone_without_geometry_is_ignored(self):
        # Имя занято, но геометрии нет — кандидатом быть не может.
        zones = {"freed": (0.0, 0.0, 0.5, 1.0)}
        self.assertEqual(zone_reflow.reflow(zones, "freed", {"ghost"}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

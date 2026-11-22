#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты чистого ядра вкладок в зоне — без X-сервера и без GTK.

Проверяют `zone_tabs`: геометрию полосы и окна-вкладки (в т.ч. узкую зону, где
полосы нет), выбор окон зоны с допуском рамок, кольцо переключения и оба
протокола — строку снимка для полосы и запросы сокета демона. Модуль не тянет за
собой ни GTK, ни X, поэтому тесты идут в обычном CI.

Запуск:
    python3 tests/test_tabs.py
    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_tabs as tabs  # noqa: E402

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

# Зона на весь левый монитор и та же зона, которой полосы уже не хватает.
ZONE = (0, 0, 960, 1080)
NARROW = (0, 0, 960, tabs.STRIP_HEIGHT + tabs.MIN_WINDOW_H - 1)
FITS = (0, 0, 960, tabs.STRIP_HEIGHT + tabs.MIN_WINDOW_H)


class TestStripRect(unittest.TestCase):
    def test_fits_on_top_of_zone(self):
        self.assertEqual(tabs.strip_rect(ZONE), (0.0, 0.0, 960.0, float(tabs.STRIP_HEIGHT)))

    def test_exact_boundary_still_fits(self):
        self.assertIsNotNone(tabs.strip_rect(FITS))

    def test_one_pixel_short_does_not_fit(self):
        self.assertIsNone(tabs.strip_rect(NARROW))

    def test_tiny_zone_has_no_strip(self):
        self.assertIsNone(tabs.strip_rect((0, 0, 200, 30)))

    def test_custom_height_and_minimum(self):
        # 30 - 10 = 20 — ровно минимум, полоса влезает.
        self.assertEqual(tabs.strip_rect((5, 7, 100, 30), height=10, min_window_h=20),
                         (5.0, 7.0, 100.0, 10.0))
        self.assertIsNone(tabs.strip_rect((5, 7, 100, 29), height=10, min_window_h=20))

    def test_fractional_rect_accepted(self):
        self.assertEqual(tabs.strip_rect((0.5, 0.5, 10.5, 100)), (0.5, 0.5, 10.5, float(tabs.STRIP_HEIGHT)))


class TestTabRect(unittest.TestCase):
    def test_window_moves_down_by_strip_height(self):
        x, y, w, h = tabs.tab_rect(ZONE)
        self.assertEqual((x, y, w, h), (0.0, float(tabs.STRIP_HEIGHT), 960.0, 1080.0 - tabs.STRIP_HEIGHT))

    def test_without_strip_window_keeps_whole_zone(self):
        self.assertEqual(tabs.tab_rect(NARROW), (0.0, 0.0, 960.0, float(NARROW[3])))

    def test_strip_and_window_tile_the_zone_exactly(self):
        for height in range(8, 40):
            zone = (10, 20, 300, 400)
            strip = tabs.strip_rect(zone, height=height)
            window = tabs.tab_rect(zone, height=height)
            self.assertIsNotNone(strip)
            self.assertEqual(strip[1], zone[1])
            self.assertEqual(strip[3], window[1] - zone[1])
            self.assertEqual(window[1] + window[3], zone[1] + zone[3])
            self.assertEqual(window[2], zone[2])

    def test_narrow_zone_gives_no_strip_and_no_offset(self):
        self.assertIsNone(tabs.strip_rect(NARROW))
        self.assertEqual(tabs.tab_rect(NARROW), (0.0, 0.0, 960.0, float(NARROW[3])))


class TestTabsOfZone(unittest.TestCase):
    def test_keeps_only_windows_on_the_tab_frame(self):
        window = tabs.tab_rect(ZONE)
        frames = {
            11: window,
            22: (0, 0, 400, 400),
            33: window,
        }
        self.assertEqual(tabs.tabs_of_zone(frames, window), [11, 33])

    def test_order_is_bottom_to_top_of_client_list(self):
        window = tabs.tab_rect(ZONE)
        frames = {7: window, 3: window, 5: window}
        self.assertEqual(tabs.tabs_of_zone(frames, window), [7, 3, 5])

    def test_tolerance_bounds(self):
        window = tabs.tab_rect(ZONE)
        near = (window[0] + tabs.FRAME_TOLERANCE, window[1], window[2], window[3])
        far = (window[0] + tabs.FRAME_TOLERANCE + 1, window[1], window[2], window[3])
        self.assertEqual(tabs.tabs_of_zone({1: near}, window), [1])
        self.assertEqual(tabs.tabs_of_zone({1: far}, window), [])

    def test_unreadable_frame_is_skipped(self):
        window = tabs.tab_rect(ZONE)
        self.assertEqual(tabs.tabs_of_zone({1: None, 2: window}, window), [2])

    def test_empty(self):
        self.assertEqual(tabs.tabs_of_zone({}, tabs.tab_rect(ZONE)), [])

    def test_single_window_on_the_whole_zone_is_not_a_stack(self):
        # Правило дизайна: одно окно занимает зону целиком (рамка — сама зона),
        # поэтому стопкой оно не считается — полоса появляется со второго окна.
        # Демон держит одиночное окно именно на `zone_rect`, а не на `tab_rect`.
        self.assertEqual(tabs.tabs_of_zone({11: ZONE}, tabs.tab_rect(ZONE)), [])
        self.assertEqual(tabs.tabs_of_zone({11: ZONE, 22: tabs.tab_rect(ZONE)}, tabs.tab_rect(ZONE)), [22])


class TestNextTab(unittest.TestCase):
    def test_wraps_around_the_ring(self):
        self.assertEqual(tabs.next_tab([1, 2, 3], 2), 3)
        self.assertEqual(tabs.next_tab([1, 2, 3], 3), 1)

    def test_unknown_current_starts_from_bottom(self):
        self.assertEqual(tabs.next_tab([1, 2, 3], 99), 1)
        self.assertEqual(tabs.next_tab([1, 2, 3], None), 1)

    def test_single_window_has_nothing_to_switch(self):
        self.assertIsNone(tabs.next_tab([1], 1))

    def test_two_tab_ring_goes_both_ways(self):
        # В стопке из двух окон «следующее» — это единственное другое.
        self.assertEqual(tabs.next_tab([1, 2], 1), 2)
        self.assertEqual(tabs.next_tab([1, 2], 2), 1)

    def test_empty_has_nothing_to_switch(self):
        self.assertIsNone(tabs.next_tab([], None))


class TestStripRecord(unittest.TestCase):
    def test_two_tabs_give_strip_on_top_with_active_flag(self):
        record = tabs.strip_record("main", ZONE, [(11, "Firefox"), (22, "Терминал")], active=22)
        self.assertEqual(record["zone"], "main")
        self.assertEqual(record["rect"], [0, 0, 960, tabs.STRIP_HEIGHT])
        self.assertEqual(record["tabs"], [
            {"window": 11, "title": "Firefox", "active": False},
            {"window": 22, "title": "Терминал", "active": True},
        ])

    def test_single_tab_has_no_strip(self):
        self.assertIsNone(tabs.strip_record("main", ZONE, [(11, "Firefox")], active=11))

    def test_tab_order_bottom_to_top_is_kept(self):
        record = tabs.strip_record(
            "main", ZONE, [(11, "A"), (22, "B"), (33, "C")], active=22
        )
        self.assertEqual([tab["window"] for tab in record["tabs"]], [11, 22, 33])
        self.assertEqual([tab["active"] for tab in record["tabs"]], [False, True, False])

    def test_narrow_zone_has_no_strip(self):
        self.assertIsNone(tabs.strip_record("main", NARROW, [(11, "A"), (22, "B")], active=11))

    def test_no_tabs_has_no_strip(self):
        self.assertIsNone(tabs.strip_record("main", ZONE, [], active=None))


class TestStripsPayload(unittest.TestCase):
    def test_is_one_json_line_with_newline(self):
        payload = tabs.strips_payload([tabs.strip_record("main", ZONE, [(1, "A"), (2, "B")], active=2)])
        self.assertTrue(payload.endswith("\n"))
        self.assertEqual(payload.count("\n"), 1)
        self.assertEqual(json.loads(payload)["strips"][0]["zone"], "main")

    def test_cyrillic_titles_stay_readable(self):
        payload = tabs.strips_payload([tabs.strip_record("main", ZONE, [(1, "Терминал"), (2, "Почта")], active=1)])
        self.assertIn("Терминал", payload)

    def test_empty_records_are_dropped(self):
        payload = tabs.strips_payload([None, tabs.strip_record("main", ZONE, [(1, "A"), (2, "B")]), None])
        self.assertEqual(len(json.loads(payload)["strips"]), 1)

    def test_nothing_to_draw(self):
        self.assertEqual(json.loads(tabs.strips_payload([])), {"strips": []})


class TestCommands(unittest.TestCase):
    def test_switch_command_names_zone_and_window(self):
        self.assertEqual(tabs.tabs_switch_command("main", 26),
                         {"cmd": "tabs-switch", "zone": "main", "window": 26})

    def test_next_command_has_no_arguments(self):
        self.assertEqual(tabs.tabs_next_command(), {"cmd": "tabs-next"})

    def test_command_names_are_the_frozen_constants(self):
        self.assertEqual(tabs.TABS_SWITCH, "tabs-switch")
        self.assertEqual(tabs.TABS_NEXT, "tabs-next")


class TestStackRect(unittest.TestCase):
    def test_single_tab_keeps_the_whole_zone(self):
        self.assertEqual(tabs.stack_rect(ZONE, 1), (0.0, 0.0, 960.0, 1080.0))
        self.assertEqual(tabs.stack_rect(ZONE, 0), tabs.stack_rect(ZONE, 1))

    def test_two_tabs_get_the_tab_frame(self):
        self.assertEqual(tabs.stack_rect(ZONE, 2), tabs.tab_rect(ZONE))
        self.assertEqual(tabs.stack_rect(ZONE, 7), tabs.tab_rect(ZONE))

    def test_narrow_zone_is_the_same_rect_for_any_count(self):
        # Полоса не влезает — рамка вкладки совпадает с зоной, и «сжатия» не происходит.
        self.assertEqual(tabs.stack_rect(NARROW, 1), tabs.stack_rect(NARROW, 3))


class TestDefaults(unittest.TestCase):
    """Умолчания полосы: их читает и человек, и ``[tabs] height`` из конфига."""

    def test_defaults_are_the_documented_values(self):
        self.assertEqual(tabs.STRIP_HEIGHT, 22)
        self.assertEqual(tabs.MIN_WINDOW_H, 44)
        self.assertEqual(tabs.FRAME_TOLERANCE, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)

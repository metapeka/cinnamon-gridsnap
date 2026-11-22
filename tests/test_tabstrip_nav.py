#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты чистой полосы вкладок — без X-сервера и без GTK.

Проверяют `zone_tabstrip_nav`: разбор построчного снимка от демона (снимок,
пустой снимок и мусор — три разных ответа), рамки вкладок по ширине полосы
(поровну, последняя забирает остаток, ничего не вылезает за зону), подпись под
ширину вкладки и вкладку под точкой клика. Модуль не тянет GTK, поэтому тесты
идут в обычном CI, где `python3-gi` не установлен.

Запуск:
    python3 tests/test_tabstrip_nav.py
    python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import json
import math
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_tabstrip_nav as nav  # noqa: E402

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

# Полоса одной зоны: две вкладки, нижняя неактивна.
RECORD = {
    "zone": "main",
    "rect": [0, 0, 200, 22],
    "tabs": [
        {"window": 0x11, "title": "Terminal", "active": False},
        {"window": 0x22, "title": "Firefox — очень длинный заголовок", "active": True},
    ],
}
SNAPSHOT = {"strips": [RECORD]}


def line(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


class TestRecordRect(unittest.TestCase):
    def test_list_and_tuple(self):
        self.assertEqual(nav.record_rect({"rect": [1, 2, 3, 4]}), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(nav.record_rect({"rect": (1, 2, 3, 4)}), (1.0, 2.0, 3.0, 4.0))

    def test_zero_or_negative_size_is_not_drawable(self):
        self.assertIsNone(nav.record_rect({"rect": [0, 0, 0, 22]}))
        self.assertIsNone(nav.record_rect({"rect": [0, 0, 100, 0]}))
        self.assertIsNone(nav.record_rect({"rect": [0, 0, -10, 22]}))

    def test_garbage_is_not_drawable(self):
        for value in (None, {}, "rect", {"rect": None}, {"rect": [1, 2, 3]},
                      {"rect": [1, 2, 3, "x"]}, {"rect": [1, 2, math.nan, 4]},
                      {"rect": [1, 2, math.inf, 4]}):
            with self.subTest(value=value):
                self.assertIsNone(nav.record_rect(value))


class TestParseSnapshot(unittest.TestCase):
    def test_strips_are_returned_in_order(self):
        second = {"zone": "side", "rect": [300, 0, 100, 22],
                  "tabs": [{"window": 1, "title": "A"}, {"window": 2, "title": "B"}]}
        parsed = nav.parse_snapshot(line({"strips": [RECORD, second]}))
        self.assertEqual([record["zone"] for record in parsed], ["main", "side"])
        self.assertEqual(parsed[0]["tabs"][1]["window"], 0x22)

    def test_bytes_are_decoded(self):
        parsed = nav.parse_snapshot(line(SNAPSHOT).encode("utf-8"))
        self.assertEqual(len(parsed), 1)

    def test_empty_snapshot_is_a_list(self):
        # Пустой снимок — законная команда «убрать все полосы», а не мусор.
        self.assertEqual(nav.parse_snapshot('{"strips": []}'), [])

    def test_garbage_is_none(self):
        # Не разобрали строку — состояние не меняем (полоса не исчезает).
        for value in ("", "   ", "{", "[]", "null", '{"strips": 1}', b"\xff\xfe"):
            with self.subTest(value=value):
                self.assertIsNone(nav.parse_snapshot(value))

    def test_records_without_tabs_or_rect_are_dropped(self):
        payload = {
            "strips": [
                RECORD,
                {"zone": "a", "rect": [0, 0, 10, 10], "tabs": []},
                {"zone": "b", "rect": [0, 0, 10, 10]},
                {"zone": "c", "rect": [0, 0, 0, 10], "tabs": [{"window": 1}]},
                {"zone": "d", "rect": [0, 0, 10, 10],
                 "tabs": ["не вкладка", {"window": 2, "title": "ok"}]},
            ]
        }
        parsed = nav.parse_snapshot(line(payload))
        self.assertEqual([record["zone"] for record in parsed], ["main", "d"])
        self.assertEqual(len(nav.layout(parsed[1])), 1)


class TestTabFrames(unittest.TestCase):
    def test_no_tabs_no_frames(self):
        self.assertEqual(nav.tab_frames((0, 0, 200, 22), 0), [])
        self.assertEqual(nav.tab_frames((0, 0, 200, 22), -2), [])

    def test_one_tab_fills_the_strip(self):
        self.assertEqual(nav.tab_frames((5, 1, 200, 22), 1), [(5.0, 1.0, 200.0, 22.0)])

    def test_two_tabs_split_equally_and_keep_gap(self):
        first, second = nav.tab_frames((0, 0, 200, 22), 2, gap=2)
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], first[2] + 2)
        self.assertEqual(second[0] + second[2], 200)

    def test_last_tab_takes_remainder_of_division(self):
        frames = nav.tab_frames((0, 0, 103, 22), 3, gap=1)
        self.assertEqual(frames[-1][0] + frames[-1][2], 103)

    def test_frames_never_leave_the_strip(self):
        rect = (10, 5, 100, 22)
        frames = nav.tab_frames(rect, 4, gap=3)
        self.assertEqual(frames[0][0], 10)
        self.assertLessEqual(frames[-1][0] + frames[-1][2], 110)
        for x, y, w, _h in frames:
            self.assertGreaterEqual(x, rect[0])
            self.assertGreater(w, 0)
            self.assertEqual(y, rect[1])

    def test_strip_narrower_than_gaps_drops_gaps(self):
        frames = nav.tab_frames((0, 0, 2, 22), 4, gap=3)
        self.assertEqual(len(frames), 4)
        self.assertEqual(frames[-1][0] + frames[-1][2], 2)
        self.assertTrue(all(w > 0 for _x, _y, w, _h in frames))

    def test_bad_rect_or_size(self):
        for rect in (None, "rect", (0, 0), (0, 0, 0, 22), (0, 0, 100, -1),
                     (0, 0, math.nan, 22)):
            with self.subTest(rect=rect):
                self.assertEqual(nav.tab_frames(rect, 2), [])


class TestFitTitle(unittest.TestCase):
    def test_short_title_fits_as_is(self):
        self.assertEqual(nav.fit_title("Firefox", 100, char_width=7), "Firefox")

    def test_long_title_is_cut_with_ellipsis(self):
        title = "Очень длинный заголовок окна браузера"
        fitted = nav.fit_title(title, 7 * 10, char_width=7)
        self.assertLessEqual(len(fitted), 10)
        self.assertTrue(fitted.endswith(nav.ELLIPSIS), fitted)
        self.assertTrue(title.startswith(fitted[: -len(nav.ELLIPSIS)]))

    def test_tiny_width_gives_empty_label(self):
        for width in (0, -5, 4):
            with self.subTest(width=width):
                self.assertEqual(nav.fit_title("Firefox", width, char_width=7), "")

    def test_one_char_room_is_ellipsis_only(self):
        self.assertEqual(nav.fit_title("Firefox", 7, char_width=7), nav.ELLIPSIS)

    def test_none_title_and_zero_char_width(self):
        self.assertEqual(nav.fit_title(None, 100), "")
        self.assertEqual(nav.fit_title("Firefox", 100, char_width=0), "")

    def test_label_width_never_negative(self):
        self.assertEqual(nav.label_width((0, 0, 200, 22)), 200 - 2 * nav.TAB_PADDING)
        self.assertEqual(nav.label_width((0, 0, 2, 22)), 0)


class TestLayout(unittest.TestCase):
    def test_rows_have_labels_active_and_frames(self):
        rows = nav.layout(RECORD)
        self.assertEqual([row["window"] for row in rows], [0x11, 0x22])
        self.assertEqual([row["active"] for row in rows], [False, True])
        self.assertTrue(all(row["label"] for row in rows))
        self.assertEqual(rows[0]["rect"], nav.tab_frames(RECORD["rect"], 2)[0])

    def test_bad_record_gives_nothing_to_draw(self):
        for record in (None, {}, {"rect": [0, 0, 100, 22], "tabs": []}):
            with self.subTest(record=record):
                self.assertEqual(nav.layout(record), [])


class TestTabAt(unittest.TestCase):
    FRAMES = [(0, 0, 50, 22), (51, 0, 49, 22)]

    def test_inside_first_and_second(self):
        self.assertEqual(nav.tab_at(self.FRAMES, (10, 10)), 0)
        self.assertEqual(nav.tab_at(self.FRAMES, (60, 10)), 1)

    def test_left_edge_belongs_to_tab_right_edge_does_not(self):
        self.assertEqual(nav.tab_at(self.FRAMES, (0, 0)), 0)
        # Правый край первой вкладки — уже зазор, а не вкладка.
        self.assertIsNone(nav.tab_at(self.FRAMES, (50, 10)))

    def test_outside_and_bad_point(self):
        self.assertIsNone(nav.tab_at(self.FRAMES, (500, 10)))
        self.assertIsNone(nav.tab_at(self.FRAMES, (10, 30)))
        self.assertIsNone(nav.tab_at(self.FRAMES, None))
        self.assertIsNone(nav.tab_at(self.FRAMES, ("x", 10)))
        self.assertIsNone(nav.tab_at([], (10, 10)))

    def test_tab_under_returns_window(self):
        frames = nav.tab_frames(RECORD["rect"], 2)
        middle = (frames[1][0] + frames[1][2] / 2, 10)
        self.assertEqual(nav.tab_under(RECORD, middle), 0x22)
        self.assertIsNone(nav.tab_under(RECORD, (0, 500)))
        self.assertIsNone(nav.tab_under(None, middle))


def load_tabstrip():
    """`zone-tabstrip.py` — файл без `.py`-модуля: `SourceFileLoader` кажет его без запуска GTK."""
    loader = importlib.machinery.SourceFileLoader(
        "zone_tabstrip_feed_test", os.path.join(ROOT, "zone-tabstrip.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeStrip:
    """Подменяет GTK-окно полосы: запоминает снимки, факт удаления и забывает себя."""

    def __init__(self, zone, owner):
        self.zone = zone
        self.owner = owner
        self.win = object()
        self.records = []
        self.destroyed = 0

    def show_record(self, record):
        if self.win is None:
            return
        self.records.append(record)

    def destroy(self):
        self.destroyed += 1
        win, self.win = self.win, None
        if win is not None:
            self.owner.forget(self)


class TestTabstripFeed(unittest.TestCase):
    """Разбор stdin процесса `zone-tabstrip.py`: строки, мусор, снимок и EOF.

    GTK не нужен: окно полосы подменено `FakeStrip`, проверяется только
    буферизация строк и применение полного снимка.
    """

    def setUp(self):
        self.mod = load_tabstrip()
        self.addCleanup(sys.modules.pop, "zone_tabstrip_feed_test", None)
        patcher = mock.patch.object(self.mod, "Strip", FakeStrip)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.strip = self.mod.Tabstrip()

    def feed(self, text):
        self.strip.feed(text)

    def test_partial_line_waits_for_newline(self):
        text = line(SNAPSHOT)
        self.feed(text[:-5])
        self.assertEqual(self.strip.strips, {})
        self.feed(text[-5:])
        self.assertEqual(list(self.strip.strips), ["main"])

    def test_garbage_line_keeps_previous_strips(self):
        self.feed(line(SNAPSHOT))
        self.feed("это не JSON\n")
        self.assertEqual(list(self.strip.strips), ["main"])
        self.assertEqual(self.strip.strips["main"].destroyed, 0)

    def test_second_snapshot_updates_the_same_strip(self):
        self.feed(line(SNAPSHOT))
        changed = dict(RECORD, rect=[0, 50, 300, 30])
        self.feed(line({"strips": [changed]}))
        self.assertEqual(list(self.strip.strips), ["main"])
        self.assertEqual(
            [record["rect"] for record in self.strip.strips["main"].records],
            [[0, 0, 200, 22], [0, 50, 300, 30]],
        )

    def test_empty_snapshot_removes_all_strips(self):
        self.feed(line(SNAPSHOT))
        strip = self.strip.strips["main"]
        self.feed('{"strips": []}\n')
        self.assertEqual(self.strip.strips, {})
        self.assertEqual(strip.destroyed, 1)

    def test_disappeared_zone_removes_only_that_strip(self):
        second = dict(RECORD, zone="side", rect=[300, 0, 100, 22])
        self.feed(line({"strips": [RECORD, second]}))
        gone = self.strip.strips["side"]
        self.feed(line(SNAPSHOT))
        self.assertEqual(list(self.strip.strips), ["main"])
        self.assertEqual(gone.destroyed, 1)

    def test_close_removes_every_strip(self):
        second = dict(RECORD, zone="side", rect=[300, 0, 100, 22])
        self.feed(line({"strips": [RECORD, second]}))
        strips = list(self.strip.strips.values())
        self.strip.close()
        self.assertEqual(self.strip.strips, {})
        self.assertEqual([strip.destroyed for strip in strips], [1, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)

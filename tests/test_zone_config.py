#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты `zone_config` — чистого слоя конфига редактора зон.

Без X-сервера и без GTK: `zone_config` намеренно не импортирует gi, поэтому
тесты идут в CI, где `python3-gi` не установлен. Проверяют парсинг обеих форм
зон, канонический вывод `x,y,w,h`, секцию `[keys]` и — главное — round-trip
чужих секций `[rules]`/`[daemon]` при сохранении из редактора.

Запуск:
    python3 tests/test_zone_config.py
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402


BASE_GRID = "[grid]\ncols = 1.0\nrows = 1.0\n"
RECT_ZONES = "[zones]\nmain = 0, 0, 0.5, 1\ntop-right = 0.5, 0, 0.5, 0.5\n"
GRID_ZONES = (
    "[grid]\ncols = 0.25, 0.5, 1.0\nrows = 0.5, 1.0\n"
    "[zones]\nleft = 0:1, 0:1\nright = 2:3, 0:1\n"
)
RULES = "[rules]\nChrome = main\nre:^(Navigator|firefox) = top-right\n"
DAEMON = "[daemon]\nreflow = off   # off | neighbors\n"


class ConfMixin:
    """Пишет временный zones.conf и убирает его после теста."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()


def sections_in(text):
    """Имена секций в файле по порядку (только заголовки строк)."""
    return [line.strip()[1:-1] for line in text.splitlines() if line.strip().startswith("[")]


def section_index(text, name):
    """Номер строки с заголовком ``[name]`` или -1 — в отличие от str.index не ловит упоминания в комментариях."""
    for i, line in enumerate(text.splitlines()):
        if line.strip() == f"[{name}]":
            return i
    return -1


class TestZoneModel(unittest.TestCase):
    def test_fmt_is_canonical_xywh(self):
        self.assertEqual(zc.Zone("main", 0, 0, 0.5, 1).fmt(), "0, 0, 0.5, 1")

    def test_fmt_trims_trailing_zeros(self):
        z = zc.Zone("q", 0.3333, 1.0, 0.5, 0.2500)
        self.assertEqual(z.fmt(), "0.3333, 1, 0.5, 0.25")

    def test_values_are_floats(self):
        z = zc.Zone("main", 0, 0, "0.5", "1")
        self.assertEqual(z.as_tuple(), (0.0, 0.0, 0.5, 1.0))

    def test_copy_is_independent(self):
        z = zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1")
        c = z.copy()
        c.name = "other"
        c.x = 0.9
        self.assertEqual(z.name, "main")
        self.assertEqual(z.x, 0.0)
        self.assertEqual(c.binding, "<Super><Alt>1")


class TestLoadZones(ConfMixin, unittest.TestCase):
    def test_rect_form_and_keys(self):
        path = self.conf(BASE_GRID + RECT_ZONES + "[keys]\nmain = <Super><Alt>1\n")
        zones = zc.load_zones(path)
        self.assertEqual([z.name for z in zones], ["main", "top-right"])
        self.assertEqual(zones[0].as_tuple(), (0.0, 0.0, 0.5, 1.0))
        self.assertEqual(zones[0].binding, "<Super><Alt>1")
        self.assertEqual(zones[1].binding, "")

    def test_grid_cell_form(self):
        path = self.conf(GRID_ZONES)
        zones = zc.load_zones(path)
        self.assertEqual(
            [(z.name, z.as_tuple()) for z in zones],
            [
                ("left", (0.0, 0.0, 0.25, 0.5)),
                ("right", (0.5, 0.0, 0.5, 0.5)),
            ],
        )

    def test_keys_section_is_optional(self):
        path = self.conf(BASE_GRID + RECT_ZONES)
        self.assertEqual([z.binding for z in zc.load_zones(path)], ["", ""])

    def test_load_config_returns_zones_and_extras(self):
        path = self.conf(BASE_GRID + RECT_ZONES + RULES + DAEMON)
        zones, extra = zc.load_config(path)
        self.assertEqual([z.name for z in zones], ["main", "top-right"])
        self.assertEqual(set(extra), {"rules", "daemon"})


class TestSaveZones(ConfMixin, unittest.TestCase):
    def test_writes_grid_zones_and_no_empty_sections(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(path, [zc.Zone("main", 0, 0, 0.5, 1)])
        text = self.read(path)
        self.assertIn("[grid]", text)
        self.assertIn("[zones]", text)
        self.assertIn("main = 0, 0, 0.5, 1", text)
        self.assertEqual(sections_in(text), ["grid", "zones"])

    def test_writes_keys_only_for_bound_zones(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(
            path,
            [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1"), zc.Zone("top", 0.5, 0, 0.5, 1)],
        )
        text = self.read(path)
        self.assertEqual(sections_in(text), ["grid", "zones", "keys"])
        self.assertEqual([z.binding for z in zc.load_zones(path)], ["<Super><Alt>1", ""])

    def test_save_load_roundtrip_keeps_zones(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zones = [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1"), zc.Zone("top", 0.5, 0, 0.5, 1)]
        zc.save_zones(path, zones)
        again = zc.load_zones(path)
        self.assertEqual(
            [(z.name, z.as_tuple(), z.binding) for z in again],
            [(z.name, z.as_tuple(), z.binding) for z in zones],
        )

    def test_creates_missing_directory(self):
        path = os.path.join(tempfile.mkdtemp(), "nested", "dir", "zones.conf")
        zc.save_zones(path, [zc.Zone("main", 0, 0, 1, 1)])
        self.assertTrue(os.path.exists(path))


class TestExtraSections(ConfMixin, unittest.TestCase):
    def test_reads_rules_and_daemon_verbatim(self):
        path = self.conf(BASE_GRID + RECT_ZONES + RULES + DAEMON)
        extra = zc.load_extra_sections(path)
        self.assertEqual(
            extra["rules"],
            ["Chrome = main", "re:^(Navigator|firefox) = top-right"],
        )
        self.assertEqual(extra["daemon"], ["reflow = off   # off | neighbors"])

    def test_absent_sections_are_not_invented(self):
        path = self.conf(BASE_GRID + RECT_ZONES)
        self.assertEqual(zc.load_extra_sections(path), {})

    def test_reserved_sections_are_skipped(self):
        path = self.conf(BASE_GRID + RECT_ZONES + "[keys]\nmain = <Super><Alt>1\n")
        self.assertEqual(zc.load_extra_sections(path), {})

    def test_unknown_section_is_preserved(self):
        path = self.conf(BASE_GRID + RECT_ZONES + "[custom]\nключ = значение\n")
        self.assertEqual(zc.load_extra_sections(path), {"custom": ["ключ = значение"]})

    def test_missing_file_gives_empty_dict(self):
        self.assertEqual(zc.load_extra_sections("/no/such/zones.conf"), {})

    def test_rules_daemon_roundtrip_is_lossless(self):
        path = self.conf(BASE_GRID + RECT_ZONES + RULES + DAEMON)
        zones = zc.load_zones(path)
        extra = zc.load_extra_sections(path)
        zc.save_zones(path, zones, extra)
        self.assertEqual(zc.load_extra_sections(path), extra)

    def test_roundtrip_preserves_case_and_comments(self):
        path = self.conf(BASE_GRID + RECT_ZONES + RULES + DAEMON)
        zc.save_zones(path, zc.load_zones(path), zc.load_extra_sections(path))
        text = self.read(path)
        self.assertIn("re:^(Navigator|firefox) = top-right", text)
        self.assertIn("reflow = off   # off | neighbors", text)

    def test_saving_without_extra_drops_nothing_but_does_not_invent(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(path, [zc.Zone("main", 0, 0, 0.5, 1)], {"rules": ["Chrome = main"]})
        text = self.read(path)
        self.assertIn("[rules]\nChrome = main", text)
        self.assertEqual(sections_in(text), ["grid", "zones", "rules"])

    def test_section_order_rules_keys_daemon(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(
            path,
            [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1")],
            {"daemon": ["reflow = off"], "rules": ["Chrome = main"]},
        )
        text = self.read(path)
        self.assertLess(section_index(text, "rules"), section_index(text, "keys"))
        self.assertLess(section_index(text, "keys"), section_index(text, "daemon"))

    def test_pairs_api_writes_key_value(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(path, [zc.Zone("main", 0, 0, 1, 1)], {"rules": [("Chrome", "main")]})
        self.assertIn("[rules]\nChrome = main", self.read(path))

    def test_empty_present_section_is_written_once(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(path, [zc.Zone("main", 0, 0, 1, 1)], {"rules": []})
        text = self.read(path)
        self.assertEqual(text.count("[rules]"), 1)
        self.assertEqual(sections_in(text), ["grid", "zones", "rules"])


class TestNames(unittest.TestCase):
    def test_sanitize_keeps_allowed(self):
        self.assertEqual(zc.sanitize_name("main"), "main")
        self.assertEqual(zc.sanitize_name("top-right.2"), "top-right.2")

    def test_sanitize_replaces_bad_runs(self):
        self.assertEqual(zc.sanitize_name("  My  Zone!  "), "My-Zone-")

    def test_sanitize_keeps_cyrillic(self):
        self.assertEqual(zc.sanitize_name("зона №1"), "зона-1")

    def test_sanitize_empty(self):
        self.assertEqual(zc.sanitize_name(""), "")
        self.assertEqual(zc.sanitize_name("###"), "-")

    def test_unique_name_counts_up(self):
        zones = [zc.Zone("zone1", 0, 0, 1, 1), zc.Zone("other", 0, 0, 1, 1)]
        self.assertEqual(zc.unique_name(zones), "zone2")
        self.assertEqual(zc.unique_name(zones, base="q"), "q1")
        self.assertEqual(zc.unique_name([]), "zone1")


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

import contextlib
import io
import os
import shutil
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
MONITOR_ZONES = "[zones:HDMI-1]\ntop = 0, 0, 1, 0.5\nbottom = 0, 0.5, 1, 0.5\n"
SECOND_MONITOR_ZONES = "[zones:DP-2]\nwide = 0, 0, 1, 1\n"
GRID_ZONES = (
    "[grid]\ncols = 0.25, 0.5, 1.0\nrows = 0.5, 1.0\n"
    "[zones]\nleft = 0:1, 0:1\nright = 2:3, 0:1\n"
)
RULES = "[rules]\nChrome = main\nre:^(Navigator|firefox) = top-right\n"
DAEMON = "[daemon]\nreflow = off   # off | neighbors\n"


class ConfMixin:
    """Временный каталог с zones.conf; каталог убирается после теста."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gridsnap-conf-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def conf(self, text):
        path = os.path.join(self.tmpdir, f"zones{len(os.listdir(self.tmpdir))}.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
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


class TestMonitorZoneSets(ConfMixin, unittest.TestCase):
    """Наборы зон на монитор: ``[zones]`` плюс ``[zones:<имя выхода>]``."""

    def two_sets(self):
        return self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + SECOND_MONITOR_ZONES)

    def test_without_monitor_reads_default_set(self):
        zones = zc.load_zones(self.two_sets())
        self.assertEqual([z.name for z in zones], ["main", "top-right"])

    def test_monitor_set_is_read_by_output_name(self):
        zones = zc.load_zones(self.two_sets(), "HDMI-1")
        self.assertEqual([z.name for z in zones], ["top", "bottom"])
        self.assertEqual(zones[1].as_tuple(), (0.0, 0.5, 1.0, 0.5))

    def test_unknown_monitor_falls_back_to_default_set(self):
        zones = zc.load_zones(self.two_sets(), "HDMI-9")
        self.assertEqual([z.name for z in zones], ["main", "top-right"])
        self.assertEqual([z.name for z in zc.load_zones(self.two_sets(), "")], ["main", "top-right"])

    def test_monitor_name_is_case_sensitive(self):
        # имя выхода X11 — не имя зоны: санитайза нет, регистр значим
        zones = zc.load_zones(self.two_sets(), "hdmi-1")
        self.assertEqual([z.name for z in zones], ["main", "top-right"])

    def test_monitor_set_takes_bindings_from_keys(self):
        path = self.conf(
            BASE_GRID + RECT_ZONES + MONITOR_ZONES + "[keys]\nmain = <Super><Alt>1\ntop = <Super><Alt>2\n"
        )
        self.assertEqual([z.binding for z in zc.load_zones(path, "HDMI-1")], ["<Super><Alt>2", ""])
        self.assertEqual([z.binding for z in zc.load_zones(path)], ["<Super><Alt>1", ""])

    def test_monitor_set_accepts_grid_cell_form(self):
        path = self.conf(
            "[grid]\ncols = 0.5, 1.0\nrows = 1.0\n"
            + RECT_ZONES
            + "[zones:HDMI-1]\nleft = 0:1, 0:1\nright = 1:2, 0:1\n"
        )
        self.assertEqual(
            [z.as_tuple() for z in zc.load_zones(path, "HDMI-1")],
            [(0.0, 0.0, 0.5, 1.0), (0.5, 0.0, 0.5, 1.0)],
        )

    def test_monitor_set_without_keys_section(self):
        self.assertEqual([z.binding for z in zc.load_zones(self.two_sets(), "DP-2")], [""])

    def test_empty_monitor_section_body_is_config_error(self):
        path = self.conf(BASE_GRID + RECT_ZONES + "[zones:HDMI-1]\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            zc.load_zones(path, "HDMI-1")

    def test_empty_monitor_name_is_config_error(self):
        path = self.conf(BASE_GRID + RECT_ZONES + "[zones:]\ntop = 0, 0, 1, 1\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.list_zone_sets(path)
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("[zones:]", err.getvalue())
        self.assertIn("[zones:HDMI-1]", err.getvalue())

    def test_extra_sections_skip_monitor_sets(self):
        # иначе `[zones:HDMI-1]` уедет в «чужие» секции нижним регистром и задвоится
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + RULES)
        self.assertEqual(set(zc.load_extra_sections(path)), {"rules"})


class TestZoneSetList(ConfMixin, unittest.TestCase):
    """``list_zone_sets`` — какие наборы есть в файле (для редактора и CLI)."""

    def test_lists_default_and_monitor_sets(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + SECOND_MONITOR_ZONES)
        self.assertEqual(
            zc.list_zone_sets(path),
            {
                None: ["main", "top-right"],
                "HDMI-1": ["top", "bottom"],
                "DP-2": ["wide"],
            },
        )

    def test_default_key_is_first_then_file_order(self):
        path = self.conf(BASE_GRID + RECT_ZONES + SECOND_MONITOR_ZONES + MONITOR_ZONES)
        self.assertEqual(list(zc.list_zone_sets(path)), [None, "DP-2", "HDMI-1"])

    def test_default_key_absent_without_zones_section(self):
        path = self.conf(BASE_GRID + MONITOR_ZONES)
        self.assertEqual(zc.list_zone_sets(path), {"HDMI-1": ["top", "bottom"]})

    def test_missing_file_lists_nothing(self):
        self.assertEqual(zc.list_zone_sets(os.path.join(tempfile.mkdtemp(), "none.conf")), {})

    def test_load_zone_sets_returns_objects_with_bindings(self):
        path = self.conf(
            BASE_GRID + RECT_ZONES + MONITOR_ZONES + "[keys]\nmain = <Super><Alt>1\ntop = <Super><Alt>2\n"
        )
        sets = zc.load_zone_sets(path)
        self.assertEqual(list(sets), [None, "HDMI-1"])
        self.assertEqual([z.binding for z in sets["HDMI-1"]], ["<Super><Alt>2", ""])


class TestSaveMonitorZoneSets(ConfMixin, unittest.TestCase):
    def test_save_monitor_set_writes_its_section(self):
        path = os.path.join(tempfile.mkdtemp(), "zones.conf")
        zc.save_zones(path, [zc.Zone("top", 0, 0, 1, 0.5)], monitor="HDMI-1")
        text = self.read(path)
        self.assertIn("[zones:HDMI-1]", text)
        self.assertEqual([z.name for z in zc.load_zones(path, "HDMI-1")], ["top"])

    def test_saving_one_monitor_keeps_other_sets_and_extras(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + SECOND_MONITOR_ZONES + RULES + DAEMON)
        zones = [zc.Zone("top", 0, 0, 1, 0.6), zc.Zone("bottom", 0, 0.6, 1, 0.4)]
        zc.save_zones(path, zones, zc.load_extra_sections(path), monitor="HDMI-1")
        text = self.read(path)
        self.assertIn("[zones:HDMI-1]", text)
        self.assertIn("[zones:DP-2]", text)  # соседний набор на месте
        self.assertIn("wide = 0, 0, 1, 1", text)
        self.assertIn("[rules]\nChrome = main", text)
        self.assertIn("reflow = off   # off | neighbors", text)
        self.assertEqual([z.as_tuple() for z in zc.load_zones(path, "HDMI-1")],
                         [(0.0, 0.0, 1.0, 0.6), (0.0, 0.6, 1.0, 0.4)])
        self.assertEqual([z.name for z in zc.load_zones(path)], ["main", "top-right"])

    def test_saving_default_keeps_monitor_sets(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES)
        zc.save_zones(path, [zc.Zone("only", 0, 0, 1, 1)], zc.load_extra_sections(path))
        self.assertEqual([z.name for z in zc.load_zones(path)], ["only"])
        self.assertEqual([z.name for z in zc.load_zones(path, "HDMI-1")], ["top", "bottom"])

    def test_monitor_section_written_once(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES)
        zc.save_zones(path, zc.load_zones(path, "HDMI-1"), zc.load_extra_sections(path), monitor="HDMI-1")
        text = self.read(path)
        self.assertEqual(text.count("[zones:HDMI-1]"), 1)
        self.assertEqual(sections_in(text), ["grid", "zones", "zones:HDMI-1"])

    def test_saving_monitor_keeps_other_monitor_bindings(self):
        path = self.conf(
            BASE_GRID
            + RECT_ZONES
            + MONITOR_ZONES
            + "[keys]\nmain = <Super><Alt>1\ntop = <Super><Alt>2\nwide = <Super><Alt>3\n"
            + SECOND_MONITOR_ZONES
        )
        zc.save_zones(path, [zc.Zone("top", 0, 0, 1, 0.5)], zc.load_extra_sections(path), monitor="HDMI-1")
        self.assertEqual([z.binding for z in zc.load_zones(path, "DP-2")], ["<Super><Alt>3"])
        self.assertEqual([z.binding for z in zc.load_zones(path)], ["<Super><Alt>1", ""])

    def test_empty_monitor_set_removes_its_section(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES)
        zc.save_zones(path, [], zc.load_extra_sections(path), monitor="HDMI-1")
        self.assertEqual(zc.list_zone_sets(path), {None: ["main", "top-right"]})

    def test_editor_roundtrip_keeps_all_sets(self):
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + SECOND_MONITOR_ZONES + RULES)
        zones, extra = zc.load_config(path)
        zones.append(zc.Zone("extra", 0.5, 0.5, 0.5, 0.5))
        zc.save_zones(path, zones, extra)
        self.assertEqual(
            zc.list_zone_sets(path),
            {
                None: ["main", "top-right", "extra"],
                "HDMI-1": ["top", "bottom"],
                "DP-2": ["wide"],
            },
        )
        self.assertIn("[rules]", self.read(path))


class TestSaveZoneSets(ConfMixin, unittest.TestCase):
    """Все наборы за один проход (`save_zone_sets`) и `[keys]` без повторов."""

    def keys_of(self, path):
        """Тело `[keys]` по строкам — не путать с подсказкой в шапке файла."""
        body, inside = [], False
        for line in self.read(path).splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                inside = stripped == "[keys]"
                continue
            if inside and "=" in line:
                body.append(line)
        return body

    def test_writes_default_and_monitor_sets_in_one_call(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(
            path,
            [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1")],
            {"HDMI-1": [zc.Zone("wide", 0, 0, 1, 1, "<Super><Alt>2")],
             "DP-2": [zc.Zone("top", 0, 0, 1, 0.5)]},
        )
        self.assertEqual(
            zc.list_zone_sets(path),
            {None: ["main"], "HDMI-1": ["wide"], "DP-2": ["top"]},
        )
        self.assertEqual(sections_in(self.read(path)), ["grid", "zones", "zones:HDMI-1",
                                                       "zones:DP-2", "keys"])
        self.assertEqual([z.binding for z in zc.load_zones(path, "HDMI-1")], ["<Super><Alt>2"])

    def test_duplicate_zone_name_in_two_sets_gives_one_key_line(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(
            path,
            [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1")],
            {"HDMI-1": [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>1"),
                        zc.Zone("wide", 0, 0, 1, 0.5, "<Super><Alt>2")]},
        )
        self.assertEqual(self.keys_of(path), ["main = <Super><Alt>1",
                                             "wide = <Super><Alt>2"])
        # главный ключ — из общего набора, он пишется первым
        sets = zc.load_zone_sets(path)
        self.assertEqual([z.binding for z in sets[None]], ["<Super><Alt>1"])
        self.assertEqual([z.binding for z in sets["HDMI-1"]], ["<Super><Alt>1", "<Super><Alt>2"])

    def test_default_set_key_wins_over_monitor_key(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(
            path,
            [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>1")],
            {"HDMI-1": [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>5")]},
        )
        self.assertEqual(self.keys_of(path), ["main = <Super><Alt>1"])

    def test_broken_keys_in_file_do_not_stop_the_rewrite(self):
        # Файл, испорченный руками: повтор в [keys]. Читать его писателю не нужно —
        # файл переписывается целиком, поэтому повтора в нём уже не будет.
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n"
                         + "[keys]\nmain = <Super><Alt>1\nmain = <Super><Alt>9\n")
        zc.save_zone_sets(path, [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>1")])
        self.assertEqual(self.keys_of(path), ["main = <Super><Alt>1"])
        self.assertEqual([z.binding for z in zc.load_zones(path)], ["<Super><Alt>1"])

    def test_sets_not_passed_are_not_kept(self):
        # Новая API — полная запись: что не передали, того в файле не будет.
        path = self.conf(BASE_GRID + RECT_ZONES + MONITOR_ZONES + SECOND_MONITOR_ZONES)
        zc.save_zone_sets(path, [zc.Zone("only", 0, 0, 1, 1)], {"HDMI-1": [zc.Zone("top", 0, 0, 1, 1)]})
        self.assertEqual(zc.list_zone_sets(path),
                         {None: ["only"], "HDMI-1": ["top"]})

    def test_no_default_section_with_none(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(path, None, {"HDMI-1": [zc.Zone("top", 0, 0, 1, 1)]})
        self.assertEqual(sections_in(self.read(path)), ["grid", "zones:HDMI-1"])
        self.assertEqual(zc.list_zone_sets(path), {"HDMI-1": ["top"]})

    def test_empty_set_and_empty_monitor_name_are_skipped(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(path, [zc.Zone("main", 0, 0, 1, 1)],
                          {"HDMI-1": [], "  ": [zc.Zone("x", 0, 0, 1, 1)], None: []})
        self.assertEqual(sections_in(self.read(path)), ["grid", "zones"])

    def test_extras_are_written_and_keys_sit_between_them(self):
        path = os.path.join(self.tmpdir, "zones.conf")
        zc.save_zone_sets(
            path,
            [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>1")],
            {"HDMI-1": [zc.Zone("wide", 0, 0, 1, 0.5)]},
            {"rules": ["Chrome = main"], "daemon": ["reflow = off"]},
        )
        text = self.read(path)
        self.assertEqual(sections_in(text), ["grid", "zones", "zones:HDMI-1", "rules",
                                             "keys", "daemon"])
        self.assertIn("[rules]\nChrome = main", text)

    def test_returns_path_and_creates_directory(self):
        path = os.path.join(self.tmpdir, "nested", "zones.conf")
        self.assertEqual(zc.save_zone_sets(path, [zc.Zone("main", 0, 0, 1, 1)]), path)
        self.assertTrue(os.path.exists(path))


class TestProfileKeys(ConfMixin, unittest.TestCase):
    def test_profile_has_one_key_line_per_zone_name(self):
        zones = [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1")]
        monitors = {"HDMI-1": [zc.Zone("main", 0, 0, 1, 1, "<Super><Alt>1"),
                                zc.Zone("wide", 0, 0, 1, 0.5, "<Super><Alt>2")]}
        path = zc.save_profile("dupes", zones, None, os.path.join(self.tmpdir, "zones.conf"),
                               monitor_sets=monitors)
        text = self.read(path)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("main = <")), 1,
                         text)
        conf = os.path.join(self.tmpdir, "zones.conf")
        self.assertEqual([z.binding for z in zc.read_profile("dupes", conf)[0]],
                         ["<Super><Alt>1"])


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


class TestMissingRuleTargets(unittest.TestCase):
    def test_finds_dangling_targets(self):
        extra = {"rules": ["Chrome = main", "re:^(Navigator|firefox) = top-right"]}
        self.assertEqual(
            zc.missing_rule_targets(extra, ["main"]),
            [("re:^(Navigator|firefox)", "top-right")],
        )

    def test_no_dangling_when_all_targets_exist(self):
        extra = {"rules": ["Chrome = main", "re:^firefox = top-right"]}
        self.assertEqual(zc.missing_rule_targets(extra, {"main", "top-right"}), [])

    def test_empty_without_rules_section(self):
        self.assertEqual(zc.missing_rule_targets({}, ["main"]), [])
        self.assertEqual(zc.missing_rule_targets({"daemon": ["reflow = off"]}, ["main"]), [])
        self.assertEqual(zc.missing_rule_targets({"rules": []}, ["main"]), [])
        self.assertEqual(zc.missing_rule_targets(None, ["main"]), [])

    def test_order_and_multiple_dangling(self):
        extra = {"rules": ["a = gone1", "re:^b = gone2"]}
        self.assertEqual(
            zc.missing_rule_targets(extra, ["other"]),
            [("a", "gone1"), ("re:^b", "gone2")],
        )

    def test_skips_comments_and_inline_comments(self):
        extra = {"rules": ["# правило", "", "Chrome = main  # браузер", " ; too",
                            "Navigator = gone ; старая зона"]}
        self.assertEqual(zc.missing_rule_targets(extra, ["main"]), [("Navigator", "gone")])

    def test_accepts_tuple_pairs(self):
        extra = {"rules": [("Chrome", "main"), ("re:^firefox", "top-right")]}
        self.assertEqual(zc.missing_rule_targets(extra, ["main"]), [("re:^firefox", "top-right")])

    def test_zone_names_may_be_zone_objects(self):
        extra = {"rules": ["Chrome = main"]}
        self.assertEqual(zc.missing_rule_targets(extra, [zc.Zone("main", 0, 0, 1, 1)]), [])

    def test_section_name_is_case_insensitive(self):
        self.assertEqual(zc.missing_rule_targets({"Rules": ["a = gone"]}, ["main"]),
                         [("a", "gone")])


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

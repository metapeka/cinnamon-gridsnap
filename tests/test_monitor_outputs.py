#!/usr/bin/env python3
"""Юнит-тесты мониторов по именам выходов — без X, без GDK и без реального xrandr.

Покрыто: разбор `xrandr --listmonitors`, сшивка имён выходов с рабочими
областями GDK (`stitch_monitor_names`, `monitor_outputs`), выбор монитора по
имени/номеру (`resolve_monitor`), наборы зон на монитор (`[zones:<выход>]`:
`zones_for`, `zone_spec`, `all_zone_names`) и вывод `list` с зонами монитора.

xrandr, GDK и рабочие области подменяются целиком: реальные утилиты и дисплей
тестам не нужны.

Запуск:
    python3 -m unittest discover -s tests -v
    python3 tests/test_monitor_outputs.py
"""

import argparse
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_monitors_under_test", TILER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()

BASE_GRID = "[grid]\ncols = 1.0\nrows = 1.0\n"

# Реальный вывод `xrandr --listmonitors` на машине с двумя выходами: у первого
# стоит флаг `+*` (авто + основной), имя — последнее поле строки.
XRANDR_TWO = (
    "Monitors: 2\n"
    " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
    " 1: +HDMI-1 1920/527x1080/296+1920+0  HDMI-1\n"
)


class ConfCase(unittest.TestCase):
    """Пишет временный zones.conf и убирает его после теста."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path


class TestParseXrandr(unittest.TestCase):
    def test_names_and_geometry_in_order(self):
        self.assertEqual(
            zt.parse_xrandr_monitors(XRANDR_TWO),
            [("eDP-1", (0, 0, 1920, 1080)), ("HDMI-1", (1920, 0, 1920, 1080))],
        )

    def test_flags_are_not_part_of_name(self):
        # `+*` — состояние монитора (авто/основной), а не имя выхода.
        parsed = zt.parse_xrandr_monitors(" 0: +*HDMI-1 1920/698x1080/393+0+0  HDMI-1\n")
        self.assertEqual(parsed, [("HDMI-1", (0, 0, 1920, 1080))])

    def test_negative_offsets(self):
        parsed = zt.parse_xrandr_monitors(" 0: +DP-1 1280/340x1024/270+-1280+0  DP-1\n")
        self.assertEqual(parsed, [("DP-1", (-1280, 0, 1280, 1024))])

    def test_name_falls_back_to_field_after_index(self):
        # Старый xrandr без последнего поля: имя берём из `+*eDP-1`, сняв флаги.
        parsed = zt.parse_xrandr_monitors(" 0: +*eDP-1 1920/344x1080/194+0+0\n")
        self.assertEqual(parsed, [("eDP-1", (0, 0, 1920, 1080))])

    def test_header_and_junk_are_ignored(self):
        text = "Monitors: 1\nмусор\n 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n\n"
        self.assertEqual(len(zt.parse_xrandr_monitors(text)), 1)
        self.assertEqual(zt.parse_xrandr_monitors(""), [])

    def test_xrandr_missing_gives_empty_list(self):
        # Утилиты нет — имён нет, поведение прежнее (синтетические имена).
        with mock.patch.object(zt.subprocess, "check_output", side_effect=FileNotFoundError):
            self.assertEqual(zt.xrandr_monitors(), [])

    def test_xrandr_failure_status_gives_empty_list(self):
        with mock.patch.object(
            zt.subprocess,
            "check_output",
            side_effect=subprocess.CalledProcessError(1, "xrandr"),
        ):
            self.assertEqual(zt.xrandr_monitors(), [])


class TestStitch(unittest.TestCase):
    def setUp(self):
        self.outputs = [("eDP-1", (0, 0, 1920, 1080)), ("HDMI-1", (1920, 0, 1920, 1080))]

    def test_equal_counts_are_paired_by_order(self):
        areas = [(0, 40, 1920, 1040), (1920, 40, 1920, 1040)]
        self.assertEqual(
            zt.stitch_monitor_names(self.outputs, areas),
            [("eDP-1", areas[0]), ("HDMI-1", areas[1])],
        )

    def test_unequal_counts_pair_by_overlap(self):
        # GDK отдала одну область — она перекрывается с первым выходом.
        stitched = zt.stitch_monitor_names(self.outputs, [(0, 40, 1920, 1040)])
        self.assertEqual(stitched, [("eDP-1", (0, 40, 1920, 1040))])

    def test_unequal_counts_use_the_output_that_overlaps(self):
        stitched = zt.stitch_monitor_names(self.outputs, [(1920, 40, 1920, 1040)])
        self.assertEqual(stitched, [("HDMI-1", (1920, 40, 1920, 1040))])

    def test_two_areas_under_one_output(self):
        # Имён меньше, чем областей: второе имя уже занято, область без
        # пересечения получает синтетическое имя.
        outputs = [("HDMI-1", (0, 0, 1920, 1080))]
        areas = [(0, 40, 1920, 500), (0, 540, 1920, 540)]
        stitched = zt.stitch_monitor_names(outputs, areas)
        self.assertEqual(stitched[0], ("HDMI-1", areas[0]))
        self.assertEqual(stitched[1], ("monitor-1", areas[1]))

    def test_no_names_gives_synthetic_monitor_index(self):
        areas = [(0, 40, 1920, 1040), (1920, 40, 1920, 1040)]
        self.assertEqual(
            zt.stitch_monitor_names([], areas),
            [("monitor-0", areas[0]), ("monitor-1", areas[1])],
        )

    def test_zero_overlap_gives_synthetic_name(self):
        stitched = zt.stitch_monitor_names(self.outputs, [(5000, 5000, 800, 600)])
        self.assertEqual(stitched, [("monitor-0", (5000, 5000, 800, 600))])

    def test_overlap_area(self):
        self.assertEqual(zt.overlap_area((0, 0, 10, 10), (5, 5, 10, 10)), 25)
        self.assertEqual(zt.overlap_area((0, 0, 10, 10), (20, 0, 10, 10)), 0)
        self.assertEqual(zt.overlap_area((0, 0, 10, 10), (0, 0, 10, 10)), 100)

    def test_monitor_outputs_takes_names_from_xrandr(self):
        areas = [(0, 40, 1920, 1040)]
        with mock.patch.object(zt, "monitors", return_value=list(areas)), mock.patch.object(
            zt, "xrandr_monitors", return_value=[("eDP-1", (0, 0, 1920, 1080))]
        ):
            self.assertEqual(zt.monitor_outputs(), [("eDP-1", areas[0])])

    def test_monitor_outputs_without_xrandr_keeps_old_behaviour(self):
        areas = [(0, 40, 1920, 1040), (1920, 40, 1920, 1040)]
        with mock.patch.object(zt, "monitors", return_value=list(areas)), mock.patch.object(
            zt, "xrandr_monitors", return_value=[]
        ):
            self.assertEqual(
                zt.monitor_outputs(),
                [("monitor-0", areas[0]), ("monitor-1", areas[1])],
            )


class TestResolveMonitor(unittest.TestCase):
    OUTPUTS = [("eDP-1", (0, 40, 1920, 1040)), ("HDMI-1", (1920, 40, 1920, 1040))]

    def test_by_index(self):
        self.assertEqual(zt.resolve_monitor(1, self.OUTPUTS), (1, "HDMI-1"))
        self.assertEqual(zt.resolve_monitor("0", self.OUTPUTS), (0, "eDP-1"))

    def test_by_name(self):
        self.assertEqual(zt.resolve_monitor("HDMI-1", self.OUTPUTS), (1, "HDMI-1"))

    def test_name_wins_over_index(self):
        # Монитор с именем "1" стоит первым: имя приоритетнее номера.
        outputs = [("1", (0, 0, 100, 100)), ("0", (100, 0, 100, 100))]
        self.assertEqual(zt.resolve_monitor("1", outputs), (0, "1"))

    def test_unknown_name_lists_available(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            zt.resolve_monitor("DP-9", self.OUTPUTS)
        self.assertEqual(ctx.exception.code, zt.EXIT_USAGE)
        self.assertIn("DP-9", err.getvalue())
        self.assertIn("eDP-1", err.getvalue())
        self.assertIn("HDMI-1", err.getvalue())

    def test_out_of_range_index_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            zt.resolve_monitor("7", self.OUTPUTS)
        self.assertEqual(ctx.exception.code, zt.EXIT_USAGE)


class TestGridMonitorZones(ConfCase):
    TWO_SETS = (
        BASE_GRID
        + "[zones]\nmain = 0, 0, 1, 1\n"
        + "[zones:HDMI-1]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    )

    def test_zones_for_section_and_default(self):
        grid = zt.Grid(self.conf(self.TWO_SETS))
        self.assertEqual(sorted(grid.zones_for("HDMI-1")), ["left", "right"])
        self.assertEqual(sorted(grid.zones_for("eDP-1")), ["main"])
        self.assertEqual(sorted(grid.zones_for(None)), ["main"])

    def test_zones_for_ignores_case(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[zones:hdmi-1]\nleft = 0, 0, 0.5, 1\n"
        )
        grid = zt.Grid(path)
        self.assertEqual(sorted(grid.zones_for("HDMI-1")), ["left"])

    def test_all_zone_names_is_union(self):
        grid = zt.Grid(self.conf(self.TWO_SETS))
        self.assertEqual(grid.all_zone_names(), ["main", "left", "right"])

    def test_monitor_zone_overrides_default_in_rect(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[zones:HDMI-1]\nmain = 0, 0, 0.5, 1\n"
        )
        grid = zt.Grid(path)
        self.assertEqual(grid.fractions("main", "HDMI-1"), (0.0, 0.0, 0.5, 1.0))
        self.assertEqual(grid.fractions("main"), (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(grid.rect("main", (0, 0, 1000, 1000), "HDMI-1"), (0, 0, 500, 1000))

    def test_zone_missing_on_monitor_falls_back_to_default(self):
        # Зона есть только в общем [zones]: на мониторе со своей секцией её нет.
        grid = zt.Grid(self.conf(self.TWO_SETS))
        self.assertEqual(
            grid.rect("main", (0, 0, 1000, 1000), "HDMI-1"), (0, 0, 1000, 1000)
        )

    def test_zone_missing_everywhere_is_config_error(self):
        grid = zt.Grid(self.conf(self.TWO_SETS))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            grid.rect("nope", (0, 0, 1000, 1000), "HDMI-1")
        self.assertEqual(ctx.exception.code, zt.EXIT_CONFIG)
        self.assertIn("'nope'", err.getvalue())

    def test_missing_default_section_is_config_error(self):
        path = self.conf(BASE_GRID + "[zones:HDMI-1]\nmain = 0, 0, 1, 1\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            zt.Grid(path)
        self.assertEqual(ctx.exception.code, zt.EXIT_CONFIG)
        self.assertIn("обязательной [zones] нет", err.getvalue())

    def test_empty_monitor_section_is_config_error(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[zones:HDMI-1]\n# пусто\n"
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_rule_may_target_zone_of_one_monitor(self):
        grid = zt.Grid(self.conf(self.TWO_SETS + "[rules]\nFirefox = left\n"))
        self.assertEqual(grid.rules, [("Firefox", "left")])

    def test_rule_to_unknown_zone_still_fails(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            zt.Grid(self.conf(self.TWO_SETS + "[rules]\nFirefox = nope\n"))
        self.assertEqual(ctx.exception.code, zt.EXIT_CONFIG)
        self.assertIn("неизвестную зону 'nope'", err.getvalue())

    def test_keys_may_use_zone_of_one_monitor(self):
        grid = zt.Grid(self.conf(self.TWO_SETS + "[keys]\nleft = <Super><Alt>1\n"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zt.cmd_keys(grid)
        self.assertEqual(buf.getvalue(), "left\t<Super><Alt>1\n")


class TestCmdList(ConfCase):
    """`list --monitor` показывает зоны выбранного монитора (GDK не нужен)."""

    OUTPUTS = [("eDP-1", (0, 40, 1000, 1000)), ("HDMI-1", (1000, 40, 960, 1000))]

    def run_list(self, grid, monitor):
        buf = io.StringIO()
        with mock.patch.object(
            zt, "monitor_outputs", return_value=list(self.OUTPUTS)
        ), contextlib.redirect_stdout(buf):
            zt.cmd_list(grid, argparse.Namespace(monitor=monitor))
        return buf.getvalue().splitlines()

    def test_list_by_monitor_name_uses_its_zones(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[zones:HDMI-1]\nleft = 0, 0, 0.5, 1\n"
        )
        lines = self.run_list(zt.Grid(path), "HDMI-1")
        self.assertEqual(
            lines[0], "рабочая область монитора 1: 1000,40 960x1000  (выход HDMI-1)"
        )
        self.assertEqual(lines[1].split()[0], "left")
        self.assertIn("480x1000", lines[1])

    def test_list_by_index_uses_default_zones(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[zones:HDMI-1]\nleft = 0, 0, 0.5, 1\n"
        )
        lines = self.run_list(zt.Grid(path), 0)
        self.assertEqual(lines[0], "рабочая область монитора 0: 0,40 1000x1000  (выход eDP-1)")
        self.assertEqual(lines[1].split()[0], "main")

    def test_list_first_line_stays_applet_compatible(self):
        # Апплет ждёт строку без имени выхода: `рабочая область монитора N: ...`.
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n")
        buf = io.StringIO()
        with mock.patch.object(
            zt, "monitor_outputs", return_value=[("monitor-0", (0, 40, 1000, 1000))]
        ), contextlib.redirect_stdout(buf):
            zt.cmd_list(zt.Grid(path), argparse.Namespace(monitor="0"))
        self.assertEqual(
            buf.getvalue().splitlines()[0], "рабочая область монитора 0: 0,40 1000x1000"
        )

    def test_unknown_monitor_name_is_usage_error(self):
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            self.run_list(zt.Grid(path), "DP-9")
        self.assertEqual(ctx.exception.code, zt.EXIT_USAGE)
        self.assertIn("Доступны", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты перемещения окна в соседнюю зону (`move`) и секции `[move]`.

Без X-сервера и GTK: мониторы, рамка окна и укладка подменяются, поэтому
проверяется именно логика — откуда и куда едет окно, что бывает на краю сетки
и с окном вне зон, что `--dry-run` ничего не двигает, а `undo` получает запись
истории. Отдельно — разбор необязательной секции `[move]`
(`zone_config.load_move_keys`: нет секции — умолчания, неизвестный ключ — код 3),
`keys --move` и то, что сохранение зон из `zone_config` секцию не теряет.

Запуск:
    python3 tests/test_move.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")
SETUP_HOTKEYS = os.path.join(ROOT, "setup-hotkeys.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402

import i18n_env  # noqa: F401  (тесты идут на исходном языке)


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_move_test", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_setup_hotkeys():
    spec = importlib.util.spec_from_file_location("setup_hotkeys_move_test", SETUP_HOTKEYS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MONITOR = ("monitor-0", (0, 0, 1000, 1000))
HDMI = ("HDMI-1", (1000, 0, 1000, 1000))

# Две половины: по ним видно и переезд «из левой в правую», и край сетки.
TWO_HALVES = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
)
# Щель посередине: окно в ней не попадает ни в одну зону.
GAP = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.4, 1\nright = 0.6, 0, 0.4, 1\n"
)
# Набор монитора HDMI-1: свои имена зон, которых в общем наборе нет.
WITH_HDMI = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    "[zones:HDMI-1]\nhdmi-left = 0, 0, 0.5, 1\nhdmi-right = 0.5, 0, 0.5, 1\n"
)


class MoveCase(unittest.TestCase):
    """Временный конфиг и подменённые X-зависимости: рамку окна задаёт тест."""

    config = TWO_HALVES

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-move-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        self.write_conf(self.config)
        self.zt = load_tiler()
        self.frame = (50, 50, 100, 100)  # окно в левой зоне
        self.applied = []  # (окно, рамка, подпись)
        self.history = []  # (вид операции, записи)
        for name, value in (
            ("monitor_outputs", lambda: [MONITOR]),
            ("current_workspace", lambda: None),
            ("active_window", lambda: 0x1234),
            ("frame_extents", lambda win: (0, 0, 0, 0)),
            ("read_frame", lambda win, extents: self.frame),
            ("apply_zone", self._apply),
            ("snapshot_window", self._snapshot),
            ("remember", self._remember),
        ):
            patcher = mock.patch.object(self.zt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- подмена X

    def _apply(self, window, rect, label="", **_kwargs):
        self.applied.append((window, rect, label))

    def _snapshot(self, win, zone=None):
        return {"id": win, "frame": [1, 2, 3, 4], "wm_class": "", "zone": zone}

    def _remember(self, kind, records, quiet=False):
        self.history.append((kind, records))

    # ---- запуск

    def write_conf(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_cli(self, *argv):
        """(код, stdout, stderr) — `main` в процессе, без подпроцесса и X."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.zt.main([*argv, "--conf", self.conf])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def run_cli_subprocess(self, *argv, env=None):
        return subprocess.run(
            [sys.executable, TILER, *argv, "--conf", self.conf],
            text=True,
            capture_output=True,
            env=env,
        )

    def rect_of(self, zone, wa, monitor=None):
        return self.zt.Grid(self.conf).rect(zone, wa, monitor_name=monitor)


class MoveNeighbourTest(MoveCase):
    """Куда едет окно: сосед по геометрии, край сетки, окно вне зон."""

    def test_window_in_left_zone_moves_to_right(self):
        code, out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied, [(0x1234, (500, 0, 500, 1000), "right")])
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0][0], "move")
        self.assertEqual(self.history[0][1][0]["id"], 0x1234)

    def test_history_keeps_previous_frame_and_zone(self):
        self.run_cli("move", "right")
        kind, records = self.history[0]
        self.assertEqual(kind, "move")
        self.assertEqual(records[0]["frame"], [1, 2, 3, 4])
        self.assertEqual(records[0]["zone"], "right")

    def test_neighbour_on_edge_is_not_an_error(self):
        self.frame = (700, 50, 100, 100)  # правая половина
        code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.history, [])
        self.assertIn("соседа right нет", err)
        self.assertIn("right", err)

    def test_quiet_hides_edge_message(self):
        self.frame = (700, 50, 100, 100)
        code, _out, err = self.run_cli("move", "right", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_window_outside_zones_takes_nearest_centre(self):
        # Щель 0.4..0.6: центр окна 0.52 ближе к центру правой зоны (0.8).
        self.write_conf(GAP)
        self.frame = (470, 400, 100, 100)
        code, _out, err = self.run_cli("move", "left")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied, [(0x1234, (0, 0, 400, 1000), "left")])

    def test_window_outside_zones_on_edge_still_reports_no_neighbour(self):
        self.write_conf(GAP)
        self.frame = (470, 400, 100, 100)  # ближайшая зона — правая
        code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0)
        self.assertEqual(self.applied, [])
        self.assertIn("соседа right нет", err)

    def test_window_on_monitor_uses_its_zone_set(self):
        self.write_conf(WITH_HDMI)
        self.frame = (1100, 100, 100, 100)  # левая половина HDMI-1
        with mock.patch.object(self.zt, "monitor_outputs", lambda: [MONITOR, HDMI]):
            code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0, err)
        expected = self.rect_of("hdmi-right", HDMI[1], monitor="HDMI-1")
        self.assertEqual(self.applied, [(0x1234, expected, "hdmi-right")])

    def test_monitor_flag_overrides_window_monitor(self):
        # Окно на первом мониторе, а --monitor указывает на HDMI-1: берётся
        # набор зон HDMI-1, а не тот, где окно лежит.
        self.write_conf(WITH_HDMI)
        with mock.patch.object(self.zt, "monitor_outputs", lambda: [MONITOR, HDMI]):
            code, _out, err = self.run_cli("move", "right", "--monitor", "HDMI-1")
        self.assertEqual(code, 0, err)
        expected = self.rect_of("hdmi-right", HDMI[1], monitor="HDMI-1")
        self.assertEqual(self.applied, [(0x1234, expected, "hdmi-right")])

    def test_dry_run_does_not_move_window(self):
        code, out, err = self.run_cli("move", "right", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.history, [])
        self.assertIn("x=500", out)
        self.assertIn("'right'", out)

    def test_window_flag_is_used(self):
        code, _out, err = self.run_cli("move", "right", "--window", "0x77")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied[0][0], 0x77)

    def test_bad_direction_is_usage_error(self):
        code, _out, err = self.run_cli("move", "sideways")
        self.assertEqual(code, 2)
        self.assertIn("направление", err)
        self.assertEqual(self.applied, [])


class MoveFailuresTest(MoveCase):
    """Ошибки выполнения: окно не прочитать, X недоступен."""

    def test_unreadable_window_is_execution_error(self):
        def boom(win, extents):
            raise RuntimeError("нет такого окна")

        with mock.patch.object(self.zt, "read_frame", boom):
            code, _out, err = self.run_cli("move", "left")
        self.assertEqual(code, 1)
        self.assertIn("не удалось прочитать рамку окна 0x1234", err)
        self.assertIn("нет такого окна", err)
        self.assertEqual(self.applied, [])

    def test_no_display_is_execution_error(self):
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        r = self.run_cli_subprocess("move", "left", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("рабочую область экрана", r.stderr)


class MoveGeometryTest(unittest.TestCase):
    """Чистые `nearest_zone`/`move_zone_index` — без конфига и X."""

    def setUp(self):
        self.zt = load_tiler()
        self.nav = self.zt.picker_nav()
        self.zones = [
            (0.0, 0.0, 0.5, 0.5),  # 0: левый верх
            (0.5, 0.0, 0.5, 0.5),  # 1: правый верх
            (0.0, 0.5, 0.5, 0.5),  # 2: левый низ
            (0.5, 0.5, 0.5, 0.5),  # 3: правый низ
        ]

    def test_nearest_zone_by_centre(self):
        self.assertEqual(self.zt.nearest_zone(self.zones, 0.05, 0.05), 0)
        self.assertEqual(self.zt.nearest_zone(self.zones, 0.9, 0.9), 3)
        self.assertIsNone(self.zt.nearest_zone([], 0.5, 0.5))

    def test_move_index_matches_picker_rule(self):
        # Сосед берётся правилом пикера, а не своей копией: сверяем с ним.
        for direction in self.nav.DIRECTIONS:
            here, there = self.zt.move_zone_index(self.zones, direction, 0.25, 0.25)
            self.assertEqual(here, 0)
            self.assertEqual(there, self.nav.move_selection(self.zones, 0, direction))

    def test_point_in_gap_uses_nearest_zone(self):
        # Точка вне всех зон (за правым-нижним краем) — берётся ближайшая зона.
        here, there = self.zt.move_zone_index(self.zones, "up", 1.05, 0.55)
        self.assertEqual(here, 3)  # ближайший центр — правый низ
        self.assertEqual(there, 1)  # «вверх» из него — правая верхняя

    def test_empty_set_gives_nothing(self):
        self.assertEqual(self.zt.move_zone_index([], "left", 0.5, 0.5), (None, None))


class MoveSectionTest(unittest.TestCase):
    """Разбор `[move]` в `zone_config` — без X и GTK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-move-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_missing_section_gives_defaults(self):
        self.write(TWO_HALVES)
        self.assertEqual(zc.load_move_keys(self.path), zc.DEFAULT_MOVE_KEYS)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(
            zc.load_move_keys(os.path.join(self.dir, "нет.conf")), zc.DEFAULT_MOVE_KEYS
        )

    def test_partial_section_fills_the_rest_with_defaults(self):
        self.write(TWO_HALVES + "[move]\nleft = <Super><Alt>h\n")
        keys = zc.load_move_keys(self.path)
        self.assertEqual(keys["left"], "<Super><Alt>h")
        for direction in ("right", "up", "down"):
            self.assertEqual(keys[direction], zc.DEFAULT_MOVE_KEYS[direction])

    def test_all_four_directions_are_read(self):
        self.write(
            TWO_HALVES
            + "[move]\n"
            "left = <Super>h\nright = <Super>l\nup = <Super>k\ndown = <Super>j\n"
        )
        self.assertEqual(
            zc.load_move_keys(self.path),
            {"left": "<Super>h", "right": "<Super>l", "up": "<Super>k", "down": "<Super>j"},
        )

    def test_unknown_key_is_config_error(self):
        self.write(TWO_HALVES + "[move]\nsideways = <Super>s\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_move_keys(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("неизвестное направление", err.getvalue())
        self.assertIn("sideways", err.getvalue())

    def test_empty_binding_is_config_error(self):
        self.write(TWO_HALVES + "[move]\nleft =\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_move_keys(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("пустое сочетание", err.getvalue())

    def test_section_is_not_a_foreign_section(self):
        # `[move]` — своя секция: редактор не должен таскать её как «чужую».
        self.write(TWO_HALVES + "[move]\nleft = <Super>h\n[daemon]\nreflow = off\n")
        self.assertNotIn("move", zc.load_extra_sections(self.path))
        self.assertIn("daemon", zc.load_extra_sections(self.path))

    def test_saving_zones_keeps_move_section(self):
        self.write(
            TWO_HALVES
            + "[move]\n# мои клавиши\nleft = <Super><Alt>h\n\n[keys]\nleft = <Super><Alt>1\n"
        )
        zc.save_zones(self.path, zc.load_zones(self.path))
        text = self.read()
        self.assertIn("[move]", text)
        self.assertEqual(zc.load_move_keys(self.path)["left"], "<Super><Alt>h")
        # Комментарий внутри секции тоже переживает сохранение зон.
        self.assertIn("# мои клавиши", text)

    def test_editor_roundtrip_keeps_move_section(self):
        # Как сохраняет редактор: все наборы + «чужие» секции одним вызовом.
        self.write(
            TWO_HALVES
            + "[move]\nup = <Super>k\n"
            + "[rules]\nChrome = left\n"
        )
        zones = zc.load_zones(self.path)
        zc.save_zone_sets(self.path, zones, {}, zc.load_extra_sections(self.path))
        self.assertEqual(zc.load_move_keys(self.path)["up"], "<Super>k")
        self.assertIn("[rules]", self.read())

    def test_apply_profile_keeps_move_section(self):
        # `profile use` переписывает zones.conf под профиль, но `[move]` —
        # настройка человека, а не раскладка: она должна остаться.
        self.write(TWO_HALVES + "[move]\nleft = <Super><Alt>h\n")
        zc.save_profile("work", zc.load_zones(self.path), None, self.path)
        zc.apply_profile("work", self.path)
        self.assertEqual(zc.load_move_keys(self.path)["left"], "<Super><Alt>h")

    def test_absent_section_is_not_invented(self):
        self.write(TWO_HALVES)
        zc.save_zones(self.path, zc.load_zones(self.path))
        self.assertNotIn("[move]", self.read())

    def test_move_goes_after_keys_and_before_daemon(self):
        self.write(TWO_HALVES + "[move]\nleft = <Super>h\n[daemon]\nreflow = off\n")
        zc.save_zones(
            self.path, zc.load_zones(self.path), zc.load_extra_sections(self.path)
        )
        text = self.read()
        self.assertLess(text.index("[zones]"), text.index("[move]"))
        self.assertLess(text.index("[move]"), text.index("[daemon]"))


class KeysMoveCliTest(unittest.TestCase):
    """`zone-tiler.py keys --move` — источник сочетаний для setup-hotkeys."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-move-keys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(TWO_HALVES + "[keys]\nleft = <Super><Alt>1\n")

    def run_cli(self, *argv):
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        return subprocess.run(
            [sys.executable, TILER, *argv, "--conf", self.path],
            text=True,
            capture_output=True,
            env=env,
        )

    def test_defaults_are_printed(self):
        r = self.run_cli("keys", "--move")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines(),
            [
                "left\t<Super>Left",
                "right\t<Super>Right",
                "up\t<Super>Up",
                "down\t<Super>Down",
            ],
        )

    def test_section_overrides_and_unknown_key_is_config_error(self):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("[move]\nleft = <Super><Alt>h\n")
        r = self.run_cli("keys", "--move")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left\t<Super><Alt>h", r.stdout)
        self.assertIn("right\t<Super>Right", r.stdout)

        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(TWO_HALVES + "[move]\nleft = <Super>h\nsideways = <Super>s\n")
        r = self.run_cli("keys", "--move")
        self.assertEqual(r.returncode, 3)
        self.assertIn("неизвестное направление", r.stderr)

    def test_keys_without_flag_work_as_before(self):
        r = self.run_cli("keys")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["left\t<Super><Alt>1"])


class HotkeyNamesTest(unittest.TestCase):
    """`setup-hotkeys`: имена сочетаний и предупреждение о конфликте с muffin.

    Сами `gsettings` не трогаются: `zone_pairs` и `push_tile_bindings`
    подменяются, проверяется сборка плана установки.
    """

    def setUp(self):
        self.mod = load_setup_hotkeys()

    def test_move_direction_is_taken_from_its_name(self):
        items = [
            ("main", "<Super><Alt>1", "cmd"),
            ("move right", "<Super><Alt>l", "cmd"),
        ]
        self.assertEqual(self.mod.conflicts_with_push_tile(items, native={}), [])
        self.assertEqual(
            self.mod.conflicts_with_push_tile(items, native={"right": ["<Super><Alt>l"]}),
            [("right", "<Super><Alt>l")],
        )

    def test_zone_binding_is_not_compared_with_push_tile(self):
        items = [("main", "<Super>Left", "cmd")]
        self.assertEqual(
            self.mod.conflicts_with_push_tile(items, native={"left": ["<Super>Left"]}),
            [],
        )

    def test_plan_registers_quiet_move_command(self):
        pairs = {
            (): [("main", "<Super><Alt>1")],
            ("--move",): [("left", "<Super><Alt>h")],
        }

        def fake_pairs(conf=None, *flags):
            return pairs[flags]

        with mock.patch.object(self.mod, "zone_pairs", fake_pairs):
            items = self.mod.planned("/tmp/zones.conf")
        self.assertEqual([name for name, _binding, _cmd in items], ["main", "move left"])
        self.assertIn(" move left --quiet", items[1][2])
        self.assertIn("--conf /tmp/zones.conf", items[1][2])
        self.assertIn(" apply main --quiet", items[0][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
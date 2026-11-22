#!/usr/bin/env python3
"""Юнит-тесты раскладки одной командой и истории undo — без X и GTK.

Покрыто: выбор подвижных окон (те же фильтры, что у демона), режимы `arrange`
(`auto`/`rules`/`order`), общая рамка вкладки для окон одной зоны, персистентная история в XDG-состоянии
(предел, битый файл), план `undo` (последняя операция и вся история), разбор
воркспейса и CLI-ошибки новых команд.

Запуск:
    python3 -m unittest discover -s tests -v
    python3 tests/test_arrange_undo.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # zone-tiler и его модули
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_arrange_under_test", TILER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()


def meta(wm_class="Firefox", wm_title="", window_type=()):
    return zt.WindowMeta(wm_class, wm_title, frozenset(window_type))


class TestMovableWindows(unittest.TestCase):
    def test_keeps_normal_classed_windows_in_order(self):
        ids = [0x1, 0x2, 0x3]
        metas = {0x1: meta("Firefox"), 0x2: meta("Terminal"), 0x3: meta("Nautilus")}
        self.assertEqual(zt.movable_windows(ids, metas), ids)

    def test_drops_window_without_class(self):
        ids = [0x1, 0x2]
        metas = {0x1: meta(""), 0x2: meta("Firefox")}
        self.assertEqual(zt.movable_windows(ids, metas), [0x2])

    def test_drops_ignored_types(self):
        ids = [0x1, 0x2, 0x3, 0x4]
        metas = {
            0x1: meta("Firefox", window_type={"_NET_WM_WINDOW_TYPE_DIALOG"}),
            0x2: meta("Firefox", window_type={"_NET_WM_WINDOW_TYPE_SPLASH"}),
            0x3: meta("Firefox", window_type={"TOOLTIP"}),  # короткое имя
            0x4: meta("Firefox", window_type={"_NET_WM_WINDOW_TYPE_NORMAL"}),
        }
        self.assertEqual(zt.movable_windows(ids, metas), [0x4])

    def test_drops_ids_without_metadata(self):
        metas = {0x2: meta("Firefox")}
        self.assertEqual(zt.movable_windows([0x1, 0x2], metas), [0x2])


class TestChooseAssignments(unittest.TestCase):
    RULES = (("firefox*", "left"), ("chromium*", "right"))

    def test_rules_mode_only_matched_windows(self):
        ids = [0x1, 0x2, 0x3]
        metas = {0x1: meta("firefox-esr"), 0x2: meta("xterm"), 0x3: meta("Chromium")}
        self.assertEqual(
            zt.choose_assignments(ids, metas, ["left", "right"], self.RULES, "rules"),
            [(0x1, "left"), (0x3, "right")],
        )

    def test_order_mode_pairs_windows_and_zones(self):
        ids = [0x1, 0x2, 0x3]
        metas = {0x1: meta("a"), 0x2: meta("b"), 0x3: meta("c")}
        self.assertEqual(
            zt.choose_assignments(ids, metas, ["left", "right"], [], "order"),
            [(0x1, "left"), (0x2, "right"), (0x3, "right")],
        )

    def test_order_mode_without_windows(self):
        self.assertEqual(zt.choose_assignments([], {}, ["left"], [], "order"), [])

    def test_auto_mode_rules_first_then_free_zones(self):
        ids = [0x1, 0x2, 0x3, 0x4]
        metas = {
            0x1: meta("firefox-esr"),  # правило -> left
            0x2: meta("xterm"),        # свободная зона -> first free
            0x3: meta("another"),      # следующая свободная
            0x4: meta("extra"),        # свободных зон больше нет -> не трогаем
        }
        self.assertEqual(
            zt.choose_assignments(ids, metas, ["left", "right", "bottom"], self.RULES, "auto"),
            [(0x1, "left"), (0x2, "right"), (0x3, "bottom")],
        )

    def test_auto_mode_leaves_unruled_when_no_free_zones(self):
        ids = [0x1, 0x2]
        metas = {0x1: meta("firefox-esr"), 0x2: meta("xterm")}
        self.assertEqual(
            zt.choose_assignments(ids, metas, ["left"], self.RULES, "auto"),
            [(0x1, "left")],
        )

    def test_auto_mode_uses_free_zone_for_unruled_window(self):
        ids = [0x1]
        metas = {0x1: meta("xterm")}
        self.assertEqual(
            zt.choose_assignments(ids, metas, ["left"], self.RULES, "auto"),
            [(0x1, "left")],
        )


class TestInZoneRect(unittest.TestCase):
    BASE = (100, 100, 400, 300)

    def test_single_window_sits_exactly_on_zone(self):
        self.assertEqual(zt.in_zone_rect(self.BASE, 1), self.BASE)

    def test_two_windows_share_frame_below_strip(self):
        # Полоса по умолчанию — 22 px: окно сдвинуто вниз на её высоту.
        self.assertEqual(zt.in_zone_rect(self.BASE, 2), (100, 122, 400, 278))

    def test_all_windows_of_zone_get_the_same_frame(self):
        self.assertEqual(
            zt.in_zone_rect(self.BASE, 2), zt.in_zone_rect(self.BASE, 7)
        )

    def test_custom_height_moves_window_down(self):
        self.assertEqual(zt.in_zone_rect(self.BASE, 2, 30), (100, 130, 400, 270))

    def test_narrow_zone_has_no_strip_and_keeps_zone(self):
        # Окну не остаётся 44 px под полосой — полосы нет, рамка — сама зона.
        base = (100, 100, 400, 50)
        self.assertEqual(zt.in_zone_rect(base, 3), base)

    def test_frame_is_in_pixels(self):
        rect = zt.in_zone_rect(self.BASE, 2)
        self.assertTrue(all(isinstance(v, int) for v in rect), rect)


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "gridsnap", "history.json")

    def operation(self, win=0x1, frame=(0, 0, 10, 10), kind="arrange"):
        return {
            "op": kind,
            "time": 123.0,
            "windows": [{"id": win, "frame": list(frame), "wm_class": "Firefox", "workspace": 0}],
        }

    def test_record_and_load_roundtrip(self):
        history = zt.History(self.path)
        history.record(self.operation())
        self.assertEqual(history.load(), [self.operation()])

    def test_limit_keeps_only_last_operations(self):
        history = zt.History(self.path, limit=3)
        for i in range(5):
            history.record(self.operation(win=0x10 + i))
        loaded = history.load()
        self.assertEqual([w["windows"][0]["id"] for w in loaded], [0x12, 0x13, 0x14])

    def test_missing_file_is_empty(self):
        self.assertEqual(zt.History(self.path).load(), [])

    def test_corrupt_file_is_empty(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ это не json")
        self.assertEqual(zt.History(self.path).load(), [])

    def test_wrong_shape_is_empty(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"operations": "нет"}')
        self.assertEqual(zt.History(self.path).load(), [])

    def test_history_path_lives_in_xdg_state(self):
        old = os.environ.get("XDG_STATE_HOME")
        self.addCleanup(self._restore, old)
        os.environ["XDG_STATE_HOME"] = self.tmp.name
        self.assertEqual(
            zt.history_path(), os.path.join(self.tmp.name, "gridsnap", "history.json")
        )

    @staticmethod
    def _restore(old):
        if old is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = old


class TestUndoTargets(unittest.TestCase):
    OPS = (
        {
            "op": "arrange",
            "windows": (
                {"id": 1, "frame": [0, 0, 10, 10], "wm_class": "A"},
                {"id": 2, "frame": [1, 1, 20, 20], "wm_class": "B"},
            ),
        },
        {"op": "stack", "windows": ({"id": 1, "frame": [5, 5, 10, 10], "wm_class": "A"},)},
    )

    def test_last_operation_only(self):
        self.assertEqual(zt.undo_targets(self.OPS), [(1, [5, 5, 10, 10], "A")])

    def test_all_operations_use_earliest_frame_per_window(self):
        self.assertEqual(
            zt.undo_targets(self.OPS, all_=True),
            [(1, [0, 0, 10, 10], "A"), (2, [1, 1, 20, 20], "B")],
        )

    def test_empty_history(self):
        self.assertEqual(zt.undo_targets([]), [])
        self.assertEqual(zt.undo_targets([], all_=True), [])

    def test_malformed_entries_are_skipped(self):
        ops = [
            {
                "windows": [
                    {"id": None, "frame": [0, 0, 1, 1]},
                    {"id": 5, "frame": [0, 0, 1]},          # не 4 числа
                    {"id": 6, "frame": [0, 0, 1, 1]},
                    "мусор",
                ]
            }
        ]
        self.assertEqual(zt.undo_targets(ops), [(6, [0, 0, 1, 1], None)])


class TestWindowWorkspace(unittest.TestCase):
    def test_parses_desktop_number(self):
        with mock.patch.object(
            zt.subprocess, "check_output", return_value="_NET_WM_DESKTOP(CARDINAL) = 2\n"
        ):
            self.assertEqual(zt.window_workspace(0x1), 2)

    def test_missing_value_is_none(self):
        with mock.patch.object(
            zt.subprocess, "check_output", return_value="_NET_WM_DESKTOP:  not found.\n"
        ):
            self.assertIsNone(zt.window_workspace(0x1))

    def test_error_is_none(self):
        with mock.patch.object(zt.subprocess, "check_output", side_effect=OSError):
            self.assertIsNone(zt.window_workspace(0x1))


class TestCli(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, TILER, *args], text=True, capture_output=True, env=env
        )

    def test_help_lists_new_commands(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        for word in ("arrange", "stack", "tabs-next", "undo", "--by", "--all", "[tabs]"):
            self.assertIn(word, r.stdout)
        # Каскада больше нет: ни флага, ни упоминания в справке.
        self.assertNotIn("--offset", r.stdout)

    def test_stack_unknown_zone_is_config_error(self):
        # Нет зоны ни в сетке стола, ни в общем [zones] — ошибка конфига (код 3),
        # см. `require_zone` в zone-tiler.py.
        r = self.run_cli("stack", "no-such-zone")
        self.assertEqual(r.returncode, 3)
        self.assertIn("нет зоны", r.stderr)

    def test_arrange_bad_mode_is_usage_error(self):
        r = self.run_cli("arrange", "--by", "nonsense")
        self.assertEqual(r.returncode, 2)

    def test_undo_empty_history_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_STATE_HOME=tmp)
            env.pop("DISPLAY", None)
            r = self.run_cli("undo", env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("история раскладок пуста", r.stdout)

    def test_arrange_without_display_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_STATE_HOME=tmp)
            env.pop("DISPLAY", None)
            r = self.run_cli("arrange", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("_NET_CLIENT_LIST", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

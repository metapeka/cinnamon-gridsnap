#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты `--workspace`: команды работают сеткой своего рабочего стола.

Без X и GTK: `monitor_outputs`, `current_workspace`, `read_client_list` и
`read_window_meta` подменяются, а рамки считаются чистой геометрией `Grid`.
Проверяется главное: стол с профилем берёт зоны профиля, стол без профиля и
неизвестный стол — общий `[zones]`, неверный номер стола — код 2, зона не из
набора — код 3 с перечнем доступных, а первая строка `list` остаётся такой,
какой её ждёт апплет.

Запуск:
    python3 tests/test_cli_workspace.py
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_ws_test", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Общий набор: одна зона `main` на половину экрана.
BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\n"
)
# Профиль стола: свои имена зон, которых в общем наборе нет.
PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nvideo = 0, 0, 1, 0.75\nside = 0, 0.75, 1, 0.25\n"
)
# Стол 2 (в Cinnamon) — это индекс 1 в EWMH.
WORKSPACES = "[workspaces]\n2 = cinema\n"
MONITOR = ("monitor-0", (0, 0, 1920, 1000))


class WorkspaceCliCase(unittest.TestCase):
    """Временный конфиг с профилем стола и подменённые X-зависимости."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-cli-ws-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        templates = os.path.join(self.dir, "templates")
        os.makedirs(templates)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(BASE + WORKSPACES)
        with open(os.path.join(templates, "cinema.conf"), "w", encoding="utf-8") as fh:
            fh.write(PROFILE)
        self.zt = load_tiler()
        # Монитор и стол подменяются: DISPLAY в тестах не нужен.
        for name, value in (
            ("monitor_outputs", lambda: [MONITOR]),
            ("current_workspace", lambda: None),
        ):
            patcher = mock.patch.object(self.zt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def grid_by_workspace(self, workspace):
        """Какая сетка достаётся столу — через те же функции, что у CLI."""
        return self.zt.grid_for_workspace(self.conf, workspace)


class WorkspaceArgTest(WorkspaceCliCase):
    """Разбор `--workspace` и значение по умолчанию."""

    def test_explicit_number_stays_number(self):
        self.assertEqual(self.zt.workspace_spec("0"), 0)
        self.assertEqual(self.zt.workspace_spec("3"), 3)

    def test_current_and_empty_are_current(self):
        self.assertEqual(self.zt.workspace_spec("current"), self.zt.WORKSPACE_CURRENT)
        self.assertEqual(self.zt.workspace_spec("Current"), self.zt.WORKSPACE_CURRENT)
        self.assertEqual(self.zt.workspace_spec(""), self.zt.WORKSPACE_CURRENT)

    def test_bad_value_is_argument_error(self):
        with self.assertRaises(Exception):
            self.zt.workspace_spec("abc")

    def test_resolve_current_calls_x_and_number_does_not(self):
        with mock.patch.object(self.zt, "current_workspace", return_value=2) as called:
            self.assertEqual(
                self.zt.resolve_workspace(self.zt.WORKSPACE_CURRENT), 2
            )
            self.assertEqual(self.zt.resolve_workspace(None), 2)
            self.assertEqual(self.zt.resolve_workspace(1), 1)
        self.assertEqual(called.call_count, 2)

    def test_profile_name_of_mapped_workspace(self):
        self.assertEqual(self.zt.workspace_profile(self.conf, 1), "cinema")
        self.assertIsNone(self.zt.workspace_profile(self.conf, 0))
        self.assertIsNone(self.zt.workspace_profile(self.conf, None))

    def test_profile_name_of_missing_profile_is_none(self):
        os.unlink(os.path.join(self.dir, "templates", "cinema.conf"))
        self.assertIsNone(self.zt.workspace_profile(self.conf, 1))
        self.assertEqual(sorted(self.grid_by_workspace(1).zones), ["main"])


class ApplyWorkspaceTest(WorkspaceCliCase):
    """`apply` считает рамку по сетке выбранного стола."""

    def test_profile_zone_on_mapped_workspace(self):
        # --workspace 1 — второй стол (EWMH с нуля), профиль cinema.
        code, out, _ = self.run_cli("apply", "video", "--workspace", "1", "--dry-run")
        self.assertEqual(code, 0)
        # video = 0, 0, 1, 0.75 рабочей области 1920x1000
        self.assertIn("video: x=0 y=0 1920x750", out)

    def test_general_zone_on_workspace_zero(self):
        code, out, _ = self.run_cli("apply", "main", "--workspace", "0", "--dry-run")
        self.assertEqual(code, 0)
        # main = половина ширины из общего [zones]
        self.assertIn("main: x=0 y=0 960x1000", out)

    def test_profile_zone_is_absent_on_general_workspace(self):
        # На столе без профиля зоны профиля не существует — ошибка конфига.
        code, _, err = self.run_cli("apply", "video", "--workspace", "0", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("нет зоны 'video'", err)
        self.assertIn("main", err)

    def test_unknown_zone_on_profile_workspace_lists_profile_zones(self):
        code, _, err = self.run_cli("apply", "main", "--workspace", "1", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("нет зоны 'main'", err)
        self.assertIn("стола 2", err)
        self.assertIn("cinema", err)
        # перечень доступных зон — из профиля, а не из общего набора
        self.assertIn("side", err)
        self.assertIn("video", err)

    def test_sticky_workspace_keeps_general_zones(self):
        # Стол без профиля (workspace 0) — прежнее поведение.
        code, out, _ = self.run_cli("apply", "main", "--workspace", "0", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("main:", out)


class CurrentWorkspaceTest(WorkspaceCliCase):
    """`--workspace current` и отсутствие аргумента идут за активным столом."""

    def test_current_uses_profile_of_active_workspace(self):
        with mock.patch.object(self.zt, "current_workspace", return_value=1):
            code, out, _ = self.run_cli("apply", "video", "--workspace", "current", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("1920x750", out)

    def test_default_is_current_workspace(self):
        # Без --workspace вообще: тот же активный стол, без X.
        with mock.patch.object(self.zt, "current_workspace", return_value=1):
            code, out, _ = self.run_cli("apply", "side", "--dry-run")
        self.assertEqual(code, 0)
        # side = 0, 0.75, 1, 0.25 -> y=750, h=250
        self.assertIn("side: x=0 y=750 1920x250", out)

    def test_current_without_display_falls_back_to_general(self):
        # current_workspace() вернул None (нет DISPLAY) — общий zones.conf.
        with mock.patch.object(self.zt, "current_workspace", return_value=None):
            code, out, _ = self.run_cli("apply", "main", "--workspace", "current", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("main: x=0 y=0 960x1000", out)

    def test_current_none_rejects_profile_zone(self):
        with mock.patch.object(self.zt, "current_workspace", return_value=None):
            code, _, err = self.run_cli("apply", "video", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("нет зоны 'video'", err)


class BadWorkspaceValueTest(WorkspaceCliCase):
    """Неверное значение `--workspace` — ошибка аргументов (код 2)."""

    def test_abc_is_usage_error(self):
        code, _, err = self.run_cli("list", "--workspace", "abc")
        self.assertEqual(code, self.zt.EXIT_USAGE)
        self.assertIn("--workspace", err)
        self.assertIn("current", err)

    def test_negative_is_usage_error(self):
        code, _, err = self.run_cli("apply", "main", "--workspace=-1", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_USAGE)
        self.assertIn("--workspace", err)

    def test_apply_and_stack_and_arrange_share_the_error(self):
        for command in (["apply", "main"], ["stack", "main"], ["arrange"]):
            code, _, _ = self.run_cli(*command, "--workspace", "десятый", "--dry-run")
            self.assertEqual(code, self.zt.EXIT_USAGE, command)


class ListWorkspaceTest(WorkspaceCliCase):
    """`list` показывает зоны стола и называет профиль."""

    def test_list_profile_workspace(self):
        code, out, _ = self.run_cli("list", "--workspace", "1")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        # Формат первой строки не сломан: его разбирает апплет.
        self.assertTrue(lines[0].startswith("рабочая область монитора 0: 0,0 1920x1000"))
        self.assertIn("cinema", lines[0])
        self.assertIn("video", out)
        self.assertIn("side", out)
        self.assertNotIn("main", out)
        # video = 0, 0, 1, 0.75 -> 1920x750
        self.assertTrue(any("video" in line and "1920x750" in line for line in lines))

    def test_list_general_workspace_has_no_profile_note(self):
        code, out, _ = self.run_cli("list", "--workspace", "0")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "рабочая область монитора 0: 0,0 1920x1000")
        self.assertIn("main", out)
        self.assertNotIn("video", out)
        self.assertNotIn("профиль", out)

    def test_list_without_mapping_is_unchanged(self):
        # В конфиге нет [workspaces] — прежнее поведение и прежняя строка.
        self.write_conf(BASE)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "рабочая область монитора 0: 0,0 1920x1000")
        self.assertIn("main", out)


class ArrangeAndStackWorkspaceTest(WorkspaceCliCase):
    """`arrange`/`stack` раскладывают зонами сетки выбранного стола."""

    def test_arrange_uses_profile_zones(self):
        meta = self.zt.WindowMeta("Firefox", "окно", frozenset())
        with mock.patch.object(self.zt, "read_client_list", return_value=[0x4D]), \
                mock.patch.object(self.zt, "read_window_meta", return_value=meta), \
                mock.patch.object(
                    self.zt,
                    "group_windows_by_monitor",
                    return_value=[(MONITOR[0], MONITOR[1], [0x4D])],
                ):
            code, out, _ = self.run_cli("arrange", "--workspace", "1", "--dry-run")
            self.assertEqual(code, 0)
            # первая свободная зона профиля по порядку файла — video
            self.assertIn("зона 'video'", out)
            self.assertIn("1920x750", out)

    def test_arrange_on_general_workspace_uses_general_zones(self):
        meta = self.zt.WindowMeta("Firefox", "окно", frozenset())
        with mock.patch.object(self.zt, "read_client_list", return_value=[0x4D]), \
                mock.patch.object(self.zt, "read_window_meta", return_value=meta), \
                mock.patch.object(
                    self.zt,
                    "group_windows_by_monitor",
                    return_value=[(MONITOR[0], MONITOR[1], [0x4D])],
                ):
            code, out, _ = self.run_cli("arrange", "--workspace", "0", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("зона 'main'", out)

    def test_stack_unknown_zone_on_profile_workspace(self):
        code, _, err = self.run_cli("stack", "main", "--workspace", "1", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("video", err)


class KeysAndProfileUnaffectedTest(WorkspaceCliCase):
    """`keys` и `profile *` не зависят от стола."""

    def test_keys_are_read_from_file_not_profile(self):
        self.write_conf(BASE + WORKSPACES + "[keys]\nmain = <Super><Alt>1\n")
        code, out, _ = self.run_cli("keys", "--workspace", "1")
        self.assertEqual(code, 0)
        # [keys] общая на файл: профиль стола её не подменяет
        self.assertIn("main\t<Super><Alt>1", out)

    def test_profile_list_is_file_level(self):
        code, out, _ = self.run_cli("profile", "list")
        self.assertEqual(code, 0)
        self.assertIn("cinema", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ConfWorkspaceTest(WorkspaceCliCase):
    """`--workspace conf` — сетка zones.conf без профилей столов.

    Это нужно редактору: холст показывает зоны самого `zones.conf`, поэтому
    «проверить зону» обязано считать ту же сетку, даже когда активный стол
    сопоставлен профилю.
    """

    def test_spec_parses_conf(self):
        self.assertEqual(self.zt.workspace_spec("conf"), self.zt.WORKSPACE_CONF)
        self.assertEqual(self.zt.workspace_spec("CONF"), self.zt.WORKSPACE_CONF)

    def test_grid_is_general_even_for_mapped_workspace(self):
        # Активный стол — второй (индекс 1) и он сопоставлен профилю cinema.
        with mock.patch.object(self.zt, "current_workspace", lambda: 1):
            grid = self.zt.grid_for_workspace(self.conf, self.zt.WORKSPACE_CONF)
        self.assertEqual(sorted(grid.zones), ["main"])

    def test_profile_name_is_none(self):
        self.assertIsNone(self.zt.workspace_profile(self.conf, self.zt.WORKSPACE_CONF))

    def test_list_shows_general_zones_and_no_profile_note(self):
        code, out, _err = self.run_cli("list", "--workspace", "conf")
        self.assertEqual(code, 0)
        self.assertIn("main", out)
        self.assertNotIn("video", out)
        self.assertNotIn("профиль", out)

    def test_apply_uses_general_zones(self):
        # Зона профиля на этом столе недоступна: сетка взята из zones.conf.
        code, _out, err = self.run_cli(
            "apply", "video", "--workspace", "conf", "--dry-run"
        )
        self.assertEqual(code, 3)
        self.assertIn("main", err)

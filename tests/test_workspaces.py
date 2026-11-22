#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты сопоставления «рабочий стол → профиль».

Без X и GTK: `load_workspace_map` живёт в чистом конфиг-слое, а
`grid_for_workspace`/`current_workspace` проверяются с подменой `xprop`.
Проверяется главное: столе без профиля получает общий `[zones]`, отсутствующий
или битый профиль не ломает раскладку, а «липкое» окно (`_NET_WM_DESKTOP =
0xFFFFFFFF`) живёт в наборе по умолчанию.

Запуск:
    python3 tests/test_workspaces.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib.util  # noqa: E402

import zone_config as zc  # noqa: E402

import i18n_env  # noqa: F401  (тесты идут на исходном языке)


def load_tiler():
    """Загрузить `zone-tiler.py` как модуль: он не пакет, а скрипт."""
    path = os.path.join(ROOT, "zone-tiler.py")
    spec = importlib.util.spec_from_file_location("zone_tiler_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\n"
)
WORKSPACES = "[workspaces]\n2 = cinema\n"
PROFILE = "[grid]\ncols = 1.0\nrows = 1.0\n[zones]\nvideo = 0, 0, 1, 0.75\n"


class WorkspaceMapTest(unittest.TestCase):
    """Секция `[workspaces]`: ключи с 1, значения — имена профилей."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-ws-")
        self.conf = os.path.join(self.dir, "zones.conf")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_no_section_is_empty_map(self):
        self.write(BASE)
        self.assertEqual(zc.load_workspace_map(self.conf), {})

    def test_missing_file_is_empty_map(self):
        self.assertEqual(zc.load_workspace_map(os.path.join(self.dir, "нет.conf")), {})

    def test_reads_numbers_and_profiles(self):
        self.write(BASE + "[workspaces]\n1 = work\n3 = cinema\n")
        self.assertEqual(zc.load_workspace_map(self.conf), {1: "work", 3: "cinema"})

    def test_bad_key_is_config_error(self):
        self.write(BASE + "[workspaces]\nfirst = work\n")
        with self.assertRaises(SystemExit) as ctx:
            zc.load_workspace_map(self.conf)
        self.assertEqual(ctx.exception.code, 3)

    def test_zero_key_is_config_error(self):
        # Ключи с 1: ноль сбивал бы перевод в нумерацию EWMH.
        self.write(BASE + "[workspaces]\n0 = work\n")
        with self.assertRaises(SystemExit):
            zc.load_workspace_map(self.conf)

    def test_empty_profile_is_config_error(self):
        self.write(BASE + "[workspaces]\n1 =\n")
        with self.assertRaises(SystemExit):
            zc.load_workspace_map(self.conf)


class GridForWorkspaceTest(unittest.TestCase):
    """Какой набор зон получает стол."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-gw-")
        self.conf = os.path.join(self.dir, "zones.conf")
        self.templates = os.path.join(self.dir, "templates")
        os.makedirs(self.templates)
        self.zt = load_tiler()
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(BASE + WORKSPACES)
        with open(os.path.join(self.templates, "cinema.conf"), "w", encoding="utf-8") as fh:
            fh.write(PROFILE)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_mapped_workspace_uses_profile_zones(self):
        # Конфиг: стол «2» (ключ 2) → профиль cinema; в EWMH это индекс 1.
        grid = self.zt.grid_for_workspace(self.conf, 1)
        self.assertEqual(sorted(grid.zones), ["video"])

    def test_unmapped_workspace_uses_default(self):
        grid = self.zt.grid_for_workspace(self.conf, 0)
        self.assertEqual(sorted(grid.zones), ["main"])

    def test_missing_profile_falls_back_to_default(self):
        os.unlink(os.path.join(self.templates, "cinema.conf"))
        grid = self.zt.grid_for_workspace(self.conf, 1)
        self.assertEqual(sorted(grid.zones), ["main"])

    def test_no_mapping_at_all_uses_default(self):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(BASE)
        grid = self.zt.grid_for_workspace(self.conf, 1)
        self.assertEqual(sorted(grid.zones), ["main"])

    def test_unknown_workspace_uses_default(self):
        self.assertIsNone(self.zt.current_workspace())
        grid = self.zt.grid_for_workspace(self.conf, None)
        self.assertEqual(sorted(grid.zones), ["main"])


class StickyWindowTest(unittest.TestCase):
    """«Липкое» окно живёт в наборе по умолчанию, а не в профиле стола."""

    def setUp(self):
        self.zt = load_tiler()

    def test_sticky_window_has_no_workspace(self):
        original = self.zt.window_workspace
        try:
            self.zt.window_workspace = lambda win: 0xFFFFFFFF
            self.assertIsNone(self.zt.window_workspace_for_zones(1))
        finally:
            self.zt.window_workspace = original

    def test_normal_window_keeps_workspace(self):
        original = self.zt.window_workspace
        try:
            self.zt.window_workspace = lambda win: 2
            self.assertEqual(self.zt.window_workspace_for_zones(1), 2)
        finally:
            self.zt.window_workspace = original


class CurrentWorkspaceTest(unittest.TestCase):
    """`_NET_CURRENT_DESKTOP` читается числом, мусор и сбой дают None."""

    def setUp(self):
        self.zt = load_tiler()

    def test_reads_number(self):
        original = subprocess.check_output
        try:
            subprocess.check_output = lambda *a, **kw: "_NET_CURRENT_DESKTOP(CARDINAL) = 2\n"
            self.assertEqual(self.zt.current_workspace(), 2)
        finally:
            subprocess.check_output = original

    def test_garbage_is_none(self):
        original = subprocess.check_output
        try:
            subprocess.check_output = lambda *a, **kw: "no property\n"
            self.assertIsNone(self.zt.current_workspace())
        finally:
            subprocess.check_output = original

    def test_failure_is_none(self):
        original = subprocess.check_output
        try:
            def boom(*a, **kw):
                raise OSError("нет xprop")

            subprocess.check_output = boom
            self.assertIsNone(self.zt.current_workspace())
        finally:
            subprocess.check_output = original


if __name__ == "__main__":
    unittest.main()

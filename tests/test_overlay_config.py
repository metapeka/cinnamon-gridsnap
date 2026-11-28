#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the desktop grid (`zone-picker.py --grid/--toggle`).

Two halves, both without a display:

* ``zone_config.load_overlay_key`` — the ``[overlay]`` shortcut: the default without the
  section, a key of one's own, an empty key that turns the shortcut off, and a typo that
  is a config error;
* the toggle's bookkeeping — the per-desktop pid file, the stale/foreign pid that must
  not be trusted, and the detached command the toggle starts.

The drawing itself is GTK, so the module is loaded through `importlib` with the same gate
as `tests/test_picker_hud.py`: without GTK the file skips instead of failing.
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
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402

try:  # GTK is needed for the picker module itself, not for the logic under test
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa: F401

    HAS_GUI = True
except Exception:  # pragma: no cover - a machine without GTK skips the module half
    HAS_GUI = False

PICKER = os.path.join(ROOT, "zone-picker.py")


def load_picker():
    spec = importlib.util.spec_from_file_location("zone_picker_overlay_under_test", PICKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TWO_HALVES = (
    "[grid]\ncols = 2\nrows = 1\n\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
)


class OverlaySectionTest(unittest.TestCase):
    """`[overlay]` in `zone_config`: parsing only — no X, no GTK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-overlay-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_the_default_key_works_without_the_section(self):
        self.write(TWO_HALVES)
        self.assertEqual(zc.load_overlay_key(self.path), zc.DEFAULT_OVERLAY_KEY)

    def test_a_missing_file_keeps_the_default_key(self):
        self.assertEqual(
            zc.load_overlay_key(os.path.join(self.dir, "absent.conf")),
            zc.DEFAULT_OVERLAY_KEY,
        )

    def test_a_key_of_one_s_own_is_read(self):
        self.write(TWO_HALVES + "[overlay]\nkey = <Super><Shift>g\n")
        self.assertEqual(zc.load_overlay_key(self.path), "<Super><Shift>g")

    def test_an_empty_key_turns_the_shortcut_off(self):
        self.write(TWO_HALVES + "[overlay]\nkey =\n")
        self.assertIsNone(zc.load_overlay_key(self.path))

    def test_an_unknown_key_is_a_config_error(self):
        self.write(TWO_HALVES + "[overlay]\nshortcut = <Super>g\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_overlay_key(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("unknown key", err.getvalue())
        self.assertIn("shortcut", err.getvalue())

    def test_the_section_is_not_reserved(self):
        # A reserved section is edited by the editor; the overlay section is carried verbatim
        # as foreign, so a save does not lose the key.
        self.assertNotIn(zc.OVERLAY_SECTION, zc.RESERVED_SECTIONS)


@unittest.skipUnless(HAS_GUI, "нужны gi/GTK: модуль пикера импортирует их на уровне файла")
class ToggleBookkeepingTest(unittest.TestCase):
    """The pid file and the detached command: no display, no window."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-grid-runtime-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_pid_file_is_per_desktop(self):
        path = self.picker.grid_pid_path(3)
        self.assertEqual(path, os.path.join(self.dir, "gridsnap", "grid-3.pid"))
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_an_unknown_desktop_gets_its_own_file(self):
        self.assertEqual(
            os.path.basename(self.picker.grid_pid_path(None)), "grid-conf.pid"
        )

    def test_a_missing_file_is_no_process(self):
        self.assertIsNone(self.picker.grid_pid(self.picker.grid_pid_path(0)))

    def test_a_garbage_file_is_no_process(self):
        path = self.picker.grid_pid_path(0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not a pid")
        self.assertIsNone(self.picker.grid_pid(path))

    def test_a_foreign_live_pid_is_not_ours(self):
        # Our own process is alive, but its command line is not the picker's grid: the toggle
        # must never kill an unrelated program that inherited a recycled pid.
        path = self.picker.grid_pid_path(0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self.assertIsNone(self.picker.grid_pid(path))

    def test_the_detached_command_carries_the_mode_and_the_conf(self):
        seen = []

        def spawn(command):
            seen.append(command)
            return mock.Mock(pid=4242)

        path = self.picker.grid_pid_path(1)
        pid = self.picker.start_grid("/tmp/zones.conf", 1, 2, path, spawn=spawn)
        self.assertEqual(pid, 4242)
        self.assertEqual(len(seen), 1)
        command = seen[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("zone-picker.py"), command)
        self.assertIn("--grid", command)
        self.assertEqual(command[command.index("--conf") + 1], "/tmp/zones.conf")
        self.assertEqual(command[command.index("--workspace") + 1], "1")
        self.assertEqual(command[command.index("--monitor") + 1], "2")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "4242")

    def test_a_stale_file_is_replaced_by_a_new_grid(self):
        path = self.picker.grid_pid_path(1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("999999")  # no such process on Linux in practice
        calls = []

        def spawn(command):
            calls.append(command)
            return mock.Mock(pid=5252)

        self.assertEqual(self.picker.toggle_grid("/tmp/zones.conf", 1, path=path, spawn=spawn), 0)
        self.assertEqual(len(calls), 1)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "5252")

    def test_a_grid_that_cannot_start_fails_loudly(self):
        def spawn(_command):
            raise OSError("no python")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.picker.toggle_grid("/tmp/zones.conf", 1, spawn=spawn)
        self.assertEqual(code, self.picker.EXIT_FAIL)
        self.assertIn("cannot start the desktop grid", err.getvalue())

    # ---- the mode combinations of the command line

    def parse(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            self.picker.parse_args(list(argv))
        return caught.exception.code, err.getvalue()

    def test_the_grid_accepts_a_monitor_and_a_snapshot(self):
        args = self.picker.parse_args(["--grid", "--monitor", "1", "--shot", "/tmp/x.png"])
        self.assertTrue(args.grid)
        self.assertEqual(args.monitor, 1)

    def test_a_grid_with_a_window_is_an_argument_error(self):
        code, err = self.parse("--grid", "--window", "0x1")
        self.assertEqual(code, 2)
        self.assertIn("--grid is incompatible", err)

    def test_a_grid_with_a_point_is_an_argument_error(self):
        code, err = self.parse("--grid", "--at", "10,20")
        self.assertEqual(code, 2)
        self.assertIn("--grid is incompatible", err)

    def test_a_toggle_with_a_snapshot_is_an_argument_error(self):
        code, err = self.parse("--toggle", "--shot", "/tmp/x.png")
        self.assertEqual(code, 2)
        self.assertIn("--toggle is incompatible", err)


if __name__ == "__main__":
    unittest.main()

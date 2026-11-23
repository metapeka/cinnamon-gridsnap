#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for `--workspace`: the commands work with the grid of their own workspace.

Without X and GTK: `monitor_outputs`, `current_workspace`, `read_client_list` and
`read_window_meta` are replaced, and the frames are computed by the pure geometry of `Grid`.
The main thing is checked: a workspace with a profile takes the zones of the profile, a workspace without a profile and
an unknown workspace — the general `[zones]`, a wrong workspace number — exit code 2, a zone outside the
set — exit code 3 with the list of the available ones, while the first line of `list` stays the way
the applet expects it.

Running:
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


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # a run of the form `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # zone-tiler and its modules
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_ws_test", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The general set: one zone `main` for half the screen.
BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\n"
)
# The workspace profile: its own zone names that the general set does not have.
PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nvideo = 0, 0, 1, 0.75\nside = 0, 0.75, 1, 0.25\n"
)
# Workspace 2 (in Cinnamon) is index 1 in EWMH.
WORKSPACES = "[workspaces]\n2 = cinema\n"
MONITOR = ("monitor-0", (0, 0, 1920, 1000))


class WorkspaceCliCase(unittest.TestCase):
    """A temporary config with a workspace profile and the replaced X dependencies."""

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
        # The monitor and the workspace are replaced: DISPLAY is not needed in the tests.
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
        """(code, stdout, stderr) — `main` in the process, without a subprocess and X."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.zt.main([*argv, "--conf", self.conf])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def grid_by_workspace(self, workspace):
        """Which grid a workspace gets — through the same functions as in the CLI."""
        return self.zt.grid_for_workspace(self.conf, workspace)


class WorkspaceArgTest(WorkspaceCliCase):
    """Parsing `--workspace` and the default value."""

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
    """`apply` computes the frame by the grid of the chosen workspace."""

    def test_profile_zone_on_mapped_workspace(self):
        # --workspace 1 is the second workspace (EWMH counts from zero), the cinema profile.
        code, out, _ = self.run_cli("apply", "video", "--workspace", "1", "--dry-run")
        self.assertEqual(code, 0)
        # video = 0, 0, 1, 0.75 of the 1920x1000 work area
        self.assertIn("video: x=0 y=0 1920x750", out)

    def test_general_zone_on_workspace_zero(self):
        code, out, _ = self.run_cli("apply", "main", "--workspace", "0", "--dry-run")
        self.assertEqual(code, 0)
        # main = half the width from the general [zones]
        self.assertIn("main: x=0 y=0 960x1000", out)

    def test_profile_zone_is_absent_on_general_workspace(self):
        # On a workspace without a profile the zone of the profile does not exist — a config error.
        code, _, err = self.run_cli("apply", "video", "--workspace", "0", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("no zone 'video'", err)
        self.assertIn("main", err)

    def test_unknown_zone_on_profile_workspace_lists_profile_zones(self):
        code, _, err = self.run_cli("apply", "main", "--workspace", "1", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("no zone 'main'", err)
        self.assertIn("workspace 2", err)
        self.assertIn("cinema", err)
        # the list of the available zones comes from the profile, not from the general set
        self.assertIn("side", err)
        self.assertIn("video", err)

    def test_sticky_workspace_keeps_general_zones(self):
        # A workspace without a profile (workspace 0) — the previous behaviour.
        code, out, _ = self.run_cli("apply", "main", "--workspace", "0", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("main:", out)


class CurrentWorkspaceTest(WorkspaceCliCase):
    """`--workspace current` and the absent argument follow the active workspace."""

    def test_current_uses_profile_of_active_workspace(self):
        with mock.patch.object(self.zt, "current_workspace", return_value=1):
            code, out, _ = self.run_cli("apply", "video", "--workspace", "current", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("1920x750", out)

    def test_default_is_current_workspace(self):
        # Without --workspace at all: the same active workspace, without X.
        with mock.patch.object(self.zt, "current_workspace", return_value=1):
            code, out, _ = self.run_cli("apply", "side", "--dry-run")
        self.assertEqual(code, 0)
        # side = 0, 0.75, 1, 0.25 -> y=750, h=250
        self.assertIn("side: x=0 y=750 1920x250", out)

    def test_current_without_display_falls_back_to_general(self):
        # current_workspace() returned None (no DISPLAY) — the general zones.conf.
        with mock.patch.object(self.zt, "current_workspace", return_value=None):
            code, out, _ = self.run_cli("apply", "main", "--workspace", "current", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("main: x=0 y=0 960x1000", out)

    def test_current_none_rejects_profile_zone(self):
        with mock.patch.object(self.zt, "current_workspace", return_value=None):
            code, _, err = self.run_cli("apply", "video", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("no zone 'video'", err)


class BadWorkspaceValueTest(WorkspaceCliCase):
    """A wrong `--workspace` value is an argument error (exit code 2)."""

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
    """`list` shows the zones of the workspace and names the profile."""

    def test_list_profile_workspace(self):
        code, out, _ = self.run_cli("list", "--workspace", "1")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        # The format of the first line is not broken: the applet parses it.
        self.assertTrue(lines[0].startswith("monitor 0 work area: 0,0 1920x1000"))
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
        self.assertEqual(lines[0], "monitor 0 work area: 0,0 1920x1000")
        self.assertIn("main", out)
        self.assertNotIn("video", out)
        self.assertNotIn("profile", out)

    def test_list_without_mapping_is_unchanged(self):
        # The config has no [workspaces] — the previous behaviour and the previous line.
        self.write_conf(BASE)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "monitor 0 work area: 0,0 1920x1000")
        self.assertIn("main", out)


class ArrangeAndStackWorkspaceTest(WorkspaceCliCase):
    """`arrange`/`stack` lay out with the zones of the chosen workspace's grid."""

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
            # the first free zone of the profile in file order — video
            self.assertIn("zone 'video'", out)
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
        self.assertIn("zone 'main'", out)

    def test_stack_unknown_zone_on_profile_workspace(self):
        code, _, err = self.run_cli("stack", "main", "--workspace", "1", "--dry-run")
        self.assertEqual(code, self.zt.EXIT_CONFIG)
        self.assertIn("video", err)


class KeysAndProfileUnaffectedTest(WorkspaceCliCase):
    """`keys` and `profile *` do not depend on the workspace."""

    def test_keys_are_read_from_file_not_profile(self):
        self.write_conf(BASE + WORKSPACES + "[keys]\nmain = <Super><Alt>1\n")
        code, out, _ = self.run_cli("keys", "--workspace", "1")
        self.assertEqual(code, 0)
        # [keys] is shared per file: the workspace profile does not replace it
        self.assertIn("main\t<Super><Alt>1", out)

    def test_profile_list_is_file_level(self):
        code, out, _ = self.run_cli("profile", "list")
        self.assertEqual(code, 0)
        self.assertIn("cinema", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ConfWorkspaceTest(WorkspaceCliCase):
    """`--workspace conf` — the grid of zones.conf without the workspace profiles.

    The editor needs this: the canvas shows the zones of `zones.conf` itself, so
    "check the zone" must compute the same grid even when the active workspace is
    mapped to a profile.
    """

    def test_spec_parses_conf(self):
        self.assertEqual(self.zt.workspace_spec("conf"), self.zt.WORKSPACE_CONF)
        self.assertEqual(self.zt.workspace_spec("CONF"), self.zt.WORKSPACE_CONF)

    def test_grid_is_general_even_for_mapped_workspace(self):
        # The active workspace is the second one (index 1) and it is mapped to the cinema profile.
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
        self.assertNotIn("profile", out)

    def test_apply_uses_general_zones(self):
        # The zone of the profile is not available on this workspace: the grid was taken from zones.conf.
        code, _out, err = self.run_cli(
            "apply", "video", "--workspace", "conf", "--dry-run"
        )
        self.assertEqual(code, 3)
        self.assertIn("main", err)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for moving a window into a neighbouring zone (`move`) and the `[move]` section.

Without an X server and GTK: the monitors, the window frame and the tiling are replaced, so
what is checked is exactly the logic - where the window moves from and to, what happens on the grid
edge and with a window outside the zones, that `--dry-run` moves nothing, and that `undo` gets a
history record. Separately, the parsing of the optional `[move]` section
(`zone_config.load_move_keys`: no section - the defaults, an unknown key - code 3),
`keys --move` and the fact that saving the zones from `zone_config` does not lose the section.

Run:
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

try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")
SETUP_HOTKEYS = os.path.join(ROOT, "setup-hotkeys.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402



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

# Two halves: they show both the move "from the left into the right one" and the grid edge.
TWO_HALVES = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
)
# A gap in the middle: a window in it falls into no zone.
GAP = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.4, 1\nright = 0.6, 0, 0.4, 1\n"
)
# The set of monitor HDMI-1: its own zone names, which the common set does not have.
WITH_HDMI = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    "[zones:HDMI-1]\nhdmi-left = 0, 0, 0.5, 1\nhdmi-right = 0.5, 0, 0.5, 1\n"
)


class MoveCase(unittest.TestCase):
    """A temporary config and replaced X dependencies: the test sets the window frame."""

    config = TWO_HALVES

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-move-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        self.write_conf(self.config)
        self.zt = load_tiler()
        self.frame = (50, 50, 100, 100)  # a window in the left zone
        self.applied = []  # (window, frame, label)
        self.history = []  # (kind of operation, records)
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

    # ---- replacing X

    def _apply(self, window, rect, label="", **_kwargs):
        self.applied.append((window, rect, label))

    def _snapshot(self, win, zone=None):
        return {"id": win, "frame": [1, 2, 3, 4], "wm_class": "", "zone": zone}

    def _remember(self, kind, records, quiet=False):
        self.history.append((kind, records))

    # ---- running

    def write_conf(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_cli(self, *argv):
        """(code, stdout, stderr) - `main` in the process, without a subprocess and X."""
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
    """Where the window moves: the neighbour by geometry, the grid edge, a window outside the zones."""

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
        self.frame = (700, 50, 100, 100)  # the right half
        code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.history, [])
        self.assertIn("has no neighbour to the right", err)
        self.assertIn("right", err)

    def test_quiet_hides_edge_message(self):
        self.frame = (700, 50, 100, 100)
        code, _out, err = self.run_cli("move", "right", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_window_outside_zones_takes_nearest_centre(self):
        # The gap 0.4..0.6: the centre of the window 0.52 is closer to the centre of the right zone (0.8).
        self.write_conf(GAP)
        self.frame = (470, 400, 100, 100)
        code, _out, err = self.run_cli("move", "left")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied, [(0x1234, (0, 0, 400, 1000), "left")])

    def test_window_outside_zones_on_edge_still_reports_no_neighbour(self):
        self.write_conf(GAP)
        self.frame = (470, 400, 100, 100)  # the nearest zone - the right one
        code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0)
        self.assertEqual(self.applied, [])
        self.assertIn("has no neighbour to the right", err)

    def test_window_on_monitor_uses_its_zone_set(self):
        self.write_conf(WITH_HDMI)
        self.frame = (1100, 100, 100, 100)  # the left half of HDMI-1
        with mock.patch.object(self.zt, "monitor_outputs", lambda: [MONITOR, HDMI]):
            code, _out, err = self.run_cli("move", "right")
        self.assertEqual(code, 0, err)
        expected = self.rect_of("hdmi-right", HDMI[1], monitor="HDMI-1")
        self.assertEqual(self.applied, [(0x1234, expected, "hdmi-right")])

    def test_monitor_flag_overrides_window_monitor(self):
        # The window is on the first monitor, while --monitor points at HDMI-1: the
        # set of HDMI-1 is taken, not the one where the window lies.
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
        self.assertIn("direction", err)
        self.assertEqual(self.applied, [])


class MoveFailuresTest(MoveCase):
    """Runtime errors: the window cannot be read, X is unavailable."""

    def test_unreadable_window_is_execution_error(self):
        def boom(win, extents):
            raise RuntimeError("нет такого окна")

        with mock.patch.object(self.zt, "read_frame", boom):
            code, _out, err = self.run_cli("move", "left")
        self.assertEqual(code, 1)
        self.assertIn("failed to read the frame of window 0x1234", err)
        self.assertIn("нет такого окна", err)
        self.assertEqual(self.applied, [])

    def test_no_display_is_execution_error(self):
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        r = self.run_cli_subprocess("move", "left", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("work area of the monitor", r.stderr)


class MoveGeometryTest(unittest.TestCase):
    """The pure `nearest_zone`/`move_zone_index` - without a config and X."""

    def setUp(self):
        self.zt = load_tiler()
        self.nav = self.zt.picker_nav()
        self.zones = [
            (0.0, 0.0, 0.5, 0.5),  # 0: left top
            (0.5, 0.0, 0.5, 0.5),  # 1: right top
            (0.0, 0.5, 0.5, 0.5),  # 2: left bottom
            (0.5, 0.5, 0.5, 0.5),  # 3: right bottom
        ]

    def test_nearest_zone_by_centre(self):
        self.assertEqual(self.zt.nearest_zone(self.zones, 0.05, 0.05), 0)
        self.assertEqual(self.zt.nearest_zone(self.zones, 0.9, 0.9), 3)
        self.assertIsNone(self.zt.nearest_zone([], 0.5, 0.5))

    def test_move_index_matches_picker_rule(self):
        # The neighbour is taken by the picker's rule, not by a copy of it: we compare with it.
        for direction in self.nav.DIRECTIONS:
            here, there = self.zt.move_zone_index(self.zones, direction, 0.25, 0.25)
            self.assertEqual(here, 0)
            self.assertEqual(there, self.nav.move_selection(self.zones, 0, direction))

    def test_point_in_gap_uses_nearest_zone(self):
        # A point outside all zones (past the bottom-right edge) - the nearest zone is taken.
        here, there = self.zt.move_zone_index(self.zones, "up", 1.05, 0.55)
        self.assertEqual(here, 3)  # the nearest centre - the right bottom
        self.assertEqual(there, 1)  # "up" from it - the right top

    def test_empty_set_gives_nothing(self):
        self.assertEqual(self.zt.move_zone_index([], "left", 0.5, 0.5), (None, None))


class MoveSectionTest(unittest.TestCase):
    """Parsing `[move]` in `zone_config` - without X and GTK."""

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
        self.assertIn("unknown direction", err.getvalue())
        self.assertIn("sideways", err.getvalue())

    def test_empty_binding_is_config_error(self):
        self.write(TWO_HALVES + "[move]\nleft =\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_move_keys(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("empty shortcut", err.getvalue())

    def test_section_is_not_a_foreign_section(self):
        # `[move]` is a section of its own: the editor must not carry it as a "foreign" one.
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
        # A comment inside the section also survives saving the zones.
        self.assertIn("# мои клавиши", text)

    def test_editor_roundtrip_keeps_move_section(self):
        # The way the editor saves: all the sets + the "foreign" sections in one call.
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
        # `profile use` rewrites zones.conf for the profile, but `[move]` is
        # a person's setting, not a layout: it must stay.
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
    """`zone-tiler.py keys --move` - the source of the combinations for setup-hotkeys."""

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
        self.assertIn("unknown direction", r.stderr)

    def test_keys_without_flag_work_as_before(self):
        r = self.run_cli("keys")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["left\t<Super><Alt>1"])


class HotkeyNamesTest(unittest.TestCase):
    """`setup-hotkeys`: the names of the combinations and the warning about a conflict with muffin.

    The `gsettings` themselves are not touched: `zone_pairs` and `push_tile_bindings`
    are replaced, what is checked is the assembly of the install plan.
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

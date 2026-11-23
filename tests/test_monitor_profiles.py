#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for binding a profile to a screen: `[monitors]` and the "whose profile" lookup.

No X and no GTK: both the screen map and the resolver live in the pure config layer.
What is checked is the main thing: a screen beats a desk, a desk beats the active config;
output names are compared case-insensitively (the parser lowercases the keys, as it does
for `[zones:<output>]`); the order of the section is kept, because the profile of the
**first** screen settles the conflict when merging in `profile use-monitors`; a missing
profile does not break the lookup — the caller does the fallback to the next level.

Running:
    python3 tests/test_monitor_profiles.py
    python3 -m unittest discover -s tests -v
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
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402



def load_tiler():
    """`zone-tiler.py` as a module: the merge and the `profile use-monitors` command live there."""
    spec = importlib.util.spec_from_file_location(
        "zone_tiler_monitor_profiles", os.path.join(ROOT, "zone-tiler.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()

BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1\n"
)


class MonitorMapTest(unittest.TestCase):
    """The `[monitors]` section: reading, writing and the limits of writing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-monitors-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_section_is_empty_map(self):
        self.write(BASE)
        self.assertEqual(zc.load_monitor_map(self.conf), {})
        # No file is also an empty map, not an error: the section is optional.
        self.assertEqual(zc.load_monitor_map(os.path.join(self.dir, "нет.conf")), {})

    def test_reads_in_file_order_and_lowercases_keys(self):
        # The order matters: the profile of the first screen settles the conflict when merging.
        self.write(BASE + "[monitors]\nHDMI-2 = cinema\nHDMI-1 = work\n")
        self.assertEqual(
            zc.load_monitor_map(self.conf), {"hdmi-2": "cinema", "hdmi-1": "work"}
        )

    def test_empty_profile_is_config_error(self):
        self.write(BASE + "[monitors]\nHDMI-1 =\n")
        with self.assertRaises(SystemExit) as ctx:
            zc.load_monitor_map(self.conf)
        self.assertEqual(ctx.exception.code, 3)

    def test_save_and_read_round_trip(self):
        self.write(BASE)
        zc.save_monitor_map(self.conf, {"HDMI-1": "work", "HDMI-2": "cinema"})
        text = open(self.conf, encoding="utf-8").read()
        self.assertIn("[monitors]\nHDMI-1 = work\nHDMI-2 = cinema", text)
        self.assertEqual(
            zc.load_monitor_map(self.conf), {"hdmi-1": "work", "hdmi-2": "cinema"}
        )

    def test_save_keeps_other_sections_and_sanitizes_profile(self):
        self.write(BASE + "[workspaces]\n1 = work\n\n[keys]\nmain = <Super><Alt>1\n")
        zc.save_monitor_map(self.conf, {"HDMI-2": "my cinema"})
        text = open(self.conf, encoding="utf-8").read()
        self.assertIn("[workspaces]\n1 = work", text)
        self.assertIn("[keys]\nmain = <Super><Alt>1", text)
        self.assertIn("HDMI-2 = my-cinema", text)

    def test_save_empty_map_removes_section(self):
        self.write(BASE + "[monitors]\nHDMI-1 = work\n")
        zc.save_monitor_map(self.conf, {})
        self.assertNotIn("[monitors]", open(self.conf, encoding="utf-8").read())

    def test_save_creates_file_with_the_section(self):
        path = os.path.join(self.dir, "new", "zones.conf")
        zc.save_monitor_map(path, {"eDP-1": "work"})
        self.assertEqual(zc.load_monitor_map(path), {"edp-1": "work"})

    def test_save_rejects_empty_names(self):
        for mapping in ({"": "work"}, {"HDMI-1": "  "}):
            with self.assertRaises(ValueError):
                zc.save_monitor_map(self.conf, mapping)


class ResolveProfileTest(unittest.TestCase):
    """The "whose profile" lookup: a screen beats a desk, a desk beats the active config."""

    def test_screen_wins_over_desk(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "HDMI-1", 0), "studio"
        )

    def test_desk_is_used_when_no_screen_binding(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "eDP-1", 0), "work"
        )

    def test_desk_code_is_translated_from_ewmh_zero_based(self):
        # In `[workspaces]` desk numbers start at 1, as in the Cinnamon UI, in EWMH at zero.
        self.assertIsNone(zc.resolve_profile({}, {1: "work"}, "eDP-1", 1))
        self.assertEqual(zc.resolve_profile({}, {2: "cinema"}, "eDP-1", 1), "cinema")

    def test_nothing_bound_means_active_config(self):
        self.assertIsNone(zc.resolve_profile({}, {}, "HDMI-1", 0))
        self.assertIsNone(zc.resolve_profile(None, None, None, None))

    def test_output_name_is_case_insensitive_and_trimmed(self):
        for output in ("hdmi-1", "HDMI-1", "Hdmi-1", " hdmi-1 "):
            self.assertEqual(
                zc.resolve_profile({"hdmi-1": "studio"}, {}, output), "studio"
            )

    def test_string_keys_of_desk_map_are_accepted(self):
        # The editor dialog and the tests may build a dict with string keys.
        self.assertEqual(zc.resolve_profile({}, {"2": "cinema"}, None, 1), "cinema")

    def test_unknown_screen_falls_back_to_the_desk(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "DP-3", 0), "work"
        )


class MergeMonitorProfilesTest(unittest.TestCase):
    """Merging screen profiles: the first screen settles the conflict, foreign rules are dropped."""

    @staticmethod
    def zone(name, binding=""):
        return zc.Zone(name, 0.0, 0.0, 1.0, 1.0, binding)

    def test_first_screen_sets_default_and_each_screen_gets_its_own_set(self):
        screens = [
            (
                "HDMI-1",
                "work",
                [self.zone("main")],
                ["firefox = main"],
                {"HDMI-1": [self.zone("wide")]},
                [],
            ),
            ("HDMI-2", "cinema", [self.zone("big")], ["mpv = big"], {}, []),
        ]
        plan = zt.merge_monitor_profiles(screens)
        # [zones] is the default set of the first screen, not of the second.
        self.assertEqual([z.name for z in plan["default"]], ["main"])
        # Each screen has its own profile set; for the first one it is [zones:HDMI-1].
        self.assertEqual([z.name for z in plan["monitor_sets"]["HDMI-1"]], ["wide"])
        self.assertEqual([z.name for z in plan["monitor_sets"]["HDMI-2"]], ["big"])
        self.assertEqual(plan["rules"], [("firefox", "main"), ("mpv", "big")])
        self.assertEqual(plan["dropped_rules"], [])
        self.assertEqual(plan["overrides"], [])

    def test_profile_without_default_zone_set_takes_its_screen_set(self):
        # A profile may consist of one [zones:<output>] (that is how the JSON import builds it).
        screens = [
            ("HDMI-1", "work", [], ["firefox = main"], {"HDMI-1": [self.zone("main")]}, [])
        ]
        plan = zt.merge_monitor_profiles(screens)
        self.assertEqual([z.name for z in plan["default"]], ["main"])
        self.assertEqual([z.name for z in plan["monitor_sets"]["HDMI-1"]], ["main"])

    def test_first_screen_wins_rule_and_key_conflicts(self):
        screens = [
            (
                "HDMI-1",
                "work",
                [self.zone("main", "<Super><Alt>1")],
                ["firefox = main"],
                {},
                [],
            ),
            (
                "HDMI-2",
                "cinema",
                [self.zone("main", "<Super><Alt>9"), self.zone("big")],
                ["firefox = big"],
                {},
                [],
            ),
        ]
        plan = zt.merge_monitor_profiles(screens)
        self.assertEqual(plan["rules"], [("firefox", "main")])
        # The losing keybinding must not override the first one in the written sets.
        seen = {
            zone.name: zone.binding
            for chunk in [plan["default"]] + list(plan["monitor_sets"].values())
            for zone in chunk
        }
        self.assertEqual(seen["main"], "<Super><Alt>1")
        self.assertTrue(any("firefox" in m and "main" in m for m in plan["overrides"]))
        self.assertTrue(any('zone key "main"' in m for m in plan["overrides"]))

    def test_rule_for_zone_outside_snapshot_is_dropped_with_reason(self):
        screens = [
            (
                "HDMI-1",
                "work",
                [self.zone("main")],
                ["firefox = ghost", "xterm = main"],
                {},
                ["steam = не трогать"],
            )
        ]
        plan = zt.merge_monitor_profiles(screens)
        # A rule for a non-existent zone would make the config invalid (code 3).
        self.assertEqual(plan["dropped_rules"], [("firefox", "ghost")])
        self.assertEqual(plan["rules"], [("xterm", "main")])
        self.assertEqual(plan["ignore"], [("steam", "не трогать")])

    def test_duplicate_rule_and_ignore_are_not_reported_as_conflicts(self):
        screens = [
            ("HDMI-1", "work", [self.zone("main")], ["firefox = main"], {}, ["steam = a"]),
            ("HDMI-2", "cinema", [self.zone("main")], ["firefox = main"], {}, ["steam = a"]),
        ]
        plan = zt.merge_monitor_profiles(screens)
        self.assertEqual(plan["rules"], [("firefox", "main")])
        self.assertEqual(plan["ignore"], [("steam", "a")])
        self.assertEqual(plan["overrides"], [])


class UseMonitorsTest(unittest.TestCase):
    """`profile use-monitors`: a snapshot of the screen profiles into the active zones.conf."""

    CONF = """# шапка
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
side = 0.5, 0, 0.5, 1

[rules]
old = main

[monitors]
HDMI-1 = work
HDMI-2 = cinema

[daemon]
reflow = neighbors

[custom]
key = value
"""

    WORK = """[zones]
main = 0, 0, 0.5, 1
side = 0.5, 0, 0.5, 1

[zones:HDMI-1]
wide = 0, 0, 1, 1

[rules]
firefox = side

[ignore]
steam = не трогать

[keys]
main = <Super><Alt>1
side = <Super><Alt>2
"""

    CINEMA = """[zones]
big = 0, 0, 1, 1

[rules]
firefox = big
mpv = big

[keys]
big = <Super><Alt>3
"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-use-monitors-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        self.templates = os.path.join(self.dir, "templates")
        os.makedirs(self.templates)
        self.write_conf(self.CONF)
        self.write_profile("work", self.WORK)
        self.write_profile("cinema", self.CINEMA)

    def write_conf(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def write_profile(self, name, text):
        with open(os.path.join(self.templates, name + ".conf"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def read_conf(self):
        with open(self.conf, encoding="utf-8") as fh:
            return fh.read()

    def run_cli(self, *argv, hotkeys=False):
        """(code, stdout, stderr) in-process — without X and without setup-hotkeys.py."""
        out, err = io.StringIO(), io.StringIO()
        args = ["profile", "use-monitors", *argv, "--conf", self.conf]
        if not hotkeys:
            args.append("--no-hotkeys")
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = zt.main(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_writes_default_and_per_screen_sets(self):
        code, _out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        text = self.read_conf()
        # [zones] is the default set of the first screen's profile, [zones:<output>] of each screen.
        self.assertIn("[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1", text)
        self.assertIn("[zones:HDMI-1]\nwide = 0, 0, 1, 1", text)
        self.assertIn("[zones:HDMI-2]\nbig = 0, 0, 1, 1", text)
        grid = zt.Grid(self.conf)
        self.assertEqual(list(grid.zones), ["main", "side"])
        self.assertEqual(list(grid.zones_for("HDMI-1")), ["wide"])
        self.assertEqual(list(grid.zones_for("HDMI-2")), ["big"])

    def test_merges_rules_ignore_and_keys(self):
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        text = self.read_conf()
        self.assertIn("[rules]\nfirefox = side\nmpv = big", text)
        self.assertIn("[ignore]\nsteam = не трогать", text)
        self.assertIn("[keys]\nmain = <Super><Alt>1\nside = <Super><Alt>2", text)
        # An old rule of the active config is replaced, not accumulated.
        self.assertNotIn("old = main", text)
        self.assertIn("overridden", out)

    def test_keeps_other_sections_verbatim(self):
        self.run_cli()
        text = self.read_conf()
        self.assertIn("[daemon]\nreflow = neighbors", text)
        self.assertIn("[custom]\nkey = value", text)
        self.assertIn("[monitors]\nHDMI-1 = work\nHDMI-2 = cinema", text)

    def test_missing_profile_is_skipped_with_warning_and_row_kept(self):
        os.remove(os.path.join(self.templates, "cinema.conf"))
        code, _out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('profile "cinema" is not on disk', err)
        self.assertIn("monitor HDMI-2 skipped", err)
        text = self.read_conf()
        # The line stays and works once the profile is back; there is no snapshot for the screen.
        self.assertIn("HDMI-2 = cinema", text)
        self.assertNotIn("[zones:HDMI-2]", text)

    def test_rule_for_missing_zone_is_skipped_with_warning(self):
        self.write_profile(
            "work",
            "[zones]\nmain = 0, 0, 1, 1\n[rules]\nfirefox = ghost\nxterm = main\n",
        )
        os.remove(os.path.join(self.templates, "cinema.conf"))
        code, _out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('rule "firefox = ghost" skipped', err)
        text = self.read_conf()
        self.assertIn("[rules]\nxterm = main", text)
        self.assertNotIn("ghost", text)
        # The config stays readable: a rule for a non-existent zone gives code 3 in Grid.
        self.assertEqual(list(zt.Grid(self.conf).rules), [("xterm", "main")])

    def test_key_conflict_keeps_first_screen_and_reports_it(self):
        self.write_profile(
            "cinema",
            "[zones]\nmain = 0, 0, 1, 1\n[keys]\nmain = <Super><Alt>9\n",
        )
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertIn('zone key "main"', out)
        self.assertIn("<Super><Alt>1", out)
        keys = self.read_conf().split("[keys]", 1)[1]
        self.assertIn("main = <Super><Alt>1", keys)
        self.assertNotIn("<Super><Alt>9", keys)

    def test_idempotent_second_run_changes_nothing(self):
        self.run_cli()
        first = self.read_conf()
        code, _out, _err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_conf(), first)

    def test_dry_run_prints_would_be_config_and_writes_nothing(self):
        before = self.read_conf()
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_conf(), before)
        self.assertIn("[zones:HDMI-1]", out)
        self.assertIn("[zones:HDMI-2]", out)
        self.assertIn("[rules]\nfirefox = side", out)

    def test_quiet_suppresses_progress_but_not_warnings(self):
        code, out, err = self.run_cli("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        os.remove(os.path.join(self.templates, "cinema.conf"))
        code, out, err = self.run_cli("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("is not on disk", err)

    def test_explicit_profile_name_is_usage_error(self):
        code, _out, err = self.run_cli("work")
        self.assertEqual(code, 2)
        self.assertIn("takes no profile name", err)

    def test_no_monitors_section_leaves_config_alone(self):
        self.write_conf(BASE)
        before = self.read_conf()
        code, out, _err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("has no monitors", out)
        self.assertEqual(self.read_conf(), before)

    def test_missing_config_is_config_error(self):
        # Nowhere to write it: a typo in --conf is a config error (code 3), not "nothing to change".
        self.conf = os.path.join(self.dir, "нет.conf")
        code, _out, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("config not found", err)

    def test_empty_profile_name_is_config_error(self):
        self.write_conf(BASE + "[monitors]\nHDMI-1 =\n")
        code, _out, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("empty profile name", err)

    def test_hotkeys_registered_after_write_unless_disabled(self):
        with mock.patch.object(zt, "_run_hotkeys") as called:
            self.run_cli(hotkeys=True)
        called.assert_called_once_with(self.conf, quiet=False)
        with mock.patch.object(zt, "_run_hotkeys") as called:
            self.run_cli()
        called.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests of the "only this zone" mode's surface: the CLI, `[focus]`, the keybinding.

The mode lives in the daemon (`ipc_focus`); what is checked here is what surrounds it:
the `gridsnap-ctl focus` command (which request goes out, what counts as an argument
error and with which code), parsing of the optional `[focus]` section in
`zone_config` (no section — the mode is not configured, a foreign key and an empty `zone` —
code 3) and the third pair in the `setup-hotkeys.py` plan.

No X server and no GTK: `send_command` in the client and `zone_pairs` in
`setup-hotkeys.py` are replaced with stubs, so the real daemon and
the tiler are not started.

Running:
    python3 tests/test_focus_config.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.machinery
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
CTL = os.path.join(ROOT, "gridsnap-ctl")
SETUP_HOTKEYS = os.path.join(ROOT, "setup-hotkeys.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402



def load_ctl():
    """`gridsnap-ctl` is a file without `.py`: it is loaded through `SourceFileLoader`."""
    loader = importlib.machinery.SourceFileLoader("gridsnap_ctl_focus_test", CTL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_setup_hotkeys():
    spec = importlib.util.spec_from_file_location(
        "setup_hotkeys_focus_test", SETUP_HOTKEYS
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1\n"
)


class FocusSectionTest(unittest.TestCase):
    """Parsing `[focus]` in `zone_config` — without X and GTK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-focus-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_is_not_configured(self):
        self.assertIsNone(zc.load_focus_keys(os.path.join(self.dir, "нет.conf")))

    def test_missing_section_is_not_configured(self):
        self.write(BASE + "[move]\nleft = <Super>h\n")
        self.assertIsNone(zc.load_focus_keys(self.path))

    def test_key_without_zone(self):
        self.write(BASE + "[focus]\nkey = <Super><Alt>f\n")
        self.assertEqual(
            zc.load_focus_keys(self.path), {"key": "<Super><Alt>f", "zone": None}
        )

    def test_key_and_zone(self):
        self.write(BASE + "[focus]\nkey = <Super><Alt>f\nzone = main\n")
        self.assertEqual(
            zc.load_focus_keys(self.path), {"key": "<Super><Alt>f", "zone": "main"}
        )

    def test_values_are_stripped(self):
        self.write(BASE + "[focus]\nkey =   <Super>f  \nzone =   side   \n")
        self.assertEqual(
            zc.load_focus_keys(self.path), {"key": "<Super>f", "zone": "side"}
        )

    def test_empty_key_is_not_configured(self):
        self.write(BASE + "[focus]\nkey =\nzone = main\n")
        self.assertIsNone(zc.load_focus_keys(self.path))

    def test_unknown_key_is_config_error(self):
        self.write(BASE + "[focus]\nkey = <Super>f\nzona = main\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_focus_keys(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("unknown key", err.getvalue())
        self.assertIn("zona", err.getvalue())
        self.assertIn("key, zone", err.getvalue())

    def test_empty_zone_is_config_error(self):
        self.write(BASE + "[focus]\nkey = <Super>f\nzone =\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_focus_keys(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        self.assertIn("empty zone value", err.getvalue())

    def test_section_is_foreign_and_survives_saving(self):
        # `[focus]` is not in RESERVED_SECTIONS: the editor carries it over verbatim,
        # so saving the layout must not lose it.
        self.write(BASE + "[focus]\nkey = <Super>f\nzone = main\n")
        self.assertIn("focus", zc.load_extra_sections(self.path))
        zc.save_zones(
            self.path, zc.load_zones(self.path), zc.load_extra_sections(self.path)
        )
        self.assertEqual(
            zc.load_focus_keys(self.path), {"key": "<Super>f", "zone": "main"}
        )


class FocusHotkeyPlanTest(unittest.TestCase):
    """The third pair in `setup-hotkeys.planned`: `focus` after the zones and `move`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-focus-hotkeys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")
        self.mod = load_setup_hotkeys()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def plan(self, text):
        self.write(text)
        pairs = {
            (): [("main", "<Super><Alt>1")],
            ("--move",): [("left", "<Super><Alt>h")],
        }
        with mock.patch.object(
            self.mod, "zone_pairs", lambda conf=None, *flags: pairs[flags]
        ), mock.patch.object(
            self.mod.zone_config, "load_overlay_key", lambda path: None
        ):
            return self.mod.planned(self.path)

    def test_focus_pair_goes_last_with_zone(self):
        items = self.plan(BASE + "[focus]\nkey = <Super><Alt>f\nzone = main\n")
        self.assertEqual([name for name, _b, _c in items], ["main", "move left", "focus"])
        name, binding, command = items[2]
        self.assertEqual((name, binding), ("focus", "<Super><Alt>f"))
        self.assertIn(" focus main --quiet", command)
        self.assertIn(f"--conf {self.path}", command)
        self.assertTrue(command.startswith(sys.executable + " "), command)

    def test_focus_pair_without_zone_has_no_zone_argument(self):
        items = self.plan(BASE + "[focus]\nkey = <Super><Alt>f\n")
        command = items[2][2]
        self.assertIn(f" {os.path.join(ROOT, 'zone-tiler.py')} focus --quiet", command)
        self.assertNotIn(" focus main", command)

    def test_no_section_means_no_focus_pair(self):
        items = self.plan(BASE)
        self.assertEqual([name for name, _b, _c in items], ["main", "move left"])

    def test_empty_binding_means_no_focus_pair(self):
        items = self.plan(BASE + "[focus]\nkey =\nzone = main\n")
        self.assertEqual([name for name, _b, _c in items], ["main", "move left"])

    def test_hotkey_name_keeps_zone_tiler_prefix(self):
        # The pair name goes into `zone-tiler: focus` — under the same prefix as
        # the others, otherwise `--remove` and `drop_owned` will not clean it up.
        items = self.plan(BASE + "[focus]\nkey = <Super>f\n")
        self.assertTrue((self.mod.NAME_PREFIX + items[2][0]).startswith("zone-tiler: "))


class FocusRequestTest(unittest.TestCase):
    """`gridsnap-ctl focus`: the request to the daemon and argument errors (code 2)."""

    def setUp(self):
        self.ctl = load_ctl()

    def run_ctl(self, *argv):
        """`(code, request, stderr)`: `send_command` is replaced, there is no daemon."""
        sent = {}

        def fake_send(request, path, timeout=0):
            sent.update(request)
            return {"ok": True}

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(self.ctl, "send_command", fake_send):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = self.ctl.main(list(argv))
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        return code, sent, err.getvalue()

    def test_zone_request(self):
        code, sent, _err = self.run_ctl("focus", "main")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "focus", "zone": "main"})

    def test_off_request_needs_no_zone(self):
        code, sent, _err = self.run_ctl("focus", "--off")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "focus", "off": True})

    def test_off_with_zone_is_usage_error(self):
        code, sent, err = self.run_ctl("focus", "--off", "main")
        self.assertEqual(code, 2)
        self.assertEqual(sent, {})
        self.assertIn("--off", err)

    def test_focus_without_zone_collapses_others(self):
        # No zone and no `--off` means "collapse everything except this window": the window
        # stays in place, and the request has neither `zone` nor `off`.
        code, sent, _err = self.run_ctl("focus")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "focus"})

    def test_monitor_is_usage_error_for_focus(self):
        code, sent, err = self.run_ctl("focus", "main", "--monitor", "1")
        self.assertEqual(code, 2)
        self.assertEqual(sent, {})
        self.assertIn("--monitor", err)

    def test_window_is_passed_as_int(self):
        code, sent, _err = self.run_ctl("focus", "main", "--window", "0x1a")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "focus", "zone": "main", "window": 26})

    def test_window_only_request(self):
        code, sent, _err = self.run_ctl("focus", "--window", "0x1a")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "focus", "window": 26})

    def test_other_commands_are_unchanged(self):
        self.assertEqual(self.run_ctl("status")[1], {"cmd": "status"})
        self.assertEqual(self.run_ctl("reload")[1], {"cmd": "reload"})
        code, sent, _err = self.run_ctl("apply", "side", "--monitor", "1")
        self.assertEqual(code, 0)
        self.assertEqual(sent, {"cmd": "apply", "zone": "side", "monitor": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)

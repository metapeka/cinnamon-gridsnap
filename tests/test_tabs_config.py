#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests of the tab "surface" in a zone: `[tabs]`, the keybinding, the CLI.

Tabs live in the daemon (`zone_tabs` is the wave's contract), and what is checked here is
what surrounds them: parsing of the optional `[tabs]` section in `zone_config` (no
section — the strip defaults, garbage in `height` and a foreign key — code 3), the
`tabs-next` pair in the `setup-hotkeys.py` plan, the removal of the cascade (`--offset` is no longer
a flag but an argument error) and the `zone-tiler.py tabs-next` command: which request
goes to the daemon and what counts as an error.

No X server and no GTK: `zone_pairs` in `setup-hotkeys.py` and `daemon_request` in
the tiler are replaced with stubs, so a real daemon and display are not needed.

Running:
    python3 tests/test_tabs_config.py
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
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")
SETUP_HOTKEYS = os.path.join(ROOT, "setup-hotkeys.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402
import zone_tabs  # noqa: E402



def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_tabs_conf_test", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_setup_hotkeys():
    spec = importlib.util.spec_from_file_location(
        "setup_hotkeys_tabs_conf_test", SETUP_HOTKEYS
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1\n"
)


class TabsSectionTest(unittest.TestCase):
    """Parsing `[tabs]` in `zone_config` — without X and GTK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-tabs-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_gives_strip_defaults(self):
        self.assertEqual(
            zc.load_tabs_config(os.path.join(self.dir, "нет.conf")),
            {"key": None, "height": zone_tabs.STRIP_HEIGHT},
        )

    def test_missing_section_gives_strip_defaults(self):
        self.write(BASE + "[focus]\nkey = <Super>f\n")
        self.assertEqual(
            zc.load_tabs_config(self.path),
            {"key": None, "height": zone_tabs.STRIP_HEIGHT},
        )

    def test_key_only(self):
        self.write(BASE + "[tabs]\nkey = <Super><Alt>g\n")
        self.assertEqual(
            zc.load_tabs_config(self.path),
            {"key": "<Super><Alt>g", "height": zone_tabs.STRIP_HEIGHT},
        )

    def test_height_only(self):
        # There is no key — the section is still read: the strip height is needed without one too.
        self.write(BASE + "[tabs]\nheight = 30\n")
        self.assertEqual(zc.load_tabs_config(self.path), {"key": None, "height": 30})

    def test_key_and_height_are_stripped(self):
        self.write(BASE + "[tabs]\nkey =   <Super>g  \nheight =  28  \n")
        self.assertEqual(
            zc.load_tabs_config(self.path), {"key": "<Super>g", "height": 28}
        )

    def test_empty_key_keeps_height(self):
        self.write(BASE + "[tabs]\nkey =\nheight = 26\n")
        self.assertEqual(zc.load_tabs_config(self.path), {"key": None, "height": 26})

    def test_unknown_key_is_config_error(self):
        self.write(BASE + "[tabs]\nkey = <Super>g\nhieght = 24\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_tabs_config(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        message = err.getvalue()
        self.assertIn("unknown key", message)
        self.assertIn("hieght", message)
        self.assertIn("key, height", message)

    def test_garbage_height_is_config_error(self):
        # A strip of zero height is not drawn: silently accepting garbage would leave
        # a person without visible tabs and without an explanation.
        for value in ("abc", "0", "-5", "1.5", ""):
            with self.subTest(height=value):
                self.write(BASE + f"[tabs]\nheight = {value}\n")
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(
                    SystemExit
                ) as caught:
                    zc.load_tabs_config(self.path)
                self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
                self.assertIn("height", err.getvalue())

    def test_section_is_foreign_and_survives_saving(self):
        # `[tabs]` is not in RESERVED_SECTIONS: the editor carries it over verbatim,
        # so saving the layout must not lose it.
        self.write(BASE + "[tabs]\nkey = <Super>g\nheight = 28\n")
        self.assertIn("tabs", zc.load_extra_sections(self.path))
        zc.save_zones(
            self.path, zc.load_zones(self.path), zc.load_extra_sections(self.path)
        )
        self.assertEqual(
            zc.load_tabs_config(self.path), {"key": "<Super>g", "height": 28}
        )


class TabsHotkeyPlanTest(unittest.TestCase):
    """The `tabs-next` pair in `setup-hotkeys.planned`: after the zones, `move` and `focus`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-tabs-hotkeys-")
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
        ):
            return self.mod.planned(self.path)

    def test_tabs_pair_goes_last_with_quiet_command(self):
        items = self.plan(BASE + "[tabs]\nkey = <Super><Alt>g\n")
        self.assertEqual(
            [name for name, _b, _c in items], ["main", "move left", "tabs-next"]
        )
        name, binding, command = items[2]
        self.assertEqual((name, binding), ("tabs-next", "<Super><Alt>g"))
        self.assertIn(f" {os.path.join(ROOT, 'zone-tiler.py')} tabs-next --quiet", command)
        self.assertIn(f"--conf {self.path}", command)
        self.assertTrue(command.startswith(sys.executable + " "), command)

    def test_no_section_means_no_tabs_pair(self):
        items = self.plan(BASE)
        self.assertEqual([name for name, _b, _c in items], ["main", "move left"])

    def test_empty_key_means_no_tabs_pair(self):
        items = self.plan(BASE + "[tabs]\nkey =\nheight = 24\n")
        self.assertEqual([name for name, _b, _c in items], ["main", "move left"])

    def test_height_without_key_means_no_tabs_pair(self):
        items = self.plan(BASE + "[tabs]\nheight = 24\n")
        self.assertEqual([name for name, _b, _c in items], ["main", "move left"])

    def test_hotkey_name_keeps_zone_tiler_prefix(self):
        items = self.plan(BASE + "[tabs]\nkey = <Super>g\n")
        self.assertTrue((self.mod.NAME_PREFIX + items[2][0]).startswith("zone-tiler: "))


class TabsCliTest(unittest.TestCase):
    """`zone-tiler.py`: `--offset` is an argument error, `tabs-next` is a request to the daemon."""

    def setUp(self):
        self.zt = load_tiler()

    def run_main(self, *argv):
        """(code, stdout, stderr) — `main` in-process, without a subprocess and X."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.zt.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_offset_is_argument_error(self):
        # There is no cascade any more: the old flag must not silently do nothing.
        code, _out, err = self.run_main("--offset", "20", "list")
        self.assertEqual(code, 2)
        self.assertIn("--offset", err)

    def test_help_lists_tabs_next_and_no_offset(self):
        r = subprocess.run(
            [sys.executable, TILER, "--help"], text=True, capture_output=True
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("tabs-next", r.stdout)
        self.assertIn("[tabs]", r.stdout)
        self.assertNotIn("--offset", r.stdout)
        self.assertNotIn("cascade", r.stdout)

    def test_tabs_next_sends_contract_request(self):
        sent = {}

        def fake_request(request, timeout=0):
            sent.update(request)
            return {"ok": True, "window": 0x2A}

        with mock.patch.object(self.zt, "daemon_request", fake_request):
            code, out, err = self.run_main("tabs-next")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sent, zone_tabs.tabs_next_command())
        self.assertEqual(sent, {"cmd": "tabs-next"})
        self.assertIn("42", out)

    def test_tabs_next_quiet_prints_nothing(self):
        with mock.patch.object(
            self.zt, "daemon_request", lambda request, timeout=0: {"ok": True, "window": 1}
        ):
            code, out, err = self.run_main("tabs-next", "--quiet")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_tabs_next_single_tab_says_there_is_nothing_to_switch(self):
        with mock.patch.object(
            self.zt, "daemon_request", lambda request, timeout=0: {"ok": True, "window": None}
        ):
            code, out, _err = self.run_main("tabs-next")
        self.assertEqual(code, 0)
        self.assertIn("nothing to switch", out)

    def test_tabs_next_without_daemon_is_code_1(self):
        # No daemon — that is not a crash but a clear message and code 1.
        with mock.patch.object(self.zt, "daemon_request", lambda request, timeout=0: None):
            code, out, err = self.run_main("tabs-next", "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("the daemon is not running", err)

    def test_tabs_next_refusal_is_code_1(self):
        response = {"ok": False, "error": "zone not found"}
        with mock.patch.object(
            self.zt, "daemon_request", lambda request, timeout=0: response
        ):
            code, _out, err = self.run_main("tabs-next")
        self.assertEqual(code, 1)
        self.assertIn("the daemon refused: zone not found", err)

    def test_tabs_next_needs_no_conf(self):
        # The command reads neither config nor grid: the daemon picks the zone by the window.
        with mock.patch.object(
            self.zt, "daemon_request", lambda request, timeout=0: {"ok": True, "window": 1}
        ):
            code, _out, err = self.run_main(
                "tabs-next", "--quiet", "--conf", "/nonexistent/definitely.conf"
            )
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the GridSnap daemon — no X server and no GTK.

`zone-daemon.py` is loaded through importlib (the file name has a hyphen), but the
loading itself does not open X: the event loop lives in `serve()` and is called only from
the CLI. The reaction to events is checked by injecting `Daemon` dependencies.

Covered: placement by rule when a window appears in `_NET_CLIENT_LIST`, ignoring
override_redirect and dialogs, protection against a repeated appearance, an empty WM_CLASS,
tolerant reading of `[daemon]`, `read_client_list` (parsing xprop), reconciliation of
the list (adding/removing, a repeat without re-reading, stability on
iconify), reflow=off/neighbors, returning an expanded zone when a
new window takes the freed one, choosing the monitor of a window (multiple monitors),
wiring of PropertyNotify (only `_NET_CLIENT_LIST`) and the `serve()` loop on a fake X11.

Workspaces: the grid comes from the workspace profile (`[workspaces]`), occupancy and
reflow do not cross a workspace, windows of invisible workspaces do not move, and
`_NET_CURRENT_DESKTOP` changes only `self.workspace`.

“Only this zone” mode: entering/`off`/toggle, iconifying only windows of the same
workspace and monitor (exceptions and manually iconified ones are skipped), deiconifying on exit with
the target activated last, dropping the mode when its target disappears and on a workspace change,
iconifying new windows of the same workspace/monitor and the `focus` field in `status`.

Tabs in a zone: the tab frame of the second window and the return to the whole zone for the last one,
switching `tabs-next` in a ring and its empty cases, `tabs-switch` with a foreign
window, `focus` with tabs in the target zone (and with a window that a rule placed
there already during the mode), zone occupancy for reflow while at least one
tab remains in it, the snapshot for the strip into the replaced process (including on leaving
the mode) and the stack memory that counts a tab frame as “a window in a zone”.
Normalising the stacks gives the whole zone back to the single remaining tab — both after
the neighbour was dragged away by hand and after a daemon restart; moving the holder by grabbing a tab
drops the `filled` record of the covered zone. A failed strip start (no GTK — the script
fails on import) gives `failed`, while the death of an already running strip — a restart.

Binding a profile to a screen: a window is placed by the zones of its own screen's profile
(`[monitors]` beats `[workspaces]`), the rules and `[ignore]` come from that same
profile, and a missing binding or profile file falls back to the next
level with a log entry; `status` counts the zones by the allowed profiles.
Autostart is checked separately: `install-daemon.sh` writes two entries —
`gridsnap-daemon.desktop` and `gridsnap-monitors.desktop` (here —
`profile use-monitors --quiet`), and `--list`/`--remove` see both.

Running:
    python3 tests/test_daemon.py
"""

import contextlib
import ctypes
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # zone-tiler and its modules
DAEMON = os.path.join(ROOT, "zone-daemon.py")


def load_daemon():
    spec = importlib.util.spec_from_file_location("zone_daemon_under_test", DAEMON)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zd = load_daemon()

WA = (0, 0, 1000, 1000)
WA1 = (1000, 0, 800, 600)

# Two neighbouring zones: left touches right along a vertical edge of length 1.
TWO_ZONES = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "left = 0, 0, 0.5, 1\n"
    "right = 0.5, 0, 0.5, 1\n"
    "[rules]\n"
    "firefox* = left\n"
    "chromium* = right\n"
)
ONE_RULE = TWO_ZONES.replace("chromium* = right\n", "")


def filled_key(wa, zone, workspace=None):
    """The coverage key in `Daemon.filled`: (workspace, work area, zone).

    Coverage of an expanded neighbour lives on a specific workspace and monitor: the same
    zone on different workspaces/screens — different records. `workspace=None` —
    a sticky window or an unknown active workspace.
    """
    return (workspace, wa, zone)


# Expected XPropertyEvent offsets from Xlib.h on LP64 (the same ABI as
# `zd.XPropertyEvent`). Hard-coded as numbers so the test catches a drift from the ABI
# instead of repeating the same structure as the code under test.
XPROPERTY_EVENT_OFFSETS = {
    "type": 0,
    "serial": 8,
    "send_event": 16,
    "display": 24,
    "window": 32,
    "atom": 40,
    "time": 48,
    "state": 56,
}
XPROPERTY_EVENT_SIZE = 64


def property_buffer(atom, window=0x2):
    """An XEvent buffer with an XPropertyEvent for PropertyNotify.

    It is built from raw bytes using the expected Xlib.h offsets (LP64) rather than from a
    copy of the structure in the module under test — otherwise the test would repeat its bugs.
    """
    raw = bytearray(ctypes.sizeof(zd.EVENT_BUFFER))
    struct.pack_into("<i", raw, XPROPERTY_EVENT_OFFSETS["type"], zd.PROPERTY_NOTIFY)
    struct.pack_into("<Q", raw, XPROPERTY_EVENT_OFFSETS["serial"], 0x1234)
    struct.pack_into("<i", raw, XPROPERTY_EVENT_OFFSETS["send_event"], 0)
    struct.pack_into("<Q", raw, XPROPERTY_EVENT_OFFSETS["display"], 0xDEAD)
    struct.pack_into("<Q", raw, XPROPERTY_EVENT_OFFSETS["window"], window)
    struct.pack_into("<Q", raw, XPROPERTY_EVENT_OFFSETS["atom"], atom)
    struct.pack_into("<Q", raw, XPROPERTY_EVENT_OFFSETS["time"], 123)
    struct.pack_into("<i", raw, XPROPERTY_EVENT_OFFSETS["state"], 0)
    return zd.EVENT_BUFFER.from_buffer_copy(bytes(raw))


class DaemonTestCase(unittest.TestCase):
    """Common scaffolding of the daemon scenarios: no X, with apply/metadata/workspaces replaced.

    `self.current` is the active workspace, `self.workspaces` the workspace of each window;
    the workspace readers are replaced, so the tests do not depend on a live `xprop` or on
    which workspace is open on the build machine.
    """

    # The window type of a dialog: auxiliary, the daemon neither moves nor iconifies it.
    DIALOG = {"_NET_WM_WINDOW_TYPE_DIALOG"}

    def setUp(self):
        self.applied = []
        self.metas = {}
        self.reads = []
        self.centers = {}
        # The actual window frames (for the tab snapshot): window -> (x, y, w, h).
        self.rects = {}
        self.current = None
        self.workspaces = {}
        # Injections of the “only this zone” mode: what was iconified/deiconified and which
        # client list the daemon sees. `iconic` is the replacement for `window_iconic`.
        self.iconified = []
        self.iconify_calls = []
        self.deiconified = []
        self.deiconify_calls = []
        self.iconic = set()
        self.clients = []

    # -- scaffolding

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def conf_dir(self):
        """An empty temporary GridSnap directory (for `zones.conf` and `templates/`)."""
        directory = tempfile.mkdtemp(prefix="gridsnap-daemon-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def daemon(self, text=ONE_RULE, reflow=None, directory=None):
        """A daemon without X; `directory` — a ready GridSnap directory with `templates/`."""
        if reflow is not None:
            text = text + f"[daemon]\nreflow = {reflow}\n"
        if directory is None:
            path = self.conf(text)
        else:
            path = self.write(os.path.join(directory, "zones.conf"), text)
        return zd.Daemon(
            path,
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            iconify=self.iconify,
            deiconify=self.deiconify,
            read_clients=lambda: self.clients,
        )

    def multi_daemon(
        self,
        text=TWO_ZONES,
        reflow=None,
        monitors=(WA, WA1),
        outputs=None,
        directory=None,
    ):
        """A daemon with two monitors; the monitor of a window — by self.centers.

        `outputs` — the output names (for `[monitors]`), `directory` — a ready
        GridSnap directory with `templates/` (otherwise — a temporary `zones.conf`).
        """
        if reflow is not None:
            text = text + f"[daemon]\nreflow = {reflow}\n"
        if directory is None:
            path = self.conf(text)
        else:
            path = self.write(os.path.join(directory, "zones.conf"), text)
        return zd.Daemon(
            path,
            apply=self.apply,
            read_meta=self.read_meta,
            monitors=lambda: list(monitors),
            monitor_outputs=lambda: list(outputs or []),
            window_center=lambda win: self.centers[win],
            read_rect=lambda win: self.rects.get(win),
            # The active window is not read from X: there is none in the tests.
            active_window=lambda: None,
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            iconify=self.iconify,
            deiconify=self.deiconify,
            read_clients=lambda: self.clients,
        )

    def apply(self, win, rect, label="", quiet=False):
        self.applied.append((win, rect, label, quiet))

    def iconify(self, win, x11=None, quiet=True):
        """Instead of `tiler.iconify`: remember the window and count it iconified (as a WM does)."""
        self.iconified.append(win)
        self.iconify_calls.append((win, x11, quiet))
        self.iconic.add(win)

    def deiconify(self, win, x11=None):
        """Instead of `tiler.deiconify`: remember the window and count it visible (as a WM does)."""
        self.deiconified.append(win)
        self.deiconify_calls.append((win, x11))
        self.iconic.discard(win)

    def read_meta(self, win):
        self.reads.append(win)
        return self.metas[win]

    def meta(self, wm_class="firefox-esr", wm_title="", window_type=()):
        return zd.Meta(wm_class, wm_title, frozenset(window_type))


class TestDaemonEvents(DaemonTestCase):
    # -- MapNotify

    def test_rule_match_applies_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.windows, {0x1: "left"})

    def test_title_regex_rule_applies(self):
        text = (
            "[grid]\ncols = 1.0\nrows = 1.0\n"
            "[zones]\nleft = 0, 0, 0.5, 1\n"
            "[rules]\nre:docs\\.google\\.com = left\n"
        )
        self.metas[0x1] = self.meta("chromium", "Docs - docs.google.com")
        d = self.daemon(text)
        d.on_map(0x1)
        self.assertEqual(self.applied[0][2], "left")

    def test_override_redirect_ignored(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.on_map(0x1, override_redirect=True)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.reads, [])  # the metadata was not even read
        self.assertEqual(d.windows, {})

    def test_dialog_window_type_ignored(self):
        self.metas[0x1] = self.meta(
            "firefox-esr", window_type=self.DIALOG
        )
        d = self.daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_splash_tooltip_utility_ignored(self):
        for wtype in (
            "_NET_WM_WINDOW_TYPE_SPLASH",
            "_NET_WM_WINDOW_TYPE_TOOLTIP",
            "_NET_WM_WINDOW_TYPE_UTILITY",
            "TOOLTIP",  # the short name is recognised too
        ):
            with self.subTest(wtype=wtype):
                self.applied.clear()
                self.metas = {0x2: self.meta("firefox-esr", window_type={wtype})}
                d = self.daemon()
                d.on_map(0x2)
                self.assertEqual(self.applied, [])

    def test_normal_window_type_not_ignored(self):
        self.metas[0x1] = self.meta(
            "firefox-esr", window_type={"_NET_WM_WINDOW_TYPE_NORMAL"}
        )
        d = self.daemon()
        d.on_map(0x1)
        self.assertEqual(len(self.applied), 1)

    def test_repeated_map_does_not_reapply(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.on_map(0x1)
        d.on_map(0x1)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.reads, [0x1])  # not re-read a second time

    def test_empty_class_is_noop(self):
        self.metas[0x1] = self.meta("")
        d = self.daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_window_without_rule_is_noop(self):
        self.metas[0x1] = self.meta("chromium")
        d = self.daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    # -- reconciliation over _NET_CLIENT_LIST

    def test_reconcile_maps_new_windows(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES)
        d.reconcile([0x1, 0x2])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})
        self.assertEqual(d.seen, {0x1, 0x2})

    def test_reconcile_same_list_is_noop(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.reconcile([0x1])
        self.applied.clear()
        self.reads.clear()
        d.reconcile([0x1])
        self.assertEqual(self.applied, [])
        self.assertEqual(self.reads, [])  # the metadata was not re-read

    def test_reconcile_removes_disappeared_window(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES)  # reflow off
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        d.reconcile([0x2])  # 0x1 closed and left the list
        self.assertEqual(d.windows, {0x2: "right"})
        self.assertEqual(d.seen, {0x2})
        self.assertEqual(self.applied, [])

    def test_reconcile_removal_with_reflow_neighbors(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        d.reconcile([0x2])
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.windows, {0x2: "right"})

    def test_reconcile_minimize_keeps_window_and_neighbors(self):
        # Iconifying does not change _NET_CLIENT_LIST -> the list is the same, we move nothing.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        d.reconcile([0x1, 0x2])
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_reconcile_does_not_recheck_unmatched(self):
        self.metas[0x1] = self.meta("libreoffice")  # no rule
        d = self.daemon()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, {0x1})
        self.reads.clear()
        d.reconcile([0x1])  # the same list -> the metadata is left alone
        self.assertEqual(self.reads, [])

    def test_reconcile_ignored_type_is_seen_but_untracked(self):
        self.metas[0x1] = self.meta(
            "firefox-esr", window_type=self.DIALOG
        )
        d = self.daemon()
        d.reconcile([0x1])
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, {0x1})

    def test_reconcile_new_window_occupies_freed_zone_before_reflow(self):
        # 0x1 closed, 0x3 opened in left: right must not expand.
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.windows = {0x1: "left", 0x2: "right"}
        d.seen = {0x1, 0x2}
        self.applied.clear()
        d.reconcile([0x2, 0x3])
        self.assertEqual(self.applied, [(0x3, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.windows, {0x2: "right", 0x3: "left"})

    def test_reconcile_deduplicates_repeated_id(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.reconcile([0x1, 0x1])
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.reads, [0x1])

    # -- Destroy: a real window close

    def test_destroy_with_reflow_off_moves_nothing(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES)  # reflow is off by default
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x2: "right"})

    def test_destroy_with_reflow_neighbors_expands_neighbor(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        # left was freed, right expands to the whole work area
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.windows, {0x2: "right"})

    # -- Unmap: iconify/hide/workspace change -> no-op

    def test_unmap_with_reflow_neighbors_moves_nothing(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_unmap(0x1)  # the window is iconified but still alive
        self.assertEqual(self.applied, [])  # no neighbour is moved

    def test_unmap_keeps_window_in_tracker(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_unmap(0x1)
        self.assertEqual(d.windows, {0x1: "left"})

    def test_map_after_unmap_does_not_reapply(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_unmap(0x1)   # iconified
        d.on_map(0x1)     # deiconified back
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.reads, [0x1])  # the metadata was not read again
        self.assertEqual(d.windows, {0x1: "left"})

    def test_unmap_then_destroy_still_reflows(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_unmap(0x1)  # iconified - tracker and layout stay in place
        self.applied.clear()
        d.on_destroy(0x1)  # really closed
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])

    def test_apply_failure_does_not_track_and_retries(self):
        self.metas[0x1] = self.meta("firefox-esr")
        calls = []

        def flaky(win, rect, label="", quiet=False):
            calls.append(win)
            if len(calls) == 1:
                raise RuntimeError("окно исчезло")

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=flaky,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )
        d.on_map(0x1)
        self.assertEqual(d.windows, {})  # a failure is not remembered
        d.on_map(0x1)  # the next MapNotify tries again
        self.assertEqual(d.windows, {0x1: "left"})
        self.assertEqual(len(calls), 2)

    def test_default_apply_reuses_daemon_connection(self):
        # With the default apply the daemon passes its own X connection (a foreign one cannot be closed).
        self.metas[0x1] = self.meta("firefox-esr")
        d = zd.Daemon(
            self.conf(ONE_RULE), read_meta=self.read_meta, workarea=lambda: WA
        )
        sentinel = object()
        d.x11 = sentinel
        with mock.patch.object(zd.tiler, "apply_zone") as apply_zone:
            d.on_map(0x1)
        apply_zone.assert_called_once()
        self.assertIs(apply_zone.call_args.kwargs["x11"], sentinel)
        self.assertEqual(d.windows, {0x1: "left"})

    def test_system_exit_from_apply_does_not_kill_daemon(self):
        # `fail()` in the layout code raises SystemExit — the daemon must survive it.
        self.metas[0x1] = self.meta("firefox-esr")

        def boom(win, rect, label="", quiet=False):
            raise SystemExit(1)

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=boom,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )
        d.on_map(0x1)  # must not raise SystemExit
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, set())

    def test_keyboard_interrupt_from_apply_is_not_swallowed(self):
        # An interrupt from the keyboard is not disguised as an X error.
        self.metas[0x1] = self.meta("firefox-esr")

        def interrupt(win, rect, label="", quiet=False):
            raise KeyboardInterrupt()

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=interrupt,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )
        with self.assertRaises(KeyboardInterrupt):
            d.on_map(0x1)

    def test_system_exit_from_workarea_does_not_kill_daemon(self):
        # The work area provider raises SystemExit when X refuses: getting the
        # work area must not bring the daemon down.
        self.metas[0x1] = self.meta("firefox-esr")

        def boom():
            raise SystemExit(1)

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=boom,
        )
        d.on_map(0x1)  # must not raise SystemExit
        self.assertEqual(self.applied, [])  # the window is not placed
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, set())

    def test_keyboard_interrupt_from_workarea_is_not_swallowed(self):
        # `_forced_workarea` catches (Exception, SystemExit), but not BaseException.
        self.metas[0x1] = self.meta("firefox-esr")

        def interrupt():
            raise KeyboardInterrupt()

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=interrupt,
        )
        with self.assertRaises(KeyboardInterrupt):
            d.on_map(0x1)

    def test_reflow_skipped_when_monitor_unknown(self):
        # The monitor of the departed window is not remembered (the tracker was restored by hand):
        # reflow is skipped rather than crashing.
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.windows = {0x1: "left", 0x2: "right"}
        d.win_monitor = {0x2: WA}
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x2: "right"})

    def test_unknown_window_unmap_is_noop(self):
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_unmap(0x999)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_unknown_window_destroy_is_noop(self):
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_destroy(0x999)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_reflow_off_removes_from_tracker(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon()
        d.on_map(0x1)
        d.on_destroy(0x1)
        self.assertEqual(d.windows, {})
        # a repeated destroy already does nothing
        d.on_destroy(0x1)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0], (0x1, (0, 0, 500, 1000), "left", True))

    def test_reflow_only_moves_remaining_tracked_window(self):
        # the freed left is taken by another window -> reflow must do nothing.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.metas[0x3] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)  # both windows are mapped to left; the tracker holds both
        d.on_map(0x3)
        self.applied.clear()
        d.on_destroy(0x1)
        # left is still taken by window 0x2 -> reflow returns {} and does not move
        # the neighbour; only the stack itself is rearranged: the single remaining tab
        # takes the whole zone again (a strip without a stack is not needed).
        self.assertEqual(self.applied, [(0x2, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.windows, {0x2: "left", 0x3: "right"})

    # -- returning an expanded zone when a new window takes the freed one

    def test_new_window_in_freed_zone_restores_expanded_neighbor(self):
        # 0x1 closed, right expanded onto left; a new window 0x3 in left
        # brings right back to its frame from the config.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})
        self.applied.clear()
        d.on_map(0x3)
        self.assertEqual(
            self.applied,
            [
                (0x3, (0, 0, 500, 1000), "left", True),
                (0x2, (500, 0, 500, 1000), "right", True),
            ],
        )
        self.assertEqual(d.filled, {})
        self.assertEqual(d.windows, {0x2: "right", 0x3: "left"})

    def test_window_without_rule_does_not_restore_neighbor(self):
        # A window without a rule does not take the freed zone — there is nothing to return.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("libreoffice")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        self.applied.clear()
        d.on_map(0x3)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})

    def test_reflow_off_never_expands_nor_restores(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES)  # reflow off
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        self.assertEqual(d.filled, {})
        self.applied.clear()
        d.on_map(0x3)
        self.assertEqual(self.applied, [(0x3, (0, 0, 500, 1000), "left", True)])

    def test_missing_holder_record_is_dropped(self):
        # The holder is gone from the tracker: the record is dropped, no crash.
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.windows = {}
        d.filled = {filled_key(WA, "left"): 0x2}
        d.on_map(0x3)
        self.assertEqual(self.applied, [(0x3, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.filled, {})

    def test_holder_destroy_clears_covered_zones(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})
        d.on_destroy(0x2)  # the holder is gone - there is nobody to return
        self.assertEqual(d.filled, {})

    def test_failed_restore_keeps_record_for_retry(self):
        calls = []
        fail = {"restore": False}

        def apply(win, rect, label="", quiet=False):
            calls.append((win, rect, label, quiet))
            if fail["restore"] and label == "right":
                raise RuntimeError("окно не переехало")

        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = zd.Daemon(
            self.conf(TWO_ZONES + "[daemon]\nreflow = neighbors\n"),
            apply=apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        calls.clear()
        fail["restore"] = True
        d.on_map(0x3)  # the new window was placed, but the neighbour could not be returned
        self.assertEqual(d.windows, {0x2: "right", 0x3: "left"})
        self.assertEqual(calls[-1][0], 0x2)  # a return attempt was made
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})  # the record stays

    def test_reconcile_restores_neighbor_when_new_window_fills_freed_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        d.reconcile([0x2])  # 0x1 closed -> right expanded into left
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})
        self.applied.clear()
        d.reconcile([0x2, 0x3])  # 0x3 opened in left
        self.assertEqual(
            self.applied,
            [
                (0x3, (0, 0, 500, 1000), "left", True),
                (0x2, (500, 0, 500, 1000), "right", True),
            ],
        )
        self.assertEqual(d.filled, {})

    # -- several monitors

    def test_window_on_second_monitor_uses_its_workarea(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = (1400, 300)
        d = self.multi_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 400, 600), "left", True)])
        self.assertEqual(d.win_monitor, {0x1: WA1})

    def test_window_center_outside_monitors_picks_nearest(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = (1400, 700)  # below monitor 1, closer to it
        d = self.multi_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 400, 600), "left", True)])

    def test_unknown_window_center_skips_placement(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = None
        d = self.multi_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_monitors_unavailable_skips_placement(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = (250, 500)

        def boom():
            raise SystemExit(1)

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=self.apply,
            read_meta=self.read_meta,
            monitors=boom,
            window_center=lambda win: self.centers[win],
        )
        d.on_map(0x1)  # X refusing to give the monitors must not bring the daemon down
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_reflow_expands_neighbor_on_same_monitor(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.centers[0x1] = (1200, 300)
        self.centers[0x2] = (1600, 300)
        d = self.multi_daemon(reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (1000, 0, 800, 600), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA1, "left"): 0x2})

    def test_reflow_does_not_cross_monitors(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.centers[0x1] = (250, 500)
        self.centers[0x2] = (1600, 300)
        d = self.multi_daemon(reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.filled, {})

    def test_reflow_expands_holder_on_the_same_monitor(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("chromium")
        self.centers[0x1] = (250, 500)
        self.centers[0x2] = (1600, 300)
        self.centers[0x3] = (750, 500)
        d = self.multi_daemon(reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_map(0x3)
        self.applied.clear()
        d.on_destroy(0x1)
        # right on monitor 0 expands with window 0x3; window 0x2 from the other screen is left alone
        self.assertEqual(self.applied, [(0x3, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x3})

    # -- tolerant reading of [daemon]

    def test_reflow_defaults_to_off(self):
        self.assertEqual(self.daemon(ONE_RULE).reflow, zd.REFLOW_OFF)

    def test_reflow_bad_value_falls_back_to_off(self):
        self.assertEqual(self.daemon(ONE_RULE, reflow="иногда").reflow, zd.REFLOW_OFF)

    def test_reflow_empty_value_falls_back_to_off(self):
        text = ONE_RULE + "[daemon]\nreflow =\n"
        self.assertEqual(self.daemon(text).reflow, zd.REFLOW_OFF)

    def test_reflow_neighbors_is_read(self):
        self.assertEqual(
            self.daemon(ONE_RULE, reflow="neighbors").reflow, zd.REFLOW_NEIGHBORS
        )

    def test_reflow_value_is_case_insensitive_and_trimmed(self):
        text = ONE_RULE + "[daemon]\nreflow =  NEIGHBORS  \n"
        self.assertEqual(self.daemon(text).reflow, zd.REFLOW_NEIGHBORS)


# A config with a workspace profile: workspace “2” in [workspaces] is index 1 in EWMH.
DESKTOP_CONF = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "main = 0, 0, 0.5, 1\n"
    "video = 0.5, 0, 0.5, 1\n"
    "[rules]\n"
    "firefox* = video\n"
    "[workspaces]\n"
    "2 = cinema\n"
)
# Workspace profile: the same zone `video` in a different geometry (and a different `main`).
CINEMA_PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "main = 0, 0, 1, 0.25\n"
    "video = 0, 0.25, 1, 0.75\n"
)


class TestWorkspaces(DaemonTestCase):
    """Workspaces: the grid from the profile, occupancy and reflow within a workspace."""

    def desktop_daemon(self, profile=CINEMA_PROFILE):
        """A daemon on a `[workspaces]` config with a workspace profile in a temporary directory."""
        directory = self.conf_dir()
        if profile is not None:
            self.write(os.path.join(directory, "templates", "cinema.conf"), profile)
        self.current = 1
        return self.daemon(DESKTOP_CONF, directory=directory)

    # -- grid per workspace

    def test_on_map_uses_profile_zones_of_window_workspace(self):
        # A window on workspace “2” (EWMH 1) takes the zone of the cinema profile: video — 0,0.25,1,0.75.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        d = self.desktop_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (0, 250, 1000, 750), "video", True)])
        self.assertEqual(d.win_workspace, {0x1: 1})

    def test_on_map_uses_default_zones_for_unmapped_workspace(self):
        # Workspace 0 has no profile — the usual `zones.conf` set.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 0}
        d = self.desktop_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "video", True)])

    def test_sticky_window_uses_default_zones(self):
        # A “sticky” window (unknown workspace) lives in the common set, even if
        # the active workspace has a profile.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: None}
        d = self.desktop_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "video", True)])
        self.assertIsNone(d.win_workspace[0x1])

    def test_missing_profile_falls_back_to_default_zones(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        d = self.desktop_daemon(profile=None)
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "video", True)])

    def test_rule_zone_missing_in_profile_is_not_placed(self):
        # The profile has no zone rule — we do not place the window, we do not crash.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        d = self.desktop_daemon(profile="[zones]\nmain = 0, 0, 1, 1\n")
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    # -- reflow only on the visible workspace

    def test_destroy_on_invisible_workspace_moves_nothing(self):
        # Two fake window tables: workspace 1 is visible, workspace 2 is not. Closing a window
        # of an invisible workspace cleans the state but moves nobody.
        self.metas = {
            0x1: self.meta("firefox-esr"),
            0x2: self.meta("chromium"),
            0x3: self.meta("firefox-esr"),
            0x4: self.meta("chromium"),
        }
        self.workspaces = {0x1: 1, 0x2: 1, 0x3: 2, 0x4: 2}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2, 0x3, 0x4])
        self.assertEqual(
            d.windows, {0x1: "left", 0x2: "right", 0x3: "left", 0x4: "right"}
        )
        self.applied.clear()
        d.reconcile([0x1, 0x2, 0x4])  # 0x3 closed on the invisible workspace 2
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right", 0x4: "right"})
        self.assertEqual(d.filled, {})
        d.reconcile([0x1, 0x2])  # 0x4 is there as well - no movement either
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_destroy_on_invisible_workspace_does_not_expand_visible_neighbor(self):
        # On the visible workspace the left zone is empty: without the “workspace is visible” check reflow
        # would expand the neighbour of the visible workspace when a window of a foreign workspace closes.
        self.metas = {
            0x2: self.meta("chromium"),
            0x3: self.meta("firefox-esr"),
            0x4: self.meta("chromium"),
        }
        self.workspaces = {0x2: 1, 0x3: 2, 0x4: 2}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x2)
        d.on_map(0x3)
        d.on_map(0x4)
        self.applied.clear()
        d.on_destroy(0x3)  # a workspace 2 window; on workspace 1 the left zone is free
        self.assertEqual(self.applied, [])
        self.assertEqual(d.filled, {})
        self.assertEqual(d.windows, {0x2: "right", 0x4: "right"})

    def test_occupied_zone_of_other_workspace_does_not_block_reflow(self):
        # On workspace 2 a window stands in left: for workspace 1 this zone is free, and right
        # expands.
        self.metas = {
            0x1: self.meta("firefox-esr"),
            0x2: self.meta("chromium"),
            0x9: self.meta("firefox-esr"),
        }
        self.workspaces = {0x1: 1, 0x2: 1, 0x9: 2}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_map(0x9)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA, "left", 1): 0x2})

    def test_reflow_target_is_on_visible_workspace(self):
        # A window of workspace 2 in the same zone was registered before the window of workspace 1: the one
        # to expand is the neighbour of the visible workspace (0x2), not 0x9 from workspace 2.
        self.metas = {
            0x9: self.meta("chromium"),
            0x1: self.meta("firefox-esr"),
            0x2: self.meta("chromium"),
        }
        self.workspaces = {0x9: 2, 0x1: 1, 0x2: 1}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x9)
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])

    def test_destroy_on_visible_workspace_moves_neighbor(self):
        self.metas = {0x1: self.meta("firefox-esr"), 0x2: self.meta("chromium")}
        self.workspaces = {0x1: 1, 0x2: 1}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA, "left", 1): 0x2})

    def test_reflow_prefers_neighbor_of_visible_workspace(self):
        # On both workspaces a window stands in `right`: the neighbour of the visible workspace expands.
        self.metas = {
            0x1: self.meta("firefox-esr"),
            0x2: self.meta("chromium"),
            0x9: self.meta("chromium"),
        }
        self.workspaces = {0x1: 1, 0x2: 1, 0x9: 2}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_map(0x9)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])

    def test_destroy_with_unknown_current_workspace_moves_nothing(self):
        # The active workspace was not read, while the windows belong to workspace 2: moving
        # blindly is not allowed.
        self.metas = {0x1: self.meta("firefox-esr"), 0x2: self.meta("chromium")}
        self.workspaces = {0x1: 2, 0x2: 2}
        self.current = None
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x2: "right"})

    def test_new_window_on_other_workspace_does_not_restore_holder(self):
        # The coverage appeared on workspace 1; a window of workspace 2 in the same zone does not drop it.
        self.metas = {
            0x1: self.meta("firefox-esr"),
            0x2: self.meta("chromium"),
            0x3: self.meta("firefox-esr"),
        }
        self.workspaces = {0x1: 1, 0x2: 1, 0x3: 2}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        self.assertEqual(d.filled, {filled_key(WA, "left", 1): 0x2})
        self.applied.clear()
        d.on_map(0x3)  # a workspace 2 window into the left zone
        self.assertEqual(self.applied, [(0x3, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.filled, {filled_key(WA, "left", 1): 0x2})

    # -- _NET_CURRENT_DESKTOP

    def test_workspace_change_updates_state_without_moving_windows(self):
        self.metas = {0x1: self.meta("firefox-esr"), 0x2: self.meta("chromium")}
        self.workspaces = {0x1: 1, 0x2: 1}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.assertEqual(d.workspace, 1)
        self.applied.clear()
        # The user switched to workspace 2: a PropertyNotify about _NET_CURRENT_DESKTOP.
        self.current = 2
        handled = zd.handle_property_event(
            d, property_buffer(0xABD), 0xABC, lambda: None, 0xABD
        )
        self.assertTrue(handled)
        self.assertEqual(d.workspace, 2)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_workspace_change_writes_journal_line(self):
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.verbose = True
        self.current = 2
        with mock.patch("builtins.print") as printed:
            d.on_workspace_change()
        lines = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("active workspace: #2", lines)
        self.assertEqual(d.workspace, 2)

    def test_workspace_change_without_readable_desktop_keeps_previous(self):
        self.current = 3
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        self.current = None  # xprop did not answer
        self.assertEqual(d.on_workspace_change(), 3)
        self.assertEqual(d.workspace, 3)

    # -- status for gridsnap-ctl

    def test_status_reports_current_workspace_and_window_workspace(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        self.current = 1
        d = self.daemon(TWO_ZONES)
        d.on_map(0x1)
        status = d.ipc_status()
        self.assertEqual(status["workspace"], 1)
        self.assertEqual(
            status["windows"],
            [
                {
                    "window": "0x1",
                    "zone": "left",
                    "monitor": list(WA),
                    "workspace": 1,
                }
            ],
        )
        # the previous fields are still there
        self.assertEqual(sorted(status["zones"]), ["left", "right"])

    def test_status_reports_workspace_of_filled_zone(self):
        self.metas = {0x1: self.meta("firefox-esr"), 0x2: self.meta("chromium")}
        self.workspaces = {0x1: 1, 0x2: 1}
        self.current = 1
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_destroy(0x1)
        filled = d.ipc_status()["filled"]
        self.assertEqual(
            filled,
            [{"workspace": 1, "monitor": list(WA), "zone": "left", "holder": "0x2"}],
        )


# --- binding a profile to a screen -----------------------------------------

# A config with screen bindings: `[monitors]` beats `[workspaces]`, and that one beats
# the active zone set. The profiles differ both in zones and in rules — otherwise one
# could not see whose profile worked: in work the firefox window goes to `main` full screen,
# in cinema — to `video` on the upper half, while the active `[rules]` says `video`.
SCREEN_CONF = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "main = 0, 0, 0.5, 1\n"
    "video = 0.5, 0, 0.5, 1\n"
    "[rules]\n"
    "firefox* = video\n"
    "[monitors]\n"
    "HDMI-1 = work\n"
    "HDMI-2 = cinema\n"
    "[workspaces]\n"
    "2 = work\n"
)
SCREEN_WORK_PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "main = 0, 0, 1, 1\n"
    "[rules]\n"
    "firefox* = main\n"
)
SCREEN_CINEMA_PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "video = 0, 0, 1, 0.5\n"
    "[rules]\n"
    "firefox* = video\n"
)
# The profile of the second screen for reflow: two halves — neighbours along a vertical edge.
SCREEN_REFLOW_PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "left = 0, 0, 0.5, 1\n"
    "right = 0.5, 0, 0.5, 1\n"
    "[rules]\n"
    "firefox* = left\n"
    "chromium* = right\n"
)
# The output names in the same order as monitors=(WA, WA1).
SCREEN_OUTPUTS = (("HDMI-1", WA), ("HDMI-2", WA1))


class TestMonitorProfiles(DaemonTestCase):
    """Binding a profile to a screen: `[monitors]` beats `[workspaces]`.

    The profiles live in `templates/` next to `zones.conf` — as for a workspace; a window
    is placed by the zones of the profile of **its own screen**, not of the workspace it stands on.
    """

    def screen_daemon(
        self,
        conf=SCREEN_CONF,
        templates=None,
        reflow=None,
        outputs=SCREEN_OUTPUTS,
    ):
        """A daemon with two screens and profiles in `templates/`.

        `templates=None` — both default profiles; otherwise — a dict
        `{name: text}`; the value `None` — there is no profile on disk.
        """
        if templates is None:
            templates = {
                "work": SCREEN_WORK_PROFILE,
                "cinema": SCREEN_CINEMA_PROFILE,
            }
        directory = self.conf_dir()
        for name, text in templates.items():
            if text is not None:
                self.write(os.path.join(directory, "templates", f"{name}.conf"), text)
        self.current = 1
        return self.multi_daemon(
            conf, reflow=reflow, outputs=outputs, directory=directory
        )

    def open_window(self, win, wm_class, center, workspace=None):
        """
 A window appeared: class, center (which chooses the screen) and workspace.

        `center` in WA1 is the second screen (HDMI-2), in WA — the first (HDMI-1).
        """
        self.metas[win] = self.meta(wm_class)
        self.centers[win] = center
        self.workspaces[win] = workspace

    # -- the zone of a window by screen

    def test_window_uses_zone_of_its_screen_profile(self):
        # Workspace 2 is bound to the work profile (firefox -> main full screen), but the window
        # stands on the second screen: the cinema screen profile decides — video, 0..1 x
        # 0..0.5 of the work area of WA1 (1000, 0, 800, 600).
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=1)
        d = self.screen_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 800, 300), "video", True)])
        self.assertEqual(d.win_monitor, {0x1: WA1})
        self.assertEqual(d.win_workspace, {0x1: 1})

    def test_rules_come_from_the_same_profile(self):
        # The active [rules] says firefox -> video, while the profile of the HDMI-1 screen
        # (work) — firefox -> main: the rule comes from the profile of the window.
        self.open_window(0x1, "firefox-esr", (500, 500), workspace=0)
        d = self.screen_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (0, 0, 1000, 1000), "main", True)])

    def test_window_ignored_by_screen_profile_is_not_placed(self):
        # The screen profile has its own [ignore] — a window that the active config touches
        # is left alone on this screen.
        templates = {
            "work": SCREEN_WORK_PROFILE,
            "cinema": SCREEN_CINEMA_PROFILE + "[ignore]\nfirefox* = своё окно\n",
        }
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=0)
        d = self.screen_daemon(templates=templates)
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    # -- fallback when there is no binding or the profile is gone

    def test_screen_without_binding_uses_desktop_profile(self):
        # HDMI-2 has no binding: the second screen lives by the workspace profile (work), that
        # is, firefox goes to main full screen rather than to video of the active set.
        conf = SCREEN_CONF.replace("HDMI-2 = cinema\n", "")
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=1)
        d = self.screen_daemon(conf=conf)
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 800, 600), "main", True)])

    def test_missing_screen_profile_falls_back_to_desktop_with_log(self):
        # The binding `HDMI-2 = cinema` exists, but there is no cinema.conf file: a fallback to
        # the work workspace profile with a log entry, the daemon is alive and places the next windows.
        templates = {"work": SCREEN_WORK_PROFILE, "cinema": None}
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=1)
        d = self.screen_daemon(templates=templates)
        d.verbose = True
        with mock.patch("builtins.print") as printed:
            d.on_map(0x1)
        lines = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('profile "cinema" (of monitor HDMI-2) is not on disk', lines)
        self.assertIn("falling back to the next level", lines)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 800, 600), "main", True)])
        # The daemon keeps working: a window on the first screen takes work.
        self.applied.clear()
        self.open_window(0x2, "firefox-esr", (500, 500), workspace=0)
        d.on_map(0x2)
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "main", True)])

    def test_missing_desktop_profile_falls_back_to_active_conf(self):
        # Neither the screen (HDMI-2 without a binding) nor the workspace (the work profile is gone) gave
        # a grid — the active zones.conf remains, and the window is placed by its rules.
        conf = SCREEN_CONF.replace("HDMI-2 = cinema\n", "")
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=1)
        d = self.screen_daemon(conf=conf, templates={"work": None})
        d.on_map(0x1)
        # The active set: video = 0.5, 0, 0.5, 1 on WA1.
        self.assertEqual(self.applied, [(0x1, (1400, 0, 400, 600), "video", True)])

    def test_unknown_output_name_keeps_working(self):
        # The monitor did not match an output name (no xrandr): the window lives by the
        # workspace profile, as before this wave, and the layout does not break.
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=1)
        d = self.screen_daemon(outputs=())
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 800, 600), "main", True)])

    def test_reload_picks_up_new_monitor_binding(self):
        # The binding was added by editing the config — reload picks it up without a restart.
        conf = SCREEN_CONF.replace("[monitors]\nHDMI-1 = work\nHDMI-2 = cinema\n", "")
        self.open_window(0x1, "firefox-esr", (500, 500), workspace=0)
        d = self.screen_daemon(conf=conf)
        d.on_map(0x1)
        # Without a screen binding workspace 1 lives by the active set: firefox -> video.
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "video", True)])
        self.write(d.conf_path, SCREEN_CONF)
        d.ipc_reload()
        self.applied.clear()
        self.open_window(0x2, "firefox-esr", (1400, 300), workspace=0)
        d.on_map(0x2)
        self.assertEqual(self.applied, [(0x2, (1000, 0, 800, 300), "video", True)])

    # -- reflow and tabs — on the grid of the same profile

    def test_reflow_uses_screen_profile_geometry(self):
        # A window in the left half of the screen closed: the neighbour on the right expands by
        # the fractions of the screen profile. The active zone set has no left/right at all.
        templates = {"work": SCREEN_WORK_PROFILE, "cinema": SCREEN_REFLOW_PROFILE}
        self.open_window(0x1, "firefox-esr", (1200, 500), workspace=1)
        self.open_window(0x2, "chromium", (1600, 500), workspace=1)
        d = self.screen_daemon(templates=templates, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.assertEqual(
            self.applied,
            [
                (0x1, (1000, 0, 400, 600), "left", True),
                (0x2, (1400, 0, 400, 600), "right", True),
            ],
        )
        self.applied.clear()
        d.on_destroy(0x1)
        self.assertEqual(self.applied, [(0x2, (1000, 0, 800, 600), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA1, "left", 1): 0x2})

    def test_tabs_snapshot_uses_screen_profile_zones(self):
        # Two tabs on the second screen: the strip snapshot counts the zone by the
        # cinema profile (video — the upper half of the screen) rather than by the active set
        # (there video is the right half of the other monitor).
        self.open_window(0x1, "firefox-esr", (1200, 100), workspace=1)
        self.open_window(0x2, "firefox-esr", (1300, 120), workspace=1)
        d = self.screen_daemon()
        d.on_map(0x1)
        d.on_map(0x2)
        # Both stand on the shared tab frame: the cinema zone minus the 22 px strip.
        self.rects = {0x1: (1000, 22, 800, 278), 0x2: (1000, 22, 800, 278)}
        records = d.tabs_picture()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["zone"], "video")
        self.assertEqual(records[0]["rect"], [1000, 0, 800, 22])
        self.assertEqual([tab["window"] for tab in records[0]["tabs"]], [0x1, 0x2])

    def test_collapsed_tab_uses_screen_profile_zone(self):
        # One tab left the stack: the remaining one takes the whole zone by the
        # geometry of the screen profile (cinema: video — the upper half), not by the
        # workspace grid.
        self.open_window(0x1, "firefox-esr", (1200, 100), workspace=1)
        self.open_window(0x2, "firefox-esr", (1300, 120), workspace=1)
        d = self.screen_daemon()
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_destroy(0x2)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 800, 300), "video", True)])

    # -- status — by the allowed profiles

    def test_status_reports_zones_of_resolved_profiles(self):
        self.open_window(0x1, "firefox-esr", (1400, 300), workspace=0)
        d = self.screen_daemon()
        d.on_map(0x1)
        status = d.ipc_status()
        # The shape is as before: zones — fractions, zones_px — frames per monitor.
        self.assertEqual(status["monitors"], [list(WA), list(WA1)])
        self.assertEqual(
            status["zones"],
            {"main": [0, 0, 1, 1], "video": [0, 0, 1, 0.5]},
        )
        self.assertEqual(
            status["zones_px"],
            {"main": [[0, 0, 1000, 1000], None], "video": [None, [1000, 0, 800, 300]]},
        )
        self.assertEqual(
            status["windows"],
            [
                {
                    "window": "0x1",
                    "zone": "video",
                    "monitor": list(WA1),
                    "workspace": 0,
                }
            ],
        )

    def test_status_without_monitor_bindings_is_unchanged(self):
        # There are no screen bindings: status counts by the active config, as before
        # (including a zone on every monitor).
        d = self.multi_daemon(TWO_ZONES)
        status = d.ipc_status()
        self.assertEqual(sorted(status["zones"]), ["left", "right"])
        self.assertEqual(
            status["zones_px"]["left"], [[0, 0, 500, 1000], [1000, 0, 400, 600]]
        )


class TestDaemonIgnore(DaemonTestCase):
    """`[ignore]` in the daemon: autoplacement by rules and an explicit apply over the socket."""

    IGNORE_LEFT = (
        "[grid]\ncols = 1.0\nrows = 1.0\n"
        "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
        "[rules]\nfirefox* = left\nchromium* = right\n"
        "[ignore]\nfirefox-esr = диалоги firefox\n"
    )

    def test_on_map_skips_ignored_selector(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.daemon(self.IGNORE_LEFT)
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_on_map_places_window_outside_ignore(self):
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(self.IGNORE_LEFT)
        d.on_map(0x2)
        self.assertEqual(self.applied, [(0x2, (500, 0, 500, 1000), "right", True)])
        self.assertEqual(d.windows, {0x2: "right"})

    def test_ipc_apply_refuses_ignored_window(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.multi_daemon(self.IGNORE_LEFT)
        with self.assertRaises(ValueError) as caught:
            d.ipc_apply({"zone": "left", "window": 0x1})
        self.assertIn("is excluded", str(caught.exception))
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    def test_ipc_apply_places_window_outside_ignore(self):
        self.metas[0x1] = self.meta("chromium")
        self.centers[0x1] = (10, 10)
        d = self.multi_daemon(self.IGNORE_LEFT)
        result = d.ipc_apply({"zone": "right", "window": 0x1})
        self.assertEqual(result["zone"], "right")
        self.assertEqual(d.windows, {0x1: "right"})


class TestFocusMode(DaemonTestCase):
    """The “only this zone” mode: entering, toggle, leaving and newcomers.

    `window_iconic` is replaced by the state of `self.iconic`: without X the real check
    always says “visible”. `self.iconify`/`self.deiconify` change that state,
    as a real WM does, so a manually iconified window stays that way.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            zd.tiler, "window_iconic", lambda win: win in self.iconic
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The polls of the window state on deiconify are zero: the tests must not wait.
        delay = mock.patch.object(zd.tiler, "FOCUS_VERIFY_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def setup_clients(self, windows, workspace=0):
        """The clients of the daemon: windows on `workspace` and on the first monitor."""
        for win in windows:
            self.workspaces[win] = workspace
            self.centers[win] = (100, 100)
        self.clients = list(windows)

    def focus_daemon(self, text=TWO_ZONES):
        """A daemon with two monitors: it is also what checks the iconify scope."""
        return self.multi_daemon(text=text)

    # -- entering

    def test_entry_minimizes_only_same_workspace_and_monitor(self):
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3, 0x4, 0x5])
        self.workspaces[0x5] = 1  # a window of another workspace
        self.centers[0x4] = (1500, 300)  # a window of another monitor
        self.metas[0x1] = self.meta("firefox-esr")  # rule: left
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("dialog-x", window_type=self.DIALOG)
        self.metas[0x4] = self.meta("chromium")
        self.metas[0x5] = self.meta("chromium")
        d = self.focus_daemon()
        reply = d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(
            reply,
            {
                "zone": "left",
                "window": "0x1",
                "monitor": list(WA),
                "minimized": ["0x2"],
            },
        )
        self.assertEqual(self.applied, [(0x1, (0, 0, 500, 1000), "left", True)])
        # the dialog and the windows of a foreign workspace/monitor stay visible
        self.assertEqual(self.iconified, [0x2])
        self.assertEqual(self.iconify_calls, [(0x2, None, True)])
        self.assertEqual(d.windows, {0x1: "left"})
        self.assertEqual(d.win_monitor, {0x1: WA})
        self.assertEqual(d.win_workspace, {0x1: 0})
        self.assertEqual(
            d.focus,
            {
                "zone": "left",
                "target": 0x1,
                "workspace": 0,
                "monitor": WA,
                "minimized": {0x2},
            },
        )

    def test_entry_without_zone_keeps_target_in_place(self):
        # The shape of `[focus]` without a zone: the target is not moved and does not reach a tracker.
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3])
        self.workspaces[0x3] = 1  # another workspace - we do not iconify
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("gimp")
        self.metas[0x3] = self.meta("gimp")
        d = self.focus_daemon()
        reply = d.ipc_focus({"window": 0x1})
        self.assertEqual(
            reply,
            {
                "zone": None,
                "window": "0x1",
                "monitor": list(WA),
                "minimized": ["0x2"],
            },
        )
        self.assertEqual(self.applied, [])
        self.assertEqual(self.iconified, [0x2])
        self.assertEqual(d.windows, {})
        self.assertIsNone(d.focus["zone"])
        self.assertEqual(d.ipc_status()["focus"]["zone"], None)

    def test_entry_uses_active_window_when_not_given(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        with mock.patch.object(zd.tiler, "active_window", lambda: 0x1):
            reply = d.ipc_focus({"zone": "left"})
        self.assertEqual(reply["window"], "0x1")

    def test_entry_accepts_hex_string_window(self):
        self.current = 0
        self.setup_clients([0x1])
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.focus_daemon()
        reply = d.ipc_focus({"zone": "left", "window": "0x1"})
        self.assertEqual(reply["window"], "0x1")

    def test_entry_rejects_unknown_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        d = self.focus_daemon()
        with self.assertRaises(ValueError) as caught:
            d.ipc_focus({"zone": "nope", "window": 0x1})
        self.assertIn("no zone 'nope'", str(caught.exception))
        self.assertEqual(self.applied, [])
        self.assertIsNone(d.focus)

    def test_entry_rejects_excluded_target(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("dialog-x", window_type=self.DIALOG)
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        with self.assertRaises(ValueError) as caught:
            d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertIn("is excluded", str(caught.exception))
        self.assertEqual(self.applied, [])
        self.assertEqual(self.iconified, [])
        self.assertIsNone(d.focus)

    def test_entry_without_active_workspace_refuses(self):
        self.current = None
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        with self.assertRaises(ValueError) as caught:
            d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertIn("active workspace", str(caught.exception))
        self.assertEqual(self.applied, [])
        self.assertEqual(self.iconified, [])
        self.assertIsNone(d.focus)

    # -- toggle and exit

    def test_toggle_restores_only_mode_windows_and_activates_target_last(self):
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("gimp")
        self.iconic = {0x3}  # iconified by hand before entering the mode
        d = self.focus_daemon()
        reply = d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(reply["minimized"], ["0x2"])
        out = d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(out, {"off": True, "restored": ["0x2"]})
        # a manually iconified window is not deiconified, the target is activated last
        self.assertEqual(self.deiconified, [0x2, 0x1])
        self.assertEqual(self.deiconify_calls, [(0x2, None), (0x1, None)])
        self.assertIsNone(d.focus)
        self.assertIsNone(d.ipc_status()["focus"])

    def test_off_on_inactive_mode_is_success(self):
        d = self.focus_daemon()
        self.assertEqual(d.ipc_focus({"off": True}), {"off": True, "restored": []})
        self.assertEqual(
            d.ipc_focus({"off": True, "zone": "left"}),
            {"off": True, "restored": []},
        )
        self.assertEqual(self.deiconified, [])
        self.assertEqual(self.iconified, [])
        self.assertEqual(self.applied, [])

    def test_explicit_exit_switches_to_mode_workspace(self):
        # An explicit exit switches to the workspace of the mode itself: without that muffin would not
        # deiconify the windows (it ignores the activation of a window that is not on the active workspace).
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.x11 = object()  # "there is a connection": without it the switch is skipped
        d.ipc_focus({"zone": "left", "window": 0x1})
        with mock.patch.object(zd.tiler, "switch_workspace") as switch:
            d.ipc_focus({"off": True})
        switch.assert_called_once()
        self.assertEqual(switch.call_args.args, (0,))
        self.assertEqual(self.deiconified, [0x2, 0x1])

    def test_target_disappearing_leaves_mode_and_restores_windows(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.seen = {0x1, 0x2}
        d.reconcile([0x2])  # target 0x1 disappeared from _NET_CLIENT_LIST
        self.assertIsNone(d.focus)
        self.assertEqual(self.deiconified, [0x2, 0x1])
        self.assertEqual(d.windows, {})

    def test_target_disappearing_without_zone_leaves_mode(self):
        # The shape of `focus` without a zone: the target does not lie in `windows`, so its disappearance
        # is caught only by the separate target check in the reconciliation.
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("gimp")  # no rule - the target is counted nowhere
        self.metas[0x2] = self.meta("gimp")
        d = self.focus_daemon()
        d.ipc_focus({"window": 0x1})
        self.assertIsNone(d.focus["zone"])
        self.assertEqual(self.iconified, [0x2])
        d.seen = {0x1, 0x2}
        d.reconcile([0x2])  # target 0x1 disappeared from _NET_CLIENT_LIST
        self.assertIsNone(d.focus)
        self.assertEqual(self.deiconified, [0x2, 0x1])

    def test_unrestorable_windows_go_to_focus_json(self):
        # muffin ignores the activation of a window that is not on the active workspace. The daemon does not lose
        # such a window but hands it to `focus.json`:
        # without the record it would stay iconified forever, while this way `focus --off` finishes it.
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        d.focus_path = path
        d.deiconify = lambda win, x11=None: None  # "deiconify does nothing"
        d.ipc_focus({"zone": "left", "window": 0x1})
        out = d.ipc_focus({"off": True})
        self.assertEqual(out, {"off": True, "restored": []})
        self.assertIsNone(d.focus)
        state = zd.tiler.load_focus_state(path)
        self.assertEqual([r["window"] for r in state["windows"]], ["0x2"])
        self.assertEqual(state["workspace"], 0)
        # The workspace and the class of the window are recorded at the moment of writing (`focus_records`), while the target
        # is not needed in the record: it is visible, there is no need to deiconify it.
        self.assertEqual(state["windows"][0]["workspace"], 0)
        self.assertIsNone(state["target"])

    def test_handoff_keeps_per_window_workspace(self):
        # The file may bring together windows of different workspaces: the shared `workspace` of an old
        # record must not take the deiconification to a foreign workspace (otherwise the window would stay
        # iconified forever, and every `focus --off` would go to the wrong place).
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        d.focus_path = path
        d.deiconify = lambda win, x11=None: None  # "deiconify does nothing"
        # A foreign record from a branch without the daemon: a window on the third workspace.
        zd.tiler.save_focus_state(
            {
                "zone": None,
                "workspace": 2,
                "monitor": [],
                "target": None,
                "windows": [
                    {"window": "0x9", "wm_class": "gimp", "workspace": 2}
                ],
            },
            path,
        )
        self.workspaces[0x9] = 2
        self.iconic = {0x9}  # another window is still iconified - that is the one to return
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.ipc_focus({"off": True})
        state = zd.tiler.load_focus_state(path)
        by_window = {r["window"]: r for r in state["windows"]}
        # Both windows are on their own workspaces — of the mode (0) and of the foreign record (2), — not on
        # the workspace of the old record: otherwise `focus --off` would go to the wrong place.
        self.assertEqual(by_window["0x2"]["workspace"], 0)
        self.assertEqual(by_window["0x9"]["workspace"], 2)

    def test_minimized_window_disappearing_keeps_mode(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("gimp")  # no rule: it is not in windows
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.seen = {0x1, 0x2}
        d.reconcile([0x1])  # the iconified 0x2 is gone, the target is alive
        self.assertIsNotNone(d.focus)
        self.assertEqual(d.focus["minimized"], set())
        self.assertEqual(self.deiconified, [])
        self.assertEqual(d.ipc_status()["focus"]["minimized"], 0)

    def test_workspace_change_keeps_mode(self):
        # Leaving the workspace does NOT drop the mode: the windows of that workspace are invisible anyway, and it cannot
        # be dropped — muffin ignores `_NET_ACTIVE_WINDOW` for a window that is not on
        # the active workspace, and the windows would stay iconified without an owner.
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.current = 1
        self.assertEqual(d.on_workspace_change(), 1)
        self.assertIsNotNone(d.focus)
        self.assertEqual(d.focus["minimized"], {0x2})
        self.assertEqual(self.deiconified, [])
        self.assertEqual(d.ipc_status()["focus"]["minimized"], 1)

    def test_workspace_change_to_same_workspace_keeps_mode(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.on_workspace_change()
        self.assertIsNotNone(d.focus)
        self.assertEqual(self.deiconified, [])

    # -- a new window while the mode is active

    def test_new_window_is_placed_then_minimized(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.applied.clear()
        self.iconified.clear()
        self.metas[0x9] = self.meta("chromium")
        self.workspaces[0x9] = 0
        self.centers[0x9] = (200, 200)
        d.on_map(0x9)
        # placement by rule comes first — after the exit the window will be in its zone
        self.assertEqual(self.applied, [(0x9, (500, 0, 500, 1000), "right", True)])
        self.assertEqual(self.iconified, [0x9])
        self.assertEqual(d.windows[0x9], "right")
        self.assertEqual(d.focus["minimized"], {0x2, 0x9})

    def test_new_window_without_rule_is_just_minimized(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.iconified.clear()
        self.metas[0x9] = self.meta("terminal")  # no rule
        self.workspaces[0x9] = 0
        self.centers[0x9] = (200, 200)
        d.on_map(0x9)
        self.assertEqual(self.iconified, [0x9])
        self.assertEqual([w for w, *_ in self.applied], [0x1])
        self.assertNotIn(0x9, d.windows)
        self.assertIn(0x9, d.focus["minimized"])

    def test_new_excluded_window_stays_visible(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.metas[0x9] = self.meta("dialog-x", window_type=self.DIALOG)
        self.workspaces[0x9] = 0
        self.centers[0x9] = (200, 200)
        d.on_map(0x9)
        self.assertEqual(self.iconified, [0x2])
        self.assertNotIn(0x9, d.windows)
        self.assertNotIn(0x9, d.focus["minimized"])

    def test_new_window_on_other_workspace_is_not_minimized(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.metas[0x9] = self.meta("chromium")
        self.workspaces[0x9] = 1
        self.centers[0x9] = (200, 200)
        d.on_map(0x9)
        # the usual behaviour: placed by rule and left visible
        self.assertEqual(self.iconified, [0x2])
        self.assertEqual(d.windows[0x9], "right")

    # -- status

    def test_status_reports_focus_mode(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        self.assertIsNone(d.ipc_status()["focus"])
        d.ipc_focus({"zone": "left", "window": 0x1})
        status = d.ipc_status()
        self.assertEqual(
            status["focus"], {"zone": "left", "window": "0x1", "minimized": 1}
        )
        # the previous fields did not change
        self.assertEqual(status["workspace"], 0)
        self.assertEqual(
            status["windows"],
            [{"window": "0x1", "zone": "left", "monitor": list(WA), "workspace": 0}],
        )
        d.ipc_focus({"off": True})
        self.assertIsNone(d.ipc_status()["focus"])


class FakeStrip:
    """A replacement for `TabStripProcess`: it collects tab snapshots instead of starting GTK."""

    def __init__(self, accept=True):
        self.payloads = []
        self.stopped = 0
        self.accept = accept

    def running(self):
        return bool(self.payloads)

    def feed(self, line):
        if not self.accept:
            return False
        self.payloads.append(line)
        return True

    def stop(self):
        self.stopped += 1
        return True


class TestZoneTabs(DaemonTestCase):
    """Tabs in a zone: a shared frame, switching and the snapshot for the strip.

    A daemon without X: `apply` is not only remembered in `self.applied` but also changes
    `self.frames` — so the window frame after placement is visible to the next read, as
    with a real WM. `self.active` is the replacement for `_NET_ACTIVE_WINDOW`.
    """

    # The zone covers the whole work area: the tab frame is the zone minus the strip.
    ZONE = (
        "[grid]\ncols = 1.0\nrows = 1.0\n"
        "[zones]\nmain = 0, 0, 1, 1\n"
        "[rules]\nfirefox* = main\nchromium* = main\n"
    )
    TAB = (0, zd.zone_tabs.STRIP_HEIGHT, 1000, 1000 - zd.zone_tabs.STRIP_HEIGHT)

    def setUp(self):
        super().setUp()
        self.frames = {}
        self.active = None
        self.strip = FakeStrip()
        self.current = 0
        # Iconified windows and their deiconification — as in the mode tests: without X the real
        # `window_iconic` always says “visible”.
        patcher = mock.patch.object(
            zd.tiler, "window_iconic", lambda win: win in self.iconic
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- scaffolding

    def apply(self, win, rect, label="", quiet=False):
        super().apply(win, rect, label, quiet)
        self.frames[win] = tuple(rect)

    def read_rect(self, win):
        return self.frames.get(win)

    def tabs_daemon(self, text=None, reflow=None, use_strip=True, state_path=None):
        """A daemon with replaced frames, an active window and a strip."""
        text = text or self.ZONE
        if reflow is not None:
            text = text + f"[daemon]\nreflow = {reflow}\n"
        return zd.Daemon(
            self.conf(text),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            iconify=self.iconify,
            deiconify=self.deiconify,
            read_clients=lambda: self.clients,
            read_rect=self.read_rect,
            active_window=lambda: self.active,
            state_path=state_path,
            strip=self.strip if use_strip else None,
        )

    def two_tabs(self, text=None, reflow=None):
        """Two windows in the zone `main` — already tabs, as after the second window."""
        self.metas[0x1] = self.meta("firefox-esr", "Почта")
        self.metas[0x2] = self.meta("chromium", "Терминал")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon(text, reflow=reflow)
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        return d

    # -- placement

    def test_first_window_takes_the_whole_zone_and_no_strip(self):
        self.metas[0x1] = self.meta("firefox-esr", "Почта")
        self.clients = [0x1]
        d = self.tabs_daemon()
        d.reconcile([0x1])
        self.assertEqual(self.applied, [(0x1, (0, 0, 1000, 1000), "main", True)])
        self.assertEqual(self.strip.payloads, [])
        self.assertEqual(d.tabs_in_zone("main", WA, 0), [])

    def test_second_window_moves_the_first_one_down(self):
        self.metas[0x1] = self.meta("firefox-esr", "Почта")
        self.metas[0x2] = self.meta("chromium", "Терминал")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1]
        d = self.tabs_daemon()
        d.reconcile([0x1])
        self.applied.clear()
        self.clients = [0x1, 0x2]
        d.reconcile([0x1, 0x2])
        # A new window goes onto the tab frame, and the window that was already there is moved there too:
        # the strip appeared, so the window became lower by its height.
        self.assertEqual(
            self.applied,
            [(0x2, self.TAB, "main", True), (0x1, self.TAB, "main", True)],
        )
        self.assertEqual(self.frames[0x1], self.TAB)
        self.assertEqual(self.frames[0x2], self.TAB)
        self.assertEqual(d.tabs_in_zone("main", WA, 0), [0x1, 0x2])

    def test_last_tab_alone_returns_the_whole_zone(self):
        d = self.two_tabs()
        self.clients = [0x2]
        d.reconcile([0x2])
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "main", True)])
        self.assertEqual(self.frames[0x2], (0, 0, 1000, 1000))

    def test_tabs_height_comes_from_conf(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        text = self.ZONE + "[tabs]\nheight = 30\n"
        d = self.tabs_daemon(text)
        self.assertEqual(d.tabs_height, 30)
        d.reconcile([0x1, 0x2])
        self.assertEqual(self.frames[0x1], (0, 30, 1000, 970))
        self.assertEqual(json.loads(self.strip.payloads[-1])["strips"][0]["rect"],
                         [0, 0, 1000, 30])

    def test_zone_is_occupied_while_a_tab_remains(self):
        # reflow=neighbors: while there is at least one tab in left, right does not
        # expand; the last one is gone — it expands.
        text = TWO_ZONES
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.metas[0x3] = self.meta("chromium")
        self.workspaces = {0x1: 0, 0x2: 0, 0x3: 0}
        self.clients = [0x1, 0x2, 0x3]
        d = self.tabs_daemon(text, reflow="neighbors")
        d.reconcile([0x1, 0x2, 0x3])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "left", 0x3: "right"})
        self.applied.clear()
        self.clients = [0x2, 0x3]
        d.reconcile([0x2, 0x3])
        # left is taken by window 0x2: right stays in its zone, while the remaining
        # tab returns to the whole zone.
        self.assertEqual(self.applied, [(0x2, (0, 0, 500, 1000), "left", True)])
        self.applied.clear()
        self.clients = [0x3]
        d.reconcile([0x3])
        # The last tab is gone: the zone is free and right expands onto it.
        self.assertEqual(
            self.applied, [(0x3, (0, 0, 1000, 1000), "right", True)]
        )

    def test_window_dragged_out_of_the_stack_returns_the_whole_zone(self):
        # The user dragged away one of the two tabs: `_refresh_detached` dropped it from
        # the tracker, the stack became single — the remaining window gets the whole
        # zone back, not a tab frame with an empty strip on top.
        d = self.two_tabs()
        self.frames[0x2] = (600, 600, 300, 300)  # dragged by hand
        self.clients = [0x1, 0x2]
        d.reconcile([0x1, 0x2])
        self.assertIn(0x2, d.detached)
        self.assertEqual(self.applied, [(0x1, (0, 0, 1000, 1000), "main", True)])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        self.assertEqual(d.ipc_status()["windows"], [
            {
                "window": "0x1",
                "zone": "main",
                "monitor": list(WA),
                "workspace": 0,
            }
        ])

    def test_reflow_expanded_window_is_not_normalized_back(self):
        # A holder expanded onto a neighbouring zone stands neither on the zone frame nor
        # on the tab frame: the stack normalisation does not touch it.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        self.clients = [0x1]
        d.reconcile([0x1])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        self.applied.clear()
        d.reconcile([0x1])  # a repeated reconciliation moves nothing
        self.assertEqual(self.applied, [])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))

    def test_ipc_apply_puts_a_second_window_on_the_tab_frame(self):
        # The rule “a strip only from two windows” — the same in the autoplacement and
        # in the IPC `apply`: a second window into a taken zone puts both on the shared frame.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("libreoffice")  # no rule - IPC only
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon()
        d.on_map(0x1)
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        d.ipc_apply({"zone": "main", "window": 0x2})
        self.assertEqual(d.ipc_status()["windows"][0]["zone"], "main")
        self.assertEqual(self.frames[0x1], self.TAB)
        self.assertEqual(self.frames[0x2], self.TAB)

    def test_moved_window_leaves_collapsed_zone_behind(self):
        # 0x2 left the stack for another zone: the single remaining tab takes
        # its zone whole again.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon(TWO_ZONES)
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.ipc_apply({"zone": "right", "window": 0x2})
        self.assertIn((0x1, (0, 0, 500, 1000), "left", True), self.applied)
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_reflow_does_not_pull_a_tab_out_of_the_stack(self):
        # right was freed, but there is a stack in left: taking one window out of it means
        # unfolding the tabs, so reflow does not expand such a zone.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.metas[0x3] = self.meta("chromium")
        self.workspaces = {0x1: 0, 0x2: 0, 0x3: 0}
        self.clients = [0x1, 0x2, 0x3]
        d = self.tabs_daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2, 0x3])
        self.applied.clear()
        self.clients = [0x1, 0x2]
        d.reconcile([0x1, 0x2])
        self.assertEqual(self.applied, [])
        self.assertEqual(d.filled, {})
        self.assertEqual(self.frames[0x1], (0, 22, 500, 978))

    def test_expanded_holder_moved_by_a_tab_releases_its_coverage(self):
        # The coverage holder became a tab itself: `_align_stack` moved it onto
        # the shared frame, so the covered zone is no longer its — the `filled` record
        # is dropped, and `status` does not lie to the applet that the zone is covered.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        self.metas[0x4] = self.meta("chromium")
        self.workspaces = {win: 0 for win in (0x1, 0x2, 0x3, 0x4)}
        d = self.tabs_daemon(TWO_ZONES, reflow="neighbors")
        self.clients = [0x1, 0x2]
        d.reconcile([0x1, 0x2])
        self.clients = [0x1]  # right closed: left expands into it
        d.reconcile([0x1])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        self.assertEqual(d.filled, {(0, WA, "right"): 0x1})
        self.applied.clear()
        self.clients = [0x1, 0x3]  # a second window in left: a stack of two
        d.reconcile([0x1, 0x3])
        self.assertEqual(self.frames[0x1], (0, 22, 500, 978))
        # The window was moved by grabbing a tab — the coverage of right is no longer its.
        self.assertEqual(d.filled, {})
        self.assertEqual(d.ipc_status()["filled"], [])
        self.applied.clear()
        self.clients = [0x1, 0x3, 0x4]  # a new window in right
        d.reconcile([0x1, 0x3, 0x4])
        # 0x1 is already on the shared stack frame: there is nowhere to return it, while 0x4 takes
        # right by its own zone.
        self.assertEqual(self.applied, [(0x4, (500, 0, 500, 1000), "right", True)])
        self.assertEqual(self.frames[0x1], (0, 22, 500, 978))
        self.assertEqual(d.filled, {})

    def test_collapsed_tab_releases_coverage_of_the_remaining_window(self):
        # One tab remains — `_collapse_stack` gave it the whole zone: the moved
        # window stopped being the coverage holder.
        d = self.two_tabs()
        d.filled[(0, WA, "right")] = 0x1
        self.assertEqual(len(d.ipc_status()["filled"]), 1)
        self.clients = [0x1]
        d.reconcile([0x1])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        self.assertEqual(d.filled, {})
        self.assertEqual(d.ipc_status()["filled"], [])

    def test_window_moved_to_another_zone_releases_its_coverage(self):
        # `_settle_in_zone` moved the holder to another zone — the records of
        # the zones it covered are dropped where the window is moved. The coverage here
        # does not coincide with the destination zone: otherwise `_restore_holder` would drop it
        # and the test could not tell one from the other.
        three = (
            "[grid]\ncols = 1.0\nrows = 1.0\n"
            "[zones]\n"
            "left = 0, 0, 0.5, 0.5\n"
            "right = 0.5, 0, 0.5, 0.5\n"
            "bottom = 0, 0.5, 1, 0.5\n"
            "[rules]\n"
            "firefox* = left\n"
            "chromium* = right\n"
        )
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 0}
        self.clients = [0x1]
        d = self.tabs_daemon(three)
        d.on_map(0x1)
        d.filled[(0, WA, "bottom")] = 0x1  # the window expanded into bottom
        d.ipc_apply({"zone": "right", "window": 0x1})
        self.assertEqual(d.windows, {0x1: "right"})
        self.assertEqual(d.filled, {})
        self.assertEqual(d.ipc_status()["filled"], [])

    # -- strip

    def test_snapshot_goes_to_the_strip_process(self):
        self.two_tabs()
        self.assertEqual(len(self.strip.payloads), 1)
        data = json.loads(self.strip.payloads[0])
        self.assertEqual(len(data["strips"]), 1)
        strip = data["strips"][0]
        self.assertEqual(strip["zone"], "main")
        self.assertEqual(strip["rect"], [0, 0, 1000, zd.zone_tabs.STRIP_HEIGHT])
        self.assertEqual(
            strip["tabs"],
            [
                {"window": 0x1, "title": "Почта", "active": False},
                {"window": 0x2, "title": "Терминал", "active": True},
            ],
        )

    def test_active_tab_is_the_active_window(self):
        d = self.two_tabs()
        self.active = 0x1
        d.sync_strips()  # the snapshot of the active tab - taken from the active window
        tabs = json.loads(self.strip.payloads[-1])["strips"][0]["tabs"]
        self.assertEqual([tab["active"] for tab in tabs], [True, False])

    def test_same_snapshot_is_not_written_twice(self):
        d = self.two_tabs()
        self.assertEqual(len(self.strip.payloads), 1)
        d.sync_strips()
        d.sync_strips()
        self.assertEqual(len(self.strip.payloads), 1)

    def test_strips_are_only_for_the_current_workspace(self):
        d = self.two_tabs()
        self.current = 1
        d.on_workspace_change()
        self.assertEqual(json.loads(self.strip.payloads[-1]), {"strips": []})

    def test_dead_strip_does_not_break_the_daemon(self):
        self.strip = FakeStrip(accept=False)
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.workspaces = {0x1: 0, 0x2: 0}
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon()
        d.reconcile([0x1, 0x2])  # the strip did not accept the snapshot
        self.assertEqual(d.windows, {0x1: "main", 0x2: "main"})
        self.assertIsNone(d._strips_last)  # the snapshot will be offered again

    def test_no_strip_means_no_work(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.clients = [0x1]
        d = self.tabs_daemon(use_strip=False)  # there is no strip at all
        self.assertIsNone(d.strip)
        self.assertFalse(d.sync_strips())

    # -- IPC: tabs-next / tabs-switch

    def test_tabs_next_raises_the_next_tab_of_the_ring(self):
        d = self.two_tabs()
        self.active = 0x1
        reply = d.ipc_tabs_next()
        self.assertEqual(
            reply,
            {"switched": True, "zone": "main", "window": "0x2", "previous": "0x1"},
        )
        self.assertEqual(self.deiconified, [0x2])
        # from the top tab the ring leads to the bottom one
        self.active = 0x2
        self.assertEqual(d.ipc_tabs_next()["window"], "0x1")
        self.assertEqual(self.deiconified, [0x2, 0x1])

    def test_tabs_next_without_active_window_is_a_honest_noop(self):
        d = self.two_tabs()
        reply = d.ipc_tabs_next()
        self.assertFalse(reply["switched"])
        self.assertIn("nothing to switch", reply["reason"])
        self.assertEqual(self.deiconified, [])

    def test_tabs_next_outside_zones_is_a_honest_noop(self):
        d = self.two_tabs()
        self.active = 0x9  # a window without a zone
        reply = d.ipc_tabs_next()
        self.assertFalse(reply["switched"])
        self.assertIsNone(reply["zone"])
        self.assertEqual(self.deiconified, [])

    def test_tabs_next_with_a_single_tab_is_a_honest_noop(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.clients = [0x1]
        d = self.tabs_daemon()
        d.reconcile([0x1])
        self.active = 0x1
        reply = d.ipc_tabs_next()
        self.assertFalse(reply["switched"])
        self.assertEqual(reply["zone"], "main")
        self.assertEqual(self.deiconified, [])

    def test_tabs_switch_raises_the_clicked_tab(self):
        d = self.two_tabs()
        reply = d.ipc_tabs_switch({"cmd": "tabs-switch", "zone": "main", "window": "0x1"})
        self.assertEqual(reply, {"zone": "main", "window": "0x1"})
        self.assertEqual(self.deiconified, [0x1])

    def test_tabs_switch_refuses_a_window_of_another_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon(TWO_ZONES)
        d.reconcile([0x1, 0x2])
        with self.assertRaises(ValueError) as caught:
            d.ipc_tabs_switch({"zone": "left", "window": 0x2})
        self.assertIn("is not a tab", str(caught.exception))
        self.assertEqual(self.deiconified, [])

    def test_tabs_switch_refuses_unknown_window(self):
        d = self.two_tabs()
        with self.assertRaises(ValueError):
            d.ipc_tabs_switch({"zone": "main", "window": 0x77})
        self.assertEqual(self.deiconified, [])

    def test_dispatcher_knows_tab_commands(self):
        d = self.two_tabs()
        dispatcher = d.build_dispatcher()
        self.active = 0x1
        reply = dispatcher.dispatch('{"cmd": "tabs-next"}')
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["window"], "0x2")
        reply = dispatcher.dispatch('{"cmd": "tabs-switch", "zone": "main", "window": 1}')
        self.assertTrue(reply["ok"])

    # -- focus × tabs

    def test_focus_keeps_the_tabs_of_the_target_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.metas[0x3] = self.meta("chromium")
        self.clients = [0x1, 0x2, 0x3]
        self.workspaces = {0x1: 0, 0x2: 0, 0x3: 0}
        self.centers = {0x1: (100, 100), 0x2: (100, 100), 0x3: (700, 100)}
        d = zd.Daemon(
            self.conf(TWO_ZONES),
            apply=self.apply,
            read_meta=self.read_meta,
            monitors=lambda: [WA],
            window_center=lambda win: self.centers[win],
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            iconify=self.iconify,
            deiconify=self.deiconify,
            read_clients=lambda: self.clients,
            read_rect=self.read_rect,
            active_window=lambda: self.active,
            strip=None,
        )
        d.reconcile([0x1, 0x2, 0x3])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "left", 0x3: "right"})
        reply = d.ipc_focus({"zone": "left", "window": 0x1})
        # tab 0x2 is part of the zone of the mode, we do not iconify it
        self.assertEqual(reply["minimized"], ["0x3"])
        self.assertEqual(self.iconified, [0x3])

    def test_new_window_in_the_target_zone_is_not_minimized_by_focus(self):
        # The mode is called “only this zone”, not “only this window”: a window that
        # a rule placed into the same zone already during the mode becomes
        # a tab and stays visible. Iconifying it means hiding a window of the very
        # zone that the user kept.
        self.metas[0x1] = self.meta("firefox-esr", "Почта")
        self.metas[0x2] = self.meta("firefox-esr", "Терминал")
        self.metas[0x3] = self.meta("chromium", "Чужое")
        self.workspaces = {win: 0 for win in (0x1, 0x2, 0x3)}
        self.clients = [0x1, 0x2, 0x3]
        d = self.tabs_daemon(TWO_ZONES)
        d.reconcile([0x1, 0x2, 0x3])
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(self.iconified, [0x3])
        self.iconified.clear()
        self.applied.clear()
        # A new window by rule — into the very zone where the mode runs.
        self.metas[0x4] = self.meta("firefox-esr", "Ещё")
        self.workspaces[0x4] = 0
        self.clients = [0x1, 0x2, 0x3, 0x4]
        d.on_map(0x4)
        self.assertEqual(self.iconified, [])  # window_iconic/iconify were not called
        self.assertNotIn(0x4, d.focus["minimized"])
        self.assertEqual(d.windows[0x4], "left")
        self.assertEqual(self.frames[0x4], (0, 22, 500, 978))  # the third tab
        self.assertEqual(d.tabs_in_zone("left", WA, 0), [0x1, 0x2, 0x4])

    def test_exit_from_focus_rebuilds_the_strip_snapshot(self):
        # Leaving the mode deiconifies the windows and activates the target last:
        # the active tab changes, so the strip snapshot has to be rebuilt.
        delay = mock.patch.object(zd.tiler, "FOCUS_VERIFY_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)
        self.metas[0x1] = self.meta("firefox-esr", "Почта")
        self.metas[0x2] = self.meta("firefox-esr", "Терминал")
        self.metas[0x3] = self.meta("chromium", "Чужое")
        self.workspaces = {win: 0 for win in (0x1, 0x2, 0x3)}
        self.clients = [0x1, 0x2, 0x3]
        d = self.tabs_daemon(TWO_ZONES)
        d.reconcile([0x1, 0x2, 0x3])
        self.active = 0x2
        d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(self.iconified, [0x3])
        entry = json.loads(self.strip.payloads[-1])["strips"][0]["tabs"]
        self.assertEqual([tab["active"] for tab in entry], [False, True])

        def deiconify_and_activate(win, x11=None):
            self.deiconify(win, x11=x11)
            self.active = win  # the WM raises and focuses the deiconified window

        d.deiconify = deiconify_and_activate
        self.active = None
        d.ipc_focus({"off": True})
        tabs = json.loads(self.strip.payloads[-1])["strips"][0]["tabs"]
        self.assertEqual([tab["active"] for tab in tabs], [True, False])

    # -- stack memory: a tab frame is “a window in a zone”

    def test_restart_keeps_a_tab_window_in_its_zone(self):
        directory = tempfile.mkdtemp(prefix="gridsnap-tabs-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        state_path = os.path.join(directory, "placed.json")
        zd.save_placed_state(
            state_path,
            {
                "1": {
                    "frame": list(self.TAB),
                    "wm_class": "firefox-esr",
                    "zone": "main",
                    "monitor": list(WA),
                    "workspace": None,
                },
                "2": {
                    "frame": list(self.TAB),
                    "wm_class": "chromium",
                    "zone": "main",
                    "monitor": list(WA),
                    "workspace": None,
                },
            },
        )
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.frames[0x1] = self.TAB
        self.frames[0x2] = self.TAB
        self.clients = [0x1, 0x2]
        d = self.tabs_daemon(state_path=state_path)
        d.reconcile([0x1, 0x2])
        # A tab window counts as standing in the zone: the daemon does not move it.
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "main", 0x2: "main"})
        self.assertNotIn(0x1, d.detached)
        self.assertNotIn(0x2, d.detached)

    def test_restart_with_a_single_leftover_tab_returns_the_whole_zone(self):
        # The second tab disappeared while the daemon was not running: the remembered frame is
        # a tab frame, but it is the only one left, so there is no strip and the zone is the whole
        # of it. Without the normalisation the window would stay 22 px lower with an empty strip.
        directory = tempfile.mkdtemp(prefix="gridsnap-tabs-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        state_path = os.path.join(directory, "placed.json")
        zd.save_placed_state(
            state_path,
            {
                "1": {
                    "frame": list(self.TAB),
                    "wm_class": "firefox-esr",
                    "zone": "main",
                    "monitor": list(WA),
                    "workspace": None,
                }
            },
        )
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = self.TAB
        self.clients = [0x1]
        d = self.tabs_daemon(state_path=state_path)
        d.reconcile([0x1])
        self.assertEqual(self.applied, [(0x1, (0, 0, 1000, 1000), "main", True)])
        self.assertEqual(self.frames[0x1], (0, 0, 1000, 1000))
        self.assertEqual(d.windows, {0x1: "main"})


class TestTabStripProcess(unittest.TestCase):
    """The strip process: the start command, a failure without GTK and the restart of a dead one."""

    def script(self, body):
        """A temporary strip script: the body is python."""
        directory = tempfile.mkdtemp(prefix="gridsnap-strip-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "zone-tabstrip.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_command_is_python_script_and_conf(self):
        strip = zd.TabStripProcess("/tmp/zones.conf")
        self.assertEqual(
            strip.command(),
            [sys.executable, zd.TABSTRIP, "--conf", "/tmp/zones.conf"],
        )

    def test_missing_script_never_starts_and_is_not_retried(self):
        strip = zd.TabStripProcess(
            "/tmp/zones.conf", script="/нет-такого/zone-tabstrip.py"
        )
        self.assertFalse(strip.start())
        self.assertTrue(strip.failed)
        self.assertFalse(strip.running())
        self.assertFalse(strip.feed('{"strips": []}\n'))

    def test_script_dying_on_import_is_failed_and_not_retried(self):
        # “No GTK”: the file is in place and python starts, but the script fails on import
        # with code 1. That is a start failure, not the death of a strip that worked — such a
        # child must not be respawned on every change of the picture.
        path = self.script("import sys\nsys.exit(1)\n")
        strip = zd.TabStripProcess("/tmp/zones.conf", script=path)
        self.assertFalse(strip.start())
        self.assertTrue(strip.failed)
        self.assertIsNone(strip.proc)  # we do not keep a dead child behind us
        self.assertFalse(strip.running())
        self.assertFalse(strip.feed('{"strips": []}\n'))

    def test_dead_working_strip_is_restarted(self):
        # A strip that already worked and died is restarted as before.
        path = self.script("import sys\nsys.stdin.readline()\nsys.exit(0)\n")
        strip = zd.TabStripProcess("/tmp/zones.conf", script=path)
        self.addCleanup(strip.stop)
        self.assertTrue(strip.start())
        self.assertFalse(strip.failed)
        first = strip.proc.pid
        self.assertTrue(strip.feed('{"strips": []}\n'))
        # The strip read the snapshot and finished by itself — that is the death of a working one.
        strip.proc.wait(timeout=5)
        self.assertFalse(strip.running())
        self.assertTrue(strip.feed('{"strips": []}\n'))  # restarted
        self.assertFalse(strip.failed)
        self.assertNotEqual(strip.proc.pid, first)

    def test_empty_snapshot_without_process_is_not_a_failure(self):
        # The strip has not started yet and there is nothing to draw: we do not spawn the process.
        strip = zd.TabStripProcess("/tmp/zones.conf")
        self.assertIsNone(strip.proc)
        self.assertFalse(strip.failed)
        self.assertFalse(strip.running())

    def test_stop_is_idempotent(self):
        strip = zd.TabStripProcess("/tmp/zones.conf")
        self.assertFalse(strip.stop())
        self.assertFalse(strip.stop())


class TestPlacedState(unittest.TestCase):
    """The stack memory file: reading tolerates garbage, writing is atomic."""

    def setUp(self):
        directory = tempfile.mkdtemp(prefix="gridsnap-placed-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.path = os.path.join(directory, "placed.json")

    def test_missing_file_is_empty(self):
        self.assertEqual(zd.load_placed_state(self.path), {})
        self.assertEqual(zd.load_placed_state(None), {})

    def test_broken_file_is_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ не json")
        self.assertEqual(zd.load_placed_state(self.path), {})

    def test_wrong_version_is_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 99, "windows": {"1": {"frame": [0,0,1,1], "zone": "main"}}}')
        self.assertEqual(zd.load_placed_state(self.path), {})

    def test_round_trip(self):
        record = {
            "frame": [0, 0, 500, 1000],
            "wm_class": "firefox-esr",
            "zone": "left",
            "monitor": [0, 0, 1000, 1000],
            "workspace": 1,
        }
        zd.save_placed_state(self.path, {"8388656": record})
        self.assertEqual(zd.load_placed_state(self.path), {8388656: record})

    def test_records_without_zone_are_dropped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"version": 1, "windows": {'
                '"1": {"frame": [0,0,1,1]}, "x": {"zone": "a"}}}'
            )
        self.assertEqual(zd.load_placed_state(self.path), {})

    def test_frame_matches_tolerance(self):
        self.assertTrue(zd.frame_matches((0, 0, 100, 100), (3, -3, 103, 97)))
        self.assertFalse(zd.frame_matches((0, 0, 100, 100), (50, 0, 100, 100)))
        self.assertFalse(zd.frame_matches(None, (0, 0, 1, 1)))
        self.assertFalse(zd.frame_matches((0, 0, 1), (0, 0, 1)))

    def test_state_from_another_session_is_dropped(self):
        record = {
            "frame": [0, 0, 500, 1000],
            "wm_class": "firefox-esr",
            "zone": "left",
            "monitor": [0, 0, 1000, 1000],
            "workspace": None,
        }
        zd.save_placed_state(self.path, {"1": record}, session="0x1400003")
        self.assertEqual(zd.load_placed_state(self.path, "0x1400003"), {1: record})
        self.assertEqual(zd.load_placed_state(self.path, "0x9999999"), {})
        # without a known session the file is read as before
        self.assertEqual(zd.load_placed_state(self.path), {1: record})


class TestDaemonPlacedMemory(DaemonTestCase):
    """The daemon memory: a window taken out of a zone does not come back by itself."""

    TEXT = (
        "[grid]\ncols = 1.0\nrows = 1.0\n"
        "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
        "[rules]\nfirefox* = left\nchromium* = right\n"
    )

    def setUp(self):
        super().setUp()
        self.frames = {}
        directory = tempfile.mkdtemp(prefix="gridsnap-placed-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.state_path = os.path.join(directory, "placed.json")

    def read_rect(self, win):
        return self.frames.get(win)

    def placed_daemon(self, text=None):
        """A daemon with replaced geometry and its own state file."""
        return zd.Daemon(
            self.conf(text or self.TEXT),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            state_path=self.state_path,
            read_rect=self.read_rect,
        )

    def test_on_map_records_placement(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        d = self.placed_daemon()
        d.on_map(0x1)
        self.assertEqual(d.placed[0x1], (0, 0, 500, 1000))
        self.assertEqual(d.win_class[0x1], "firefox-esr")
        saved = zd.load_placed_state(self.state_path)
        self.assertEqual(saved[0x1]["zone"], "left")
        self.assertEqual(saved[0x1]["frame"], [0, 0, 500, 1000])

    def test_window_moved_by_user_is_detached(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        d = self.placed_daemon()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {0x1: "left"})
        d.filled[(None, WA, "left")] = 0x1
        self.frames[0x1] = (600, 600, 300, 300)  # the user moved the window out
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.filled, {})  # the zone is free for reflow
        self.assertIn(0x1, d.detached)
        self.assertEqual(len(self.applied), 1)  # did not place it again
        d.on_map(0x1)
        self.assertEqual(len(self.applied), 1)

    def test_restart_adopts_window_still_in_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        self.placed_daemon().on_map(0x1)
        self.applied.clear()
        second = self.placed_daemon()
        second.reconcile([0x1])
        self.assertEqual(self.applied, [])  # adopted without apply
        self.assertEqual(second.windows, {0x1: "left"})
        self.assertEqual(second.win_monitor, {0x1: WA})

    def test_restart_skips_window_moved_out(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        self.placed_daemon().on_map(0x1)
        self.frames[0x1] = (600, 600, 300, 300)
        self.applied.clear()
        second = self.placed_daemon()
        second.reconcile([0x1])
        self.assertEqual(self.applied, [])
        self.assertEqual(second.windows, {})
        self.assertIn(0x1, second.detached)

    def test_restart_ignores_record_of_reused_xid(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        self.placed_daemon().on_map(0x1)
        self.metas[0x1] = self.meta("chromium")  # the XID was reused
        self.applied.clear()
        second = self.placed_daemon()
        second.reconcile([0x1])
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "right", True)])
        self.assertEqual(second.windows, {0x1: "right"})

    def test_broken_state_file_does_not_break_start(self):
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write("{ мусор")
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        d = self.placed_daemon()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {0x1: "left"})

    def test_restart_returns_reflow_expanded_window_to_its_zone(self):
        # The state file: the daemon expanded window 0x1 (zone left) onto right and wrote
        # the merged frame. A restart must return the window to left: the coverage
        # book `filled` does not live in the file, and otherwise a new window in right would land
        # on top of the expanded one.
        zd.save_placed_state(
            self.state_path,
            {
                "1": {
                    "frame": [0, 0, 1000, 1000],
                    "wm_class": "firefox-esr",
                    "zone": "left",
                    "monitor": list(WA),
                    "workspace": None,
                }
            },
        )
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 1000, 1000)
        d = self.placed_daemon()
        d.reconcile([0x1])
        self.assertEqual(self.applied, [(0x1, (0, 0, 500, 1000), "left", True)])
        self.assertEqual(d.windows, {0x1: "left"})

    def test_placement_retries_late_wm_move(self):
        # The WM applied `_NET_MOVERESIZE_WINDOW` not at once: the first read is the old
        # frame, the second one is already the target. The target must get into `placed`, otherwise
        # the window would at once be considered “gone out of the zone”.
        self.metas[0x1] = self.meta("firefox-esr")
        reads = iter([(300, 300, 200, 200), (0, 0, 500, 1000)])
        d = zd.Daemon(
            self.conf(self.TEXT),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
            read_rect=lambda win: next(reads, (0, 0, 500, 1000)),
        )
        d.on_map(0x1)
        self.assertEqual(d.placed[0x1], (0, 0, 500, 1000))

    def test_moved_window_on_invisible_desktop_stays_tracked(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        self.workspaces = {0x1: 2}
        self.current = 1
        d = self.placed_daemon()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {0x1: "left"})
        self.frames[0x1] = (600, 600, 300, 300)
        d.reconcile([0x1])
        self.assertEqual(d.windows, {0x1: "left"})  # we do not touch the invisible workspace
        self.current = 2
        d.on_workspace_change()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})  # on its own workspace the move is visible
        self.assertIn(0x1, d.detached)

    def test_no_state_path_writes_nothing(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        d = zd.Daemon(
            self.conf(self.TEXT),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
            read_rect=self.read_rect,
        )
        d.on_map(0x1)
        self.assertEqual(d.placed[0x1], (0, 0, 500, 1000))
        self.assertFalse(os.path.exists(self.state_path))


class TestFractionsToPixels(unittest.TestCase):
    def test_divides_workarea(self):
        self.assertEqual(
            zd.fractions_to_pixels(0.5, 0.0, 0.5, 1.0, WA), (500, 0, 500, 1000)
        )

    def test_respects_workarea_offset(self):
        wa = (100, 40, 1000, 500)
        self.assertEqual(
            zd.fractions_to_pixels(0.5, 0.0, 0.5, 1.0, wa), (600, 40, 500, 500)
        )

    def test_pixel_edge_is_shared_between_neighbors(self):
        left = zd.fractions_to_pixels(0.0, 0.0, 0.5, 1.0, WA)
        right = zd.fractions_to_pixels(0.5, 0.0, 0.5, 1.0, WA)
        self.assertEqual(left[0] + left[2], right[0])

    def test_tiny_zone_is_at_least_one_pixel(self):
        x, y, w, h = zd.fractions_to_pixels(0.0, 0.0, 0.0001, 0.0001, WA)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)


class TestMonitorScaleCache(unittest.TestCase):
    """Changing the screen scale on the fly drops the work area cache.

    The GDK work areas change together with the scale (`GDK_SCALE=2` — twice
    smaller), so the daemon compares a fingerprint of the scales: otherwise until `reload` it
    would place windows by the old pixels.
    """

    def setUp(self):
        fd, self.conf = tempfile.mkstemp(suffix=".conf")
        os.close(fd)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(ONE_RULE)
        self.addCleanup(os.unlink, self.conf)
        # The monitors and the scales are read from this state, as from an X session.
        self.areas = [(0, 0, 960, 540)]
        self.scales = [2]
        self.calls = 0

    def read_monitors(self):
        self.calls += 1
        return list(self.areas)

    def daemon(self, workarea=None):
        return zd.Daemon(
            self.conf,
            workarea=workarea,
            monitors=self.read_monitors,
            monitor_scales=lambda: list(self.scales),
        )

    def test_unchanged_scale_uses_cache(self):
        dae = self.daemon()
        self.assertEqual(dae._monitors(), [(0, 0, 960, 540)])
        self.assertEqual(dae._monitors(), [(0, 0, 960, 540)])
        self.assertEqual(self.calls, 1)

    def test_new_scale_re_reads_monitors(self):
        dae = self.daemon()
        self.assertEqual(dae._monitors(), [(0, 0, 960, 540)])
        # The user set scale 1: the areas became twice as wide.
        self.scales = [1]
        self.areas = [(0, 0, 1920, 1080)]
        self.assertEqual(dae._monitors(), [(0, 0, 1920, 1080)])
        self.assertEqual(self.calls, 2)

    def test_new_scale_drops_forced_workarea(self):
        dae = self.daemon(workarea=lambda: self.areas[0])
        self.assertEqual(dae._monitor_of(0x1), (0, 0, 960, 540))
        self.areas = [(0, 0, 1920, 1080)]
        self.scales = [1]
        self.assertEqual(dae._monitor_of(0x1), (0, 0, 1920, 1080))

    def test_unreadable_scale_keeps_cache(self):
        dae = zd.Daemon(
            self.conf,
            monitors=self.read_monitors,
            monitor_scales=lambda: (_ for _ in ()).throw(RuntimeError("нет GDK")),
        )
        self.assertEqual(dae._monitors(), [(0, 0, 960, 540)])
        self.assertEqual(dae._monitors(), [(0, 0, 960, 540)])
        self.assertEqual(self.calls, 1)


class TestMonitorForPoint(unittest.TestCase):
    def test_point_inside_first(self):
        self.assertEqual(zd.monitor_for_point([WA, WA1], (250, 500)), WA)

    def test_point_inside_second(self):
        self.assertEqual(zd.monitor_for_point([WA, WA1], (1400, 300)), WA1)

    def test_point_outside_picks_nearest_center(self):
        self.assertEqual(zd.monitor_for_point([WA, WA1], (1400, 700)), WA1)

    def test_right_edge_belongs_to_next_monitor(self):
        self.assertEqual(zd.monitor_for_point([WA, WA1], (1000, 500)), WA1)


class TestReadWindowCenter(unittest.TestCase):
    def test_parses_xwininfo_output(self):
        out = (
            'xwininfo: Window id: 0x400003 "x"\n\n'
            "  Absolute upper-left X:  100\n"
            "  Absolute upper-left Y:  50\n"
            "  Width: 400\n"
            "  Height: 300\n"
        )
        with mock.patch.object(zd.tiler.subprocess, "check_output", return_value=out):
            self.assertEqual(zd.read_window_center(0x400003), (300, 200))

    def test_parses_negative_coordinates(self):
        out = (
            "  Absolute upper-left X:  -1200\n"
            "  Absolute upper-left Y:  0\n"
            "  Width: 400\n"
            "  Height: 300\n"
        )
        with mock.patch.object(zd.tiler.subprocess, "check_output", return_value=out):
            self.assertEqual(zd.read_window_center(0x1), (-1000, 150))

    def test_missing_field_returns_none(self):
        with mock.patch.object(zd.tiler.subprocess, "check_output", return_value="Width: 10\n"):
            self.assertIsNone(zd.read_window_center(0x1))

    def test_xwininfo_failure_returns_none(self):
        with mock.patch.object(
            zd.tiler.subprocess, "check_output", side_effect=OSError("нет xwininfo")
        ):
            self.assertIsNone(zd.read_window_center(0x1))


class TestReadWindowMeta(unittest.TestCase):
    """The default read_window_meta: one retry when the class/title are empty."""

    def setUp(self):
        self._class = zd.tiler.window_class_title
        self._sleep = zd.time.sleep
        self._types = mock.patch.object(zd, "window_types", lambda _win: frozenset())
        self._types.start()
        zd.time.sleep = lambda _s: None
        self.addCleanup(self._restore)

    def _restore(self):
        zd.tiler.window_class_title = self._class
        self._types.stop()
        zd.time.sleep = self._sleep

    def test_retries_once_when_title_arrives_late(self):
        seq = [("firefox-esr", ""), ("firefox-esr", "Later title")]
        seen = []

        def fake(win):
            seen.append(win)
            return seq.pop(0)

        zd.tiler.window_class_title = fake
        meta = zd.read_window_meta(0x1)
        self.assertEqual((meta.wm_class, meta.wm_title), ("firefox-esr", "Later title"))
        self.assertEqual(seen, [0x1, 0x1])

    def test_no_retry_when_both_present(self):
        calls = []

        def fake(win):
            calls.append(win)
            return ("firefox-esr", "T")

        zd.tiler.window_class_title = fake
        meta = zd.read_window_meta(0x1)
        self.assertEqual((meta.wm_class, meta.wm_title), ("firefox-esr", "T"))
        self.assertEqual(calls, [0x1])


class TestPropertyEvent(unittest.TestCase):
    """handle_property_event: reconciliation only on a PropertyNotify about _NET_CLIENT_LIST.

    The event buffer is built from raw bytes using the expected XPropertyEvent offsets
    from Xlib.h (LP64) rather than from a copy of the structure in the module under test: the test catches
    a drift of the offsets against the X11 ABI.
    """

    class Stub:
        def __init__(self):
            self.calls = []
            self.workspace_calls = 0

        def reconcile(self, client_ids):
            self.calls.append(list(client_ids))

        def on_workspace_change(self):
            self.workspace_calls += 1

    def test_xproperty_event_offsets_match_xlib_abi(self):
        fields = {name: getattr(zd.XPropertyEvent, name).offset for name, _ in zd.XPropertyEvent._fields_}
        self.assertEqual(fields, XPROPERTY_EVENT_OFFSETS)
        self.assertEqual(ctypes.sizeof(zd.XPropertyEvent), XPROPERTY_EVENT_SIZE)

    def test_client_list_atom_triggers_reconcile(self):
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xABC), 0xABC, lambda: [0x9]
        )
        self.assertTrue(handled)
        self.assertEqual(dae.calls, [[0x9]])

    def test_unreadable_list_skips_reconcile(self):
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xABC), 0xABC, lambda: None
        )
        self.assertTrue(handled)  # an event about our atom
        self.assertEqual(dae.calls, [])  # but there is no reconciliation

    def test_other_atom_is_ignored(self):
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xDEF), 0xABC, lambda: [0x9], 0xABD
        )
        self.assertFalse(handled)
        self.assertEqual(dae.calls, [])
        self.assertEqual(dae.workspace_calls, 0)

    def test_current_desktop_atom_updates_workspace_without_reconcile(self):
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xABD), 0xABC, lambda: [0x9], 0xABD
        )
        self.assertTrue(handled)
        self.assertEqual(dae.workspace_calls, 1)
        self.assertEqual(dae.calls, [])  # the client list was not read

    def test_current_desktop_atom_ignored_without_atom_argument(self):
        # The old call with four arguments does not know about the workspace — the event is foreign.
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xABD), 0xABC, lambda: [0x9]
        )
        self.assertFalse(handled)
        self.assertEqual(dae.workspace_calls, 0)
        self.assertEqual(dae.calls, [])


class TestReadClientList(unittest.TestCase):
    """read_client_list: parsing `xprop -root _NET_CLIENT_LIST`."""

    def setUp(self):
        self._check_output = zd.tiler.subprocess.check_output
        self.addCleanup(self._restore)

    def _restore(self):
        zd.tiler.subprocess.check_output = self._check_output

    def test_parses_window_ids(self):
        zd.tiler.subprocess.check_output = lambda *a, **k: (
            "_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0x600005\n"
        )
        self.assertEqual(zd.read_client_list(), [0x400003, 0x600005])

    def test_asks_for_net_client_list_on_root(self):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = args
            return "_NET_CLIENT_LIST(WINDOW): window id # 0x1\n"

        zd.tiler.subprocess.check_output = fake
        zd.read_client_list()
        self.assertEqual(seen["args"], ["xprop", "-root", "_NET_CLIENT_LIST"])

    def test_empty_when_property_missing(self):
        # when the property is absent xprop prints a message and exits with code 0 —
        # that is not “no clients” but “the list is unavailable”.
        zd.tiler.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST:  not found.\n"
        self.assertIsNone(zd.read_client_list())

    def test_none_when_no_such_atom(self):
        zd.tiler.subprocess.check_output = lambda *a, **k: (
            "no such atom on any window.\n"
        )
        self.assertIsNone(zd.read_client_list())

    def test_none_without_window_id_marker(self):
        # Output without the `window id #` marker is not parsed as a window list.
        zd.tiler.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST(WINDOW): \n"
        self.assertIsNone(zd.read_client_list())

    def test_empty_list_when_property_present_but_no_windows(self):
        # The valid state “no clients” is [], not None.
        zd.tiler.subprocess.check_output = lambda *a, **k: (
            "_NET_CLIENT_LIST(WINDOW): window id #\n"
        )
        self.assertEqual(zd.read_client_list(), [])

    def test_none_on_error(self):
        def boom(*args, **kwargs):
            raise OSError("нет xprop")

        zd.tiler.subprocess.check_output = boom
        self.assertIsNone(zd.read_client_list())


class _StubFunc:
    """A callable wrapper with argtypes/restype fields (like ctypes _FuncPtr)."""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class FakeX11:
    """A mini X11 stub: the event queue and everything that `serve` calls."""

    CLIENT_LIST_ATOM = 0xABC
    DESKTOP_ATOM = 0xABD
    OTHER_ATOM = 0xDEF

    def __init__(self, events=(), fd=-1):
        self.dpy = 0x1
        self.root = 0x2
        self._events = list(events)
        self._fd = fd
        self.selected = []
        self.closed = False
        self.x = types.SimpleNamespace(
            XSelectInput=_StubFunc(self._select_input),
            XSync=_StubFunc(lambda dpy, flag: None),
            XConnectionNumber=_StubFunc(lambda dpy: self._fd),
            XPending=_StubFunc(lambda dpy: len(self._events)),
            XNextEvent=_StubFunc(self._next_event),
            XCloseDisplay=_StubFunc(self._close),
        )

    def atom(self, name):
        if name == zd.CLIENT_LIST_ATOM:
            return self.CLIENT_LIST_ATOM
        if name == zd.CURRENT_DESKTOP_ATOM:
            return self.DESKTOP_ATOM
        return self.OTHER_ATOM

    def _select_input(self, dpy, win, mask):
        self.selected.append((dpy, win, mask))
        return 1

    def _next_event(self, dpy, buf):
        ev = self._events.pop(0)
        ctypes.memmove(buf, ev, ctypes.sizeof(ev))
        return 1

    def _close(self, dpy):
        self.closed = True
        return 1


class StubDaemon:
    """A stub daemon for serve(): it only records reconciliations and workspace changes."""

    def __init__(self):
        self.reconciled = []
        self.reflow = zd.REFLOW_OFF
        self.logs = []
        self.x11 = None
        self.workspace = None
        self.workspace_calls = 0

    def reconcile(self, client_ids):
        self.reconciled.append(list(client_ids))

    def on_workspace_change(self):
        self.workspace_calls += 1

    def log(self, message):
        self.logs.append(message)


class TestServeLoop(unittest.TestCase):
    """serve(): PropertyChangeMask on root, the initial reconciliation and PropertyNotify."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def pipe_fd(self):
        """An fd that will never become readable (for the timeout in --once)."""
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        return r

    def test_selects_property_change_mask_and_does_initial_reconcile(self):
        dae = StubDaemon()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x11], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x11]])
        self.assertEqual(
            fake.selected, [(fake.dpy, fake.root, zd.PROPERTY_CHANGE_MASK)]
        )
        # serve() passes the connection to the daemon so that apply_zone reuses it
        self.assertIs(dae.x11, fake)
        self.assertTrue(fake.closed)

    def test_unreadable_list_skips_initial_reconcile(self):
        dae = StubDaemon()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: None, once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [])
        self.assertTrue(fake.closed)

    def test_unreadable_list_keeps_tracker_on_property_notify(self):
        # The real Daemon (not a stub with its own reconcile): a skipped
        # reconciliation must touch neither the tracker nor `seen`.
        dae = zd.Daemon(self.conf(ONE_RULE), workarea=lambda: WA)
        dae.windows = {0x1: "left"}
        dae.seen = {0x1}
        fake = FakeX11(
            events=[property_buffer(FakeX11.CLIENT_LIST_ATOM)], fd=self.pipe_fd()
        )
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: None, once_timeout=0.05
        )
        # the list was not read — no reconciliation and no changes of the tracker
        self.assertEqual(dae.windows, {0x1: "left"})
        self.assertEqual(dae.seen, {0x1})

    def test_client_list_property_notify_reconciles(self):
        dae = StubDaemon()
        fake = FakeX11(
            events=[property_buffer(FakeX11.CLIENT_LIST_ATOM)], fd=self.pipe_fd()
        )
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        # the first is the initial one, the second one is by the PropertyNotify about _NET_CLIENT_LIST
        self.assertEqual(dae.reconciled, [[0x22], [0x22]])
        self.assertTrue(fake.closed)

    def test_other_property_notify_is_ignored(self):
        dae = StubDaemon()
        fake = FakeX11(events=[property_buffer(FakeX11.OTHER_ATOM)], fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x22]])  # the initial one only
        self.assertEqual(dae.workspace_calls, 0)  # the workspace was not touched either

    def test_current_desktop_property_notify_updates_workspace_only(self):
        # A workspace change is not a change of the client list: no reconciliation, no
        # moves, only an update of `self.workspace`.
        dae = StubDaemon()
        fake = FakeX11(events=[property_buffer(FakeX11.DESKTOP_ATOM)], fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x22]])  # the initial one only
        self.assertEqual(dae.workspace_calls, 1)

    def test_once_without_events_times_out(self):
        dae = StubDaemon()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(dae, x11=fake, once=True, read_clients=lambda: [], once_timeout=0.05)
        self.assertEqual(dae.reconciled, [[]])
        self.assertTrue(fake.closed)

    def test_serve_restores_focus_windows_on_exit(self):
        # The “only this zone” mode must not survive the daemon: in `finally` serve
        # deiconifies its windows, the target last.
        restored = []
        dae = zd.Daemon(
            self.conf(ONE_RULE),
            workarea=lambda: WA,
            deiconify=lambda win, x11=None: restored.append(win),
        )
        dae.seen = {0x1, 0x2}
        dae.focus = {
            "zone": "left",
            "target": 0x1,
            "workspace": 0,
            "monitor": WA,
            "minimized": {0x2},
        }
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x1, 0x2], once_timeout=0.05
        )
        self.assertEqual(restored, [0x2, 0x1])
        self.assertIsNone(dae.focus)
        self.assertTrue(fake.closed)

    def test_focus_state_is_dropped_without_x(self):
        # An early exit without an X connection: the windows cannot be deiconified, the state is dropped.
        dae = zd.Daemon(self.conf(ONE_RULE), workarea=lambda: WA)
        dae.focus = {
            "zone": "left",
            "target": 0x1,
            "workspace": 0,
            "monitor": WA,
            "minimized": {0x2},
        }
        dae.x11 = None
        zd._restore_focus_on_exit(dae)
        self.assertIsNone(dae.focus)

    def test_the_loop_wakes_up_for_the_palette_poll(self):
        # Without X events the wait is bounded by `theme_interval`, so a wallpaper change is
        # noticed by a daemon that is otherwise idle.
        class ThemeStub(StubDaemon):
            theme_interval = 0.02

            def __init__(self):
                super().__init__()
                self.polls = 0
                self.stop_requested = False

            def poll_theme(self):
                self.polls += 1
                self.stop_requested = True

        dae = ThemeStub()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(dae, x11=fake, read_clients=lambda: [])
        self.assertEqual(dae.polls, 1)
        self.assertTrue(fake.closed)

    def test_once_never_polls_the_palette(self):
        # A probe must not write theme.conf: `--once` keeps the unbounded wait and skips the poll.
        dae = StubDaemon()
        dae.theme_interval = 0.02
        dae.polls = 0

        def poll_theme():
            dae.polls += 1

        dae.poll_theme = poll_theme
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(dae, x11=fake, once=True, read_clients=lambda: [], once_timeout=0.02)
        self.assertEqual(dae.polls, 0)
        self.assertTrue(fake.closed)


class TestThemeRefresh(unittest.TestCase):
    """A generated `theme.conf` follows the wallpaper: the poll, its throttle and the strip."""

    def setUp(self):
        fd, self.conf = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(ONE_RULE)
        self.addCleanup(os.unlink, self.conf)

    def daemon(self, **kwargs):
        return zd.Daemon(self.conf, workarea=lambda: WA, **kwargs)

    def test_no_interval_means_no_poll(self):
        calls = []
        d = self.daemon(refresh_theme=lambda: calls.append(1))
        self.assertIsNone(d.poll_theme(now=100.0))
        self.assertEqual(calls, [])

    def test_the_poll_is_throttled_by_the_interval(self):
        calls = []

        def refresh():
            calls.append(1)

        d = self.daemon(refresh_theme=refresh, theme_interval=5.0)
        self.assertIsNone(d.poll_theme(now=100.0))
        self.assertIsNone(d.poll_theme(now=104.9))
        self.assertEqual(len(calls), 1)
        d.poll_theme(now=105.0)
        self.assertEqual(len(calls), 2)

    def test_a_rewrite_restarts_the_tab_strip(self):
        strip = FakeStrip()
        strip.payloads.append("{}")  # the process was up, so the restart can be seen
        d = self.daemon(
            refresh_theme=lambda: "/tmp/theme.conf",
            theme_interval=5.0,
            strip=strip,
            read_clients=lambda: [],
            active_window=lambda: None,
            current_workspace=lambda: 0,
        )
        self.assertEqual(d.poll_theme(now=100.0), "/tmp/theme.conf")
        self.assertEqual(strip.stopped, 1)
        self.assertEqual(json.loads(strip.payloads[-1]), {"strips": []})

    def test_the_palette_is_written_before_the_controls_theme(self):
        # The controls theme reads theme.conf: one tick must see the new palette, not the old.
        order = []
        d = self.daemon(
            refresh_theme=lambda: order.append("theme"),
            refresh_controls=lambda: order.append("controls"),
            theme_interval=5.0,
        )
        d.poll_theme(now=100.0)
        self.assertEqual(order, ["theme", "controls"])

    def test_the_controls_theme_is_refreshed_and_logged(self):
        calls = []

        def refresh_controls():
            calls.append(1)
            return "/tmp/Gridsnap"

        d = self.daemon(
            refresh_theme=lambda: None,
            refresh_controls=refresh_controls,
            theme_interval=5.0,
            verbose=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(d.poll_theme(now=100.0))
        self.assertEqual(len(calls), 1)
        self.assertIn("controls theme rebuilt from the palette: /tmp/Gridsnap", out.getvalue())
        self.assertNotIn("theme.conf rebuilt", out.getvalue())

    def test_a_controls_rewrite_does_not_restart_the_strip(self):
        # Only the palette of GridSnap's own surfaces needs a new strip process; GTK apps take
        # the controls theme on restart.
        strip = FakeStrip()
        strip.payloads.append("{}")
        d = self.daemon(
            refresh_theme=lambda: None,
            refresh_controls=lambda: "/tmp/Gridsnap",
            theme_interval=5.0,
            strip=strip,
        )
        d.poll_theme(now=100.0)
        self.assertEqual(strip.stopped, 0)

    def test_a_controls_failure_is_logged_once(self):
        error = ["first"]

        def refresh_controls():
            raise OSError(error[0])

        d = self.daemon(
            refresh_theme=lambda: None,
            refresh_controls=refresh_controls,
            theme_interval=5.0,
            verbose=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            d.poll_theme(now=100.0)
            error[0] = "second"
            d.poll_theme(now=200.0)
            d.poll_theme(now=300.0)
        text = out.getvalue()
        self.assertEqual(text.count("controls theme was not refreshed: first"), 1)
        self.assertEqual(text.count("controls theme was not refreshed: second"), 1)

    def test_a_failure_of_one_theme_does_not_hide_the_other(self):
        def broken_theme():
            raise ValueError("no wallpaper")

        d = self.daemon(
            refresh_theme=broken_theme,
            refresh_controls=lambda: "/tmp/Gridsnap",
            theme_interval=5.0,
            verbose=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            d.poll_theme(now=100.0)
        text = out.getvalue()
        self.assertIn("theme.conf was not refreshed: no wallpaper", text)
        self.assertIn("controls theme rebuilt from the palette: /tmp/Gridsnap", text)

    def test_a_failure_is_logged_once_and_does_not_break_the_loop(self):
        error = ["first"]

        def refresh():
            raise ValueError(error[0])

        d = self.daemon(refresh_theme=refresh, theme_interval=5.0, verbose=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(d.poll_theme(now=100.0))
            error[0] = "second"
            self.assertIsNone(d.poll_theme(now=200.0))
            self.assertIsNone(d.poll_theme(now=300.0))
        text = out.getvalue()
        self.assertEqual(text.count("theme.conf was not refreshed: first"), 1)
        self.assertEqual(text.count("theme.conf was not refreshed: second"), 1)


INSTALL_DAEMON = os.path.join(ROOT, "install-daemon.sh")


class TestAutostart(unittest.TestCase):
    """Autostart: `install-daemon.sh` installs the daemon and the binding of profiles to screens.

    There is no `.desktop` in the package: the install script writes it into
    `$XDG_CONFIG_HOME/autostart` (as for a workspace), so what is checked is the behaviour
    of the script, not a file in the repository.
    """

    def run_script(self, home, *args):
        env = dict(os.environ, XDG_CONFIG_HOME=home)
        return subprocess.run(
            ["bash", INSTALL_DAEMON, *args], text=True, capture_output=True, env=env
        )

    def test_install_writes_daemon_and_monitors_entries(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(home)
            self.assertEqual(r.returncode, 0, r.stderr)
            daemon_entry = os.path.join(home, "autostart", "gridsnap-daemon.desktop")
            monitors_entry = os.path.join(
                home, "autostart", "gridsnap-monitors.desktop"
            )
            self.assertTrue(os.path.exists(daemon_entry))
            self.assertTrue(os.path.exists(monitors_entry))
            with open(monitors_entry, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("[Desktop Entry]", text)
            # Exec — the same directory as the script's, and the pinned flags of the command.
            self.assertIn(
                f"Exec=python3 {ROOT}/zone-tiler.py profile use-monitors --quiet", text
            )
            self.assertIn("X-GNOME-Autostart-enabled=true", text)

    def test_list_and_remove_cover_both_entries(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_script(home).returncode, 0)
            listed = self.run_script(home, "--list")
            self.assertEqual(listed.returncode, 0)
            self.assertIn("gridsnap-daemon.desktop", listed.stdout)
            self.assertIn("gridsnap-monitors.desktop", listed.stdout)
            self.assertIn("profile use-monitors --quiet", listed.stdout)
            removed = self.run_script(home, "--remove")
            self.assertEqual(removed.returncode, 0)
            for name in ("gridsnap-daemon.desktop", "gridsnap-monitors.desktop"):
                self.assertFalse(
                    os.path.exists(os.path.join(home, "autostart", name)), name
                )

    def test_unknown_flag_is_argument_error(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(home, "--nope")
            self.assertEqual(r.returncode, 2)
            self.assertIn("неизвестный флаг", r.stderr)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, DAEMON, *args], text=True, capture_output=True
        )

    def test_version_imports_without_x(self):
        r = self.run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"zone-daemon {zd.VERSION}", r.stdout)

    def test_help(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--once", r.stdout)

    def test_missing_conf_is_config_error(self):
        r = self.run_cli("--conf", "/nonexistent/definitely.conf", "--once")
        self.assertEqual(r.returncode, 3)
        self.assertIn("config not found", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

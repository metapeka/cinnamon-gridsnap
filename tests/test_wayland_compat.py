#!/usr/bin/env python3
"""Tests for the Wayland compatibility of the window layer.

They check what was measured in ``docs/RESEARCH-wayland-recon.md``: in a Wayland session
Cinnamon does not publish `_NET_CURRENT_DESKTOP` before the first desktop switch, and a window
on another desktop is indistinguishable from a minimized one. No X server is needed: `xprop` is replaced.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import window_backend as wb  # noqa: E402

CURRENT = "_NET_CURRENT_DESKTOP"
ACTIVE = "_NET_ACTIVE_WINDOW"
DESKTOP = "_NET_WM_DESKTOP"


def xprop(answers=None, unavailable=False):
    """A replacement for `subprocess.check_output`: it answers by property name.

    `answers` — {property: answer line | Exception}; the property is not in the dictionary —
    we answer as `xprop` does for a missing one ("not found"). `unavailable` — the utility itself
    is absent (this is how a machine without X behaves).
    """

    def fake(argv, **_kwargs):
        if unavailable:
            raise FileNotFoundError("xprop")
        prop = next((a for a in argv[1:] if a.startswith("_")), "")
        value = (answers or {}).get(prop)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return f"{prop}:  not found\n"
        return value

    return fake


class TestCurrentWorkspaceOrFirst(unittest.TestCase):
    def test_property_wins(self):
        with mock.patch.object(
            wb.subprocess, "check_output", xprop({CURRENT: f"{CURRENT}(CARDINAL) = 2\n"})
        ):
            self.assertEqual(wb.current_workspace_or_first(), 2)

    def test_missing_property_while_x_answers_gives_first_desktop(self):
        # Exactly the case of a Cinnamon Wayland session: `xprop` answers "no such atom" and
        # exits with zero, that is X is alive but the property is not there. Without this `focus` and
        # `wmctrl -d` are dead until the first desktop switch.
        def answers(argv, **kwargs):
            if "_NET_SUPPORTED" in argv:
                return f"{'_NET_SUPPORTED'}(ATOM) = _NET_WM_NAME\n"
            return f"{CURRENT}:  no such atom on any window.\n"

        with mock.patch.object(wb.subprocess, "check_output", answers):
            self.assertIsNone(wb.current_workspace())
            self.assertEqual(wb.current_workspace_or_first(), 0)

    def test_x_unavailable_still_means_unknown(self):
        # X does not answer (no xprop or no display) — the desktop is still unknown,
        # and it must not be replaced with zero: on X11 the property does not disappear.
        with mock.patch.object(wb.subprocess, "check_output", xprop(unavailable=True)):
            self.assertIsNone(wb.current_workspace_or_first())

    def test_live_session_costs_one_call(self):
        # When everything is fine (the property is there) — not a single extra probe: every
        # command takes this path.
        calls = []

        def counting(argv, **kwargs):
            calls.append(list(argv))
            return f"{CURRENT}(CARDINAL) = 3\n"

        with mock.patch.object(wb.subprocess, "check_output", counting):
            self.assertEqual(wb.current_workspace_or_first(), 3)
        self.assertEqual(len(calls), 1, calls)

    def test_wayland_start_costs_one_liveness_probe(self):
        # The property is missing — "no X" must be told apart from "X is alive, the property is missing": one
        # extra probe, and only on this path.
        calls = []

        def answers(argv, **kwargs):
            calls.append(list(argv))
            if "_NET_SUPPORTED" in argv:
                return f"{'_NET_SUPPORTED'}(ATOM) = _NET_WM_NAME, _NET_WORKAREA\n"
            return f"{CURRENT}:  no such atom on any window.\n"

        with mock.patch.object(wb.subprocess, "check_output", answers):
            self.assertEqual(wb.current_workspace_or_first(), 0)
        self.assertEqual(len(calls), 2, calls)


class TestWindowOnOtherDesktop(unittest.TestCase):
    def test_true_when_desktops_differ(self):
        with mock.patch.object(
            wb.subprocess,
            "check_output",
            xprop({DESKTOP: f"{DESKTOP}(CARDINAL) = 1\n", CURRENT: f"{CURRENT}(CARDINAL) = 0\n"}),
        ):
            self.assertTrue(wb.window_on_other_desktop(0x400003))

    def test_false_when_desktops_match(self):
        with mock.patch.object(
            wb.subprocess,
            "check_output",
            xprop({DESKTOP: f"{DESKTOP}(CARDINAL) = 0\n", CURRENT: f"{CURRENT}(CARDINAL) = 0\n"}),
        ):
            self.assertFalse(wb.window_on_other_desktop(0x400003))

    def test_sticky_window_is_on_every_desktop(self):
        with mock.patch.object(
            wb.subprocess,
            "check_output",
            xprop({DESKTOP: f"{DESKTOP}(CARDINAL) = 4294967295\n", CURRENT: f"{CURRENT}(CARDINAL) = 1\n"}),
        ):
            self.assertIsNone(wb.window_on_other_desktop(0x400003))

    def test_unknown_current_desktop_is_not_a_guess(self):
        with mock.patch.object(
            wb.subprocess, "check_output", xprop({DESKTOP: f"{DESKTOP}(CARDINAL) = 3\n"})
        ):
            self.assertIsNone(wb.window_on_other_desktop(0x400003))


class TestPreferX11Gdk(unittest.TestCase):
    """GDK must go through X11: in a Wayland session `move()` does not work otherwise."""

    def setUp(self):
        self.addCleanup(lambda: os.environ.pop("GDK_BACKEND", None))
        os.environ.pop("GDK_BACKEND", None)

    def test_sets_x11_backend_when_unset(self):
        wb.prefer_x11_gdk()
        self.assertEqual(os.environ.get("GDK_BACKEND"), "x11")

    def test_does_not_override_explicit_choice(self):
        os.environ["GDK_BACKEND"] = "wayland"
        wb.prefer_x11_gdk()
        self.assertEqual(os.environ.get("GDK_BACKEND"), "wayland")

    def test_gdk_readers_ask_for_x11_themselves(self):
        # `monitors()`/`monitor_scales()` go through `gdk_monitors()`, and it must
        # enable the X11 backend before touching GDK — otherwise the area comes from another world.
        calls = []
        real = wb.prefer_x11_gdk

        def spy():
            calls.append(1)
            real()

        with mock.patch.object(wb, "prefer_x11_gdk", spy):
            with mock.patch.dict(sys.modules, {"gi": None, "gi.repository": None}):
                self.assertEqual(wb.gdk_monitors(), [])
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()

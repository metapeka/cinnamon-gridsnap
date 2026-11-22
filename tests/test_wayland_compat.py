#!/usr/bin/env python3
"""Тесты Wayland-совместимости оконного слоя.

Проверяется то, что замерено в ``docs/RESEARCH-wayland-recon.md``: в Wayland-сессии
Cinnamon `_NET_CURRENT_DESKTOP` не опубликован до первого переключения стола, а окно
на другом столе неотличимо от свёрнутого. X-сервер не нужен: `xprop` подменяется.
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
    """Подмена `subprocess.check_output`: отвечает по имени свойства.

    `answers` — {свойство: строка ответа | Exception}; свойства нет в словаре —
    отвечаем как `xprop` на отсутствующее («not found»). `unavailable` — нет самой
    утилиты (так ведёт себя машина без X).
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
        # Ровно случай Wayland-сессии Cinnamon: `xprop` отвечает «no such atom» и
        # выходит с нулём, то есть X жив, а свойства нет. Без этого `focus` и
        # `wmctrl -d` мертвы до первого переключения стола.
        def answers(argv, **kwargs):
            if "_NET_SUPPORTED" in argv:
                return f"{'_NET_SUPPORTED'}(ATOM) = _NET_WM_NAME\n"
            return f"{CURRENT}:  no such atom on any window.\n"

        with mock.patch.object(wb.subprocess, "check_output", answers):
            self.assertIsNone(wb.current_workspace())
            self.assertEqual(wb.current_workspace_or_first(), 0)

    def test_x_unavailable_still_means_unknown(self):
        # X не отвечает (нет xprop или нет дисплея) — стол по-прежнему неизвестен,
        # и подменять его нулём нельзя: на X11 свойство не пропадает.
        with mock.patch.object(wb.subprocess, "check_output", xprop(unavailable=True)):
            self.assertIsNone(wb.current_workspace_or_first())

    def test_live_session_costs_one_call(self):
        # Когда всё в порядке (свойство есть) — ни одной лишней пробы: этим путём
        # идёт каждая команда.
        calls = []

        def counting(argv, **kwargs):
            calls.append(list(argv))
            return f"{CURRENT}(CARDINAL) = 3\n"

        with mock.patch.object(wb.subprocess, "check_output", counting):
            self.assertEqual(wb.current_workspace_or_first(), 3)
        self.assertEqual(len(calls), 1, calls)

    def test_wayland_start_costs_one_liveness_probe(self):
        # Свойства нет — надо отличить «нет X» от «X жив, свойства нет»: одна
        # дополнительная проба, и только на этом пути.
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
    """GDK обязан идти через X11: в Wayland-сессии иначе `move()` не работает."""

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
        # `monitors()`/`monitor_scales()` ходят через `gdk_monitors()`, и он обязан
        # включить X11-бэкенд до обращения к GDK — иначе область придёт из другого мира.
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты примитивов режима «только эта зона» — без X-сервера и GTK.

Проверяют то, что не требует дисплея: отбор окон для сворачивания
(`focus_candidates`), состояние режима в `focus.json` и сами EWMH-запросы
(поддельное X-соединение вместо живого X11).

Живое сворачивание muffin проверяется дымовым `tests/smoke-focus.sh`.

Запуск:
    python3 -m unittest discover -s tests -v
    python3 tests/test_focus.py
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_focus_test", TILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()

WA = (0, 0, 1000, 1000)
WA1 = (1000, 0, 800, 600)


class FakeX11:
    """Поддельное X-соединение: запоминает, что и кому послали."""

    def __init__(self, desktop=0):
        self.iconified = []
        self.activated = []
        self.desktops = []
        self.closed = 0
        self.desktop = desktop

    def iconify(self, win):
        self.iconified.append(win)

    def activate(self, win):
        self.activated.append(win)

    def set_current_desktop(self, desktop):
        self.desktops.append(desktop)
        self.desktop = desktop

    def close(self):
        self.closed += 1


class TestFocusCandidates(unittest.TestCase):
    """Какие окна сворачивает режим: тот же стол и монитор, кроме цели."""

    def test_same_workspace_and_monitor_only(self):
        entries = [
            (0x1, 0, WA),  # цель
            (0x2, 0, WA),  # сворачиваем
            (0x3, 1, WA),  # чужой стол
            (0x4, 0, WA1),  # чужой монитор
            (0x5, 0, WA),  # сворачиваем
        ]
        self.assertEqual(zt.focus_candidates(entries, 0x1, 0, WA), [0x2, 0x5])

    def test_order_is_client_list_order(self):
        entries = [(0x9, 0, WA), (0x2, 0, WA), (0x5, 0, WA)]
        self.assertEqual(zt.focus_candidates(entries, 0x9, 0, WA), [0x2, 0x5])

    def test_target_never_in_candidates(self):
        entries = [(0x7, 0, WA), (0x7, 0, WA)]
        self.assertEqual(zt.focus_candidates(entries, 0x7, 0, WA), [])

    def test_sticky_window_visible_everywhere(self):
        # `None` — липкое окно (`_NET_WM_DESKTOP = 0xFFFFFFFF`): видно на любом столе.
        entries = [(0x2, None, WA)]
        self.assertEqual(zt.focus_candidates(entries, 0x1, 3, WA), [0x2])

    def test_unknown_active_workspace_keeps_only_sticky(self):
        # Активный стол не прочитан: сворачивать окна чужих столов вслепую нельзя.
        entries = [(0x2, None, WA), (0x3, 0, WA), (0x4, 2, WA)]
        self.assertEqual(zt.focus_candidates(entries, 0x1, None, WA), [0x2])

    def test_empty_entries(self):
        self.assertEqual(zt.focus_candidates([], 0x1, 0, WA), [])


class TestFocusPrimitives(unittest.TestCase):
    """EWMH-запросы сворачивания и разворота — на поддельном соединении."""

    def test_iconify_uses_given_connection_and_keeps_it(self):
        x11 = FakeX11()
        zt.iconify(0x2A, x11=x11, quiet=True)
        self.assertEqual(x11.iconified, [0x2A])
        self.assertEqual(x11.closed, 0)  # чужое соединение не закрываем

    def test_deiconify_activates_and_keeps_connection(self):
        x11 = FakeX11()
        zt.deiconify(0x2A, x11=x11)
        self.assertEqual(x11.activated, [0x2A])
        self.assertEqual(x11.closed, 0)

    def test_window_iconic_reads_wm_state(self):
        iconic = "WM_STATE(WM_STATE):\n\twindow state: Iconic\n\ticon window: 0x0\n"
        with mock.patch.object(zt.subprocess, "check_output", return_value=iconic):
            self.assertTrue(zt.window_iconic(0x1))

    def test_window_iconic_reads_hidden_state(self):
        hidden = "WM_STATE: Normal\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN\n"
        with mock.patch.object(zt.subprocess, "check_output", return_value=hidden):
            self.assertTrue(zt.window_iconic(0x1))

    def test_window_iconic_false_for_normal_window(self):
        normal = "WM_STATE: Normal\n_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED\n"
        with mock.patch.object(zt.subprocess, "check_output", return_value=normal):
            self.assertFalse(zt.window_iconic(0x1))

    def test_window_iconic_tolerates_unreadable_window(self):
        with mock.patch.object(zt.subprocess, "check_output", side_effect=OSError):
            self.assertFalse(zt.window_iconic(0x1))

    def test_iconic_state_value_is_icccm_iconic(self):
        self.assertEqual(zt.ICONIC_STATE, 3)
        self.assertEqual(zt.SOURCE_PAGER, 2)


class TestFocusStateFile(unittest.TestCase):
    """`focus.json` — состояние режима для ветки без демона."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-focus-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = os.path.join(self.dir, "focus.json")

    def state(self):
        return {
            "zone": "main",
            "workspace": 0,
            "monitor": list(WA),
            "target": {"window": "0x1a00007", "wm_class": "Code"},
            "windows": [{"window": "0x1a00009", "wm_class": "firefox"}],
        }

    def test_missing_file_is_inactive_mode(self):
        self.assertEqual(zt.load_focus_state(self.path), {})

    def test_round_trip(self):
        zt.save_focus_state(self.state(), path=self.path)
        loaded = zt.load_focus_state(self.path)
        self.assertEqual(loaded["zone"], "main")
        self.assertEqual(loaded["windows"][0]["window"], "0x1a00009")
        self.assertEqual(loaded["version"], zt.FOCUS_STATE_VERSION)

    def test_clear_is_idempotent(self):
        zt.save_focus_state(self.state(), path=self.path)
        zt.clear_focus_state(self.path)
        self.assertEqual(zt.load_focus_state(self.path), {})
        zt.clear_focus_state(self.path)  # повторный вызов не падает

    def test_foreign_version_is_inactive(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 99, "windows": []}\n')
        self.assertEqual(zt.load_focus_state(self.path), {})

    def test_broken_file_is_inactive(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        self.assertEqual(zt.load_focus_state(self.path), {})

    def test_state_without_windows_is_inactive(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1}\n')
        self.assertEqual(zt.load_focus_state(self.path), {})

    def test_save_is_atomic_and_leaves_no_tmp(self):
        zt.save_focus_state(self.state(), path=self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertTrue(os.path.exists(self.path))

    def test_default_path_is_under_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.dir}):
            self.assertEqual(
                zt.focus_path(), os.path.join(self.dir, "gridsnap", "focus.json")
            )


class TestFocusRestore(unittest.TestCase):
    """Разворот по записи: только свои окна, цель последней, остаток не теряется.

    `window_iconic` подменён на «всё развернулось», иначе проверка состояния
    трогала бы настоящий `xprop` без дисплея; отдельный тест оставляет окно
    свёрнутым и проверяет «остаток». Паузы проверки сведены к нулю.
    """

    def state(self):
        return {
            "zone": "main",
            "workspace": 0,
            "monitor": [0, 0, 1000, 1000],
            "target": {"window": "0x10", "wm_class": "Code"},
            "windows": [
                {"window": "0x11", "wm_class": "firefox"},
                {"window": "0x12", "wm_class": "chromium"},
                {"window": "0x13", "wm_class": "Telegram"},
            ],
        }

    def restore(self, state, alive, classes, iconic=(), desktop=0):
        x11 = FakeX11(desktop=desktop)
        with mock.patch.object(zt, "read_client_list", return_value=alive), \
                mock.patch.object(zt, "current_workspace", side_effect=lambda: x11.desktop), \
                mock.patch.object(zt, "window_iconic", side_effect=lambda w: w in iconic), \
                mock.patch.object(zt, "FOCUS_VERIFY_DELAY", 0), \
                mock.patch.object(
                    zt, "window_class_title",
                    side_effect=lambda w: (classes.get(w, ""), ""),
                ):
            restored, remaining = zt._focus_restore(state, x11=x11)
        return x11, restored, remaining

    def test_restores_own_windows_and_target_last(self):
        alive = [0x10, 0x11, 0x12, 0x13]
        classes = {0x10: "Code", 0x11: "firefox", 0x12: "chromium", 0x13: "Telegram"}
        x11, restored, remaining = self.restore(self.state(), alive, classes)
        self.assertEqual(restored, [0x11, 0x12, 0x13, 0x10])
        self.assertEqual(remaining, [])
        # цель активируется последней — внимание остаётся на ней
        self.assertEqual(x11.activated, [0x11, 0x12, 0x13, 0x10])
        self.assertEqual(x11.closed, 0)
        self.assertEqual(x11.desktops, [])  # стол записи уже активен

    def test_switches_to_record_workspace_before_restoring(self):
        # Активация окна не на активном столе muffin-ом игнорируется: сначала стол.
        alive = [0x10, 0x11, 0x12, 0x13]
        classes = {0x10: "Code", 0x11: "firefox", 0x12: "chromium", 0x13: "Telegram"}
        x11, restored, _ = self.restore(self.state(), alive, classes, desktop=2)
        self.assertEqual(x11.desktops, [0])
        self.assertEqual(len(restored), 4)

    def test_skips_window_gone_from_client_list(self):
        alive = [0x10, 0x11, 0x13]
        classes = {0x10: "Code", 0x11: "firefox", 0x12: "chromium", 0x13: "Telegram"}
        x11, restored, remaining = self.restore(self.state(), alive, classes)
        self.assertEqual(restored, [0x11, 0x13, 0x10])
        self.assertEqual(remaining, [])
        self.assertNotIn(0x12, x11.activated)

    def test_skips_window_with_reused_xid(self):
        # XID переиспользован другой программой — разворачивать чужое нельзя.
        alive = [0x10, 0x11, 0x12, 0x13]
        classes = {0x10: "Code", 0x11: "firefox", 0x12: "Другое", 0x13: "Telegram"}
        x11, restored, remaining = self.restore(self.state(), alive, classes)
        self.assertEqual(restored, [0x11, 0x13, 0x10])
        self.assertEqual(remaining, [])

    def test_leftover_windows_are_reported_not_dropped(self):
        # Развернуть не вышло (не тот стол, отказ WM) — это не «развёрнуто».
        alive = [0x10, 0x11, 0x12, 0x13]
        classes = {0x10: "Code", 0x11: "firefox", 0x12: "chromium", 0x13: "Telegram"}
        x11, restored, remaining = self.restore(
            self.state(), alive, classes, iconic={0x12}
        )
        self.assertEqual(restored, [0x11, 0x13, 0x10])
        self.assertEqual(remaining, [0x12])

    def test_empty_state_restores_nothing(self):
        x11, restored, remaining = self.restore({}, [0x10], {0x10: "Code"})
        self.assertEqual(restored, [])
        self.assertEqual(remaining, [])
        self.assertEqual(x11.activated, [])

    def test_window_id_parsing(self):
        self.assertEqual(zt._focus_window_id({"window": "0x1a"}), 0x1A)
        self.assertEqual(zt._focus_window_id({"window": 26}), 26)
        self.assertIsNone(zt._focus_window_id({"window": None}))
        self.assertIsNone(zt._focus_window_id({"window": "мусор"}))
        self.assertIsNone(zt._focus_window_id({"нет": 1}))

    def test_alive_without_recorded_class_is_trusted(self):
        self.assertTrue(zt._focus_alive(0x1, {}, {0x1}))
        self.assertFalse(zt._focus_alive(0x2, {}, {0x1}))


class TestFocusResponse(unittest.TestCase):
    """Печать ответа демона `--quiet` и без."""

    def printed(self, response, quiet=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zt._print_focus_response(response, quiet=quiet)
        return buf.getvalue().strip()

    def test_enter_summary(self):
        text = self.printed(
            {"ok": True, "zone": "main", "window": "0x1a", "minimized": ["0x2", "0x3"]}
        )
        self.assertIn("0x1a -> зона 'main'", text)
        self.assertIn("свёрнуто окон — 2", text)

    def test_exit_summary(self):
        text = self.printed({"ok": True, "off": True, "restored": ["0x2"]})
        self.assertIn("выключен", text)
        self.assertIn("развёрнуто окон — 1", text)

    def test_quiet_prints_nothing(self):
        self.assertEqual(self.printed({"ok": True, "off": True}, quiet=True), "")


class TestFocusCommand(unittest.TestCase):
    """CLI без демона: `--off` при неактивном режиме не падает и не шумит."""

    def args(self, **kw):
        base = {"zone": None, "off": False, "window": None, "quiet": False, "dry_run": False}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_focus_without_zone_requests_mode_without_zone(self):
        # Форма клавиши `[focus]` без `zone`: «свернуть всё, кроме этого окна».
        seen = {}

        def fake(request, timeout=None):
            seen.update(request)
            return {"ok": True, "off": False, "zone": None, "window": "0x1", "minimized": []}

        with mock.patch.object(zt, "daemon_request", side_effect=fake), \
                mock.patch.object(zt, "load_focus_state", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            zt.cmd_focus(None, self.args())
        self.assertNotIn("zone", seen)
        self.assertEqual(seen["cmd"], "focus")
        self.assertNotIn("off", seen)

    def test_off_without_state_and_without_daemon_reports_nothing_restored(self):
        buf = io.StringIO()
        with mock.patch.object(zt, "daemon_request", return_value=None), \
                mock.patch.object(zt, "load_focus_state", return_value={}), \
                mock.patch.object(zt, "clear_focus_state"), \
                contextlib.redirect_stdout(buf):
            zt.cmd_focus(None, self.args(off=True))
        self.assertIn("развёрнуто окон — 0", buf.getvalue())

    def test_daemon_response_errors_are_fatal(self):
        with mock.patch.object(zt, "daemon_request", return_value={"ok": False, "error": "нет зоны"}), \
                mock.patch.object(zt, "load_focus_state", return_value={}), \
                mock.patch.object(zt, "fail", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                zt.cmd_focus(None, self.args(zone="main"))


if __name__ == "__main__":
    unittest.main()

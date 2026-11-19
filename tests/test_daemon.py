#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты демона GridSnap — без X-сервера и без GTK.

`zone-daemon.py` загружается через importlib (имя файла с дефисом), но сама
загрузка не открывает X: цикл событий живёт в `serve()` и вызывается только из
CLI. Реакция на события проверяется через инъекцию зависимостей `Daemon`.

Покрыто: вложение по правилу на MapNotify, игнор override_redirect и диалогов,
защита от повторного MapNotify, пустой WM_CLASS, tolerant-чтение `[daemon]`,
reflow=off (ничего не двигает) и reflow=neighbors (сосед расширяется при
DestroyNotify). Отдельно — разделение DestroyNotify (снять с трекера + reflow)
и UnmapNotify (no-op: окно остаётся в трекере, соседей не двигаем) и разводка
событий в `handle_event`.

Запуск:
    python3 tests/test_daemon.py
"""

import ctypes
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "zone-daemon.py")


def load_daemon():
    spec = importlib.util.spec_from_file_location("zone_daemon_under_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zd = load_daemon()

WA = (0, 0, 1000, 1000)

# Две соседние зоны: left примыкает к right по вертикальному ребру длиной 1.
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


class TestDaemonEvents(unittest.TestCase):
    def setUp(self):
        self.applied = []
        self.metas = {}
        self.reads = []

    # -- обвязка

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def daemon(self, text=ONE_RULE, reflow=None):
        if reflow is not None:
            text = text + f"[daemon]\nreflow = {reflow}\n"
        return zd.Daemon(
            self.conf(text),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )

    def apply(self, win, rect, label="", quiet=False):
        self.applied.append((win, rect, label, quiet))

    def read_meta(self, win):
        self.reads.append(win)
        return self.metas[win]

    def meta(self, wm_class="firefox-esr", wm_title="", window_type=()):
        return zd.Meta(wm_class, wm_title, frozenset(window_type))

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
        self.assertEqual(self.reads, [])  # метаданные даже не читались
        self.assertEqual(d.windows, {})

    def test_dialog_window_type_ignored(self):
        self.metas[0x1] = self.meta(
            "firefox-esr", window_type={"_NET_WM_WINDOW_TYPE_DIALOG"}
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
            "TOOLTIP",  # короткое имя тоже распознаётся
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
        self.assertEqual(self.reads, [0x1])  # второй раз не перечитывали

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

    # -- Destroy: реальное закрытие окна

    def test_destroy_with_reflow_off_moves_nothing(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES)  # reflow по умолчанию off
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
        # left освободилась, right расширяется на всю рабочую область
        self.assertEqual(self.applied, [(0x2, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.windows, {0x2: "right"})

    # -- Unmap: сворачивание/скрытие/смена воркспейса -> no-op

    def test_unmap_with_reflow_neighbors_moves_nothing(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        self.applied.clear()
        d.on_unmap(0x1)  # окно свёрнуто, но ещё живо
        self.assertEqual(self.applied, [])  # соседей не двигаем

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
        d.on_unmap(0x1)   # свернули
        d.on_map(0x1)     # развернули обратно
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.reads, [0x1])  # повторно метаданные не читали
        self.assertEqual(d.windows, {0x1: "left"})

    def test_unmap_then_destroy_still_reflows(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)
        d.on_unmap(0x1)  # свернули — трекер и раскладка на месте
        self.applied.clear()
        d.on_destroy(0x1)  # действительно закрыли
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
        self.assertEqual(d.windows, {})  # неудача не запоминается
        d.on_map(0x1)  # следующий MapNotify пробует снова
        self.assertEqual(d.windows, {0x1: "left"})
        self.assertEqual(len(calls), 2)

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
        # повторный destroy уже ничего не делает
        d.on_destroy(0x1)
        self.assertEqual(self.applied[1:], [])

    def test_reflow_only_moves_remaining_tracked_window(self):
        # freed left занята ещё одним окном -> reflow не должен ничего делать.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("firefox-esr")
        self.metas[0x3] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.on_map(0x1)
        d.on_map(0x2)  # оба окна замаплены в left; трекер хранит оба
        d.on_map(0x3)
        self.applied.clear()
        d.on_destroy(0x1)
        # left всё ещё занята окном 0x2 -> reflow возвращает {}
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x2: "left", 0x3: "right"})

    # -- tolerant-чтение [daemon]

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


class TestReadWindowMeta(unittest.TestCase):
    """Дефолтный read_window_meta: один retry, если класс/заголовок пусты."""

    def setUp(self):
        self._class = zd.tiler.window_class_title
        self._types = zd.window_types
        self._sleep = zd.time.sleep
        zd.time.sleep = lambda _s: None
        zd.window_types = lambda _win: frozenset()
        self.addCleanup(self._restore)

    def _restore(self):
        zd.tiler.window_class_title = self._class
        zd.window_types = self._types
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


class TestHandleEventDispatch(unittest.TestCase):
    """handle_event: MapNotify -> on_map, DestroyNotify -> on_destroy, UnmapNotify -> on_unmap.

    Именно разводка гарантирует, что сворачивание (UnmapNotify) не проходит через
    reflow-путь, зарезервированный за реальным закрытием (DestroyNotify).

    Буферы событий собираются по эталонным структурам Xlib (не по структурам
    демона), чтобы тест ловил и разъезд offset'ов относительно X11 ABI.
    """

    # Копии Xlib.h: type, serial, send_event, display, event, window[, override].
    class XMapEventRef(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
            ("event", ctypes.c_ulong), ("window", ctypes.c_ulong),
            ("override_redirect", ctypes.c_int),
        ]

    class XUnmapEventRef(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
            ("event", ctypes.c_ulong), ("window", ctypes.c_ulong),
            ("from_configure", ctypes.c_int),
        ]

    class XDestroyWindowEventRef(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
            ("event", ctypes.c_ulong), ("window", ctypes.c_ulong),
        ]

    class Stub:
        def __init__(self):
            self.calls = []

        def on_map(self, win, override_redirect=False):
            self.calls.append(("map", win, override_redirect))

        def on_destroy(self, win):
            self.calls.append(("destroy", win))

        def on_unmap(self, win):
            self.calls.append(("unmap", win))

    def event_buffer(self, struct, window, override_redirect=0):
        ev = struct()
        ctypes.memset(ctypes.byref(ev), 0, ctypes.sizeof(ev))
        ev.window = window
        # `event` (root) заполняется ненулевым: если демон перепутает поля,
        # тест поймает root вместо окна.
        ev.event = 0x46F
        if hasattr(ev, "override_redirect"):
            ev.override_redirect = override_redirect
        buf = (ctypes.c_char * zd.EVENT_BUFFER._length_)()
        ctypes.memmove(buf, ctypes.byref(ev), ctypes.sizeof(ev))
        return buf

    def test_map_notify_calls_on_map(self):
        dae = self.Stub()
        buf = self.event_buffer(self.XMapEventRef, 0x11)
        zd.handle_event(dae, buf, zd.MAP_NOTIFY)
        self.assertEqual(dae.calls, [("map", 0x11, False)])

    def test_map_notify_forwards_override_redirect(self):
        dae = self.Stub()
        buf = self.event_buffer(self.XMapEventRef, 0x12, override_redirect=1)
        zd.handle_event(dae, buf, zd.MAP_NOTIFY)
        self.assertEqual(dae.calls, [("map", 0x12, True)])

    def test_destroy_notify_calls_on_destroy(self):
        dae = self.Stub()
        buf = self.event_buffer(self.XDestroyWindowEventRef, 0x22)
        zd.handle_event(dae, buf, zd.DESTROY_NOTIFY)
        self.assertEqual(dae.calls, [("destroy", 0x22)])

    def test_unmap_notify_calls_on_unmap(self):
        dae = self.Stub()
        buf = self.event_buffer(self.XUnmapEventRef, 0x33)
        zd.handle_event(dae, buf, zd.UNMAP_NOTIFY)
        self.assertEqual(dae.calls, [("unmap", 0x33)])


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
        self.assertIn("не найден конфиг", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

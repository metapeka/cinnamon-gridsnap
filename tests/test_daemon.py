#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты демона GridSnap — без X-сервера и без GTK.

`zone-daemon.py` загружается через importlib (имя файла с дефисом), но сама
загрузка не открывает X: цикл событий живёт в `serve()` и вызывается только из
CLI. Реакция на события проверяется через инъекцию зависимостей `Daemon`.

Покрыто: вложение по правилу при появлении окна в `_NET_CLIENT_LIST`, игнор
override_redirect и диалогов, защита от повторного появления, пустой WM_CLASS,
tolerant-чтение `[daemon]`, `read_client_list` (парсинг xprop), реконсиляция
списка (добавление/исчезновение, повтор без перечитывания, стабильность при
сворачивании), reflow=off/neighbors, разводка PropertyNotify (только
`_NET_CLIENT_LIST`) и цикл `serve()` на фейковом X11.

Запуск:
    python3 tests/test_daemon.py
"""

import ctypes
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

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


# Ожидаемые смещения XPropertyEvent из Xlib.h на LP64 (тот же ABI, что у
# `zd.XPropertyEvent`). Зашиты числами, чтобы тест ловил разъезд с ABI, а не
# повторял ту же структуру, что и проверяемый код.
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
    """XEvent-буфер с XPropertyEvent для PropertyNotify.

    Собирается сырыми байтами по ожидаемым смещениям Xlib.h (LP64), а не через
    копию структуры из проверяемого модуля, — иначе тест повторял бы его ошибки.
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

    # -- реконсиляция по _NET_CLIENT_LIST

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
        self.assertEqual(self.reads, [])  # метаданные не перечитывались

    def test_reconcile_removes_disappeared_window(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES)  # reflow off
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        d.reconcile([0x2])  # 0x1 закрылось и ушло из списка
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
        # Сворачивание не меняет _NET_CLIENT_LIST -> список тот же, ничего не двигаем.
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        self.applied.clear()
        d.reconcile([0x1, 0x2])
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_reconcile_does_not_recheck_unmatched(self):
        self.metas[0x1] = self.meta("libreoffice")  # нет правила
        d = self.daemon()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, {0x1})
        self.reads.clear()
        d.reconcile([0x1])  # тот же список -> метаданные не трогаем
        self.assertEqual(self.reads, [])

    def test_reconcile_ignored_type_is_seen_but_untracked(self):
        self.metas[0x1] = self.meta(
            "firefox-esr", window_type={"_NET_WM_WINDOW_TYPE_DIALOG"}
        )
        d = self.daemon()
        d.reconcile([0x1])
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, {0x1})

    def test_reconcile_new_window_occupies_freed_zone_before_reflow(self):
        # 0x1 закрылось, 0x3 открылось в left: right не должен расширяться.
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

    def test_default_apply_reuses_daemon_connection(self):
        # При дефолтном apply демон передаёт своё X-соединение (чужое не закрыть).
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
        # `fail()` в layout-коде бросает SystemExit — демон должен его пережить.
        self.metas[0x1] = self.meta("firefox-esr")

        def boom(win, rect, label="", quiet=False):
            raise SystemExit(1)

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=boom,
            read_meta=self.read_meta,
            workarea=lambda: WA,
        )
        d.on_map(0x1)  # не должно выбросить SystemExit
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, set())

    def test_keyboard_interrupt_from_apply_is_not_swallowed(self):
        # Прерывание с клавиатуры не маскируется под ошибку X.
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
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0], (0x1, (0, 0, 500, 1000), "left", True))

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


class TestPropertyEvent(unittest.TestCase):
    """handle_property_event: реконсиляция только на PropertyNotify о _NET_CLIENT_LIST.

    Буфер событий собирается сырыми байтами по ожидаемым смещениям XPropertyEvent
    из Xlib.h (LP64), а не копией структуры из проверяемого модуля: тест ловит
    разъезд offset'ов относительно X11 ABI.
    """

    class Stub:
        def __init__(self):
            self.calls = []

        def reconcile(self, client_ids):
            self.calls.append(list(client_ids))

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
        self.assertTrue(handled)  # событие про наш атом
        self.assertEqual(dae.calls, [])  # но реконсиляции нет

    def test_other_atom_is_ignored(self):
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xDEF), 0xABC, lambda: [0x9]
        )
        self.assertFalse(handled)
        self.assertEqual(dae.calls, [])


class TestReadClientList(unittest.TestCase):
    """read_client_list: разбор `xprop -root _NET_CLIENT_LIST`."""

    def setUp(self):
        self._check_output = zd.subprocess.check_output
        self.addCleanup(self._restore)

    def _restore(self):
        zd.subprocess.check_output = self._check_output

    def test_parses_window_ids(self):
        zd.subprocess.check_output = lambda *a, **k: (
            "_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0x600005\n"
        )
        self.assertEqual(zd.read_client_list(), [0x400003, 0x600005])

    def test_asks_for_net_client_list_on_root(self):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = args
            return "_NET_CLIENT_LIST(WINDOW): window id # 0x1\n"

        zd.subprocess.check_output = fake
        zd.read_client_list()
        self.assertEqual(seen["args"], ["xprop", "-root", "_NET_CLIENT_LIST"])

    def test_empty_when_property_missing(self):
        # xprop при отсутствии свойства печатает сообщение и выходит с кодом 0 —
        # это не «клиентов нет», а «список недоступен».
        zd.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST:  not found.\n"
        self.assertIsNone(zd.read_client_list())

    def test_none_when_no_such_atom(self):
        zd.subprocess.check_output = lambda *a, **k: (
            "no such atom on any window.\n"
        )
        self.assertIsNone(zd.read_client_list())

    def test_none_without_window_id_marker(self):
        # Вывод без маркера `window id #` не разбирается как список окон.
        zd.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST(WINDOW): \n"
        self.assertIsNone(zd.read_client_list())

    def test_empty_list_when_property_present_but_no_windows(self):
        # Валидное состояние «клиентов нет» — это [], а не None.
        zd.subprocess.check_output = lambda *a, **k: (
            "_NET_CLIENT_LIST(WINDOW): window id #\n"
        )
        self.assertEqual(zd.read_client_list(), [])

    def test_none_on_error(self):
        def boom(*args, **kwargs):
            raise OSError("нет xprop")

        zd.subprocess.check_output = boom
        self.assertIsNone(zd.read_client_list())


class _StubFunc:
    """Обёртка callable с полями argtypes/restype (как у ctypes _FuncPtr)."""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class FakeX11:
    """Мини-заглушка X11: очередь событий и всё, что зовёт `serve`."""

    CLIENT_LIST_ATOM = 0xABC

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
        return self.CLIENT_LIST_ATOM if name == zd.CLIENT_LIST_ATOM else 0xDEF

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
    """Демон-заглушка для serve(): только фиксирует реконсиляции."""

    def __init__(self):
        self.reconciled = []
        self.reflow = zd.REFLOW_OFF
        self.logs = []

    def reconcile(self, client_ids):
        self.reconciled.append(list(client_ids))

    def log(self, message):
        self.logs.append(message)


class TestServeLoop(unittest.TestCase):
    """serve(): PropertyChangeMask на root, стартовая реконсиляция и PropertyNotify."""

    def pipe_fd(self):
        """fd, который никогда не станет читаемым (для таймаута в --once)."""
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
        # serve() передаёт соединение демону, чтобы apply_zone переиспользовал его
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
        dae = StubDaemon()
        dae.windows = {0x1: "left"}
        dae.seen = {0x1}
        fake = FakeX11(
            events=[property_buffer(FakeX11.CLIENT_LIST_ATOM)], fd=self.pipe_fd()
        )
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: None, once_timeout=0.05
        )
        # список не прочитался — ни реконсиляции, ни изменений трекера
        self.assertEqual(dae.reconciled, [])
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
        # первая — стартовая, вторая — по PropertyNotify о _NET_CLIENT_LIST
        self.assertEqual(dae.reconciled, [[0x22], [0x22]])
        self.assertTrue(fake.closed)

    def test_other_property_notify_is_ignored(self):
        dae = StubDaemon()
        fake = FakeX11(events=[property_buffer(0xDEF)], fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x22]])  # только стартовая

    def test_once_without_events_times_out(self):
        dae = StubDaemon()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(dae, x11=fake, once=True, read_clients=lambda: [], once_timeout=0.05)
        self.assertEqual(dae.reconciled, [[]])
        self.assertTrue(fake.closed)


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

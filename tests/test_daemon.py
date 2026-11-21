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
сворачивании), reflow=off/neighbors, возврат расширенной зоны, когда
освобождённую занимает новое окно, выбор монитора окна (мультимониторность),
разводка PropertyNotify (только `_NET_CLIENT_LIST`) и цикл `serve()` на фейковом X11.

Рабочие столы: сетка берётся из профиля стола (`[workspaces]`), занятость и
reflow не переходят через стол, окна невидимых столов не двигаются, а
`_NET_CURRENT_DESKTOP` меняет только `self.workspace`.

Режим «только эта зона»: вход/`off`/toggle, сворачивание только окон того же
стола и монитора (исключения и свёрнутое вручную — мимо), разворот на выходе с
активацией цели последней, снятие режима по исчезновению цели и смене стола,
сворачивание новых окон того же стола/монитора и поле `focus` в `status`.

Запуск:
    python3 tests/test_daemon.py
"""

import ctypes
import importlib.util
import os
import shutil
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
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zd = load_daemon()

WA = (0, 0, 1000, 1000)
WA1 = (1000, 0, 800, 600)

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


def filled_key(wa, zone, workspace=None):
    """Ключ покрытия в `Daemon.filled`: (стол, рабочая область, зона).

    Покрытие расширенного соседа живёт на конкретном столе и мониторе: одна и
    та же зона на разных столах/экранах — разные записи. `workspace=None` —
    липкое окно или неизвестный активный стол.
    """
    return (workspace, wa, zone)


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


class DaemonTestCase(unittest.TestCase):
    """Общая обвязка сценариев демона: без X, с подменой apply/метаданных/столов.

    `self.current` — активный стол, `self.workspaces` — стол каждого окна;
    читатели столов подменены, поэтому тесты не зависят от живого `xprop` и от
    того, какой стол открыт на машине сборки.
    """

    # Тип окна диалога: служебный, демон его не двигает и не сворачивает.
    DIALOG = {"_NET_WM_WINDOW_TYPE_DIALOG"}

    def setUp(self):
        self.applied = []
        self.metas = {}
        self.reads = []
        self.centers = {}
        self.current = None
        self.workspaces = {}
        # Инъекции режима «только эта зона»: что свернули/развернули и какой
        # список клиентов видит демон. `iconic` — подмена `window_iconic`.
        self.iconified = []
        self.iconify_calls = []
        self.deiconified = []
        self.deiconify_calls = []
        self.iconic = set()
        self.clients = []

    # -- обвязка

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def conf_dir(self):
        """Пустой временный каталог GridSnap (для `zones.conf` и `templates/`)."""
        directory = tempfile.mkdtemp(prefix="gridsnap-daemon-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def daemon(self, text=ONE_RULE, reflow=None, directory=None):
        """Демон без X; `directory` — готовый каталог GridSnap с `templates/`."""
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

    def multi_daemon(self, text=TWO_ZONES, reflow=None, monitors=(WA, WA1)):
        """Демон с двумя мониторами; монитор окна — по self.centers."""
        if reflow is not None:
            text = text + f"[daemon]\nreflow = {reflow}\n"
        return zd.Daemon(
            self.conf(text),
            apply=self.apply,
            read_meta=self.read_meta,
            monitors=lambda: list(monitors),
            window_center=lambda win: self.centers[win],
            current_workspace=lambda: self.current,
            window_workspace=lambda win: self.workspaces.get(win),
            iconify=self.iconify,
            deiconify=self.deiconify,
            read_clients=lambda: self.clients,
        )

    def apply(self, win, rect, label="", quiet=False):
        self.applied.append((win, rect, label, quiet))

    def iconify(self, win, x11=None, quiet=True):
        """Вместо `tiler.iconify`: запомнить и считать окно свёрнутым (как WM)."""
        self.iconified.append(win)
        self.iconify_calls.append((win, x11, quiet))
        self.iconic.add(win)

    def deiconify(self, win, x11=None):
        """Вместо `tiler.deiconify`: запомнить и считать окно видимым (как WM)."""
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
        self.assertEqual(self.reads, [])  # метаданные даже не читались
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
            "firefox-esr", window_type=self.DIALOG
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

    def test_system_exit_from_workarea_does_not_kill_daemon(self):
        # Провайдер рабочей области бросает SystemExit при отказе X: получение
        # рабочей области не должно ронять демон.
        self.metas[0x1] = self.meta("firefox-esr")

        def boom():
            raise SystemExit(1)

        d = zd.Daemon(
            self.conf(ONE_RULE),
            apply=self.apply,
            read_meta=self.read_meta,
            workarea=boom,
        )
        d.on_map(0x1)  # не должно выбросить SystemExit
        self.assertEqual(self.applied, [])  # окно не уложено
        self.assertEqual(d.windows, {})
        self.assertEqual(d.seen, set())

    def test_keyboard_interrupt_from_workarea_is_not_swallowed(self):
        # `_forced_workarea` ловит (Exception, SystemExit), но не BaseException.
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
        # Монитор ушедшего окна не запомнен (трекер восстановлен вручную):
        # reflow пропускается, а не падает.
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

    # -- возврат расширенной зоны, когда новое окно встаёт в освобождённую

    def test_new_window_in_freed_zone_restores_expanded_neighbor(self):
        # 0x1 закрылось, right расширился на left; новое окно 0x3 в left
        # возвращает right к его рамке из конфига.
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
        # Окно без правила освобождённую зону не занимает — возвращать нечего.
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
        # Держателя уже нет в трекере: запись снимается, падения нет.
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
        d.on_destroy(0x2)  # держатель исчез — возвращать некого
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
        d.on_map(0x3)  # новое окно встало, но вернуть соседа не удалось
        self.assertEqual(d.windows, {0x2: "right", 0x3: "left"})
        self.assertEqual(calls[-1][0], 0x2)  # попытка возврата была
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})  # запись осталась

    def test_reconcile_restores_neighbor_when_new_window_fills_freed_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("firefox-esr")
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        d.reconcile([0x1, 0x2])
        d.reconcile([0x2])  # 0x1 закрылось -> right расширился на left
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x2})
        self.applied.clear()
        d.reconcile([0x2, 0x3])  # 0x3 открылось в left
        self.assertEqual(
            self.applied,
            [
                (0x3, (0, 0, 500, 1000), "left", True),
                (0x2, (500, 0, 500, 1000), "right", True),
            ],
        )
        self.assertEqual(d.filled, {})

    # -- несколько мониторов

    def test_window_on_second_monitor_uses_its_workarea(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = (1400, 300)
        d = self.multi_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (1000, 0, 400, 600), "left", True)])
        self.assertEqual(d.win_monitor, {0x1: WA1})

    def test_window_center_outside_monitors_picks_nearest(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.centers[0x1] = (1400, 700)  # ниже монитора 1, ближе к нему
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
        d.on_map(0x1)  # отказ X при получении мониторов не должен уронить демон
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
        # right на мониторе 0 расширяется окном 0x3; окно 0x2 с другого экрана не трогаем
        self.assertEqual(self.applied, [(0x3, (0, 0, 1000, 1000), "right", True)])
        self.assertEqual(d.filled, {filled_key(WA, "left"): 0x3})

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


# Конфиг со профилем стола: стол «2» в [workspaces] — это индекс 1 в EWMH.
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
# Профиль стола: та же зона `video` в другой геометрии (и другая `main`).
CINEMA_PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "main = 0, 0, 1, 0.25\n"
    "video = 0, 0.25, 1, 0.75\n"
)


class TestWorkspaces(DaemonTestCase):
    """Рабочие столы: сетка из профиля, занятость и reflow в пределах стола."""

    def desktop_daemon(self, profile=CINEMA_PROFILE):
        """Демон на конфиге `[workspaces]` с профилем стола во временном каталоге."""
        directory = self.conf_dir()
        if profile is not None:
            self.write(os.path.join(directory, "templates", "cinema.conf"), profile)
        self.current = 1
        return self.daemon(DESKTOP_CONF, directory=directory)

    # -- сетка на стол

    def test_on_map_uses_profile_zones_of_window_workspace(self):
        # Окно на столе «2» (EWMH 1) встаёт в зону профиля cinema: video — 0,0.25,1,0.75.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        d = self.desktop_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (0, 250, 1000, 750), "video", True)])
        self.assertEqual(d.win_workspace, {0x1: 1})

    def test_on_map_uses_default_zones_for_unmapped_workspace(self):
        # Стол 0 профиля не имеет — обычный набор `zones.conf`.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 0}
        d = self.desktop_daemon()
        d.on_map(0x1)
        self.assertEqual(self.applied, [(0x1, (500, 0, 500, 1000), "video", True)])

    def test_sticky_window_uses_default_zones(self):
        # «Липкое» окно (стол неизвестен) живёт в общем наборе, даже если
        # активный стол — профильный.
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
        # В профиле нет зоны правила — окно не укладываем, а не падаем.
        self.metas[0x1] = self.meta("firefox-esr")
        self.workspaces = {0x1: 1}
        d = self.desktop_daemon(profile="[zones]\nmain = 0, 0, 1, 1\n")
        d.on_map(0x1)
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {})

    # -- reflow только на видимом столе

    def test_destroy_on_invisible_workspace_moves_nothing(self):
        # Две поддельные таблицы окон: стол 1 виден, стол 2 — нет. Закрытие окна
        # невидимого стола чистит состояние, но не двигает никого.
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
        d.reconcile([0x1, 0x2, 0x4])  # 0x3 закрылось на невидимом столе 2
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right", 0x4: "right"})
        self.assertEqual(d.filled, {})
        d.reconcile([0x1, 0x2])  # 0x4 там же — тоже без движений
        self.assertEqual(self.applied, [])
        self.assertEqual(d.windows, {0x1: "left", 0x2: "right"})

    def test_destroy_on_invisible_workspace_does_not_expand_visible_neighbor(self):
        # На видимом столе левая зона пуста: без проверки «стол виден» reflow
        # расширил бы соседа видимого стола на закрытие окна чужого стола.
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
        d.on_destroy(0x3)  # окно стола 2; на столе 1 зона left пуста
        self.assertEqual(self.applied, [])
        self.assertEqual(d.filled, {})
        self.assertEqual(d.windows, {0x2: "right", 0x4: "right"})

    def test_occupied_zone_of_other_workspace_does_not_block_reflow(self):
        # На столе 2 в left стоит окно: для стола 1 эта зона свободна, и right
        # расширяется.
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
        # Окно стола 2 в той же зоне заведено раньше окна стола 1: расширять
        # надо соседа видимого стола (0x2), а не 0x9 со стола 2.
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
        # На обоих столах в `right` стоит окно: расширяется сосед видимого стола.
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
        # Активный стол не прочитался, а окна принадлежат столу 2: двигать
        # вслепую нельзя.
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
        # Покрытие появилось на столе 1; окно стола 2 в ту же зону его не снимает.
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
        d.on_map(0x3)  # окно стола 2 в зону left
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
        # Пользователь перешёл на стол 2: PropertyNotify о _NET_CURRENT_DESKTOP.
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
        self.assertIn("активный стол: #2", lines)
        self.assertEqual(d.workspace, 2)

    def test_workspace_change_without_readable_desktop_keeps_previous(self):
        self.current = 3
        d = self.daemon(TWO_ZONES, reflow="neighbors")
        self.current = None  # xprop не ответил
        self.assertEqual(d.on_workspace_change(), 3)
        self.assertEqual(d.workspace, 3)

    # -- status для gridsnap-ctl

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
        # прежние поля на месте
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


class TestDaemonIgnore(DaemonTestCase):
    """`[ignore]` в демоне: автоукладка по правилам и явный apply по сокету."""

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
        self.assertIn("исключением", str(caught.exception))
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
    """Режим «только эта зона»: вход, toggle, выход и новички.

    `window_iconic` подменён состоянием `self.iconic`: без X настоящая проверка
    всегда говорит «видимо». `self.iconify`/`self.deiconify` это состояние
    меняют, как настоящий WM, поэтому вручную свёрнутое окно остаётся таковым.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            zd.tiler, "window_iconic", lambda win: win in self.iconic
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Паузы проверки состояния в развороте — нулевые: тесты не должны ждать.
        delay = mock.patch.object(zd.tiler, "FOCUS_VERIFY_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def setup_clients(self, windows, workspace=0):
        """Клиенты демона: окна на столе `workspace` и первом мониторе."""
        for win in windows:
            self.workspaces[win] = workspace
            self.centers[win] = (100, 100)
        self.clients = list(windows)

    def focus_daemon(self, text=TWO_ZONES):
        """Демон с двумя мониторами: он же проверяет область сворачивания."""
        return self.multi_daemon(text=text)

    # -- вход

    def test_entry_minimizes_only_same_workspace_and_monitor(self):
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3, 0x4, 0x5])
        self.workspaces[0x5] = 1  # окно другого стола
        self.centers[0x4] = (1500, 300)  # окно другого монитора
        self.metas[0x1] = self.meta("firefox-esr")  # правило: left
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
        # диалог и окна чужого стола/монитора остаются видимыми
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
        # Форма `[focus]` без зоны: цель не двигается и в трекеры не попадает.
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3])
        self.workspaces[0x3] = 1  # чужой стол — не сворачиваем
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
        self.assertIn("нет зоны 'nope'", str(caught.exception))
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
        self.assertIn("исключением", str(caught.exception))
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
        self.assertIn("активный стол", str(caught.exception))
        self.assertEqual(self.applied, [])
        self.assertEqual(self.iconified, [])
        self.assertIsNone(d.focus)

    # -- toggle и выход

    def test_toggle_restores_only_mode_windows_and_activates_target_last(self):
        self.current = 0
        self.setup_clients([0x1, 0x2, 0x3])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        self.metas[0x3] = self.meta("gimp")
        self.iconic = {0x3}  # свёрнуто вручную до входа в режим
        d = self.focus_daemon()
        reply = d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(reply["minimized"], ["0x2"])
        out = d.ipc_focus({"zone": "left", "window": 0x1})
        self.assertEqual(out, {"off": True, "restored": ["0x2"]})
        # свёрнутое вручную не разворачиваем, цель активируется последней
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

    def test_target_disappearing_leaves_mode_and_restores_windows(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.seen = {0x1, 0x2}
        d.reconcile([0x2])  # цель 0x1 исчезла из _NET_CLIENT_LIST
        self.assertIsNone(d.focus)
        self.assertEqual(self.deiconified, [0x2, 0x1])
        self.assertEqual(d.windows, {})

    def test_target_disappearing_without_zone_leaves_mode(self):
        # Форма `focus` без зоны: цель в `windows` не лежит, поэтому её исчезновение
        # ловит только отдельная проверка цели в реконсиляции.
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("gimp")  # правила нет — цель нигде не учтена
        self.metas[0x2] = self.meta("gimp")
        d = self.focus_daemon()
        d.ipc_focus({"window": 0x1})
        self.assertIsNone(d.focus["zone"])
        self.assertEqual(self.iconified, [0x2])
        d.seen = {0x1, 0x2}
        d.reconcile([0x2])  # цель 0x1 исчезла из _NET_CLIENT_LIST
        self.assertIsNone(d.focus)
        self.assertEqual(self.deiconified, [0x2, 0x1])

    def test_unrestorable_windows_go_to_focus_json(self):
        # Активацию окна на неактивном столе muffin игнорирует. Демон не теряет
        # такое окно, а отдаёт в `focus.json`:
        # без записи оно осталось бы свёрнутым навсегда, а так его доведёт `focus --off`.
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("chromium")
        d = self.focus_daemon()
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        d.focus_path = path
        d.deiconify = lambda win, x11=None: None  # «разворот не действует»
        d.ipc_focus({"zone": "left", "window": 0x1})
        out = d.ipc_focus({"off": True})
        self.assertEqual(out, {"off": True, "restored": []})
        self.assertIsNone(d.focus)
        state = zd.tiler.load_focus_state(path)
        self.assertEqual([r["window"] for r in state["windows"]], ["0x2"])
        self.assertEqual(state["workspace"], 0)
        self.assertEqual(state["target"]["window"], "0x1")

    def test_minimized_window_disappearing_keeps_mode(self):
        self.current = 0
        self.setup_clients([0x1, 0x2])
        self.metas[0x1] = self.meta("firefox-esr")
        self.metas[0x2] = self.meta("gimp")  # правила нет: в windows его нет
        d = self.focus_daemon()
        d.ipc_focus({"zone": "left", "window": 0x1})
        d.seen = {0x1, 0x2}
        d.reconcile([0x1])  # свёрнутое 0x2 исчезло, цель жива
        self.assertIsNotNone(d.focus)
        self.assertEqual(d.focus["minimized"], set())
        self.assertEqual(self.deiconified, [])
        self.assertEqual(d.ipc_status()["focus"]["minimized"], 0)

    def test_workspace_change_keeps_mode(self):
        # Уход со стола режим НЕ снимает: окна того стола и так не видны, а снять
        # его нельзя — `_NET_ACTIVE_WINDOW` muffin игнорирует для окна не на
        # активном столе, и окна остались бы свёрнутыми без хозяина.
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

    # -- новое окно при активном режиме

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
        # сначала укладка по правилу — после выхода окно окажется в своей зоне
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
        self.metas[0x9] = self.meta("terminal")  # правила нет
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
        # обычное поведение: уложено по правилу и осталось видимым
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
        # прежние поля не изменились
        self.assertEqual(status["workspace"], 0)
        self.assertEqual(
            status["windows"],
            [{"window": "0x1", "zone": "left", "monitor": list(WA), "workspace": 0}],
        )
        d.ipc_focus({"off": True})
        self.assertIsNone(d.ipc_status()["focus"])


class TestPlacedState(unittest.TestCase):
    """Файл памяти об укладках: чтение терпимо к мусору, запись — атомарная."""

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
        # без известной сессии файл читается как раньше
        self.assertEqual(zd.load_placed_state(self.path), {1: record})


class TestDaemonPlacedMemory(DaemonTestCase):
    """Память демона: окно, вынесенное из зоны, само туда не возвращается."""

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
        """Демон с подменённой геометрией и своим файлом состояния."""
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
        self.frames[0x1] = (600, 600, 300, 300)  # пользователь вынес окно
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})
        self.assertEqual(d.filled, {})  # зона свободна для reflow
        self.assertIn(0x1, d.detached)
        self.assertEqual(len(self.applied), 1)  # заново не укладывал
        d.on_map(0x1)
        self.assertEqual(len(self.applied), 1)

    def test_restart_adopts_window_still_in_zone(self):
        self.metas[0x1] = self.meta("firefox-esr")
        self.frames[0x1] = (0, 0, 500, 1000)
        self.placed_daemon().on_map(0x1)
        self.applied.clear()
        second = self.placed_daemon()
        second.reconcile([0x1])
        self.assertEqual(self.applied, [])  # усыновил без apply
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
        self.metas[0x1] = self.meta("chromium")  # XID переиспользован
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
        # Файл состояния: демон расширил окно 0x1 (зона left) на right и записал
        # объединённую рамку. Перезапуск должен вернуть окно в left: книга
        # покрытий `filled` в файле не живёт, и иначе новое окно в right ляжет
        # поверх расширенного.
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
        # WM применил `_NET_MOVERESIZE_WINDOW` не сразу: первое чтение — старая
        # рамка, второе — уже целевая. В `placed` должна попасть целевая, иначе
        # окно тут же сочли бы «вышедшим из зоны».
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
        self.assertEqual(d.windows, {0x1: "left"})  # невидимый стол не трогаем
        self.current = 2
        d.on_workspace_change()
        d.reconcile([0x1])
        self.assertEqual(d.windows, {})  # на своём столе переезд виден
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
    """Дефолтный read_window_meta: один retry, если класс/заголовок пусты."""

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
    """handle_property_event: реконсиляция только на PropertyNotify о _NET_CLIENT_LIST.

    Буфер событий собирается сырыми байтами по ожидаемым смещениям XPropertyEvent
    из Xlib.h (LP64), а не копией структуры из проверяемого модуля: тест ловит
    разъезд offset'ов относительно X11 ABI.
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
        self.assertTrue(handled)  # событие про наш атом
        self.assertEqual(dae.calls, [])  # но реконсиляции нет

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
        self.assertEqual(dae.calls, [])  # список клиентов не читали

    def test_current_desktop_atom_ignored_without_atom_argument(self):
        # Старый вызов из четырёх аргументов не знает про стол — событие чужое.
        dae = self.Stub()
        handled = zd.handle_property_event(
            dae, property_buffer(0xABD), 0xABC, lambda: [0x9]
        )
        self.assertFalse(handled)
        self.assertEqual(dae.workspace_calls, 0)
        self.assertEqual(dae.calls, [])


class TestReadClientList(unittest.TestCase):
    """read_client_list: разбор `xprop -root _NET_CLIENT_LIST`."""

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
        # xprop при отсутствии свойства печатает сообщение и выходит с кодом 0 —
        # это не «клиентов нет», а «список недоступен».
        zd.tiler.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST:  not found.\n"
        self.assertIsNone(zd.read_client_list())

    def test_none_when_no_such_atom(self):
        zd.tiler.subprocess.check_output = lambda *a, **k: (
            "no such atom on any window.\n"
        )
        self.assertIsNone(zd.read_client_list())

    def test_none_without_window_id_marker(self):
        # Вывод без маркера `window id #` не разбирается как список окон.
        zd.tiler.subprocess.check_output = lambda *a, **k: "_NET_CLIENT_LIST(WINDOW): \n"
        self.assertIsNone(zd.read_client_list())

    def test_empty_list_when_property_present_but_no_windows(self):
        # Валидное состояние «клиентов нет» — это [], а не None.
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
    """Демон-заглушка для serve(): только фиксирует реконсиляции и смену стола."""

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
    """serve(): PropertyChangeMask на root, стартовая реконсиляция и PropertyNotify."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

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
        # Настоящий Daemon (а не заглушка со своим reconcile): пропущенная
        # реконсиляция не должна трогать ни трекер, ни `seen`.
        dae = zd.Daemon(self.conf(ONE_RULE), workarea=lambda: WA)
        dae.windows = {0x1: "left"}
        dae.seen = {0x1}
        fake = FakeX11(
            events=[property_buffer(FakeX11.CLIENT_LIST_ATOM)], fd=self.pipe_fd()
        )
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: None, once_timeout=0.05
        )
        # список не прочитался — ни реконсиляции, ни изменений трекера
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
        fake = FakeX11(events=[property_buffer(FakeX11.OTHER_ATOM)], fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x22]])  # только стартовая
        self.assertEqual(dae.workspace_calls, 0)  # стол тоже не трогали

    def test_current_desktop_property_notify_updates_workspace_only(self):
        # Смена стола — это не изменение списка клиентов: ни реконсиляции, ни
        # перемещений, только обновление `self.workspace`.
        dae = StubDaemon()
        fake = FakeX11(events=[property_buffer(FakeX11.DESKTOP_ATOM)], fd=self.pipe_fd())
        zd.serve(
            dae, x11=fake, once=True, read_clients=lambda: [0x22], once_timeout=0.05
        )
        self.assertEqual(dae.reconciled, [[0x22]])  # только стартовая
        self.assertEqual(dae.workspace_calls, 1)

    def test_once_without_events_times_out(self):
        dae = StubDaemon()
        fake = FakeX11(fd=self.pipe_fd())
        zd.serve(dae, x11=fake, once=True, read_clients=lambda: [], once_timeout=0.05)
        self.assertEqual(dae.reconciled, [[]])
        self.assertTrue(fake.closed)

    def test_serve_restores_focus_windows_on_exit(self):
        # Режим «только эта зона» не должен пережить демон: в `finally` serve
        # разворачивает его окна, цель — последней.
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
        # Ранний выход без X-соединения: окна не развернуть, состояние снимаем.
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

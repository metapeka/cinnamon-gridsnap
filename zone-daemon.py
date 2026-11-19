#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-daemon — долгоживущий демон GridSnap: сам вкладывает новые окна в зоны
по правилам `[rules]` и (опционально) перестраивает раскладку при закрытии окна.

Работает в Cinnamon / Muffin (X11) через тот же ctypes/libX11 и EWMH, что и
`zone-tiler.py`; внешние утилиты (wmctrl, xdotool) не нужны. Подписка —
`SubstructureNotifyMask` на root, поэтому клиентские окна muffin приходят как
прямые дети root: `MapNotify` даёт сразу клиентское окно с `WM_CLASS`.

Конфиг — тот же `zones.conf` (по умолчанию путь как у раскладчика,
`zone-tiler.default_conf()`):

  [rules]
  Chrome = main                       # glob по WM_CLASS (без учёта регистра)
  re:^(Navigator|firefox) = main      # регулярка по строке "class|title"

  [daemon]
  reflow = off                        # off | neighbors (по умолчанию off)

Запуск:
  zone-daemon.py                 # слушать события до Ctrl-C
  zone-daemon.py --verbose       # писать в stdout, что делает демон
  zone-daemon.py --once          # обработать очередную пачку событий и выйти
  zone-daemon.py --conf PATH
"""

import argparse
import collections
import ctypes
import importlib.util
import os
import re
import select
import subprocess
import sys
import time

VERSION = "0.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))

REFLOW_OFF = "off"
REFLOW_NEIGHBORS = "neighbors"

# Задержка перед единственным повтором чтения метаданных: `_NET_WM_NAME` иногда
# появляется уже после MapNotify.
META_RETRY_DELAY = 0.2

# Типы окон, которые демон не трогает: это не самостоятельные «окна-документы»,
# а всплывающие/служебные поверхности.
IGNORED_WINDOW_TYPES = frozenset(
    {
        "_NET_WM_WINDOW_TYPE_DIALOG",
        "_NET_WM_WINDOW_TYPE_SPLASH",
        "_NET_WM_WINDOW_TYPE_TOOLTIP",
        "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
        "_NET_WM_WINDOW_TYPE_UTILITY",
    }
)
# Короткие имена тех же типов — на случай, если xprop/тест отдал их без префикса.
IGNORED_TYPE_TAILS = frozenset(name.rsplit("_", 1)[-1] for name in IGNORED_WINDOW_TYPES)

# Коды событий X11.
DESTROY_NOTIFY = 17
UNMAP_NOTIFY = 18
MAP_NOTIFY = 19

# SubstructureNotifyMask: MapNotify/UnmapNotify/DestroyNotify о детях root.
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19

# XEvent — объединение размером 192 байта на LP64.
EVENT_BUFFER = ctypes.c_char * 192


def _load_sibling(name, filename):
    """Загрузить соседний hyphen-модуль (zone-tiler.py) без зависимости от sys.path."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_zone_tiler", "zone-tiler.py")
zone_reflow = _load_sibling("gridsnap_zone_reflow", "zone_reflow.py")


# ------------------------------------------------------------------ метаданные


# Метаданные окна, которые нужны демону. `window_type` — набор имён
# `_NET_WM_WINDOW_TYPE_*` (может быть пустым). Namedtuple: и кортеж, и атрибуты.
Meta = collections.namedtuple("Meta", "wm_class wm_title window_type")


def window_types(win):
    """Набор `_NET_WM_WINDOW_TYPE` окна через xprop (пустой при ошибке)."""
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_WM_WINDOW_TYPE"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return frozenset()
    return frozenset(re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", out))


def read_window_meta(win):
    """Метаданные окна по умолчанию: WM_CLASS/_NET_WM_NAME + тип окна.

    Если класс или заголовок ещё не появились, один раз перечитывает их после
    короткой паузы (заголовок порой приходит уже после MapNotify).
    """
    wm_class, wm_title = tiler.window_class_title(win)
    if not wm_class or not wm_title:
        time.sleep(META_RETRY_DELAY)
        wm_class2, wm_title2 = tiler.window_class_title(win)
        wm_class = wm_class or wm_class2
        wm_title = wm_title or wm_title2
    return Meta(wm_class, wm_title, window_types(win))


def is_ignored_type(window_type):
    """True, если среди типов окна есть служебный (диалог, сплэш и т. п.).

    Принимает набор имён `_NET_WM_WINDOW_TYPE_*`, строку с ними или короткое
    имя (`DIALOG`, ...). Пустое значение — не служебное окно.
    """
    names = _type_names(window_type)
    if names & IGNORED_WINDOW_TYPES:
        return True
    return bool({name.rsplit("_", 1)[-1] for name in names} & IGNORED_TYPE_TAILS)


def _type_names(window_type):
    if not window_type:
        return frozenset()
    if isinstance(window_type, str):
        found = re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", window_type)
        return frozenset(found) if found else frozenset({window_type.strip().upper()})
    return frozenset(window_type)


# ---------------------------------------------------------------------- конфиг


def read_reflow(conf):
    """Режим reflow из `[daemon]`. Нет секции/значения или мусор -> `off`."""
    try:
        value = (conf.get("daemon", "reflow", fallback="off") or "").strip().lower()
    except Exception:
        return REFLOW_OFF
    return value if value in (REFLOW_OFF, REFLOW_NEIGHBORS) else REFLOW_OFF


# -------------------------------------------------------------------- геометрия


def fractions_to_pixels(fx, fy, fw, fh, wa):
    """Доли рабочей области -> пиксельная рамка (x, y, w, h).

    Края считаются от одних и тех же линий (`round(ww*(fx+fw))`), как в
    `Grid.rect`, — соседние зоны стыкуются без щели. Ширина/высота >= 1 px.
    """
    wx, wy, ww, wh = wa
    x1, x2 = wx + round(ww * fx), wx + round(ww * (fx + fw))
    y1, y2 = wy + round(wh * fy), wy + round(wh * (fy + fh))
    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


def default_workarea():
    """Рабочая область монитора 0 (Gdk, резерв — EWMH)."""
    return tiler.monitors()[0]


# ----------------------------------------------------------------------- демон


class Daemon:
    """Ядро демона без X: вся реакция на события — в `on_map`/`on_destroy`/`on_unmap`.

    Зависимости инъектируются, поэтому сценарии гоняются юнит-тестами без
    дисплея:

      conf_path  — путь к zones.conf (по умолчанию — как у раскладчика);
      apply      — callable(window, rect, label=..., quiet=...) (по умолчанию
                   `zone-tiler.apply_zone`);
      read_meta  — callable(window) -> Meta (по умолчанию `read_window_meta`);
      workarea   — callable() -> (x, y, w, h) либо сам кортеж (по умолчанию
                   `default_workarea`, вызывается лениво);
      verbose    — печатать ли, что делает демон.

    `windows` — трекер `window id -> имя зоны`: им же отсекаются повторные
    MapNotify (развернуть/свернуть не переставляют окно).

    Закрытие окна (`on_destroy`) снимает его с трекера и, если включён
    `reflow=neighbors`, перестраивает раскладку. Скрытие окна (`on_unmap`:
    сворачивание, смена воркспейса) — no-op: окно ещё живёт и занимает зону,
    двигать соседей нельзя.
    """

    def __init__(
        self,
        conf_path=None,
        apply=None,
        read_meta=None,
        workarea=None,
        verbose=False,
    ):
        self.conf_path = conf_path or tiler.default_conf()
        self.grid = tiler.Grid(self.conf_path)
        self.apply = apply or tiler.apply_zone
        self.read_meta = read_meta or read_window_meta
        self.workarea = workarea if workarea is not None else default_workarea
        self.verbose = verbose
        self.reflow = read_reflow(self.grid.conf)
        self.windows = {}
        self._wa_cache = None

    # -- вспомогательное

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    def _workarea(self):
        if self._wa_cache is None:
            wa = self.workarea() if callable(self.workarea) else self.workarea
            self._wa_cache = tuple(wa)
        return self._wa_cache

    def _apply(self, win, rect, zone):
        """Вложить окно, не уронив долгоживущий демон из-за одной ошибки."""
        try:
            self.apply(win, rect, label=zone, quiet=True)
            return True
        except Exception as exc:  # окно могло исчезнуть между MapNotify и apply
            self.log(f"не удалось вложить окно 0x{win:x} в зону '{zone}': {exc}")
            return False

    def _window_for_zone(self, zone):
        for win, occupied in self.windows.items():
            if occupied == zone:
                return win
        return None

    # -- события

    def on_map(self, win, override_redirect=False):
        """MapNotify: вложить окно в зону по правилу, если оно подходит."""
        if win in self.windows:
            return  # повторный MapNotify (развернуть/свернуть) — не переставлять
        if override_redirect:
            return  # меню/подсказки WM — не самостоятельные окна
        meta = self.read_meta(win)
        wm_class = getattr(meta, "wm_class", "") or ""
        if not wm_class:
            return  # без WM_CLASS правило не проверить
        if is_ignored_type(getattr(meta, "window_type", ())):
            return
        zone = tiler.match_rules(
            self.grid.rules, wm_class, getattr(meta, "wm_title", "") or ""
        )
        if zone is None:
            return
        rect = self.grid.rect(zone, self._workarea())
        if not self._apply(win, rect, zone):
            return  # не запоминаем: следующий MapNotify попробует снова
        self.windows[win] = zone
        self.log(f"окно 0x{win:x} -> зона '{zone}'")

    def on_destroy(self, win):
        """DestroyNotify: окно закрылось — снять с трекера и, если нужно, reflow."""
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        self.log(f"окно 0x{win:x} ушло из зоны '{zone}'")
        if self.reflow != REFLOW_NEIGHBORS:
            return
        occupied = set(self.windows.values())
        zones = {name: self.grid.fractions(name) for name in self.grid.zones}
        changes = zone_reflow.reflow(zones, zone, occupied)
        if not changes:
            return
        wa = self._workarea()
        for name, frac in changes.items():
            target = self._window_for_zone(name)
            if target is None:
                continue
            self._apply(target, fractions_to_pixels(*frac, wa), name)
            self.log(f"reflow: окно 0x{target:x} -> зона '{name}'")

    def on_unmap(self, win):
        """UnmapNotify: окно скрыто (свёрнуто/сменился воркспейс) — no-op.

        Окно ещё существует и занимает свою зону, поэтому трекер не трогаем:
        при возврате `MapNotify` не должен переставлять окно заново, а соседи
        не должны расширяться на занятую зону. Снятие с трекера и reflow —
        только в `on_destroy`.
        """
        self.log(f"окно 0x{win:x} скрыто — оставляем в трекере")


# ------------------------------------------------------------------ X11-контур


class XMapEvent(ctypes.Structure):
    # Порядок полей — как в Xlib.h: у XMapEvent `event` идёт перед `window`
    # (в отличие от XAnyEvent), иначе `ev.window` прочитал бы root.
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("event", ctypes.c_ulong),
        ("window", ctypes.c_ulong),
        ("override_redirect", ctypes.c_int),
    ]


class XUnmapEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("event", ctypes.c_ulong),
        ("window", ctypes.c_ulong),
        ("from_configure", ctypes.c_int),
    ]


class XDestroyWindowEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("event", ctypes.c_ulong),
        ("window", ctypes.c_ulong),
    ]


def handle_event(dae, buf, etype):
    """Разобрать XEvent из буфера и позвать нужный обработчик демона.

    DestroyNotify — реальное закрытие окна (`on_destroy`, снимает с трекера и
    делает reflow). UnmapNotify — сворачивание/скрытие/смена воркспейса
    (`on_unmap`, no-op).
    """
    if etype == MAP_NOTIFY:
        ev = ctypes.cast(buf, ctypes.POINTER(XMapEvent)).contents
        dae.on_map(ev.window, override_redirect=bool(ev.override_redirect))
    elif etype == DESTROY_NOTIFY:
        ev = ctypes.cast(buf, ctypes.POINTER(XDestroyWindowEvent)).contents
        dae.on_destroy(ev.window)
    elif etype == UNMAP_NOTIFY:
        ev = ctypes.cast(buf, ctypes.POINTER(XUnmapEvent)).contents
        dae.on_unmap(ev.window)


def _bind_event_functions(x):
    """Проставить сигнатуры libX11-функциям, которых нет в `zone-tiler.X11`."""
    x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
    x.XSelectInput.restype = ctypes.c_int
    x.XConnectionNumber.argtypes = [ctypes.c_void_p]
    x.XConnectionNumber.restype = ctypes.c_int
    x.XPending.argtypes = [ctypes.c_void_p]
    x.XPending.restype = ctypes.c_int
    x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x.XNextEvent.restype = ctypes.c_int
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.restype = ctypes.c_int


def serve(dae, x11=None, once=False):
    """Цикл событий X11: select на fd дисплея -> XPending/XNextEvent -> handle_event.

    `DestroyNotify` уходит в `on_destroy` (окно закрылось), `UnmapNotify` — в
    `on_unmap` (свернулось/скрылось, но живёт).

    `x11` можно подменить (тогда реальный дисплей не нужен). `once=True` —
    обработать очередную пачку событий и выйти (для smoke-проверок).
    """
    if x11 is None:
        x11 = tiler.X11()
    _bind_event_functions(x11.x)
    x11.x.XSelectInput(x11.dpy, x11.root, SUBSTRUCTURE_NOTIFY_MASK)
    x11.x.XSync(x11.dpy, 0)
    fd = x11.x.XConnectionNumber(x11.dpy)
    dae.log(f"демон слушает X11 (root 0x{x11.root:x}, reflow={dae.reflow})")
    try:
        while True:
            if not x11.x.XPending(x11.dpy):
                select.select([fd], [], [])
            while x11.x.XPending(x11.dpy):
                buf = EVENT_BUFFER()
                x11.x.XNextEvent(x11.dpy, ctypes.byref(buf))
                etype = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
                handle_event(dae, buf, etype)
            if once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        x11.x.XCloseDisplay(x11.dpy)


# ------------------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Автоукладка окон GridSnap по правилам [rules] и reflow (Cinnamon/X11)"
    )
    ap.add_argument(
        "--conf",
        default=None,
        help="путь к zones.conf (по умолчанию XDG/рядом с кодом)",
    )
    ap.add_argument("--verbose", action="store_true", help="печатать, что делает демон")
    ap.add_argument("--once", action="store_true", help="обработать пачку событий и выйти")
    ap.add_argument("--version", action="version", version=f"zone-daemon {VERSION}")
    args = ap.parse_args(argv)

    dae = Daemon(args.conf, verbose=args.verbose)
    serve(dae, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-daemon — долгоживущий демон GridSnap: сам вкладывает новые окна в зоны
по правилам `[rules]` и (опционально) перестраивает раскладку при закрытии окна.

Работает в Cinnamon / Muffin (X11) через тот же ctypes/libX11 и EWMH, что и
`zone-tiler.py`; внешние утилиты (wmctrl, xdotool) не нужны.

Источник истины о клиентах — EWMH-список `_NET_CLIENT_LIST` на root: muffin
РЕПАРЕНТИТ клиентские окна в frame-окна, поэтому `SubstructureNotify` на root
даёт `MapNotify` с frame-окном без `WM_CLASS`. Вместо этого демон подписан на
`PropertyChangeMask` и на каждый `PropertyNotify` про `_NET_CLIENT_LIST` читает
список через `xprop -root _NET_CLIENT_LIST` и реконсилирует состояние:
появившиеся окна укладывает по правилам, исчезнувшие снимает (и делает reflow).
Сворачивание окна из `_NET_CLIENT_LIST` не убирает — поэтому reflow не
срабатывает, и это желаемое поведение.

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
  zone-daemon.py --once          # обработать одно изменение _NET_CLIENT_LIST и выйти
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
# появляется уже после того, как окно попало в `_NET_CLIENT_LIST`.
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
PROPERTY_NOTIFY = 28

# PropertyChangeMask: PropertyNotify о свойствах root (в т.ч. _NET_CLIENT_LIST).
PROPERTY_CHANGE_MASK = 1 << 22

# Свойство root со списком управляемых клиентских окон (EWMH).
CLIENT_LIST_ATOM = "_NET_CLIENT_LIST"

# Таймаут ожидания единственного PropertyNotify в режиме `--once`.
ONCE_TIMEOUT = 2.0

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
    короткой паузы (заголовок порой приходит уже после появления окна в списке).
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


# ----------------------------------------------------- список клиентов (EWMH)


def read_client_list():
    """Клиентские окна из `_NET_CLIENT_LIST` (EWMH) через `xprop -root`.

    Возвращает список id в порядке из свойства (снизу вверх). Любая ошибка
    (нет xprop/дисплея/свойства) — пустой список. Функцию можно подменить в
    тестах.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", CLIENT_LIST_ATOM],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    # `_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0x600005`
    _, _, value = out.partition("#")
    return [int(h, 16) for h in re.findall(r"0x[0-9a-fA-F]+", value)]


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
    появления окна (свернуть/развернуть не переставляют окно).

    Точка входа из X11-контура — `reconcile(client_ids)`: список id текущих
    клиентов из `_NET_CLIENT_LIST`. Новые id (которых нет в `seen`) проходят
    через `on_map`, исчезнувшие из трекера — через `on_destroy`. `seen` хранит
    все id последнего списка, включая не подошедшие под правила, — тогда их
    метаданные не перечитываются на каждой реконсиляции.

    Закрытие окна (`on_destroy`) снимает его с трекера и, если включён
    `reflow=neighbors`, перестраивает раскладку. Скрытие окна (`on_unmap`:
    сворачивание, смена воркспейса) в `_NET_CLIENT_LIST` ничего не меняет,
    поэтому реконсиляция его не видит: окно остаётся в трекере, соседей не
    двигаем.
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
        # Все id из последнего `_NET_CLIENT_LIST` (и уложенные, и без правила) —
        # чтобы не читать метаданные повторно на каждой реконсиляции.
        self.seen = set()
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
        except Exception as exc:  # окно могло исчезнуть между появлением в списке и apply
            self.log(f"не удалось вложить окно 0x{win:x} в зону '{zone}': {exc}")
            return False

    def _window_for_zone(self, zone):
        for win, occupied in self.windows.items():
            if occupied == zone:
                return win
        return None

    # -- события

    def reconcile(self, client_ids):
        """Свести состояние к списку клиентов `_NET_CLIENT_LIST`.

        Новые окна укладываются по правилам (`on_map`), исчезнувшие снимаются
        с трекера (`on_destroy`, с reflow). Новые обрабатываются раньше
        исчезнувших: если новое окно заняло освободившуюся зону, reflow уже
        увидит её занятой и соседей не сдвинет.
        """
        current = set(client_ids)
        new = [win for win in dict.fromkeys(client_ids) if win not in self.seen]
        for win in new:
            self.log(f"новое окно 0x{win:x} в _NET_CLIENT_LIST")
            self.on_map(win)
        for win in list(self.windows):
            if win not in current:
                self.on_destroy(win)
        self.seen = current

    def on_map(self, win, override_redirect=False):
        """Новое клиентское окно: вложить в зону по правилу, если оно подходит."""
        if win in self.windows:
            return  # окно уже уложено (повторное появление в списке)
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
            return  # не запоминаем: окно не уложено, зону занятой не считаем
        self.windows[win] = zone
        self.log(f"окно 0x{win:x} -> зона '{zone}'")

    def on_destroy(self, win):
        """Окно исчезло из `_NET_CLIENT_LIST` — снять с трекера и, если нужно, reflow."""
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
        """Окно скрыто (свёрнуто/сменился воркспейс) — no-op.

        В `_NET_CLIENT_LIST` такое окно остаётся, поэтому реконсиляция его не
        снимает с трекера: при разворачивании окно не переставляется заново, а
        соседи не расширяются на занятую зону. Снятие с трекера и reflow —
        только когда окно исчезает из `_NET_CLIENT_LIST` (`on_destroy`).
        """
        self.log(f"окно 0x{win:x} скрыто — оставляем в трекере")


# ------------------------------------------------------------------ X11-контур


class XPropertyEvent(ctypes.Structure):
    # Порядок и типы полей — как в Xlib.h (XPropertyEvent).
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("atom", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("state", ctypes.c_int),
    ]


def handle_property_event(dae, buf, client_list_atom, read_clients):
    """PropertyNotify: если изменился `_NET_CLIENT_LIST` — реконсиляция.

    Возвращает True, если событие относилось к списку клиентов.
    """
    ev = ctypes.cast(buf, ctypes.POINTER(XPropertyEvent)).contents
    if ev.atom != client_list_atom:
        return False
    dae.reconcile(read_clients())
    return True


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


def serve(dae, x11=None, once=False, read_clients=None, once_timeout=ONCE_TIMEOUT):
    """Цикл событий X11: PropertyNotify о `_NET_CLIENT_LIST` -> реконсиляция.

    Подписка — `PropertyChangeMask` на root. На старте один раз читается текущий
    список клиентов и укладываются уже открытые окна; дальше то же происходит на
    каждый PropertyNotify про `_NET_CLIENT_LIST`.

    `x11` и `read_clients` можно подменить (тогда реальный дисплей не нужен).
    `once=True` — обработать один PropertyNotify и выйти; если событий нет —
    подождать `once_timeout` секунд и выйти (для smoke-проверок).
    """
    if x11 is None:
        x11 = tiler.X11()
    if read_clients is None:
        read_clients = read_client_list
    _bind_event_functions(x11.x)
    x11.x.XSelectInput(x11.dpy, x11.root, PROPERTY_CHANGE_MASK)
    x11.x.XSync(x11.dpy, 0)
    client_list_atom = x11.atom(CLIENT_LIST_ATOM)
    fd = x11.x.XConnectionNumber(x11.dpy)
    dae.log(f"демон слушает X11 (root 0x{x11.root:x}, reflow={dae.reflow})")
    try:
        # Раскладываем окна, уже открытые к запуску (вход в готовую сессию).
        dae.reconcile(read_clients())
        while True:
            if not x11.x.XPending(x11.dpy):
                ready, _, _ = select.select(
                    [fd], [], [], once_timeout if once else None
                )
                if not ready:
                    break  # once: событий так и не пришло
            handled = False
            while x11.x.XPending(x11.dpy):
                buf = EVENT_BUFFER()
                x11.x.XNextEvent(x11.dpy, ctypes.byref(buf))
                etype = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
                if etype == PROPERTY_NOTIFY:
                    handled = (
                        handle_property_event(
                            dae, buf, client_list_atom, read_clients
                        )
                        or handled
                    )
            if once and handled:
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
    ap.add_argument(
        "--once",
        action="store_true",
        help="обработать одно изменение _NET_CLIENT_LIST и выйти",
    )
    ap.add_argument("--version", action="version", version=f"zone-daemon {VERSION}")
    args = ap.parse_args(argv)

    dae = Daemon(args.conf, verbose=args.verbose)
    serve(dae, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())

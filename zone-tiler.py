#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-tiler — раскладывает активное окно по зонам собственной (нарисованной) сетки.

Работает в Cinnamon / Muffin (X11) через EWMH-сообщения, внешние утилиты
(wmctrl, xdotool) не нужны. Нужен только python3 + libX11 (уже есть в системе).

Сетка задаётся в zones.conf в долях рабочей области (без панелей):

  [grid]
  cols = 0.5, 0.75, 1.0      # вертикальные линии: 3 колонки (0..1, 1..2, 2..3)
  rows = 0.5, 1.0            # горизонтальные линии: 2 строки (0..1, 1..2)

  [zones]
  left      = 0:1, 0:2       # колонки 0..1, строки 0..2
  top-right = 1:3, 0:1

Команды:
  zone-tiler.py list                      # показать зоны и их пиксельные рамки
  zone-tiler.py apply left                # вложить активное окно в зону "left"
  zone-tiler.py apply left --dry-run      # только посчитать рамку
  zone-tiler.py apply left --monitor 1    # зона на другом мониторе
  zone-tiler.py apply left --window 0x3400007
  zone-tiler.py arrange                   # разложить окна по зонам (по правилам и остатку)
  zone-tiler.py arrange --by order        # разложить окна по зонам по порядку списка
  zone-tiler.py stack main                # собрать окна в зону main стопкой (каскадом)
  zone-tiler.py undo                      # вернуть окна из последней операции
  zone-tiler.py undo --all                # откатить всю историю раскладок

Профили раскладок (свои шаблоны: зоны + правила + клавиши) хранятся в
`~/.config/gridsnap/templates/*.conf`, активация — `profile use <имя>`:

  zone-tiler.py profile list              # сохранённые профили
  zone-tiler.py profile save work         # текущий zones.conf — в профиль «work»
  zone-tiler.py profile use work          # активировать профиль (и обновить сочетания)
  zone-tiler.py profile use work --no-hotkeys
  zone-tiler.py profile show work
  zone-tiler.py profile rm work

История раскладок — JSON в XDG-состоянии (`~/.local/state/gridsnap/history.json`),
её читает и пишет каждая команда отдельным процессом (подробности — в --help и в
`docs/specs/zone-tiler.md`).
"""

import argparse
import collections
import configparser
import contextlib
import ctypes
import ctypes.util
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from typing import NoReturn

VERSION = "0.1.0"

# Коды возврата CLI: 0 — успех, 1 — ошибка выполнения, 2 — ошибка аргументов,
# 3 — ошибка конфига (argparse и сам сообщает об ошибках аргументов кодом 2).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3


def fail(message, code=EXIT_FAIL) -> NoReturn:
    """Сообщить об ошибке в stderr и завершить процесс нужным кодом."""
    print(message, file=sys.stderr)
    sys.exit(code)


HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY_CONF = os.path.join(HERE, "zones.conf")


def config_home():
    """Каталог конфигурации XDG (его уважает и Cinnamon, и остальные утилиты)."""
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def xdg_conf():
    return os.path.join(config_home(), "gridsnap", "zones.conf")


def default_conf():
    """Путь к zones.conf.

    Приоритет — ~/.config/gridsnap/zones.conf; если его нет, читаем/пишем файл рядом
    с кодом (совместимость с рабочими установками). Переопределяется флагом --conf.
    """
    path = xdg_conf()
    return path if os.path.exists(path) else LEGACY_CONF


CONF = default_conf()

# ------------------------------------------------------------ правила окон


def _xprop_props(text):
    """Разобрать вывод xprop в {имя_свойства: [строки значения]}.

    Строки продолжения (значение перенесено на следующую строку) приклеиваются
    к текущему свойству, чтобы один и тот же разбор годился и для однострочной
    формы `WM_CLASS(STRING) = "a", "b"`, и для многострочной.
    """
    props = {}
    current = None
    for raw in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s*=\s*(.*)$", raw)
        if m:
            current = m.group(1)
            props.setdefault(current, []).append(m.group(2))
        elif current is not None and raw.strip():
            props[current].append(raw.strip())
    return props


def _strings(value):
    """Строки из значения xprop: в кавычках — как есть, иначе — по запятой."""
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    if quoted:
        return [re.sub(r"\\(.)", r"\1", s) for s in quoted]
    return [s.strip() for s in value.split(",") if s.strip()]


def window_class_title(win):
    """WM_CLASS (class, вторая строка) и заголовок окна через xprop.

    Заголовок берётся из _NET_WM_NAME, при его отсутствии — из WM_NAME. Любая
    ошибка (нет xprop, окна, доступа) даёт ("", "") — вызывающий просто не найдёт
    совпадений. Чистый разбор вывода вынесен в _xprop_props/_strings.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "WM_CLASS", "_NET_WM_NAME", "WM_NAME"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        props = _xprop_props(out)
        wm_class = ""
        classes = _strings(" ".join(props.get("WM_CLASS", [])))
        if len(classes) >= 2:
            wm_class = classes[1]  # instance, class -> берём class
        elif classes:
            wm_class = classes[0]
        wm_title = ""
        for prop in ("_NET_WM_NAME", "WM_NAME"):
            names = _strings(" ".join(props.get(prop, [])))
            if names and names[0]:
                wm_title = names[0]
                break
        return (wm_class, wm_title)
    except Exception:
        return ("", "")


def match_rules(rules, wm_class, wm_title):
    """Первая подходящая зона для окна или None. Чистая функция — под тесты.

    Селектор `re:<regex>` ищется в строке `class|title` без учёта регистра;
    обычный селектор — это glob по WM_CLASS (тоже без учёта регистра).
    """
    haystack = f"{wm_class}|{wm_title}"
    for selector, zone in rules:
        if selector.lower().startswith("re:"):
            try:
                if re.search(selector[3:], haystack, re.IGNORECASE):
                    return zone
            except re.error:
                continue
        elif fnmatch.fnmatch(wm_class.lower(), selector.lower()):
            return zone
    return None


# --------------------------------------------------- фильтры «какие окна двигать»

# Свойство root со списком управляемых клиентских окон (EWMH).
CLIENT_LIST_ATOM = "_NET_CLIENT_LIST"

# Типы окон, которые не трогают ни демон, ни команды arrange/stack: это не
# самостоятельные «окна-документы», а всплывающие/служебные поверхности.
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

# Метаданные окна, нужные фильтрам и раскладке.
# `window_type` — набор имён `_NET_WM_WINDOW_TYPE_*` (может быть пустым).
WindowMeta = collections.namedtuple("WindowMeta", "wm_class wm_title window_type")


def _type_names(window_type):
    """Имена типов окна из набора/строки; короткое имя поднимается как есть."""
    if not window_type:
        return frozenset()
    if isinstance(window_type, str):
        found = re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", window_type)
        return frozenset(found) if found else frozenset({window_type.strip().upper()})
    return frozenset(window_type)


def is_ignored_type(window_type):
    """True, если среди типов окна есть служебный (диалог, сплэш и т. п.).

    Общий фильтр раскладчика и демона. Принимает набор имён
    `_NET_WM_WINDOW_TYPE_*`, строку с ними или короткое имя (`DIALOG`, ...).
    Пустое значение — не служебное окно.
    """
    names = _type_names(window_type)
    if names & IGNORED_WINDOW_TYPES:
        return True
    return bool({name.rsplit("_", 1)[-1] for name in names} & IGNORED_TYPE_TAILS)


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


# Задержка перед единственным повтором чтения метаданных: `_NET_WM_NAME` иногда
# появляется уже после того, как окно попало в `_NET_CLIENT_LIST`.
META_RETRY_DELAY = 0.2


def read_window_meta(win):
    """Метаданные окна: `WM_CLASS`/`_NET_WM_NAME` + тип окна.

    Если класс или заголовок ещё не появились, один раз перечитывает их после
    короткой паузы (заголовок порой приходит уже после появления окна).
    """
    wm_class, wm_title = window_class_title(win)
    if not wm_class or not wm_title:
        time.sleep(META_RETRY_DELAY)
        wm_class2, wm_title2 = window_class_title(win)
        wm_class = wm_class or wm_class2
        wm_title = wm_title or wm_title2
    return WindowMeta(wm_class, wm_title, window_types(win))


def read_client_list():
    """Клиентские окна из `_NET_CLIENT_LIST` (EWMH) через `xprop -root`.

    Возвращает список id в порядке свойства (снизу вверх) или `None`, если
    список недоступен: нет xprop/дисплея, отсутствует само свойство (xprop
    печатает `no such atom on any window.`) или в выводе нет маркера
    `window id #`. Пустой список — валидное состояние «клиентов нет», его
    нельзя путать с ошибкой. Функцию можно подменить в тестах.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", CLIENT_LIST_ATOM],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    # `_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0x600005`
    if "window id #" not in out:
        return None
    _, _, value = out.partition("#")
    return [int(h, 16) for h in re.findall(r"0x[0-9a-fA-F]+", value)]


# ---------------------------------------------------------------- конфиг сетки


class Grid:
    def __init__(self, path=CONF):
        cp = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            delimiters=("=",),  # ключ `re:<regex>` содержит ':', разделитель только '='
        )
        if not cp.read(path, encoding="utf-8"):
            fail(f"zone-tiler: не найден конфиг {path}", EXIT_CONFIG)
        self.conf = cp
        self.cols = self._numlist(cp.get("grid", "cols", fallback="1.0"))
        self.rows = self._numlist(cp.get("grid", "rows", fallback="1.0"))
        self.zones = {}
        if cp.has_section("zones"):
            for name, spec in cp.items("zones"):
                self.zones[name.strip()] = self._parse_zone(name, spec)
        if not self.zones:
            fail("zone-tiler: в zones.conf нет ни одной зоны в секции [zones]", EXIT_CONFIG)
        # `[rules]` читается отдельным парсером: ConfigParser по умолчанию
        # приводит ключи к нижнему регистру (optionxform=str.lower), из-за чего
        # селектор `re:\S+Deploy\S*` превратился бы в `re:\s+deploy\s*` —
        # `\S` (непробел) стал бы `\s` (пробел), и правило молча не срабатывало
        # бы. Секции `[zones]`/`[keys]` остаются у нижнерегистрового `cp`.
        rules_cp = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            delimiters=("=",),
        )
        rules_cp.optionxform = str  # type: ignore  # сохранить регистр селектора (`re:`)
        rules_cp.read(path, encoding="utf-8")
        self.rules = []
        if rules_cp.has_section("rules"):
            for selector, zone in rules_cp.items("rules"):
                selector, zone = selector.strip(), zone.strip()
                if zone not in self.zones:
                    fail(
                        f"zone-tiler: правило '{selector}' ссылается на неизвестную зону '{zone}'",
                        EXIT_CONFIG,
                    )
                self.rules.append((selector, zone))

    @staticmethod
    def _numlist(value):
        nums = [float(x) for x in re.split(r"[,;\s]+", value.strip()) if x]
        if not nums or abs(nums[-1] - 1.0) > 1e-6:
            fail("zone-tiler: последняя линия в cols/rows должна быть 1.0", EXIT_CONFIG)
        lines = [0.0] + nums
        if any(b <= a for a, b in zip(lines, lines[1:])):
            fail("zone-tiler: линии в cols/rows должны возрастать", EXIT_CONFIG)
        return lines

    def _parse_zone(self, name, spec):
        spec = spec.strip()
        if ":" in spec:  # зона как диапазон ячеек сетки: "0:1, 0:2"
            try:
                cols_part, rows_part = [p.strip() for p in spec.split(",")]
                c0, c1 = (int(v) for v in cols_part.split(":"))
                r0, r1 = (int(v) for v in rows_part.split(":"))
            except Exception:
                fail(
                    f"zone-tiler: зона '{name}' должна быть 'c0:c1, r0:r1' или 'x,y,w,h'",
                    EXIT_CONFIG,
                )
            ncols, nrows = len(self.cols) - 1, len(self.rows) - 1
            if not (0 <= c0 < c1 <= ncols and 0 <= r0 < r1 <= nrows):
                fail(
                    f"zone-tiler: зона '{name}' выходит за сетку "
                    f"({ncols} колонок, {nrows} строк)",
                    EXIT_CONFIG,
                )
            return ("grid", c0, c1, r0, r1)
        # зона как свободный прямоугольник в долях: "x,y,w,h"
        nums = [p for p in re.split(r"[,\s]+", spec) if p]
        try:
            x, y, w, h = (float(v) for v in nums)
        except Exception:
            fail(
                f"zone-tiler: зона '{name}' должна быть 'c0:c1, r0:r1' или 'x,y,w,h'",
                EXIT_CONFIG,
            )
        if not (
            0 <= x < 1
            and 0 <= y < 1
            and w > 0
            and h > 0
            and x + w <= 1.0001
            and y + h <= 1.0001
        ):
            fail(f"zone-tiler: зона '{name}' выходит за пределы экрана (доли 0..1)", EXIT_CONFIG)
        return ("rect", x, y, w, h)

    def fractions(self, zone):
        """Зона в долях рабочей области: (x, y, w, h)."""
        spec = self.zones[zone]
        if spec[0] == "rect":
            return spec[1:]
        _, c0, c1, r0, r1 = spec
        return (
            self.cols[c0],
            self.rows[r0],
            self.cols[c1] - self.cols[c0],
            self.rows[r1] - self.rows[r0],
        )

    def rect(self, zone, wa):
        """Пиксельная рамка зоны внутри рабочей области wa=(x,y,w,h)."""
        spec = self.zones[zone]
        if spec[0] == "rect":
            _, fx, fy, fw, fh = spec
            return self._px(wa, fx, fy, fx + fw, fy + fh)
        _, c0, c1, r0, r1 = spec
        return self._px(wa, self.cols[c0], self.rows[r0], self.cols[c1], self.rows[r1])

    @staticmethod
    def _px(wa, fx1, fy1, fx2, fy2):
        wx, wy, ww, wh = wa
        # края считаем от одних и тех же линий -> соседние зоны стыкуются без щелей
        x1, x2 = wx + round(ww * fx1), wx + round(ww * fx2)
        y1, y2 = wy + round(wh * fy1), wy + round(wh * fy2)
        return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


# ------------------------------------------------------------- мониторы (Gdk)


def read_window_center(win):
    """Центр окна (px, py) по абсолютной геометрии `xwininfo`; None при ошибке.

    Нужен только чтобы выбрать монитор окна, поэтому берётся клиентская
    геометрия целиком: рамка WM сдвигает её на считаные пиксели и на выбор
    монитора не влияет.
    """
    try:
        out = subprocess.check_output(
            ["xwininfo", "-id", hex(win)], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None

    def num(label):
        found = re.search(rf"{label}:\s+(-?\d+)", out)
        return int(found.group(1)) if found else None

    x = num("Absolute upper-left X")
    y = num("Absolute upper-left Y")
    w = num("Width")
    h = num("Height")
    if x is None or y is None or w is None or h is None:
        return None
    return (x + w // 2, y + h // 2)


def monitor_for_point(monitors, point):
    """Рабочая область монитора, содержащего точку; иначе ближайшая по центру.

    Точка может не попасть ни в одну область (окно под панелью, в щели между
    мониторами) — тогда берётся область с ближайшим центром, а не монитор 0.
    `monitors` — непустой список прямоугольников (x, y, w, h).
    """
    px, py = point
    for wa in monitors:
        x, y, w, h = wa
        if x <= px < x + w and y <= py < y + h:
            return wa
    return min(
        monitors,
        key=lambda wa: (wa[0] + wa[2] / 2 - px) ** 2 + (wa[1] + wa[3] / 2 - py) ** 2,
    )


def window_monitor(win, mons):
    """Рабочая область монитора окна или None, если её не определить."""
    point = read_window_center(win)
    if point is None:
        return None
    return monitor_for_point(mons, point)


def window_workspace(win):
    """Номер виртуального рабочего стола окна (`_NET_WM_DESKTOP`) или None."""
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_WM_DESKTOP"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"=\s*(-?\d+)", out)
    return int(found.group(1)) if found else None


def monitors():
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk  # type: ignore

        dpy = Gdk.Display.get_default()
        out = []
        for i in range(dpy.get_n_monitors() if dpy else 0):
            a = dpy.get_monitor(i).get_workarea()
            out.append((a.x, a.y, a.width, a.height))
        if out:
            return out
    except Exception:
        pass
    # резерв: EWMH _NET_WORKAREA (первый рабочий стол)
    try:
        txt = subprocess.check_output(
            ["xprop", "-root", "_NET_WORKAREA"], text=True, stderr=subprocess.DEVNULL
        )
        nums = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
        if len(nums) == 4 and nums[2] > 0:
            return [tuple(nums)]
    except Exception:
        pass
    fail("zone-tiler: не удалось определить рабочую область экрана", EXIT_FAIL)


def frame_extents(win):
    """Толщина рамки/титлбара оконного менеджера: (left, right, top, bottom)."""
    try:
        txt = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_FRAME_EXTENTS"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        nums = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
        if len(nums) == 4 and all(0 <= n < 200 for n in nums):
            return tuple(nums)
    except Exception:
        pass
    return (0, 0, 0, 0)


def client_size(rect, extents):
    """Геометрия клиента внутри целевой рамки окна (компенсация рамки WM).

    WM рисует рамку/титлбар вокруг клиента, поэтому клиенту выдаётся зона минус
    _NET_FRAME_EXTENTS. Ширина и высота не меньше 1 px. Чистая функция — под тесты.
    """
    x, y, w, h = rect
    left, right, top, bottom = extents
    return (x, y, max(1, w - left - right), max(1, h - top - bottom))


def active_window():
    try:
        txt = subprocess.check_output(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        fail("zone-tiler: xprop не найден (пакет x11-utils)", EXIT_FAIL)
    m = re.search(r"(0x[0-9a-fA-F]+)", txt)
    if not m or int(m.group(1), 16) == 0:
        fail("zone-tiler: нет активного окна", EXIT_FAIL)
    return int(m.group(1), 16)


# ----------------------------------------------------------------------- X11


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        ("pad", ctypes.c_long * 15),  # чтобы структура была не меньше sizeof(XEvent)
    ]


CLIENT_MESSAGE = 33
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # Notify | Redirect
X_FLAGS = 1 | 2 | 4 | 8  # X, Y, Width, Height
GRAVITY_NORTH_WEST = 1
SOURCE_PAGER = 2


class X11:
    def __init__(self):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        self.x = ctypes.CDLL(name)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        dpy_name = (os.environ.get("DISPLAY") or "").encode() or None
        self.dpy = self.x.XOpenDisplay(dpy_name)
        if not self.dpy:
            fail("zone-tiler: не удалось подключиться к X-серверу (DISPLAY)", EXIT_FAIL)
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.root = self.x.XDefaultRootWindow(self.dpy)
        self.x.XInternAtom.restype = ctypes.c_ulong
        self.x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(XClientMessageEvent),
        ]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XCloseDisplay.restype = ctypes.c_int

    def close(self):
        """Закрыть X-соединение (парно `XOpenDisplay`). Идемпотентно."""
        if getattr(self, "dpy", None):
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    def atom(self, name):
        return self.x.XInternAtom(self.dpy, name.encode(), 0)

    def send(self, win, msg_type, values):
        ev = XClientMessageEvent()
        ev.type = CLIENT_MESSAGE
        ev.send_event = 1
        ev.display = self.dpy
        ev.window = win
        ev.message_type = self.atom(msg_type)
        ev.format = 32
        for i, v in enumerate(values):
            ev.data[i] = v
        self.x.XSendEvent(self.dpy, self.root, 0, SUBSTRUCTURE_MASK, ctypes.byref(ev))
        self.x.XSync(self.dpy, 0)

    def set_state(self, win, action, first, second=None):
        """action: 0 = снять, 1 = добавить, 2 = переключить.

        `second` — второе свойство состояния (например
        `_NET_WM_STATE_MAXIMIZED_HORZ`); пустое — без него.
        """
        self.send(
            win,
            "_NET_WM_STATE",
            [action, self.atom(first), self.atom(second) if second else 0, SOURCE_PAGER, 0],
        )

    def move_resize(self, win, x, y, w, h):
        flags = (SOURCE_PAGER << 12) | (X_FLAGS << 8) | GRAVITY_NORTH_WEST
        self.send(win, "_NET_MOVERESIZE_WINDOW", [flags, x, y, w, h])


# --------------------------------------------- раскладка и история (без X)

# Сколько последних операций хранить в истории раскладок.
HISTORY_LIMIT = 20


def movable_windows(client_ids, metas):
    """id окон, которые можно двигать, в порядке `_NET_CLIENT_LIST`.

    Те же фильтры, что у демона: нужен непустой `WM_CLASS` и не служебный тип
    окна. `override_redirect` окон в списке клиентов EWMH нет, поэтому там такой
    проверки не требуется. Чистая функция — под тесты.
    """
    out = []
    for win in client_ids:
        meta = metas.get(win)
        if meta is None:
            continue
        if not meta.wm_class or is_ignored_type(meta.window_type):
            continue
        out.append(win)
    return out


def choose_assignments(client_ids, metas, zones, rules, by="auto"):
    """Сопоставить окна зонам. Чистая функция — под тесты.

    client_ids — id в порядке `_NET_CLIENT_LIST` (снизу вверх);
    metas — {id: WindowMeta};
    zones — имена зон в порядке файла (важен для order/auto);
    rules — [(селектор, зона)] из `[rules]`;
    by — режим:
      `rules` — только окна со сработавшим правилом, каждое в свою зону;
      `order` — все подвижные окна по порядку в зоны по порядку; лишним окнам
                (сверх числа зон) достаётся последняя зона (там они — стопкой);
      `auto`  — сначала правила, затем свободные (никем не занятые по правилу)
                зоны по порядку для окон без правила; лишние не трогаются.

    Возвращает список (id, зона) в порядке укладки; неназначенные окна в него
    не попадают.
    """
    windows = movable_windows(client_ids, metas)
    if by == "rules":
        result = []
        for win in windows:
            zone = match_rules(rules, metas[win].wm_class, metas[win].wm_title)
            if zone is not None:
                result.append((win, zone))
        return result
    if by == "order":
        if not zones:
            return []
        return [
            (win, zones[i] if i < len(zones) else zones[-1])
            for i, win in enumerate(windows)
        ]
    # auto
    by_rule = {}
    used = set()
    for win in windows:
        zone = match_rules(rules, metas[win].wm_class, metas[win].wm_title)
        if zone is not None:
            by_rule[win] = zone
            used.add(zone)
    free = [zone for zone in zones if zone not in used]
    result = []
    nxt = 0
    for win in windows:
        if win in by_rule:
            result.append((win, by_rule[win]))
        elif nxt < len(free):
            result.append((win, free[nxt]))
            nxt += 1
    return result


def cascade_rect(base, index, step, bounds):
    """Рамка `index`-го окна стопки: `base` сдвинута на `index*step` вниз-вправо.

    Окно не уменьшается и не прижимается к зоне: так у каждого окна остаётся
    виден заголовок (обычно вверху-слева), даже если само окно частично уйдёт
    за правый/нижний край зоны или рабочей области. Левый верхний угол при этом
    не выпускается за `bounds` (рабочую область), чтобы окно вообще не уехало с
    экрана. `step <= 0` отключает каскад. Чистая функция — под тесты.
    """
    x, y, w, h = base
    bx, by, bw, bh = bounds
    shift = max(0, index) * max(0, step)
    max_dx = max(0, bx + bw - 1 - x)
    max_dy = max(0, by + bh - 1 - y)
    return (x + min(shift, max_dx), y + min(shift, max_dy), w, h)


def state_home():
    """XDG-каталог состояния (не конфиг и не кэш)."""
    return os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )


def history_path():
    """Путь к файлу истории раскладок.

    История — это состояние: undo запускается отдельным процессом, поэтому она
    должна пережить выход; но это не конфиг и не то, что вправе вычистить
    чистильщик кэша. Отсюда XDG_STATE_HOME
    (`~/.local/state/gridsnap/history.json`) — место именно для такого состояния.
    """
    return os.path.join(state_home(), "gridsnap", "history.json")


class History:
    """Персистентная история операций раскладки (JSON, до HISTORY_LIMIT штук)."""

    def __init__(self, path=None, limit=HISTORY_LIMIT):
        self.path = path or history_path()
        self.limit = max(1, int(limit))

    def load(self):
        """Операции из файла (старые → новые). Битый/чужой файл — пустой список."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        ops = data.get("operations") if isinstance(data, dict) else None
        if not isinstance(ops, list):
            return []
        return [op for op in ops if isinstance(op, dict)]

    def save(self, operations):
        """Записать операции, обрезав до предела, атомарно. Райзит OSError."""
        ops = list(operations)[-self.limit :]
        directory = os.path.dirname(self.path)
        tmp = f"{self.path}.tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "operations": ops}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def record(self, operation):
        """Дописать операцию, обрезав историю до предела. Райзит OSError."""
        ops = self.load()
        ops.append(operation)
        self.save(ops)


def undo_targets(operations, all_=False):
    """Какие окна и в какие рамки возвращать. Чистая функция — под тесты.

    `all_=False` — последняя операция; `all_=True` — вся история, причём для
    каждого окна берётся рамка из самой ранней операции, где оно встречается
    (так окно возвращается в состояние «до всех раскладок»). Возвращает список
    `(id, frame, wm_class)` в порядке первого появления окон.
    """
    if not operations:
        return []
    ops = operations if all_ else operations[-1:]
    first = {}
    order = []
    for op in ops:
        for item in op.get("windows") or []:
            if not isinstance(item, dict):
                continue
            win = item.get("id")
            frame = item.get("frame")
            if win is None or not isinstance(frame, (list, tuple)) or len(frame) != 4:
                continue
            if win not in first:
                first[win] = (list(frame), item.get("wm_class"))
                order.append(win)
    return [(win, first[win][0], first[win][1]) for win in order]


def remember(kind, records, quiet=False):
    """Записать операцию в историю, не ломая раскладку из-за ошибки записи."""
    if not records:
        return
    operation = {"op": kind, "time": time.time(), "windows": records}
    try:
        History().record(operation)
    except OSError as exc:
        if not quiet:
            print(f"zone-tiler: не удалось сохранить историю: {exc}", file=sys.stderr)


def snapshot_window(win, zone=None):
    """Снимок окна для истории: прежняя рамка, класс, воркспейс.

    Райзит исключение, если геометрию не прочитать (окно исчезло) — вызывающий
    решает, записывать ли операцию.
    """
    extents = frame_extents(win)
    frame = read_frame(win, extents)
    wm_class, _ = window_class_title(win)
    return {
        "id": win,
        "frame": list(frame),
        "wm_class": wm_class,
        "workspace": window_workspace(win),
        "zone": zone,
    }


# -------------------------------------------------------------------- команды


def cmd_list(grid, mon_index):
    wa = monitors()[mon_index]
    print(f"рабочая область монитора {mon_index}: {wa[0]},{wa[1]} {wa[2]}x{wa[3]}")
    width = max(len(z) for z in grid.zones)
    for name in sorted(grid.zones):
        x, y, w, h = grid.rect(name, wa)
        print(f"  {name:<{width}}  x={x:<5} y={y:<5} {w}x{h}")


def read_frame(window, extents):
    """Фактическая рамка окна (x, y, w, h) по xwininfo с учётом рамки WM."""
    out = subprocess.check_output(
        ["xwininfo", "-id", hex(window)], text=True, stderr=subprocess.DEVNULL
    )

    def num(label):
        found = re.search(rf"{label}:\s+(-?\d+)", out)
        if found is None:
            raise ValueError(f"xwininfo: нет поля '{label}'")
        return int(found.group(1))

    geom = {
        k: num(k)
        for k in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
    }
    left, right, top, bottom = extents
    return (
        geom["Absolute upper-left X"] - left,
        geom["Absolute upper-left Y"] - top,
        geom["Width"] + left + right,
        geom["Height"] + top + bottom,
    )


def apply_zone(window, rect, label="", x11=None, verify=False, quiet=False):
    """Вложить окно в пиксельную рамку rect=(x, y, w, h). Без глобального состояния.

    Снимает максимизацию/фуллскрин и компенсирует рамку WM. `x11` можно подменить
    (в тестах) — тогда X-сервер не нужен. Соединение, открытое самой функцией
    (`x11 is None`), она же и закрывает; переданное чужое — не трогает, им
    владеет вызывающий (долгоживущий демон переиспользует одно соединение).
    При `verify` возвращает фактическую рамку.
    """
    x, y, w, h = rect
    extents = frame_extents(window)
    own = x11 is None
    if own:
        x11 = X11()
    try:
        # снять максимизацию/фуллскрин/половинное тайлирование, иначе WM вернёт своё
        x11.set_state(window, 0, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
        x11.set_state(window, 0, "_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_FULLSCREEN")
        time.sleep(0.05)
        x11.move_resize(window, *client_size(rect, extents))
        time.sleep(0.05)
        if not quiet:
            where = f"зона '{label}'" if label else "зона"
            print(f"окно 0x{window:x} -> {where}: x={x} y={y} {w}x{h}")
        if verify:
            try:
                actual = read_frame(window, extents)
            except Exception:
                return None
            if not quiet:
                mark = "— совпало с зоной" if actual == (x, y, w, h) else "— не совпало!"
                print(f"рамка по факту: x={actual[0]} y={actual[1]} {actual[2]}x{actual[3]} {mark}")
            return actual
        return None
    finally:
        # чужое соединение не закрываем: им владеет вызывающий (демон)
        if own:
            x11.close()


def cmd_apply(grid, args):
    try:
        wa = monitors()[args.monitor]
    except IndexError:
        fail(f"zone-tiler: монитора {args.monitor} нет", EXIT_USAGE)
    rect = grid.rect(args.zone, wa)
    if args.dry_run:
        x, y, w, h = rect
        print(f"{args.zone}: x={x} y={y} {w}x{h} (монитор {args.monitor})")
        return
    window = args.window or active_window()
    before = None
    try:
        before = snapshot_window(window, zone=args.zone)
    except Exception:
        before = None  # геометрию не прочитать — без записи в историю
    apply_zone(window, rect, label=args.zone, verify=args.verify, quiet=args.quiet)
    if before is not None:
        remember("apply", [before], quiet=args.quiet)


def cmd_arrange(grid, args):
    """Разложить клиентские окна `_NET_CLIENT_LIST` по зонам.

    Режим `--by` (auto/rules/order) выбирает окна чистой `choose_assignments`;
    каждое окно ставится на мониторе, где оно находится. Несколько окон,
    попавших в одну зону одного монитора, ложатся туда стопкой со сдвигом
    `--offset`. Прежние рамки записываются в историю для `undo`.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail("zone-tiler: не удалось прочитать _NET_CLIENT_LIST (нужен DISPLAY и xprop)", EXIT_FAIL)
    metas = {win: read_window_meta(win) for win in client_ids}
    assignments = choose_assignments(client_ids, metas, list(grid.zones), grid.rules, args.by)
    if not assignments:
        if not args.quiet:
            print("zone-tiler: нет окон, подходящих под раскладку")
        return
    mons = monitors()
    x11 = None if args.dry_run else X11()
    counts = {}
    records = []
    try:
        for win, zone in assignments:
            wa = window_monitor(win, mons)
            if wa is None:
                if not args.quiet:
                    print(f"окно 0x{win:x}: не удалось определить монитор — пропускаю", file=sys.stderr)
                continue
            key = (wa, zone)
            index = counts.get(key, 0)
            counts[key] = index + 1
            base = grid.rect(zone, wa)
            rect = base if index == 0 else cascade_rect(base, index, args.offset, wa)
            if args.dry_run:
                x, y, w, h = rect
                print(f"окно 0x{win:x} -> зона '{zone}': x={x} y={y} {w}x{h}")
                continue
            try:
                before = snapshot_window(win, zone=zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(f"окно 0x{win:x}: не удалось переставить ({exc})", file=sys.stderr)
                continue
            if before is not None:
                records.append(before)
    finally:
        if x11 is not None:
            x11.close()
    if not args.dry_run:
        remember("arrange", records, quiet=args.quiet)


def cmd_stack(grid, args):
    """Собрать подвижные клиентские окна в одну зону стопкой (каскадом)."""
    client_ids = read_client_list()
    if client_ids is None:
        fail("zone-tiler: не удалось прочитать _NET_CLIENT_LIST (нужен DISPLAY и xprop)", EXIT_FAIL)
    metas = {win: read_window_meta(win) for win in client_ids}
    windows = movable_windows(client_ids, metas)
    if not windows:
        if not args.quiet:
            print("zone-tiler: нет окон для стопки")
        return
    try:
        wa = monitors()[args.monitor]
    except IndexError:
        fail(f"zone-tiler: монитора {args.monitor} нет", EXIT_USAGE)
    base = grid.rect(args.zone, wa)
    x11 = None if args.dry_run else X11()
    records = []
    try:
        for index, win in enumerate(windows):
            rect = base if index == 0 else cascade_rect(base, index, args.offset, wa)
            if args.dry_run:
                x, y, w, h = rect
                print(f"окно 0x{win:x} -> зона '{args.zone}' (стопка {index + 1}): x={x} y={y} {w}x{h}")
                continue
            try:
                before = snapshot_window(win, zone=args.zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=args.zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(f"окно 0x{win:x}: не удалось положить в стопку ({exc})", file=sys.stderr)
                continue
            if before is not None:
                records.append(before)
    finally:
        if x11 is not None:
            x11.close()
    if not args.dry_run:
        remember("stack", records, quiet=args.quiet)


def cmd_undo(grid, args):
    """Вернуть окна на прежние рамки из истории (последнюю или всю)."""
    history = History()
    operations = history.load()
    if not operations:
        if not args.quiet:
            print("zone-tiler: история раскладок пуста")
        return
    targets = undo_targets(operations, all_=args.all)
    x11 = X11() if targets else None
    restored = 0
    skipped = 0

    def warn(message):
        nonlocal skipped
        skipped += 1
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        for win, frame, wm_class in targets:
            current_class, _ = window_class_title(win)
            if wm_class and current_class and current_class != wm_class:
                # id мог переиспользоваться другим приложением после выхода старого
                warn(f"окно 0x{win:x}: класс не совпал ({current_class} ≠ {wm_class}) — пропускаю")
                continue
            if wm_class and not current_class:
                warn(f"окно 0x{win:x}: уже не существует — пропускаю")
                continue
            try:
                apply_zone(win, tuple(frame), x11=x11, quiet=args.quiet, verify=args.verify)
                restored += 1
            except (Exception, SystemExit) as exc:
                warn(f"окно 0x{win:x}: не удалось вернуть ({exc})")
    finally:
        if x11 is not None:
            x11.close()
    try:
        history.save([] if args.all else operations[:-1])
    except OSError as exc:
        if not args.quiet:
            print(f"zone-tiler: не удалось обновить историю: {exc}", file=sys.stderr)
    if not args.quiet:
        tail = f", пропущено: {skipped}" if skipped else ""
        print(f"zone-tiler: возвращено окон: {restored}{tail}")


def cmd_keys(grid):
    """Печатает пары 'зона<TAB>сочетание' из секции [keys] (для setup-hotkeys.py)."""
    if not grid.conf.has_section("keys"):
        return
    for zone, binding in grid.conf.items("keys"):
        zone, binding = zone.strip(), binding.strip()
        if zone not in grid.zones:
            print(f"zone-tiler: в [keys] зона '{zone}' отсутствует в [zones]", file=sys.stderr)
            continue
        if binding:
            print(f"{zone}\t{binding}")


# ---------------------------------------------------------------- профили

PROFILE_ACTIONS = ("list", "show", "save", "use", "rm")


def _run_hotkeys(conf, quiet=False):
    """Перечитать [keys] из conf и обновить сочетания Cinnamon (best-effort)."""
    script = os.path.join(HERE, "setup-hotkeys.py")
    try:
        r = subprocess.run(
            [sys.executable, script, "--conf", conf], text=True, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"zone-tiler: сочетания не обновлены: {exc}", file=sys.stderr)
        return
    if r.returncode:
        tail = (r.stderr or r.stdout).strip()
        print(f"zone-tiler: сочетания не обновлены ({tail})", file=sys.stderr)
        return
    if not quiet:
        first = (r.stdout or r.stderr).strip().splitlines()
        print(f"zone-tiler: {first[0] if first else 'сочетания обновлены'}")


def cmd_profile(args):
    """Профили раскладок: list/show/save/use/rm (импорт zone_config ленивый)."""
    import zone_config as zc

    action = (args.zone or "").strip()
    if action not in PROFILE_ACTIONS:
        fail(
            "zone-tiler: profile — укажите действие: "
            "list | show <имя> | save <имя> | use <имя> | rm <имя>",
            EXIT_USAGE,
        )
    conf = args.conf
    if action == "list":
        for name in zc.list_profiles(conf):
            print(name)
        return

    name = (args.name or "").strip()
    if not name:
        fail(f"zone-tiler: profile {action} — укажите имя профиля", EXIT_USAGE)

    try:
        if action == "show":
            sys.stdout.write(zc.profile_text(name, conf))
        elif action == "save":
            zones = zc.load_zones(conf)
            rules = zc.load_extra_sections(conf, sections=["rules"]).get("rules", [])
            path = zc.save_profile(name, zones, rules, conf)
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» сохранён: {path}")
        elif action == "rm":
            zc.remove_profile(name, conf)
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» удалён")
        else:  # use
            zones = zc.apply_profile(name, conf)
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» применён: {len(zones)} зон")
            if not args.no_hotkeys:
                _run_hotkeys(conf, quiet=args.quiet)
    except ValueError as exc:
        fail(f"zone-tiler: {exc}", EXIT_USAGE)
    except FileNotFoundError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Раскладка окон по зонам сетки (Cinnamon/X11)",
        epilog=(
            "Примеры: zone-tiler.py arrange --by order; zone-tiler.py stack main; "
            "zone-tiler.py undo --all; zone-tiler.py profile use work"
        ),
    )
    ap.add_argument(
        "command",
        choices=["list", "apply", "keys", "arrange", "stack", "undo", "profile"],
        help=(
            "list/apply/keys — как раньше; arrange — разложить окна по зонам; "
            "stack — собрать окна в зону стопкой; undo — вернуть рамки из истории; "
            "profile — свои профили раскладок (зоны + правила + клавиши)"
        ),
    )
    ap.add_argument("zone", nargs="?", help="имя зоны для apply/stack или действие profile")
    ap.add_argument("name", nargs="?", help="имя профиля для profile show/save/use/rm")
    ap.add_argument("--monitor", type=int, default=0, help="номер монитора (по умолчанию 0)")
    ap.add_argument("--window", type=lambda s: int(s, 0), help="id окна (по умолчанию активное)")
    ap.add_argument(
        "--by",
        choices=["auto", "rules", "order"],
        default="auto",
        help="arrange: выбор окон — правила и остаток (auto), только правила, или по порядку",
    )
    ap.add_argument(
        "--offset",
        type=int,
        default=20,
        help="сдвиг окна в стопке/каскаде, px (по умолчанию 20; 0 — без сдвига)",
    )
    ap.add_argument("--all", action="store_true", help="undo: откатить всю историю, а не последнюю операцию")
    ap.add_argument(
        "--no-hotkeys",
        action="store_true",
        help="profile use: не регистрировать сочетания клавиш после активации",
    )
    ap.add_argument("--dry-run", action="store_true", help="только посчитать рамку")
    ap.add_argument("--verify", action="store_true", help="перечитать реальную геометрию")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--version", action="version", version=f"zone-tiler {VERSION}")
    args = ap.parse_args(argv)

    if args.command == "profile":
        cmd_profile(args)
        return EXIT_OK

    grid = Grid(args.conf)
    if args.command == "list":
        cmd_list(grid, args.monitor)
        return EXIT_OK
    if args.command == "keys":
        cmd_keys(grid)
        return EXIT_OK
    if args.command == "arrange":
        cmd_arrange(grid, args)
        return EXIT_OK
    if args.command == "undo":
        cmd_undo(grid, args)
        return EXIT_OK
    if not args.zone:
        fail("zone-tiler: укажите зону, например: zone-tiler.py apply left", EXIT_USAGE)
    if args.zone not in grid.zones:
        fail(
            f"zone-tiler: нет зоны '{args.zone}'. Есть: " + ", ".join(sorted(grid.zones)),
            EXIT_USAGE,
        )
    if args.command == "stack":
        cmd_stack(grid, args)
        return EXIT_OK
    cmd_apply(grid, args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

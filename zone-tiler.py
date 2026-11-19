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
"""

import argparse
import configparser
import ctypes
import ctypes.util
import fnmatch
import os
import re
import subprocess
import sys
import time

VERSION = "0.1.0"

# Коды возврата CLI: 0 — успех, 1 — ошибка выполнения, 2 — ошибка аргументов,
# 3 — ошибка конфига (argparse и сам сообщает об ошибках аргументов кодом 2).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3


def fail(message, code=EXIT_FAIL):
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
        self.rules = []
        if cp.has_section("rules"):
            for selector, zone in cp.items("rules"):
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


def monitors():
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

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

    def set_state(self, win, action, first, second=0):
        """action: 0 = снять, 1 = добавить, 2 = переключить."""
        self.send(
            win, "_NET_WM_STATE", [action, self.atom(first), self.atom(second) or 0, SOURCE_PAGER, 0]
        )

    def move_resize(self, win, x, y, w, h):
        flags = (SOURCE_PAGER << 12) | (X_FLAGS << 8) | GRAVITY_NORTH_WEST
        self.send(win, "_NET_MOVERESIZE_WINDOW", [flags, x, y, w, h])


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
    geom = {
        k: int(re.search(rf"{k}:\s+(-?\d+)", out).group(1))
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
    (в тестах) — тогда X-сервер не нужен. При `verify` возвращает фактическую рамку.
    """
    x, y, w, h = rect
    extents = frame_extents(window)
    if x11 is None:
        x11 = X11()
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
    apply_zone(window, rect, label=args.zone, verify=args.verify, quiet=args.quiet)


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Раскладка окон по зонам сетки (Cinnamon/X11)")
    ap.add_argument("command", choices=["list", "apply", "keys"])
    ap.add_argument("zone", nargs="?", help="имя зоны для apply")
    ap.add_argument("--monitor", type=int, default=0, help="номер монитора (по умолчанию 0)")
    ap.add_argument("--window", type=lambda s: int(s, 0), help="id окна (по умолчанию активное)")
    ap.add_argument("--dry-run", action="store_true", help="только посчитать рамку")
    ap.add_argument("--verify", action="store_true", help="перечитать реальную геометрию")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--version", action="version", version=f"zone-tiler {VERSION}")
    args = ap.parse_args(argv)

    grid = Grid(args.conf)
    if args.command == "list":
        cmd_list(grid, args.monitor)
        return EXIT_OK
    if args.command == "keys":
        cmd_keys(grid)
        return EXIT_OK
    if not args.zone:
        fail("zone-tiler: укажите зону, например: zone-tiler.py apply left", EXIT_USAGE)
    if args.zone not in grid.zones:
        fail(
            f"zone-tiler: нет зоны '{args.zone}'. Есть: " + ", ".join(sorted(grid.zones)),
            EXIT_USAGE,
        )
    cmd_apply(grid, args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

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

Зоны можно задать на каждый монитор отдельно — секцией `[zones:<выход>]`:

  [zones:HDMI-1]
  main = 0, 0, 0.6, 1

Имя выхода берётся из `xrandr --listmonitors` (`eDP-1`, `HDMI-1`) и стабильно
между перезагрузками и переподключениями кабеля; `[zones]` обязательна и служит
набором по умолчанию для мониторов без своей секции. `[rules]` и `[keys]` общие.

Перемещение активного окна в соседнюю зону — команда `move <направление>`, а
сочетания для неё — в необязательной секции `[move]` (зона окна ищется по его
центру, сосед — тем же правилом, что стрелки в пикере):

  [move]
  left  = <Super>Left
  right = <Super>Right
  up    = <Super>Up
  down  = <Super>Down

Секции нет — действуют эти же сочетания (их же muffin занимает своим
`push-tile-*`, поэтому их и берём по умолчанию). Неизвестный ключ — ошибка
конфига.

Команды:
  zone-tiler.py list                      # показать зоны и их пиксельные рамки
  zone-tiler.py apply left                # вложить активное окно в зону "left"
  zone-tiler.py apply left --dry-run      # только посчитать рамку
  zone-tiler.py apply left --monitor 1    # зона на другом мониторе
  zone-tiler.py apply left --monitor HDMI-1   # или по имени выхода (приоритетнее номера)
  zone-tiler.py apply left --window 0x3400007
  zone-tiler.py apply left --workspace 1  # зона сетки рабочего стола 1
  zone-tiler.py move left                 # активное окно — в соседнюю зону слева
  zone-tiler.py keys --move               # сочетания перемещения (для setup-hotkeys.py)
  zone-tiler.py arrange                   # разложить окна по зонам (по правилам и остатку)
  zone-tiler.py arrange --by order        # разложить окна по зонам по порядку списка
  zone-tiler.py stack main                # собрать окна в зону main стопкой (каскадом)
  zone-tiler.py undo                      # вернуть окна из последней операции
  zone-tiler.py undo --all                # откатить всю историю раскладок

Своя сетка на рабочем столе — это профиль: секция `[workspaces]` сопоставляет
номер стола (с 1) имени профиля из `templates/`. `list`/`apply`/`arrange`/`stack`
по умолчанию берут сетку активного стола (`--workspace current`), `--workspace N`
адресует стол номером EWMH (с нуля). Имя зоны остаётся глобальным, `[keys]`
общая на файл.

Профили раскладок (свои шаблоны: зоны + правила + клавиши) хранятся в
`~/.config/gridsnap/templates/*.conf`, активация — `profile use <имя>`:

  zone-tiler.py profile list              # сохранённые профили
  zone-tiler.py profile save work         # текущий zones.conf — в профиль «work»
  zone-tiler.py profile use work          # активировать профиль (и обновить сочетания)
  zone-tiler.py profile use work --no-hotkeys
  zone-tiler.py profile show work
  zone-tiler.py profile rm work
  zone-tiler.py profile export work              # JSON в stdout (для пайпов)
  zone-tiler.py profile export work work.json    # JSON в файл — поделиться раскладкой
  zone-tiler.py profile import work.json         # JSON → templates/<имя>.conf
  zone-tiler.py profile import work.json --name home  # то же, но под другим именем

JSON-профиль — переносимый обмен между машинами (``gridsnap_profile``: 1);
основным остаётся ``.conf``, JSON только для выгрузки и загрузки.

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
import socket
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


def selector_matches(selector, wm_class, wm_title):
    """Подходит ли окно под один селектор. Чистая функция — под тесты.

    Селектор `re:<regex>` ищется в строке `class|title` без учёта регистра;
    обычный селектор — это glob по WM_CLASS (тоже без учёта регистра).
    Некорректная регулярка не совпадает ни с чем. Один разбор на `[rules]` и
    `[ignore]`, чтобы правила и исключения сопоставлялись одинаково.
    """
    if selector.lower().startswith("re:"):
        try:
            found = re.search(selector[3:], f"{wm_class}|{wm_title}", re.IGNORECASE)
        except re.error:
            return False
        return found is not None
    return fnmatch.fnmatch(wm_class.lower(), selector.lower())


def match_rules(rules, wm_class, wm_title):
    """Первая подходящая зона для окна или None. Чистая функция — под тесты."""
    for selector, zone in rules:
        if selector_matches(selector, wm_class, wm_title):
            return zone
    return None


def matches_selectors(selectors, wm_class, wm_title):
    """Есть ли среди селекторов хотя бы один подходящий. Чистая функция."""
    return any(selector_matches(s, wm_class, wm_title) for s in (selectors or ()))


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


def window_excluded(wm_class, wm_title, window_type, ignore_selectors=()):
    """Оставить ли окно в покое. Чистая функция — один источник истины фильтра.

    True, если тип окна служебный (`is_ignored_type`) или окно подошло хотя бы
    под один селектор `[ignore]` (glob по `WM_CLASS` без учёта регистра или
    `re:<regex>` по строке `class|title`). Один предикат для автоукладки демона,
    `arrange`/`stack`, явных `apply`/`move` и пикера — поэтому фильтр не
    расходится между точками вызова.
    """
    if is_ignored_type(window_type):
        return True
    return matches_selectors(ignore_selectors, wm_class, wm_title)


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


def window_is_excluded(win, ignore_selectors=()):
    """Под исключением ли окно `win` (`[ignore]`/служебный тип). Читает X.

    Одна проверка для явных команд (`apply`, `move`, `undo`) и пикера: факт для
    вызывающего, сообщение печатает он сам. Это та же `window_excluded`, что и у
    автоукладки, поэтому фильтр не может разойтись между точками вызова.
    """
    meta = read_window_meta(win)
    return window_excluded(meta.wm_class, meta.wm_title, meta.window_type, ignore_selectors)


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
        # Путь нужен там, где секции читает не сам Grid: `keys --move` берёт
        # `[move]` через `zone_config.load_move_keys`, а `[move]` в профилях
        # столов не живёт (она общая на файл).
        self.path = path
        self.conf = cp
        self.cols = self._numlist(cp.get("grid", "cols", fallback="1.0"))
        self.rows = self._numlist(cp.get("grid", "rows", fallback="1.0"))
        self.zones = {}
        if cp.has_section("zones"):
            for name, spec in cp.items("zones"):
                self.zones[name.strip()] = self._parse_zone(name, spec)
        # Зоны на каждый монитор: [zones:<выход xrandr>], например [zones:HDMI-1].
        # Имя секции регистрозависимо (ConfigParser секции не приводит к нижнему
        # регистру), чтобы совпадать с именем выхода как его печатает xrandr.
        self.monitor_zones = {}
        for section in cp.sections():
            if not section.startswith("zones:"):
                continue
            output = section.split(":", 1)[1].strip()
            if not output:
                fail(f"zone-tiler: пустое имя монитора в секции [{section}]", EXIT_CONFIG)
            zones = {}
            for name, spec in cp.items(section):
                zones[name.strip()] = self._parse_zone(name, spec)
            if not zones:
                fail(f"zone-tiler: в секции [{section}] нет ни одной зоны", EXIT_CONFIG)
            self.monitor_zones[output] = zones
        if not self.zones:
            if self.monitor_zones:
                fail(
                    "zone-tiler: есть только секции [zones:<выход>], а обязательной [zones] нет; "
                    "[zones] задаёт набор зон по умолчанию",
                    EXIT_CONFIG,
                )
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
        # Правило проверяется по объединению наборов: зона может быть описана
        # только для части мониторов (тогда на остальных берётся общий [zones]).
        known = set(self.all_zone_names())
        if rules_cp.has_section("rules"):
            for selector, zone in rules_cp.items("rules"):
                selector, zone = selector.strip(), zone.strip()
                if zone not in known:
                    fail(
                        f"zone-tiler: правило '{selector}' ссылается на неизвестную зону '{zone}'",
                        EXIT_CONFIG,
                    )
                self.rules.append((selector, zone))
        # `[ignore]` — те же селекторы, что в `[rules]`, но без цели: значение —
        # произвольная заметка для человека/журнала и на сопоставление не влияет.
        # Секция читается тем же парсером с `optionxform = str`, поэтому регистр
        # `re:`-регулярки сохраняется; порядок строк сохраняется для журнала.
        self.ignore = []
        if rules_cp.has_section("ignore"):
            for selector, _note in rules_cp.items("ignore"):
                selector = selector.strip()
                if selector:
                    self.ignore.append(selector)

    def all_zone_names(self):
        """Имена зон в объединении всех наборов (общий и по мониторам).

        Порядок — сначала `[zones]` как в файле, затем зоны из `[zones:<выход>]`,
        которых в общем наборе нет. Нужен для проверки правил/клавиш и выбора
        `--monitor` по имени.
        """
        names = list(self.zones)
        seen = set(names)
        for zones in self.monitor_zones.values():
            for name in zones:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def zones_for(self, monitor_name=None):
        """Набор зон монитора: `[zones:<выход>]` или общий `[zones]`.

        Имя выхода сравнивается как есть, а при промахе — без учёта регистра
        (в конфиге легко написать `[zones:hdmi-1]`). Нет секции для монитора —
        возвращается набор по умолчанию, то есть прежнее поведение.
        """
        if monitor_name:
            zones = self.monitor_zones.get(monitor_name)
            if zones is not None:
                return zones
            for output, zones in self.monitor_zones.items():
                if output.casefold() == str(monitor_name).casefold():
                    return zones
        return self.zones

    def zone_spec(self, zone, monitor_name=None):
        """Описание зоны с учётом монитора: набор монитора, иначе общий `[zones]`.

        Правило/клавиша может ссылаться на зону, которой нет в наборе
        конкретного монитора; тогда при укладке на этом мониторе берётся зона из
        общего набора. Нет ни там, ни там — ошибка конфига, а не трейсбек.
        """
        spec = self.zones_for(monitor_name).get(zone)
        if spec is None:
            spec = self.zones.get(zone)
        if spec is None:
            where = (
                f"ни в наборе монитора '{monitor_name}', ни в [zones]"
                if monitor_name
                else "в [zones] и ни в одном наборе мониторов"
            )
            fail(f"zone-tiler: зоны '{zone}' нет {where}", EXIT_CONFIG)
        return spec

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

    def fractions(self, zone, monitor_name=None):
        """Зона в долях рабочей области: (x, y, w, h)."""
        spec = self.zone_spec(zone, monitor_name)
        if spec[0] == "rect":
            return spec[1:]
        _, c0, c1, r0, r1 = spec
        return (
            self.cols[c0],
            self.rows[r0],
            self.cols[c1] - self.cols[c0],
            self.rows[r1] - self.rows[r0],
        )

    def rect(self, zone, wa, monitor_name=None):
        """Пиксельная рамка зоны внутри рабочей области wa=(x,y,w,h).

        `monitor_name` — имя выхода: у монитора может быть свой набор зон
        (`[zones:<выход>]`), тогда берётся описание оттуда.
        """
        spec = self.zone_spec(zone, monitor_name)
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


def current_workspace():
    """Номер активного рабочего стола (`_NET_CURRENT_DESKTOP`) или None.

    Ноль — первый стол: так его нумерует EWMH. В ``[workspaces]`` ключи на
    единицу больше (как в интерфейсе Cinnamon), перевод — в
    :func:`grid_for_workspace`.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_CURRENT_DESKTOP"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"=\s*(-?\d+)", out)
    if not found:
        return None
    number = int(found.group(1))
    return number if number >= 0 else None


def window_workspace_for_zones(win):
    """Стол окна для выбора сетки: ``None`` у «липких» и неопознанных.

    ``_NET_WM_DESKTOP = 0xFFFFFFFF`` по EWMH значит «на всех столах». Решено:
    такие окна живут в сетке по умолчанию (``zones.conf``), а не в профиле
    конкретного стола, — иначе липкое окно меняло бы сетку вместе со столом.
    """
    workspace = window_workspace(win)
    if workspace is None or workspace >= 0xFFFFFFF0:
        return None
    return workspace


def grid_for_workspace(conf_path, workspace=None):
    """Сетка для рабочего стола: профиль из ``[workspaces]`` или сам конфиг.

    ``workspace`` — номер из EWMH (с нуля); ``None`` — взять активный стол
    (`current_workspace()`). Если стол не сопоставлен профилю, стол неизвестен,
    или профиля нет на диске, возвращается обычный ``Grid(conf_path)``: молчаливая
    деградация до общего набора зон лучше, чем отказ, — раскладку на столе без
    профиля пользователь всё равно ожидает получить.
    """
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как в редакторе

    if workspace == WORKSPACE_CONF:
        return Grid(conf_path)
    if workspace is None:
        workspace = current_workspace()
    if workspace is None:
        return Grid(conf_path)
    profile = zc.load_workspace_map(conf_path).get(workspace + 1)
    if profile is None:
        return Grid(conf_path)
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        return Grid(conf_path)
    if not os.path.exists(path):
        return Grid(conf_path)
    return Grid(path)


WORKSPACE_CURRENT = "current"
# Значение `--workspace conf`: брать сетку прямо из zones.conf, не глядя на
# `[workspaces]`. Нужно редактору: он рисует и правит сам `zones.conf`, поэтому
# «проверить зону» обязано считать ту же сетку, что на холсте, а не профиль стола.
WORKSPACE_CONF = "conf"


def workspace_spec(text):
    """Разобрать `--workspace`: ``current`` (и пусто) или номер стола EWMH.

    Возвращает ``WORKSPACE_CURRENT`` или ``int``; номер отрицательным быть не
    может, поэтому `-1` — ошибка аргументов. Разбор идёт в `argparse` (тип
    аргумента), чтобы неверное значение давало код 2 и внятное сообщение.
    """
    value = "" if text is None else str(text).strip()
    if not value or value.lower() == WORKSPACE_CURRENT:
        return WORKSPACE_CURRENT
    if value.lower() == WORKSPACE_CONF:
        return WORKSPACE_CONF
    if re.fullmatch(r"\d+", value):
        return int(value)
    raise argparse.ArgumentTypeError(
        f"неверный рабочий стол '{text}': нужен номер стола (0, 1, …), "
        f"{WORKSPACE_CURRENT} или {WORKSPACE_CONF}"
    )


def resolve_workspace(spec):
    """Номер стола для запроса сетки: ``current``/``None`` — активный стол.

    ``None`` у `current_workspace()` (нет xprop, нет `_NET_CURRENT_DESKTOP`)
    означает «стол неизвестен» — вызывающий получает общий `zones.conf`.
    """
    if spec is None or spec == WORKSPACE_CURRENT:
        return current_workspace()
    if spec == WORKSPACE_CONF:
        return WORKSPACE_CONF
    return int(spec)


def workspace_profile(conf_path, workspace):
    """Имя профиля стола из ``[workspaces]`` или ``None``, если его нет.

    Отвечает только на вопрос «какая сетка на этом столе»: сопоставления нет,
    имя профиля не санируется или файла профиля нет на диске — ``None``, то есть
    стол живёт в общем наборе зон. Та же логика деградации, что у
    :func:`grid_for_workspace`, — чтобы подпись в `list` не расходилась с тем,
    что команда реально показала.
    """
    if workspace is None or workspace == WORKSPACE_CONF:
        return None
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как в редакторе

    profile = zc.load_workspace_map(conf_path).get(workspace + 1)
    if profile is None:
        return None
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        return None
    return profile if os.path.exists(path) else None


def require_zone(grid, zone, workspace=None, profile=None):
    """Проверить, что зона есть в наборе сетки; иначе ошибка конфига (код 3).

    Зона ищется в объединении наборов показанной сетки (общий `[zones]` и
    `[zones:<выход>]`) — то есть ровно там, где её найдёт раскладка. Перечень
    доступных зон печатается целиком: по нему видно, что стол получил не тот
    профиль, а не только что имя набрано с ошибкой.
    """
    known = grid.all_zone_names()
    if zone in known:
        return
    if profile and workspace is not None:
        where = f" в сетке стола {workspace + 1} (профиль «{profile}»)"
    elif profile:
        where = f" в сетке профиля «{profile}»"
    else:
        where = " в [zones]"
    fail(
        f"zone-tiler: нет зоны '{zone}'{where}. Есть: " + ", ".join(sorted(known)),
        EXIT_CONFIG,
    )


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


def monitor_outputs():
    """[(имя выхода, (x, y, w, h))] — по одному на монитор.

    Имя (`eDP-1`, `HDMI-1`) стабильно между перезагрузками и переподключениями
    кабеля, поэтому именно оно адресует набор зон (`[zones:<выход>]`) и `--monitor`.
    Геометрия — прежняя GDK `get_workarea()` (в ней уже вычтена панель), а не
    геометрия xrandr: xrandr даёт полный экран выхода, без панелей.
    """
    return stitch_monitor_names(xrandr_monitors(), monitors())


# Строка `xrandr --listmonitors`: ` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`.
# Флаги `+*` относятся к состоянию монитора (`+` — авто, `*` — основной) и
# частью имени не являются.
_XRANDR_MONITOR_RE = re.compile(
    r"^\s*\d+:\s+[+*]*(?P<fallback>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    r"(?:\s+(?P<name>\S+))?\s*$"
)


def parse_xrandr_monitors(text):
    """[(имя, (x, y, w, h))] из вывода `xrandr --listmonitors`, в его порядке.

    Имя — последнее поле строки (` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`).
    Если имени в строке нет (старый xrandr), берётся поле после `N:` без флагов
    `+*`. Строки без геометрии (заголовок `Monitors:`, мусор) пропускаются.
    Чистая функция — под тесты.
    """
    out = []
    for raw in text.splitlines():
        m = _XRANDR_MONITOR_RE.match(raw)
        if not m:
            continue
        name = (m.group("name") or m.group("fallback") or "").lstrip("+*")
        if not name:
            continue
        rect = (
            int(m.group("x")),
            int(m.group("y")),
            int(m.group("w")),
            int(m.group("h")),
        )
        out.append((name, rect))
    return out


def xrandr_monitors():
    """Мониторы из `xrandr --listmonitors`; [] если утилиты нет или она упала."""
    try:
        text = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    return parse_xrandr_monitors(text)


def overlap_area(a, b):
    """Площадь пересечения прямоугольников a и b (0, если не пересекаются)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0


def stitch_monitor_names(outputs, areas):
    """Сшить имена выходов с рабочими областями: [(имя, область)].

    Совпало количество — имена по порядку: у GDK и xrandr один и тот же порядок
    мониторов. Иначе — по максимальному пересечению площадей (GDK может отдать
    одну область на несколько выходов или наоборот), а области без пересечения
    получают имя `monitor-<i>`. Без имён (нет xrandr) — только `monitor-<i>`, то
    есть прежнее поведение. Чистая функция — под тесты.
    """
    if not outputs:
        return [(f"monitor-{i}", wa) for i, wa in enumerate(areas)]
    if len(outputs) == len(areas):
        return [(outputs[i][0], areas[i]) for i in range(len(areas))]
    stitched = []
    used = set()
    for i, wa in enumerate(areas):
        best = None
        best_area = 0
        for j, (_, rect) in enumerate(outputs):
            if j in used:
                continue
            area = overlap_area(wa, rect)
            if area > best_area:
                best, best_area = j, area
        if best is None:
            stitched.append((f"monitor-{i}", wa))
        else:
            used.add(best)
            stitched.append((outputs[best][0], wa))
    return stitched


def resolve_monitor(spec, outputs=None):
    """(индекс, имя) монитора по `--monitor`: имя выхода приоритетнее номера.

    `spec` — имя (`HDMI-1`) или номер (`1`); `outputs` — результат
    `monitor_outputs()` (можно подменить в тестах). Неизвестное имя или
    несуществующий номер — ошибка аргументов с перечнем доступных мониторов.
    """
    outputs = monitor_outputs() if outputs is None else outputs
    text = "" if spec is None else str(spec).strip()
    for index, (name, _) in enumerate(outputs):
        if name == text:
            return index, name
    number = int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    if number is not None and 0 <= number < len(outputs):
        return number, outputs[number][0]
    available = ", ".join(f"{i} — {name}" for i, (name, _) in enumerate(outputs))
    fail(
        f"zone-tiler: монитора '{text}' нет. Доступны: {available or 'нет'}",
        EXIT_USAGE,
    )


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
# `WM_CHANGE_STATE` со значением IconicState (ICCCM) — просьба WM свернуть окно.
ICONIC_STATE = 3


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

    def iconify(self, win):
        """Свернуть окно: `WM_CHANGE_STATE` со значением `IconicState`."""
        self.send(win, "WM_CHANGE_STATE", [ICONIC_STATE, 0, 0, 0, 0])

    def activate(self, win):
        """Развернуть и поднять окно: `_NET_ACTIVE_WINDOW` от имени pager'а.

        `data.l[0] = 2` (source indication, pager) и нулевой timestamp: muffin
        принимает активацию от pager'а, а устаревший ненулевой timestamp может
        отклонить (focus-stealing prevention) — и окно осталось бы свёрнутым.
        """
        self.send(win, "_NET_ACTIVE_WINDOW", [SOURCE_PAGER, 0, 0, 0, 0])


# --------------------------------------------- сворачивание (режим «только эта зона»)


def window_iconic(win):
    """Свёрнуто ли окно сейчас — `WM_STATE: Iconic` или `_NET_WM_STATE_HIDDEN`.

    muffin при `WM_CHANGE_STATE` ставит оба признака (проверено дымом на
    Cinnamon/muffin), поэтому читается настоящий EWMH/ICCCM-признак, а не
    догадка по геометрии. Не прочитать свойство — окно считается видимым.
    """
    try:
        txt = subprocess.check_output(
            ["xprop", "-id", hex(win), "WM_STATE", "_NET_WM_STATE"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return "Iconic" in txt or "_NET_WM_STATE_HIDDEN" in txt


def iconify(window, x11=None, quiet=True):
    """Свернуть окно. Своё X-соединение закрывает, чужое не трогает.

    Единственный работающий путь: muffin игнорирует и `XMapWindow`, и
    `WM_CHANGE_STATE` с `NormalState`, а `_NET_WM_STATE_HIDDEN` принадлежит WM
    и снять его клиент не может (проверено дымом). `x11` подменяется в тестах;
    демон передаёт своё долгоживущее соединение.
    """
    own = x11 is None
    if own:
        x11 = X11()
    try:
        x11.iconify(window)
        if not quiet:
            print(f"окно 0x{window:x} -> свёрнуто")
    finally:
        if own:
            x11.close()


def deiconify(window, x11=None):
    """Развернуть окно; заодно поднимает и фокусирует.

    Поэтому зовётся по одному окну, а последним — то, на котором должно
    остаться внимание (цель режима). `x11` — как у :func:`iconify`.
    """
    own = x11 is None
    if own:
        x11 = X11()
    try:
        x11.activate(window)
    finally:
        if own:
            x11.close()


def focus_candidates(entries, target, workspace, wa):
    """Окна, которые сворачивает режим «только эта зона». Чистая функция.

    `entries` — `(окно, стол, рабочая область)` в порядке `_NET_CLIENT_LIST`
    (демон отдаёт то же, что хранит в `win_workspace`/`win_monitor`). Берутся
    окна **той же рабочей области** и того же стола, кроме цели; липкое окно
    (`workspace is None`) видно везде и потому подходит. Активный стол не
    прочитан (`workspace is None`) — подходят только липкие окна: сворачивать
    вслепую окна чужих столов хуже, чем не сворачивать ничего.

    Исключения (`window_excluded`) и уже свёрнутые отсеивает вызывающий: у него
    есть метаданные окна и `window_iconic`, а у чистой функции их нет.
    """
    out = []
    for win, entry_workspace, entry_wa in entries:
        if win == target or entry_wa != wa:
            continue
        if workspace is None:
            if entry_workspace is not None:
                continue
        elif entry_workspace is not None and entry_workspace != workspace:
            continue
        out.append(win)
    return out


def focus_entries(client_ids, monitors_):
    """`(окно, стол, рабочая область)` для каждого клиента — вход `focus_candidates`.

    Читает `_NET_WM_DESKTOP` и центр окна (`xwininfo`). Монитор не прочитался —
    окно попадает с `None` и кандидатом не станет: сворачивать окно, которого
    не видно, вслепую хуже, чем не сворачивать. Читает X, но решений не
    принимает — отбор делает чистая `focus_candidates`.
    """
    return [
        (win, window_workspace_for_zones(win), window_monitor(win, monitors_))
        for win in client_ids
    ]


# --------------------------------------------- раскладка и история (без X)

# Сколько последних операций хранить в истории раскладок.
HISTORY_LIMIT = 20


def movable_windows(client_ids, metas, ignore=None):
    """id окон, которые можно двигать, в порядке `_NET_CLIENT_LIST`.

    Те же фильтры, что у демона: нужен непустой `WM_CLASS` и окно не под
    исключением — `window_excluded` (служебный тип или селектор `[ignore]`).
    `override_redirect` окон в списке клиентов EWMH нет, поэтому там такой
    проверки не требуется. Чистая функция — под тесты.
    """
    out = []
    for win in client_ids:
        meta = metas.get(win)
        if meta is None:
            continue
        if not meta.wm_class:
            continue
        if window_excluded(meta.wm_class, meta.wm_title, meta.window_type, ignore):
            continue
        out.append(win)
    return out


def choose_assignments(client_ids, metas, zones, rules, by="auto", ignore=None):
    """Сопоставить окна зонам. Чистая функция — под тесты.

    client_ids — id в порядке `_NET_CLIENT_LIST` (снизу вверх);
    metas — {id: WindowMeta};
    zones — имена зон в порядке файла (важен для order/auto);
    rules — [(селектор, зона)] из `[rules]`;
    ignore — селекторы `[ignore]` (окна под ними не двигаются);
    by — режим:
      `rules` — только окна со сработавшим правилом, каждое в свою зону;
      `order` — все подвижные окна по порядку в зоны по порядку; лишним окнам
                (сверх числа зон) достаётся последняя зона (там они — стопкой);
      `auto`  — сначала правила, затем свободные (никем не занятые по правилу)
                зоны по порядку для окон без правила; лишние не трогаются.

    Возвращает список (id, зона) в порядке укладки; неназначенные окна в него
    не попадают.
    """
    windows = movable_windows(client_ids, metas, ignore)
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


# --------------------------------------------------- режим «только эта зона»

# Версия `focus.json` — состояния режима для ветки без демона.
FOCUS_STATE_VERSION = 1


def focus_path():
    """Путь состояния режима «только эта зона» (`$XDG_STATE_HOME/gridsnap/focus.json`).

    Файл нужен только ветке без демона: у запущенного демона режим живёт в
    памяти. Это состояние (переживает выход процесса), а не конфиг и не кэш.
    """
    return os.path.join(state_home(), "gridsnap", "focus.json")


def load_focus_state(path=None):
    """Состояние режима из файла; `{}` — режим не активен.

    Битый файл, чужая версия и неполная запись читаются как «режим не
    активен»: испорченное состояние не должно ронять раскладку. Переоткрытое
    окно (новый XID) остаётся в записи и отсеивается при разворачивании — по
    `wm_class`, как это делает память демона об укладках.
    """
    path = path or focus_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != FOCUS_STATE_VERSION:
        return {}
    if not isinstance(data.get("windows"), list):
        return {}
    return data


def save_focus_state(state, path=None):
    """Записать состояние режима атомарно (`.tmp` + `os.replace`). Райзит OSError."""
    path = path or focus_path()
    payload = dict(state)
    payload["version"] = FOCUS_STATE_VERSION
    directory = os.path.dirname(path)
    tmp = f"{path}.tmp"
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_focus_state(path=None):
    """Убрать файл состояния режима (выход из режима без демона). Идемпотентно."""
    path = path or focus_path()
    with contextlib.suppress(OSError):
        os.unlink(path)


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


def cmd_list(grid, args, profile=None):
    """Показать зоны сетки: `[zones:<выход>]` монитора или общий `[zones]`.

    `profile` — имя профиля стола, которым показана сетка (`None` — общий
    `zones.conf`); оно дописывается в первую строку, чтобы было видно, почему
    набор зон именно такой. Начало первой строки неизменно: его разбирает
    апплет Cinnamon (регулярка `рабочая область монитора N: ...`).
    """
    outputs = monitor_outputs()
    index, output = resolve_monitor(monitor_arg(args), outputs)
    wa = outputs[index][1]
    note = "" if output == f"monitor-{index}" else f"  (выход {output})"
    if profile:
        note += f"  (профиль «{profile}»)"
    print(f"рабочая область монитора {index}: {wa[0]},{wa[1]} {wa[2]}x{wa[3]}{note}")
    zones = grid.zones_for(output)
    width = max(len(z) for z in zones)
    for name in sorted(zones):
        x, y, w, h = grid.rect(name, wa, monitor_name=output)
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
    outputs = monitor_outputs()
    index, output = resolve_monitor(monitor_arg(args), outputs)
    wa = outputs[index][1]
    rect = grid.rect(args.zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(f"{args.zone}: x={x} y={y} {w}x{h} (монитор {index}: {output})")
        return
    window = args.window or active_window()
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                f"zone-tiler: окно 0x{window:x} под исключением "
                "([ignore]/служебный тип) — не двигаю",
                file=sys.stderr,
            )
        return
    before = None
    try:
        before = snapshot_window(window, zone=args.zone)
    except Exception:
        before = None  # геометрию не прочитать — без записи в историю
    apply_zone(window, rect, label=args.zone, verify=args.verify, quiet=args.quiet)
    if before is not None:
        remember("apply", [before], quiet=args.quiet)


# -------------------------------------------------- режим «только эта зона» (CLI)

# Сколько ждать ответа демона, секунд (тот же порядок, что у `gridsnap-ctl`).
DAEMON_TIMEOUT = 5.0


def daemon_request(request, timeout=DAEMON_TIMEOUT):
    """Отправить одну команду живому демону; `None` — демона нет.

    `zone_ipc` подключается лениво (как `picker_nav`): без демона раскладчику
    этот модуль не нужен. Мёртвый сокет/отсутствие демона (`OSError`) — это
    `None`: вызывающий сделает то же самое разово. Демон жив, но не ответил за
    `timeout`, или ответил непонятно — это уже не «демона нет», и второй хозяин
    режима не заводится: отказ с внятным сообщением.
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_ipc  # noqa: PLC0415 — ленивый импорт, рядом с раскладчиком

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(zone_ipc.default_socket_path())
    except OSError:
        conn.close()
        return None
    try:
        conn.sendall(zone_ipc.encode_message(request))
        return zone_ipc.read_response(conn)
    except TimeoutError:
        fail(
            f"zone-tiler: демон не ответил за {timeout:g} с — режим не переключаю "
            "(работающего демона нельзя обойти снаружи)",
            EXIT_FAIL,
        )
    except zone_ipc.ProtocolError as exc:
        fail(f"zone-tiler: демон ответил непонятно: {exc}", EXIT_FAIL)
    finally:
        conn.close()


def cmd_focus(grid, args):
    """Режим «только эта зона»: окно в зону, остальные окна монитора — свёрнуты.

    Вход и выход — **одна** команда (toggle): повторный `focus` при активном
    режиме разворачивает окна, свёрнутые режимом, и больше ничего; свёрнутое
    вручную остаётся свёрнутым. `--off` — явный выход для скриптов и второй
    клавиши. Зона необязательна: без неё окно остаётся где стоит, и режим
    значит «только это окно» (это форма клавиши `[focus]` без `zone`). Область —
    рабочий стол и монитор целевого окна: другие экраны и столы не трогаются, а
    исключения (`window_excluded`) и диалоги — тем более.

    С живым демоном команда **делегирует ему**: у режима должен быть один
    хозяин, и только демон умеет сворачивать окна, появляющиеся после входа.
    Без демона работает разово, а список свёрнутых держит в `focus.json`
    (см. `focus_path`) — новые окна в таком режиме не отслеживаются.
    """
    request = {"cmd": "focus"}
    if args.off:
        request["off"] = True
    elif args.zone:
        request["zone"] = args.zone
    if args.window is not None:
        request["window"] = args.window

    state = load_focus_state()
    if args.dry_run:
        _focus_dry_run(grid, args, state)
        return

    # Живой демон — единственный хозяин режима: он видит `_NET_CLIENT_LIST`
    # и умеет довернуть окна, появившиеся после входа. Без демона — разово.
    response = daemon_request(request)
    if response is not None:
        if not response.get("ok"):
            fail(
                f"zone-tiler: {response.get('error', 'демон отклонил focus')}",
                EXIT_FAIL,
            )
        _print_focus_response(response, quiet=args.quiet)
        return

    if request.get("off") or state:
        restored = _focus_restore(state)
        clear_focus_state()
        if not args.quiet:
            print(f"режим «только эта зона» выключен: развёрнуто окон — {len(restored)}")
        return
    _focus_enter(grid, args)


def _focus_dry_run(grid, args, state):
    """Показать, что сделал бы `focus`, ничего не двигая и не сворачивая."""
    if args.off or state:
        windows = state.get("windows") or []
        print(f"режим «только эта зона»: выключился бы, развернулось бы окон — {len(windows)}")
        return
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    wa = window_monitor(window, monitors())
    if wa is None:
        fail(f"zone-tiler: не удалось определить монитор окна 0x{window:x}", EXIT_FAIL)
    candidates = focus_candidates(
        focus_entries(read_client_list() or [], monitors()), window,
        current_workspace(), wa,
    )
    todo = [
        win
        for win in candidates
        if not window_is_excluded(win, grid.ignore) and not window_iconic(win)
    ]
    where = "остаётся на месте" if not args.zone else "зона '{}'".format(args.zone)
    if args.zone:
        x, y, w, h = grid.rect(args.zone, wa)
        where += f" x={x} y={y} {w}x{h}"
    print(
        f"режим «только эта зона»: окно 0x{window:x} -> {where}, "
        f"свернулось бы окон — {len(todo)}"
    )


def _focus_enter(grid, args):
    """Разовый вход в режим без демона: цель в зону (если задана), остальные — свёрнуты."""
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                f"zone-tiler: окно 0x{window:x} под исключением "
                "([ignore]/служебный тип) — режим не включаю",
                file=sys.stderr,
            )
        return
    workspace = current_workspace()
    if workspace is None:
        fail("zone-tiler: не удалось определить активный рабочий стол", EXIT_FAIL)
    monitors_ = monitors()
    wa = window_monitor(window, monitors_)
    if wa is None:
        fail(f"zone-tiler: не удалось определить монитор окна 0x{window:x}", EXIT_FAIL)
    # Зона проверяется на этой рабочей области — до того, как что-то двинулось.
    rect = grid.rect(args.zone, wa) if args.zone else None
    entries = focus_entries(read_client_list() or [], monitors_)
    todo = [
        win
        for win in focus_candidates(entries, window, workspace, wa)
        if not window_is_excluded(win, grid.ignore) and not window_iconic(win)
    ]

    before = None
    if rect is not None:
        try:
            before = snapshot_window(window, zone=args.zone)
        except Exception:
            before = None  # геометрию не прочитать — без записи в историю

    # Одно X-соединение на всю операцию: `iconify` чужое соединение не закрывает.
    x11 = X11()
    minimized = []
    try:
        if rect is not None:
            apply_zone(window, rect, label=args.zone, x11=x11, quiet=True)
        for win in todo:
            iconify(win, x11=x11, quiet=True)
            minimized.append(
                {"window": hex(win), "wm_class": window_class_title(win)[0]}
            )
    finally:
        x11.close()
    if before is not None:
        remember("apply", [before], quiet=args.quiet)

    save_focus_state(
        {
            "zone": args.zone,
            "workspace": workspace,
            "monitor": list(wa),
            "target": {"window": hex(window), "wm_class": window_class_title(window)[0]},
            "windows": minimized,
        }
    )
    if not args.quiet:
        where = f" -> зона '{args.zone}'" if args.zone else ""
        print(
            f"режим «только эта зона»: окно 0x{window:x}{where}, "
            f"свёрнуто окон — {len(minimized)}"
        )


def _focus_restore(state, x11=None):
    """Развернуть окна из записи `focus.json`; чужое не трогаем.

    Окно, которого больше нет в `_NET_CLIENT_LIST`, и окно с другим `WM_CLASS`
    (XID переиспользован другой программой) пропускаются: развернуть чужое окно
    хуже, чем не развернуть своё. Последней активируется цель — она и должна
    остаться в фокусе. Записей нет — и X не нужен.
    """
    records = [
        record
        for record in (state.get("windows") or [])
        if _focus_window_id(record) is not None
    ]
    target = state.get("target") if isinstance(state.get("target"), dict) else None
    target_id = _focus_window_id(target) if target else None
    if not records and target_id is None:
        return []

    alive = read_client_list()
    alive = set(alive) if alive is not None else None
    own = x11 is None
    if own:
        x11 = X11()
    restored = []
    try:
        for record in records:
            win = _focus_window_id(record)
            if win is None or not _focus_alive(win, record, alive):
                continue
            deiconify(win, x11=x11)
            restored.append(win)
        if target_id is not None and _focus_alive(target_id, target, alive):
            deiconify(target_id, x11=x11)
    finally:
        if own:
            x11.close()
    return restored


def _focus_window_id(record):
    """Идентификатор окна из записи `focus.json` (число или `0x…`); иначе None."""
    value = record.get("window") if isinstance(record, dict) else record
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _focus_alive(win, record, alive):
    """Наше ли это ещё окно: в списке клиентов и с тем же `WM_CLASS`."""
    if alive is not None and win not in alive:
        return False
    expected = record.get("wm_class") if isinstance(record, dict) else ""
    if not expected:
        return True
    return window_class_title(win)[0] == expected


def _print_focus_response(response, quiet=False):
    """Напечатать ответ демона на `focus` по-человечески (`--quiet` — молча)."""
    if quiet:
        return
    if response.get("off"):
        count = len(response.get("restored") or [])
        print(f"режим «только эта зона» выключен: развёрнуто окон — {count}")
        return
    count = len(response.get("minimized") or [])
    print(
        f"режим «только эта зона»: окно {response.get('window')} -> зона "
        f"'{response.get('zone')}', свёрнуто окон — {count}"
    )


def monitor_arg(args):
    """`--monitor` из аргументов: не задан — 0, как было у list/apply/stack.

    У `move` «не задан» значит другое: монитор окна (см. `cmd_move`), поэтому
    значение по умолчанию осталось `None`, а не `"0"`.
    """
    return "0" if args.monitor is None else args.monitor


def picker_nav():
    """Модуль `zone_picker_nav`: правила выбора соседа живут только там."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_picker_nav  # noqa: PLC0415 — ленивый импорт, рядом с раскладчиком

    return zone_picker_nav


def nearest_zone(zones, fx, fy):
    """Индекс зоны с ближайшим к точке `(fx, fy)` центром; `None` — зон нет.

    Нужна для окна, которое свободно плавает мимо всех зон: без неё `move` из
    такого окна молча ничего бы не делал («зоны под центром нет»). Чистая
    функция — под тесты.
    """
    nav = picker_nav()
    best, best_dist = None, None
    for index, zone in enumerate(zones):
        cx, cy = nav.center(zone)
        dist = (cx - fx) ** 2 + (cy - fy) ** 2
        if best_dist is None or dist < best_dist:
            best, best_dist = index, dist
    return best


def move_zone_index(zones, direction, fx, fy):
    """`(откуда, куда)` для `move`; равные индексы — соседа нет.

    `zones` — зоны набора в порядке файла как кортежи `(x, y, w, h)` в долях
    рабочей области, `(fx, fy)` — центр окна в тех же долях. Зона окна — та,
    в которой его центр (`zone_picker_nav.zone_at`), а если центр мимо всех
    зон — ближайшая по центру (`nearest_zone`). Сосед выбирается чужим правилом
    `zone_picker_nav.move_selection`: пикер и `move` обязаны вести себя
    одинаково, а не двумя похожими реализациями. Чистая функция — под тесты.
    """
    nav = picker_nav()
    if not zones:
        return None, None
    index = nav.zone_at(zones, fx, fy)
    if index is None:
        index = nearest_zone(zones, fx, fy)
    return index, nav.move_selection(zones, index, direction)


def cmd_move(grid, args):
    """Перебросить окно в соседнюю зону по направлению (left/right/up/down).

    Зона окна — по центру его рамки в долях рабочей области **своего** монитора
    (набор `[zones:<выход>]`, иначе общий `[zones]`); сосед — тот же, что
    подсветил бы пикер (`move_zone_index`). Окно вне зон — ближайшая по центру
    зона. На краю сетки соседа нет: это не ошибка — сообщаем в stderr и
    выходим с кодом 0. Удачный переезд (не `--dry-run`) пишется в историю
    операцией `move`, поэтому `undo` возвращает окно на прежнее место.
    """
    nav = picker_nav()
    direction = (args.zone or "").strip().lower()
    if direction not in nav.DIRECTIONS:
        fail(
            "zone-tiler: move — укажите направление: " + " | ".join(nav.DIRECTIONS),
            EXIT_USAGE,
        )
    outputs = monitor_outputs()
    window = args.window or active_window()
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                f"zone-tiler: окно 0x{window:x} под исключением "
                "([ignore]/служебный тип) — не двигаю",
                file=sys.stderr,
            )
        return EXIT_OK
    extents = frame_extents(window)
    try:
        frame = read_frame(window, extents)
    except Exception as exc:
        fail(f"zone-tiler: не удалось прочитать рамку окна 0x{window:x}: {exc}", EXIT_FAIL)
    if args.monitor is None:
        # Монитор окна — по центру его рамки (в рабочей области уже вычтена панель).
        areas = [area for _, area in outputs]
        wa = monitor_for_point(areas, (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2))
        index = areas.index(wa)
        output = outputs[index][0]
    else:
        index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[index][1]
    names = list(grid.zones_for(output))
    zones = [grid.fractions(name, monitor_name=output) for name in names]
    wx, wy, ww, wh = wa
    fx = (frame[0] + frame[2] / 2.0 - wx) / ww
    fy = (frame[1] + frame[3] / 2.0 - wy) / wh
    here, there = move_zone_index(zones, direction, fx, fy)
    if here is None:
        fail(f"zone-tiler: у монитора {index} ({output}) нет ни одной зоны", EXIT_CONFIG)
    if there == here:
        if not args.quiet:
            print(
                f"zone-tiler: у зоны '{names[here]}' соседа {direction} нет — "
                "окно не двигаю",
                file=sys.stderr,
            )
        return EXIT_OK
    zone = names[there]
    rect = grid.rect(zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(
            f"окно 0x{window:x}: зона '{names[here]}' -> '{zone}': "
            f"x={x} y={y} {w}x{h} (монитор {index}: {output})"
        )
        return EXIT_OK
    before = None
    try:
        before = snapshot_window(window, zone=zone)
    except Exception:
        before = None  # геометрию не прочитать — без записи в историю
    apply_zone(window, rect, label=zone, verify=args.verify, quiet=args.quiet)
    if before is not None:
        remember("move", [before], quiet=args.quiet)
    return EXIT_OK


def group_windows_by_monitor(client_ids, outputs):
    """[(имя, область, [id окон])] — окна по мониторам, порядок сохранён.

    Монитор окна — по его центру (та же `window_monitor`, что у демона). Окна
    без центра/монитора в результат не попадают. Нужна, чтобы применять зоны
    своего монитора (`[zones:<выход>]`) в пределах группы.
    """
    areas = [wa for _, wa in outputs]
    groups = collections.OrderedDict()
    for win in client_ids:
        wa = window_monitor(win, areas)
        if wa is None:
            continue
        groups.setdefault(wa, []).append(win)
    name_of = {wa: name for name, wa in outputs}
    return [(name_of.get(wa), wa, wins) for wa, wins in groups.items()]


def cmd_arrange(grid, args):
    """Разложить клиентские окна `_NET_CLIENT_LIST` по зонам.

    Режим `--by` (auto/rules/order) выбирает окна чистой `choose_assignments`;
    окна группируются по монитору, и в каждой группе работает набор зон этого
    монитора (`[zones:<выход>]`) или общий `[zones]`. Несколько окон,
    попавших в одну зону одного монитора, ложатся туда стопкой со сдвигом
    `--offset`. Прежние рамки записываются в историю для `undo`.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail("zone-tiler: не удалось прочитать _NET_CLIENT_LIST (нужен DISPLAY и xprop)", EXIT_FAIL)
    metas = {win: read_window_meta(win) for win in client_ids}
    outputs = monitor_outputs()
    groups = group_windows_by_monitor(client_ids, outputs)
    assignments = []
    for output, wa, wins in groups:
        zones = list(grid.zones_for(output))
        for win, zone in choose_assignments(
            wins, metas, zones, grid.rules, args.by, ignore=grid.ignore
        ):
            assignments.append((win, zone, output, wa))
    if not groups:
        if not args.quiet:
            print("zone-tiler: не удалось определить монитор ни для одного окна", file=sys.stderr)
        return
    if not assignments:
        if not args.quiet:
            print("zone-tiler: нет окон, подходящих под раскладку")
        return
    x11 = None if args.dry_run else X11()
    counts = {}
    records = []
    try:
        for win, zone, output, wa in assignments:
            key = (wa, zone)
            index = counts.get(key, 0)
            counts[key] = index + 1
            base = grid.rect(zone, wa, monitor_name=output)
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
    windows = movable_windows(client_ids, metas, grid.ignore)
    if not windows:
        if not args.quiet:
            print("zone-tiler: нет окон для стопки")
        return
    outputs = monitor_outputs()
    mon_index, output = resolve_monitor(monitor_arg(args), outputs)
    wa = outputs[mon_index][1]
    base = grid.rect(args.zone, wa, monitor_name=output)
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
            current_class, current_title = window_class_title(win)
            if wm_class and current_class and current_class != wm_class:
                # id мог переиспользоваться другим приложением после выхода старого
                warn(f"окно 0x{win:x}: класс не совпал ({current_class} ≠ {wm_class}) — пропускаю")
                continue
            if wm_class and not current_class:
                warn(f"окно 0x{win:x}: уже не существует — пропускаю")
                continue
            if window_excluded(current_class, current_title, window_types(win), grid.ignore):
                warn(f"окно 0x{win:x}: под исключением ([ignore]/служебный тип) — пропускаю")
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


def cmd_keys(grid, move=False, conf=None):
    """Печатает пары 'зона<TAB>сочетание' из секции [keys] (для setup-hotkeys.py).

    `[keys]` общая для всех мониторов, а зона может быть описана только в
    секции конкретного монитора — поэтому проверка идёт по объединению наборов.

    `move=True` — вместо зон печатаются направления перемещения
    (`направление<TAB>сочетание`) из необязательной секции `[move]`: источник
    для `setup-hotkeys.py`, который регистрирует `move <направление>`. Разбор
    секции и умолчания — в `zone_config.load_move_keys` (неизвестный ключ —
    ошибка конфига, код 3).
    """
    if move:
        import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как у редактора

        keys = zc.load_move_keys(conf or grid.path)
        for direction in zc.MOVE_DIRECTIONS:
            print(f"{direction}\t{keys[direction]}")
        return
    if not grid.conf.has_section("keys"):
        return
    known = set(grid.all_zone_names())
    for zone, binding in grid.conf.items("keys"):
        zone, binding = zone.strip(), binding.strip()
        if zone not in known:
            print(
                f"zone-tiler: в [keys] зона '{zone}' отсутствует в [zones] "
                f"и в [zones:<выход>]",
                file=sys.stderr,
            )
            continue
        if binding:
            print(f"{zone}\t{binding}")


# ---------------------------------------------------------------- профили

PROFILE_ACTIONS = ("list", "show", "save", "use", "rm", "export", "import")


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


def _profile_export(zc, args, name, conf):
    """``profile export``: JSON-профиля в stdout или в файл."""
    target = (args.target or "").strip()
    if target and args.json:
        fail("zone-tiler: profile export — или --json, или файл, не оба сразу", EXIT_USAGE)
    data = zc.profile_to_dict(name, conf)
    if not target:
        sys.stdout.write(zc.dumps_profile(data))
        return
    path = zc.write_profile_json(data, target)
    print(
        f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» выгружен: "
        f"{path} ({zc.profile_zone_total(data)} зон)"
    )


def _profile_import(zc, args, path, conf):
    """``profile import``: JSON-файл → профиль ``templates/<имя>.conf``."""
    data = zc.read_profile_json(path)
    written = zc.profile_from_dict(data, (args.as_name or "").strip() or None, conf)
    name = os.path.basename(written)[: -len(zc.PROFILE_EXT)]
    print(
        f"zone-tiler: профиль «{name}» импортирован: {written} "
        f"({zc.profile_zone_total(data)} зон)"
    )


def cmd_profile(args):
    """Профили раскладок: list/show/save/use/rm/export/import (zone_config лениво)."""
    import zone_config as zc

    action = (args.zone or "").strip()
    if action not in PROFILE_ACTIONS:
        fail(
            "zone-tiler: profile — укажите действие: list | show <имя> | save <имя> | "
            "use <имя> | rm <имя> | export <имя> [файл | --json] | "
            "import <файл> [--name <имя>]",
            EXIT_USAGE,
        )
    conf = args.conf
    if action == "list":
        for name in zc.list_profiles(conf):
            print(name)
        return

    name = (args.name or "").strip()
    if not name:
        what = "файл JSON-профиля" if action == "import" else "имя профиля"
        fail(f"zone-tiler: profile {action} — укажите {what}", EXIT_USAGE)

    try:
        if action == "show":
            sys.stdout.write(zc.profile_text(name, conf))
        elif action == "save":
            zones = zc.load_zones(conf)
            extra = zc.load_extra_sections(conf, sections=["rules", "ignore"])
            path = zc.save_profile(
                name, zones, extra.get("rules", []), conf, ignore=extra.get("ignore")
            )
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» сохранён: {path}")
        elif action == "rm":
            zc.remove_profile(name, conf)
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» удалён")
        elif action == "export":
            _profile_export(zc, args, name, conf)
        elif action == "import":
            _profile_import(zc, args, name, conf)
        else:  # use
            zones = zc.apply_profile(name, conf)
            print(f"zone-tiler: профиль «{zc.sanitize_profile_name(name)}» применён: {len(zones)} зон")
            if not args.no_hotkeys:
                _run_hotkeys(conf, quiet=args.quiet)
    except zc.ProfileJsonError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
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
        choices=[
            "list",
            "apply",
            "move",
            "focus",
            "keys",
            "arrange",
            "stack",
            "undo",
            "profile",
        ],
        help=(
            "list/apply/keys — как раньше; move — перебросить окно в соседнюю зону; "
            "focus — режим «только эта зона» (окно в зону, остальные свёрнуты; "
            "повторно — выход); arrange — разложить окна по зонам; stack — собрать "
            "окна в зону стопкой; undo — вернуть рамки из истории; profile — свои "
            "профили раскладок (зоны + правила + клавиши)"
        ),
    )
    ap.add_argument(
        "zone",
        nargs="?",
        help="имя зоны для apply/stack, направление для move или действие profile",
    )
    ap.add_argument("name", nargs="?", help="имя профиля или файл JSON для profile import")
    ap.add_argument(
        "target",
        nargs="?",
        help="profile export: файл для JSON (без него и без --json — stdout)",
    )
    ap.add_argument(
        "--monitor",
        default=None,
        help=(
            "монитор для list/apply/stack/move: имя выхода (HDMI-1) или номер "
            "(по умолчанию 0; у move — монитор окна)"
        ),
    )
    ap.add_argument(
        "--workspace",
        metavar="N|current",
        default=None,
        type=workspace_spec,
        help=(
            "рабочий стол для list/apply/move/arrange/stack: номер (EWMH, с нуля), "
            "current (сетка активного стола — по умолчанию) или conf "
            "(всегда сетка zones.conf, без профилей столов)"
        ),
    )
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
    ap.add_argument(
        "--json",
        action="store_true",
        help="profile export: печатать JSON в stdout, а не писать файл",
    )
    ap.add_argument(
        "--name",
        dest="as_name",
        metavar="ИМЯ",
        help="profile import: имя профиля (по умолчанию — из JSON)",
    )
    ap.add_argument("--dry-run", action="store_true", help="только посчитать рамку")
    ap.add_argument(
        "--off",
        action="store_true",
        help="focus: явно выйти из режима «только эта зона» (развернуть свёрнутое режимом)",
    )
    ap.add_argument(
        "--move",
        action="store_true",
        help="keys: сочетания перемещения в соседнюю зону из секции [move]",
    )
    ap.add_argument("--verify", action="store_true", help="перечитать реальную геометрию")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--version", action="version", version=f"zone-tiler {VERSION}")
    args = ap.parse_args(argv)

    if args.command == "profile":
        cmd_profile(args)
        return EXIT_OK
    # `[keys]` общая на файл, а undo возвращает прежние рамки: профиль стола им
    # не указ, поэтому читают сам zones.conf, а не сетку рабочего стола.
    if args.command == "keys":
        cmd_keys(Grid(args.conf), move=args.move, conf=args.conf)
        return EXIT_OK
    if args.command == "undo":
        cmd_undo(Grid(args.conf), args)
        return EXIT_OK

    # list/apply/arrange/stack работают сеткой своего стола: `--workspace N` или
    # активный стол по умолчанию; сетка берётся через grid_for_workspace, поэтому
    # стол без профиля молча живёт в общем [zones].
    workspace = resolve_workspace(args.workspace)
    profile = workspace_profile(args.conf, workspace)
    grid = grid_for_workspace(args.conf, workspace)
    if args.command == "list":
        cmd_list(grid, args, profile=profile)
        return EXIT_OK
    if args.command == "arrange":
        cmd_arrange(grid, args)
        return EXIT_OK
    if args.command == "move":
        cmd_move(grid, args)
        return EXIT_OK
    if args.command == "focus":
        cmd_focus(grid, args)
        return EXIT_OK
    if not args.zone:
        fail("zone-tiler: укажите зону, например: zone-tiler.py apply left", EXIT_USAGE)
    # Зона ищется в наборе показанной сетки (монитор + общий [zones]); нет её
    # там — это ошибка конфига: конфиг и профиль стола разошлись.
    require_zone(grid, args.zone, workspace=workspace, profile=profile)
    if args.command == "stack":
        cmd_stack(grid, args)
        return EXIT_OK
    cmd_apply(grid, args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

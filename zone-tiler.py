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

Вкладки в зоне — необязательная секция `[tabs]`: `key` — сочетание «следующее
окно зоны» (`tabs-next`), `height` — высота полосы вкладок в пикселях (по
умолчанию 22). Несколько окон в одной зоне стоят на одной рамке (зона минус
полоса), видно верхнее; полосу рисует отдельный процесс `zone-tabstrip.py`.

  [tabs]
  key    = <Super><Alt>g
  height = 22

Каскада больше нет: `--offset` и сдвиг стопки удалены, вызов с `--offset` —
ошибка аргументов (код 2).

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
  zone-tiler.py stack main                # собрать окна в зону main вкладками
  zone-tiler.py tabs-next                 # поднять следующее окно зоны (клавиша)
  zone-tiler.py action mirror              # выполнить действие из [action:<имя>]
  zone-tiler.py action term-main --dry-run # что действие сделало бы, ничего не делая
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
  zone-tiler.py profile use-monitors      # снимок профилей экранов из [monitors]
  zone-tiler.py profile use-monitors --dry-run   # показать конфиг, ничего не писать
  zone-tiler.py profile show work
  zone-tiler.py profile rm work
  zone-tiler.py profile export work              # JSON в stdout (для пайпов)
  zone-tiler.py profile export work work.json    # JSON в файл — поделиться раскладкой
  zone-tiler.py profile import work.json         # JSON → templates/<имя>.conf
  zone-tiler.py profile import work.json --name home  # то же, но под другим именем

Своя сетка на экране — привязка в секции `[monitors]` (`HDMI-1 = work`):
`profile use-monitors` собирает из профилей экранов снимок (`[zones]` первого
экрана плюс `[zones:<выход>]` остальных, слияние `[rules]`/`[ignore]`/`[keys]`),
а `apply`/`move`/`arrange`/`stack` ищут зону в профиле экрана (у `move`,
`arrange` — экрана окна, у `apply`/`stack` — того, что задан `--monitor`),
потом стола, потом в `zones.conf`.

JSON-профиль — переносимый обмен между машинами (``gridsnap_profile``: 1);
основным остаётся ``.conf``, JSON только для выгрузки и загрузки.

Действие — запись `[action:<имя>]` в `zones.conf`: `run = builtin:<имя>` либо
своя команда, необязательные `zone` (зона-предмет) и `key` (сочетание, его
ставит `setup-hotkeys.py`). Действие получает окно и зону: переменными
`GRIDSNAP_*` и подстановками `{zone}`/`{window}`/`{x}`… Своя команда
запускается без оболочки и без ожидания; встроенные — `minimize-others`,
`show-window`, `mirror`, `terminal` (подробности — в --help команды `action`).

История раскладок — JSON в XDG-состоянии (`~/.local/state/gridsnap/history.json`),
её читает и пишет каждая команда отдельным процессом (подробности — в --help и в
`docs/specs/zone-tiler.md`).
"""

import argparse
import collections
import configparser
import contextlib
import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

from zone_i18n import _  # строки интерфейса; без каталога — исходный текст
import window_backend  # оконный слой: xprop/xwininfo/xrandr и libX11 — в одном месте
import zone_units  # перевод логических пикселей GTK в физические X — в одном месте

VERSION = "0.2.0"

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


def window_class_title(win):
    """WM_CLASS (класс, вторая строка) и заголовок окна — см. `window_backend`."""
    return window_backend.window_class_title(win)


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
    """Набор `_NET_WM_WINDOW_TYPE` окна (пустой при ошибке) — см. `window_backend`."""
    return window_backend.window_types(win)


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
    """Клиентские окна `_NET_CLIENT_LIST` или None — см. `window_backend`."""
    return window_backend.read_client_list()


# ---------------------------------------------------------------- конфиг сетки


class Grid:
    def __init__(self, path=CONF):
        cp = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            delimiters=("=",),  # ключ `re:<regex>` содержит ':', разделитель только '='
        )
        try:
            read = cp.read(path, encoding="utf-8")
        except configparser.Error as exc:
            # Повтор секции или ключа: код 3 с именем файла, а не трассировка.
            fail(
                _(
                    "zone-tiler: не разобрать конфиг {path}: {error}"
                ).format(path=path, error=exc),
                EXIT_CONFIG,
            )
        if not read:
            fail(_("zone-tiler: не найден конфиг {path}").format(path=path), EXIT_CONFIG)
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
                fail(
                    _("zone-tiler: пустое имя монитора в секции [{section}]").format(section=section),
                    EXIT_CONFIG,
                )
            zones = {}
            for name, spec in cp.items(section):
                zones[name.strip()] = self._parse_zone(name, spec)
            if not zones:
                fail(
                    _("zone-tiler: в секции [{section}] нет ни одной зоны").format(section=section),
                    EXIT_CONFIG,
                )
            self.monitor_zones[output] = zones
        if not self.zones:
            if self.monitor_zones:
                fail(
                    _(
                        "zone-tiler: есть только секции [zones:<выход>], а обязательной [zones] нет; "
                        "[zones] задаёт набор зон по умолчанию"
                    ),
                    EXIT_CONFIG,
                )
            fail(_("zone-tiler: в zones.conf нет ни одной зоны в секции [zones]"), EXIT_CONFIG)
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
                        _("zone-tiler: правило '{selector}' ссылается на неизвестную зону '{zone}'").format(
                            selector=selector, zone=zone
                        ),
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
            if monitor_name:
                fail(
                    _(
                        "zone-tiler: зоны '{zone}' нет ни в наборе монитора '{monitor}', "
                        "ни в [zones]"
                    ).format(zone=zone, monitor=monitor_name),
                    EXIT_CONFIG,
                )
            fail(
                _("zone-tiler: зоны '{zone}' нет в [zones] и ни в одном наборе мониторов").format(
                    zone=zone
                ),
                EXIT_CONFIG,
            )
        return spec

    @staticmethod
    def _numlist(value):
        nums = [float(x) for x in re.split(r"[,;\s]+", value.strip()) if x]
        if not nums or abs(nums[-1] - 1.0) > 1e-6:
            fail(_("zone-tiler: последняя линия в cols/rows должна быть 1.0"), EXIT_CONFIG)
        lines = [0.0] + nums
        if any(b <= a for a, b in zip(lines, lines[1:])):
            fail(_("zone-tiler: линии в cols/rows должны возрастать"), EXIT_CONFIG)
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
                    _("zone-tiler: зона '{name}' должна быть 'c0:c1, r0:r1' или 'x,y,w,h'").format(
                        name=name
                    ),
                    EXIT_CONFIG,
                )
            ncols, nrows = len(self.cols) - 1, len(self.rows) - 1
            if not (0 <= c0 < c1 <= ncols and 0 <= r0 < r1 <= nrows):
                fail(
                    _(
                        "zone-tiler: зона '{name}' выходит за сетку "
                        "({ncols} колонок, {nrows} строк)"
                    ).format(name=name, ncols=ncols, nrows=nrows),
                    EXIT_CONFIG,
                )
            return ("grid", c0, c1, r0, r1)
        # зона как свободный прямоугольник в долях: "x,y,w,h"
        nums = [p for p in re.split(r"[,\s]+", spec) if p]
        try:
            x, y, w, h = (float(v) for v in nums)
        except Exception:
            fail(
                _("zone-tiler: зона '{name}' должна быть 'c0:c1, r0:r1' или 'x,y,w,h'").format(
                    name=name
                ),
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
            fail(
                _("zone-tiler: зона '{name}' выходит за пределы экрана (доли 0..1)").format(
                    name=name
                ),
                EXIT_CONFIG,
            )
        return ("rect", x, y, w, h)

    def fractions(self, zone, monitor_name=None):
        """Зона в долях рабочей области: (x, y, w, h)."""
        spec = self.zone_spec(zone, monitor_name)
        if spec[0] == "rect":
            return spec[1:]
        _unused, c0, c1, r0, r1 = spec
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
            _unused, fx, fy, fw, fh = spec
            return self._px(wa, fx, fy, fx + fw, fy + fh)
        _unused, c0, c1, r0, r1 = spec
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
    """Центр окна (px, py) по xwininfo; None при ошибке — см. `window_backend`."""
    return window_backend.read_window_center(win)


def output_at_point(outputs, point):
    """Индекс и имя выхода, в чью рабочую область попал `point` (центр окна).

    Общий кусок `move` и «без `--monitor`» у `apply`/`stack`: монитор берётся по
    центру рамки окна (`monitor_for_point`), а не по номеру 0 — иначе окно уезжает
    на первый экран, а зона ищется в чужом профиле.
    """
    areas = [area for _unused, area in outputs]
    wa = monitor_for_point(areas, point)
    index = areas.index(wa)
    return index, outputs[index][0], wa


def window_output(outputs, window, warn=True):
    """Куда адресуется команда без `--monitor`: по экрану окна, иначе первый.

    Рамку окна не прочитать (окно исчезло, окно не задано) — первый экран; `warn`
    решает, писать ли об этом в stderr: вызывающий может предупредить и сам, а
    повторять одну строку дважды не нужно (см. `target_output`).
    """
    if window is None:
        return 0, outputs[0][0], outputs[0][1]
    try:
        frame = read_frame(window, frame_extents(window))
    except Exception as exc:
        if warn:
            print(
                _(
                    "zone-tiler: не удалось прочитать рамку окна 0x{window:x}: {error} — "
                    "беру первый экран"
                ).format(window=window, error=exc),
                file=sys.stderr,
            )
        return 0, outputs[0][0], outputs[0][1]
    return output_at_point(
        outputs, (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2)
    )


def monitor_for_point(monitors, point):
    """Рабочая область монитора точки (иначе ближайшая) — см. `window_backend`."""
    return window_backend.monitor_for_point(monitors, point)


def window_monitor(win, mons):
    """Рабочая область монитора окна или None, если её не определить."""
    point = read_window_center(win)
    if point is None:
        return None
    return monitor_for_point(mons, point)


def window_workspace(win):
    """Номер стола окна (`_NET_WM_DESKTOP`) или None — см. `window_backend`."""
    return window_backend.window_workspace(win)


def current_workspace():
    """Номер активного стола (`_NET_CURRENT_DESKTOP`) или None — см. `window_backend`.

    Перевод нумерации EWMH (с нуля) в ключи ``[workspaces]`` (с единицы) —
    по-прежнему в :func:`grid_for_workspace`.
    """
    return window_backend.current_workspace()


def current_workspace_or_first():
    """Активный стол, а без `_NET_CURRENT_DESKTOP` — 0 (если X вообще отвечает).

    Нужен там, где стол должен быть числом (режим `focus`, демон): в Wayland-сессии
    Cinnamon свойства нет до первого переключения стола, и отказ здесь ломал `focus`
    целиком (замер: ``docs/RESEARCH-wayland-recon.md``). ``None`` остаётся только
    на «X не отвечает» — тогда, как и прежде, идёт общий `zones.conf`.

    Сначала спрашивается `current_workspace()`, а не внутреннее чтение: это точка
    подмены в тестах и у демона (``current_workspace=lambda: …``), и обходить её
    нельзя.
    """
    desk = current_workspace()
    if desk is not None:
        return desk
    return 0 if window_backend.x_alive() else None


def window_on_other_desktop(win):
    """Окно на другом столе — не то же самое, что свёрнуто; см. `window_backend`."""
    return window_backend.window_on_other_desktop(win)


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
        _(
            "неверный рабочий стол '{text}': нужен номер стола (0, 1, …), "
            "{current} или {conf}"
        ).format(text=text, current=WORKSPACE_CURRENT, conf=WORKSPACE_CONF)
    )


def resolve_workspace(spec):
    """Номер стола для запроса сетки: ``current``/``None`` — активный стол.

    Стол ищется через `current_workspace_or_first`: в Wayland-сессии Cinnamon
    свойства `_NET_CURRENT_DESKTOP` нет до первого переключения стола, и раньше
    здесь получался отказ всей команды. ``None`` теперь только на «X не
    отвечает» — тогда, как и прежде, идёт общий `zones.conf`.
    """
    if spec is None or spec == WORKSPACE_CURRENT:
        return current_workspace_or_first()
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


def grid_for_output(conf_path, output, fallback=None, workspace=None, quiet=False):
    """Сетка экрана: профиль из ``[monitors]`` → ``[workspaces]`` → активный конфиг.

    ``output`` — имя выхода окна (``HDMI-1``); ``workspace`` — номер стола из EWMH
    (с нуля), ``None`` — активный. Порядок разбора —
    :func:`zone_config.resolve_profile`: экран важнее стола, стол важнее активного
    конфига. ``fallback`` — уже сосчитанная сетка стола: ею экран живёт, если
    профиля нет на диске или он не найден. Откат с предупреждением, а не отказ:
    раскладку на экране без профиля пользователь всё равно ждёт.

    Возвращает ``(Grid, имя профиля | None)``: имя — чтобы сказать в сообщении, из
    какого профиля взята сетка. ``--workspace conf`` профилей не читает вообще —
    редактор обязан считать ту же сетку, что на холсте.
    """
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как у grid_for_workspace

    if workspace is None:
        workspace = current_workspace()
    if workspace == WORKSPACE_CONF:
        return (fallback if fallback is not None else Grid(conf_path)), None
    base = fallback if fallback is not None else Grid(conf_path)
    monitor_map = zc.load_monitor_map(conf_path)
    profile = zc.resolve_profile(
        monitor_map,
        zc.load_workspace_map(conf_path),
        output,
        workspace,
    )
    if profile is None:
        return base, None
    # Профиль мог прийти со стола, а не с экрана: сетку стола base уже несёт, и
    # подпись «экран …» в сообщениях была бы враньём.
    bound_to_screen = any(
        str(key).strip().casefold() == str(output or "").strip().casefold()
        for key in monitor_map
    )
    if not bound_to_screen:
        return base, None
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        path = None
    if path is None or not os.path.exists(path):
        if not quiet:
            if output:
                print(
                    _(
                        "zone-tiler: профиля «{profile}» нет на диске: экран {output} "
                        "раскладывается без него: беру набор стола или общий"
                    ).format(profile=zc.sanitize_profile_name(profile), output=output),
                    file=sys.stderr,
                )
            else:
                print(
                    _(
                        "zone-tiler: профиля «{profile}» нет на диске: экран окна "
                        "раскладывается без него: беру набор стола или общий"
                    ).format(profile=zc.sanitize_profile_name(profile)),
                    file=sys.stderr,
                )
        return base, None
    return Grid(path), profile


def monitor_bindings(conf_path):
    """Есть ли в конфиге привязки экранов ``[monitors]`` (без обращения к X)."""
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как у grid_for_output

    return bool(zc.load_monitor_map(conf_path))


def require_zone(grid, zone, workspace=None, profile=None, output=None):
    """Проверить, что зона есть в наборе сетки; иначе ошибка конфига (код 3).

    Зона ищется в объединении наборов показанной сетки (общий `[zones]` и
    `[zones:<выход>]`) — то есть ровно там, где её найдёт раскладка. Перечень
    доступных зон печатается целиком: по нему видно, что стол получил не тот
    профиль, а не только что имя набрано с ошибкой. ``output`` — экран, чей
    профиль дал сетку: сообщение про «сетку стола» на профиле экрана врало бы.
    """
    known = grid.all_zone_names()
    if zone in known:
        return
    have = ", ".join(sorted(known))
    if output and profile:
        fail(
            _(
                "zone-tiler: нет зоны '{zone}' в сетке экрана {output} (профиль «{profile}»). "
                "Есть: {known}"
            ).format(zone=zone, output=output, profile=profile, known=have),
            EXIT_CONFIG,
        )
    if profile and workspace is not None:
        fail(
            _(
                "zone-tiler: нет зоны '{zone}' в сетке стола {workspace} (профиль «{profile}»). "
                "Есть: {known}"
            ).format(zone=zone, workspace=workspace + 1, profile=profile, known=have),
            EXIT_CONFIG,
        )
    if profile:
        fail(
            _(
                "zone-tiler: нет зоны '{zone}' в сетке профиля «{profile}». Есть: {known}"
            ).format(zone=zone, profile=profile, known=have),
            EXIT_CONFIG,
        )
    fail(
        _("zone-tiler: нет зоны '{zone}' в [zones]. Есть: {known}").format(
            zone=zone, known=have
        ),
        EXIT_CONFIG,
    )


def gdk_monitors():
    """``[(логическая рабочая область, масштаб)]`` из GDK — см. `window_backend`."""
    return window_backend.gdk_monitors()


def monitors():
    """Рабочие области мониторов в **физических** пикселях X.

    См. `window_backend.monitors`: он отвечает `WindowBackendError`, а сообщение
    и код возврата остаются здесь — те же, что до переноса оконного слоя.
    """
    try:
        return window_backend.monitors()
    except window_backend.WindowBackendError:
        fail(_("zone-tiler: не удалось определить рабочую область экрана"), EXIT_FAIL)


def monitor_scales():
    """Масштабы мониторов в том же порядке, что `monitors()` — см. `window_backend`."""
    return window_backend.monitor_scales()


def scale_at_point(point):
    """Масштаб монитора, в чью физическую рабочую область попала точка."""
    return window_backend.scale_at_point(point)


def monitor_outputs():
    """[(имя выхода, (x, y, w, h))] — по одному на монитор.

    См. `window_backend.monitor_outputs`: тела сшивки живут в оконном слое, а
    собираются здесь затем, чтобы подмены `monitors`/`xrandr_monitors` в тестах
    и остальном коде продолжали действовать.
    """
    return window_backend.stitch_monitor_names(xrandr_monitors(), monitors())


def parse_xrandr_monitors(text):
    """[(имя, (x, y, w, h))] из вывода `xrandr --listmonitors` — см. `window_backend`."""
    return window_backend.parse_xrandr_monitors(text)


def xrandr_monitors():
    """Мониторы из `xrandr --listmonitors`; [] если утилиты нет — см. `window_backend`."""
    return window_backend.xrandr_monitors()


def overlap_area(a, b):
    """Площадь пересечения прямоугольников a и b (0, если не пересекаются)."""
    return window_backend.overlap_area(a, b)


def stitch_monitor_names(outputs, areas):
    """Сшить имена выходов с рабочими областями: [(имя, область)].

    См. `window_backend.stitch_monitor_names`: совпало количество — по порядку,
    иначе — по максимальному пересечению, без имён — `monitor-<i>`.
    """
    return window_backend.stitch_monitor_names(outputs, areas)


def resolve_monitor(spec, outputs=None):
    """(индекс, имя) монитора по `--monitor`: имя выхода приоритетнее номера.

    `spec` — имя (`HDMI-1`) или номер (`1`); `outputs` — результат
    `monitor_outputs()` (можно подменить в тестах). Неизвестное имя или
    несуществующий номер — ошибка аргументов с перечнем доступных мониторов.
    """
    outputs = monitor_outputs() if outputs is None else outputs
    text = "" if spec is None else str(spec).strip()
    for index, (name, _unused) in enumerate(outputs):
        if name == text:
            return index, name
    number = int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    if number is not None and 0 <= number < len(outputs):
        return number, outputs[number][0]
    available = ", ".join(f"{i} — {name}" for i, (name, _unused) in enumerate(outputs))
    fail(
        _("zone-tiler: монитора '{monitor}' нет. Доступны: {available}").format(
            monitor=text, available=available or _("нет")
        ),
        EXIT_USAGE,
    )


def target_output(args, outputs=None):
    """Имя выхода, к которому относится `apply`/`stack`, или ``None``.

    Явный `--monitor` важнее; без него адресат — **экран окна** (активного или
    заданного `--window`), ровно как в самих командах: иначе сетка пришла бы с
    одного экрана, а рамка считалась на другом, и зона искалась бы в чужом
    профиле. Не поднимает ошибку, а отвечает ``None``, если мониторов нет или
    адресат не разрешается: проверка зоны идёт раньше, и человек должен увидеть
    «нет зоны», а внятную ошибку про монитор всё равно даст `resolve_monitor` в
    самой команде. О непрочитанной рамке окна не предупреждает — это сделает
    команда (`warn=False`).
    """
    outputs = monitor_outputs() if outputs is None else outputs
    if not outputs:
        return None
    if args.monitor is None:
        window = getattr(args, "window", None) or active_window_quiet()
        _index, output, _wa = window_output(outputs, window, warn=False)
        return output
    text = str(args.monitor).strip()
    for name, _wa in outputs:
        if name == text:
            return name
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text)
        if 0 <= number < len(outputs):
            return outputs[number][0]
    return None


def frame_extents(win):
    """Толщина рамки/титлбара оконного менеджера — см. `window_backend`."""
    return window_backend.frame_extents(win)


def client_size(rect, extents):
    """Геометрия клиента внутри целевой рамки окна (компенсация рамки WM).

    WM рисует рамку/титлбар вокруг клиента, поэтому клиенту выдаётся зона минус
    _NET_FRAME_EXTENTS. Ширина и высота не меньше 1 px. Чистая функция — под тесты.
    """
    x, y, w, h = rect
    left, right, top, bottom = extents
    return (x, y, max(1, w - left - right), max(1, h - top - bottom))


def read_active_window():
    """Активное окно и текст ошибки: ``(окно | None, причина | None)``.

    Чтение — в `window_backend` (он отдаёт код причины), а текст для человека
    остаётся здесь: `active_window` падает с кодом 1, когда окна нет (это
    правильно для команд, которым окно нужно), а `active_window_quiet` отдаёт
    ``None`` без сообщений в stderr — тому, для кого отсутствие окна не ошибка.
    """
    window, reason = window_backend.read_active_window()
    if window is not None:
        return window, None
    if reason == window_backend.ACTIVE_WINDOW_NO_XPROP:
        return None, _("zone-tiler: xprop не найден (пакет x11-utils)")
    return None, _("zone-tiler: нет активного окна")


def active_window():
    """Активное окно; нет его — сообщение в stderr и код 1."""
    window, error = read_active_window()
    if window is None:
        fail(error, EXIT_FAIL)
    return window


def active_window_quiet():
    """Активное окно или ``None`` — без сообщений в stderr.

    Нужно там, где отсутствие окна не ошибка: выбор монитора по экрану окна,
    `apply --dry-run` в скрипте, `stack` без окон. Тогда берётся первый экран —
    как было до адресации по экрану.
    """
    return read_active_window()[0]


# ----------------------------------------------------------------------- X11


# Соединение с X-сервером и EWMH-команды целиком живут в оконном слое. Имя
# оставлено здесь ради вызывающих и тестов: `apply_zone` открывает соединение
# через `X11()`, а тесты подменяют `zone-tiler.X11` поддельным соединением.
X11 = window_backend.X11

# Значения EWMH/ICCCM, которые видны снаружи класса (их сверяют тесты).
SOURCE_PAGER = window_backend.SOURCE_PAGER
ICONIC_STATE = window_backend.ICONIC_STATE


def _x11_failed():
    """Сообщить, что X-соединения нет, и выйти с кодом 1.

    Раньше это делал сам `X11.__init__` через `fail`; теперь соединение открывает
    оконный слой и отвечает `WindowBackendError`, а текст и код возврата остались
    здесь — на границе CLI (см. `main`).
    """
    fail(_("zone-tiler: не удалось подключиться к X-серверу (DISPLAY)"), EXIT_FAIL)


# --------------------------------------------- сворачивание (режим «только эта зона»)


def window_iconic(win):
    """Свёрнуто ли окно сейчас (`Iconic`/`_NET_WM_STATE_HIDDEN`) — см. `window_backend`."""
    return window_backend.window_iconic(win)


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
            print(_("окно 0x{window:x} -> свёрнуто").format(window=window))
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


def focus_records(windows):
    """Записи окон для `focus.json`: `WM_CLASS` и стол берутся прямо сейчас.

    Стол читается у **каждого** окна, а не берётся из одного поля записи: в файле
    сходятся окна разных столов (остаток CLI и остаток демона), а разворот
    возможен только на активном столе — значит, стол нужен по каждому окну.
    Здесь же читается и `WM_CLASS`: записи без класса уклоняются от защиты от
    переиспользованного XID, а читать класс в момент записи дешевле, чем потом
    развернуть чужое окно.
    """
    return [
        {
            "window": hex(win),
            "wm_class": window_class_title(win)[0],
            "workspace": window_workspace_for_zones(win),
        }
        for win in dict.fromkeys(windows)
    ]


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


# Сколько раз перечитать состояние окна после запроса и с какой паузой: и разворот,
# и смена стола — клиентские сообщения, WM применяет их не мгновенно.
FOCUS_VERIFY_TRIES = 5
FOCUS_VERIFY_DELAY = 0.2


def switch_workspace(desktop, x11=None, read_current=None):
    """Переключить активный стол и дождаться, что он стал активным.

    Нужно **до** разворота: `_NET_ACTIVE_WINDOW` muffin игнорирует для окна не
    на активном столе (проверено дымом), а окна режима лежат на своём столе.
    `read_current` подменяется в тестах. `True` — стол нужный (был или стал).
    """
    reader = read_current or current_workspace
    own = x11 is None
    if own:
        x11 = X11()
    try:
        if reader() == desktop:
            return True
        x11.set_current_desktop(desktop)
        for _unused in range(FOCUS_VERIFY_TRIES):
            time.sleep(FOCUS_VERIFY_DELAY)
            if reader() == desktop:
                return True
        return reader() == desktop
    finally:
        if own:
            x11.close()


def deiconify_all(windows, x11=None, deiconify_fn=None):
    """Развернуть окна и перечитать их состояние; вернёт **не развернувшиеся**.

    `deiconify` — клиентское сообщение, и WM обрабатывает его не мгновенно,
    поэтому состояние (`window_iconic`) перечитывается с паузами. Оставшееся
    свёрнутым окно почти наверняка на неактивном столе: muffin игнорирует
    активацию таких окон (проверено дымом). Вызывающий решает, что с остатком
    делать, — молча считать его развёрнутым нельзя.

    `deiconify_fn` подменяется в тестах (и демон передаёт свою инъекцию): с ней
    X-соединение не открывается вовсе.
    """
    pending = list(dict.fromkeys(windows))
    if not pending:
        return []
    send = deiconify_fn or deiconify
    own = deiconify_fn is None and x11 is None
    if own:
        x11 = X11()
    try:
        for win in pending:
            send(win, x11=x11)
        for _unused in range(FOCUS_VERIFY_TRIES):
            time.sleep(FOCUS_VERIFY_DELAY)
            pending = [win for win in pending if window_iconic(win)]
            if not pending:
                return []
        return pending
    finally:
        if own:
            x11.close()


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
                (сверх числа зон) достаётся последняя зона (там они —
                вкладками);
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


def zone_tabs_module():
    """Модуль `zone_tabs`: арифметика вкладок в зоне живёт только там."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_tabs  # noqa: PLC0415 — ленивый импорт, рядом с раскладчиком

    return zone_tabs


def in_zone_rect(zone_rect, count, height=None):
    """Рамка окна в зоне: одно окно — ровно зона, несколько — рамка вкладки.

    `count` — сколько окон оказалось в зоне, `height` — высота полосы вкладок
    (`[tabs] height`, по умолчанию `zone_tabs.STRIP_HEIGHT`). Несколько окон
    делят одну рамку вкладки (`zone_tabs.tab_rect`: зона минус полоса), а не
    сдвигаются каскадом: сдвиг оставлял видимыми куски всех окон сразу и ломал
    рамку зоны. В узкой зоне полосы нет и рамка вкладки — сама зона, но вкладки
    всё равно работают. Чистая функция — под тесты. `arrange` и `stack` кладут
    окна одним и тем же правилом.
    """
    tabs = zone_tabs_module()
    strip = tabs.STRIP_HEIGHT if height is None else int(height)
    # Правило «одна вкладка — вся зона, две и больше — рамка вкладки» живёт
    # в ядре (`zone_tabs.stack_rect`): одно на демон и раскладчик, иначе
    # `apply` и `arrange` разойдутся на одном и том же числе окон.
    rect = tabs.stack_rect(zone_rect, int(count), strip)
    # Рамки окон — пиксели: ядро считает float, а EWMH и вывод `--dry-run`
    # ждут целых, как у `Grid.rect`.
    return tuple(int(round(v)) for v in rect)


def tabs_height(grid):
    """Высота полосы вкладок из `[tabs] height`; нет секции — умолчание полосы.

    Разбор секции живёт в `zone_config.load_tabs_config` (там же — ошибка на
    мусор в высоте), чтобы раскладчик и полоса не разбирали `[tabs]` по-своему.
    Импорт ленивый: `zone_config` сам загружает этот файл как модуль.
    """
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как у редактора

    return zc.load_tabs_config(grid.path)["height"]


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
            print(
                _("zone-tiler: не удалось сохранить историю: {error}").format(error=exc),
                file=sys.stderr,
            )


def snapshot_window(win, zone=None):
    """Снимок окна для истории: прежняя рамка, класс, воркспейс.

    Райзит исключение, если геометрию не прочитать (окно исчезло) — вызывающий
    решает, записывать ли операцию.
    """
    extents = frame_extents(win)
    frame = read_frame(win, extents)
    wm_class, _title = window_class_title(win)
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
    набор зон именно такой. В первой строке — индекс монитора, его рабочая
    область `x,y WxH` и, при необходимости, пометки выхода и профиля; апплет
    Cinnamon разбирает в ней именно числа (`N: x,y WxH`), а слова переводимы.
    """
    outputs = monitor_outputs()
    index, output = resolve_monitor(monitor_arg(args), outputs)
    wa = outputs[index][1]
    area = {"index": index, "x": wa[0], "y": wa[1], "w": wa[2], "h": wa[3]}
    named = output != f"monitor-{index}"
    if not named and profile:
        line = _(
            "рабочая область монитора {index}: {x},{y} {w}x{h}  (профиль «{profile}»)"
        ).format(profile=profile, **area)
    elif not named:
        line = _("рабочая область монитора {index}: {x},{y} {w}x{h}").format(**area)
    elif profile:
        line = _(
            "рабочая область монитора {index}: {x},{y} {w}x{h}  (выход {output})  "
            "(профиль «{profile}»)"
        ).format(output=output, profile=profile, **area)
    else:
        line = _("рабочая область монитора {index}: {x},{y} {w}x{h}  (выход {output})").format(
            output=output, **area
        )
    print(line)
    zones = grid.zones_for(output)
    width = max(len(z) for z in zones)
    for name in sorted(zones):
        x, y, w, h = grid.rect(name, wa, monitor_name=output)
        print(f"  {name:<{width}}  x={x:<5} y={y:<5} {w}x{h}")


def read_frame(window, extents):
    """Фактическая рамка окна (x, y, w, h) по xwininfo с учётом рамки WM.

    Чтение — в `window_backend.read_frame`; он сообщает только имя поля, которого
    не оказалось в выводе, а текст `ValueError` для человека собирается здесь.
    """
    try:
        return window_backend.read_frame(window, extents)
    except window_backend.WindowBackendError as exc:
        raise ValueError(
            _("xwininfo: нет поля '{label}'").format(label=str(exc))
        ) from None


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
            if label:
                print(
                    _("окно 0x{window:x} -> зона '{zone}': x={x} y={y} {w}x{h}").format(
                        window=window, zone=label, x=x, y=y, w=w, h=h
                    )
                )
            else:
                print(
                    _("окно 0x{window:x} -> зона: x={x} y={y} {w}x{h}").format(
                        window=window, x=x, y=y, w=w, h=h
                    )
                )
        if verify:
            try:
                actual = read_frame(window, extents)
            except Exception:
                return None
            if not quiet:
                if actual == (x, y, w, h):
                    print(
                        _("рамка по факту: x={x} y={y} {w}x{h} — совпало с зоной").format(
                            x=actual[0], y=actual[1], w=actual[2], h=actual[3]
                        )
                    )
                else:
                    print(
                        _("рамка по факту: x={x} y={y} {w}x{h} — не совпало!").format(
                            x=actual[0], y=actual[1], w=actual[2], h=actual[3]
                        )
                    )
            return actual
        return None
    finally:
        # чужое соединение не закрываем: им владеет вызывающий (демон)
        if own:
            x11.close()


def cmd_apply(grid, args):
    """Вложить окно в зону; без `--monitor` — на экране самого окна.

    Экран окна задаёт и набор зон: зона ищется в профиле того экрана, где окно
    (`grid_for_output`), — как `move` и как демон в IPC `apply`.
    """
    outputs = monitor_outputs()
    # Окно читается заранее: без `--monitor` оно задаёт экран, а его профиль —
    # набор зон. `--dry-run` переживает отсутствие окна: тогда это первый экран,
    # как было до адресации по экрану.
    window = args.window or active_window_quiet()
    if args.monitor is None:
        index, output, wa = window_output(outputs, window)
    else:
        index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[index][1]
    rect = grid.rect(args.zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(
            _("{zone}: x={x} y={y} {w}x{h} (монитор {index}: {output})").format(
                zone=args.zone, x=x, y=y, w=w, h=h, index=index, output=output
            )
        )
        return
    if window is None:
        fail(_("zone-tiler: нет активного окна"), EXIT_FAIL)
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: окно 0x{window:x} под исключением "
                    "([ignore]/служебный тип) — не двигаю"
                ).format(window=window),
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
# Укладка по `arrange` — не мгновенный запрос: демон ставит каждое окно сам и
# перечитывает его рамку, поэтому на большом столе 5 с на ответ может не хватить,
# хотя демон жив и работает.
DAEMON_ARRANGE_TIMEOUT = 30.0


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
            _(
                "zone-tiler: демон не ответил за {timeout:g} с — режим не переключаю "
                "(работающего демона нельзя обойти снаружи)"
            ).format(timeout=timeout),
            EXIT_FAIL,
        )
    except zone_ipc.ProtocolError as exc:
        fail(_("zone-tiler: демон ответил непонятно: {error}").format(error=exc), EXIT_FAIL)
    finally:
        conn.close()


def cmd_tabs_next(args):
    """Поднять следующую вкладку зоны активного окна (по клавише `[tabs] key`).

    Хозяин вкладок — демон: он один знает, какие окна стоят на рамке вкладки, и
    только он умеет активировать окно (`_NET_ACTIVE_WINDOW` + подъём). Поэтому
    команда ничего не раскладывает сама, а шлёт ему `zone_tabs.tabs_next_command()`
    тем же сокетом, что и `gridsnap-ctl` (ленивый клиент — `daemon_request`).
    Демона нет — это код 1 с понятным сообщением, а не трейсбек: без демона
    вкладок нет, а кнопка на клавише осталась. Окно вне зон демон считает тихим
    no-op (код 0): переключать там нечего, и это не ошибка.
    """
    request = zone_tabs_module().tabs_next_command()
    response = daemon_request(request)
    if response is None:
        fail(
            _(
                "zone-tiler: демон не запущен — вкладки в зоне переключает он "
                "(запустите gridsnap-daemon)"
            ),
            EXIT_FAIL,
        )
    if not response.get("ok", True):
        reason = response.get("error") or _("причина не названа")
        fail(_("zone-tiler: демон отказал: {reason}").format(reason=reason), EXIT_FAIL)
    if args.quiet:
        return
    window = response.get("window")
    if window is None:
        print(_("zone-tiler: переключать нечего — в зоне активного окна одно окно"))
        return
    print(_("zone-tiler: следующее окно зоны — {window}").format(window=window))


def cmd_action(grid, args):
    """Выполнить действие из `[action:<имя>]` — то, что висит на клавише.

    Имя действия приходит первым позиционным аргументом (как у `profile`: у
    команды `action` в `args.zone` лежит имя, а не зона). Запись читает
    `zone_config.load_actions`.

    **Что действие получает** (`zone_actions.ActionContext`): окно, зону, её
    пиксельную рамку, монитор, стол, класс и заголовок окна, путь к конфигу —
    переменными окружения `GRIDSNAP_*` и подстановками `{zone}`/`{window}`/`{x}`…
    Внешняя команда разбирается `shlex` и запускается **без оболочки** и без
    ожидания (`start_new_session`): клавишу нельзя вешать на `gnome-terminal`,
    который живёт до закрытия окна.

    **Предмет** — зона из записи (`zone = main`), а окно ищется в ней; без зоны —
    активное окно и зона, в которой оно стоит. `--window` задаёт окно явно,
    `--monitor` — экран для поиска зоны (без него: экран окна, затем активного
    окна, затем первый).

    **Встроенные** (`run = builtin:<имя>`): `minimize-others` — тот же режим, что
    `focus` (делегирует демону, если он жив); `show-window` — поднять/развернуть
    окно зоны (вкладки — следующая от активного); `mirror` — обмен с зеркальной
    зоной; `terminal` — запустить терминал и положить его окно в зону.

    `--dry-run` печатает разобранный контекст и план (встроенное) или готовый
    argv (команда), ничего не делая. Коды: 3 — ошибка конфига (нет такого
    действия, битое поле, неизвестная подстановка), 1 — отказ по состоянию, 0 —
    успех **и** честный no-op («показывать нечего», «зеркальная зона — та же
    зона») с сообщением в stderr.

    У `terminal` последнее слово — за действием: окно могло уже попасть под
    `[rules]` демона и встать по правилу, но раскладка действия важнее, поэтому
    рамка зоны ставится ещё раз. `show-window` на пустой зоне — no-op с кодом 0:
    клавиша не должна ругаться на то, что показывать нечего (как `tabs-next`).
    """
    za = actions_module()
    name = (args.zone or "").strip()
    if not name:
        fail(
            _(
                "zone-tiler: action — укажите имя действия, например: "
                "zone-tiler.py action mirror"
            ),
            EXIT_CONFIG,
        )
    actions = load_actions(args.conf)
    if name not in actions:
        known = ", ".join(actions) or _("ни одного")
        fail(
            _("zone-tiler: нет действия '{name}'. Есть: {known}").format(
                name=name, known=known
            ),
            EXIT_CONFIG,
        )
    record = actions[name]
    run = (record.get("run") or "").strip()
    try:
        builtin = za.builtin_name(run)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)

    # Экран — адресат действия: явный `--monitor`, иначе экран окна, иначе первый.
    # Окно-кандидат для выбора экрана — `--window` или активное: зону-предмет из
    # записи ещё надо посчитать в пикселях, а для этого нужна сетка экрана.
    outputs = monitor_outputs()
    if args.monitor is not None:
        index, output = resolve_monitor(args.monitor, outputs)
    else:
        index, output, _wa = window_output(outputs, args.window or active_window_quiet())
    wa = outputs[index][1]
    workspace = resolve_workspace(getattr(args, "workspace", None))
    grid, profile = grid_for_output(
        args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
    )

    window, zone = _action_subject(
        grid, args, record, output, wa, workspace=workspace, profile=profile
    )
    ctx = _action_context(grid, args, window, zone, output, wa)

    if builtin is None:
        _action_command(args, name, run, ctx, za)
        return
    handlers = {
        za.MINIMIZE_OTHERS: _action_minimize_others,
        za.SHOW_WINDOW: _action_show_window,
        za.MIRROR: _action_mirror,
        za.TERMINAL: _action_terminal,
    }
    handlers[builtin](grid, args, name, record, ctx, wa)


def actions_module():
    """Модуль `zone_actions`: контекст, подстановки и зеркало живут только там."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_actions  # noqa: PLC0415 — ленивый импорт, рядом с раскладчиком

    return zone_actions


def zone_config_module():
    """Модуль `zone_config`: разбор `[action:<имя>]` живёт только там."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_config  # noqa: PLC0415 — ленивый импорт, рядом с раскладчиком

    return zone_config


def load_actions(conf):
    """Действия конфига через `zone_config.load_actions` (битый конфиг — код 3)."""
    return zone_config_module().load_actions(conf)


# Сколько ждать окна терминала: 50 попыток по 0.1 с — около 5 секунд. Терминал
# на холодную (загрузка профиля, первый запуск) легко не успевает за меньшее.
ACTION_WAIT_TRIES = 50
ACTION_WAIT_DELAY = 0.1


def _action_subject(grid, args, record, output, wa, workspace=None, profile=None):
    """Окно и зона действия — «предмет», который получает контекст.

    Зона из записи задаёт предмет сама: окно — активное (`--window`), если оно
    стоит в этой зоне, иначе верхнее по `_NET_CLIENT_LIST` (список идёт снизу
    вверх, поэтому «верхнее» — последнее); пустая зона — окна нет. Без зоны в
    записи окно — `--window` или активное, а зона — та, в чью рамку (или рамку
    вкладки) встала его рамка (`zone_actions.zone_of_frame`); вне зон — `None`.

    Зона из записи ищется в объединении наборов сетки (`require_zone`, код 3),
    как у `apply`: имя из чужого профиля — ошибка конфига, а не «пустая зона».
    """
    client_ids = read_client_list() or []
    height = tabs_height(grid)
    preferred = args.window or active_window_quiet()
    zone = record.get("zone")
    if zone:
        require_zone(grid, zone, workspace=workspace, profile=profile, output=output)
        rect = grid.rect(zone, wa, monitor_name=output)
        return _action_window_in_zone(rect, client_ids, height, preferred), zone
    zones_px = {
        name: grid.rect(name, wa, monitor_name=output)
        for name in grid.zones_for(output)
    }
    frame = _action_frame(preferred)
    return preferred, actions_module().zone_of_frame(frame, zones_px, height)


def _action_frame(window):
    """Рамка окна с компенсацией `_NET_FRAME_EXTENTS`; не прочитать — `None`."""
    if window is None:
        return None
    try:
        return read_frame(window, frame_extents(window))
    except Exception:
        return None


def _action_frames(client_ids):
    """`{окно: рамка}` для клиентов `_NET_CLIENT_LIST` (не прочиталась — `None`).

    Рамки читаются один раз на действие: и `show-window`, и `mirror` смотрят на
    окна двух зон, и второй проход `xwininfo` по тем же окнам ничего не добавил
    бы, а окно могло бы за это время исчезнуть.
    """
    return {win: _action_frame(win) for win in client_ids}


def _action_windows_in_zone(zone_rect, frames, height):
    """Окна зоны по её пиксельной рамке — правило ядра `zone_actions.zone_windows`.

    Окно в зоне, если его рамка совпала с рамкой зоны целиком или с рамкой
    вкладки (зона минус полоса `[tabs] height`); сравнение — с допуском ядра.
    """
    tab_rect = in_zone_rect(zone_rect, 2, height)
    return actions_module().zone_windows(frames, zone_rect, tab_rect)


def _action_window_in_zone(zone_rect, client_ids, height, preferred=None):
    """Верхнее окно зоны, а если `preferred` уже в ней — само `preferred`.

    «Верхнее» — последнее в `_NET_CLIENT_LIST`: список идёт снизу вверх. Пустая
    зона — `None` (действие решает, no-op это или отказ).
    """
    frames = _action_frames(client_ids)
    if preferred is not None and _action_windows_in_zone(
        zone_rect, {preferred: frames.get(preferred)}, height
    ):
        return preferred
    found = _action_windows_in_zone(zone_rect, frames, height)
    return found[-1] if found else None


def _action_context(grid, args, window, zone, output, wa):
    """Собрать `zone_actions.ActionContext` для окна и зоны действия.

    `rect` — пиксельная рамка зоны на её мониторе (её и ждёт чужая команда),
    `conf` — **абсолютный** путь: действие запускает не CLI, а чужой процесс, и
    относительный `--conf zones.conf` в нём означал бы другой файл или никакого.
    Стол — стол окна, у липкого окна и окна без стола — активный.
    """
    rect = grid.rect(zone, wa, monitor_name=output) if zone else None
    meta = read_window_meta(window) if window is not None else None
    workspace = window_workspace_for_zones(window) if window is not None else None
    if workspace is None:
        workspace = current_workspace()
    return actions_module().ActionContext(
        window=window,
        zone=zone,
        rect=rect,
        monitor=output,
        workspace=workspace,
        wm_class=meta.wm_class if meta is not None else "",
        wm_title=meta.wm_title if meta is not None else "",
        conf=os.path.abspath(args.conf),
    )


def _action_plan_line(name, run, ctx):
    """Первая строка `--dry-run`: что действие получило (окно, зона, рамка, экран)."""
    window = _("окна нет") if ctx.window is None else _("окно 0x{window:x}").format(
        window=ctx.window
    )
    where = _("вне зон") if not ctx.zone else _("зона '{zone}'").format(zone=ctx.zone)
    if ctx.rect:
        where += ": x={} y={} {}x{}".format(*ctx.rect)
    screen = ctx.monitor or _("экран не определён")
    desk = _("неизвестен") if ctx.workspace is None else str(ctx.workspace + 1)
    print(
        _(
            "действие '{name}' ({run}): {window} -> {where} "
            "(монитор {screen}, стол {desk})"
        ).format(
            name=name,
            run=run or "?",
            window=window,
            where=where,
            screen=screen,
            desk=desk,
        )
    )


def _action_noop(args, message):
    """Честный no-op: сообщение в stderr (если не `--quiet`) и код 0.

    Клавиша не должна ругаться на то, что показывать или обменивать нечего:
    так же ведёт себя `tabs-next` в зоне с одним окном.
    """
    if not args.quiet:
        print(f"zone-tiler: {message}", file=sys.stderr)


def _action_dry_run_noop(args, name, record, ctx, message):
    """Пустой предмет действия: no-op, а под `--dry-run` — контекст и план.

    `--dry-run` обещан любому действию, поэтому даже «показывать нечего» он
    печатает разобранный контекст и объясняет план в stdout, а не молчит: иначе
    по пустому выводу не отличить «зона пуста» от «действие не нашлось».
    """
    if not args.dry_run:
        _action_noop(args, message)
        return
    _action_plan_line(name, record.get("run"), ctx)
    print(_("план: {message} — ничего не делаю").format(message=message))


def _spawn(argv, ctx):
    """Запустить команду действия отсоединённо: без оболочки и без ожидания.

    `start_new_session` — чтобы терминал пережил нажатие клавиши (и чтобы
    сигнал нашей группы процессу не дошёл); `stdin` — `/dev/null`, иначе
    запущенный из клавиши процесс делил бы с ней ввод. Возвращает `Popen`.
    """
    try:
        return subprocess.Popen(
            argv,
            env={**os.environ, **ctx.env()},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        fail(_("zone-tiler: не удалось запустить действие: {error}").format(error=exc), EXIT_FAIL)


def _action_command(args, name, run, ctx, za):
    """Внешняя команда действия: `argv` из конфига, запуск без оболочки.

    Неизвестная подстановка и пустая команда — `ActionError` ядра, то есть
    ошибка конфига (код 3), а не «команда не запустилась»: опечатку в `{что-то}`
    человек увидит сразу, а не как пустой аргумент у чужого скрипта.
    """
    try:
        argv = za.expand_command(run, ctx)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if args.dry_run:
        _action_plan_line(name, run, ctx)
        print(_("план: argv = {argv}").format(argv=argv))
        return
    _spawn(argv, ctx)
    if not args.quiet:
        print(
            _("zone-tiler: действие '{name}' запущено: {command}").format(
                name=name, command=argv[0]
            )
        )


def _action_minimize_others(grid, args, name, record, ctx, wa):
    """`builtin:minimize-others` — режим «только эта зона», то же, что `focus`.

    Своей копии у режима нет: собираем `cmd_focus` его аргументы, и он делает
    всё остальное — в том числе отдаёт работу живому демону, который один умеет
    доворачивать окна, появившиеся уже в режиме. Зона — из записи (нет её —
    «только это окно»), окно — предмет действия, иначе `--window`.
    """
    zone = record.get("zone")
    window = ctx.window if ctx.window is not None else args.window
    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        # `None` печатается как раньше: шаблон и подстановка без перевода.
        shown = window if window is None else hex(window)
        if zone:
            print(
                _(
                    "план: окно {window} -> зону '{zone}', "
                    "остальные окна стола и монитора — свёрнуты (повторное нажатие вернёт)"
                ).format(window=shown, zone=zone)
            )
        else:
            print(
                _(
                    "план: окно {window} -> на месте, "
                    "остальные окна стола и монитора — свёрнуты (повторное нажатие вернёт)"
                ).format(window=shown)
            )
        return
    cmd_focus(
        grid,
        argparse.Namespace(
            zone=zone, off=False, window=window, dry_run=False, quiet=args.quiet
        ),
    )


def _action_show_window(grid, args, name, record, ctx, wa):
    """`builtin:show-window` — поднять окно зоны (у стопки — сосед по кольцу).

    Окна зоны — `zone_actions.zone_windows` по рамкам клиентов и рамке вкладки;
    цель — `zone_tabs.next_tab` от активного окна (одно окно или активное вне
    стопки — первое в порядке списка). `deiconify` — это `_NET_ACTIVE_WINDOW`:
    развернуть, поднять, сфокусировать. Зоны нет или она пуста — no-op (код 0).
    """
    if not ctx.zone:
        _action_dry_run_noop(args, name, record, ctx, _("окно вне зон — показывать нечего"))
        return
    base = grid.rect(ctx.zone, wa, monitor_name=ctx.monitor)
    height = tabs_height(grid)
    frames = _action_frames(read_client_list() or [])
    stack = _action_windows_in_zone(base, frames, height)
    if not stack:
        _action_dry_run_noop(
            args,
            name,
            record,
            ctx,
            _("зона '{zone}' пуста — показывать нечего").format(zone=ctx.zone),
        )
        return
    target = zone_tabs_module().next_tab(stack, active_window_quiet()) or stack[0]
    if args.dry_run:
        ring = ", ".join(f"0x{win:x}" for win in stack)
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _("план: поднять окно 0x{window:x} зоны '{zone}' (вкладки: {ring})").format(
                window=target, zone=ctx.zone, ring=ring
            )
        )
        return
    deiconify(target)
    if not args.quiet:
        print(_("zone-tiler: окно зоны '{zone}' — 0x{window:x}").format(zone=ctx.zone, window=target))


def _action_mirror(grid, args, name, record, ctx, wa):
    """`builtin:mirror` — обмен окнами с зеркальной зоной.

    Зеркало ищется по геометрии (`zone_actions.find_mirror` по долям зон экрана),
    а не по имени: имена зон человек придумывает сам. Окно-предмет идёт в
    зеркальную зону, окно зеркальной — в предмет; пустая зона вырождает обмен в
    переезд одного окна, обе пусты — no-op (код 0). После обмена **каждой** зоне
    выставляется рамка по числу оказавшихся в ней окон, и она применяется ко всем
    окнам зоны: иначе вкладки разъедутся (у стопки из двух окно стоит на рамке
    вкладки, а не на всей зоне).
    """
    za = actions_module()
    zone = ctx.zone
    if not zone:
        fail(_("zone-tiler: окно вне зон — зеркалить нечего"), EXIT_FAIL)
    axis = record.get("axis") or za.DEFAULT_MIRROR_AXIS
    fractions = {
        name_: grid.fractions(name_, monitor_name=ctx.monitor)
        for name_ in grid.zones_for(ctx.monitor)
    }
    try:
        partner = za.find_mirror(fractions, zone, axis)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if partner is None:
        fail(
            _("zone-tiler: в этой сетке нет зеркальной зоны для '{zone}'").format(zone=zone),
            EXIT_FAIL,
        )

    height = tabs_height(grid)
    client_ids = read_client_list() or []
    frames = _action_frames(client_ids)
    rect = grid.rect(zone, wa, monitor_name=ctx.monitor)
    mirror_rect = grid.rect(partner, wa, monitor_name=ctx.monitor)
    here = _action_windows_in_zone(rect, frames, height)
    there = _action_windows_in_zone(mirror_rect, frames, height)
    # Переезжает предмет действия (окно, которое получил контекст), а если его в
    # зоне уже нет — верхнее окно зоны: «верхнее» и есть видимое.
    window = ctx.window if ctx.window in here else (here[-1] if here else None)
    other = there[-1] if there else None
    if window is None and other is None:
        _action_dry_run_noop(
            args,
            name,
            record,
            ctx,
            _("зоны '{zone}' и '{partner}' пусты — обменивать нечего").format(
                zone=zone, partner=partner
            ),
        )
        return
    final_here = [win for win in here if win != window]
    final_there = [win for win in there if win != other]
    if other is not None:
        final_here.append(other)
    if window is not None:
        final_there.append(window)
    here_frame = in_zone_rect(rect, len(final_here), height)
    there_frame = in_zone_rect(mirror_rect, len(final_there), height)

    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _(
                "план: обмен зон '{zone}' и '{partner}' ({axis}): "
                "{here} -> '{partner}', {there} -> '{zone}'; "
                "рамки x={hx} y={hy} {hw}x{hh} "
                "и x={tx} y={ty} {tw}x{th}"
            ).format(
                zone=zone,
                partner=partner,
                axis=axis,
                here=_action_hex(window),
                there=_action_hex(other),
                hx=here_frame[0],
                hy=here_frame[1],
                hw=here_frame[2],
                hh=here_frame[3],
                tx=there_frame[0],
                ty=there_frame[1],
                tw=there_frame[2],
                th=there_frame[3],
            )
        )
        return

    # Прежние рамки — в историю, чтобы `undo` вернул окна. Снимок берётся до
    # первого движения: после него геометрия уже чужая.
    records = []
    for win, target_zone in ((window, partner), (other, zone)):
        if win is None:
            continue
        try:
            records.append(snapshot_window(win, zone=target_zone))
        except Exception:
            continue  # геометрию не прочитать — окно всё равно двигаем
    x11 = X11()
    try:
        for win in final_here:
            apply_zone(win, here_frame, label=zone, x11=x11, quiet=True)
        for win in final_there:
            apply_zone(win, there_frame, label=partner, x11=x11, quiet=True)
    finally:
        x11.close()
    remember("mirror", records, quiet=args.quiet)
    if not args.quiet:
        print(
            _(
                "zone-tiler: окна зон '{zone}' и '{partner}' обменяны "
                "(окон в зонах: {here} и {there})"
            ).format(zone=zone, partner=partner, here=len(final_here), there=len(final_there))
        )


def _action_hex(window):
    """Окно для печати: `0x…` или «пустая зона» — в плане `--dry-run`."""
    if window is None:
        return _("пустая зона")
    return _("окно 0x{window:x}").format(window=window)


def _action_terminal(grid, args, name, record, ctx, wa):
    """`builtin:terminal` — терминал в зоне (своё окно встаёт на рамку зоны).

    Команда — `command` записи или `zone_actions.DEFAULT_TERMINAL`, разбор и
    подстановки — `zone_actions.expand_command`. Запуск отсоединённый: терминал
    живёт до закрытия окна, и ждать его нельзя. Новое окно ищется в
    `_NET_CLIENT_LIST` до ~5 с (окна под `[ignore]`/служебные типы не берутся) и
    ставится рамкой по числу окон зоны плюс одно — то есть при занятой зоне на
    рамку вкладки. Если окно не появилось — код 1 с понятным сообщением.
    """
    za = actions_module()
    if not ctx.zone:
        fail(
            _(
                "zone-tiler: у действия '{name}' нет зоны — задайте zone в "
                "[action:{name}]"
            ).format(name=name),
            EXIT_FAIL,
        )
    base = grid.rect(ctx.zone, wa, monitor_name=ctx.monitor)
    height = tabs_height(grid)
    client_ids = read_client_list() or []
    count = len(_action_windows_in_zone(base, _action_frames(client_ids), height))
    rect = in_zone_rect(base, count + 1, height)
    command = record.get("command") or za.DEFAULT_TERMINAL
    try:
        argv = za.expand_command(command, ctx)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _(
                "план: запустить {argv} и положить его окно в зону '{zone}': "
                "x={x} y={y} {w}x{h} (окон в зоне: {count} + 1)"
            ).format(
                argv=argv,
                zone=ctx.zone,
                x=rect[0],
                y=rect[1],
                w=rect[2],
                h=rect[3],
                count=count,
            )
        )
        return

    before = set(client_ids)
    _spawn(argv, ctx)
    window = _wait_new_window(before, grid)
    if window is None:
        fail(
            _(
                "zone-tiler: терминал в зоне '{zone}' не появился за "
                "{seconds:g} с — окно не найдено"
            ).format(
                zone=ctx.zone, seconds=ACTION_WAIT_TRIES * ACTION_WAIT_DELAY
            ),
            EXIT_FAIL,
        )
    # Последнее слово за действием: окно могло уже попасть под `[rules]` демона и
    # встать по правилу; рамка зоны действия важнее, поэтому ставится ещё раз.
    apply_zone(window, rect, label=ctx.zone, quiet=args.quiet)
    if not args.quiet:
        print(
            _("zone-tiler: терминал в зоне '{zone}': окно 0x{window:x}").format(
                zone=ctx.zone, window=window
            )
        )


def _wait_new_window(before, grid, tries=ACTION_WAIT_TRIES, delay=ACTION_WAIT_DELAY):
    """Новое окно `_NET_CLIENT_LIST` (не исключённое) или `None` по таймауту.

    `before` — окна до запуска команды. Окно под `[ignore]`/служебного типа не
    берётся: это не терминал, а splash или диалог, и двигать его нельзя.
    Первая проверка — сразу: быстрый терминал успевает появиться до паузы.
    """
    for attempt in range(max(1, int(tries))):
        if attempt:
            time.sleep(delay)
        for win in read_client_list() or []:
            if win in before:
                continue
            if window_is_excluded(win, grid.ignore):
                continue
            return win
    return None


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

    # Toggle: `--off` или уже активный режим — выход, а не вход. Решается до
    # делегирования, потому что вход и выход идут по сокету разными командами.
    exiting = bool(request.get("off")) or bool(state)
    # Запись CLI разворачивает сам CLI: демон мог быть поднят уже в режиме, и
    # тогда эти окна для него — «свёрнутые не им». Разворот идёт до команды
    # демону, иначе тот принял бы их за уже свёрнутые и не запомнил.
    restored, remaining = _focus_restore(state) if state else ([], [])
    if state and not remaining:
        # Запись убирается только когда разворот подтверждён: иначе окно
        # осталось бы свёрнутым без хозяина.
        clear_focus_state()
    if state and remaining:
        _focus_keep(state, remaining)
    if remaining and not args.quiet:
        print(
            _(
                "zone-tiler: {count} окон развернуть не удалось — "
                "запись режима сохранена, повторите focus --off"
            ).format(count=len(remaining)),
            file=sys.stderr,
        )

    if exiting:
        # У демона — свой режим со своей книгой: просим выйти и складываем счёт.
        response = daemon_request({"cmd": "focus", "off": True})
        if response is not None:
            if not response.get("ok"):
                fail(
                    _("zone-tiler: {error}").format(
                        error=response.get("error", _("демон отклонил focus"))
                    ),
                    EXIT_FAIL,
                )
            restored += _focus_ids(response.get("restored"))
        if not args.quiet:
            print(
                _("режим «только эта зона» выключен: развёрнуто окон — {count}").format(
                    count=len(restored)
                )
            )
        return

    # Живой демон — единственный хозяин режима: он видит `_NET_CLIENT_LIST`
    # и умеет довернуть окна, появившиеся после входа. Без демона — разово.
    response = daemon_request(request)
    if response is not None:
        if not response.get("ok"):
            fail(
                _("zone-tiler: {error}").format(
                    error=response.get("error", _("демон отклонил focus"))
                ),
                EXIT_FAIL,
            )
        _print_focus_response(response, quiet=args.quiet)
        return

    _focus_enter(grid, args)


def _focus_ids(values):
    """Идентификаторы окон из ответа демона (`["0x2", …]`) — числа; мусор отброшен."""
    out = []
    for value in values or []:
        win = _focus_window_id({"window": value})
        if win is not None:
            out.append(win)
    return out


def _focus_dry_run(grid, args, state):
    """Показать, что сделал бы `focus`, ничего не двигая и не сворачивая.

    Живой демон — хозяин режима, его книга окон CLI не видна, поэтому при
    доступном сокете спрашивается `status`: иначе `--dry-run` врал бы про
    «ноль свёрнутых» — и на входе, и на выходе.
    """
    if not state:
        response = daemon_request({"cmd": "status"})
        mode = (response or {}).get("focus") if response and response.get("ok") else None
        if mode:
            print(
                _(
                    "режим «только эта зона»: держит демон (зона '{zone}', цель {window}, "
                    "свёрнуто {count}) — выключился бы"
                ).format(
                    zone=mode.get("zone") or _("без зоны"),
                    window=mode.get("window"),
                    count=mode.get("minimized"),
                )
            )
            return
    if args.off or state:
        windows = state.get("windows") or []
        print(
            _("режим «только эта зона»: выключился бы, развернулось бы окон — {count}").format(
                count=len(windows)
            )
        )
        return
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    wa = window_monitor(window, monitors())
    if wa is None:
        fail(
            _("zone-tiler: не удалось определить монитор окна 0x{window:x}").format(
                window=window
            ),
            EXIT_FAIL,
        )
    candidates = focus_candidates(
        focus_entries(read_client_list() or [], monitors()), window,
        current_workspace(), wa,
    )
    todo = [
        win
        for win in candidates
        if not window_is_excluded(win, grid.ignore) and not window_iconic(win)
    ]
    if args.zone:
        x, y, w, h = grid.rect(args.zone, wa)
        print(
            _(
                "режим «только эта зона»: окно 0x{window:x} -> зона '{zone}' "
                "x={x} y={y} {w}x{h}, свернулось бы окон — {count}"
            ).format(
                window=window, zone=args.zone, x=x, y=y, w=w, h=h, count=len(todo)
            )
        )
        return
    print(
        _(
            "режим «только эта зона»: окно 0x{window:x} -> остаётся на месте, "
            "свернулось бы окон — {count}"
        ).format(window=window, count=len(todo))
    )


def _focus_enter(grid, args):
    """Разовый вход в режим без демона: цель в зону (если задана), остальные — свёрнуты."""
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: окно 0x{window:x} под исключением "
                    "([ignore]/служебный тип) — режим не включаю"
                ).format(window=window),
                file=sys.stderr,
            )
        return
    workspace = current_workspace()
    if workspace is None:
        fail(_("zone-tiler: не удалось определить активный рабочий стол"), EXIT_FAIL)
    monitors_ = monitors()
    wa = window_monitor(window, monitors_)
    if wa is None:
        fail(
            _("zone-tiler: не удалось определить монитор окна 0x{window:x}").format(
                window=window
            ),
            EXIT_FAIL,
        )
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
        # Состояние пишется ДО сворачивания: не записалось — ни одно окно ещё не
        # свёрнуто, и команда честно падает вместо того, чтобы оставить окна
        # свёрнутыми без записи (при переполнении диска или недоступном
        # XDG_STATE_HOME). Список окон известен заранее, так что запись ничего
        # не врёт: лишние окна при развороте просто активируются.
        save_focus_state(
            {
                "zone": args.zone,
                "workspace": workspace,
                "monitor": list(wa),
                "target": focus_records([window])[0],
                "windows": focus_records(todo),
            }
        )
        for win in todo:
            iconify(win, x11=x11, quiet=True)
            minimized.append(win)
    except OSError as exc:
        fail(
            _("zone-tiler: не удалось записать состояние режима: {error}").format(error=exc),
            EXIT_FAIL,
        )
    finally:
        x11.close()
    if before is not None:
        remember("apply", [before], quiet=args.quiet)

    if not args.quiet:
        if args.zone:
            print(
                _(
                    "режим «только эта зона»: окно 0x{window:x} -> зона '{zone}', "
                    "свёрнуто окон — {count}"
                ).format(window=window, zone=args.zone, count=len(minimized))
            )
        else:
            print(
                _(
                    "режим «только эта зона»: окно 0x{window:x}, свёрнуто окон — {count}"
                ).format(window=window, count=len(minimized))
            )


def _focus_restore(state, x11=None):
    """Развернуть окна из записи `focus.json`; вернёт `(развёрнутые, остаток)`.

    Окна разворачиваются **по столам записи**: `_NET_ACTIVE_WINDOW` muffin
    игнорирует для окна не на активном столе (проверено дымом), а в файле могут
    сойтись окна разных столов — у каждого окна свой стол (`focus_records`), а
    общий `workspace` записи — только запасной вариант. Перед каждой группой стол
    делается активным.

    Окно, которого больше нет в `_NET_CLIENT_LIST`, и окно с другим `WM_CLASS`
    (XID переиспользован другой программой) пропускаются: развернуть чужое окно
    хуже, чем не развернуть своё. Последней активируется цель — она и должна
    остаться в фокусе. Записей нет — и X не нужен.

    `остаток` — окна, которые так и остались свёрнутыми: вызывающий обязан
    сохранить их запись, а не считать развёрнутыми на веру.
    """
    records = [
        record
        for record in (state.get("windows") or [])
        if _focus_window_id(record) is not None
    ]
    target = state.get("target") if isinstance(state.get("target"), dict) else None
    target_id = _focus_window_id(target) if target else None
    if not records and target_id is None:
        return [], []

    alive = read_client_list()
    alive = set(alive) if alive is not None else None
    own = x11 is None
    if own:
        x11 = X11()
    order, remaining = [], []
    try:
        groups = {}
        for record in records:
            win = _focus_window_id(record)
            if win is None or win == target_id or not _focus_alive(win, record, alive):
                continue
            groups.setdefault(_focus_workspace(record, state), []).append(win)
        if target_id is not None and _focus_alive(target_id, target, alive):
            groups.setdefault(_focus_workspace(target, state), []).append(target_id)
        for workspace in sorted(groups, key=lambda ws: (ws is not None, ws or 0)):
            if workspace is not None:
                switch_workspace(workspace, x11=x11)
            order += groups[workspace]
            remaining += deiconify_all(groups[workspace], x11=x11)
    finally:
        if own:
            x11.close()
    return [win for win in order if win not in remaining], remaining


def _focus_workspace(record, state):
    """Стол окна из записи: свой, иначе общий стол записи; `None` — неизвестен."""
    value = record.get("workspace", state.get("workspace"))
    return value if isinstance(value, int) else None


def _focus_keep(state, remaining):
    """Оставить в `focus.json` только неразвёрнутое — со столом каждого окна сейчас.

    Записи окон строит `focus_records`: она читает `WM_CLASS` и **стол каждого
    окна** заново. Без этого у окна не было бы своего стола (или остался бы
    чужой из старой записи), а разворот возможен только на активном столе —
    следующий `focus --off` опять перешёл бы не туда. Не записалось — старая
    запись остаётся целиком; повторный разворот идемпотентен, так что хуже не
    будет.
    """
    try:
        save_focus_state(
            {
                "zone": state.get("zone"),
                "workspace": state.get("workspace"),
                "monitor": state.get("monitor"),
                "target": None,
                "windows": focus_records(remaining),
            }
        )
    except OSError as exc:
        print(
            _("zone-tiler: не удалось переписать запись режима: {error}").format(error=exc),
            file=sys.stderr,
        )


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
        print(
            _("режим «только эта зона» выключен: развёрнуто окон — {count}").format(
                count=count
            )
        )
        return
    count = len(response.get("minimized") or [])
    print(
        _(
            "режим «только эта зона»: окно {window} -> зона '{zone}', "
            "свёрнуто окон — {count}"
        ).format(window=response.get("window"), zone=response.get("zone"), count=count)
    )


def monitor_arg(args):
    """`--monitor` из аргументов: не задан — 0.

    Так осталось только у `list`: он показывает рабочую область и зоны первого
    экрана, пока `--monitor` не скажет иначе (первую строку разбирает апплет по
    числам `N: x,y WxH`, поэтому опираться на её слова нельзя).
    `apply`/`stack`/`move` без `--monitor` работают на экране окна.
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

    Сетка — профиль **экрана** окна: `[monitors]` → `[workspaces]` → `zones.conf`
    (`grid_for_output`); профиля нет на диске — откат на сетку стола с
    предупреждением, а не отказ укладки.
    """
    nav = picker_nav()
    direction = (args.zone or "").strip().lower()
    if direction not in nav.DIRECTIONS:
        fail(
            _("zone-tiler: move — укажите направление: {directions}").format(
                directions=" | ".join(nav.DIRECTIONS)
            ),
            EXIT_USAGE,
        )
    outputs = monitor_outputs()
    window = args.window or active_window()
    extents = frame_extents(window)
    try:
        frame = read_frame(window, extents)
    except Exception as exc:
        fail(
            _("zone-tiler: не удалось прочитать рамку окна 0x{window:x}: {error}").format(
                window=window, error=exc
            ),
            EXIT_FAIL,
        )
    if args.monitor is None:
        # Монитор окна — по центру его рамки (в рабочей области уже вычтена панель).
        index, output, wa = output_at_point(
            outputs, (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2)
        )
    else:
        index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[index][1]
    # Зона ищется в профиле экрана окна, а не только в профиле его стола.
    grid, _profile = grid_for_output(
        args.conf,
        output,
        fallback=grid,
        workspace=resolve_workspace(args.workspace),
        quiet=args.quiet,
    )
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: окно 0x{window:x} под исключением "
                    "([ignore]/служебный тип) — не двигаю"
                ).format(window=window),
                file=sys.stderr,
            )
        return EXIT_OK
    names = list(grid.zones_for(output))
    zones = [grid.fractions(name, monitor_name=output) for name in names]
    wx, wy, ww, wh = wa
    fx = (frame[0] + frame[2] / 2.0 - wx) / ww
    fy = (frame[1] + frame[3] / 2.0 - wy) / wh
    here, there = move_zone_index(zones, direction, fx, fy)
    if here is None:
        fail(
            _("zone-tiler: у монитора {index} ({output}) нет ни одной зоны").format(
                index=index, output=output
            ),
            EXIT_CONFIG,
        )
    if there == here:
        if not args.quiet:
            print(
                _("zone-tiler: у зоны '{zone}' соседа {direction} нет — окно не двигаю").format(
                    zone=names[here], direction=direction
                ),
                file=sys.stderr,
            )
        return EXIT_OK
    zone = names[there]
    rect = grid.rect(zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(
            _(
                "окно 0x{window:x}: зона '{here}' -> '{zone}': "
                "x={x} y={y} {w}x{h} (монитор {index}: {output})"
            ).format(
                window=window,
                here=names[here],
                zone=zone,
                x=x,
                y=y,
                w=w,
                h=h,
                index=index,
                output=output,
            )
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
    areas = [wa for _output, wa in outputs]
    groups = collections.OrderedDict()
    for win in client_ids:
        wa = window_monitor(win, areas)
        if wa is None:
            continue
        groups.setdefault(wa, []).append(win)
    name_of = {wa: name for name, wa in outputs}
    return [(name_of.get(wa), wa, wins) for wa, wins in groups.items()]


def arrange_via_daemon(assignments, workspace, quiet=False):
    """Разложить окна силами демона: он применит их и разошлёт события.

    Алгоритм выбора окон и зон и память `undo` остаются здесь
    (`choose_assignments` + `remember`), а применяет и запоминает укладки демон:
    только он ведёт трекер, по которому закрытие окна запускает reflow, и только
    он шлёт подписчикам `window-moved`. Без демона `False` — вызывающий
    раскладывает окна сам, как раньше.

    Снимки «до» берутся **до** отправки команды: после неё окна уже переехали, и
    `undo` вернул бы их не туда. В историю идут только те окна, которые демон
    действительно переставил (ответ `arranged`); отказы по отдельным окнам
    печатаются, а не тонут в молчании.
    """
    placements = []
    records = []
    for win, zone, _output, wa, _grid in assignments:
        placements.append(
            {
                "window": hex(win),
                "zone": zone,
                "monitor": [int(v) for v in wa],
            }
        )
        try:
            before = snapshot_window(win, zone=zone)
        except Exception:
            before = None
        if before is not None:
            records.append(before)
    response = daemon_request(
        {"cmd": "arrange", "workspace": workspace, "placements": placements},
        timeout=DAEMON_ARRANGE_TIMEOUT,
    )
    if response is None:
        return False
    if not response.get("ok"):
        fail(
            _("zone-tiler: {error}").format(
                error=response.get("error", _("демон отклонил arrange"))
            ),
            EXIT_FAIL,
        )
    applied = set()
    for value in response.get("arranged") or []:
        try:
            applied.add(int(value, 0))
        except (TypeError, ValueError):
            continue
    for item in response.get("skipped") or []:
        if quiet or not isinstance(item, dict):
            continue
        print(
            _("окно {window}: демон не переставил его ({error})").format(
                window=item.get("window"), error=item.get("error")
            ),
            file=sys.stderr,
        )
    remember(
        "arrange",
        [record for record in records if record["id"] in applied],
        quiet=quiet,
    )
    return True


def cmd_arrange(grid, args):
    """Разложить клиентские окна `_NET_CLIENT_LIST` по зонам.

    Режим `--by` (auto/rules/order) выбирает окна чистой `choose_assignments`;
    окна группируются по монитору, и в каждой группе работает сетка **экрана**
    (`grid_for_output`: профиль из `[monitors]` → `[workspaces]` → `zones.conf`),
    в ней — набор `[zones:<выход>]` или общий `[zones]`. Несколько окон,
    попавших в одну зону одного монитора, получают общую рамку вкладки
    (`in_zone_rect`): зона минус полоса `[tabs] height`; одно окно в зоне
    ставится ровно по зоне. Прежние рамки записываются в историю для `undo`.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail(
            _("zone-tiler: не удалось прочитать _NET_CLIENT_LIST (нужен DISPLAY и xprop)"),
            EXIT_FAIL,
        )
    metas = {win: read_window_meta(win) for win in client_ids}
    outputs = monitor_outputs()
    groups = group_windows_by_monitor(client_ids, outputs)
    workspace = resolve_workspace(args.workspace)
    assignments = []
    for output, wa, wins in groups:
        screen_grid, _profile = grid_for_output(
            args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
        )
        zones = list(screen_grid.zones_for(output))
        for win, zone in choose_assignments(
            wins, metas, zones, screen_grid.rules, args.by, ignore=screen_grid.ignore
        ):
            assignments.append((win, zone, output, wa, screen_grid))
    if not groups:
        if not args.quiet:
            print(
                _("zone-tiler: не удалось определить монитор ни для одного окна"),
                file=sys.stderr,
            )
        return
    if not assignments:
        if not args.quiet:
            print(_("zone-tiler: нет окон, подходящих под раскладку"))
        return
    if not args.dry_run and not args.verify:
        # Живой демон — один хозяин укладки: он применит раскладку, обновит свой
        # трекер (иначе закрытие окна запускало бы reflow не туда) и разошлёт
        # подписчикам `window-moved`. `--verify` и `--dry-run` остаются локальными.
        if arrange_via_daemon(assignments, workspace, quiet=args.quiet):
            return
    x11 = None if args.dry_run else X11()
    totals = collections.Counter((wa, zone) for _win, zone, _output, wa, _grid in assignments)
    height = tabs_height(grid)
    records = []
    try:
        for win, zone, output, wa, screen_grid in assignments:
            base = screen_grid.rect(zone, wa, monitor_name=output)
            rect = in_zone_rect(base, totals[(wa, zone)], height)
            if args.dry_run:
                x, y, w, h = rect
                print(
                    _("окно 0x{window:x} -> зона '{zone}': x={x} y={y} {w}x{h}").format(
                        window=win, zone=zone, x=x, y=y, w=w, h=h
                    )
                )
                continue
            try:
                before = snapshot_window(win, zone=zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(
                        _("окно 0x{window:x}: не удалось переставить ({error})").format(
                            window=win, error=exc
                        ),
                        file=sys.stderr,
                    )
                continue
            if before is not None:
                records.append(before)
    finally:
        if x11 is not None:
            x11.close()
    if not args.dry_run:
        remember("arrange", records, quiet=args.quiet)


def cmd_stack(grid, args):
    """Собрать подвижные клиентские окна в одну зону вкладками.

    Окна встают на одну рамку вкладки (`in_zone_rect`): зона минус полоса
    `[tabs] height`, последнее окно списка — наверху. Одно окно в зоне ставится
    ровно по зоне, без полосы. Переключение вкладок — `tabs-next`/клик по полосе.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail(
            _("zone-tiler: не удалось прочитать _NET_CLIENT_LIST (нужен DISPLAY и xprop)"),
            EXIT_FAIL,
        )
    metas = {win: read_window_meta(win) for win in client_ids}
    windows = movable_windows(client_ids, metas, grid.ignore)
    if not windows:
        if not args.quiet:
            print(_("zone-tiler: нет окон для вкладок"))
        return
    outputs = monitor_outputs()
    if args.monitor is None:
        # Без `--monitor` собираем в зону на экране активного окна: там же, где
        # пользователь смотрит, и с профилем этого экрана.
        mon_index, output, wa = window_output(outputs, active_window_quiet())
    else:
        mon_index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[mon_index][1]
    base = grid.rect(args.zone, wa, monitor_name=output)
    rect = in_zone_rect(base, len(windows), tabs_height(grid))
    x11 = None if args.dry_run else X11()
    records = []
    try:
        for index, win in enumerate(windows):
            if args.dry_run:
                x, y, w, h = rect
                print(
                    _(
                        "окно 0x{window:x} -> зона '{zone}' "
                        "(вкладка {tab} из {total}): x={x} y={y} {w}x{h}"
                    ).format(
                        window=win,
                        zone=args.zone,
                        tab=index + 1,
                        total=len(windows),
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                    )
                )
                continue
            try:
                before = snapshot_window(win, zone=args.zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=args.zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(
                        _("окно 0x{window:x}: не удалось положить во вкладки ({error})").format(
                            window=win, error=exc
                        ),
                        file=sys.stderr,
                    )
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
            print(_("zone-tiler: история раскладок пуста"))
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
                warn(
                    _(
                        "окно 0x{window:x}: класс не совпал ({actual} ≠ {expected}) — "
                        "пропускаю"
                    ).format(window=win, actual=current_class, expected=wm_class)
                )
                continue
            if wm_class and not current_class:
                warn(
                    _("окно 0x{window:x}: уже не существует — пропускаю").format(window=win)
                )
                continue
            if window_excluded(current_class, current_title, window_types(win), grid.ignore):
                warn(
                    _(
                        "окно 0x{window:x}: под исключением ([ignore]/служебный тип) — "
                        "пропускаю"
                    ).format(window=win)
                )
                continue
            try:
                apply_zone(win, tuple(frame), x11=x11, quiet=args.quiet, verify=args.verify)
                restored += 1
            except (Exception, SystemExit) as exc:
                warn(
                    _("окно 0x{window:x}: не удалось вернуть ({error})").format(
                        window=win, error=exc
                    )
                )
    finally:
        if x11 is not None:
            x11.close()
    try:
        history.save([] if args.all else operations[:-1])
    except OSError as exc:
        if not args.quiet:
            print(
                _("zone-tiler: не удалось обновить историю: {error}").format(error=exc),
                file=sys.stderr,
            )
    if not args.quiet:
        if skipped:
            print(
                _("zone-tiler: возвращено окон: {restored}, пропущено: {skipped}").format(
                    restored=restored, skipped=skipped
                )
            )
        else:
            print(_("zone-tiler: возвращено окон: {restored}").format(restored=restored))


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
                _(
                    "zone-tiler: в [keys] зона '{zone}' отсутствует в [zones] "
                    "и в [zones:<выход>]"
                ).format(zone=zone),
                file=sys.stderr,
            )
            continue
        if binding:
            print(f"{zone}\t{binding}")


# ---------------------------------------------------------------- профили

PROFILE_ACTIONS = (
    "list",
    "show",
    "save",
    "use",
    "use-monitors",
    "rm",
    "export",
    "import",
    "gallery",
    "from-gallery",
)


def _run_hotkeys(conf, quiet=False):
    """Перечитать [keys] из conf и обновить сочетания Cinnamon (best-effort)."""
    script = os.path.join(HERE, "setup-hotkeys.py")
    try:
        r = subprocess.run(
            [sys.executable, script, "--conf", conf], text=True, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            _("zone-tiler: сочетания не обновлены: {error}").format(error=exc),
            file=sys.stderr,
        )
        return
    if r.returncode:
        tail = (r.stderr or r.stdout).strip()
        print(
            _("zone-tiler: сочетания не обновлены ({details})").format(details=tail),
            file=sys.stderr,
        )
        return
    if not quiet:
        first = (r.stdout or r.stderr).strip().splitlines()
        line = first[0] if first else _("сочетания обновлены")
        print(_("zone-tiler: {message}").format(message=line))


def _profile_export(zc, args, name, conf):
    """``profile export``: JSON-профиля в stdout или в файл."""
    target = (args.target or "").strip()
    if target and args.json:
        fail(
            _("zone-tiler: profile export — или --json, или файл, не оба сразу"),
            EXIT_USAGE,
        )
    data = zc.profile_to_dict(name, conf)
    if not target:
        sys.stdout.write(zc.dumps_profile(data))
        return
    path = zc.write_profile_json(data, target)
    print(
        _("zone-tiler: профиль «{profile}» выгружен: {path} ({zones} зон)").format(
            profile=zc.sanitize_profile_name(name),
            path=path,
            zones=zc.profile_zone_total(data),
        )
    )


def _profile_import(zc, args, path, conf):
    """``profile import``: JSON-файл → профиль ``templates/<имя>.conf``."""
    data = zc.read_profile_json(path)
    written = zc.profile_from_dict(data, (args.as_name or "").strip() or None, conf)
    name = os.path.basename(written)[: -len(zc.PROFILE_EXT)]
    print(
        _("zone-tiler: профиль «{profile}» импортирован: {path} ({zones} зон)").format(
            profile=name,
            path=written,
            zones=zc.profile_zone_total(data),
        )
    )


def section_pairs(lines):
    """Пары ``(ключ, значение)`` из тела секции в порядке строк.

    Тело — строки как их отдаёт ``zone_config.load_extra_sections`` (или готовые
    пары): комментарии, пустые строки и строки без ``=`` пропускаются, инлайн-
    комментарий после пробела отбрасывается, как у ``ConfigParser``. Нужно для
    слияния ``[rules]``/``[ignore]``: сопоставлять надо селектор, а не текст строки.
    """
    pairs = []
    for item in lines or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            key, value = str(item[0]).strip(), str(item[1]).strip()
        else:
            line = str(item).strip()
            if not line or line[0] in "#;":
                continue
            line = re.sub(r"\s[#;].*$", "", line)
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
        if key:
            pairs.append((key, value))
    return pairs


def monitor_screen_map(conf_path):
    """``[(выход, профиль)]`` из ``[monitors]`` в порядке строк, регистр — из файла.

    :func:`zone_config.load_monitor_map` валидирует секцию и санирует имена
    профилей, но ``ConfigParser`` опускает регистр ключей, а секции
    ``[zones:<выход>]`` принято писать так, как выход печатает ``xrandr``
    (``HDMI-1``). Поэтому имя берём из текста секции (публичный
    ``load_extra_sections``), а профиль — из проверенной карты; сравнение —
    без учёта регистра, как у резолвера. Пустая секция — ``[]``.
    """
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, как у grid_for_workspace

    mapping = zc.load_monitor_map(conf_path)
    if not mapping:
        return []
    by_lower = {str(key).casefold(): name for key, name in mapping.items()}
    rows, seen = [], set()
    body = zc.load_extra_sections(conf_path, sections=[zc.MONITORS_SECTION]).get(
        zc.MONITORS_SECTION, []
    )
    for key, _value in section_pairs(body):
        output = key.strip()
        lowered = output.casefold()
        profile = by_lower.get(lowered)
        if profile is None or lowered in seen:
            continue
        seen.add(lowered)
        rows.append((output, profile))
    for lowered, profile in mapping.items():
        if lowered not in seen:
            rows.append((lowered, profile))
    return rows


def _render_planned_config(zc, conf, default, monitor_sets, extra):
    """Текст, который записал бы ``use-monitors``, без записи в настоящий конфиг.

    Пишем во временный файл через тот же ``zone_config._write_config``: иначе
    `--dry-run` пришлось бы повторять его правила сборки вручную, и показанное
    разошлось бы с тем, что реально легло бы на диск.
    """
    fd, tmp = tempfile.mkstemp(prefix="gridsnap-plan-", suffix=".conf")
    try:
        with open(conf, encoding="utf-8") as src:
            data = src.read()
        with os.fdopen(fd, "w", encoding="utf-8") as dst:
            fd = None
            dst.write(data)
        zc._write_config(tmp, default, monitor_sets, extra)
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _profile_screen_zone_set(output, zones, monitor_sets):
    """Набор зон экрана из профиля: ``[zones:<выход>]``, иначе общий ``[zones]``."""
    for name, chunk in (monitor_sets or {}).items():
        if str(name).casefold() == str(output).casefold():
            return list(chunk)
    return list(zones or [])


def _profile_zone_chunks(zones, monitor_sets):
    """Наборы зон профиля в порядке файла: общий, затем ``[zones:<выход>]``."""
    return [zones or []] + list((monitor_sets or {}).values())


def merge_monitor_profiles(screens):
    """Слить профили экранов в снимок активного конфига (чистая функция).

    ``screens`` — ``[(выход, профиль, zones, rules, monitor_sets, ignore)]`` в
    порядке строк ``[monitors]``; зоны — ``Zone`` с привязками ``[keys]``.

    Набор по умолчанию — ``[zones]`` **первого** экрана, у которого есть зоны (его
    ``[zones:<выход>]``, если общего набора в профиле нет). ``[zones:<выход>]``
    пишется для каждого экрана: свой набор профиля, иначе его общий ``[zones]``.
    ``[rules]``, ``[ignore]`` и ``[keys]`` сливаются по всем экранам: где селектор
    или имя зоны расходятся, выигрывает первый экран, а расхождение попадает в
    ``overrides`` (ничего не теряется молча). Правило на зону, которой нет в
    итоговом объединении наборов, отбрасывается в ``dropped_rules``: иначе
    ``Grid`` отказал бы кодом 3 и конфиг стал бы нечитаемым целиком.
    """
    default = None
    monitor_sets = {}
    zone_names = set()
    bindings = {}  # имя зоны -> (выход, профиль, сочетание)
    key_reported = set()
    raw_rules = []  # (селектор, зона, выход, профиль)
    raw_ignore = []  # (селектор, заметка, выход, профиль)
    overrides = []
    usable = []
    for output, profile, zones, rules, sets, ignore in screens:
        own = _profile_screen_zone_set(output, zones, sets)
        if not own:
            continue
        usable.append((output, profile))
        monitor_sets[output] = [zone.copy() for zone in own]
        if default is None:
            default = [zone.copy() for zone in (list(zones or []) or own)]
        for chunk in _profile_zone_chunks(zones, sets):
            for zone in chunk:
                zone_names.add(zone.name)
                binding = (zone.binding or "").strip()
                if not binding:
                    continue
                owner = bindings.get(zone.name)
                if owner is None:
                    bindings[zone.name] = (output, profile, binding)
                elif owner[2] != binding and zone.name not in key_reported:
                    key_reported.add(zone.name)
                    overrides.append(
                        _(
                            "клавиша зоны «{zone}»: «{first_key}» (профиль «{first_profile}», "
                            "экран {first_output}) вместо «{key}» (профиль «{profile}», "
                            "экран {output}) — оставлена первая"
                        ).format(
                            zone=zone.name,
                            first_key=owner[2],
                            first_profile=owner[1],
                            first_output=owner[0],
                            key=binding,
                            profile=profile,
                            output=output,
                        )
                    )
        for selector, target in section_pairs(rules):
            raw_rules.append((selector, target, output, profile))
        for selector, note in section_pairs(ignore):
            raw_ignore.append((selector, note, output, profile))

    dropped, seen_dropped = [], set()
    merged_rules = []
    rule_owner = {}
    for selector, target, output, profile in raw_rules:
        if target not in zone_names:
            if (selector, target) not in seen_dropped:
                seen_dropped.add((selector, target))
                dropped.append((selector, target))
            continue
        owner = rule_owner.get(selector)
        if owner is None:
            rule_owner[selector] = (target, output, profile)
            merged_rules.append((selector, target))
        elif owner[0] != target:
            overrides.append(
                _(
                    "правило для «{selector}»: зона «{first_zone}» (профиль "
                    "«{first_profile}», экран {first_output}) вместо «{zone}» (профиль "
                    "«{profile}», экран {output}) — оставлено первое"
                ).format(
                    selector=selector,
                    first_zone=owner[0],
                    first_profile=owner[2],
                    first_output=owner[1],
                    zone=target,
                    profile=profile,
                    output=output,
                )
            )

    merged_ignore = []
    ignore_owner = {}
    for selector, note, output, profile in raw_ignore:
        owner = ignore_owner.get(selector)
        if owner is None:
            ignore_owner[selector] = (note, output, profile)
            merged_ignore.append((selector, note))
        elif owner[0] != note:
            overrides.append(
                _(
                    "исключение «{selector}»: заметка «{first_note}» (профиль "
                    "«{first_profile}», экран {first_output}) вместо «{note}» (профиль "
                    "«{profile}», экран {output}) — оставлена первая"
                ).format(
                    selector=selector,
                    first_note=owner[0],
                    first_profile=owner[2],
                    first_output=owner[1],
                    note=note,
                    profile=profile,
                    output=output,
                )
            )

    # `[keys]` пишется по первому появлению имени зоны: проставляем победившую
    # привязку всем записанным наборам, иначе проигравший экран перебил бы её.
    for chunk in [default] + list(monitor_sets.values()):
        for zone in chunk:
            owner = bindings.get(zone.name)
            zone.binding = owner[2] if owner else ""
    return {
        "default": default,
        "monitor_sets": monitor_sets,
        "rules": merged_rules,
        "ignore": merged_ignore,
        "dropped_rules": dropped,
        "overrides": overrides,
        "screens": usable,
    }


def _profile_use_monitors(zc, args, conf):
    """``profile use-monitors``: снимок профилей экранов из ``[monitors]``.

    Пишет в **активный** конфиг то, что видят все, кто профилей не читает
    (пикер, редактор, апплет): ``[zones]`` первого экрана, ``[zones:<выход>]``
    каждого экрана, слитые ``[rules]``/``[ignore]``/``[keys]``. Прочие секции
    (``[grid]``, ``[monitors]``, ``[workspaces]``, ``[daemon]``, …) переживают
    запись дословно. Экран без файла профиля пропускается с предупреждением:
    строка ``[monitors]`` остаётся и заработает, когда профиль вернут.
    """
    if (args.name or "").strip():
        fail(
            _(
                "zone-tiler: profile use-monitors не берёт имени профиля: экраны и "
                "профили задаёт секция [monitors]"
            ),
            EXIT_USAGE,
        )
    if not os.path.exists(conf):
        # Пустой [monitors] читается и из несуществующего файла, но писать снимок
        # некуда: опечатка в --conf не должна выглядеть как «нечего менять».
        fail(_("zone-tiler: не найден конфиг {path}").format(path=conf), EXIT_CONFIG)
    monitor_map = monitor_screen_map(conf)
    if not monitor_map:
        if not args.quiet:
            print(
                _("zone-tiler: в [monitors] нет ни одного экрана — активный конфиг не меняю")
            )
        return
    screens = []
    for output, name in monitor_map:
        safe = zc.sanitize_profile_name(name)
        try:
            path = zc.profile_path(name, conf)
        except ValueError:
            path = None
        if path is None or not os.path.exists(path):
            print(
                _(
                    "zone-tiler: профиля «{profile}» нет на диске: экран {output} "
                    "пропущен, строка [monitors] остаётся и заработает, когда профиль вернут"
                ).format(profile=safe, output=output),
                file=sys.stderr,
            )
            continue
        zones, rules, sets, ignore = zc.read_profile_sets(name, conf)
        if not zones and not sets:
            print(
                _(
                    "zone-tiler: в профиле «{profile}» нет ни одной зоны: экран {output} "
                    "пропущен"
                ).format(profile=safe, output=output),
                file=sys.stderr,
            )
            continue
        screens.append((output, name, zones, rules, sets, ignore))
    if not screens:
        if not args.quiet:
            print(
                _(
                    "zone-tiler: ни один экран из [monitors] не нашёл своего профиля — "
                    "активный конфиг не меняю"
                )
            )
        return
    plan = merge_monitor_profiles(screens)
    if not args.quiet:
        for message in plan["overrides"]:
            print(_("zone-tiler: переопределено: {message}").format(message=message))
    for selector, zone in plan["dropped_rules"]:
        print(
            _(
                "zone-tiler: правило «{selector} = {zone}» пропущено: зоны '{zone}' нет "
                "в итоговом наборе — иначе конфиг стал бы невалидным"
            ).format(selector=selector, zone=zone),
            file=sys.stderr,
        )
    host = zc.load_extra_sections(conf)
    host.pop("rules", None)
    host.pop("ignore", None)
    if plan["rules"]:
        host["rules"] = plan["rules"]
    if plan["ignore"]:
        host["ignore"] = plan["ignore"]
    if args.dry_run:
        if not args.quiet:
            sys.stdout.write(
                _render_planned_config(
                    zc, conf, plan["default"], plan["monitor_sets"], host
                )
            )
        return
    zc._write_config(conf, plan["default"], plan["monitor_sets"], host)
    if not args.quiet:
        for output, name in plan["screens"]:
            print(
                _(
                    "zone-tiler: экран {output} — профиль «{profile}»: {zones} зон"
                ).format(
                    output=output,
                    profile=zc.sanitize_profile_name(name),
                    zones=len(plan["monitor_sets"][output]),
                )
            )
        print(
            _(
                "zone-tiler: активный конфиг обновлён: экранов {screens}, "
                "правил {rules}, исключений {ignore}"
            ).format(
                screens=len(plan["screens"]),
                rules=len(plan["rules"]),
                ignore=len(plan["ignore"]),
            )
        )
    if not args.no_hotkeys:
        _run_hotkeys(conf, quiet=args.quiet)


def _profile_gallery(zc, args):
    """`profile gallery` — список готовых раскладок из поставки.

    Печатается по строке на раскладку (id, название, сколько зон, описание) либо
    JSON при `--json` — как у остальных команд `profile`: id нужен скриптам, название
    и описание — человеку.
    """
    layouts = zc.gallery_layouts()
    if not layouts:
        fail(_("zone-tiler: галерея раскладок пуста"), EXIT_FAIL)
    if args.json:
        print(json.dumps(layouts, ensure_ascii=False, indent=2))
        return
    for item in layouts:
        print(
            _("{id}\t{title}\tзон: {count}\t{description}").format(
                id=item["id"],
                title=item["title"],
                count=len(item["zones"]),
                description=item["description"],
            )
        )


def _profile_from_gallery(zc, args, conf):
    """`profile from-gallery <id> [--name <имя>]` — скопировать раскладку в свой профиль.

    Копия, а не ссылка: дальше человек правит её как обычный профиль (и может
    экспортировать/перенести). Существующий профиль не перезаписывается (даже с тем
    же именем) — это чужой файл, и тихая потеря раскладки была бы неприятным
    сюрпризом: выбери другое имя или убери старый (`profile rm <имя>`).
    """
    gid = (args.name or "").strip()
    if not gid:
        fail(
            _("zone-tiler: profile from-gallery — укажите id раскладки (см. profile gallery)"),
            EXIT_USAGE,
        )
    try:
        path = zc.install_gallery_layout(
            gid,
            name=(args.as_name or None),
            force=False,
            conf_path=conf,
        )
    except zc.GalleryError as exc:
        fail(_("zone-tiler: {error}").format(error=exc), EXIT_CONFIG)
    name = os.path.splitext(os.path.basename(path))[0]
    print(
        _("профиль «{name}» создан из готовой раскладки «{id}»: {path}").format(
            name=name, id=gid, path=path
        )
    )
    print(_("применить: zone-tiler.py profile use {name}").format(name=name))


def cmd_profile(args):
    """Профили раскладок: list/show/save/use/rm/export/import (zone_config лениво)."""
    import zone_config as zc

    action = (args.zone or "").strip()
    if action not in PROFILE_ACTIONS:
        fail(
            _(
                "zone-tiler: profile — укажите действие: list | show <имя> | save <имя> | "
                "use <имя> | use-monitors | rm <имя> | export <имя> [файл | --json] | "
                "import <файл> [--name <имя>] | gallery | from-gallery <id> [--name <имя>]"
            ),
            EXIT_USAGE,
        )
    conf = args.conf
    if action == "list":
        for name in zc.list_profiles(conf):
            print(name)
        return
    if action == "gallery":
        _profile_gallery(zc, args)
        return
    if action == "from-gallery":
        _profile_from_gallery(zc, args, conf)
        return
    if action == "use-monitors":
        try:
            _profile_use_monitors(zc, args, conf)
        except ValueError as exc:
            fail(f"zone-tiler: {exc}", EXIT_USAGE)
        except FileNotFoundError as exc:
            fail(f"zone-tiler: {exc}", EXIT_CONFIG)
        return

    name = (args.name or "").strip()
    if not name:
        what = _("файл JSON-профиля") if action == "import" else _("имя профиля")
        fail(
            _("zone-tiler: profile {action} — укажите {what}").format(action=action, what=what),
            EXIT_USAGE,
        )

    try:
        if action == "show":
            sys.stdout.write(zc.profile_text(name, conf))
        elif action == "save":
            zones = zc.load_zones(conf)
            extra = zc.load_extra_sections(conf, sections=["rules", "ignore"])
            path = zc.save_profile(
                name, zones, extra.get("rules", []), conf, ignore=extra.get("ignore")
            )
            print(
                _("zone-tiler: профиль «{profile}» сохранён: {path}").format(
                    profile=zc.sanitize_profile_name(name), path=path
                )
            )
        elif action == "rm":
            zc.remove_profile(name, conf)
            print(
                _("zone-tiler: профиль «{profile}» удалён").format(
                    profile=zc.sanitize_profile_name(name)
                )
            )
        elif action == "export":
            _profile_export(zc, args, name, conf)
        elif action == "import":
            _profile_import(zc, args, name, conf)
        else:  # use
            zones = zc.apply_profile(name, conf)
            print(
                _("zone-tiler: профиль «{profile}» применён: {zones} зон").format(
                    profile=zc.sanitize_profile_name(name), zones=len(zones)
                )
            )
            if not args.no_hotkeys:
                _run_hotkeys(conf, quiet=args.quiet)
    except zc.ProfileJsonError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    except ValueError as exc:
        fail(f"zone-tiler: {exc}", EXIT_USAGE)
    except FileNotFoundError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)


def main(argv=None):
    """Точка входа CLI: отказ X-сервера остаётся сообщением и кодом 1.

    Раньше об этом заботился сам `X11.__init__` через `fail`; теперь соединение
    открывает оконный слой и отвечает `WindowBackendError`, поэтому перевод в
    прежний текст и код возврата — здесь, на границе CLI.
    """
    try:
        return _main(argv)
    except window_backend.WindowBackendError:
        _x11_failed()


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description=_("Раскладка окон по зонам сетки (Cinnamon/X11)"),
        # Без сокращений: иначе удалённый `--offset` совпадает с `--off` и
        # вместо «неизвестный флаг» человек получает ошибку про команду '20'.
        allow_abbrev=False,
        epilog=_(
            "Примеры: zone-tiler.py arrange --by order; zone-tiler.py stack main; "
            "zone-tiler.py tabs-next; zone-tiler.py undo --all; "
            "zone-tiler.py action mirror; "
            "zone-tiler.py profile use work; zone-tiler.py profile use-monitors"
        ),
    )
    ap.add_argument(
        "command",
        choices=[
            "list",
            "apply",
            "move",
            "focus",
            "action",
            "keys",
            "arrange",
            "stack",
            "tabs-next",
            "undo",
            "profile",
        ],
        help=_(
            "list/apply/keys — как раньше; move — перебросить окно в соседнюю зону; "
            "focus — режим «только эта зона» (окно в зону, остальные свёрнуты; "
            "повторно — выход); action — выполнить действие из [action:<имя>] "
            "(встроенное или своя команда); arrange — разложить окна по зонам; "
            "stack — собрать "
            "окна в зону вкладками; tabs-next — поднять следующую вкладку зоны "
            "(клавиша [tabs]); undo — вернуть рамки из истории; profile — свои "
            "профили раскладок (зоны + правила + клавиши)"
        ),
    )
    ap.add_argument(
        "zone",
        nargs="?",
        help=_("имя зоны для apply/stack, направление для move или действие profile"),
    )
    ap.add_argument(
        "name", nargs="?", help=_("имя профиля или файл JSON для profile import")
    )
    ap.add_argument(
        "target",
        nargs="?",
        help=_("profile export: файл для JSON (без него и без --json — stdout)"),
    )
    ap.add_argument(
        "--monitor",
        default=None,
        help=_(
            "монитор для list/apply/stack/move: имя выхода (HDMI-1) или номер "
            "(без флага — экран окна у apply/stack/move, первый у list)"
        ),
    )
    ap.add_argument(
        "--workspace",
        metavar="N|current",
        default=None,
        type=workspace_spec,
        help=_(
            "рабочий стол для list/apply/move/arrange/stack: номер (EWMH, с нуля), "
            "current (сетка активного стола — по умолчанию) или conf "
            "(всегда сетка zones.conf, без профилей столов)"
        ),
    )
    ap.add_argument(
        "--window", type=lambda s: int(s, 0), help=_("id окна (по умолчанию активное)")
    )
    ap.add_argument(
        "--by",
        choices=["auto", "rules", "order"],
        default="auto",
        help=_(
            "arrange: выбор окон — правила и остаток (auto), только правила, или по порядку"
        ),
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help=_("undo: откатить всю историю, а не последнюю операцию"),
    )
    ap.add_argument(
        "--no-hotkeys",
        action="store_true",
        help=_(
            "profile use/use-monitors: не регистрировать сочетания клавиш после активации"
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help=_(
            "profile export/gallery: печатать JSON (у export — в stdout, а не в файл)"
        ),
    )
    ap.add_argument(
        "--name",
        dest="as_name",
        metavar=_("ИМЯ"),
        help=_("profile import/from-gallery: имя профиля (import — по умолчанию из JSON)"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=_(
            "только посчитать рамку (у profile use-monitors — показать конфиг), "
            "ничего не меняя"
        ),
    )
    ap.add_argument(
        "--off",
        action="store_true",
        help=_(
            "focus: явно выйти из режима «только эта зона» (развернуть свёрнутое режимом)"
        ),
    )
    ap.add_argument(
        "--move",
        action="store_true",
        help=_("keys: сочетания перемещения в соседнюю зону из секции [move]"),
    )
    ap.add_argument(
        "--verify", action="store_true", help=_("перечитать реальную геометрию")
    )
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--version", action="version", version=f"zone-tiler {VERSION}")
    # `--offset` удалён вместе с каскадом. Без явной проверки argparse съедает
    # его значение как команду и ругается на «20», а не на флаг: человек
    # видит причину только в этом сообщении.
    for value in sys.argv[1:] if argv is None else argv:
        if value == "--offset" or value.startswith("--offset="):
            ap.error(
                _(
                    "--offset удалён: окна одной зоны больше не сдвигаются каскадом, "
                    "а стоят вкладками на общей рамке (переключение — tabs-next)"
                )
            )
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
    # `tabs-next` не нужен ни конфиг, ни сетка: демон сам выбирает зону по
    # активному окну, а высота полосы его не касается.
    if args.command == "tabs-next":
        cmd_tabs_next(args)
        return EXIT_OK

    # list/apply/arrange/stack работают сеткой своего стола: `--workspace N` или
    # активный стол по умолчанию; сетка берётся через grid_for_workspace, поэтому
    # стол без профиля молча живёт в общем [zones]. Экран важнее стола: если в
    # конфиге есть привязки `[monitors]`, у apply/stack адресат — экран окна
    # (`target_output`), и сетка берётся из профиля этого экрана. Мониторы читаются
    # только при наличии привязок: без них это лишний поход в X (и раньше проверки
    # зоны).
    workspace = resolve_workspace(args.workspace)
    profile = workspace_profile(args.conf, workspace)
    grid = grid_for_workspace(args.conf, workspace)
    output = None
    if args.command in ("apply", "stack") and monitor_bindings(args.conf):
        output = target_output(args)
        if output is not None:
            grid, profile = grid_for_output(
                args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
            )
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
    # `action` — как `focus`: ни зоны ни окна от CLI не требует (зона бывает в
    # записи действия, а действие бывает и вовсе без зоны).
    if args.command == "action":
        cmd_action(grid, args)
        return EXIT_OK
    if not args.zone:
        fail(
            _("zone-tiler: укажите зону, например: zone-tiler.py apply left"), EXIT_USAGE
        )
    # Зона ищется в наборе показанной сетки (экран + стол + общий [zones]); нет её
    # там — это ошибка конфига: конфиг и профиль разошлись.
    require_zone(grid, args.zone, workspace=workspace, profile=profile, output=output)
    if args.command == "stack":
        cmd_stack(grid, args)
        return EXIT_OK
    cmd_apply(grid, args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

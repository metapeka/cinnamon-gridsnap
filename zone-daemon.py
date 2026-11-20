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
  zone-daemon.py                 # слушать события до Ctrl-C (и поднять IPC-сокет)
  zone-daemon.py --verbose       # писать в stdout, что делает демон
  zone-daemon.py --once          # обработать одно изменение _NET_CLIENT_LIST и выйти
  zone-daemon.py --conf PATH
  zone-daemon.py --socket PATH   # свой путь UNIX-сокета IPC
  zone-daemon.py --no-socket     # без IPC (только автоукладка)

IPC — локальный UNIX-сокет (по умолчанию `$XDG_RUNTIME_DIR/gridsnap/daemon.sock`,
права 0600): по одной JSON-команде на соединение, ответ — тоже JSON-строка.
Команды: `status`, `apply`, `reload`, `quit`; говорит с сокетом клиент
`gridsnap-ctl`. Кадрирование и диспетчер живут в чистом `zone_ipc.py`.
"""

import argparse
import collections
import ctypes
import importlib.util
import json
import os
import select
import sys
import time
from typing import cast

VERSION = "0.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))

REFLOW_OFF = "off"
REFLOW_NEIGHBORS = "neighbors"

# Задержка перед единственным повтором чтения метаданных: `_NET_WM_NAME` иногда
# появляется уже после того, как окно попало в `_NET_CLIENT_LIST`.
META_RETRY_DELAY = 0.2

# Коды событий X11.
PROPERTY_NOTIFY = 28

# PropertyChangeMask: PropertyNotify о свойствах root (в т.ч. _NET_CLIENT_LIST).
PROPERTY_CHANGE_MASK = 1 << 22

# Таймаут ожидания единственного PropertyNotify в режиме `--once`.
ONCE_TIMEOUT = 2.0

# XEvent — объединение размером 192 байта на LP64.
EVENT_BUFFER = ctypes.c_char * 192


def _load_sibling(name, filename):
    """Загрузить соседний hyphen-модуль (zone-tiler.py) без зависимости от sys.path."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_zone_tiler", "zone-tiler.py")
zone_reflow = _load_sibling("gridsnap_zone_reflow", "zone_reflow.py")
zone_ipc = _load_sibling("gridsnap_zone_ipc", "zone_ipc.py")


# ------------------------------------------------------------------ метаданные


# Общие с раскладчиком фильтры и чтение EWMH — один источник истины, чтобы
# `arrange`/`stack` в zone-tiler.py и демон отсеивали окна одинаково.
IGNORED_WINDOW_TYPES = tiler.IGNORED_WINDOW_TYPES
IGNORED_TYPE_TAILS = tiler.IGNORED_TYPE_TAILS
CLIENT_LIST_ATOM = tiler.CLIENT_LIST_ATOM
window_types = tiler.window_types
is_ignored_type = tiler.is_ignored_type
read_client_list = tiler.read_client_list
read_window_center = tiler.read_window_center
monitor_for_point = tiler.monitor_for_point

# Метаданные окна, которые нужны демону. `window_type` — набор имён
# `_NET_WM_WINDOW_TYPE_*` (может быть пустым). Namedtuple: и кортеж, и атрибуты.
Meta = collections.namedtuple("Meta", "wm_class wm_title window_type")


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


# ----------------------------------------------------------------------- демон


class Daemon:
    """Ядро демона без X: вся реакция на события — в `on_map`/`on_destroy`/`on_unmap`.

    Зависимости инъектируются, поэтому сценарии гоняются юнит-тестами без
    дисплея:

      conf_path  — путь к zones.conf (по умолчанию — как у раскладчика);
      apply      — callable(window, rect, label=..., quiet=...) (по умолчанию —
                   `zone-tiler.apply_zone` с общим соединением `self.x11`,
                   которое ставит `serve`; переданное чужое не закрывается);
      read_meta  — callable(window) -> Meta (по умолчанию `read_window_meta`);
      workarea   — принудительная одна рабочая область: callable() -> (x, y, w, h)
                   либо сам кортеж (одноэкранный режим и тесты). При `None`
                   монитор окна определяется по `monitors` и `window_center`;
      monitors   — callable() -> список (x, y, w, h) рабочих областей мониторов
                   (по умолчанию `tiler.monitors`, вызывается лениво);
      window_center — callable(window) -> (px, py) либо None (по умолчанию
                   `read_window_center`, вызывается лениво);
      verbose    — печатать ли, что делает демон.

    `windows` — трекер `window id -> имя зоны`: им же отсекаются повторные
    появления окна (свернуть/развернуть не переставляют окно). `win_monitor`
    хранит рабочую область, на которой окно уложено; `filled` — покрытые зоны
    по мониторам.

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
        monitors=None,
        window_center=None,
        verbose=False,
    ):
        self.conf_path = conf_path or tiler.default_conf()
        self.grid = tiler.Grid(self.conf_path)
        self.x11 = None  # соединение кладёт serve(); чужое для apply
        self.apply = apply or self._default_apply
        self.read_meta = read_meta or read_window_meta
        self.workarea = workarea
        self.monitors = monitors if monitors is not None else tiler.monitors
        self.window_center = window_center or read_window_center
        self.verbose = verbose
        self.reflow = read_reflow(self.grid.conf)
        self.windows = {}
        # Рабочая область, на которой уложено окно: window id -> (x, y, w, h).
        # Нужна reflow, чтобы не расширять соседа с другого монитора.
        self.win_monitor = {}
        # Зоны, покрытые расширенным соседом (reflow=neighbors): пара
        # (рабочая область, освобождённая зона) -> окно-держатель. Покрытие
        # ключуется монитором: одна и та же зона на двух экранах независима.
        self.filled = {}
        # Все id из последнего `_NET_CLIENT_LIST` (и уложенные, и без правила) —
        # чтобы не читать метаданные повторно на каждой реконсиляции.
        self.seen = set()
        self._wa_cache = None
        self._mons_cache = None
        # Команда `quit` по сокету: цикл `serve` проверяет флаг и выходит.
        self.stop_requested = False

    # -- вспомогательное

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    def _forced_workarea(self):
        """Принудительная одна рабочая область; `None`, если её не получить."""
        if self._wa_cache is None:
            try:
                wa = self.workarea() if callable(self.workarea) else self.workarea
                self._wa_cache = tuple(cast("tuple[int, int, int, int]", wa))
            except (Exception, SystemExit) as exc:
                self.log(f"не удалось получить рабочую область: {exc}")
                return None
        return self._wa_cache

    def _monitors(self):
        """Список рабочих областей мониторов, закэшированный.

        `tiler.monitors()` через `fail()` бросает `SystemExit` при отказе
        X-сервера — это не подкласс `Exception`. Ошибку демон переживает:
        логирует и возвращает `None`, а вызывающий пропускает применение.
        Неудачу не кэсим, успех — кэсим. `KeyboardInterrupt` не перехватывается.
        """
        if self._mons_cache is None:
            try:
                mons = cast(
                    "list[tuple[int, int, int, int]]",
                    self.monitors() if callable(self.monitors) else self.monitors,
                )
                self._mons_cache = [tuple(wa) for wa in mons]
            except (Exception, SystemExit) as exc:
                self.log(f"не удалось получить мониторы: {exc}")
                return None
        return self._mons_cache

    def _monitor_of(self, win):
        """Рабочая область монитора окна; `None`, если её не определить.

        При заданном `workarea` всегда возвращается она (одноэкранный режим).
        Иначе берётся область, содержащая центр окна; если центр не попал ни в
        одну — ближайшая по центру. Неизвестное положение окна (`xwininfo`
        не ответил) даёт `None`: лучше не трогать окно, чем угадать монитор 0.
        """
        if self.workarea is not None:
            return self._forced_workarea()
        monitors = self._monitors()
        if not monitors:
            return None
        try:
            point = self.window_center(win)
        except (Exception, SystemExit) as exc:
            self.log(f"не удалось определить положение окна 0x{win:x}: {exc}")
            return None
        if point is None:
            return None
        return monitor_for_point(monitors, point)

    def _default_apply(self, win, rect, label="", quiet=False):
        """Замыкание по умолчанию: вкладывает окно через общее X-соединение демона.

        `self.x11` может быть задано позже (`serve` ставит его перед
        реконсиляцией), поэтому берётся из атрибута в момент вызова, а не
        запоминается. Подменённый `apply` это замыкание не задевает.
        """
        tiler.apply_zone(win, rect, label=label, quiet=quiet, x11=self.x11)

    def _apply(self, win, rect, zone):
        """Вложить окно, не уронив долгоживущий демон из-за одной ошибки.

        Ловим и `SystemExit`: `fail()` в layout-коде бросает его при отказе
        X-сервера (например, исчерпаны соединения), а он не подкласс
        `Exception`. `KeyboardInterrupt` не перехватываем.
        """
        try:
            self.apply(win, rect, label=zone, quiet=True)
            return True
        except (Exception, SystemExit) as exc:  # окно могло исчезнуть/отказ X
            self.log(f"не удалось вложить окно 0x{win:x} в зону '{zone}': {exc}")
            return False

    def _window_for_zone(self, zone, wa=None):
        """Окно в зоне `zone`; при `wa` — именно на этой рабочей области."""
        for win, occupied in self.windows.items():
            if occupied != zone:
                continue
            if wa is None or self.win_monitor.get(win) == wa:
                return win
        return None

    def _restore_holder(self, wa, zone):
        """Вернуть соседа, расширившегося на `zone`, к его рамке из конфига.

        `zone` на рабочей области `wa` занята новым окном, поэтому покрытие
        больше не нужно. Если окна-держателя уже нет в трекере, запись снимается
        без перемещений. При неудачном `apply` запись остаётся: попробуем при
        следующем окне.
        """
        key = (wa, zone)
        holder = self.filled.get(key)
        if holder is None:
            return
        holder_zone = self.windows.get(holder)
        if holder_zone is None or holder_zone not in self.grid.zones:
            # Держателя нет или его зона исчезла после reload — покрытие снимаем.
            del self.filled[key]
            return
        if self._apply(holder, self.grid.rect(holder_zone, wa), holder_zone):
            del self.filled[key]
            self.log(f"возвращаю зону '{holder_zone}' к исходной рамке")

    # -- IPC: обработчики команд сокета

    def build_dispatcher(self):
        """Диспетчер команд сокета с инъекцией обработчиков демона.

        Конкретные обработчики можно подменить в тестах; сам диспетчер
        (`zone_ipc.Dispatcher`) не знает ни о X, ни о конфиге.
        """
        return zone_ipc.Dispatcher(
            {
                "status": self.ipc_status,
                "apply": self.ipc_apply,
                "reload": self.ipc_reload,
                "quit": self.ipc_quit,
            }
        )

    def _monitor_list(self):
        """Рабочие области для команд IPC: принудительная или все мониторы."""
        if self.workarea is not None:
            wa = self._forced_workarea()
            return [wa] if wa else []
        return self._monitors() or []

    def ipc_status(self, request=None):
        """Состояние демона: зоны, окно→зона, монитор, профиль, reflow.

        Пиксельные рамки зон даются на каждый монитор (`zones_px`), потому что
        зона — доля рабочей области, а у монитора своя область.
        """
        zones = {name: list(self.grid.fractions(name)) for name in sorted(self.grid.zones)}
        monitors = self._monitor_list()
        zones_px = {
            name: [list(self.grid.rect(name, wa)) for wa in monitors] for name in zones
        }
        windows = []
        for win, zone in sorted(self.windows.items()):
            wa = self.win_monitor.get(win)
            windows.append(
                {"window": hex(win), "zone": zone, "monitor": list(wa) if wa else None}
            )
        filled = [
            {"monitor": list(wa), "zone": zone, "holder": hex(holder)}
            for (wa, zone), holder in sorted(
                self.filled.items(), key=lambda item: (item[0][1], item[1])
            )
        ]
        return {
            "version": VERSION,
            "conf": self.conf_path,
            "reflow": self.reflow,
            # Профили раскладок — волна 3; поле есть заранее для совместимости.
            "profile": None,
            "monitors": [list(wa) for wa in monitors],
            "zones": zones,
            "zones_px": zones_px,
            "windows": windows,
            "filled": filled,
        }

    def _monitor_for_command(self, request, window):
        """Рабочая область для `apply`: явный номер монитора или монитор окна.

        Без явного `monitor` (и без читаемого положения окна) берётся первый
        монитор — команду дал пользователь, и отказ хуже приблизительного
        выбора.
        """
        monitors = self._monitor_list()
        index = request.get("monitor")
        if index is not None:
            index = int(index)
            if not 0 <= index < len(monitors):
                raise ValueError(f"монитора {index} нет (всего {len(monitors)})")
            return monitors[index]
        wa = self._monitor_of(window)
        if wa is not None:
            return wa
        return monitors[0] if monitors else None

    def ipc_apply(self, request):
        """Вложить окно в зону по команде сокета (по умолчанию — активное).

        Успешная укладка запоминается в трекере демона: закрытие окна затем
        штатно запускает reflow, а не оставляет призрак.
        """
        zone = request.get("zone")
        if not isinstance(zone, str) or zone not in self.grid.zones:
            raise ValueError(
                f"нет зоны '{zone}'. Есть: " + ", ".join(sorted(self.grid.zones))
            )
        window = request.get("window")
        if window is None:
            window = tiler.active_window()  # нет активного окна -> SystemExit
        elif isinstance(window, str):
            window = int(window, 0)
        else:
            window = int(window)
        wa = self._monitor_for_command(request, window)
        if wa is None:
            raise ValueError(f"не удалось определить монитор окна 0x{window:x}")
        rect = self.grid.rect(zone, wa)
        if not self._apply(window, rect, zone):
            raise ValueError(f"не удалось вложить окно 0x{window:x} в зону '{zone}'")
        self.windows[window] = zone
        self.win_monitor[window] = wa
        self._restore_holder(wa, zone)
        return {
            "window": hex(window),
            "zone": zone,
            "monitor": list(wa),
            "rect": list(rect),
        }

    def ipc_reload(self, request=None):
        """Перечитать zones.conf: сетка, правила и reflow; кэши сбросить."""
        self.grid = tiler.Grid(self.conf_path)
        self.reflow = read_reflow(self.grid.conf)
        self._wa_cache = None
        self._mons_cache = None
        self.log(f"конфиг перечитан: {self.conf_path}")
        return {
            "conf": self.conf_path,
            "zones": sorted(self.grid.zones),
            "reflow": self.reflow,
        }

    def ipc_quit(self, request=None):
        """Попросить цикл демона завершиться после текущего события."""
        self.stop_requested = True
        self.log("получена команда quit — завершаюсь")
        return {"bye": True}

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
        wa = self._monitor_of(win)
        if wa is None:
            return  # монитор окна недоступен: не падаем и окно не трогаем
        rect = self.grid.rect(zone, wa)
        if not self._apply(win, rect, zone):
            return  # не запоминаем: окно не уложено, зону занятой не считаем
        self.windows[win] = zone
        self.win_monitor[win] = wa
        self.log(f"окно 0x{win:x} -> зона '{zone}'")
        # Зона снова занята — расширившийся на неё сосед больше не нужен.
        self._restore_holder(wa, zone)

    def on_destroy(self, win):
        """Окно исчезло из `_NET_CLIENT_LIST` — снять с трекера и, если нужно, reflow."""
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        self.log(f"окно 0x{win:x} ушло из зоны '{zone}'")
        # Держатель исчез: снять записи о зонах, которые он покрывал.
        for key, holder in list(self.filled.items()):
            if holder == win:
                del self.filled[key]
        if self.reflow != REFLOW_NEIGHBORS or wa is None:
            return
        # reflow — в пределах монитора ушедшего окна: окна на других экранах в
        # занятых зонах не учитываются, соседа с другого монитора не двигаем.
        occupied = {
            other_zone
            for other, other_zone in self.windows.items()
            if self.win_monitor.get(other) == wa
        }
        zones = {name: self.grid.fractions(name) for name in self.grid.zones}
        changes = zone_reflow.reflow(zones, zone, occupied)
        if not changes:
            return
        for name, frac in changes.items():
            target = self._window_for_zone(name, wa)
            if target is None:
                continue
            fx, fy, fw, fh = frac
            if not self._apply(target, fractions_to_pixels(fx, fy, fw, fh, wa), name):
                continue  # окно не переехало: зону покрытой не считаем
            self.filled[(wa, zone)] = target
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

    Возвращает True, если событие относилось к списку клиентов. Если список
    прочитать не удалось (`read_clients()` дал `None`), реконсиляция
    пропускается: трекер и `seen` не трогаем.
    """
    ev = ctypes.cast(buf, ctypes.POINTER(XPropertyEvent)).contents
    if ev.atom != client_list_atom:
        return False
    client_ids = read_clients()
    if client_ids is None:
        log = getattr(dae, "log", None)
        if log is not None:
            log("_NET_CLIENT_LIST недоступен — реконсиляцию пропускаю")
        return True
    dae.reconcile(client_ids)
    return True


def _reconcile_if_known(dae, read_clients):
    """Прочитать список клиентов и свести состояние; `None` — пропустить."""
    client_ids = read_clients()
    if client_ids is None:
        dae.log("_NET_CLIENT_LIST недоступен — реконсиляцию пропускаю")
        return
    dae.reconcile(client_ids)


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


def _serve_ipc(dae, ipc):
    """Принять и обслужить команды сокета; ошибки не роняют демон.

    Ответы и ошибки пишутся в журнал (`--verbose`). Исключение приёма/разбора
    логируется и глушится: сокет — вспомогательный канал, X-цикл дороже.
    """
    try:
        responses = ipc.accept_all()
    except (Exception, SystemExit) as exc:
        dae.log(f"ошибка IPC-сокета: {exc}")
        return
    for response in responses:
        if response.get("ok"):
            dae.log("IPC: " + json.dumps(response, ensure_ascii=False))
        else:
            dae.log("IPC-ошибка: " + str(response.get("error", "?")))


def serve(dae, x11=None, once=False, read_clients=None, once_timeout=ONCE_TIMEOUT, ipc=None):
    """Цикл событий X11: PropertyNotify о `_NET_CLIENT_LIST` -> реконсиляция.

    Подписка — `PropertyChangeMask` на root. На старте один раз читается текущий
    список клиентов и укладываются уже открытые окна; дальше то же происходит на
    каждый PropertyNotify про `_NET_CLIENT_LIST`.

    `x11` и `read_clients` можно подменить (тогда реальный дисплей не нужен).
    Своё соединение `serve` кладёт в `dae.x11`, чтобы `apply_zone` переиспользовал
    его на все окна и не открывал новое на каждый `apply` (утечка соединений).
    `None`, возвращённое `read_clients` (список недоступен), — сигнал пропустить
    реконсиляцию, не трогая трекер.
    `once=True` — обработать один PropertyNotify и выйти; если событий нет —
    подождать `once_timeout` секунд и выйти (для smoke-проверок).
    `ipc` — слушающий сокет `zone_ipc.Server`: его дескриптор добавляется в тот же
    `select`, а команды обслуживаются между X-событиями; команда `quit` ставит
    `dae.stop_requested`, и цикл завершается.
    """
    if x11 is None:
        x11 = tiler.X11()
    if read_clients is None:
        read_clients = read_client_list
    dae.x11 = x11  # общее соединение: apply_zone не должен его закрывать
    _bind_event_functions(x11.x)
    x11.x.XSelectInput(x11.dpy, x11.root, PROPERTY_CHANGE_MASK)
    x11.x.XSync(x11.dpy, 0)
    client_list_atom = x11.atom(CLIENT_LIST_ATOM)
    fd = x11.x.XConnectionNumber(x11.dpy)
    watch = [fd]
    if ipc is not None:
        watch.append(ipc.fileno())
    dae.log(f"демон слушает X11 (root 0x{x11.root:x}, reflow={dae.reflow})")
    if ipc is not None:
        dae.log(f"IPC-сокет: {getattr(ipc, 'path', '?')}")
    try:
        # Раскладываем окна, уже открытые к запуску (вход в готовую сессию).
        _reconcile_if_known(dae, read_clients)
        while True:
            if not x11.x.XPending(x11.dpy):
                ready, _, _ = select.select(
                    watch, [], [], once_timeout if once else None
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
            if ipc is not None:
                _serve_ipc(dae, ipc)
            if getattr(dae, "stop_requested", False):
                break
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
    ap.add_argument(
        "--socket",
        default=None,
        help="путь UNIX-сокета IPC (по умолчанию $XDG_RUNTIME_DIR/gridsnap/daemon.sock)",
    )
    ap.add_argument("--no-socket", action="store_true", help="не поднимать IPC-сокет")
    ap.add_argument("--version", action="version", version=f"zone-daemon {VERSION}")
    args = ap.parse_args(argv)

    dae = Daemon(args.conf, verbose=args.verbose)
    server = None
    if not args.no_socket:
        path = args.socket or zone_ipc.default_socket_path()
        try:
            server = zone_ipc.Server(path, dae.build_dispatcher())
        except OSError as exc:
            print(f"zone-daemon: IPC-сокет: {exc}", file=sys.stderr)
            return tiler.EXIT_FAIL
    try:
        serve(dae, once=args.once, ipc=server)
    finally:
        if server is not None:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

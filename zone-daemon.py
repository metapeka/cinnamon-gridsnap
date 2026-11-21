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

Рабочие столы: демон помнит активный стол (`_NET_CURRENT_DESKTOP`) и стол
каждого уложенного окна (`_NET_WM_DESKTOP`). Сетка зон берётся для стола окна
через `zone-tiler.grid_for_workspace()` (профиль из `[workspaces]`, иначе общий
`[zones]`); «липкое» окно (`0xFFFFFFFF`) живёт в общем наборе. Окна невидимых
столов не двигаются никогда: reflow работает только на текущем столе.

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
import re
import select
import subprocess
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

# Активный рабочий стол (EWMH, на root). Читается тем же PropertyNotify-циклом,
# что и список клиентов: маска PropertyChangeMask на root уже стоит.
CURRENT_DESKTOP_ATOM = "_NET_CURRENT_DESKTOP"

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
window_excluded = tiler.window_excluded
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


def window_rect(win):
    """Рамка окна с компенсацией `_NET_FRAME_EXTENTS` (для памяти об укладке).

    Та же геометрия, что `apply_zone` читает при `--verify`, поэтому сравнение
    «окно на месте / вышло из зоны» не зависит от рамки оконного менеджера.
    """
    return tiler.read_frame(win, tiler.frame_extents(win))


# ---------------------------------------------------------------------- конфиг


def read_reflow(conf):
    """Режим reflow из `[daemon]`. Нет секции/значения или мусор -> `off`."""
    try:
        value = (conf.get("daemon", "reflow", fallback="off") or "").strip().lower()
    except Exception:
        return REFLOW_OFF
    return value if value in (REFLOW_OFF, REFLOW_NEIGHBORS) else REFLOW_OFF


# --------------------------------------------------------------- память укладок

# Память о том, куда демон положил окно, живёт между запусками: иначе стартовая
# реконсиляция после перезапуска снова утащила бы в зону окно, которое
# пользователь оттуда вынес. Это состояние, а не конфиг (его не переносят) и не
# кэш (его вправе вычистить чистильщик) — отсюда `XDG_STATE_HOME`.
PLACED_STATE_VERSION = 1

# Насколько рамка окна вправе разойтись с запомненной, чтобы окно всё ещё
# считалось стоящим в своей зоне. Пара пикселей — дрожание окна/приложения после
# укладки; настоящий переезд пользователя заметно больше.
PLACED_TOLERANCE = 8

# `_NET_MOVERESIZE_WINDOW` — клиентское сообщение, WM применяет его не мгновенно.
# Если первое чтение застало старую рамку, ждём столько и перечитываем ещё раз,
# иначе окно тут же сочли бы «вышедшим из зоны».
PLACED_RETRY_DELAY = 0.15


def placed_path():
    """Путь памяти об укладках: `$XDG_STATE_HOME/gridsnap/placed.json`."""
    return os.path.join(tiler.state_home(), "gridsnap", "placed.json")


def read_session_id():
    """Идентификатор X-сессии: окно `_NET_SUPPORTING_WM_CHECK`.

    Память об укладках не должна переезжать в новую X-сессию: там XID раздаются
    заново, и старая запись может «попасть» на чужое окно того же класса.
    Не читается — `None`, тогда проверка выключена.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
    return found.group(1) if found else None


def frame_matches(actual, placed, tolerance=PLACED_TOLERANCE):
    """Совпадают ли рамки в пределах допуска. Чистая функция — под тесты."""
    if actual is None or placed is None or len(actual) != 4 or len(placed) != 4:
        return False
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(actual, placed))


def _state_window_id(raw):
    """XID из ключа файла состояния; не число — `None`."""
    try:
        win = int(raw)
    except (TypeError, ValueError):
        return None
    return win if win > 0 else None


def _state_frame(value):
    """Рамка `[x, y, w, h]` из JSON; кривая — `None`."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError):
        return None


def _state_record(record):
    """Запись об укладке из JSON; кривая — `None`.

    Обязательны `frame` и непустая `zone`: без зоны запись бесполезна — некуда
    возвращать окно. `monitor`/`workspace` необязательны (липкое окно,
    неопознанный стол), `wm_class` нужен, чтобы отличить переиспользованный XID.
    """
    if not isinstance(record, dict):
        return None
    frame = _state_frame(record.get("frame"))
    zone = record.get("zone")
    if frame is None or not isinstance(zone, str) or not zone:
        return None
    monitor = _state_frame(record.get("monitor"))
    workspace = record.get("workspace")
    if workspace is not None:
        try:
            workspace = int(workspace)
        except (TypeError, ValueError):
            workspace = None
    return {
        "frame": frame,
        "wm_class": str(record.get("wm_class") or ""),
        "zone": zone,
        "monitor": monitor,
        "workspace": workspace,
    }


def load_placed_state(path, session=None):
    """Память об укладках из файла: `{XID: запись}`; нет файла/битый — `{}`.

    Файл состояния вспомогательный: демон обязан подняться и без него, поэтому
    ошибки чтения не поднимаются наверх, а кривые записи отбрасываются поодиночке.
    Записи другой X-сессии (если и файл, и аргумент знают её id) отбрасываются
    целиком: XID в новой сессии раздаются заново.
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != PLACED_STATE_VERSION:
        return {}
    file_session = data.get("session")
    if session is not None and file_session is not None and file_session != session:
        return {}
    windows = data.get("windows")
    if not isinstance(windows, dict):
        return {}
    result = {}
    for raw_win, record in windows.items():
        win = _state_window_id(raw_win)
        parsed = _state_record(record)
        if win is not None and parsed is not None:
            result[win] = parsed
    return result


def save_placed_state(path, state, session=None):
    """Записать память об укладках атомарно (`.tmp` + `os.replace`)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = {"version": PLACED_STATE_VERSION, "windows": state}
    if session is not None:
        data["session"] = session
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


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


def workspace_label(workspace):
    """Стол для журнала: номер EWMH (с нуля) или пометка «неизвестен»."""
    return "неизвестен" if workspace is None else f"#{workspace}"


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
      current_workspace — callable() -> номер активного стола (по умолчанию
                   `tiler.current_workspace`); читается один раз в `__init__`,
                   потом на каждый PropertyNotify о `_NET_CURRENT_DESKTOP`;
      window_workspace — callable(window) -> номер стола окна либо None
                   (по умолчанию `tiler.window_workspace_for_zones`; None же
                   у «липкого» окна — оно живёт в общем наборе зон);
      verbose    — печатать ли, что делает демон.

    `windows` — трекер `window id -> имя зоны`: им же отсекаются повторные
    появления окна (свернуть/развернуть не переставляют окно). `win_monitor`
    хранит рабочую область, на которой окно уложено; `win_workspace` — стол,
    на котором оно уложено (`None` — липкое/неизвестный стол); `filled` —
    покрытые зоны по столу и монитору. Стол учитывается везде, где считается
    занятость: окно стола 2 не делает зону занятой для reflow на столе 1.

    Точка входа из X11-контура — `reconcile(client_ids)`: список id текущих
    клиентов из `_NET_CLIENT_LIST`. Новые id (которых нет в `seen`) проходят
    через `on_map`, исчезнувшие из трекера — через `on_destroy`. `seen` хранит
    все id последнего списка, включая не подошедшие под правила, — тогда их
    метаданные не перечитываются на каждой реконсиляции.

    Закрытие окна (`on_destroy`) снимает его с трекера и, если включён
    `reflow=neighbors`, перестраивает раскладку — но только когда стол
    закрытого окна виден (`self.workspace`); иначе состояние чистится, а окна
    на других столах не двигаются. Скрытие окна (`on_unmap`: сворачивание,
    смена воркспейса) в `_NET_CLIENT_LIST` ничего не меняет, поэтому
    реконсиляция его не видит: окно остаётся в трекере, соседей не двигаем.
    Смена активного стола (`on_workspace_change`) обновляет только
    `self.workspace` — окна не переставляются.
    """

    def __init__(
        self,
        conf_path=None,
        apply=None,
        read_meta=None,
        workarea=None,
        monitors=None,
        window_center=None,
        current_workspace=None,
        window_workspace=None,
        state_path=None,
        read_rect=None,
        session_id=None,
        iconify=None,
        deiconify=None,
        read_clients=None,
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
        self.read_current_workspace = current_workspace or tiler.current_workspace
        self.read_window_workspace = window_workspace or tiler.window_workspace_for_zones
        # Рамку окна читает память об укладках (сравнение «окно на месте/ушло»).
        self.read_rect = read_rect or window_rect
        # Сворачивание/разворот и список клиентов — инъекция как у apply/read_meta:
        # тесты не должны трогать X, а демон в работе зовёт общие примитивы
        # раскладчика (`tiler.iconify`/`tiler.deiconify`).
        self.iconify = iconify or tiler.iconify
        self.deiconify = deiconify or tiler.deiconify
        self.read_clients = read_clients or read_client_list
        # Путь файла состояния; `None` — только в памяти (тесты, утилитный запуск).
        self.state_path = state_path
        # Идентификатор X-сессии: запись из другой сессии (те же XID, но чужая)
        # не подхватывается. `None` — проверка выключена.
        self.session_id = session_id
        self.verbose = verbose
        self.reflow = read_reflow(self.grid.conf)
        self.windows = {}
        # Рабочая область, на которой уложено окно: window id -> (x, y, w, h).
        # Нужна reflow, чтобы не расширять соседа с другого монитора.
        self.win_monitor = {}
        # Стол, на котором уложено окно: window id -> номер стола или None
        # (липкое окно). Окно другого стола не делает зону занятой на этом.
        self.win_workspace = {}
        # Рамка, которую демон применил: window id -> (x, y, w, h). По ней видно,
        # что пользователь вынес окно из зоны (окно уже не там, где его положили).
        self.placed = {}
        # `WM_CLASS` уложенного окна — записи состояния нужно отличить
        # переиспользованный XID при усыновлении после перезапуска.
        self.win_class = {}
        # Окна, которые пользователь вынес из своих зон: window id -> запись.
        # Их демон больше не двигает до переоткрытия окна (новый XID).
        self.detached = {}
        # Память прошлого запуска: разбирается на первой же реконсиляции.
        self._pending = load_placed_state(state_path, session_id) if state_path else {}
        # Зоны, покрытые расширенным соседом (reflow=neighbors): тройка
        # (стол, рабочая область, освобождённая зона) -> окно-держатель.
        # Покрытие ключуется столом и монитором: одна и та же зона на разных
        # столах и экранах независима, и reflow одного стола не трогает другой.
        self.filled = {}
        # Сетки столов, посчитанные по `[workspaces]`: стол -> Grid. Кэш
        # сбрасывается `reload`, чтобы новый профиль подхватился без рестарта.
        self._grids = {}
        # Активный стол (нумерация EWMH с нуля); None — не определить.
        self.workspace = self._read_current_workspace()
        # Все id из последнего `_NET_CLIENT_LIST` (и уложенные, и без правила) —
        # чтобы не читать метаданные повторно на каждой реконсиляции.
        self.seen = set()
        # Режим «только эта зона» (None — выключен): словарь с полями
        # `zone`, `target`, `workspace`, `monitor` и множеством `minimized` —
        # окна, которые свернул именно режим (их и только их он разворачивает).
        self.focus = None
        self._wa_cache = None
        self._mons_cache = None
        # Команда `quit` по сокету: цикл `serve` проверяет флаг и выходит.
        self.stop_requested = False

    # -- вспомогательное

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    # -- рабочие столы

    def _read_current_workspace(self):
        """Активный стол из `_NET_CURRENT_DESKTOP`; `None` — не определить.

        `tiler.current_workspace()` при отказе `xprop` возвращает `None` сам,
        но подменённый читатель может и бросить: стол — не повод падать, при
        неизвестном столе reflow просто не двигает окна.
        """
        try:
            return self.read_current_workspace()
        except (Exception, SystemExit) as exc:
            self.log(f"не удалось определить активный стол: {exc}")
            return None

    def _window_workspace(self, win):
        """Стол окна для выбора сетки; `None` — «липкое» или не опознано."""
        try:
            return self.read_window_workspace(win)
        except (Exception, SystemExit) as exc:
            self.log(f"не удалось определить стол окна 0x{win:x}: {exc}")
            return None

    def _visible(self, workspace):
        """Виден ли стол окна сейчас — то есть можно ли двигать это окно.

        `None` (липкое окно или неопознанный стол) — видно всегда. Неизвестный
        же активный стол (`self.workspace is None`) видимым не делает никого:
        двигать окна вслепую хуже, чем не двигать.
        """
        if workspace is None:
            return True
        return workspace == self.workspace

    def _grid_for(self, workspace=None):
        """Сетка рабочего стола: профиль из `[workspaces]` или общий набор зон.

        `workspace=None` (липкое/неопознанное окно) — обычный `Grid(conf_path)`:
        общий набор зон, как зафиксировано контрактом `grid_for_workspace`.
        Сетки кэшируются по столу, чтобы не перечитывать конфиг и профиль на
        каждое окно; `reload` сбрасывает кэш. Битый `[workspaces]` или профиль
        (в т.ч. `SystemExit` из `fail()`) не роняют демон: берётся общий набор.
        """
        if workspace is None:
            return self.grid
        cached = self._grids.get(workspace)
        if cached is not None:
            return cached
        try:
            grid = tiler.grid_for_workspace(self.conf_path, workspace)
        except (Exception, SystemExit) as exc:
            self.log(
                f"не удалось взять сетку стола {workspace_label(workspace)}: {exc} — "
                "беру общий набор"
            )
            grid = self.grid
        self._grids[workspace] = grid
        return grid

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
        `Exception`. `KeyboardInterrupt` не перехватываем. Удачная укладка
        запоминается: позже по её рамке видно, что окно вышло из зоны.
        """
        try:
            self.apply(win, rect, label=zone, quiet=True)
        except (Exception, SystemExit) as exc:  # окно могло исчезнуть/отказ X
            self.log(f"не удалось вложить окно 0x{win:x} в зону '{zone}': {exc}")
            return False
        self._remember_placement(win, rect)
        return True

    # -- память об укладках

    def _read_rect(self, win):
        """Текущая рамка окна; `None`, если её не прочитать (нет X, окно ушло)."""
        try:
            rect = self.read_rect(win)
        except (Exception, SystemExit) as exc:
            self.log(f"не удалось прочитать рамку окна 0x{win:x}: {exc}")
            return None
        if rect is None:
            return None
        try:
            return tuple(int(v) for v in rect)
        except (TypeError, ValueError):
            return None

    def _remember_placement(self, win, rect):
        """Запомнить рамку укладки: по ней видно, что окно вышло из зоны.

        Берётся фактическая рамка (рамку мог поправить WM), а если её не
        прочитать — запрошенная. WM применяет `_NET_MOVERESIZE_WINDOW` не
        мгновенно, поэтому несовпавшую рамку перечитываем ещё раз: иначе окно,
        которое демон только что уложил, тут же сочли бы «вышедшим из зоны».
        Файл состояния пишут вызывающие, когда трекер уже обновлён.
        """
        frame = self._read_rect(win)
        if frame is not None and not frame_matches(frame, rect):
            time.sleep(PLACED_RETRY_DELAY)
            again = self._read_rect(win)
            if again is not None:
                frame = again
        self.placed[win] = frame or tuple(int(v) for v in rect)
        self.detached.pop(win, None)

    def _placement_record(self, win, frame):
        """Запись об укладке для файла состояния."""
        wa = self.win_monitor.get(win)
        return {
            "frame": [int(v) for v in frame],
            "wm_class": self.win_class.get(win, ""),
            "zone": self.windows.get(win),
            "monitor": [int(v) for v in wa] if wa else None,
            "workspace": self.win_workspace.get(win),
        }

    def _save_state(self):
        """Сохранить память об укладках (сбой записи демон не роняет)."""
        if not self.state_path:
            return
        state = {
            str(win): self._placement_record(win, frame)
            for win, frame in self.placed.items()
        }
        for win, record in self.detached.items():
            state[str(win)] = dict(record)
        try:
            save_placed_state(self.state_path, state, self.session_id)
        except OSError as exc:
            self.log(f"не удалось сохранить память об укладках: {exc}")

    def _detach(self, win):
        """Снять окно с трекера: оно больше не в своей зоне (пользователь вынес).

        Зона освобождается (записи `filled` на окно-держателя снимаются), окно
        уходит в `detached` — само оно уже не вернётся ни на новом появлении,
        ни после перезапуска демона (пока XID не сменится).
        """
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        workspace = self.win_workspace.pop(win, None)
        frame = self.placed.pop(win, None)
        wm_class = self.win_class.pop(win, None)
        for key, holder in list(self.filled.items()):
            if holder == win:
                del self.filled[key]
        if frame is not None:
            self.detached[win] = {
                "frame": [int(v) for v in frame],
                "wm_class": wm_class or "",
                "zone": zone,
                "monitor": [int(v) for v in wa] if wa else None,
                "workspace": workspace,
            }
        self._save_state()
        self.log(f"окно 0x{win:x} вышло из зоны '{zone}' — больше его не двигаю")

    def _refresh_detached(self):
        """Заметить окна, которые пользователь вынес из своих зон.

        Сравнивается текущая рамка окна с той, что демон применил: разошлась
        больше допуска — окно ушло. Его записи снимаются, зона освобождается для
        reflow, и заново демон его не укладывает. Рамка не читается — считаем,
        что окно на месте (не двигать хуже, чем пропустить переезд).
        """
        for win in list(self.windows):
            if not self._visible(self.win_workspace.get(win)):
                continue  # окно невидимого стола не двигаем — и ушедшим не считаем
            placed = self.placed.get(win)
            if placed is None:
                continue
            actual = self._read_rect(win)
            if actual is None:
                continue
            if not frame_matches(actual, placed):
                self._detach(win)

    def _zone_rect_for(self, win, record):
        """Рамка зоны записи на её мониторе; `None` — не посчитать.

        Нужна, чтобы отличить «окно стоит в своей зоне» от «окно расширено
        reflow на соседнюю»: во втором случае записанная рамка — объединение, а
        не зона.
        """
        wa = tuple(record["monitor"]) if record.get("monitor") else None
        if wa is None:
            wa = self._monitor_of(win)
        if wa is None:
            return None
        try:
            grid = self._grid_for(record.get("workspace"))
            return grid.rect(record["zone"], wa)
        except (Exception, SystemExit) as exc:
            self.log(
                f"не удалось посчитать зону '{record['zone']}' записи 0x{win:x}: {exc}"
            )
            return None

    def _adopt_placement(self, win, record):
        """Учесть запись прошлого запуска: усыновить окно или пометить ушедшим.

        Рамка совпала с запомненной — окно всё ещё там, куда его положили: берём
        под присмотр без `apply`. Разошлась — пользователь его вынес, и демон
        больше окно не двигает. Если записанная рамка — не зона, а объединение с
        соседней (расширенный reflow сосед), окно тоже возвращается в свою зону
        через `on_map`. `False` — запись не про это окно (XID переиспользован
        другим приложением или класс не сошёлся): окно идёт обычным путём
        `on_map`.
        """
        try:
            meta = self.read_meta(win)
        except (Exception, SystemExit) as exc:
            self.log(f"не удалось прочитать метаданные 0x{win:x}: {exc}")
            return False
        wm_class = getattr(meta, "wm_class", "") or ""
        recorded_class = record.get("wm_class") or ""
        if recorded_class and wm_class and recorded_class != wm_class:
            return False
        actual = self._read_rect(win)
        if actual is not None and frame_matches(actual, record["frame"]):
            zone_rect = self._zone_rect_for(win, record)
            if zone_rect is not None and not frame_matches(actual, zone_rect):
                # Рамка не своя зона: это сосед, расширенный reflow, а книга
                # покрытий (`filled`) в файле состояния не живёт. Даём окну
                # пройти обычный путь `on_map` — правило вернёт его в зону из
                # конфига, и новое окно не ляжет поверх.
                self.log(
                    f"окно 0x{win:x} стоит не в своей зоне '{record['zone']}' — "
                    "укладываю по правилу"
                )
                return False
            self.windows[win] = record["zone"]
            if record.get("monitor"):
                self.win_monitor[win] = tuple(record["monitor"])
            self.win_workspace[win] = record.get("workspace")
            self.placed[win] = tuple(record["frame"])
            self.win_class[win] = wm_class or recorded_class
            self.log(
                f"окно 0x{win:x} осталось в зоне '{record['zone']}' — беру под присмотр"
            )
            return True
        self.detached[win] = dict(record)
        self.log(
            f"окно 0x{win:x} вышло из зоны '{record['zone']}' — само не вернётся"
        )
        return True

    def _apply_pending(self, current):
        """Разобрать память прошлого запуска по текущему списку клиентов."""
        pending, self._pending = self._pending, {}
        if not pending:
            return
        for win, record in pending.items():
            if win not in current:
                continue  # окно закрылось, пока демон не работал
            if not self._adopt_placement(win, record):
                self.log(f"запись о 0x{win:x} не подошла — укладываю по правилу")

    def _prune_state(self, current):
        """Забыть окна, которых больше нет в `_NET_CLIENT_LIST`."""
        dropped = False
        for win in list(self.placed):
            if win not in current:
                self.placed.pop(win, None)
                self.win_class.pop(win, None)
                dropped = True
        for win in list(self.detached):
            if win not in current:
                del self.detached[win]
                dropped = True
        if dropped:
            self._save_state()

    def _window_for_zone(self, zone, wa=None):
        """Окно в зоне `zone` на видимом столе; при `wa` — именно на этой области.

        Окна других столов не кандидаты: reflow двигает только видимый стол.
        """
        for win, occupied in self.windows.items():
            if occupied != zone:
                continue
            if wa is not None and self.win_monitor.get(win) != wa:
                continue
            if not self._visible(self.win_workspace.get(win)):
                continue
            return win
        return None

    def _restore_holder(self, workspace, wa, zone):
        """Вернуть соседа, расширившегося на `zone`, к его рамке из конфига.

        `zone` на рабочей области `wa` занята новым окном стола `workspace`,
        поэтому покрытие больше не нужно. Покрытие живёт на видимом столе
        (reflow туда и только туда), поэтому окно другого стола его не трогает.
        Если окна-держателя уже нет в трекере, запись снимается без
        перемещений. При неудачном `apply` запись остаётся: попробуем при
        следующем окне.
        """
        if not self._visible(workspace):
            return  # окно невидимого стола: покрытие видимого стола не наше
        key = (self.workspace, wa, zone)
        holder = self.filled.get(key)
        if holder is None:
            return
        holder_zone = self.windows.get(holder)
        grid = self._grid_for(self.win_workspace.get(holder))
        if holder_zone is None or holder_zone not in grid.all_zone_names():
            # Держателя нет или его зоны больше нет в наборе — покрытие снимаем.
            del self.filled[key]
            return
        if self._apply(holder, grid.rect(holder_zone, wa), holder_zone):
            del self.filled[key]
            self._save_state()
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
                "focus": self.ipc_focus,
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
        """Состояние демона: зоны, окно→зона, стол, монитор, профиль, reflow.

        Пиксельные рамки зон даются на каждый монитор (`zones_px`), потому что
        зона — доля рабочей области, а у монитора своя область. Поля
        `windows[].zone/window/monitor` — прежние: апплет и тесты читают их как
        раньше; `workspace` (номер активного стола и стол каждого окна и
        покрытия) добавлен рядом, а не вместо.
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
                {
                    "window": hex(win),
                    "zone": zone,
                    "monitor": list(wa) if wa else None,
                    "workspace": self.win_workspace.get(win),
                }
            )
        filled = [
            {
                "workspace": workspace,
                "monitor": list(wa),
                "zone": zone,
                "holder": hex(holder),
            }
            for (workspace, wa, zone), holder in sorted(
                self.filled.items(),
                key=lambda item: (
                    item[0][2],
                    -1 if item[0][0] is None else item[0][0],
                    item[1],
                ),
            )
        ]
        return {
            "version": VERSION,
            "conf": self.conf_path,
            "reflow": self.reflow,
            # Активный рабочий стол (нумерация EWMH с нуля); None — неизвестен.
            "workspace": self.workspace,
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
        meta = self.read_meta(window)
        wm_class = getattr(meta, "wm_class", "") or ""
        if window_excluded(
            wm_class,
            getattr(meta, "wm_title", "") or "",
            getattr(meta, "window_type", ()),
            self.grid.ignore,
        ):
            raise ValueError(
                f"окно 0x{window:x} под исключением ([ignore]/служебный тип) — не двигаю"
            )
        wa = self._monitor_for_command(request, window)
        if wa is None:
            raise ValueError(f"не удалось определить монитор окна 0x{window:x}")
        rect = self.grid.rect(zone, wa)
        if not self._apply(window, rect, zone):
            raise ValueError(f"не удалось вложить окно 0x{window:x} в зону '{zone}'")
        self.windows[window] = zone
        self.win_monitor[window] = wa
        # Окно вкладывается там, где сейчас пользователь: занятость считаем по
        # активному столу (сетка зоны — по-прежнему из основного конфига).
        self.win_workspace[window] = self.workspace
        self.win_class[window] = wm_class
        self._save_state()
        self._restore_holder(self.workspace, wa, zone)
        return {
            "window": hex(window),
            "zone": zone,
            "monitor": list(wa),
            "rect": list(rect),
        }

    def ipc_focus(self, request):
        """Режим «только эта зона»: вход/toggle/выход.

        `focus <зона>` кладёт активное окно (или `window` из запроса) в зону и
        сворачивает остальные окна **того же стола и того же монитора**; команда
        при уже активном режиме — **toggle**: сворачивает режим и разворачивает
        окна, которые свернул он сам. `{"off": true}` — явный выход без входа.

        Разворачиваются только окна из `self.focus["minimized"]` (свёрнутое
        вручную остаётся свёрнутым), последней — цель: `deiconify` поднимает и
        фокусирует окно. Исключения (`window_excluded`: `[ignore]`, служебные
        типы, диалоги) и уже свёрнутые окна не трогаются и в `minimized` не
        попадают.

        Активный стол не прочитан — отказ: сворачивать окна чужих столов
        вслепую хуже, чем не включать режим.

        Возврат:

        - вход — `{"zone", "window" (hex), "monitor" ([x,y,w,h]), "minimized"
          ([hex] — что свернули)}`;
        - выход — `{"off": True, "restored": [hex]}`.

        Режим входит в `status` полем `focus`: `null` или
        `{"zone", "window", "minimized" (число)}`.
        """
        raise NotImplementedError("zone-daemon: focus ещё не реализован")

    def ipc_reload(self, request=None):
        """Перечитать zones.conf: сетка, правила, профили столов и reflow; кэши сбросить."""
        self.grid = tiler.Grid(self.conf_path)
        self.reflow = read_reflow(self.grid.conf)
        self._grids = {}
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
        # Память прошлого запуска и переезды пользователя — до укладки: окно,
        # которое вынесли из зоны, не должно быть уложено снова.
        self._apply_pending(current)
        self._refresh_detached()
        new = [win for win in dict.fromkeys(client_ids) if win not in self.seen]
        for win in new:
            self.log(f"новое окно 0x{win:x} в _NET_CLIENT_LIST")
            self.on_map(win)
        for win in list(self.windows):
            if win not in current:
                self.on_destroy(win)
        self._prune_state(current)
        self.seen = current

    def on_workspace_change(self):
        """Активный стол сменился (`_NET_CURRENT_DESKTOP`): запомнить новый.

        Окна не двигаем: укладка и reflow идут только по событиям окон, а
        невидимые столы остаются как есть. Возвращает новый стол (прежний,
        если прочитать не удалось).
        """
        previous = self.workspace
        workspace = self._read_current_workspace()
        if workspace is None:
            self.log(
                f"активный стол не прочитался — остаётся {workspace_label(previous)}"
            )
            return previous
        self.workspace = workspace
        if workspace == previous:
            self.log(f"активный стол не изменился: {workspace_label(workspace)}")
        else:
            self.log(
                f"активный стол: {workspace_label(workspace)} "
                f"(был {workspace_label(previous)}) — окна не двигаю"
            )
        return workspace

    def on_map(self, win, override_redirect=False):
        """Новое клиентское окно: вложить в зону по правилу, если оно подходит.

        Сетка берётся для стола окна (`tiler.grid_for_workspace`): на столе с
        профилем окно встаёт в зоны профиля, а «липкое» окно — в общий набор
        `zones.conf`. Правила — из основного конфига: имя зоны глобально, но
        если в наборе стола такой зоны нет, окно не укладывается (лучше никак,
        чем в чужую геометрию).
        """
        if win in self.windows:
            return  # окно уже уложено (повторное появление в списке)
        if win in self.detached:
            return  # пользователь вынес окно из зоны — само оно не вернётся
        if override_redirect:
            return  # меню/подсказки WM — не самостоятельные окна
        meta = self.read_meta(win)
        wm_class = getattr(meta, "wm_class", "") or ""
        wm_title = getattr(meta, "wm_title", "") or ""
        if not wm_class:
            return  # без WM_CLASS правило не проверить
        if window_excluded(
            wm_class, wm_title, getattr(meta, "window_type", ()), self.grid.ignore
        ):
            self.log(f"окно 0x{win:x} под исключением ([ignore]/служебный тип) — не трогаю")
            return
        zone = tiler.match_rules(self.grid.rules, wm_class, wm_title)
        if zone is None:
            return
        workspace = self._window_workspace(win)
        grid = self._grid_for(workspace)
        if zone not in grid.all_zone_names():
            self.log(
                f"зоны '{zone}' нет в наборе стола {workspace_label(workspace)} — "
                f"окно 0x{win:x} не укладываю"
            )
            return
        wa = self._monitor_of(win)
        if wa is None:
            return  # монитор окна недоступен: не падаем и окно не трогаем
        rect = grid.rect(zone, wa)
        if not self._apply(win, rect, zone):
            return  # не запоминаем: окно не уложено, зону занятой не считаем
        self.windows[win] = zone
        self.win_monitor[win] = wa
        self.win_workspace[win] = workspace
        self.win_class[win] = wm_class
        self.log(
            f"окно 0x{win:x} -> зона '{zone}' (стол {workspace_label(workspace)})"
        )
        self._save_state()
        # Зона снова занята — расширившийся на неё сосед больше не нужен.
        self._restore_holder(workspace, wa, zone)

    def on_destroy(self, win):
        """Окно исчезло из `_NET_CLIENT_LIST` — снять с трекера и, если нужно, reflow.

        Перестройка — только когда стол закрытого окна виден (`self.workspace`):
        окна невидимых столов не двигаются никогда, состояние такого стола
        просто чистится.
        """
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        workspace = self.win_workspace.pop(win, None)
        self.log(f"окно 0x{win:x} ушло из зоны '{zone}'")
        # Держатель исчез: снять записи о зонах, которые он покрывал.
        for key, holder in list(self.filled.items()):
            if holder == win:
                del self.filled[key]
        if self.reflow != REFLOW_NEIGHBORS or wa is None:
            return
        if not self._visible(workspace):
            self.log(
                f"стол {workspace_label(workspace)} не виден "
                f"(сейчас {workspace_label(self.workspace)}) — reflow не делаю"
            )
            return
        # reflow — в пределах монитора ушедшего окна и видимого стола: окна на
        # других экранах и столах в занятых зонах не учитываются и не двигаются.
        occupied = {
            other_zone
            for other, other_zone in self.windows.items()
            if self.win_monitor.get(other) == wa
            and self._visible(self.win_workspace.get(other))
        }
        grid = self._grid_for(self.workspace)
        zones = {name: grid.fractions(name) for name in grid.zones}
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
            self.filled[(self.workspace, wa, zone)] = target
            self.log(f"reflow: окно 0x{target:x} -> зона '{name}'")
        self._save_state()

    def on_unmap(self, win):
        """Окно скрыто (свёрнуто/сменился воркспейс) — no-op.

        В `_NET_CLIENT_LIST` такое окно остаётся, поэтому реконсиляция его не
        снимает с трекера: при разворачивании окно не переставляется заново, а
        соседи не расширяются на занятую зону. Снятие с трекера и reflow —
        только когда окно исчезает из `_NET_CLIENT_LIST` (`on_destroy`), и там
        же стоит проверка «стол окна виден»: перестраивать здесь нечего.
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


def handle_property_event(dae, buf, client_list_atom, read_clients, desktop_atom=None):
    """PropertyNotify: `_NET_CLIENT_LIST` — реконсиляция, `_NET_CURRENT_DESKTOP` — стол.

    Возвращает True, если событие относилось к одному из этих свойств. Если
    список прочитать не удалось (`read_clients()` дал `None`), реконсиляция
    пропускается: трекер и `seen` не трогаем.

    `desktop_atom` — атом `_NET_CURRENT_DESKTOP`: на него демон обновляет
    активный стол (`dae.on_workspace_change()`), но окна не двигает: укладка и
    reflow — только по событиям окон. `None` — атом не передан, событие о столе
    пропускается (старый вызов из четырёх аргументов).
    """
    ev = ctypes.cast(buf, ctypes.POINTER(XPropertyEvent)).contents
    if desktop_atom is not None and ev.atom == desktop_atom:
        dae.on_workspace_change()
        return True
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
    каждый PropertyNotify про `_NET_CLIENT_LIST`. PropertyNotify про
    `_NET_CURRENT_DESKTOP` обновляет только активный стол демона.

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
    # Тот же PropertyChangeMask на root даёт и смену активного стола.
    desktop_atom = x11.atom(CURRENT_DESKTOP_ATOM)
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
                            dae, buf, client_list_atom, read_clients, desktop_atom
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

    dae = Daemon(
        args.conf,
        state_path=placed_path(),
        session_id=read_session_id(),
        verbose=args.verbose,
    )
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

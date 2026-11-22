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

Рабочие столы и экраны: демон помнит активный стол (`_NET_CURRENT_DESKTOP`) и
стол каждого уложенного окна (`_NET_WM_DESKTOP`). Сетка зон окна берётся по
его экрану: `[monitors]` важнее `[workspaces]`, `[workspaces]` — важнее общего
`[zones]` (`zone_config.resolve_profile`); профиль стола — это
`templates/<имя>.conf` из `[workspaces]`. «Липкое» окно (`0xFFFFFFFF`) живёт в
общем наборе. Окна невидимых столов не двигаются никогда: reflow работает
только на текущем столе.

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
Команды: `status`, `apply`, `focus`, `reload`, `quit`; говорит с сокетом клиент
`gridsnap-ctl`. Кадрирование и диспетчер живут в чистом `zone_ipc.py`.

Режим «только эта зона» (`focus`): окно-цель ложится в зону, а остальные окна
его стола и монитора сворачиваются. Живёт режим здесь, в демоне, потому что
только он видит `_NET_CLIENT_LIST` и потому умеет свернуть окна, появившиеся
после входа; повторная команда — toggle, `{"off": true}` — явный выход.

Вкладки в зоне: несколько окон одной зоны стоят на общей рамке (зона минус
полоса), верхнее видно целиком. Хозяин — тоже демон. Окно, попавшее в зону,
само становится вкладкой; вторая вкладка отнимает у окон высоту полосы и
переставляет уже стоявшее окно. Состав стопки выводится из трекера и рамок
(файла состояния нет), а полосу рисует отдельный процесс `zone-tabstrip.py`
(GTK в демон не добавляется): ему на каждое изменение пишется снимок
`zone_tabs.strips_payload`. Команды `tabs-next` (клавиша) и `tabs-switch`
(клик по полосе) приходят в тот же сокет, что и команды `gridsnap-ctl`.
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

from zone_i18n import _

VERSION = "0.2.0"

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
        raise ImportError(_("не удалось загрузить модуль {path}").format(path=path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_zone_tiler", "zone-tiler.py")
zone_reflow = _load_sibling("gridsnap_zone_reflow", "zone_reflow.py")
zone_ipc = _load_sibling("gridsnap_zone_ipc", "zone_ipc.py")
zone_tabs = _load_sibling("gridsnap_zone_tabs", "zone_tabs.py")

# `zone_config` — чистое ядро профилей (`[monitors]`, `[workspaces]`,
# `resolve_profile`). Демону оно нужно только когда привязки есть, поэтому
# грузится лениво: старт демона не должен зависеть от лишнего модуля.
_zone_config = None


def zone_config_module():
    """Лениво загрузить `zone_config` (ядро разбора профилей) рядом с демоном."""
    global _zone_config
    if _zone_config is None:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import zone_config as module  # noqa: PLC0415 — ленивый импорт, как у раскладчика
        _zone_config = module
    return _zone_config

# Полоса вкладок — отдельный GTK-процесс рядом с демоном (как `--hud` у
# горячих краёв): GTK в демон не добавляется, а полосе для снимка вкладок
# довольно stdin.
TABSTRIP = os.path.join(HERE, "zone-tabstrip.py")

# Сколько проверять только запущенный процесс полосы, чтобы отличить отказ
# запуска («нет GTK»: python стартует, `zone-tabstrip.py` падает на импорте с
# кодом 1) от работающей полосы. Проверка короткая и только на старте: демон
# полосу не ждёт, а без неё вкладки работают.
START_VERIFY_DELAY = 0.3
START_POLL_INTERVAL = 0.02


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


class TabStripProcess:
    """Дочерний ``zone-tabstrip.py`` — полоса вкладок поверх зон.

    Снимок вкладок пишется в stdin одной строкой JSON на изменение (протокол
    ``zone_tabs.strips_payload``); EOF в stdin завершает главный цикл полосы,
    поэтому останов — сначала мягкий EOF, потом ``terminate``/``kill``.

    Сбой запуска демон не роняет: без полосы вкладки работают, просто на экране
    нет подсказки, что окон в зоне несколько. Повторять запуск, которого быть не
    может (нет ``zone-tabstrip.py`` или нет GTK), смысла нет — ``failed``.

    «Нет GTK» выглядит не как `OSError` от `Popen`: python стартует нормально, а
    ``zone-tabstrip.py`` падает на импорте с кодом 1 — и такой ребёнок больше не
    перезапускается на каждом изменении картины. Отличить отказ запуска от
    смерти уже работавшей полосы помогает короткая проверка сразу после
    `Popen` (`_launch_failure`): умершая позже полоса остаётся перезапускаемой.
    """

    def __init__(self, conf, script=None, python=None, verbose=False):
        self.conf = conf
        self.script = script or TABSTRIP
        self.python = python or sys.executable
        self.verbose = verbose
        self.proc = None
        self.failed = False

    def log(self, message):
        if self.verbose:
            print(f"zone-daemon: {message}", file=sys.stderr, flush=True)

    def command(self):
        return [self.python, self.script, "--conf", self.conf]

    def start(self):
        """Запустить полосу; уже запущенную не трогаем, безнадёжную не повторяем."""
        if self.failed or self.running():
            return False
        if not os.path.exists(self.script):
            self.failed = True
            self.log(
                _("полосы вкладок нет: {script} не найден").format(script=self.script)
            )
            return False
        try:
            self.proc = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if self.verbose else subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.failed = True
            self.log(_("полоса вкладок не запустилась: {exc}").format(exc=exc))
            return False
        code = self._launch_failure()
        if code is not None:
            self.failed = True
            self.stop()  # закрыть stdin мёртвого ребёнка
            self.log(
                _(
                    "полоса вкладок не запустилась: {script} вышел с кодом {code}"
                ).format(script=self.script, code=code)
            )
            return False
        self.log(_("полоса вкладок запущена: {script}").format(script=self.script))
        return True

    def _launch_failure(self):
        """Код возврата, если ребёнок умер сразу после старта, иначе ``None``.

        `poll()` сразу после `Popen` обычно ещё `None`: python только-только
        запустился. Поэтому коротко ждём и опрашиваем: упавший на импорте
        `zone-tabstrip.py` (нет GTK) выходит почти сразу, а живая полоса
        доживает до конца проверки. Смерть уже работавшей полосы — не отказ
        запуска: её `feed` перезапускает как раньше.
        """
        proc = self.proc
        if proc is None:
            return None
        if START_VERIFY_DELAY <= 0:
            return proc.poll()
        deadline = time.monotonic() + START_VERIFY_DELAY
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(START_POLL_INTERVAL)

    def feed(self, line):
        """Отдать полосе снимок; ``False`` — она его не приняла (умерла/не поднялась)."""
        if self.failed:
            return False
        if not self.running() and not self.start():
            return False
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.stop()
            return False
        return True

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Погасить полосу: EOF в stdin, затем terminate/kill. Идемпотентно."""
        proc, self.proc = self.proc, None
        if proc is None:
            return False
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        return True


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


def read_tabs_height(conf):
    """Высота полосы вкладок из `[tabs]`; нет секции/значения или мусор -> умолчание.

    Высота — величина с умолчанием (``zone_tabs.STRIP_HEIGHT``), а не догма:
    читается тем же конфигом, что и зоны, но разбирает её сам демон — разбор
    секции ``[tabs]`` целиком живёт в ``zone_config`` у полосы и раскладчика.
    """
    try:
        raw = conf.get("tabs", "height", fallback=None)
    except Exception:
        return zone_tabs.STRIP_HEIGHT
    if raw is None:
        return zone_tabs.STRIP_HEIGHT
    try:
        height = int(str(raw).strip())
    except (TypeError, ValueError):
        return zone_tabs.STRIP_HEIGHT
    # 0/отрицательная/абсурдно большая высота — не полоса, а мусор в конфиге.
    if not 0 < height < 200:
        return zone_tabs.STRIP_HEIGHT
    return height


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
    return _("неизвестен") if workspace is None else f"#{workspace}"


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
      read_clients — callable() -> список id из `_NET_CLIENT_LIST` или None
                   (`read_client_list`); вкладкам нужен порядок окон снизу вверх;
      active_window — callable() -> id активного окна (`tiler.active_window`):
                   `tabs-next` переключает стопку его зоны, а полоса метит
                   активную вкладку;
      strip      — процесс полосы вкладок (`TabStripProcess` или подмена в
                   тестах) либо `None` — полосы нет; её сбой демон не роняет;
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

    Режим «только эта зона» (`self.focus`, команда `focus`): окно-цель ложится
    в зону, а окна того же стола и монитора сворачиваются; их id попадают в
    `minimized`, и на выходе разворачиваются только они (свёрнутое вручную
    остаётся свёрнутым). Режим снимается сам, когда цель исчезла, сменился
    активный стол или демон завершается — и тогда окна возвращаются. Вкладки
    зоны-цели режим не сворачивает: они часть той зоны, которую владелец
    попросил оставить.

    Вкладки в зоне (`_zone_stack`, `_settle_in_zone`, `tabs_picture`): окна
    одной зоны на одном столе и мониторе стоят на общей рамке. Одно окно
    занимает зону целиком, два и больше — зону минус полосу
    (`zone_tabs.tab_rect`); вторая вкладка переставляет первую. Снимок стопок
    уходит в `self.strip` одной строкой `zone_tabs.strips_payload`.
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
        focus_path=None,
        iconify=None,
        deiconify=None,
        read_clients=None,
        active_window=None,
        strip=None,
        monitor_outputs=None,
        verbose=False,
    ):
        self.conf_path = conf_path or tiler.default_conf()
        self.grid = tiler.Grid(self.conf_path)
        self.x11 = None  # соединение кладёт serve(); чужое для apply
        self.apply = apply or self._default_apply
        self.read_meta = read_meta or read_window_meta
        self.workarea = workarea
        self.monitors = monitors if monitors is not None else tiler.monitors
        # [(имя выхода, рабочая область)] — имя адресует профиль в `[monitors]`.
        self.monitor_outputs = (
            monitor_outputs if monitor_outputs is not None else tiler.monitor_outputs
        )
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
        # Активное окно читается вкладками: по нему выбирается стопка для
        # `tabs-next` и метится видимая вкладка в снимке для полосы.
        self.active_window = active_window or tiler.active_window
        # Полоса вкладок — отдельный GTK-процесс (`TabStripProcess`) или None:
        # без неё вкладки работают, просто нет подсказки на экране.
        self.strip = strip
        # Высота полосы из `[tabs]`; нет секции или мусор — умолчание ядра.
        self.tabs_height = read_tabs_height(self.grid.conf)
        # Последний отданный полосе снимок: одинаковые не пишем повторно.
        self._strips_last = None
        # Путь файла состояния; `None` — только в памяти (тесты, утилитный запуск).
        self.state_path = state_path
        # Путь `focus.json` — куда отдаётся неразвёрнутый остаток режима при
        # завершении демона. `None` (тесты, утилитный запуск) — только в журнал.
        self.focus_path = focus_path
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
        # [(имя выхода, рабочая область)] из `xrandr --listmonitors`: имя выхода
        # адресует профиль экрана в `[monitors]`. Кэш на время жизни конфига.
        self._outputs_cache = None
        # `[monitors]`/`[workspaces]` активного конфига: разбираются один раз за
        # reload — привязки меняются только правкой файла.
        self._monitor_map_cache = None
        self._workspace_map_cache = None
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
            self.log(_("не удалось определить активный стол: {exc}").format(exc=exc))
            return None

    def _window_workspace(self, win):
        """Стол окна для выбора сетки; `None` — «липкое» или не опознано."""
        try:
            return self.read_window_workspace(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось определить стол окна 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
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

    def _grid_for(self, workspace=None, output=None):
        """Сетка окна: профиль экрана, стола или общий набор `zones.conf`.

        `output` — имя выхода монитора окна (`HDMI-2`), `workspace` — стол окна
        из EWMH (с нуля). `None`/`None` (липкое или неопознанное окно) — обычный
        `Grid(conf_path)`: общий набор зон, как зафиксировано контрактом
        `grid_for_workspace`. Привязки разбираются в порядке `[monitors]` →
        `[workspaces]` → активный конфиг (`zone_config.resolve_profile`), откат
        на следующий уровень — в `_profile_grid`.

        Сетки кэшируются по паре (стол, выход), чтобы не перечитывать конфиг и
        профиль на каждое окно; `reload` сбрасывает кэш. Битый конфиг, профиль
        или привязки (в т.ч. `SystemExit` из `fail()`) не роняют демон: берётся
        общий набор.
        """
        if workspace is None and output is None:
            return self.grid
        key = (workspace, output)
        cached = self._grids.get(key)
        if cached is not None:
            return cached
        try:
            grid = self._profile_grid(workspace, output)
        except (Exception, SystemExit) as exc:
            self.log(
                _(
                    "не удалось взять сетку профиля ({desk}, экран {output}): "
                    "{exc} — беру общий набор"
                ).format(
                    desk=workspace_label(workspace),
                    output=output or _("неизвестен"),
                    exc=exc,
                )
            )
            grid = self.grid
        self._grids[key] = grid
        return grid

    def _profile_grid(self, workspace, output):
        """Сетка по привязкам: экран важнее стола, стол — важнее активного конфига.

        Профиль экрана ищется в `[monitors]`, профиль стола — в `[workspaces]`;
        порядок разбора один на всех — `zone_config.resolve_profile` (номер стола
        EWMH он же переводит в нумерацию конфига с 1). Профиля нет на диске —
        **откат на следующий уровень** с записью в журнал: раскладка на экране
        без профиля всё равно нужна, отказ работы хуже общего набора зон.
        """
        zc = zone_config_module()
        monitor_map = self._monitor_map()
        workspace_map = self._workspace_map()
        levels = []
        if output:
            levels.append(
                (output, workspace, _("экрана {output}").format(output=output))
            )
        if workspace is not None:
            desk = _("стола {desk}").format(desk=workspace_label(workspace))
            levels.append((None, workspace, desk))
        tried = set()
        for monitor, desk, where in levels:
            name = zc.resolve_profile(monitor_map, workspace_map, monitor, desk)
            if not name or name in tried:
                continue
            tried.add(name)
            try:
                path = zc.profile_path(name, self.conf_path)
            except ValueError as exc:
                self.log(
                    _(
                        "плохое имя профиля «{name}» ({where}): {exc} — "
                        "беру следующий уровень"
                    ).format(name=name, where=where, exc=exc)
                )
                continue
            if not os.path.exists(path):
                self.log(
                    _(
                        "профиля «{name}» ({where}) нет на диске: {path} — "
                        "беру следующий уровень"
                    ).format(name=name, where=where, path=path)
                )
                continue
            return tiler.Grid(path)
        return self.grid

    def _monitor_map(self):
        """`[monitors]` активного конфига (выход → профиль); кэш на reload."""
        if self._monitor_map_cache is None:
            try:
                self._monitor_map_cache = dict(
                    zone_config_module().load_monitor_map(self.conf_path)
                )
            except (Exception, SystemExit) as exc:
                self.log(
                    _(
                        "не удалось прочитать [monitors]: {exc} — экраны не привязаны"
                    ).format(exc=exc)
                )
                self._monitor_map_cache = {}
        return self._monitor_map_cache

    def _workspace_map(self):
        """`[workspaces]` активного конфига (стол с 1 → профиль); кэш на reload."""
        if self._workspace_map_cache is None:
            try:
                self._workspace_map_cache = dict(
                    zone_config_module().load_workspace_map(self.conf_path)
                )
            except (Exception, SystemExit) as exc:
                self.log(
                    _(
                        "не удалось прочитать [workspaces]: {exc} — беру общий набор"
                    ).format(exc=exc)
                )
                self._workspace_map_cache = {}
        return self._workspace_map_cache

    def _outputs(self):
        """[(имя выхода, рабочая область)] с кэшем; отказ — пустой список.

        Имя выхода (`HDMI-1`) адресует профиль в `[monitors]`, а рабочая область
        — то, что демон и так знает про монитор окна. `monitor_outputs` через
        `fail()` бросает `SystemExit` при отказе X-сервера (не подкласс
        `Exception`), поэтому ловятся оба; неудачу не кэсим.
        """
        if self._outputs_cache is None:
            try:
                pairs = self.monitor_outputs()
                self._outputs_cache = [
                    (str(name), tuple(int(v) for v in area)) for name, area in pairs
                ]
            except (Exception, SystemExit) as exc:
                self.log(
                    _("не удалось сопоставить выходы мониторов: {exc}").format(
                        exc=exc
                    )
                )
                return []
        return self._outputs_cache

    def _output_for(self, wa):
        """Имя выхода монитора с рабочей областью `wa` (`HDMI-1`); нет — `None`.

        `None` — экран не привязан (`[monitors]` пуст) или выход не сопоставился
        с рабочей областью. Тогда `xrandr` вовсе не зовётся, а разбор профиля
        идёт следующим уровнем (стол/активный конфиг).
        """
        if wa is None or not self._monitor_map():
            return None
        try:
            rect = tuple(int(v) for v in wa)
        except (TypeError, ValueError):
            return None
        for name, area in self._outputs():
            if area == rect:
                return name
        return None

    def _forced_workarea(self):
        """Принудительная одна рабочая область; `None`, если её не получить."""
        if self._wa_cache is None:
            try:
                wa = self.workarea() if callable(self.workarea) else self.workarea
                self._wa_cache = tuple(cast("tuple[int, int, int, int]", wa))
            except (Exception, SystemExit) as exc:
                self.log(
                    _("не удалось получить рабочую область: {exc}").format(exc=exc)
                )
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
                self.log(_("не удалось получить мониторы: {exc}").format(exc=exc))
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
            self.log(
                _("не удалось определить положение окна 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
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
            self.log(
                _("не удалось вложить окно 0x{win:x} в зону '{zone}': {exc}").format(
                    win=win, zone=zone, exc=exc
                )
            )
            return False
        self._remember_placement(win, rect)
        return True

    # -- память об укладках

    def _read_rect(self, win):
        """Текущая рамка окна; `None`, если её не прочитать (нет X, окно ушло)."""
        try:
            rect = self.read_rect(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось прочитать рамку окна 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
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
            self.log(
                _("не удалось сохранить память об укладках: {exc}").format(exc=exc)
            )

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
        self._drop_coverage(win)
        if frame is not None:
            self.detached[win] = {
                "frame": [int(v) for v in frame],
                "wm_class": wm_class or "",
                "zone": zone,
                "monitor": [int(v) for v in wa] if wa else None,
                "workspace": workspace,
            }
        self._save_state()
        self.log(
            _("окно 0x{win:x} вышло из зоны '{zone}' — больше его не двигаю").format(
                win=win, zone=zone
            )
        )

    def _drop_coverage(self, win):
        """Снять покрытия, где держателем было `win`: окно сдвинули из зоны.

        `filled` — книга «кто расширился на чужую зону». Держателя сдвинули
        (стопка переставила его на общую рамку, он остался единственной
        вкладкой, он вышел из зоны или его закрыли) — запись о покрытой зоне
        больше не правда, и `status.filled` не должен её показывать. Геометрия
        от этого не едет: запись снялась бы и позже, когда зону займут заново.
        """
        dropped = False
        for key, holder in list(self.filled.items()):
            if holder == win:
                del self.filled[key]
                dropped = True
        return dropped

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
            grid = self._grid_for(record.get("workspace"), self._output_for(wa))
            return grid.rect(record["zone"], wa)
        except (Exception, SystemExit) as exc:
            self.log(
                _(
                    "не удалось посчитать зону '{zone}' записи 0x{win:x}: {exc}"
                ).format(zone=record["zone"], win=win, exc=exc)
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
            self.log(
                _("не удалось прочитать метаданные 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return False
        wm_class = getattr(meta, "wm_class", "") or ""
        recorded_class = record.get("wm_class") or ""
        if recorded_class and wm_class and recorded_class != wm_class:
            return False
        actual = self._read_rect(win)
        if actual is not None and frame_matches(actual, record["frame"]):
            zone_rect = self._zone_rect_for(win, record)
            if zone_rect is not None and not self._in_zone_frame(actual, zone_rect):
                # Рамка не своя зона: это сосед, расширенный reflow, а книга
                # покрытий (`filled`) в файле состояния не живёт. Даём окну
                # пройти обычный путь `on_map` — правило вернёт его в зону из
                # конфига, и новое окно не ляжет поверх.
                self.log(
                    _(
                        "окно 0x{win:x} стоит не в своей зоне '{zone}' — "
                        "укладываю по правилу"
                    ).format(win=win, zone=record["zone"])
                )
                return False
            self.windows[win] = record["zone"]
            if record.get("monitor"):
                self.win_monitor[win] = tuple(record["monitor"])
            self.win_workspace[win] = record.get("workspace")
            self.placed[win] = tuple(record["frame"])
            self.win_class[win] = wm_class or recorded_class
            self.log(
                _(
                    "окно 0x{win:x} осталось в зоне '{zone}' — беру под присмотр"
                ).format(win=win, zone=record["zone"])
            )
            return True
        self.detached[win] = dict(record)
        self.log(
            _("окно 0x{win:x} вышло из зоны '{zone}' — само не вернётся").format(
                win=win, zone=record["zone"]
            )
        )
        return True

    def _in_zone_frame(self, actual, zone_rect):
        """Стоит ли окно в своей зоне: на рамке зоны или на общей рамке вкладок.

        Рамка вкладки — та же зона минус полоса. Без этой проверки память
        укладок решила бы, что окно-вкладку вынесли из зоны, и вернула его
        обратно на рамку зоны (а с ним и всю стопку).
        """
        if frame_matches(actual, zone_rect):
            return True
        return frame_matches(actual, self._tab_frame(zone_rect))

    def _apply_pending(self, current):
        """Разобрать память прошлого запуска по текущему списку клиентов."""
        pending, self._pending = self._pending, {}
        if not pending:
            return
        for win, record in pending.items():
            if win not in current:
                continue  # окно закрылось, пока демон не работал
            if not self._adopt_placement(win, record):
                self.log(
                    _("запись о 0x{win:x} не подошла — укладываю по правилу").format(
                        win=win
                    )
                )

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
        holder_workspace = self.win_workspace.get(holder)
        # Профиль — того экрана, где стоит окно-держатель (reflow живёт на `wa`).
        grid = self._grid_for(holder_workspace, self._output_for(wa))
        if holder_zone is None or holder_zone not in grid.all_zone_names():
            # Держателя нет или его зоны больше нет в наборе — покрытие снимаем.
            del self.filled[key]
            return
        # Рамка возврата — по числу вкладок стопки: окно-вкладка возвращается на
        # общую рамку зоны, а не на всю её площадь.
        count = len(self._zone_stack(holder_zone, wa, holder_workspace))
        rect = self._stack_rect(holder_zone, wa, holder_workspace, count, grid=grid)
        if self._apply(holder, rect, holder_zone):
            del self.filled[key]
            self._save_state()
            self.log(
                _("возвращаю зону '{zone}' к исходной рамке").format(
                    zone=holder_zone
                )
            )

    # -- вкладки в зоне

    def _client_order(self, client_ids=None):
        """id клиентов в порядке `_NET_CLIENT_LIST` — снизу вверх.

        Список читает демон (только он его видит); недоступен — берётся
        порядок трекера, чтобы одна неудача xprop не рвала стопки на части.
        `client_ids` — уже прочитанный список (его же читает `reconcile`):
        тогда лишнего запроса к X нет.
        """
        clients = []
        try:
            source = self.read_clients() if client_ids is None else client_ids
            clients = [int(win) for win in (source or [])]
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось прочитать список клиентов: {exc}").format(exc=exc)
            )
        order = list(dict.fromkeys(clients))
        order.extend(win for win in self.windows if win not in order)
        return order

    def _tracked_order(self, client_ids=None):
        """Уложенные окна видимого стола в порядке `_NET_CLIENT_LIST` (снизу вверх)."""
        return [
            win
            for win in self._client_order(client_ids)
            if win in self.windows and self._visible(self.win_workspace.get(win))
        ]

    def _frames(self, windows):
        """`{окно: рамка}` в заданном порядке; нечитаемая рамка — `None`.

        Словарь в Python хранит порядок вставки, поэтому `zone_tabs.tabs_of_zone`
        видит окна именно в порядке `_NET_CLIENT_LIST`.
        """
        return {win: self._read_rect(win) for win in windows}

    def _zone_pixel_rect(self, zone, wa, workspace=None, grid=None):
        """Пиксельная рамка зоны на мониторе `wa` (сетка — по экрану или заданная).

        Без явной сетки профиль разрешается по **этому** монитору: у экрана
        может быть свой профиль, и чужая геометрия тут не годится.
        """
        grid = grid or self._grid_for(workspace, self._output_for(wa))
        return tuple(int(round(v)) for v in grid.rect(zone, wa))

    def _tab_frame(self, zone_rect):
        """Рамка окна-вкладки: зона минус полоса (узкая зона — сама зона)."""
        return tuple(
            int(round(v)) for v in zone_tabs.tab_rect(zone_rect, self.tabs_height)
        )

    def _stack_rect(self, zone, wa, workspace=None, count=1, grid=None):
        """Рамка окна зоны: вся зона до двух вкладок, дальше — зона минус полоса.

        Полоса отнимает высоту у окон только когда вкладок больше одной: одно
        окно занимает зону целиком, как до вкладок.
        """
        rect = self._zone_pixel_rect(zone, wa, workspace, grid)
        # Правило «одна вкладка — вся зона» берётся из ядра — то же, что у
        # раскладчика (`zone-tiler.in_zone_rect`), иначе `apply` и `arrange`
        # разойдутся на одном и том же числе окон.
        return tuple(
            int(round(v)) for v in zone_tabs.stack_rect(rect, count, self.tabs_height)
        )

    def _zone_stack(self, zone, wa, workspace=None, exclude=()):
        """Окна стопки зоны `zone` — в порядке `_NET_CLIENT_LIST` (снизу вверх).

        Стопка — окна одной зоны, одного монитора и одного стола: без этого в
        неё попало бы окно соседнего экрана или другого стола, и переключение
        подняло бы чужое. «Липкое»/неопознанное окно (`workspace is None`) не
        спорит с столом зоны — она живёт в наборе по умолчанию и видна везде.
        `wa is None` — монитор не задан (тесты, окно без рабочей области).
        """
        skip = set(exclude)
        out = []
        for win in self._client_order():
            if self.windows.get(win) != zone or win in skip:
                continue
            if wa is not None and self.win_monitor.get(win) != wa:
                continue
            other = self.win_workspace.get(win)
            if workspace is not None and other is not None and other != workspace:
                continue
            out.append(win)
        return out

    def _align_stack(self, stack, rect):
        """Переставить уже стоящие вкладки на общую рамку: полоса появилась/ушла.

        Сдвинутое окно перестаёт быть держателем покрытия: его зона-покрытие
        больше не за ним, и запись снимается здесь же (`_drop_coverage`).
        """
        for win in stack:
            if self._apply(win, rect, self.windows.get(win)):
                self._drop_coverage(win)
                self.log(
                    _(
                        "вкладки: окно 0x{win:x} встало на общую рамку зоны '{zone}'"
                    ).format(win=win, zone=self.windows.get(win))
                )

    def _settle_in_zone(self, win, zone, wa, workspace, wm_class="", grid=None):
        """Поставить окно вкладкой в зону и выровнять стопку.

        Рамка считается по числу вкладок, которое станет после укладки: первое
        окно — вся зона, второе — общая рамка вкладок, и тогда же переставляется
        уже стоявшее окно (полоса появилась, окно стало ниже на её высоту).
        Окно, переехавшее из другой зоны, оставляет за собой одну оставшуюся
        вкладку — ей возвращается вся зона (`_collapse_stack`).

        Возвращает рамку, в которую легло окно, или `None` — уложить не вышло
        (тогда трекер не трогается: зону занятой не считаем).
        """
        previous = (
            self.windows.get(win),
            self.win_monitor.get(win),
            self.win_workspace.get(win),
        )
        stack = self._zone_stack(zone, wa, workspace, exclude=(win,))
        rect = self._stack_rect(zone, wa, workspace, len(stack) + 1, grid=grid)
        if not self._apply(win, rect, zone):
            return None
        # Окно переехало — покрытия, которые оно держало, больше не его.
        self._drop_coverage(win)
        self.windows[win] = zone
        self.win_monitor[win] = wa
        self.win_workspace[win] = workspace
        self.win_class[win] = wm_class
        self._align_stack(stack, rect)
        if previous[0] is not None and previous[0] != zone:
            self._collapse_stack(previous[0], previous[1], previous[2])
        self._save_state()
        self._restore_holder(workspace, wa, zone)
        return rect

    def _collapse_stack(self, zone, wa, workspace):
        """Вернуть единственной оставшейся вкладке зону целиком.

        Полоса — признак стопки: ушла предпоследняя вкладка — полосы нет, и
        окно снова занимает всю зону. `False` — в стопке не одна вкладка (или
        двигать нельзя: невидимый стол, нет монитора).
        """
        if wa is None or not self._visible(workspace):
            return False
        remaining = self._zone_stack(zone, wa, workspace)
        if len(remaining) != 1:
            return False
        win = remaining[0]
        rect = self._zone_pixel_rect(zone, wa, workspace)
        if not self._apply(win, rect, zone):
            return False
        # Единственная вкладка сдвинулась на всю зону — держателем покрытия она
        # быть перестала (`_drop_coverage`), иначе `status.filled` врёт.
        self._drop_coverage(win)
        self._save_state()
        self.log(
            _(
                "в зоне '{zone}' осталась одна вкладка — окно 0x{win:x} "
                "занимает зону целиком"
            ).format(zone=zone, win=win)
        )
        return True

    def tabs_in_zone(self, zone, wa, workspace=None, frames=None, grid=None):
        """Окна-вкладки зоны по рамкам (снизу вверх) — `zone_tabs.tabs_of_zone`.

        `frames` — `{окно: рамка}` в порядке `_NET_CLIENT_LIST`; по умолчанию
        читаются рамки уложенных окон видимого стола. Вкладка — окно, чья рамка
        совпала с общей (зона минус полоса, допуск `zone_tabs.FRAME_TOLERANCE`):
        одно окно стоит на рамке зоны целиком и стопкой не считается.
        """
        if frames is None:
            frames = self._frames(self._tracked_order())
        tab_frame = self._stack_rect(zone, wa, workspace, 2, grid)
        return zone_tabs.tabs_of_zone(frames, tab_frame)

    def _window_title(self, win):
        """Заголовок окна для полосы; не прочитан — пустая строка."""
        try:
            return getattr(self.read_meta(win), "wm_title", "") or ""
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось прочитать заголовок 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return ""

    def _active_window(self):
        """Активное окно (`_NET_ACTIVE_WINDOW`); `None` — не прочитать."""
        try:
            active = self.active_window()
        except (Exception, SystemExit) as exc:
            self.log(_("не удалось определить активное окно: {exc}").format(exc=exc))
            return None
        return None if active is None else int(active)

    def tabs_picture(self, client_ids=None):
        """Записи снимка для полосы — только зоны текущего рабочего стола.

        На других столах окон не видно, и подсказка «здесь стопка» там не нужна.
        Активная вкладка — та, в которой активное окно; активное окно вне
        стопки — верхняя (полоса всё равно должна что-то подсветить). Записи
        строит `zone_tabs.strip_record`: одна вкладка и узкая зона дают `None`
        (полосы нет), и такие записи в снимок не попадают.
        """
        workspace = self.workspace
        monitors = self._monitor_list()
        if not monitors:
            return []
        frames = self._frames(self._tracked_order(client_ids))
        active = self._active_window()
        records = []
        for wa in monitors:
            # У каждого экрана свой профиль, поэтому и зоны — свой набор.
            grid = self._grid_for(workspace, self._output_for(wa))
            for zone in sorted(grid.zones):
                zone_rect = self._zone_pixel_rect(zone, wa, workspace, grid)
                tabs = [
                    win
                    for win in self.tabs_in_zone(zone, wa, workspace, frames, grid)
                    if self.win_monitor.get(win) == wa
                ]
                if not tabs:
                    continue
                titles = [(win, self._window_title(win)) for win in tabs]
                shown = active if active in tabs else tabs[-1]
                record = zone_tabs.strip_record(
                    zone, zone_rect, titles, active=shown, height=self.tabs_height
                )
                if record is not None:
                    records.append(record)
        return records

    def _normalize_stacks(self, client_ids=None):
        """Привести рамки стопок к числу вкладок — по зонам видимого стола.

        Правило «осталась одна вкладка — ей вся зона» живёт в ядре
        (`zone_tabs.stack_rect`), но применяется не только при укладке и
        закрытии: окно уходит из стопки руками (`_refresh_detached`) или вторая
        вкладка не возвращается после перезапуска демона (`_adopt_placement` и
        `_in_zone_frame`, который считает рамку вкладки «окно в зоне»). Тогда
        оставшееся окно стоит на рамке вкладки — на высоту полосы ниже, с пустой
        полосой сверху. Один проход по зонам вместо россыпи вызовов
        `_collapse_stack` возвращает таким окнам рамку по числу вкладок.

        Двигаются только окна, стоящие на рамке вкладки при единственной
        вкладке в зоне: окно на своей зоне и расширенный reflow-держатель (его
        рамка — объединение зон) не трогаются. `True` — что-то переставили.
        """
        if self.workspace is None:
            return False
        stacks = {}
        for win in self._client_order(client_ids):
            zone = self.windows.get(win)
            wa = self.win_monitor.get(win)
            if zone is None or wa is None:
                continue
            if not self._visible(self.win_workspace.get(win)):
                continue
            # Зона ищется в профиле того экрана, где стоит окно.
            grid = self._grid_for(self.workspace, self._output_for(wa))
            if zone not in grid.all_zone_names():
                continue
            stacks.setdefault((zone, wa), []).append(win)
        moved = False
        for (zone, wa), stack in stacks.items():
            if len(stack) != 1 or wa is None:
                continue
            win = stack[0]
            grid = self._grid_for(self.workspace, self._output_for(wa))
            zone_rect = self._zone_pixel_rect(zone, wa, self.workspace, grid)
            tab_frame = self._tab_frame(zone_rect)
            if tab_frame == zone_rect:
                continue  # узкая зона: полосы нет, рамки вкладки и зоны совпали
            # Сравниваем с памятью укладок, а не с чтением X: до этого прохода
            # `_refresh_detached` уже сравнил факт с памятью, и раз окно в
            # трекере — оно там, куда демон его положил (или его не прочитать).
            actual = self.placed.get(win)
            if actual is None:
                actual = self._read_rect(win)
            if actual is None or not frame_matches(actual, tab_frame):
                continue  # окно не на рамке вкладки — не наше дело
            if not self._apply(win, zone_rect, zone):
                continue
            self._drop_coverage(win)
            moved = True
            self.log(
                _(
                    "в зоне '{zone}' осталась одна вкладка — окно 0x{win:x} "
                    "занимает зону целиком"
                ).format(zone=zone, win=win)
            )
        if moved:
            self._save_state()
        return moved

    def sync_strips(self, client_ids=None):
        """Отдать полосе полный снимок вкладок; сбой полосы демон не роняет.

        Снимок — одна строка `zone_tabs.strips_payload` на каждое изменение
        (не дельта): полосе не надо помнить прошлое. Одинаковые снимки не
        пишутся дважды — событий много, а меняется картина реже. Без полосы
        (`strip is None`) метод ничего не стоит и не читает ни X, ни конфиг.
        """
        if self.strip is None:
            return False
        try:
            records = self.tabs_picture(client_ids)
        except (Exception, SystemExit) as exc:
            self.log(_("не удалось собрать снимок вкладок: {exc}").format(exc=exc))
            return False
        payload = zone_tabs.strips_payload(records)
        if payload == self._strips_last:
            return False
        self._strips_last = payload
        if not records and not self.strip.running():
            return False  # рисовать нечего и рисовать некому
        try:
            accepted = self.strip.feed(payload)
        except (Exception, SystemExit) as exc:
            self.log(_("полоса вкладок: {exc}").format(exc=exc))
            accepted = False
        if not accepted:
            # Полоса не приняла снимок (упала/не запустилась): в следующий раз
            # отдадим тот же снимок заново — уже новому процессу.
            self._strips_last = None
            self.log(_("полоса вкладок снимок не приняла"))
        return accepted

    def _raise_window(self, win):
        """Показать окно: `_NET_ACTIVE_WINDOW` через общее соединение демона."""
        try:
            self.deiconify(win, x11=self.x11)
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось поднять окно 0x{win:x}: {exc}").format(win=win, exc=exc)
            )
            return False
        self.log(_("окно 0x{win:x} поднято").format(win=win))
        return True

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
                zone_tabs.TABS_NEXT: self.ipc_tabs_next,
                zone_tabs.TABS_SWITCH: self.ipc_tabs_switch,
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
        зона — доля рабочей области, а у монитора своя область.
        **Наполнение считается по разрешённым профилям**, а не по активному
        `zones.conf`: у каждого экрана свой профиль (см. `_grid_for`), поэтому
        `zones` — объединение наборов по экранам (в их порядке), а
        `zones_px[name][i]` — рамка зоны в профиле монитора `i` (или `None`,
        если в том профиле такой зоны нет: рисовать её там нечего). Форма
        ответа при этом прежняя.

        Поля `windows[].zone/window/monitor` — прежние: апплет и тесты читают
        их как раньше; `workspace` (номер активного стола и стол каждого окна и
        покрытия) добавлен рядом, а не вместо. `focus` — режим «только эта
        зона»: `None` или `{zone, window, minimized (число)}`.
        """
        monitors = self._monitor_list()
        # Сетка на каждый монитор — ровно та, по которой демон кладёт окна.
        grids = [
            self._grid_for(self.workspace, self._output_for(wa)) for wa in monitors
        ]
        zones = {}
        for grid in grids or [self.grid]:
            for name in sorted(grid.zones):
                zones.setdefault(name, list(grid.fractions(name)))
        zones_px = {}
        for name in zones:
            zones_px[name] = [
                list(grid.rect(name, wa)) if name in grid.zones else None
                for grid, wa in zip(grids, monitors)
            ]
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
            # Режим «только эта зона»: None или зона, цель и число свёрнутых.
            "focus": None
            if self.focus is None
            else {
                "zone": self.focus["zone"],
                "window": hex(self.focus["target"]),
                "minimized": len(self.focus["minimized"]),
            },
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
                raise ValueError(
                    _("монитора {index} нет (всего {total})").format(
                        index=index, total=len(monitors)
                    )
                )
            return monitors[index]
        wa = self._monitor_of(window)
        if wa is not None:
            return wa
        return monitors[0] if monitors else None

    def _known_zone_names(self):
        """Имена зон, доступные явным командам: активный конфиг и профили экранов.

        Команда приходит с именем зоны от апплета или раскладчика, а те
        показывают зоны **разрешённого** профиля — у экрана он может быть свой.
        Поэтому проверять имя только по активному `zones.conf` нельзя: зона
        профиля второго экрана была бы отвергнута как несуществующая.
        """
        names = set(self.grid.all_zone_names())
        for wa in self._monitor_list():
            grid = self._grid_for(self.workspace, self._output_for(wa))
            names |= set(grid.all_zone_names())
        return names

    def ipc_apply(self, request):
        """Вложить окно в зону по команде сокета (по умолчанию — активное).

        Успешная укладка запоминается в трекере демона: закрытие окна затем
        штатно запускает reflow, а не оставляет призрак. Сетка зоны — профиля
        того экрана, где стоит окно (см. `_grid_for`).
        """
        zone = request.get("zone")
        known = self._known_zone_names()
        if not isinstance(zone, str) or zone not in known:
            raise ValueError(
                _("нет зоны '{zone}'. Есть: {known}").format(
                    zone=zone, known=", ".join(sorted(known))
                )
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
            raise ValueError(
                _("не удалось определить монитор окна 0x{window:x}").format(
                    window=window
                )
            )
        grid = self._grid_for(self.workspace, self._output_for(wa))
        meta = self.read_meta(window)
        wm_class = getattr(meta, "wm_class", "") or ""
        if window_excluded(
            wm_class,
            getattr(meta, "wm_title", "") or "",
            getattr(meta, "window_type", ()),
            grid.ignore or self.grid.ignore,
        ):
            raise ValueError(
                _(
                    "окно 0x{window:x} под исключением ([ignore]/служебный тип) — "
                    "не двигаю"
                ).format(window=window)
            )
        # Окно вкладывается там, где сейчас пользователь: занятость считаем по
        # активному столу, а геометрию зоны — по профилю экрана окна.
        rect = self._settle_in_zone(
            window, zone, wa, self.workspace, wm_class, grid=grid
        )
        if rect is None:
            raise ValueError(
                _("не удалось вложить окно 0x{window:x} в зону '{zone}'").format(
                    window=window, zone=zone
                )
            )
        self.sync_strips()
        return {
            "window": hex(window),
            "zone": zone,
            "monitor": list(wa),
            "rect": list(rect),
        }

    def ipc_tabs_next(self, request=None):
        """Поднять следующую вкладку стопки зоны активного окна (`tabs-next`).

        Зону демон выбирает сам — по активному окну. Окно вне зон, стопка из
        одной вкладки и нечитаемое активное окно — не ошибка, а честный ответ
        `{"switched": false, ...}`: клавиша не должна ругаться на то, что
        переключать нечего.

        Возврат: `{"switched": true, "zone", "window" (hex), "previous" (hex)}`
        или `{"switched": false, "zone", "window": None, "reason"}`.
        """
        active = self._active_window()
        if active is None:
            return {
                "switched": False,
                "zone": None,
                "window": None,
                "reason": _("активное окно не прочитано — переключать нечего"),
            }
        zone = self.windows.get(active)
        if zone is None:
            return {
                "switched": False,
                "zone": None,
                "window": None,
                "reason": _("окно 0x{active:x} вне зон — переключать нечего").format(
                    active=active
                ),
            }
        stack = self._zone_stack(
            zone,
            self.win_monitor.get(active),
            self.win_workspace.get(active),
        )
        target = zone_tabs.next_tab(stack, active)
        if target is None:
            return {
                "switched": False,
                "zone": zone,
                "window": None,
                "reason": _("в зоне '{zone}' одна вкладка — переключать нечего").format(
                    zone=zone
                ),
            }
        if not self._raise_window(target):
            raise ValueError(
                _("не удалось поднять окно 0x{target:x} зоны '{zone}'").format(
                    target=target, zone=zone
                )
            )
        self.sync_strips()
        return {
            "switched": True,
            "zone": zone,
            "window": hex(target),
            "previous": hex(active),
        }

    def ipc_tabs_switch(self, request):
        """Показать окно зоны по клику в полосе (`tabs-switch`).

        Клик приходит из полосы в тот же сокет, что и команды `gridsnap-ctl`.
        Окно, которого в вкладках этой зоны нет (уже закрылось, ушло в другую
        зону или его вынесли), не поднимаем: `ValueError` вместо чужого окна
        поверх зоны.
        """
        request = request or {}
        zone = request.get("zone")
        if not isinstance(zone, str) or not zone:
            raise ValueError(_("в команде tabs-switch нет зоны"))
        window = request.get("window")
        if window is None:
            raise ValueError(_("в команде tabs-switch нет окна"))
        window = int(window, 0) if isinstance(window, str) else int(window)
        stack = self._zone_stack(
            zone, self.win_monitor.get(window), self.win_workspace.get(window)
        )
        if window not in stack:
            raise ValueError(
                _("окно 0x{window:x} не вкладка зоны '{zone}' — не поднимаю").format(
                    window=window, zone=zone
                )
            )
        if not self._raise_window(window):
            raise ValueError(
                _("не удалось поднять окно 0x{window:x} зоны '{zone}'").format(
                    window=window, zone=zone
                )
            )
        self.sync_strips()
        return {"zone": zone, "window": hex(window)}

    def _focus_covers(self, win, workspace, wa=None):
        """Сворачивает ли активный режим это окно: тот же стол и монитор режима.

        Правило отбора — то же, что у `tiler.focus_candidates` (чистая функция —
        один источник истины), одна запись: само окно, его стол и его рабочая
        область. `wa` передаётся, если монитор уже прочитан. Монитор окна не
        прочитать — не сворачиваем: хуже угадать и спрятать чужое.

        Вкладки зоны-цели не сворачиваются — ни те, что были в стопке на входе
        в режим (`_focus_candidates`), ни те, что правило положило туда уже во
        время режима: режим называется «только эта зона», и прятать окно самой
        зоны значит противоречить его смыслу (плюс полоса показывала бы
        свёрнутую вкладку, а `ipc_apply` в ту же зону окно не сворачивает).
        """
        state = self.focus
        if state is None:
            return False
        if wa is None:
            wa = self._monitor_of(win)
        if wa is None:
            return False
        entries = [(win, workspace, wa)]
        if not tiler.focus_candidates(
            entries, state["target"], state["workspace"], state["monitor"]
        ):
            return False
        if win in self._focus_zone_tabs(state["target"]):
            self.log(
                _("окно 0x{win:x} — вкладка зоны режима — не сворачиваю").format(
                    win=win
                )
            )
            return False
        return True

    def _focus_zone_tabs(self, target):
        """Вкладки зоны цели — их режим не сворачивает: они часть зоны.

        Стопка ищется в трекере (цель уже уложена в зону до отбора, или была
        уложена раньше). Цель в множестве всегда: сворачивать её самой себе
        нечего.
        """
        keep = {target}
        zone = self.windows.get(target)
        if zone is None:
            return keep
        keep.update(
            self._zone_stack(
                zone, self.win_monitor.get(target), self.win_workspace.get(target)
            )
        )
        return keep

    def _focus_candidates(self, target, wa):
        """Окна, которые режим сворачивает сейчас, в порядке `_NET_CLIENT_LIST`.

        Список клиентов читает демон (`read_clients`): только он видит окна,
        появившиеся после входа в режим. Отбор окон того же стола и монитора —
        `tiler.focus_candidates`; здесь же отсеиваются исключения
        (`window_excluded`: диалоги, `[ignore]`, служебные типы остаются
        видимыми — это требование продукта) и свёрнутое вручную: такое окно
        режим не сворачивал и на выходе не развернёт. Вкладки зоны-цели из
        отбора исключаются (`_focus_zone_tabs`): они часть той самой зоны,
        которую владелец попросил оставить.
        """
        try:
            client_ids = self.read_clients() or []
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось прочитать список клиентов: {exc}").format(exc=exc)
            )
            return []
        entries = [
            (win, self._window_workspace(win), self._monitor_of(win))
            for win in client_ids
        ]
        keep = self._focus_zone_tabs(target)
        out = []
        for win in tiler.focus_candidates(entries, target, self.workspace, wa):
            if win in out:
                continue  # дубль в списке клиентов
            if win in keep:
                self.log(
                    _("окно 0x{win:x} — вкладка зоны режима — остаётся видимой").format(
                        win=win
                    )
                )
                continue
            try:
                meta = self.read_meta(win)
            except (Exception, SystemExit) as exc:
                self.log(
                    _("не удалось прочитать метаданные 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                continue
            if window_excluded(
                getattr(meta, "wm_class", "") or "",
                getattr(meta, "wm_title", "") or "",
                getattr(meta, "window_type", ()),
                self.grid.ignore,
            ):
                self.log(
                    _(
                        "окно 0x{win:x} под исключением — в режиме остаётся видимым"
                    ).format(win=win)
                )
                continue
            try:
                iconic = tiler.window_iconic(win)
            except (Exception, SystemExit) as exc:
                self.log(
                    _("не удалось узнать состояние окна 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                iconic = False
            if iconic:
                self.log(
                    _("окно 0x{win:x} свёрнуто не режимом — не трогаю").format(
                        win=win
                    )
                )
                continue
            out.append(win)
        return out

    def _focus_minimize(self, win):
        """Свернуть окно и запомнить, что свернул именно режим; `False` — не вышло."""
        try:
            self.iconify(win, x11=self.x11, quiet=True)
        except (Exception, SystemExit) as exc:
            self.log(
                _("не удалось свернуть окно 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return False
        if self.focus is not None:
            self.focus["minimized"].add(win)
        self.log(
            _("окно 0x{win:x} свёрнуто режимом «только эта зона»").format(win=win)
        )
        return True

    def leave_focus(self, switch=False):
        """Выйти из режима: развернуть то, что свернул он сам.

        Возвращает `(развёрнутые, остаток, состояние режима)`: состояние — уже
        снятое с `self.focus`, оно нужно вызывающему, чтобы отдать остаток в
        `focus.json` (`_focus_handoff`).

        `tiler.deiconify_all` перечитывает состояние, поэтому в остатке — окна,
        которые так и остались свёрнутыми. Чаще всего это окна не на активном
        столе: muffin игнорирует активацию таких окон (проверено дымом). Что
        делать с остатком, решает вызывающий — `_focus_handoff` отдаёт его в
        `focus.json`, а не теряет.

        `switch=True` — сначала перейти на стол режима: так делает **явный** выход
        (toggle/`off`), ведь пользователь попросил вернуть окна. Неявные (цель
        исчезла, демон завершается) рабочий стол не трогают: дёргать пользователя
        из-за чужого окна хуже, чем оставить список CLI.
        """
        state, self.focus = self.focus, None
        if state is None:
            return [], [], None
        if switch and self.x11 is not None and isinstance(state.get("workspace"), int):
            try:
                tiler.switch_workspace(state["workspace"], x11=self.x11)
            except (Exception, SystemExit) as exc:  # стол нужен, но не обязателен
                self.log(
                    _("не удалось переключить рабочий стол: {exc}").format(exc=exc)
                )
        order = [win for win in sorted(state["minimized"]) if win != state["target"]]
        try:
            remaining = tiler.deiconify_all(
                order + [state["target"]], x11=self.x11, deiconify_fn=self.deiconify
            )
        except (Exception, SystemExit) as exc:
            self.log(_("не удалось развернуть окна режима: {exc}").format(exc=exc))
            remaining = list(order) + [state["target"]]
        restored = [
            win
            for win in order
            if win in state["minimized"] and win not in remaining
        ]
        self.log(
            _(
                "режим «только эта зона» выключен (зона '{zone}'): "
                "развёрнуто {restored} из {total}"
            ).format(
                zone=state["zone"] or _("без зоны"),
                restored=len(restored),
                total=len(state["minimized"]),
            )
        )
        return restored, remaining, state

    def _focus_records(self, windows):
        """Записи окон для `focus.json` — через инъекции демона, а не через X напрямую.

        Стол и `WM_CLASS` читаются **сейчас**: стол у каждого окна свой (в файле
        могут сойтись окна разных столов), а без класса запись уклонялась бы от
        защиты от переиспользованного XID.
        """
        records = []
        for win in dict.fromkeys(windows):
            try:
                wm_class = getattr(self.read_meta(win), "wm_class", "") or ""
            except (Exception, SystemExit) as exc:
                self.log(
                    _("не удалось прочитать метаданные 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                wm_class = ""
            records.append(
                {
                    "window": hex(win),
                    "wm_class": wm_class,
                    "workspace": self._window_workspace(win),
                }
            )
        return records

    def _focus_handoff(self, remaining, state):
        """Отдать неразвёрнутый остаток в `focus.json` — его доведёт `focus --off`.

        Окно на неактивном столе развернуть нельзя (`_NET_ACTIVE_WINDOW` muffin
        игнорирует для таких окон), а терять его нельзя тем более: демон режим
        больше не держит, и без записи оно осталось бы свёрнутым навсегда. Файл
        читает CLI, а `focus --off` сам переходит на стол записи.

        Записи строит `tiler.focus_records`: она читает `WM_CLASS` и **стол
        каждого окна** прямо сейчас. Иначе окно из остатка унаследовало бы чужой
        `workspace` старой записи (в ней могли остаться окна другого стола) — и
        разворот переходил бы не на тот стол, а окно так и осталось бы свёрнутым.
        Из старой записи остаются только те окна, которые ещё свёрнуты: уже
        развёрнутые разворачивать больше не нужно.
        """
        if not remaining:
            return
        path = getattr(self, "focus_path", None)
        if not path:
            self.log(
                _(
                    "{count} окон режима развернуть не удалось, но файла "
                    "состояния нет — разверните их вручную"
                ).format(count=len(remaining))
            )
            return
        old = tiler.load_focus_state(path)
        still_iconic = []
        for record in old.get("windows") or []:
            if not isinstance(record, dict):
                continue
            try:
                win = int(record.get("window"), 0)
            except (TypeError, ValueError):
                continue
            if tiler.window_iconic(win):
                still_iconic.append(win)
        windows = still_iconic + [
            win for win in remaining if win not in still_iconic
        ]
        records = self._focus_records(windows)
        target = None
        if state["target"] in windows:
            target = self._focus_records([state["target"]])[0]
        try:
            tiler.save_focus_state(
                {
                    "zone": state.get("zone"),
                    "workspace": state.get("workspace"),
                    "monitor": list(state.get("monitor") or []),
                    "target": target,
                    "windows": records,
                },
                path,
            )
        except OSError as exc:
            self.log(_("не удалось сохранить остаток режима: {exc}").format(exc=exc))
            return
        self.log(
            _(
                "{count} окон режима остались свёрнутыми — запись в {path}: "
                "развернуть командой focus --off"
            ).format(count=len(remaining), path=path)
        )

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

        `zone` необязательна: без неё (форма `[focus]` в `zones.conf` без зоны)
        цель остаётся где стоит, и сворачивается только окружение — укладки и
        записи в трекеры тогда нет, а в `self.focus["zone"]` лежит `None`.
        Монитор нужен и в этом случае: он задаёт область сворачивания.
        """
        request = request or {}
        if request.get("off") or self.focus is not None:
            # Явный выход и повторная команда (toggle) — одно и то же: снять режим
            # и развернуть только то, что свернул он сам. Стол переключаем (если
            # режим не на активном): так окна на другом столе тоже вернутся.
            restored, remaining, state = self.leave_focus(switch=True)
            self._focus_handoff(remaining, state)
            # `leave_focus` разворачивает окна и активирует цель последней:
            # активное окно (а с ним подсвеченная вкладка) могло смениться,
            # поэтому снимок полосы пересобирается и здесь, а не только на входе.
            self.sync_strips()
            return {"off": True, "restored": [hex(w) for w in restored]}
        zone = request.get("zone")
        known = self._known_zone_names()
        if zone is not None and (not isinstance(zone, str) or zone not in known):
            raise ValueError(
                _("нет зоны '{zone}'. Есть: {known}").format(
                    zone=zone, known=", ".join(sorted(known))
                )
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
                _(
                    "окно 0x{window:x} под исключением ([ignore]/служебный тип) — "
                    "режим «только эта зона» не включаю"
                ).format(window=window)
            )
        if self.workspace is None:
            raise ValueError(
                _(
                    "активный стол не прочитан — режим «только эта зона» не включаю: "
                    "сворачивать окна чужих столов вслепую хуже, чем не включать"
                )
            )
        # Монитор берётся только у цели и не угадывается: режим сворачивает окна
        # той же рабочей области, а `_monitor_for_command` при непрочитанном
        # положении падает на первый монитор — и режим прятал бы окна соседнего
        # экрана. У CLI в том же случае отказ.
        wa = self._monitor_of(window)
        if wa is None:
            raise ValueError(
                _(
                    "не удалось определить монитор окна 0x{window:x} — режим "
                    "«только эта зона» не включаю: его область зависит от монитора цели"
                ).format(window=window)
            )
        # Геометрия зоны — профиля того экрана, где стоит цель.
        grid = self._grid_for(self.workspace, self._output_for(wa))
        if zone is not None:
            # Цель сама становится вкладкой зоны: если там уже было окно, оба
            # встают на общую рамку, а полоса получает новый снимок.
            rect = self._settle_in_zone(
                window, zone, wa, self.workspace, wm_class, grid=grid
            )
            if rect is None:
                raise ValueError(
                    _("не удалось вложить окно 0x{window:x} в зону '{zone}'").format(
                        window=window, zone=zone
                    )
                )
        self.focus = {
            "zone": zone,
            "target": window,
            "workspace": self.workspace,
            "monitor": wa,
            "minimized": set(),
        }
        for win in self._focus_candidates(window, wa):
            self._focus_minimize(win)
        self.sync_strips()
        return {
            "zone": zone,
            "window": hex(window),
            "monitor": list(wa),
            "minimized": [hex(w) for w in sorted(self.focus["minimized"])],
        }

    def ipc_reload(self, request=None):
        """Перечитать zones.conf: сетку, правила, привязки экранов/столов и reflow.

        Кэши сбрасываются все, включая пары (стол, выход) и разобранные
        `[monitors]`/`[workspaces]`: новый профиль экрана подхватывается без
        рестарта демона.
        """
        self.grid = tiler.Grid(self.conf_path)
        self.reflow = read_reflow(self.grid.conf)
        self.tabs_height = read_tabs_height(self.grid.conf)
        self._grids = {}
        self._wa_cache = None
        self._mons_cache = None
        self._outputs_cache = None
        self._monitor_map_cache = None
        self._workspace_map_cache = None
        self.log(_("конфиг перечитан: {path}").format(path=self.conf_path))
        self.sync_strips()
        return {
            "conf": self.conf_path,
            "zones": sorted(self.grid.zones),
            "reflow": self.reflow,
        }

    def ipc_quit(self, request=None):
        """Попросить цикл демона завершиться после текущего события."""
        self.stop_requested = True
        self.log(_("получена команда quit — завершаюсь"))
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
            self.log(_("новое окно 0x{win:x} в _NET_CLIENT_LIST").format(win=win))
            self.on_map(win)
        for win in list(self.windows):
            if win not in current:
                self.on_destroy(win)
        # Свёрнутые режимом окна могут быть без зоны (правила не было) — их в
        # `windows` нет, но забыть их надо: разворачивать исчезнувшее нечем.
        for win in list(self.focus["minimized"]) if self.focus is not None else []:
            if win not in current and win not in self.windows:
                self.on_destroy(win)
        # Цель могла быть и не уложена в зону (форма `focus` без зоны): тогда
        # первый проход её не увидит, а режим без цели держать незачем.
        if self.focus is not None and self.focus["target"] not in current:
            self.on_destroy(self.focus["target"])
        self._prune_state(current)
        self.seen = current
        # Состав стопок мог измениться без укладки (окно вынесли руками, вторая
        # вкладка не вернулась после перезапуска): рамки приводим одним проходом.
        self._normalize_stacks(client_ids)
        # Картина вкладок поменялась (окна пришли/ушли): полосе — новый снимок.
        # Список клиентов уже прочитан — передаём его, чтобы не спрашивать X дважды.
        self.sync_strips(client_ids)

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
                _("активный стол не прочитался — остаётся {desk}").format(
                    desk=workspace_label(previous)
                )
            )
            return previous
        self.workspace = workspace
        if workspace == previous:
            self.log(
                _("активный стол не изменился: {desk}").format(
                    desk=workspace_label(workspace)
                )
            )
        else:
            self.log(
                _(
                    "активный стол: {desk} (был {previous}) — окна не двигаю"
                ).format(
                    desk=workspace_label(workspace),
                    previous=workspace_label(previous),
                )
            )
        if self.focus is not None and workspace != self.focus["workspace"]:
            # Режим живёт на своём столе и уход его НЕ снимает: окна того стола и
            # так не видны, вернувшись — увидишь режим как был. Снять его при
            # уходе нельзя: `_NET_ACTIVE_WINDOW` muffin игнорирует для окна не на
            # активном столе (проверено дымом), и окна остались бы свёрнутыми без
            # хозяина. Выход работает на своём столе или сам переводит на него.
            self.log(
                _(
                    "режим «только эта зона» живёт на столе {desk} — "
                    "оставляю его на месте"
                ).format(desk=workspace_label(self.focus["workspace"]))
            )
        # Полосы — только у зон текущего стола: снимок пересобирается на смену.
        self.sync_strips()
        return workspace

    def on_map(self, win, override_redirect=False):
        """Новое клиентское окно: вложить в зону по правилу, если оно подходит.

        Сетка берётся по экрану окна: `[monitors]` важнее `[workspaces]`,
        `[workspaces]` — важнее активного `zones.conf` (см. `_grid_for`). Правила
        и `[ignore]` читаются из **того же** профиля — они приходят внутри
        `Grid`, а профиль без своих правил (снимок одних зон) откатывается на
        правила активного конфига. «Липкое» окно и окно с неопознанным экраном
        живут в наборе следующего уровня. Имя зоны глобально: если в наборе
        экрана такой зоны нет, окно не укладывается (лучше никак, чем в чужую
        геометрию). Окно, попавшее в область активного режима «только эта зона»
        (тот же стол и монитор, что у цели), укладывается по правилу и сверх
        того сворачивается: так после выхода из режима оно окажется уже в
        своей зоне.
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
        # Стол и экран нужны до правил: у профиля экрана свои правила и свои
        # исключения, и активный `zones.conf` тут не указ. Без `[monitors]`
        # `_output_for` молчит и за `xrandr` не идёт — разбор идёт по столу.
        workspace = self._window_workspace(win)
        wa = self._monitor_of(win)
        grid = self._grid_for(workspace, self._output_for(wa))
        if window_excluded(
            wm_class,
            wm_title,
            getattr(meta, "window_type", ()),
            grid.ignore or self.grid.ignore,
        ):
            self.log(
                _(
                    "окно 0x{win:x} под исключением ([ignore]/служебный тип) — "
                    "не трогаю"
                ).format(win=win)
            )
            return
        zone = tiler.match_rules(
            grid.rules or self.grid.rules, wm_class, wm_title
        )
        if zone is None:
            # Правила нет — в режиме окно всё равно прячется (иначе оно окажется
            # поверх цели), но укладывать его некуда: просто сворачиваем.
            if self._focus_covers(win, workspace):
                self._focus_minimize(win)
            return
        if zone not in grid.all_zone_names():
            self.log(
                _(
                    "зоны '{zone}' нет в наборе (стол {desk}, экран {output}) — "
                    "окно 0x{win:x} не укладываю"
                ).format(
                    zone=zone,
                    desk=workspace_label(workspace),
                    output=self._output_for(wa) or _("неизвестен"),
                    win=win,
                )
            )
            if self._focus_covers(win, workspace):
                self._focus_minimize(win)
            return
        if wa is None:
            return  # монитор окна недоступен: не падаем и окно не трогаем
        # Правило дизайна волны: одно окно занимает зону целиком, два и больше
        # встают на общую рамку вкладок — и тогда переставляется и уже стоявшее.
        if self._settle_in_zone(win, zone, wa, workspace, wm_class, grid=grid) is None:
            return  # не запоминаем: окно не уложено, зону занятой не считаем
        self.log(
            _("окно 0x{win:x} -> зона '{zone}' (стол {desk})").format(
                win=win, zone=zone, desk=workspace_label(workspace)
            )
        )
        # Режим «только эта зона»: окно уже в своей зоне — теперь прячем его,
        # чтобы после выхода оно оказалось именно там, а не где пришлось.
        if self._focus_covers(win, workspace, wa):
            self._focus_minimize(win)

    def on_destroy(self, win):
        """Окно исчезло из `_NET_CLIENT_LIST` — снять с трекера и, если нужно, reflow.

        Перестройка — только когда стол закрытого окна виден (`self.workspace`):
        окна невидимых столов не двигаются никогда, состояние такого стола
        просто чистится.

        Режим «только эта зона» снимается здесь же: его цель исчезла — значит,
        держать остальные окна свёрнутыми незачем. Ушедшее свёрнутое окно
        убирается из `minimized` (разворачивать нечего), но при живой цели
        режим остаётся.
        """
        state = self.focus
        if state is not None:
            state["minimized"].discard(win)
            if win == state["target"]:
                self.log(
                    _(
                        "цель режима «только эта зона» 0x{win:x} исчезла — "
                        "выхожу из режима"
                    ).format(win=win)
                )
                _unused, remaining, consumed = self.leave_focus()
                self._focus_handoff(remaining, consumed)
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        workspace = self.win_workspace.pop(win, None)
        self.log(
            _("окно 0x{win:x} ушло из зоны '{zone}'").format(win=win, zone=zone)
        )
        # Зона занята, пока в ней есть хоть одна вкладка: ушла последняя — ниже
        # reflow увидит зону свободной, а пока в стопке осталась одна — ей
        # возвращается вся зона (полосы у одиночного окна нет).
        self._collapse_stack(zone, wa, workspace)
        # Держатель исчез: снять записи о зонах, которые он покрывал.
        self._drop_coverage(win)
        if self.reflow != REFLOW_NEIGHBORS or wa is None:
            return
        if not self._visible(workspace):
            self.log(
                _("стол {desk} не виден (сейчас {current}) — reflow не делаю").format(
                    desk=workspace_label(workspace),
                    current=workspace_label(self.workspace),
                )
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
        grid = self._grid_for(self.workspace, self._output_for(wa))
        zones = {name: grid.fractions(name) for name in grid.zones}
        changes = zone_reflow.reflow(zones, zone, occupied)
        if not changes:
            return
        for name, frac in changes.items():
            if len(self._zone_stack(name, wa, self.workspace)) > 1:
                # В зоне стопка вкладок: они живут на общей рамке, и вынуть из
                # неё одно окно — значит разложить стопку. Такую зону не
                # расширяем, а зону не считаем покрытой.
                self.log(
                    _("reflow: в зоне '{zone}' вкладки — не расширяю").format(
                        zone=name
                    )
                )
                continue
            target = self._window_for_zone(name, wa)
            if target is None:
                continue
            fx, fy, fw, fh = frac
            if not self._apply(target, fractions_to_pixels(fx, fy, fw, fh, wa), name):
                continue  # окно не переехало: зону покрытой не считаем
            self.filled[(self.workspace, wa, zone)] = target
            self.log(
                _("reflow: окно 0x{win:x} -> зона '{zone}'").format(
                    win=target, zone=name
                )
            )
        self._save_state()

    def on_unmap(self, win):
        """Окно скрыто (свёрнуто/сменился воркспейс) — no-op.

        В `_NET_CLIENT_LIST` такое окно остаётся, поэтому реконсиляция его не
        снимает с трекера: при разворачивании окно не переставляется заново, а
        соседи не расширяются на занятую зону. Снятие с трекера и reflow —
        только когда окно исчезает из `_NET_CLIENT_LIST` (`on_destroy`), и там
        же стоит проверка «стол окна виден»: перестраивать здесь нечего.
        """
        self.log(_("окно 0x{win:x} скрыто — оставляем в трекере").format(win=win))


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
            log(_("_NET_CLIENT_LIST недоступен — реконсиляцию пропускаю"))
        return True
    dae.reconcile(client_ids)
    return True


def _reconcile_if_known(dae, read_clients):
    """Прочитать список клиентов и свести состояние; `None` — пропустить."""
    client_ids = read_clients()
    if client_ids is None:
        dae.log(_("_NET_CLIENT_LIST недоступен — реконсиляцию пропускаю"))
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
        dae.log(_("ошибка IPC-сокета: {exc}").format(exc=exc))
        return
    for response in responses:
        if response.get("ok"):
            dae.log("IPC: " + json.dumps(response, ensure_ascii=False))
        else:
            dae.log(
                _("IPC-ошибка: {error}").format(error=response.get("error", "?"))
            )


def _restore_focus_on_exit(dae):
    """Развернуть окна режима «только эта зона» при выходе демона (best-effort).

    Зовётся из `finally` у `serve` — до `XCloseDisplay`, иначе посылать будет
    уже некуда. Если X-соединения нет (ранний выход, отказ X-сервера), просто
    снимаем состояние: так после выхода демона никто не останется со свёрнутыми
    окнами без хозяина.
    """
    if getattr(dae, "focus", None) is None:
        return
    log = getattr(dae, "log", None)
    if getattr(dae, "x11", None) is None:
        dae.focus = None
        if log is not None:
            log(_("X-соединения нет — окна режима «только эта зона» не разворачиваю"))
        return
    try:
        _unused, remaining, state = dae.leave_focus()
        dae._focus_handoff(remaining, state)
    except (Exception, SystemExit) as exc:
        if log is not None:
            log(
                _("не удалось выйти из режима «только эта зона»: {exc}").format(
                    exc=exc
                )
            )


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
    dae.log(
        _("демон слушает X11 (root 0x{root:x}, reflow={reflow})").format(
            root=x11.root, reflow=dae.reflow
        )
    )
    if ipc is not None:
        dae.log(_("IPC-сокет: {path}").format(path=getattr(ipc, "path", "?")))
    try:
        # Раскладываем окна, уже открытые к запуску (вход в готовую сессию).
        _reconcile_if_known(dae, read_clients)
        while True:
            if not x11.x.XPending(x11.dpy):
                ready, _unused, _unused_again = select.select(
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
        # Режим «только эта зона» не должен пережить демон: иначе свёрнутые окна
        # останутся свёрнутыми без хозяина. Полоса вкладок тоже живёт ровно
        # столько, сколько демон: EOF в stdin завершает её главный цикл.
        _restore_focus_on_exit(dae)
        strip = getattr(dae, "strip", None)
        if strip is not None:
            strip.stop()
        x11.x.XCloseDisplay(x11.dpy)


# ------------------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=_(
            "Автоукладка окон GridSnap по правилам [rules] и reflow (Cinnamon/X11)"
        )
    )
    ap.add_argument(
        "--conf",
        default=None,
        help=_("путь к zones.conf (по умолчанию XDG/рядом с кодом)"),
    )
    ap.add_argument(
        "--verbose", action="store_true", help=_("печатать, что делает демон")
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help=_("обработать одно изменение _NET_CLIENT_LIST и выйти"),
    )
    ap.add_argument(
        "--socket",
        default=None,
        help=_(
            "путь UNIX-сокета IPC (по умолчанию $XDG_RUNTIME_DIR/gridsnap/daemon.sock)"
        ),
    )
    ap.add_argument(
        "--no-socket", action="store_true", help=_("не поднимать IPC-сокет")
    )
    ap.add_argument("--version", action="version", version=f"zone-daemon {VERSION}")
    args = ap.parse_args(argv)

    conf_path = args.conf or tiler.default_conf()
    dae = Daemon(
        conf_path,
        state_path=placed_path(),
        session_id=read_session_id(),
        focus_path=tiler.focus_path(),
        strip=TabStripProcess(conf_path, verbose=args.verbose),
        verbose=args.verbose,
    )
    server = None
    if not args.no_socket:
        path = args.socket or zone_ipc.default_socket_path()
        try:
            server = zone_ipc.Server(path, dae.build_dispatcher())
        except OSError as exc:
            print(
                "zone-daemon: " + _("IPC-сокет: {exc}").format(exc=exc),
                file=sys.stderr,
            )
            return tiler.EXIT_FAIL
    try:
        serve(dae, once=args.once, ipc=server)
    finally:
        if server is not None:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

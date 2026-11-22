#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-hotcorners — watcher перетаскивания окна: водит HUD ``zone-picker --hud``
и вкладывает окно в зону под точкой отпускания (Cinnamon/Muffin, X11).

События «пользователь тащит окно» в EWMH нет: ``_NET_WM_MOVERESIZE`` только
начинает drag, а перетаскивание за титлбар — внутренний grab muffin. Поэтому
watcher слушает XInput2 raw events на root: они приходят «at all times, even
when the device is grabbed by another client». Ни XGrabPointer, ни XTest, ни
XRecord не нужны и не используются.

Цикл:

1. ``RawButtonPress`` (кнопка 1) — запоминаем, что кнопка нажата, и «arm»:
   попала ли точка нажатия в полосу титлбара активного окна (высота полосы —
   ``_NET_FRAME_EXTENTS.top`` плюс запас). Нажатие мимо титлбара — обычный клик
   в приложении, watcher ничего не делает.
2. ``RawMotion`` при нажатой кнопке и «arm» — точку (её берём ``XQueryPointer``:
   raw-значения у относительных устройств — смещения, а не координаты) отдаём
   в HUD и не чаще ~30 раз в секунду. HUD — дочерний ``zone-picker.py --hud
   --follow`` на мониторе под точкой; при переходе на другой монитор он
   перезапускается там. Пока HUD жив, edge-tiling muffin гасится: наш дроп
   главный, а его OSD при тайлинге у края мешает.
3. ``RawButtonRelease`` — точка отпускания переводится в доли рабочей области
   монитора (``zone_picker_nav.zone_at``); попала в зону — окно вкладывается
   через ``zone-tiler.apply_zone``, мимо зон — не делается ничего. HUD гасится,
   edge-tiling возвращается в прежнее значение.

Если XI2 не поднялся (нет libXi, старый сервер, отказ ``XIQueryVersion``),
watcher уходит в fallback: опрос ``XQueryPointer`` ~30 Гц по позиции и маске
кнопок. Поведение то же, только вместо событий — сравнение двух срезов.

Возврат edge-tiling обязателен: он делается на дропе, на отмене, в ``finally``
и по ``SIGTERM``/``SIGINT``. Другие настройки Cinnamon не трогаются.

Запуск:
  zone-hotcorners.py                # слушать перетаскивания до Ctrl-C
  zone-hotcorners.py --dry-run      # печатать, что вложил бы, но окно не двигать
  zone-hotcorners.py --no-hud       # без оверлея: только дроп по отпусканию
  zone-hotcorners.py --verbose      # писать в stderr, что происходит
  zone-hotcorners.py --conf PATH

Чистая часть (без X и без gsettings) — в начале файла: попадание в титлбар,
троттлинг точек, «точка → монитор → зона», разбор аргументов и состояние
edge-tiling как данные. Она проверяется ``tests/test_hotcorners.py`` без
дисплея; X и запуск процессов — в ``Hotcorners``/``XClient``/``HudProcess``.
"""

import argparse
import ctypes
import ctypes.util
import importlib.util
import os
import select
import signal
import subprocess
import sys
import time

VERSION = "0.2.0"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zone_i18n import _  # noqa: E402  (там же: строки интерфейса и их перевод)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

PICKER = os.path.join(HERE, "zone-picker.py")


def _load_sibling(name, filename):
    """Загрузить соседний hyphen-модуль (zone-tiler.py) без зависимости от sys.path."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(_("не удалось загрузить модуль {}").format(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_hotcorners_tiler", "zone-tiler.py")
nav = _load_sibling("gridsnap_hotcorners_nav", "zone_picker_nav.py")


# ------------------------------------------------------------------ чистая часть

# Запас к полосе титлбара: нажатие чуть ниже неё тоже считается «за титлбар».
# Мышью легко попасть на 1–2 px ниже, а «arm» — это разрешение тащить окно, а
# не точное измерение; ложное срабатывание стоит одного лишнего HUD, промах —
# неработающего перетаскивания.
TITLE_SLACK = 8

# Если `_NET_FRAME_EXTENTS` не прочитались (нет xprop, окно без рамки), полоса
# титлбара всё равно должна существовать: у muffin она около 30 px.
TITLE_FALLBACK_TOP = 30

# Точки в HUD — не чаще 30 раз в секунду: чаще курсор всё равно не двигается
# заметно, а каждое сообщение — перерисовка оверлея.
POINT_INTERVAL = 1.0 / 30.0

# Период опроса в fallback-режиме (XI2 не поднялся).
POLL_INTERVAL = 1.0 / 30.0

# Допуск сравнения времён: точка ровно через интервал не должна теряться.
EPS = 1e-9

# Ожидание активации окна: muffin активирует окно по нажатию, и raw-событие
# может прийти раньше, чем `_NET_ACTIVE_WINDOW` обновится.
ARM_RETRY_DELAY = 0.05


def normalize_bool_text(text):
    """``'true'``/``'false'`` (как печатает gsettings) или ``None``.

    Чистая функция: так исходное значение edge-tiling хранится и проверяется
    как данные, без запуска gsettings.
    """
    value = ("" if text is None else str(text)).strip().lower()
    return value if value in ("true", "false") else None


def title_band(top, slack=TITLE_SLACK):
    """Высота полосы титлбара с запасом, в пикселях (не меньше fallback)."""
    try:
        height = int(top)
    except (TypeError, ValueError):
        height = 0
    if height <= 0:
        height = TITLE_FALLBACK_TOP
    return height + max(0, int(slack))


def is_title_bar_press(point, frame, top, slack=TITLE_SLACK):
    """Попало ли нажатие ``point`` в полосу титлбара окна с рамкой ``frame``?

    ``frame`` — внешняя рамка окна ``(x, y, w, h)`` в экранных пикселях (её даёт
    ``zone-tiler.read_frame``: клиент плюс ``_NET_FRAME_EXTENTS``); ``top`` —
    верхняя рамка из ``frame_extents``, то есть высота титлбара без запаса.
    Полоса — от верхнего края рамки вниз на :func:`title_band`, по всей ширине
    окна. Нажатие выше или ниже полосы, левее/правее окна — не «arm».
    Чистая функция — под тесты.
    """
    if frame is None or point is None:
        return False
    px, py = point
    fx, fy, fw, fh = (float(v) for v in frame)
    if fw <= 0 or fh <= 0:
        return False
    return fx <= px < fx + fw and fy <= py < fy + title_band(top, slack)


def should_emit(last, now, interval=POINT_INTERVAL):
    """Пора ли отдать очередную точку в HUD (троттлинг ~30 Гц).

    ``last`` — время прошлой отданной точки (``None`` — точек ещё не было).
    Чистая функция: времени она не читает, поэтому тест задаёт его сам.
    Допуск ``EPS`` — чтобы точка ровно через интервал не терялась на
    погрешности float (``1000 + 1/30 - 1000`` чуть меньше ``1/30``).
    """
    if last is None:
        return True
    return now - last >= interval - EPS


def poll_events(prev, now):
    """События перетаскивания по двум срезам ``XQueryPointer``.

    Срез — ``(x, y, button1_down)``; ``now=None`` — опрос не удался. Возвращает
    список ``('press'|'motion'|'release', (x, y))``. Первый срез с уже нажатой
    кнопкой даёт ``press`` (watcher запустили посреди перетаскивания), дальше
    сравниваются соседние срезы. Чистая функция — под тесты и для fallback.
    """
    if now is None:
        return []
    x, y, down = now
    if prev is None:
        return [("press", (x, y))] if down else []
    px, py, pdown = prev
    events = []
    if down and not pdown:
        # Нажатие и есть событие этого такта: точку «когда нажали» точнее
        # текущей не знает никто, а отдельное motion с той же точкой — шум.
        events.append(("press", (x, y)))
    elif down and (x, y) != (px, py):
        events.append(("motion", (x, y)))
    if pdown and not down:
        events.append(("release", (x, y)))
    return events


def monitor_index_for_point(outputs, point):
    """Индекс монитора под точкой; ``None`` — мониторов нет.

    ``outputs`` — ``[(имя выхода, (x, y, w, h))]`` как у
    ``zone-tiler.monitor_outputs``. Точка вне всех областей (щель между
    мониторами, панель) достаётся ближайшему по центру монитору — тем же
    правилом, что и в раскладчике (``zone-tiler.monitor_for_point``), чтобы
    watcher и HUD не расходились в выборе экрана.
    """
    if not outputs:
        return None
    areas = [wa for _, wa in outputs]
    target = tiler.monitor_for_point(areas, point)
    return areas.index(target)


def zone_rects(grid, monitor_name=None):
    """``[(имя зоны, доли (x, y, w, h))]`` для набора зон монитора.

    Порядок — как в файле конфига: ``zone_picker_nav.zone_at`` при перекрытии
    выбирает последнюю нарисованную зону, и это должно совпадать с HUD.
    """
    zones = grid.zones_for(monitor_name)
    return [(name, grid.fractions(name, monitor_name)) for name in zones]


def zone_under(rects, area, point):
    """``(имя, доли)`` зоны под экранной точкой или ``None`` — мимо зон.

    Точка переводится в доли рабочей области монитора ``area`` и ищется той же
    ``zone_picker_nav.zone_at_point``, что и в HUD: подсвеченная зона и зона
    дропа — один и тот же код.
    """
    index = nav.zone_at_point([fractions for _, fractions in rects], area, point)
    if index is None:
        return None
    return rects[index]


class EdgeTiling:
    """Состояние edge-tiling muffin: запомнить, погасить, вернуть.

    Значение настройки живёт здесь как данные (``original`` — ``'true'`` /
    ``'false'`` / ``None``), а gsettings запускается через ``runner``: в тестах
    это записывающая заглушка, в бою — `subprocess.run`. Решение «гасить ли»
    тоже чистое: если настройка уже выключена, её не трогают вовсе, чтобы не
    «включать» её потом по ошибке.
    """

    SCHEMA = "org.cinnamon.muffin"
    KEY = "edge-tiling"

    def __init__(self, original=None, runner=None):
        self.original = normalize_bool_text(original)
        self.runner = runner or run_gsettings
        self.touched = False

    @classmethod
    def get_args(cls):
        return ["gsettings", "get", cls.SCHEMA, cls.KEY]

    @classmethod
    def set_args(cls, value):
        return ["gsettings", "set", cls.SCHEMA, cls.KEY, value]

    @property
    def restore_value(self):
        """Куда возвращать: исходное значение, а без него — ``'true'``.

        Незнакомое/пустое исходное значение трактуется как «было включено»:
        оставить edge-tiling выключенным после нас хуже, чем включить его там,
        где он и был включён по умолчанию.
        """
        return self.original or "true"

    def read(self):
        """Прочитать текущее значение (``self.original``); ``None`` при сбое."""
        code, out = self.runner(self.get_args())
        self.original = normalize_bool_text(out) if code == 0 else None
        return self.original

    def disable(self):
        """Погасить edge-tiling; ``False`` — не понадобилось или не вышло."""
        if self.original == "false":  # уже выключен: не трогаем вовсе
            return False
        code, _ = self.runner(self.set_args("false"))
        if code != 0:
            return False
        self.touched = True
        return True

    def restore(self):
        """Вернуть исходное значение; ``False`` — мы ничего не меняли."""
        if not self.touched:
            return False
        code, _ = self.runner(self.set_args(self.restore_value))
        if code == 0:
            self.touched = False
        return code == 0


def run_gsettings(argv):
    """``(код возврата, stdout)`` для argv gsettings; сбой запуска — ``(1, '')``."""
    try:
        done = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


# ------------------------------------------------------------------ HUD и дроп


class HudProcess:
    """Дочерний ``zone-picker.py --hud --follow`` на одном мониторе.

    Точки пишутся ему в stdin строками ``X Y`` — тот же формат, что у
    ``--follow``; закрытие stdin (EOF) завершает его главный цикл, поэтому
    останов — это сначала мягкий EOF, потом ``terminate``/``kill``.
    """

    def __init__(self, conf, picker=PICKER, python=None, verbose=False):
        self.conf = conf
        self.picker = picker
        self.python = python or sys.executable
        self.verbose = verbose
        self.proc = None
        self.index = None
        self.name = ""
        self.failed = False

    def command(self, index):
        return [
            self.python,
            self.picker,
            "--hud",
            "--follow",
            "--conf",
            self.conf,
            "--monitor",
            str(index),
        ]

    def start(self, index, name=""):
        """Запустить HUD на мониторе ``index``; уже запущенный — не трогаем.

        Сбой запуска (нет ``python3-gi``, нет ``zone-picker.py``) не должен
        ронять watcher: дроп работает и без оверлея, поэтому запоминаем отказ
        и больше не пытаемся.
        """
        if self.proc is not None and self.index == index:
            return False
        if self.failed:
            return False
        self.stop()
        try:
            self.proc = subprocess.Popen(
                self.command(index),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if self.verbose else subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.failed = True
            if self.verbose:
                print(
                    _("zone-hotcorners: HUD не запустился: {}").format(exc),
                    file=sys.stderr,
                    flush=True,
                )
            return False
        self.index = index
        self.name = name
        return True

    def feed(self, point):
        """Отдать HUD точку; ``False`` — канал закрыт (HUD завершился)."""
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(f"{int(point[0])} {int(point[1])}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.stop()
            return False
        return True

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Погасить HUD: EOF в stdin, затем terminate/kill. Идемпотентно."""
        proc, self.proc = self.proc, None
        self.index = None
        self.name = ""
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


class Hotcorners:
    """Ядро watcher'а: события кнопки/движения -> HUD и дроп.

    Зависимости инъектируются (как у ``zone-daemon.Daemon``), поэтому весь
    сценарий перетаскивания проверяется юнит-тестами без X, без HUD и без
    gsettings:

      active_window  — id активного окна;
      frame_extents  — толщина рамки окна ``(left, right, top, bottom)``;
      read_frame     — внешняя рамка окна ``(x, y, w, h)``;
      outputs        — ``[(имя выхода, рабочая область)]``;
      grid_for       — ``Grid`` для окна (по его рабочему столу);
      apply          — вложить окно в пиксельную рамку;
      hud            — :class:`HudProcess` или ``None`` (``--no-hud``);
      edge           — :class:`EdgeTiling`;
      clock/sleep    — время и пауза (в тестах — управляемые).
    """

    def __init__(
        self,
        conf,
        dry_run=False,
        no_hud=False,
        verbose=False,
        active_window=None,
        frame_extents=None,
        read_frame=None,
        outputs=None,
        grid_for=None,
        apply=None,
        is_excluded=None,
        hud=None,
        edge=None,
        clock=time.monotonic,
        sleep=time.sleep,
        out=None,
        err=None,
    ):
        self.conf = conf
        self.dry_run = dry_run
        self.no_hud = no_hud
        self.verbose = verbose
        self.active_window = active_window or tiler.active_window
        self.frame_extents = frame_extents or tiler.frame_extents
        self.read_frame = read_frame or tiler.read_frame
        self.outputs = outputs or tiler.monitor_outputs
        self.grid_for = grid_for or self.default_grid
        self.apply = apply or (lambda win, rect, label: tiler.apply_zone(win, rect, label=label))
        self.is_excluded = is_excluded or tiler.window_is_excluded
        self.hud = None if no_hud else (hud if hud is not None else HudProcess(conf, verbose=verbose))
        self.edge = edge or EdgeTiling()
        self.clock = clock
        self.sleep = sleep
        self.out = out or (lambda text: print(text, flush=True))
        self.err = err or (lambda text: print(text, file=sys.stderr, flush=True))
        self.reset()

    # ---- состояние перетаскивания

    def reset(self):
        """Забыть текущее перетаскивание (без побочных эффектов HUD/gsettings)."""
        self.pressed = False
        self.armed = False
        self.drag_win = None
        self.grid = None
        self.monitors = []
        self.title_top = 0
        self.last_point = None
        self.rects = {}

    def log(self, text):
        if self.verbose:
            self.err(f"zone-hotcorners: {text}")

    def default_grid(self, win):
        """Сетка окна: профиль его рабочего стола (``grid_for_workspace``)."""
        workspace = tiler.window_workspace_for_zones(win)
        return tiler.grid_for_workspace(self.conf, workspace)

    def read_monitors(self):
        """Рабочие области мониторов один раз на перетаскивание.

        ``zone-tiler.monitor_outputs`` дёргает xrandr и GDK, а точки идут до
        30 раз в секунду — поэтому список читается на «arm», а не на каждое
        движение. Сбой чтения не роняет watcher: без мониторов просто нет дропа.
        """
        try:
            return list(self.outputs())
        except SystemExit:  # tiler.fail: не удалось определить рабочую область
            self.err(_("zone-hotcorners: не удалось прочитать рабочие области мониторов"))
            return []
        except Exception as exc:
            self.err(_("zone-hotcorners: мониторы не прочитаны: {}").format(exc))
            return []

    def zones_for(self, index, name):
        """Зоны монитора (с кэшем на время одного перетаскивания)."""
        if index not in self.rects:
            self.rects[index] = zone_rects(self.grid, name)
        return self.rects[index]

    # ---- «arm»: нажатие в титлбаре

    def arm_at(self, point):
        """Окно, чей титлбар накрывает точку нажатия, или ``None``.

        Активное окно может обновиться чуть позже самого нажатия (muffin
        активирует окно по нажатию), поэтому промах повторяется один раз после
        короткой паузы — но только промах: попав, мы не ждём зря.
        """
        for attempt in (0, 1):
            try:
                win = self.active_window()
            except SystemExit:  # tiler.fail: в сессии нет активного окна
                self.log(_("активного окна нет — это обычный клик"))
                win = None
            if win:
                extents = self.frame_extents(win)
                try:
                    frame = self.read_frame(win, extents)
                except Exception as exc:  # xwininfo не прочитал геометрию
                    self.log(_("рамка окна 0x{:x} не прочитана: {}").format(win, exc))
                    frame = None
                if is_title_bar_press(point, frame, extents[2]):
                    return win, extents[2]
            if attempt == 0:
                self.sleep(ARM_RETRY_DELAY)
        return None

    def on_press(self, point, button=1):
        """Кнопка нажата: запомнить нажатие и, если это титлбар, «arm»."""
        if button != 1 or point is None:
            return False
        self.finish_drag()  # предыдущее перетаскивание не должно протечь
        self.pressed = True
        found = self.arm_at(point)
        if found is None:
            self.log(_("нажатие {} вне титлбара активного окна — обычный клик").format(point))
            return False
        self.drag_win, self.title_top = found
        self.armed = True
        try:
            self.grid = self.grid_for(self.drag_win)
        except SystemExit:  # tiler.fail: битый конфиг или нет конфига
            self.err(_("zone-hotcorners: сетка зон не прочитана, перетаскивание брошено"))
            self.finish_drag()
            return False
        if self.is_excluded(self.drag_win, self.grid.ignore):
            self.log(
                _(
                    "окно 0x{:x} под исключением ([ignore]/служебный тип) — "
                    "перетаскивание не отслеживаю"
                ).format(self.drag_win)
            )
            self.finish_drag()
            return False
        self.monitors = self.read_monitors()
        self.rects = {}
        self.last_point = None
        self.log(
            _("arm: окно 0x{:x}, титлбар {} px, нажатие {}").format(
                self.drag_win, title_band(self.title_top), point
            )
        )
        return True

    # ---- движение: HUD

    def hud_point(self, point):
        """Отдать точку в HUD (запустив/перезапустив его на нужном мониторе)."""
        if self.hud is None or point is None:
            return False
        outputs = self.monitors
        index = monitor_index_for_point(outputs, point)
        if index is None:
            return False
        name, _area = outputs[index]
        if self.hud.start(index, name):
            self.log(_("HUD на мониторе {}: {}").format(index, name))
            self.disable_edge_tiling()
        return self.hud.feed(point)

    def motion_due(self):
        """Стоит ли вообще опрашивать позицию по очередному RawMotion.

        RawMotion приходит с частотой устройства (у игровых мышей — до 1000 Гц), а
        позиция нужна только пока жив HUD: до «arm», без HUD и чаще 30 Гц
        спрашивать ``XQueryPointer`` незачем. Предикат передаётся в
        ``XClient.read_events`` и решает это ДО запроса к серверу; само решение
        «отдать точку» остаётся в :meth:`on_motion`.
        """
        if self.hud is None or not (self.pressed and self.armed):
            return False
        return should_emit(self.last_point, self.clock())

    def on_motion(self, point):
        """Движение при нажатой кнопке и «arm»: точка в HUD (не чаще 30 Гц)."""
        if not (self.pressed and self.armed) or point is None:
            return False
        now = self.clock()
        if not should_emit(self.last_point, now):
            return False
        self.last_point = now
        return self.hud_point(point)

    # ---- отпускание: дроп

    def target_at(self, point):
        """``(имя, доли, область, имя выхода, индекс)`` зоны под точкой."""
        if point is None or not self.monitors:
            return None
        outputs = self.monitors
        index = monitor_index_for_point(outputs, point)
        if index is None:
            return None
        name, area = outputs[index]
        try:
            found = zone_under(self.zones_for(index, name), area, point)
        except SystemExit:  # битая секция [zones:<выход>]
            self.err(_("zone-hotcorners: зоны монитора {} не прочитаны").format(name))
            return None
        if found is None:
            return None
        return found[0], found[1], area, name, index

    def on_release(self, point, button=1):
        """Кнопка отпущена: вернуть настройки, вложить окно в зону под точкой."""
        if button != 1:
            return False
        was_armed = self.pressed and self.armed
        win = self.drag_win
        grid = self.grid
        if not was_armed or win is None or grid is None:
            self.finish_drag()
            return False
        target = self.target_at(point)
        rect = None
        if target is not None:
            zone, _fractions, area, output, index = target
            rect = grid.rect(zone, area, monitor_name=output)
        # HUD и edge-tiling гасим ДО укладки: иначе muffin может дотэйлить окно
        # по своему правилу края уже после нашего move/resize.
        self.finish_drag()
        if rect is None:
            self.log(_("отпущено в {} — мимо зон, окно не двигаем").format(point))
            return False
        x, y, w, h = rect
        if self.dry_run:
            self.out(
                _(
                    "окно 0x{:x} -> зона '{}': x={} y={} {}x{} "
                    "(монитор {}: {}) — dry-run, окно не двигаем"
                ).format(win, zone, x, y, w, h, index, output)
            )
            return True
        self.log(
            _("окно 0x{:x} -> зона '{}': x={} y={} {}x{}").format(win, zone, x, y, w, h)
        )
        try:
            self.apply(win, rect, zone)
        except (SystemExit, OSError, subprocess.SubprocessError) as exc:
            # Долгоживущий watcher не должен умирать из-за одного окна или
            # пропавшего X-соединения: сообщаем и продолжаем слушать.
            self.err(
                _("zone-hotcorners: окно 0x{:x} не вложено: {}").format(
                    win, exc or _("сбой X")
                )
            )
            return False
        return True

    # ---- edge-tiling

    def disable_edge_tiling(self):
        """Погасить edge-tiling на время HUD (без HUD настройка не трогается).

        В ``--dry-run`` настройка тоже не трогается: dry-run обещает не менять
        ничего, кроме собственного вывода.
        """
        if self.hud is None or self.edge.touched:
            return False
        if self.dry_run:
            self.log(_("dry-run: edge-tiling не трогаем"))
            return False
        if self.edge.original is None:
            value = self.edge.read()
            self.log(_("edge-tiling сейчас: {}").format(value or _("не прочитан")))
        if self.edge.disable():
            self.log(
                _("edge-tiling выключен на время HUD (было {})").format(
                    self.edge.restore_value
                )
            )
            return True
        return False

    def finish_drag(self):
        """Погасить HUD, вернуть edge-tiling и забыть перетаскивание.

        Вызывается на дропе, на отмене (сигнал) и в ``finally`` — поэтому
        настройка возвращается при любом выходе, а не только на удачном дропе.
        """
        if self.hud is not None:
            self.hud.stop()
        if self.edge.touched:
            if self.edge.restore():
                self.log(_("edge-tiling возвращён: {}").format(self.edge.restore_value))
            else:
                self.err(_("zone-hotcorners: не удалось вернуть edge-tiling в gsettings"))
        self.reset()


# ------------------------------------------------------------------ X11 (ctypes)

GENERIC_EVENT = 35

XI_RawButtonPress = 15
XI_RawButtonRelease = 16
XI_RawMotion = 17

XI_ALL_MASTER_DEVICES = 1

BUTTON1 = 1
# Маска кнопок у XQueryPointer: Button1Mask (X.h).
BUTTON1_MASK = 1 << 8

# XEvent — объединение 192 байта на LP64: XNextEvent пишет в него событие
# целиком, поэтому буфер не может быть меньше, чем сам XEvent.
EVENT_BUFFER = ctypes.c_char * 192


class XGenericEventCookie(ctypes.Structure):
    """Префикс ``XGenericEventCookie``: через него Xlib отдаёт события XI2."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int),
        ("evtype", ctypes.c_int),
        ("cookie", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    ]


class XIRawEvent(ctypes.Structure):
    """Префикс ``XIRawEvent`` — до ``detail``/``flags`` включительно.

    Координаты raw-события намеренно не читаются: у относительных устройств
    ``raw_values`` — смещения, а не позиция. Позиция всегда берётся
    ``XQueryPointer``, поэтому она одна и та же в XI2 и в fallback.
    """

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int),
        ("evtype", ctypes.c_int),
        ("time", ctypes.c_ulong),
        ("deviceid", ctypes.c_int),
        ("sourceid", ctypes.c_int),
        ("detail", ctypes.c_int),
        ("flags", ctypes.c_int),
    ]


class XIEventMask(ctypes.Structure):
    """``XIEventMask``: какие XI2-события слушать и на каких устройствах."""

    _fields_ = [
        ("deviceid", ctypes.c_int),
        ("mask_len", ctypes.c_int),
        ("mask", ctypes.POINTER(ctypes.c_ubyte)),
    ]


RAW_EVENT_KINDS = {
    XI_RawButtonPress: "press",
    XI_RawButtonRelease: "release",
    XI_RawMotion: "motion",
}

# Обработчик ошибок Xlib: ``int (*)(Display *, XErrorEvent *)``. Нужен, чтобы
# убедиться, что XISelectEvents принят сервером, а не отвалился асинхронно.
X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class XClient:
    """Соединение с X: XQueryPointer всегда, XI2 raw events — если поднялись."""

    def __init__(self, display=None):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        self.x = ctypes.CDLL(name)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        shown = display if display is not None else os.environ.get("DISPLAY") or ""
        self.dpy = self.x.XOpenDisplay(shown.encode() or None)
        if not self.dpy:
            raise RuntimeError(_("не удалось подключиться к X-серверу (DISPLAY)"))
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.root = self.x.XDefaultRootWindow(self.dpy)
        self.x.XConnectionNumber.restype = ctypes.c_int
        self.x.XConnectionNumber.argtypes = [ctypes.c_void_p]
        self.x.XPending.restype = ctypes.c_int
        self.x.XPending.argtypes = [ctypes.c_void_p]
        self.x.XNextEvent.restype = ctypes.c_int
        self.x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.x.XSync.restype = ctypes.c_int
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XCloseDisplay.restype = ctypes.c_int
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.x.XQueryExtension.restype = ctypes.c_int
        self.x.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x.XQueryPointer.restype = ctypes.c_int
        self.x.XGetEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(XGenericEventCookie)]
        self.x.XGetEventData.restype = ctypes.c_int
        self.x.XFreeEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(XGenericEventCookie)]
        self.x.XFreeEventData.restype = None
        self.x.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
        self.x.XSetErrorHandler.restype = X_ERROR_HANDLER
        self.xi = None
        self.xi_opcode = 0
        self.xi_version = None
        self.raw_events = False

    # ---- обычный указатель

    def pointer_state(self):
        """``(x, y, button1_down)`` в корневых координатах; ``None`` при сбое."""
        root_ret = ctypes.c_ulong()
        child_ret = ctypes.c_ulong()
        root_x, root_y = ctypes.c_int(), ctypes.c_int()
        win_x, win_y = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        ok = self.x.XQueryPointer(
            self.dpy,
            self.root,
            ctypes.byref(root_ret),
            ctypes.byref(child_ret),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(win_x),
            ctypes.byref(win_y),
            ctypes.byref(mask),
        )
        if not ok:
            return None
        return root_x.value, root_y.value, bool(mask.value & BUTTON1_MASK)

    def pointer_point(self):
        state = self.pointer_state()
        return None if state is None else (state[0], state[1])

    def fd(self):
        return self.x.XConnectionNumber(self.dpy)

    # ---- XI2 raw events

    def enable_raw_events(self):
        """Подписать root на XI_RawMotion|RawButtonPress|RawButtonRelease.

        Возвращает ``(major, minor)`` версии XInput, которую согласовал сервер,
        или ``None`` — тогда watcher идёт в fallback по XQueryPointer. Никаких
        grab'ов: raw-события приходят и при чужом grab, в этом весь смысл.
        """
        opcode = ctypes.c_int()
        event = ctypes.c_int()
        error = ctypes.c_int()
        if not self.x.XQueryExtension(
            self.dpy,
            b"XInputExtension",
            ctypes.byref(opcode),
            ctypes.byref(event),
            ctypes.byref(error),
        ):
            return None
        try:
            self.xi = ctypes.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        except OSError:
            return None
        self.xi.XIQueryVersion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.xi.XIQueryVersion.restype = ctypes.c_int
        self.xi.XISelectEvents.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(XIEventMask),
            ctypes.c_int,
        ]
        self.xi.XISelectEvents.restype = ctypes.c_int
        major, minor = ctypes.c_int(2), ctypes.c_int(1)
        if self.xi.XIQueryVersion(self.dpy, ctypes.byref(major), ctypes.byref(minor)) != 0:
            return None
        # raw-события появились в XI2.0, но версию просим 2.1 (стабильный
        # контракт raw events); сервер вправе ответить своей, обычно 2.4.
        if (major.value, minor.value) < (2, 1):
            return None
        mask = (ctypes.c_ubyte * 4)()
        for evtype in RAW_EVENT_KINDS:
            mask[evtype // 8] |= 1 << (evtype % 8)
        masks = XIEventMask()
        masks.deviceid = XI_ALL_MASTER_DEVICES
        masks.mask_len = len(mask)
        masks.mask = ctypes.cast(mask, ctypes.POINTER(ctypes.c_ubyte))
        # Ошибка X приходит асинхронно: ловим её обработчиком и синхронизируемся
        # до возврата, иначе «подписка не удалась» выглядела бы как рабочий XI2.
        errors = []

        def on_error(_dpy, _event):
            errors.append(True)
            return 0

        trap = X_ERROR_HANDLER(on_error)
        previous = self.x.XSetErrorHandler(trap)
        try:
            self.xi.XISelectEvents(self.dpy, self.root, ctypes.byref(masks), 1)
            self.x.XSync(self.dpy, 0)
        finally:
            self.x.XSetErrorHandler(previous)
        if errors:
            return None
        self.xi_opcode = opcode.value
        self.xi_version = (major.value, minor.value)
        self.raw_events = True
        return self.xi_version

    def read_events(self, query_motion=None):
        """События перетаскивания из очереди X; ``[('press'|'motion'|'release', (x, y), button)]``.

        Не наши события (MappingNotify, XI2 не от root и т.п.) пропускаются.
        Данные generic-события обязаны быть освобождены через XFreeEventData —
        иначе Xlib копит их в памяти.

        ``query_motion`` — необязательный предикат: спрашивать ли позицию для
        очередного ``RawMotion``. Без него спрашиваем всегда. Нажатие и
        отпускание опрашиваются всегда: их точка — это и есть результат.
        """
        events = []
        while self.x.XPending(self.dpy):
            buf = EVENT_BUFFER()
            self.x.XNextEvent(self.dpy, ctypes.byref(buf))
            cookie = ctypes.cast(ctypes.byref(buf), ctypes.POINTER(XGenericEventCookie))
            if cookie.contents.type != GENERIC_EVENT:
                continue
            if not self.x.XGetEventData(self.dpy, cookie):
                continue
            try:
                if cookie.contents.extension != self.xi_opcode:
                    continue
                raw = ctypes.cast(cookie.contents.data, ctypes.POINTER(XIRawEvent)).contents
                kind = RAW_EVENT_KINDS.get(raw.evtype)
                if kind is None:
                    continue
                if kind == "motion" and query_motion is not None and not query_motion():
                    continue
                point = self.pointer_point()
                events.append((kind, point, raw.detail))
            finally:
                self.x.XFreeEventData(self.dpy, cookie)
        return events

    def poll_events(self, state):
        """Fallback: одно сравнение срезов XQueryPointer (``state`` — прошлый)."""
        now = self.pointer_state()
        events = [(kind, point, BUTTON1) for kind, point in poll_events(state, now)]
        return events, (state if now is None else now)

    def close(self):
        if getattr(self, "dpy", None):
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None


class StopFlag:
    """Флаг остановки + self-pipe: сигнал будит ``select``, а не ждёт таймаута.

    Обработчик сигнала только ставит флаг и пишет байт в pipe: делать в нём
    что-то ещё (останавливать HUD, звать gsettings) нельзя. ``select`` в
    главном цикле слушает читающий конец, поэтому завершение по SIGTERM
    мгновенное и детерминированное.
    """

    def __init__(self):
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.reader, False)
        os.set_blocking(self.writer, False)
        self.stopped = False

    def handler(self, _signum=None, _frame=None):
        self.stopped = True
        try:
            os.write(self.writer, b"x")
        except OSError:
            pass

    def drain(self):
        try:
            os.read(self.reader, 4096)
        except (BlockingIOError, OSError):
            pass

    def close(self):
        for fd in (self.reader, self.writer):
            try:
                os.close(fd)
            except OSError:
                pass


def install_signal_handlers(stop):
    """SIGTERM/SIGINT/SIGHUP — в ``StopFlag`` (аккуратное завершение)."""
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(signum, stop.handler)
        except (ValueError, OSError):  # не главный поток / сигнал недоступен
            pass


def serve(watcher, client, stop):
    """Цикл watcher'а: ``select`` по fd X и self-pipe, события -> ``Hotcorners``.

    В XI2-режиме ждём события X; в fallback — таймаут ``POLL_INTERVAL`` и один
    опрос ``XQueryPointer`` за итерацию. Выход — по флагу ``stop`` (сигнал).
    """
    watch = [stop.reader]
    if client.raw_events:
        watch.append(client.fd())
    timeout = None if client.raw_events else POLL_INTERVAL
    state = None
    while not stop.stopped:
        try:
            ready, _, _ = select.select(watch, [], [], timeout)
        except OSError:  # EINTR на некоторых платформах
            continue
        if stop.stopped or stop.reader in ready:
            stop.drain()
            break
        if client.raw_events:
            if client.fd() not in ready:
                continue
            events = client.read_events(getattr(watcher, "motion_due", None))
        else:
            events, state = client.poll_events(state)
        for kind, point, button in events:
            if stop.stopped:
                break
            if kind == "press":
                watcher.on_press(point, button)
            elif kind == "motion":
                watcher.on_motion(point)
            else:
                watcher.on_release(point, button)


# ------------------------------------------------------------------------- CLI


def default_conf():
    return tiler.default_conf()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="zone-hotcorners.py",
        description=_(
            "Перетаскивание окна за титлбар вкладывает его в зону GridSnap (Cinnamon/X11)"
        ),
        epilog=_(
            "Watcher слушает XInput2 raw events на root (grab'ов не делает), водит HUD "
            "zone-picker --hud и вкладывает активное окно в зону под точкой отпускания. "
            "Если XI2 недоступен — опрос XQueryPointer ~30 Гц."
        ),
    )
    ap.add_argument(
        "--conf",
        metavar="PATH",
        default=None,
        help=_("путь к zones.conf (по умолчанию XDG/рядом с кодом)"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=_("не двигать окно и не менять настройки — только печатать, что сделал бы"),
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help=_("печатать в stderr, что происходит (arm, HUD, edge-tiling, дроп)"),
    )
    ap.add_argument(
        "--no-hud",
        action="store_true",
        help=_("без оверлея: только дроп по отпусканию (edge-tiling не трогается)"),
    )
    ap.add_argument("--version", action="version", version=f"zone-hotcorners {VERSION}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    conf = args.conf or default_conf()
    if not os.path.exists(conf):
        print(_("zone-hotcorners: не найден конфиг {}").format(conf), file=sys.stderr)
        return EXIT_CONFIG

    stop = StopFlag()
    install_signal_handlers(stop)
    try:
        client = XClient()
    except (OSError, RuntimeError) as exc:
        print(f"zone-hotcorners: {exc}", file=sys.stderr)
        stop.close()
        return EXIT_FAIL

    watcher = None
    try:
        version = client.enable_raw_events()
        if version:
            mode = f"XI2 raw events {version[0]}.{version[1]}"
        else:
            mode = _("fallback: опрос XQueryPointer {:.0f} Гц").format(1.0 / POLL_INTERVAL)
        print(
            _("zone-hotcorners: {}; конфиг {}").format(mode, conf),
            file=sys.stderr,
            flush=True,
        )
        watcher = Hotcorners(
            conf,
            dry_run=args.dry_run,
            no_hud=args.no_hud,
            verbose=args.verbose,
        )
        serve(watcher, client, stop)
        return EXIT_OK
    finally:
        if watcher is not None:
            watcher.finish_drag()  # HUD погашен, edge-tiling возвращён
        client.close()
        stop.close()


if __name__ == "__main__":
    sys.exit(main())

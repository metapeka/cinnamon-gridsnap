#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты watcher'а перетаскивания `zone-hotcorners.py` — без X и без gsettings.

Проверяется ровно то, что требует контракт задачи:

* «arm»: нажатие в полосе титлбара активного окна (рамка + ``frame_extents.top``
  + запас) и промахи — выше/ниже полосы, левее окна, окно без рамки;
* троттлинг точек в HUD — не чаще ~30 раз в секунду;
* отпускание мимо зон (в том числе в «щель» между зонами) — ничего не делает;
  отпускание в зоне — вкладывает окно в ту же зону, что подсветил бы HUD, и с
  правильной пиксельной рамкой; край сетки (последний пиксель и точка ровно на
  правой/нижней границе) относится к крайней зоне, а не теряется;
* edge-tiling: исходное значение запоминается, гасится на время HUD и
  возвращается на дропе; уже offную настройку не трогаем; если исходное
  значение не прочиталось, возвращаем «включено» (true), а не оставляем
  offной; восстановление — данными (argv gsettings), без запуска утилиты;
* fallback: `poll_events` по двум срезам XQueryPointer даёт press/motion/release
  и не повторяет события;
* разбор аргументов: `--conf`, `--dry-run`, `--verbose`, `--no-hud`, `--version`.

Сценарий перетаскивания целиком (press -> motion -> release) гоняется на
заглушках HUD, gsettings и укладчика: настоящий drag в CI не воспроизвести
(XTest-инъекции запрещены решением, а XWarpPointer raw-событий не порождает).
X нужен только тесту `XClient`, и он пропускается без DISPLAY.

Запуск:
    python3 tests/test_hotcorners.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest


try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WATCHER = os.path.join(ROOT, "zone-hotcorners.py")


def load_watcher():
    spec = importlib.util.spec_from_file_location("zone_hotcorners_under_test", WATCHER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = load_watcher()

# Окно: внешняя рамка (x=100, y=100, 400x300), титлбар 30 px — как у muffin.
WIN = 0x400001
FRAME = (100, 100, 400, 300)
EXTENTS = (0, 0, 30, 0)
INSIDE_TITLE = (300, 115)  # полоса 100..138 (30 + запас 8)
BELOW_TITLE = (300, 200)  # в окне, но ниже полосы
LEFT_OF_WINDOW = (50, 115)

# Рабочая область одного монитора: 1000x800 в корневых координатах.
ONE_MONITOR = [("eDP-1", (0, 0, 1000, 800))]
# Два монитора рядом: второй — справа.
TWO_MONITORS = [("eDP-1", (0, 0, 1000, 800)), ("HDMI-1", (1000, 0, 800, 600))]

# Сетка 2x2 в общем [zones]: имена и порядок — как в файле.
GRID_CONF = """[grid]
cols = 0.5, 1.0
rows = 0.5, 1.0

[zones]
tl = 0:1, 0:1
tr = 1:2, 0:1
bl = 0:1, 1:2
br = 1:2, 1:2
"""
# Зоны с «щелью» посередине: 0.4..0.6 не занято ни одной.
GAP_CONF = """[grid]
cols = 1.0
rows = 1.0

[zones]
left = 0, 0, 0.4, 1
right = 0.6, 0, 0.4, 1
"""


class TempConf:
    """zones.conf во временном каталоге; удаляется на выходе."""

    def __init__(self, text):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-hotcorners-")
        self.path = os.path.join(self.dir, "zones.conf")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class FakeHud:
    """Заглушка HudProcess: помнит запуски, точки и остановки."""

    def __init__(self):
        self.starts = []
        self.points = []
        self.stops = 0
        self.index = None
        self.name = ""
        self.alive = False

    def start(self, index, name=""):
        if self.alive and self.index == index:
            return False
        self.starts.append((index, name))
        self.index = index
        self.name = name
        self.alive = True
        return True

    def feed(self, point):
        if not self.alive:
            return False
        self.points.append(point)
        return True

    def running(self):
        return self.alive

    def stop(self):
        was = self.alive
        self.alive = False
        self.index = None
        self.name = ""
        if was:
            self.stops += 1
        return was


def recording_runner(log, get_value="true", set_code=0):
    """Заглушка `run_gsettings`: пишет argv и отдаёт заданное значение."""

    def runner(argv):
        log.append(list(argv))
        if argv[:2] == ["gsettings", "get"]:
            return 0, get_value
        return set_code, ""

    return runner


class Clock:
    """Управляемое время: `clock()` и `advance()` вместо настоящего сна."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def make_watcher(conf_text=GRID_CONF, *, hud=None, edge=None, clock=None, dry_run=False,
                 no_hud=False, outputs=None, applied=None, is_excluded=None):
    """Hotcorners с заглушками X/HUD/gsettings; возвращает (watcher, контекст).

    Контекст — TempConf; вызывающий держит его открытым, пока watcher жив
    (конфиг читается лениво, на первом перетаскивании).
    """
    ctx = TempConf(conf_text)
    log = [] if edge is None else None
    if edge is None:
        edge = hc.EdgeTiling(original="true", runner=recording_runner(log or []))
    if applied is None:
        applied = []
    watcher = hc.Hotcorners(
        ctx.path,
        dry_run=dry_run,
        no_hud=no_hud,
        verbose=False,
        active_window=lambda: WIN,
        frame_extents=lambda win: EXTENTS,
        read_frame=lambda win, ext: FRAME,
        outputs=outputs or (lambda: ONE_MONITOR),
        apply=lambda win, rect, label: applied.append((win, rect, label)),
        is_excluded=is_excluded or (lambda win, ignore: False),
        hud=FakeHud() if hud is None else hud,
        edge=edge,
        clock=clock or Clock(),
        sleep=lambda seconds: None,
        out=lambda text: applied.append(("вывод", text)),
        err=lambda text: None,
    )
    return watcher, ctx, applied


def full_drag(conf_text=GRID_CONF, *, points=(INSIDE_TITLE,), release=None, **kwargs):
    """press -> движения -> release; возвращает watcher, applied, hud, edge-log."""
    clock = kwargs.pop("clock", None) or Clock()
    hud = kwargs.pop("hud", None) or FakeHud()
    log = []
    edge = kwargs.pop("edge", None) or hc.EdgeTiling(original="true", runner=recording_runner(log))
    watcher, ctx, applied = make_watcher(
        conf_text, hud=hud, edge=edge, clock=clock, **kwargs
    )
    ctx.__enter__()
    try:
        watcher.on_press(INSIDE_TITLE, 1)
        for point in points[1:] if len(points) > 1 else ():
            clock.advance(hc.POINT_INTERVAL)
            watcher.on_motion(point)
        target = points[-1] if release is None else release
        watcher.on_release(target, 1)
    finally:
        watcher.finish_drag()
        ctx.__exit__(None, None, None)
    return watcher, applied, hud, log, edge


class TestTitleBar(unittest.TestCase):
    """«Arm»: попадание нажатия в полосу титлбара и промахи."""

    def test_band_adds_slack_to_frame_top(self):
        self.assertEqual(hc.title_band(30), 30 + hc.TITLE_SLACK)
        self.assertEqual(hc.title_band(0), hc.TITLE_FALLBACK_TOP + hc.TITLE_SLACK)
        self.assertEqual(hc.title_band(None), hc.TITLE_FALLBACK_TOP + hc.TITLE_SLACK)
        self.assertEqual(hc.title_band(-5, slack=0), hc.TITLE_FALLBACK_TOP)

    def test_press_in_title_bar(self):
        self.assertTrue(hc.is_title_bar_press(INSIDE_TITLE, FRAME, EXTENTS[2]))

    def test_press_at_top_edge_and_at_last_pixel_of_band(self):
        self.assertTrue(hc.is_title_bar_press((100, 100), FRAME, 30))
        band = hc.title_band(30)
        self.assertTrue(hc.is_title_bar_press((100, 100 + band - 1), FRAME, 30))
        self.assertFalse(hc.is_title_bar_press((100, 100 + band), FRAME, 30))

    def test_press_below_title_bar(self):
        self.assertFalse(hc.is_title_bar_press(BELOW_TITLE, FRAME, 30))

    def test_press_outside_horizontal_bounds(self):
        self.assertFalse(hc.is_title_bar_press(LEFT_OF_WINDOW, FRAME, 30))
        self.assertFalse(hc.is_title_bar_press((500, 115), FRAME, 30))
        self.assertTrue(hc.is_title_bar_press((499, 115), FRAME, 30))

    def test_press_above_window(self):
        self.assertFalse(hc.is_title_bar_press((300, 99), FRAME, 30))

    def test_no_frame_or_empty_frame_means_no_arm(self):
        self.assertFalse(hc.is_title_bar_press(INSIDE_TITLE, None, 30))
        self.assertFalse(hc.is_title_bar_press(INSIDE_TITLE, (100, 100, 0, 0), 30))

    def test_arm_at_uses_active_window_frame(self):
        watcher, ctx, _ = make_watcher()
        ctx.__enter__()
        try:
            self.assertEqual(watcher.arm_at(INSIDE_TITLE), (WIN, 30))
            self.assertIsNone(watcher.arm_at(BELOW_TITLE))
        finally:
            ctx.__exit__(None, None, None)

    def test_arm_at_retries_when_window_not_active_yet(self):
        """Активация приходит позже нажатия: промах повторяется один раз."""
        sequence = [None, WIN]
        sleeps = []
        watcher, ctx, _ = make_watcher()
        watcher.active_window = lambda: sequence.pop(0) if sequence else WIN
        watcher.sleep = sleeps.append
        ctx.__enter__()
        try:
            self.assertEqual(watcher.arm_at(INSIDE_TITLE), (WIN, 30))
            self.assertEqual(sleeps, [hc.ARM_RETRY_DELAY])
        finally:
            ctx.__exit__(None, None, None)


class TestThrottle(unittest.TestCase):
    """Точки в HUD — не чаще POINT_INTERVAL (30 в секунду)."""

    def test_first_point_always_emitted(self):
        self.assertTrue(hc.should_emit(None, 0.0))

    def test_too_soon_is_skipped(self):
        self.assertFalse(hc.should_emit(1000.0, 1000.0 + hc.POINT_INTERVAL / 2))
        self.assertTrue(hc.should_emit(1000.0, 1000.0 + hc.POINT_INTERVAL))
        self.assertTrue(hc.should_emit(1000.0, 1000.0 + hc.POINT_INTERVAL * 2))

    def test_at_most_thirty_per_second(self):
        """1000 предложений подряд за секунду -> не больше 31 отданной точки."""
        emitted = 0
        last = None
        step = 0.001
        for i in range(1000):
            now = i * step
            if hc.should_emit(last, now):
                emitted += 1
                last = now
        self.assertLessEqual(emitted, 31)
        self.assertGreaterEqual(emitted, 29)

    def test_motion_due_only_when_armed_with_hud(self):
        """Позицию по RawMotion спрашиваем только при живом HUD и не чаще 30 Гц."""
        clock = Clock()
        watcher, ctx, _ = make_watcher(clock=clock)
        ctx.__enter__()
        try:
            self.assertFalse(watcher.motion_due())  # до нажатия
            watcher.on_press(INSIDE_TITLE, 1)
            self.assertTrue(watcher.motion_due())
            watcher.on_motion((150, 150))
            self.assertFalse(watcher.motion_due())  # только что отдали точку
            clock.advance(hc.POINT_INTERVAL)
            self.assertTrue(watcher.motion_due())
        finally:
            ctx.__exit__(None, None, None)

    def test_motion_due_false_without_hud(self):
        """--no-hud: движение в HUD не нужно вовсе, XQueryPointer не дёргаем."""
        watcher, ctx, _ = make_watcher(no_hud=True)
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            self.assertFalse(watcher.motion_due())
        finally:
            ctx.__exit__(None, None, None)

    def test_motion_while_pressed_feeds_hud_with_throttle(self):
        clock = Clock()
        hud = FakeHud()
        watcher, ctx, _ = make_watcher(hud=hud, clock=clock)
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            self.assertTrue(watcher.on_motion((150, 150)))
            self.assertFalse(watcher.on_motion((160, 160)))  # слишком часто
            clock.advance(hc.POINT_INTERVAL)
            self.assertTrue(watcher.on_motion((170, 170)))
            self.assertEqual(hud.points, [(150, 150), (170, 170)])
        finally:
            ctx.__exit__(None, None, None)

    def test_motion_without_arm_does_nothing(self):
        """Нажатие не в титлбаре — обычный клик: HUD не запускается вовсе."""
        clock = Clock()
        hud = FakeHud()
        watcher, ctx, _ = make_watcher(hud=hud, clock=clock)
        ctx.__enter__()
        try:
            self.assertFalse(watcher.on_press(BELOW_TITLE, 1))
            clock.advance(hc.POINT_INTERVAL)
            self.assertFalse(watcher.on_motion((150, 150)))
            self.assertEqual(hud.starts, [])
            self.assertEqual(hud.points, [])
        finally:
            ctx.__exit__(None, None, None)

    def test_other_buttons_are_ignored(self):
        hud = FakeHud()
        watcher, ctx, applied = make_watcher(hud=hud)
        ctx.__enter__()
        try:
            self.assertFalse(watcher.on_press(INSIDE_TITLE, 3))
            self.assertFalse(watcher.pressed)
            self.assertEqual(hud.starts, [])
        finally:
            ctx.__exit__(None, None, None)


class TestIgnore(unittest.TestCase):
    """Окно под `[ignore]` watcher не отслеживает: ни HUD, ни дропа."""

    def test_excluded_window_is_not_armed(self):
        watcher, ctx, applied = make_watcher(
            GRID_CONF + "[ignore]\nXmessage* = \n",
            is_excluded=lambda win, ignore: True,
        )
        with ctx:
            self.assertFalse(watcher.on_press(INSIDE_TITLE, 1))
            self.assertFalse(watcher.armed)
            self.assertIsNone(watcher.drag_win)
            watcher.on_release((1500, 300), 1)
        self.assertEqual(applied, [])

    def test_ignore_list_from_config_reaches_the_check(self):
        seen = []

        def is_excluded(win, ignore):
            seen.append(list(ignore))
            return True

        watcher, ctx, _applied = make_watcher(
            GRID_CONF + "[ignore]\nXmessage* = \n",
            is_excluded=is_excluded,
        )
        with ctx:
            self.assertFalse(watcher.on_press(INSIDE_TITLE, 1))
        self.assertEqual(seen, [["Xmessage*"]])


class TestZones(unittest.TestCase):
    """«Точка -> монитор -> зона» и пиксельная рамка дропа."""

    def test_monitor_index_point_inside_and_between(self):
        self.assertEqual(hc.monitor_index_for_point(TWO_MONITORS, (1500, 300)), 1)
        self.assertEqual(hc.monitor_index_for_point(TWO_MONITORS, (10, 10)), 0)
        # Щель правее первого монитора и выше второго: ближайший по центру — 0.
        self.assertEqual(hc.monitor_index_for_point(TWO_MONITORS, (1200, 700)), 1)
        self.assertIsNone(hc.monitor_index_for_point([], (10, 10)))

    def test_zone_rects_follows_config_order(self):
        with TempConf(GRID_CONF) as conf:
            grid = hc.tiler.Grid(conf)
            rects = hc.zone_rects(grid, "eDP-1")
        self.assertEqual([name for name, _ in rects], ["tl", "tr", "bl", "br"])
        self.assertEqual(rects[0][1], (0.0, 0.0, 0.5, 0.5))

    def test_zone_under_corners(self):
        with TempConf(GRID_CONF) as conf:
            grid = hc.tiler.Grid(conf)
            rects = hc.zone_rects(grid, "eDP-1")
        area = ONE_MONITOR[0][1]
        self.assertEqual(hc.zone_under(rects, area, (10, 10))[0], "tl")
        self.assertEqual(hc.zone_under(rects, area, (990, 10))[0], "tr")
        self.assertEqual(hc.zone_under(rects, area, (10, 790))[0], "bl")
        self.assertEqual(hc.zone_under(rects, area, (990, 790))[0], "br")

    def test_zone_under_grid_edge_belongs_to_edge_zone(self):
        """Край сетки: последний пиксель и точка ровно на границе — крайняя зона."""
        with TempConf(GRID_CONF) as conf:
            grid = hc.tiler.Grid(conf)
            rects = hc.zone_rects(grid, "eDP-1")
        area = ONE_MONITOR[0][1]
        self.assertEqual(hc.zone_under(rects, area, (999, 799))[0], "br")
        self.assertEqual(hc.zone_under(rects, area, (1000, 800))[0], "br")

    def test_zone_under_gap_and_outside(self):
        with TempConf(GAP_CONF) as conf:
            grid = hc.tiler.Grid(conf)
            rects = hc.zone_rects(grid, "eDP-1")
        area = ONE_MONITOR[0][1]
        self.assertIsNone(hc.zone_under(rects, area, (500, 400)))
        self.assertIsNone(hc.zone_under(rects, area, (-1, 400)))

    def test_drop_in_zone_applies_that_zone(self):
        _, applied, hud, log, edge = full_drag(
            points=(INSIDE_TITLE, (150, 600)), release=(150, 600)
        )
        self.assertEqual(applied, [(WIN, (0, 400, 500, 400), "bl")])
        # HUD водили той же точкой и остановили; edge-tiling погашен и возвращён.
        self.assertEqual(hud.points, [(150, 600)])
        self.assertEqual(hud.stops, 1)
        self.assertEqual(
            log,
            [
                ["gsettings", "set", "org.cinnamon.muffin", "edge-tiling", "false"],
                ["gsettings", "set", "org.cinnamon.muffin", "edge-tiling", "true"],
            ],
        )
        self.assertFalse(edge.touched)

    def test_drop_on_grid_edge_applies_edge_zone(self):
        _, applied, _, _, _ = full_drag(points=(INSIDE_TITLE, (999, 799)), release=(999, 799))
        self.assertEqual(applied, [(WIN, (500, 400, 500, 400), "br")])

    def test_drop_in_gap_does_nothing(self):
        watcher, applied, hud, log, edge = full_drag(
            GAP_CONF, points=(INSIDE_TITLE, (500, 300)), release=(500, 300)
        )
        self.assertEqual(applied, [])
        self.assertEqual(hud.stops, 1)
        # Ghost-дроп: настройка всё равно вернулась.
        self.assertIn(["gsettings", "set", "org.cinnamon.muffin", "edge-tiling", "true"], log)
        self.assertFalse(edge.touched)

    def test_drop_on_second_monitor_uses_its_area_and_hud(self):
        hud = FakeHud()
        watcher, ctx, applied = make_watcher(hud=hud, outputs=lambda: TWO_MONITORS, clock=Clock())
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_motion((1500, 300))
            self.assertEqual(hud.starts[0][0], 1)
            watcher.on_release((1500, 300), 1)
            # Рабочая область HDMI-1: 800x600, точка (500, 300) в неё — правый
            # нижний квадрант, зона br.
            self.assertEqual(applied, [(WIN, (1400, 300, 400, 300), "br")])
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)

    def test_no_active_window_does_not_kill_watcher(self):
        """tiler.active_window поднимает SystemExit, если окна нет: press — мимо."""
        watcher, ctx, _ = make_watcher()
        watcher.active_window = lambda: (_ for _ in ()).throw(SystemExit(1))
        watcher.sleep = lambda seconds: None
        ctx.__enter__()
        try:
            self.assertFalse(watcher.on_press(INSIDE_TITLE, 1))
            self.assertFalse(watcher.armed)
        finally:
            ctx.__exit__(None, None, None)

    def test_apply_failure_does_not_kill_watcher(self):
        """Сбой apply_zone (нет X/EWMH) логируется, а watcher живёт дальше."""
        errors = []
        applied = []

        def boom(win, rect, label):
            applied.append(rect)
            raise SystemExit(1)

        watcher, ctx, _ = make_watcher(applied=applied)
        watcher.apply = boom
        watcher.err = errors.append
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            self.assertFalse(watcher.on_release((150, 600), 1))
            self.assertEqual(len(applied), 1)
            self.assertTrue(any("not placed" in text for text in errors))
            # HUD погашен и состояние сброшено: следующее перетаскивание работает.
            self.assertFalse(watcher.pressed or watcher.armed)
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)

    def test_hud_restarts_when_point_moves_to_other_monitor(self):
        clock = Clock()
        hud = FakeHud()
        watcher, ctx, _ = make_watcher(hud=hud, outputs=lambda: TWO_MONITORS, clock=clock)
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_motion((150, 150))
            clock.advance(hc.POINT_INTERVAL)
            watcher.on_motion((1500, 150))
            self.assertEqual([index for index, _ in hud.starts], [0, 1])
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)

    def test_dry_run_prints_plan_and_moves_nothing(self):
        watcher, ctx, applied = make_watcher(dry_run=True, clock=Clock())
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_release((150, 600), 1)
            # Применения нет: только строка плана.
            self.assertEqual(len(applied), 1)
            kind, text = applied[0]
            self.assertEqual(kind, "вывод")
            self.assertIn("zone 'bl'", text)
            self.assertIn("dry-run", text)
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)

    def test_no_hud_does_not_touch_gsettings(self):
        log = []
        edge = hc.EdgeTiling(original="true", runner=recording_runner(log))
        watcher, ctx, applied = make_watcher(no_hud=True, edge=edge, clock=Clock())
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_motion((150, 600))
            watcher.on_release((150, 600), 1)
            self.assertEqual(applied, [(WIN, (0, 400, 500, 400), "bl")])
            self.assertEqual(log, [])  # edge-tiling не трогали вовсе
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)

    def test_dry_run_does_not_touch_gsettings(self):
        log = []
        edge = hc.EdgeTiling(original="true", runner=recording_runner(log))
        watcher, ctx, _ = make_watcher(dry_run=True, edge=edge, clock=Clock())
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_motion((150, 600))
            watcher.on_release((150, 600), 1)
            self.assertEqual(log, [])
        finally:
            watcher.finish_drag()
            ctx.__exit__(None, None, None)


class TestEdgeTiling(unittest.TestCase):
    """edge-tiling как данные: запомнить, погасить, вернуть."""

    def test_restore_value_keeps_original(self):
        self.assertEqual(hc.EdgeTiling("true").restore_value, "true")
        self.assertEqual(hc.EdgeTiling("false").restore_value, "false")
        self.assertEqual(hc.EdgeTiling("TRUE\n").restore_value, "true")

    def test_unreadable_original_restores_true(self):
        self.assertEqual(hc.EdgeTiling(None).restore_value, "true")
        self.assertEqual(hc.EdgeTiling("garbage").restore_value, "true")

    def test_disable_then_restore_commands(self):
        log = []
        edge = hc.EdgeTiling("true", runner=recording_runner(log))
        self.assertTrue(edge.disable())
        self.assertTrue(edge.touched)
        self.assertTrue(edge.restore())
        self.assertFalse(edge.touched)
        self.assertEqual(
            log,
            [
                ["gsettings", "set", "org.cinnamon.muffin", "edge-tiling", "false"],
                ["gsettings", "set", "org.cinnamon.muffin", "edge-tiling", "true"],
            ],
        )
        # Повторный restore ничего не запускает.
        self.assertFalse(edge.restore())
        self.assertEqual(len(log), 2)

    def test_already_disabled_is_untouched(self):
        log = []
        edge = hc.EdgeTiling("false", runner=recording_runner(log))
        self.assertFalse(edge.disable())
        self.assertFalse(edge.touched)
        self.assertFalse(edge.restore())
        self.assertEqual(log, [])

    def test_read_original_from_gsettings(self):
        log = []
        edge = hc.EdgeTiling(None, runner=recording_runner(log, get_value="true"))
        self.assertEqual(edge.read(), "true")
        self.assertEqual(edge.original, "true")
        self.assertEqual(log, [["gsettings", "get", "org.cinnamon.muffin", "edge-tiling"]])

    def test_read_failure_stays_none(self):
        edge = hc.EdgeTiling(None, runner=lambda argv: (1, ""))
        self.assertIsNone(edge.read())
        self.assertEqual(edge.restore_value, "true")

    def test_gsettings_set_failure_does_not_mark_touched(self):
        edge = hc.EdgeTiling("true", runner=recording_runner([], set_code=1))
        self.assertFalse(edge.disable())
        self.assertFalse(edge.touched)

    def test_hud_start_reads_and_greens_setting(self):
        """HUD ещё не запускался — исходное значение читается один раз."""
        log = []
        edge = hc.EdgeTiling(None, runner=recording_runner(log, get_value="true"))
        hud = FakeHud()
        clock = Clock()
        watcher, ctx, _ = make_watcher(hud=hud, edge=edge, clock=clock)
        ctx.__enter__()
        try:
            watcher.on_press(INSIDE_TITLE, 1)
            watcher.on_motion((150, 150))
            self.assertEqual(log[0], ["gsettings", "get", "org.cinnamon.muffin", "edge-tiling"])
            self.assertEqual(log[1][-1], "false")
            watcher.finish_drag()
            self.assertEqual(log[2][-1], "true")
        finally:
            ctx.__exit__(None, None, None)


class TestPollFallback(unittest.TestCase):
    """Fallback без XI2: события — по двум срезам XQueryPointer."""

    def test_no_change_no_events(self):
        state = (10, 20, False)
        self.assertEqual(hc.poll_events(state, (10, 20, False)), [])

    def test_press_motion_release(self):
        self.assertEqual(hc.poll_events(None, (10, 20, False)), [])
        self.assertEqual(hc.poll_events((10, 20, False), (10, 20, True)), [("press", (10, 20))])
        self.assertEqual(hc.poll_events((10, 20, True), (30, 40, True)), [("motion", (30, 40))])
        self.assertEqual(
            hc.poll_events((30, 40, True), (50, 60, True)), [("motion", (50, 60))]
        )
        self.assertEqual(hc.poll_events((50, 60, True), (50, 60, False)), [("release", (50, 60))])

    def test_started_mid_drag_gives_press(self):
        self.assertEqual(hc.poll_events(None, (5, 6, True)), [("press", (5, 6))])

    def test_failed_query_keeps_previous_state(self):
        state = (1, 2, False)
        events, state2 = hc.XClient.poll_events(_PollStub(None), state)
        self.assertEqual(events, [])
        self.assertEqual(state2, state)

    def test_press_and_motion_in_one_tick(self):
        """Нажатие между опросами — одно событие press: точку задаёт текущий срез."""
        self.assertEqual(hc.poll_events((10, 10, False), (40, 40, True)), [("press", (40, 40))])


class _PollStub:
    """Подмена XClient для чистого теста `poll_events`-обёртки."""

    def __init__(self, state):
        self.state = state

    def pointer_state(self):
        return self.state


class TestArguments(unittest.TestCase):
    """Разбор аргументов: флаги по умолчанию, --conf, --version, мусор."""

    def test_defaults(self):
        args = hc.parse_args([])
        self.assertIsNone(args.conf)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.verbose)
        self.assertFalse(args.no_hud)

    def test_flags(self):
        args = hc.parse_args(["--conf", "/tmp/x.conf", "--dry-run", "--verbose", "--no-hud"])
        self.assertEqual(args.conf, "/tmp/x.conf")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.verbose)
        self.assertTrue(args.no_hud)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                hc.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"zone-hotcorners {hc.VERSION}")

    def test_unknown_argument_is_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                hc.parse_args(["--nope"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_is_russian(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                hc.parse_args(["--help"])
        text = out.getvalue()
        self.assertIn("Dragging a window by its title bar", text)
        self.assertIn("--no-hud", text)

    def test_missing_conf_returns_config_code(self):
        with TempConf(GRID_CONF) as conf:
            missing = os.path.join(os.path.dirname(conf), "нет.conf")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = hc.main(["--conf", missing])
        self.assertEqual(code, hc.EXIT_CONFIG)
        self.assertIn("config not found", err.getvalue())


class TestHudCommand(unittest.TestCase):
    """Команда HUD — тот же контракт `zone-picker --hud --follow`, что у пикера."""

    def test_command_has_monitor_and_follow(self):
        with TempConf(GRID_CONF) as conf:
            hud = hc.HudProcess(conf, python="/usr/bin/python3")
            cmd = hud.command(2)
        self.assertEqual(cmd[1], hc.PICKER)
        self.assertIn("--hud", cmd)
        self.assertIn("--follow", cmd)
        self.assertEqual(cmd[cmd.index("--conf") + 1], conf)
        self.assertEqual(cmd[cmd.index("--monitor") + 1], "2")

    def test_start_is_idempotent_per_monitor(self):
        """Заглушка повторяет контракт: тот же монитор — не перезапуск."""
        hud = FakeHud()
        self.assertTrue(hud.start(0, "eDP-1"))
        self.assertFalse(hud.start(0, "eDP-1"))
        self.assertTrue(hud.start(1, "HDMI-1"))
        self.assertEqual(len(hud.starts), 2)


@unittest.skipUnless(os.environ.get("DISPLAY"), "нет DISPLAY: XClient не поднять")
class TestXClient(unittest.TestCase):
    """Живое соединение с X: XI2 raw events поднимаются или дают fallback."""

    def test_xi2_or_fallback_and_pointer(self):
        client = hc.XClient()
        try:
            version = client.enable_raw_events()
            if version is not None:
                self.assertGreaterEqual(version, (2, 1))
                self.assertTrue(client.raw_events)
            state = client.pointer_state()
            self.assertIsNotNone(state)
            events = client.read_events()
            self.assertIsInstance(events, list)
            for kind, point, button in events:
                self.assertIn(kind, ("press", "motion", "release"))
                self.assertIn(button, (0, 1))
        finally:
            client.close()


class RecordingWatcher:
    """Что получил цикл `serve`: порядок и аргументы вызовов."""

    def __init__(self):
        self.calls = []

    def on_press(self, point, button=1):
        self.calls.append(("press", point, button))

    def on_motion(self, point):
        self.calls.append(("motion", point))

    def on_release(self, point, button=1):
        self.calls.append(("release", point, button))

    def motion_due(self):
        return True


class RawFakeClient:
    """XI2-режим без X: fd из pipe, пачка событий, потом остановка."""

    raw_events = True

    def __init__(self, stop, events):
        self.stop = stop
        self.events = list(events)
        self.read_fd, self.write_fd = os.pipe()
        os.write(self.write_fd, b"x")  # select сразу готов
        self.queries = []

    def fd(self):
        return self.read_fd

    def read_events(self, query_motion=None):
        self.queries.append(query_motion)
        if self.events:
            events, self.events = self.events, []
            return events
        self.stop.handler()  # очередь пуста — цикл должен выйти
        return []


class PollFakeClient:
    """Fallback без X: одна пачка опроса, потом остановка."""

    raw_events = False

    def __init__(self, stop, batch):
        self.stop = stop
        self.batch = list(batch)
        self.states = []

    def poll_events(self, state):
        self.states.append(state)
        if self.batch:
            events, self.batch = self.batch, []
            return events, ("now",)
        self.stop.handler()
        return [], "now"


class TestServe(unittest.TestCase):
    """Главный цикл: select по fd, разбор событий, выход по флагу."""

    def test_raw_events_are_dispatched_in_order(self):
        stop = hc.StopFlag()
        try:
            client = RawFakeClient(
                stop,
                [("press", INSIDE_TITLE, 1), ("motion", (200, 200), 0), ("release", (150, 600), 1)],
            )
            watcher = RecordingWatcher()
            hc.serve(watcher, client, stop)
        finally:
            stop.close()
            os.close(client.read_fd)
            os.close(client.write_fd)
        self.assertEqual(
            watcher.calls,
            [("press", INSIDE_TITLE, 1), ("motion", (200, 200)), ("release", (150, 600), 1)],
        )
        # Предикат «спрашивать ли позицию по motion» передан клиенту.
        self.assertTrue(client.queries)
        self.assertTrue(all(q == watcher.motion_due for q in client.queries))

    def test_fallback_batches_are_dispatched(self):
        stop = hc.StopFlag()
        try:
            client = PollFakeClient(stop, [("press", (10, 10), 1), ("release", (20, 20), 1)])
            watcher = RecordingWatcher()
            hc.serve(watcher, client, stop)
        finally:
            stop.close()
        self.assertEqual(
            watcher.calls, [("press", (10, 10), 1), ("release", (20, 20), 1)]
        )
        # Первый опрос — без прошлого среза, дальше состояние передаётся.
        self.assertEqual(client.states[0], None)

    def test_stop_flag_ends_loop_without_events(self):
        stop = hc.StopFlag()
        try:
            client = PollFakeClient(stop, [])
            watcher = RecordingWatcher()
            stop.handler()  # сигнал ещё до цикла
            hc.serve(watcher, client, stop)
        finally:
            stop.close()
        self.assertEqual(watcher.calls, [])


class TestStopFlag(unittest.TestCase):
    """Сигнал будит select через self-pipe; close идемпотентен."""

    def test_handler_sets_flag_and_wakes_pipe(self):
        import select

        stop = hc.StopFlag()
        try:
            stop.handler()
            self.assertTrue(stop.stopped)
            ready, _, _ = select.select([stop.reader], [], [], 1.0)
            self.assertEqual(ready, [stop.reader])
            stop.drain()
            ready, _, _ = select.select([stop.reader], [], [], 0.05)
            self.assertEqual(ready, [])
        finally:
            stop.close()


if __name__ == "__main__":
    unittest.main()

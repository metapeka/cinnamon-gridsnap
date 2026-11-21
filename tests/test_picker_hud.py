#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты HUD-режима пикера (`zone-picker.py --hud`).

Проверяют три вещи:

* чистый выбор зоны под экранной точкой — `zone_picker_nav.zone_at_point`
  (та же `zone_at`, только экранные пиксели переводятся в доли рабочей области);
  этот класс тестов идёт без GTK и без X;
* разбор аргументов `--hud`/`--at`/`--follow`: без источника точки — код 2,
  с интерактивными флагами — код 2, `--at` и `--follow` взаимоисключающие,
  `--at` понимает и «X,Y», и «X Y»;
* снимок `--hud --at X,Y --shot FILE.png` — файл непустой, а разные точки
  (разные зоны) дают разные файлы, то есть подсветка действительно рисуется.

Модулю нужны `gi`/GTK и cairo (он импортирует их на уровне файла), а снимку —
ещё и дисплей (`Gdk.Display`), поэтому в CI без GTK или без X эти тесты
пропускаются. Чистая часть (точка → зона) работает всегда.

Запуск:
    python3 tests/test_picker_hud.py
    python3 -m unittest discover -s tests -v
    xvfb-run -a python3 tests/test_picker_hud.py   # вместе со снимком
"""

import collections
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_picker_nav as nav  # noqa: E402

PICKER = os.path.join(ROOT, "zone-picker.py")

Rect = collections.namedtuple("Rect", "x y w h")

# Две зоны во всю высоту: граница ровно посередине рабочей области.
LEFT, RIGHT = Rect(0, 0, 0.5, 1.0), Rect(0.5, 0, 0.5, 1.0)
TWO = [LEFT, RIGHT]
# Щель между зонами: 0.4..0.6 не занято ни одной.
GAP = [Rect(0, 0, 0.4, 1.0), Rect(0.6, 0, 0.4, 1.0)]

AREA = (0, 0, 1000, 500)  # рабочая область монитора: экранные пиксели


def gui_available():
    """Есть ли GTK и cairo: без них `zone-picker.py` не импортируется."""
    try:
        import cairo  # noqa: F401
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except Exception:
        return False


def display_available():
    """Есть ли X-дисплей: без него не посчитать мониторы и не записать снимок."""
    if not HAS_GUI:
        return False
    try:
        from gi.repository import Gdk

        return Gdk.Display.get_default() is not None
    except Exception:
        return False


HAS_GUI = gui_available()
HAS_DISPLAY = display_available()

# Конфиг для снимка: две зоны, чтобы точка слева и точка справа попадали в разные.
CONF2 = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    "[keys]\nleft = <Super><Alt>1\nright = <Super><Alt>2\n"
)


class TestZoneAtPoint(unittest.TestCase):
    """Точка в экранных пикселях -> индекс зоны; без GTK и без X."""

    def test_point_inside_left_zone(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (100, 100)), 0)

    def test_point_inside_right_zone(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (700, 100)), 1)

    def test_point_at_bottom_right_corner(self):
        self.assertEqual(nav.zone_at_point(TWO, AREA, (999, 499)), 1)

    def test_boundary_goes_to_later_zone(self):
        # Ровно на стыке побеждает та зона, что нарисована позже, — как zone_at.
        self.assertEqual(nav.zone_at_point(TWO, AREA, (500, 250)), 1)

    def test_gap_between_zones_has_no_zone(self):
        self.assertIsNone(nav.zone_at_point(GAP, AREA, (500, 250)))

    def test_point_outside_work_area(self):
        self.assertIsNone(nav.zone_at_point(TWO, AREA, (1500, 250)))
        self.assertIsNone(nav.zone_at_point(TWO, AREA, (-10, 250)))

    def test_work_area_offset_is_subtracted(self):
        # Монитор справа: его рабочая область начинается с x=100, y=50.
        shifted = (100, 50, 1000, 500)
        self.assertEqual(nav.zone_at_point(TWO, shifted, (150, 60)), 0)
        self.assertEqual(nav.zone_at_point(TWO, shifted, (1050, 60)), 1)
        # та же точка, но без поправки на смещение, попала бы в левую зону
        self.assertEqual(nav.zone_at_point(TWO, AREA, (150, 60)), 0)

    def test_empty_zones(self):
        self.assertIsNone(nav.zone_at_point([], AREA, (100, 100)))

    def test_accepts_objects_like_zone_config(self):
        class Zone:
            def __init__(self, x, y, w, h):
                self.x, self.y, self.w, self.h = x, y, w, h

        zones = [Zone(0, 0, 0.5, 1), Zone(0.5, 0, 0.5, 1)]
        self.assertEqual(nav.zone_at_point(zones, AREA, (900, 400)), 1)


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo: zone-picker.py не загрузить")
class PickerHudArgsCase(unittest.TestCase):
    """Разбор аргументов и мелкие помощники HUD; окно и X не нужны."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("zone_picker_hud_under_test", PICKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.picker = module

    def parse(self, argv):
        """Разобрать аргументы; SystemExit и stderr — наружу, тесту."""
        return self.picker.parse_args(argv)

    def parse_error(self, argv):
        """Код возврата и текст ошибки разбора для заведомо негодных аргументов."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self.picker.parse_args(argv)
        return ctx.exception.code, err.getvalue()

    # ---- --hud требует источник точки

    def test_hud_without_point_source_exits_2(self):
        code, err = self.parse_error(["--hud"])
        self.assertEqual(code, 2)
        self.assertIn("нужен источник точки", err)

    def test_hud_with_at(self):
        args = self.parse(["--hud", "--at", "100,100"])
        self.assertTrue(args.hud)
        self.assertEqual(args.at, (100, 100))
        self.assertFalse(args.follow)

    def test_hud_with_follow(self):
        args = self.parse(["--hud", "--follow"])
        self.assertTrue(args.hud)
        self.assertTrue(args.follow)
        self.assertIsNone(args.at)

    def test_at_and_follow_are_mutually_exclusive(self):
        code, err = self.parse_error(["--hud", "--at", "1,2", "--follow"])
        self.assertEqual(code, 2)
        self.assertIn("взаимоисключающие", err)

    def test_at_without_hud_exits_2(self):
        code, err = self.parse_error(["--at", "100,100"])
        self.assertEqual(code, 2)
        self.assertIn("только вместе с --hud", err)

    def test_follow_without_hud_exits_2(self):
        code, err = self.parse_error(["--follow"])
        self.assertEqual(code, 2)
        self.assertIn("только вместе с --hud", err)

    # ---- --hud несовместим с интерактивным режимом

    def test_hud_with_interactive_flags_exits_2(self):
        for extra in (["--select", "left"], ["--window", "0x1"], ["--dry-run"], ["--verify"]):
            with self.subTest(extra=extra):
                code, err = self.parse_error(["--hud", "--at", "1,2"] + extra)
                self.assertEqual(code, 2)
                self.assertIn("--hud несовместим", err)
                flag = extra[0]
                self.assertIn(flag, err)

    def test_shot_with_follow_exits_2(self):
        code, err = self.parse_error(["--hud", "--follow", "--shot", "/tmp/x.png"])
        self.assertEqual(code, 2)
        self.assertIn("--shot не работает с --follow", err)

    def test_hud_keeps_common_flags(self):
        # --conf/--monitor/--workspace/--shot общие: HUD их принимает.
        args = self.parse(
            ["--hud", "--at", "5,6", "--shot", "/tmp/x.png", "--conf", "/tmp/z.conf",
             "--monitor", "1", "--workspace", "conf"]
        )
        self.assertEqual(args.shot, "/tmp/x.png")
        self.assertEqual(args.conf, "/tmp/z.conf")
        self.assertEqual(args.monitor, 1)
        self.assertEqual(args.workspace, self.picker.zt.WORKSPACE_CONF)

    # ---- точка: один разбор на --at и на строки stdin

    def test_parse_point_formats(self):
        parse = self.picker.parse_point
        self.assertEqual(parse("100,100"), (100, 100))
        self.assertEqual(parse("100 100"), (100, 100))
        self.assertEqual(parse("  -5, 7 "), (-5, 7))
        self.assertEqual(parse(b"640 512"), (640, 512))
        self.assertEqual(parse("100,100\n"), (100, 100))

    def test_parse_point_rejects_garbage(self):
        parse = self.picker.parse_point
        for text in ("", "12", "1,2,3", "abc", "1;2", "1,два", None):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_bad_at_value_exits_2(self):
        code, err = self.parse_error(["--hud", "--at", "12"])
        self.assertEqual(code, 2)
        self.assertIn("нужны экранные координаты", err)

    # ---- подпись профиля

    def test_hud_caption(self):
        caption = self.picker.hud_caption
        self.assertEqual(caption("cinema"), "профиль «cinema»")
        self.assertEqual(caption(None), "")
        self.assertEqual(caption(""), "")

    # ---- HUD рисуется без подсказки про клавиши

    def test_draw_zones_without_hint_differs_from_hint(self):
        """Пустой hint убирает подсказку «стрелки/Enter»: HUD их не слушает."""
        import cairo

        zone = self.picker.zc.Zone("left", 0.0, 0.0, 0.5, 1.0, "")

        def render(hint):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
            cr = cairo.Context(surface)
            cr.set_source_rgb(0, 0, 0)
            cr.paint()
            self.picker.draw_zones(cr, 400, 300, [zone], 0, "профиль «cinema»", hint)
            surface.flush()
            return bytes(surface.get_data())

        self.assertNotEqual(render(""), render(self.picker.HINT))


@unittest.skipUnless(
    HAS_GUI and HAS_DISPLAY, "нужны GTK, cairo и X-дисплей: снимок HUD считается по мониторам"
)
class PickerHudShotCase(unittest.TestCase):
    """`--hud --at X,Y --shot`: PNG непустой, разные точки — разные файлы."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("zone_picker_hud_shot_under_test", PICKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.picker = module

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-picker-hud-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        with open(self.conf, "w", encoding="utf-8") as handle:
            handle.write(CONF2)

    def shot(self, name, point):
        """Запустить `main` в режиме снимка HUD и вернуть путь, размер и вывод."""
        path = os.path.join(self.dir, name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.picker.main(
                ["--hud", "--at", f"{point[0]},{point[1]}", "--shot", path,
                 "--conf", self.conf, "--monitor", "0", "--workspace", "conf"]
            )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(path), out.getvalue())
        with open(path, "rb") as handle:
            return path, handle.read(), out.getvalue()

    def test_shot_is_not_empty_and_names_the_zone(self):
        _path, data, out = self.shot("hud.png", (100, 100))
        self.assertTrue(data.startswith(b"\x89PNG"), "снимок — не PNG")
        self.assertGreater(len(data), 1000, "PNG подозрительно пустой")
        self.assertIn("снимок HUD", out)
        self.assertIn("-> left", out)

    def test_different_points_highlight_different_zones(self):
        _p1, left_png, out_left = self.shot("left.png", (100, 100))
        _p2, right_png, out_right = self.shot("right.png", (700, 100))
        self.assertIn("-> left", out_left)
        self.assertIn("-> right", out_right)
        # разные зоны подсвечены — значит, различаются и картинки
        self.assertNotEqual(left_png, right_png, "подсветка не попала в снимок")

    def test_point_in_gap_highlights_nothing(self):
        # Щели в CONF2 нет, поэтому берём точку за правым краем рабочей области:
        # подсветки нет, но сетка всё равно рисуется.
        _path, data, out = self.shot("gap.png", (10_000, 10_000))
        self.assertIn("мимо зон", out)
        self.assertGreater(len(data), 1000)


if __name__ == "__main__":
    unittest.main()

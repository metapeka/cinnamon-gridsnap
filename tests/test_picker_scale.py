#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты пикера и HUD на стыке двух пространств пикселей: логические GTK, физические X.

`zone-tiler.monitors()` отдаёт рабочие области в физических пикселях X, а
GTK-окно (``move``/``set_default_size``) и указатель GDK живут в логических.
Забытый перевод — это не «на пиксель мимо», а окно вдвое больше монитора и
первое выделение вдвое ближе к левому верхнему углу, поэтому проверяется ровно
это:

* помощники пикера `logical_areas` (рамка монитора для GTK), `pointer_physical`
  и `pick_monitor` (курсор GDK → физическая точка и монитор под ней), а также
  `Picker._initial_selection` — без X-сервера;
* ``Hud``/``Picker`` действительно ставят окну логический размер — это видно
  только на живом дисплее (`get_default_size`), поэтому такие тесты
  пропускаются без X;
* физические входы не сломаны: ``Hud.set_point`` по-прежнему получает
  физическую точку и физическую область.

Запуск:
    python3 tests/test_picker_scale.py
    python3 -m unittest discover -s tests -v
    xvfb-run -a python3 tests/test_picker_scale.py   # вместе с окнами
"""

import contextlib
import importlib.util
import io
import os
import sys
import types
import unittest

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PICKER = os.path.join(ROOT, "zone-picker.py")


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


HAS_GUI = gui_available()


def display_available():
    """Есть ли X-дисплей: без него Gtk.Window не создать."""
    if not HAS_GUI:
        return False
    try:
        from gi.repository import Gdk

        return Gdk.Display.get_default() is not None
    except Exception:
        return False


HAS_DISPLAY = display_available()

# Один монитор 1920x1080 при GDK_SCALE=2: X видит физическую область целиком,
# GDK — вдвое меньшую логическую.
MONO_PHYSICAL = [(0, 0, 1920, 1080)]
# Два таких монитора рядом: физически 1920..3840, логически 960..1920.
DUO_PHYSICAL = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]


def load_picker(name):
    """Загрузить `zone-picker.py` модулем: он импортирует GTK на уровне файла."""
    spec = importlib.util.spec_from_file_location(name, PICKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def two_zones(picker):
    """Две зоны во всю высоту: граница ровно посередине рабочей области."""
    return [
        picker.zc.Zone("left", 0.0, 0.0, 0.5, 1.0, ""),
        picker.zc.Zone("right", 0.5, 0.0, 0.5, 1.0, ""),
    ]


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo: zone-picker.py не загрузить")
class PickerScaleUnitsCase(unittest.TestCase):
    """Перевод у пикера: рамка монитора для GTK, курсор GDK, монитор под курсором."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_units_under_test")

    # ---- рамка окна: физическая область -> логическая

    def test_logical_areas_halves_the_physical_area_at_scale_two(self):
        # Замер из дымового сценария: 3840x2160 в X — это 1920x1080 для GTK.
        self.assertEqual(
            self.picker.logical_areas([(0, 0, 3840, 2160)], [2]),
            [(0, 0, 1920, 1080)],
        )

    def test_logical_areas_keeps_scale_one(self):
        area = (0, 40, 1920, 1040)
        self.assertEqual(self.picker.logical_areas([area], [1]), [area])

    def test_logical_areas_without_scales_changes_nothing(self):
        # GDK недоступен — масштаба нет, перевод не должен ничего портить.
        self.assertEqual(self.picker.logical_areas(DUO_PHYSICAL, []), DUO_PHYSICAL)

    def test_logical_areas_keeps_order_of_monitors(self):
        self.assertEqual(
            self.picker.logical_areas(DUO_PHYSICAL, [2, 2]),
            [(0, 0, 960, 540), (960, 0, 960, 540)],
        )

    # ---- курсор: логическая точка GDK -> физическая точка X

    def test_pointer_physical_doubles_at_scale_two(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, [2]), (800, 600))

    def test_pointer_physical_keeps_scale_one(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, [1]), (400, 300))

    def test_pointer_physical_without_scale(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, []), (400, 300))

    def test_pointer_physical_none_stays_none(self):
        self.assertIsNone(self.picker.pointer_physical(None, MONO_PHYSICAL, [2]))

    def test_pointer_physical_picks_the_monitor_by_logical_area(self):
        # Логическая точка 1500 — это второй монитор (логически 960..1920),
        # то есть физическая 3000. Без перевода она попала бы в первый.
        self.assertEqual(self.picker.pointer_physical((1500, 300), DUO_PHYSICAL, [2, 2]), (3000, 600))

    # ---- монитор под курсором

    def test_pick_monitor_by_logical_pointer(self):
        args = types.SimpleNamespace(monitor=None)
        real = self.picker.pointer_point
        self.picker.pointer_point = lambda: (1500, 300)  # логическая точка GDK
        try:
            self.assertEqual(self.picker.pick_monitor(DUO_PHYSICAL, [2, 2], None, args), 1)
            self.picker.pointer_point = lambda: (400, 300)
            self.assertEqual(self.picker.pick_monitor(DUO_PHYSICAL, [2, 2], None, args), 0)
        finally:
            self.picker.pointer_point = real

    def test_pick_monitor_at_scale_one_is_unchanged(self):
        args = types.SimpleNamespace(monitor=None)
        real = self.picker.pointer_point
        self.picker.pointer_point = lambda: (2000, 300)
        try:
            self.assertEqual(self.picker.pick_monitor(DUO_PHYSICAL, [1, 1], None, args), 1)
        finally:
            self.picker.pointer_point = real

    def test_pick_monitor_explicit_flag_wins(self):
        args = types.SimpleNamespace(monitor=1)
        self.assertEqual(self.picker.pick_monitor(MONO_PHYSICAL, [2], None, args), 1)

    # ---- первое выделение: курсор в физических координатах области

    def initial_selection(self, scale, logical_point):
        """`Picker._initial_selection` на заглушке: окно и X не нужны."""
        fake = types.SimpleNamespace(
            zones=two_zones(self.picker),
            args=types.SimpleNamespace(select=None),
            wa=MONO_PHYSICAL[0],
            scale=scale,
        )
        real = self.picker.pointer_point
        self.picker.pointer_point = lambda: logical_point
        try:
            return self.picker.Picker._initial_selection(fake, MONO_PHYSICAL[0][2], MONO_PHYSICAL[0][3])
        finally:
            self.picker.pointer_point = real

    def test_initial_selection_translates_logical_pointer(self):
        # Логическая 700 — это физическая 1400, то есть правая половина.
        # Без перевода 700/1920 = 0.36 дало бы левую зону.
        self.assertEqual(self.initial_selection(2, (700, 200)), 1)
        self.assertEqual(self.initial_selection(2, (300, 200)), 0)

    def test_initial_selection_at_scale_one_is_unchanged(self):
        self.assertEqual(self.initial_selection(1, (700, 200)), 0)
        self.assertEqual(self.initial_selection(1, (1400, 200)), 1)

    def test_initial_selection_by_name_ignores_pointer(self):
        fake = types.SimpleNamespace(
            zones=two_zones(self.picker),
            args=types.SimpleNamespace(select="right"),
            wa=MONO_PHYSICAL[0],
            scale=2,
        )
        self.assertEqual(self.picker.Picker._initial_selection(fake, 1920, 1080), 1)


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo: zone-picker.py не загрузить")
class HudPointCase(unittest.TestCase):
    """HUD по-прежнему живёт в физических пикселях: область и точка — физические."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_hud_under_test")

    def test_zone_at_point_takes_physical_point(self):
        # Точка и область — физические: 1400 из 1920 — правая зона, без масштаба.
        self.assertEqual(
            self.picker.nav.zone_at_point(two_zones(self.picker), (0, 0, 1920, 1080), (1400, 540)),
            1,
        )
        self.assertEqual(
            self.picker.nav.zone_at_point(two_zones(self.picker), (0, 0, 1920, 1080), (700, 540)),
            0,
        )


@unittest.skipUnless(
    HAS_DISPLAY, "нужен X-дисплей: Gtk.Window без него не создать"
)
class HudWindowGeometryCase(unittest.TestCase):
    """Окно HUD получает ЛОГИЧЕСКИЙ размер: физическая рамка вдвое больше при scale 2."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_hud_window_under_test")

    def make_hud(self, area, scale):
        # Gtk.init идемпотентен, а без него окно не создаётся в отдельном процессе.
        self.picker.Gtk.init()
        hud = self.picker.Hud(two_zones(self.picker), area, "", scale)
        # Гасим, а не destroy(): без запущенного Gtk.main() обработчик destroy зовёт
        # Gtk.main_quit и печатает Gtk-CRITICAL «main_loops == NULL».
        self.addCleanup(hud.win.hide)
        width, height = hud.win.get_default_size()
        return hud, width, height

    def test_window_size_is_logical_at_scale_two(self):
        _hud, width, height = self.make_hud((0, 0, 3840, 2160), 2)
        self.assertEqual((width, height), (1920, 1080))

    def test_window_size_is_physical_at_scale_one(self):
        _hud, width, height = self.make_hud((0, 0, 1920, 1080), 1)
        self.assertEqual((width, height), (1920, 1080))

    def test_hud_keeps_physical_work_area_for_points(self):
        hud, _w, _h = self.make_hud((0, 0, 3840, 2160), 2)
        self.assertEqual(hud.wa, (0, 0, 3840, 2160))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hud.apply_point((3000, 1500))  # физическая точка правой половины
        self.assertIn("-> right", out.getvalue())


@unittest.skipUnless(
    HAS_DISPLAY, "нужен X-дисплей: Gtk.Window без него не создать"
)
class PickerWindowGeometryCase(unittest.TestCase):
    """Окно интерактивного оверлея — тоже в логических пикселях."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_picker_window_under_test")

    def make_picker(self, area, scale):
        self.picker.Gtk.init()
        args = types.SimpleNamespace(
            select="left", dry_run=True, verify=False, quiet=True
        )
        grid = types.SimpleNamespace(ignore=[])
        picker = self.picker.Picker(
            two_zones(self.picker), grid, area, "TEST-1", 0, args, None, "", scale
        )
        # hide, а не destroy: см. `HudWindowGeometryCase` — Gtk-CRITICAL без цикла.
        self.addCleanup(picker.win.hide)
        width, height = picker.win.get_default_size()
        return picker, width, height

    def test_window_size_is_logical_at_scale_two(self):
        _picker, width, height = self.make_picker((0, 0, 3840, 2160), 2)
        self.assertEqual((width, height), (1920, 1080))

    def test_window_size_is_physical_at_scale_one(self):
        _picker, width, height = self.make_picker((0, 0, 1920, 1080), 1)
        self.assertEqual((width, height), (1920, 1080))


if __name__ == "__main__":
    unittest.main()

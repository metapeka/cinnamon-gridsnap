#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the picker and the HUD at the seam of two pixel spaces: logical GTK, physical X.

`zone-tiler.monitors()` returns the work areas in physical X pixels, while the
GTK window (``move``/``set_default_size``) and the GDK pointer live in logical ones.
A forgotten translation is not "a pixel off": the window is twice the monitor and
the first selection is twice as close to the top-left corner, so exactly these are
checked:

* the picker helpers `logical_areas` (the monitor frame for GTK), `pointer_physical`
  and `pick_monitor` (GDK cursor → a physical point and the monitor under it), plus
  `Picker._initial_selection` — without an X server;
* ``Hud``/``Picker`` really set a logical size on the window — that is visible
  only on a live display (`get_default_size`), so such tests are
  skipped without X;
* the physical inputs are not broken: ``Hud.set_point`` still receives
  a physical point and a physical area.

Running:
    python3 tests/test_picker_scale.py
    python3 -m unittest discover -s tests -v
    xvfb-run -a python3 tests/test_picker_scale.py   # together with the windows
"""

import contextlib
import importlib.util
import io
import os
import sys
import types
import unittest

try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # a run of the form `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PICKER = os.path.join(ROOT, "zone-picker.py")


def gui_available():
    """Are GTK and cairo usable: without them `zone-picker.py` does not import.

    Drawing text needs pycairo joined to gi (python3-gi-cairo); without that bridge
    `PangoCairo.create_layout` raises "could not find foreign type Context", so a bare
    `import cairo` is not enough.
    """
    try:
        import cairo
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Gtk, PangoCairo  # noqa: F401

        PangoCairo.create_layout(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)))
        return True
    except Exception:
        return False


HAS_GUI = gui_available()


def display_available():
    """Is there an X display: without it Gtk.Window cannot be created."""
    if not HAS_GUI:
        return False
    try:
        from gi.repository import Gdk

        return Gdk.Display.get_default() is not None
    except Exception:
        return False


HAS_DISPLAY = display_available()

# One monitor 1920x1080 at GDK_SCALE=2: X sees the whole physical area,
# GDK sees a logical one half the size.
MONO_PHYSICAL = [(0, 0, 1920, 1080)]
# Two such monitors side by side: physically 1920..3840, logically 960..1920.
DUO_PHYSICAL = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]


def load_picker(name):
    """Load `zone-picker.py` as a module: it imports GTK at file level."""
    spec = importlib.util.spec_from_file_location(name, PICKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def two_zones(picker):
    """Two zones over the full height: the border right in the middle of the work area."""
    return [
        picker.zc.Zone("left", 0.0, 0.0, 0.5, 1.0, ""),
        picker.zc.Zone("right", 0.5, 0.0, 0.5, 1.0, ""),
    ]


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo/python3-gi-cairo: zone-picker.py не загрузить или Pango не рисует")
class PickerScaleUnitsCase(unittest.TestCase):
    """Translation in the picker: the monitor frame for GTK, the GDK cursor, the monitor under it."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_units_under_test")

    # ---- the window frame: a physical area -> a logical one

    def test_logical_areas_halves_the_physical_area_at_scale_two(self):
        # Measured from the smoke scenario: 3840x2160 in X is 1920x1080 for GTK.
        self.assertEqual(
            self.picker.logical_areas([(0, 0, 3840, 2160)], [2]),
            [(0, 0, 1920, 1080)],
        )

    def test_logical_areas_keeps_scale_one(self):
        area = (0, 40, 1920, 1040)
        self.assertEqual(self.picker.logical_areas([area], [1]), [area])

    def test_logical_areas_without_scales_changes_nothing(self):
        # GDK is unavailable — there is no scale, the translation must not break anything.
        self.assertEqual(self.picker.logical_areas(DUO_PHYSICAL, []), DUO_PHYSICAL)

    def test_logical_areas_keeps_order_of_monitors(self):
        self.assertEqual(
            self.picker.logical_areas(DUO_PHYSICAL, [2, 2]),
            [(0, 0, 960, 540), (960, 0, 960, 540)],
        )

    # ---- the cursor: a logical GDK point -> a physical X point

    def test_pointer_physical_doubles_at_scale_two(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, [2]), (800, 600))

    def test_pointer_physical_keeps_scale_one(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, [1]), (400, 300))

    def test_pointer_physical_without_scale(self):
        self.assertEqual(self.picker.pointer_physical((400, 300), MONO_PHYSICAL, []), (400, 300))

    def test_pointer_physical_none_stays_none(self):
        self.assertIsNone(self.picker.pointer_physical(None, MONO_PHYSICAL, [2]))

    def test_pointer_physical_picks_the_monitor_by_logical_area(self):
        # Logical point 1500 is the second monitor (logically 960..1920),
        # that is physical 3000. Without the translation it would land in the first one.
        self.assertEqual(self.picker.pointer_physical((1500, 300), DUO_PHYSICAL, [2, 2]), (3000, 600))

    # ---- the monitor under the cursor

    def test_pick_monitor_by_logical_pointer(self):
        args = types.SimpleNamespace(monitor=None)
        real = self.picker.pointer_point
        self.picker.pointer_point = lambda: (1500, 300)  # a logical GDK point
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

    # ---- the first selection: the cursor in the physical coordinates of the area

    def initial_selection(self, scale, logical_point):
        """`Picker._initial_selection` on a stub: no window and no X needed."""
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
        # Logical 700 is physical 1400, that is the right half.
        # Without the translation 700/1920 = 0.36 would give the left zone.
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


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo/python3-gi-cairo: zone-picker.py не загрузить или Pango не рисует")
class HudPointCase(unittest.TestCase):
    """The HUD still lives in physical pixels: the area and the point are physical."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_hud_under_test")

    def test_zone_at_point_takes_physical_point(self):
        # The point and the area are physical: 1400 out of 1920 is the right zone, without a scale.
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
    """The HUD window gets a LOGICAL size: the physical frame is twice as large at scale 2."""

    @classmethod
    def setUpClass(cls):
        cls.picker = load_picker("zone_picker_scale_hud_window_under_test")

    def make_hud(self, area, scale):
        # Gtk.init is idempotent, and without it the window is not created in a separate process.
        self.picker.Gtk.init()
        hud = self.picker.Hud(two_zones(self.picker), area, "", scale)
        # We hide it, not destroy(): without a running Gtk.main() the destroy handler calls
        # Gtk.main_quit and prints Gtk-CRITICAL "main_loops == NULL".
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
            hud.apply_point((3000, 1500))  # a physical point of the right half
        self.assertIn("-> right", out.getvalue())


@unittest.skipUnless(
    HAS_DISPLAY, "нужен X-дисплей: Gtk.Window без него не создать"
)
class PickerWindowGeometryCase(unittest.TestCase):
    """The window of the interactive overlay is in logical pixels too."""

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
        # hide, not destroy: see `HudWindowGeometryCase` — Gtk-CRITICAL without a loop.
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

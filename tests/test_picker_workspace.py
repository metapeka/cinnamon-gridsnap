#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for choosing the workspace grid in the picker - without an X server.

They check the pure helpers of `zone-picker.py` with which the picker chooses the grid
of its workspace, of the screen and the zone set of a monitor: `zones_from_grid` (names, fractions and
bindings from one grid - the same ones the window is moved by), `screen_grid` (the grid
of the screen by the same resolver as the tiler's: `[monitors]` -> `[workspaces]` ->
`zones.conf`, and the fallback without a profile on disk), `workspace_caption`/`screen_caption`
(the caption in the overlay) and `describe_grid` (the line about the grid for `--dry-run`/`--shot`).
Plus the parsing of `--workspace`: it reuses `zone-tiler.workspace_spec`, and the
test guards exactly that, not a copy of the regexp.

The module needs `gi`/GTK and cairo (it imports them at file level), so in
CI without `python3-gi`, `gir1.2-gtk-3.0`, `python3-cairo` and `python3-gi-cairo`
the test is skipped - the workspace grid is checked there by
`tests/smoke-picker-workspace.sh`.

Run:
    python3 tests/test_picker_workspace.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PICKER = os.path.join(ROOT, "zone-picker.py")


def gui_available():
    """Whether GTK and cairo are usable: without them `zone-picker.py` does not import.

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

# The common set: three zones whose names deliberately do not match the profile.
BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.6, 1\ntop-right = 0.6, 0, 0.4, 0.5\nbottom = 0.6, 0.5, 0.4, 0.5\n"
    "[keys]\nmain = <Super><Alt>1\n"
)
# A workspace profile: its own zone names and its own key combination.
PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nvideo = 0, 0, 1, 0.75\nchat = 0, 0.75, 1, 0.25\n"
    "[keys]\nvideo = <Super><Alt>5\n"
)
# The zone set of a monitor: only part of the zones, its own geometry.
MONITOR_SECTION = "[zones:HDMI-1]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
# A screen profile: names that are in neither the common set nor the workspace profile.
SCREEN = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\ncode = 0, 0, 0.5, 1\nbrowser = 0.5, 0, 0.5, 1\n"
    "[keys]\ncode = <Super><Alt>9\n"
)
# The binding of the screen to the SCREEN profile.
BIND = "[monitors]\nHDMI-1 = studio\n"


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo/python3-gi-cairo: zone-picker.py не загрузить или Pango не рисует")
class PickerWorkspaceCase(unittest.TestCase):
    """The helpers of the picker on temporary configs; X and GTK windows are not needed."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("zone_picker_under_test", PICKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.picker = module
        cls.zt = module.zt

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-picker-ws-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        self.templates = os.path.join(self.dir, "templates")
        os.makedirs(self.templates)

    def write(self, name, text):
        """Put a file next to zones.conf (profiles - into templates/)."""
        path = os.path.join(self.templates, name) if not os.path.isabs(name) else name
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ---- the zone set of a monitor

    def test_default_set_from_grid(self):
        """Without an output name - the common [zones]: the same zones, fractions and bindings."""
        path = self.write(self.conf, BASE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual([z.name for z in zones], ["main", "top-right", "bottom"])
        self.assertEqual(zones[0].as_tuple(), (0.0, 0.0, 0.6, 1.0))
        self.assertEqual(zones[0].binding, "<Super><Alt>1")
        self.assertEqual(zones[1].binding, "")

    def test_monitor_set_comes_with_its_geometry(self):
        """The set of a monitor: the names of [zones:<output>] with the fractions of that same set."""
        path = self.write(self.conf, BASE + MONITOR_SECTION)
        grid = self.zt.Grid(path)
        zones = self.picker.zones_from_grid(grid, "HDMI-1")
        self.assertEqual([z.name for z in zones], ["left", "right"])
        self.assertEqual(zones[0].as_tuple(), (0.0, 0.0, 0.5, 1.0))
        # the pixel frame - from the same set as the names
        self.assertEqual(grid.rect("right", (0, 0, 1000, 500), "HDMI-1"), (500, 0, 500, 500))

    def test_monitor_name_case_insensitive_like_grid(self):
        """An output name in another case behaves like `Grid.zones_for`."""
        path = self.write(self.conf, BASE + MONITOR_SECTION)
        zones = self.picker.zones_from_grid(self.zt.Grid(path), "hdmi-1")
        self.assertEqual([z.name for z in zones], ["left", "right"])

    def test_profile_grid_gives_profile_zones(self):
        """The grid of a workspace profile: the names and bindings from the profile, not from [zones]."""
        self.write(self.conf, BASE)
        path = self.write("cinema.conf", PROFILE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual([z.name for z in zones], ["video", "chat"])
        self.assertEqual(zones[0].binding, "<Super><Alt>5")

    def test_grid_for_workspace_selects_profile(self):
        """Workspace 2 (EWMH 1) in [workspaces] -> the profile; workspace 1 -> the common set."""
        self.write(self.conf, BASE + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        prof = self.picker.zones_from_grid(self.zt.grid_for_workspace(self.conf, 1))
        base = self.picker.zones_from_grid(self.zt.grid_for_workspace(self.conf, 0))
        self.assertEqual([z.name for z in prof], ["video", "chat"])
        self.assertEqual([z.name for z in base], ["main", "top-right", "bottom"])

    # ---- the grid of the screen: [monitors] -> [workspaces] -> zones.conf

    def test_screen_binding_gives_screen_profile_zones(self):
        """A screen in [monitors] -> the zones of the screen profile, even if the workspace has its own."""
        self.write(self.conf, BASE + BIND + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        path = self.write("studio.conf", SCREEN)
        self.assertTrue(os.path.exists(path))
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertEqual(screen, "studio")
        self.assertEqual(desk, "cinema")  # the workspace is known anyway - for the fallback
        zones = self.picker.zones_from_grid(grid, "HDMI-1")
        self.assertEqual([z.name for z in zones], ["code", "browser"])
        self.assertEqual(zones[0].binding, "<Super><Alt>9")
        # the geometry - from the same profile as the names
        self.assertEqual(grid.rect("browser", (0, 0, 1000, 500), "HDMI-1"), (500, 0, 500, 500))

    def test_screen_binding_is_case_insensitive(self):
        """An output name in another case finds the binding too (like resolve_profile)."""
        self.write(self.conf, BASE + BIND)
        self.write("studio.conf", SCREEN)
        grid, screen, _desk = self.picker.screen_grid(self.conf, "hdmi-1", 0)
        self.assertEqual(screen, "studio")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "hdmi-1")],
                         ["code", "browser"])

    def test_screen_without_binding_keeps_workspace_grid(self):
        """No binding - everything as before: the workspace grid, the screen changes nothing."""
        self.write(self.conf, BASE + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertIsNone(screen)
        self.assertEqual(desk, "cinema")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["video", "chat"])

    def test_screen_without_binding_and_desktop_uses_common_set(self):
        """Neither a screen binding nor a workspace profile - the common [zones]."""
        self.write(self.conf, BASE)
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertIsNone(screen)
        self.assertIsNone(desk)
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid)],
                         ["main", "top-right", "bottom"])

    def test_missing_screen_profile_falls_back_to_workspace_with_message(self):
        """The screen profile is not on disk - a fallback to the workspace and a warning on stderr."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = ghost\n[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)  # we do not create profiles/ghost.conf
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertIsNone(screen)
        self.assertEqual(desk, "cinema")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["video", "chat"])
        warning = err.getvalue()
        self.assertIn("ghost", warning)
        self.assertIn("is not on disk", warning)
        self.assertIn("HDMI-1", warning)

    def test_missing_screen_profile_falls_back_to_common_set(self):
        """The workspace has none either - the common [zones], but the overlay draws a grid anyway."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = ghost\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertIsNone(screen)
        self.assertIsNone(desk)
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid)],
                         ["main", "top-right", "bottom"])
        self.assertIn("is not on disk", err.getvalue())

    def test_workspace_conf_ignores_monitor_bindings(self):
        """`--workspace conf` - zones.conf itself: neither [monitors] nor [workspaces]."""
        self.write(self.conf, BASE + BIND + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        self.write("studio.conf", SCREEN)
        grid, screen, desk = self.picker.screen_grid(
            self.conf, "HDMI-1", self.zt.WORKSPACE_CONF
        )
        self.assertIsNone(screen)
        self.assertIsNone(desk)
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid)],
                         ["main", "top-right", "bottom"])

    def test_screen_binding_wins_over_monitor_zone_set(self):
        """`[zones:<output>]` narrows the set of the already chosen screen profile."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = studio\n")
        self.write("studio.conf", SCREEN + MONITOR_SECTION)
        grid, screen, _desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertEqual(screen, "studio")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["left", "right"])

    # ---- the caption and the line about the grid

    def test_workspace_caption(self):
        caption = self.picker.workspace_caption
        self.assertEqual(caption(1, "cinema"), 'workspace 2 · profile "cinema"')
        self.assertEqual(caption(0, None), "workspace 1 · default set")
        self.assertEqual(caption(self.zt.WORKSPACE_CONF, None), "zones.conf, without workspace profiles")
        # the workspace is unknown (no _NET_CURRENT_DESKTOP) - there is nothing to caption
        self.assertEqual(caption(None, None), "")
        self.assertEqual(caption(self.zt.WORKSPACE_CURRENT, None), "")

    def test_screen_caption(self):
        """The caption names the screen if its profile gave the grid, otherwise the workspace."""
        caption = self.picker.screen_caption
        self.assertEqual(caption(1, "cinema", "HDMI-1", "studio"),
                         'monitor HDMI-1 · profile "studio"')
        self.assertEqual(caption(1, "cinema", "HDMI-1", None), 'workspace 2 · profile "cinema"')
        self.assertEqual(caption(0, None, "HDMI-1", None), "workspace 1 · default set")
        self.assertEqual(caption(self.zt.WORKSPACE_CONF, None, "HDMI-1", None),
                         "zones.conf, without workspace profiles")

    def test_describe_grid(self):
        path = self.write(self.conf, BASE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual(
            self.picker.describe_grid(zones, "workspace 1 · default set"),
            "workspace 1 · default set · zones 3: main, top-right, bottom",
        )
        self.assertEqual(
            self.picker.describe_grid(zones[:1], ""),
            "zones 1: main",
        )

    # ---- --workspace is parsed by the same code as in zone-tiler

    def test_workspace_flag_is_parsed_by_tiler(self):
        parse = self.picker.parse_args
        self.assertIsNone(parse([]).workspace)  # active workspace by default
        self.assertEqual(parse(["--workspace", "2"]).workspace, 2)
        self.assertEqual(parse(["--workspace", "current"]).workspace, self.zt.WORKSPACE_CURRENT)
        self.assertEqual(parse(["--workspace", "conf"]).workspace, self.zt.WORKSPACE_CONF)

    def test_bad_workspace_exits_with_usage_code(self):
        """A wrong workspace - code 2 and a message from `zone-tiler.workspace_spec`."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self.picker.parse_args(["--workspace", "-1"])
        self.assertEqual(ctx.exception.code, 2)
        # the text is the same as in zone-tiler.py: the parsing is reused, not copied
        self.assertIn("a workspace number", err.getvalue())


if __name__ == "__main__":
    unittest.main()

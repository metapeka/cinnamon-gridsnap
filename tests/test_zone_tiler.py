#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the GridSnap tiler - without an X server and without GTK.

They check the zones.conf parser, the conversion of work area fractions into pixels, the joining
of neighbouring zones and the window frame compensation. Window layout (EWMH) is checked separately
with a fake X11, a real display is not needed.

Run:
    python3 -m unittest discover -s tests -v
    python3 tests/test_zone_tiler.py
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_under_test", TILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()


class ConfMixin:
    """Writes a temporary zones.conf and removes it after the test."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path


BASE_GRID = "[grid]\ncols = 1.0\nrows = 1.0\n"


class TestGridParsing(ConfMixin, unittest.TestCase):
    def test_rect_zone_fractions_and_pixels(self):
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 0.8358, 1\n")
        grid = zt.Grid(path)
        self.assertEqual(grid.fractions("main"), (0.0, 0.0, 0.8358, 1.0))
        # 1920 * 0.8358 = 1604.736 -> 1605
        self.assertEqual(grid.rect("main", (0, 40, 1920, 1000)), (0, 40, 1605, 1000))

    def test_grid_range_zone(self):
        path = self.conf(
            "[grid]\ncols = 0.5, 1.0\nrows = 0.5, 1.0\n"
            "[zones]\ntl = 0:1, 0:1\nmain = 0:1, 0:2\n"
        )
        grid = zt.Grid(path)
        self.assertEqual(grid.fractions("tl"), (0.0, 0.0, 0.5, 0.5))
        self.assertEqual(grid.fractions("main"), (0.0, 0.0, 0.5, 1.0))
        self.assertEqual(grid.rect("tl", (0, 0, 100, 100)), (0, 0, 50, 50))

    def test_adjacent_zones_share_edge_without_gap(self):
        path = self.conf(
            BASE_GRID + "[zones]\n"
            "left = 0, 0, 0.8358, 1\nright = 0.8358, 0, 0.1642, 1\n"
        )
        grid = zt.Grid(path)
        wa = (0, 40, 1920, 1000)
        lx, ly, lw, lh = grid.rect("left", wa)
        rx, ry, rw, rh = grid.rect("right", wa)
        self.assertEqual(lx + lw, rx)          # the border is shared
        self.assertEqual(rx + rw, wa[2])       # and reaches the edge of the area

    def test_thirds_tile_exactly(self):
        path = self.conf(
            BASE_GRID + "[zones]\n"
            "left = 0, 0, 0.3333333, 1\n"
            "mid = 0.3333333, 0, 0.3333333, 1\n"
            "right = 0.6666666, 0, 0.3333334, 1\n"
        )
        grid = zt.Grid(path)
        wa = (0, 0, 1920, 1000)
        left = grid.rect("left", wa)
        mid = grid.rect("mid", wa)
        right = grid.rect("right", wa)
        self.assertEqual(left[0] + left[2], mid[0])
        self.assertEqual(mid[0] + mid[2], right[0])
        self.assertEqual(right[0] + right[2], wa[2])

    def test_inline_comments_are_ignored(self):
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 0.5, 1  # главная зона\n")
        grid = zt.Grid(path)
        self.assertEqual(grid.fractions("main"), (0.0, 0.0, 0.5, 1.0))

    def test_keys_section_is_preserved(self):
        path = self.conf(
            BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n"
            "[keys]\nmain = <Super><Alt>1\n"
        )
        grid = zt.Grid(path)
        self.assertEqual(grid.conf.get("keys", "main"), "<Super><Alt>1")

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            zt.Grid(os.path.join(HERE, "нет-такого-файла.conf"))

    def test_no_zones_exits(self):
        path = self.conf("[grid]\ncols = 1.0\nrows = 1.0\n[zones]\n# пусто\n")
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_last_grid_line_must_be_one(self):
        path = self.conf("[grid]\ncols = 0.5\nrows = 1.0\n[zones]\nmain = 0, 0, 1, 1\n")
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_grid_lines_must_increase(self):
        path = self.conf(
            "[grid]\ncols = 0.5, 0.5, 1.0\nrows = 1.0\n[zones]\nmain = 0, 0, 1, 1\n"
        )
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_rect_zone_out_of_range_exits(self):
        path = self.conf(BASE_GRID + "[zones]\nmain = 0, 0, 1.5, 1\n")
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_grid_range_out_of_bounds_exits(self):
        path = self.conf("[grid]\ncols = 1.0\nrows = 1.0\n[zones]\nmain = 0:2, 0:1\n")
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_bad_zone_spec_exits(self):
        path = self.conf(BASE_GRID + "[zones]\nmain = мусор\n")
        with self.assertRaises(SystemExit):
            zt.Grid(path)

    def test_default_conf_prefers_existing_xdg(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        self.addCleanup(self._restore_env, old)
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CONFIG_HOME"] = tmp
            self.assertEqual(zt.default_conf(), zt.LEGACY_CONF)
            os.makedirs(os.path.join(tmp, "gridsnap"))
            xdg = os.path.join(tmp, "gridsnap", "zones.conf")
            with open(xdg, "w", encoding="utf-8") as fh:
                fh.write("[zones]\nmain = 0, 0, 1, 1\n")
            self.assertEqual(zt.default_conf(), xdg)

    @staticmethod
    def _restore_env(old):
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old


class TestFrameCompensation(unittest.TestCase):
    def test_client_size_subtracts_frame(self):
        self.assertEqual(
            zt.client_size((100, 40, 800, 600), (1, 2, 25, 3)),
            (100, 40, 797, 572),
        )

    def test_client_size_never_below_one_pixel(self):
        self.assertEqual(
            zt.client_size((0, 0, 10, 10), (20, 20, 20, 20)),
            (0, 0, 1, 1),
        )

    def test_zero_frame_is_identity(self):
        self.assertEqual(
            zt.client_size((5, 6, 700, 400), (0, 0, 0, 0)),
            (5, 6, 700, 400),
        )


class FakeX11:
    """A fake X11: it records the calls, no X server is needed."""

    def __init__(self):
        self.calls = []

    def set_state(self, win, action, first, second=0):
        self.calls.append(("state", win, action, first, second))

    def move_resize(self, win, x, y, w, h):
        self.calls.append(("move_resize", win, x, y, w, h))


class ClosableX11(FakeX11):
    """FakeX11 aware of connection ownership - to check that nothing leaks."""

    instances = []

    def __init__(self):
        super().__init__()
        self.closed = False
        ClosableX11.instances.append(self)

    def close(self):
        self.closed = True


class TestApplyZone(unittest.TestCase):
    def setUp(self):
        self._original = zt.frame_extents
        zt.frame_extents = lambda win: (1, 2, 25, 3)
        ClosableX11.instances = []
        self.addCleanup(self._restore)

    def _restore(self):
        zt.frame_extents = self._original

    def test_apply_zone_clears_state_and_compensates_frame(self):
        fake = FakeX11()
        zt.apply_zone(0x1234, (100, 40, 800, 600), label="main", x11=fake, quiet=True)
        self.assertEqual(
            [c[0] for c in fake.calls],
            ["state", "state", "move_resize"],
        )
        self.assertEqual(fake.calls[0][3], "_NET_WM_STATE_MAXIMIZED_VERT")
        self.assertEqual(fake.calls[1][3], "_NET_WM_STATE_FULLSCREEN")
        self.assertEqual(fake.calls[2], ("move_resize", 0x1234, 100, 40, 797, 572))

    def test_apply_zone_closes_connection_it_opened(self):
        # apply_zone opened the connection itself (x11=None) - it must close it.
        original = zt.X11
        zt.X11 = ClosableX11
        self.addCleanup(setattr, zt, "X11", original)
        zt.apply_zone(0x1, (0, 0, 100, 100), x11=None, quiet=True)
        self.assertEqual(len(ClosableX11.instances), 1)
        self.assertTrue(ClosableX11.instances[0].closed)

    def test_apply_zone_does_not_close_passed_connection(self):
        # A foreign connection that was passed in (the daemon's connection) must not be touched.
        fake = ClosableX11()
        zt.apply_zone(0x1, (0, 0, 100, 100), x11=fake, quiet=True)
        self.assertFalse(fake.closed)

    def test_apply_zone_closes_even_if_move_resize_fails(self):
        # No leak must remain even after an error in the middle of the layout.
        original = zt.X11
        zt.X11 = ClosableX11
        self.addCleanup(setattr, zt, "X11", original)

        def boom(self, *a, **k):
            raise RuntimeError("X-сервер отказал")

        self.addCleanup(setattr, ClosableX11, "move_resize", ClosableX11.move_resize)
        ClosableX11.move_resize = boom
        with self.assertRaises(RuntimeError):
            zt.apply_zone(0x1, (0, 0, 100, 100), x11=None, quiet=True)
        self.assertTrue(ClosableX11.instances[0].closed)

    def test_quiet_suppresses_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zt.apply_zone(0x1, (0, 0, 100, 100), label="x", x11=FakeX11(), quiet=True)
        self.assertEqual(buf.getvalue(), "")

    def test_label_is_printed(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zt.apply_zone(0x1, (0, 0, 100, 100), label="main", x11=FakeX11(), quiet=False)
        self.assertIn("zone 'main'", buf.getvalue())
        self.assertIn("0x1", buf.getvalue())


class TestX11Close(unittest.TestCase):
    """The real `X11.close()` without an X server: it closes `dpy` exactly once.

    The leak tests above replace `close()` with their own stub `ClosableX11`, so the
    real behaviour of `X11.close()` (a call of `XCloseDisplay` and `dpy = None`) is not
    pinned down there. Here the object is created past `__init__` (`object.__new__`),
    and the LibX11 function `XCloseDisplay` is replaced with a recording stub.
    """

    class _FakeLib:
        def __init__(self):
            self.closed = []

        def XCloseDisplay(self, dpy):
            self.closed.append(dpy)
            return 1

    def test_close_calls_xclosedisplay_once_and_clears_dpy(self):
        obj = object.__new__(zt.X11)
        obj.x = self._FakeLib()
        obj.dpy = 0x1234
        obj.close()
        self.assertEqual(obj.x.closed, [0x1234])  # the first close - exactly one call
        self.assertIsNone(obj.dpy)
        obj.close()  # a repeated close - a no-op
        self.assertEqual(obj.x.closed, [0x1234])


class TestCli(ConfMixin, unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, TILER, *args], text=True, capture_output=True
        )

    def test_version(self):
        r = self.run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"zone-tiler {zt.VERSION}", r.stdout)

    def test_help(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("apply", r.stdout)

    def test_unknown_zone_is_config_error(self):
        # A zone that is in neither the workspace grid nor the common [zones] is a discrepancy
        # of the config and the profile, not a typo in the argument: code 3 and the list of zones.
        r = self.run_cli("apply", "no-such-zone")
        self.assertEqual(r.returncode, 3)
        self.assertIn("no zone", r.stderr)
        self.assertIn("Known:", r.stderr)

    def test_missing_conf_is_config_error(self):
        r = self.run_cli("list", "--conf", "/nonexistent/definitely.conf")
        self.assertEqual(r.returncode, 3)
        self.assertIn("config not found", r.stderr)

    def test_keys_command(self):
        path = self.conf(
            "[grid]\ncols = 1.0\nrows = 1.0\n[zones]\nmain = 0, 0, 1, 1\n"
            "[keys]\nmain = <Super><Alt>1\n"
        )
        r = self.run_cli("keys", "--conf", path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("main\t<Super><Alt>1", r.stdout)


class TestTabsArrangement(ConfMixin, unittest.TestCase):
    """`arrange`/`stack` put the windows of a zone onto the shared tab frame, not in a cascade.

    Without X and GTK: the window list, the metadata, the monitor and the work area
    are replaced, `--dry-run` prints the frames and moves nothing.
    """

    CONF = BASE_GRID + "[zones]\nmain = 0, 0, 1, 1\n[tabs]\nheight = 30\n"

    def setUp(self):
        self.path = self.conf(self.CONF)
        self.monitor = ("monitor-0", (0, 0, 1000, 1000))
        for name, value in (
            ("monitor_outputs", lambda: [self.monitor]),
            ("current_workspace", lambda: None),
            ("read_client_list", lambda: [0x1, 0x2]),
            (
                "read_window_meta",
                lambda win: zt.WindowMeta("Firefox", f"окно {win}", frozenset()),
            ),
            ("window_monitor", lambda win, areas: areas[0]),
        ):
            patcher = mock.patch.object(zt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        """(code, stdout, stderr) - `main` in the process, always with `--dry-run`."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = zt.main([*argv, "--dry-run", "--conf", self.path])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_arrange_puts_windows_of_one_zone_on_one_tab_frame(self):
        # `order`: with one zone for two windows both get `main`.
        code, out, _err = self.run_cli("arrange", "--by", "order")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("x=0 y=30 1000x970"), 2, out)

    def test_arrange_uses_strip_height_from_tabs_section(self):
        # 30 px from `[tabs] height`, not the default 22: the window is shifted by the strip height.
        _code, out, _err = self.run_cli("arrange", "--by", "order")
        self.assertNotIn("y=22", out)
        self.assertIn("y=30", out)

    def test_arrange_single_window_in_zone_sits_exactly_on_zone(self):
        with mock.patch.object(zt, "read_client_list", lambda: [0x1]):
            code, out, _err = self.run_cli("arrange", "--by", "order")
        self.assertEqual(code, 0)
        self.assertIn("x=0 y=0 1000x1000", out)

    def test_stack_collects_windows_as_tabs_on_one_frame(self):
        code, out, _err = self.run_cli("stack", "main")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertIn("tab 1 of 2", lines[0])
        self.assertIn("tab 2 of 2", lines[1])
        for line in lines:
            self.assertIn("x=0 y=30 1000x970", line)

    def test_stack_single_window_sits_exactly_on_zone(self):
        with mock.patch.object(zt, "read_client_list", lambda: [0x1]):
            code, out, _err = self.run_cli("stack", "main")
        self.assertEqual(code, 0)
        self.assertIn("x=0 y=0 1000x1000", out)


class TestScreenProfile(ConfMixin, unittest.TestCase):
    """The work commands take the grid from the **screen** profile (`[monitors]`).

    The screen is more important than the workspace, and a missing profile file is not a refusal to
    tile but a fallback to the grid of the workspace/active config with a warning. Without X: the
    monitors, the window frame and the exclusions are replaced; everything is computed on `--dry-run`.
    """

    CONF = (
        BASE_GRID
        + "[zones]\nglobal = 0, 0, 1, 1\n"
        + "[tabs]\nheight = 30\n"
        + "[monitors]\nMON2 = wide\n"
    )
    PROFILE = (
        "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\nbig = 0, 0, 1, 1\n"
        "[zones:MON2]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    )

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-screen-profile-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf_path = os.path.join(self.dir, "zones.conf")
        with open(self.conf_path, "w", encoding="utf-8") as fh:
            fh.write(self.CONF)
        self.templates = os.path.join(self.dir, "templates")
        os.makedirs(self.templates)
        self.profile_path = os.path.join(self.templates, "wide.conf")
        with open(self.profile_path, "w", encoding="utf-8") as fh:
            fh.write(self.PROFILE)
        self.outputs = [("MON1", (0, 0, 1000, 1000)), ("MON2", (1000, 0, 1000, 1000))]
        for name, value in (
            ("monitor_outputs", lambda: self.outputs),
            ("current_workspace", lambda: None),
            ("read_client_list", lambda: [0x1]),
            ("read_window_meta", lambda win: zt.WindowMeta("Firefox", f"окно {win}", frozenset())),
            # The window is on the second monitor, in the left half of its area.
            ("window_monitor", lambda win, areas: areas[1]),
            ("frame_extents", lambda win: (0, 0, 0, 0)),
            ("read_frame", lambda win, extents: (1000, 0, 500, 1000)),
            ("window_is_excluded", lambda win, ignore=(): False),
        ):
            patcher = mock.patch.object(zt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = zt.main([*argv, "--dry-run", "--conf", self.conf_path])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_apply_takes_zones_from_the_screen_profile(self):
        # `[zones:MON2]` of the profile, not the common `[zones]` of the active config.
        code, out, err = self.run_cli("apply", "right", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("right: x=1500 y=0 500x1000 (monitor 1: MON2)", out)

    def test_apply_without_monitor_takes_the_window_screen(self):
        # Without `--monitor` the addressee is the screen of the window (read_frame is replaced, so
        # it is on MON2), which means the zone also comes from the profile of that screen: silently
        # moving the window to the first screen and looking for the zone in a foreign profile would be a lie.
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("apply", "right")
        self.assertEqual(code, 0, err)
        self.assertIn("right: x=1500 y=0 500x1000 (monitor 1: MON2)", out)

    def test_monitor_flag_overrides_the_window_screen(self):
        # `--monitor` wins over the screen of the window: a deliberate move to the first screen.
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("apply", "global", "--monitor", "MON1")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=0 y=0 1000x1000 (monitor 0: MON1)", out)

    def test_without_window_falls_back_to_the_first_screen(self):
        # There is no window (`--dry-run` in the script): the first screen, as before, not a refusal.
        with mock.patch.object(zt, "active_window_quiet", lambda: None):
            code, out, err = self.run_cli("apply", "global")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=0 y=0 1000x1000 (monitor 0: MON1)", out)

    def test_stack_without_monitor_takes_the_active_window_screen(self):
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("stack", "right")
        self.assertEqual(code, 0, err)
        # One window - the whole zone of the second screen's profile.
        self.assertIn("x=1500 y=0 500x1000", out)

    def test_apply_sees_zones_of_the_profile_default_set_too(self):
        # The zone `big` is not in `[zones:MON2]`, but it is in the `[zones]` of the profile -
        # Grid falls back to that set, not to the active config.
        code, out, err = self.run_cli("apply", "big", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("big: x=1000 y=0 1000x1000", out)

    def test_apply_rejects_zone_of_another_screen(self):
        # MON1 is not bound to a profile: the zone `left` is not there - code 3, not a silent miss.
        # The screen is given explicitly: without `--monitor` the addressee is the screen of the window (MON2), where `left` exists.
        code, _out, err = self.run_cli("apply", "left", "--monitor", "MON1")
        self.assertEqual(code, 3)
        self.assertIn("no zone 'left'", err)
        self.assertIn("global", err)

    def test_apply_names_the_screen_profile_in_the_error(self):
        code, _out, err = self.run_cli("apply", "nosuch", "--monitor", "MON2")
        self.assertEqual(code, 3)
        self.assertIn("in the grid of monitor MON2 (profile \"wide\")", err)

    def test_apply_falls_back_when_profile_file_is_gone(self):
        os.remove(self.profile_path)
        code, out, err = self.run_cli("apply", "global", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=1000 y=0 1000x1000", out)
        self.assertIn("profile \"wide\" is not on disk", err)
        self.assertIn("using the workspace set or the default one", err)

    def test_stack_uses_the_screen_profile_zone(self):
        code, out, err = self.run_cli("stack", "right", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        # One window - the whole zone of the profile on the second monitor.
        self.assertIn("x=1500 y=0 500x1000", out)

    def test_move_finds_the_window_zone_in_the_screen_profile(self):
        code, out, err = self.run_cli("move", "right", "--window", "0x1")
        self.assertEqual(code, 0, err)
        # A window on MON2 in the left zone of the profile; the neighbour on the right is right, not global.
        self.assertIn("zone 'left' -> 'right'", out)
        self.assertIn("monitor 1: MON2", out)

    def test_arrange_uses_the_screen_profile(self):
        code, out, err = self.run_cli("arrange", "--by", "order")
        self.assertEqual(code, 0, err)
        # On MON2 there are the zones of the profile (left/right), not `global` of the active config;
        # one window in a zone takes it whole.
        self.assertIn("zone 'left': x=1000 y=0 500x1000", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

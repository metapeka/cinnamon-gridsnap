#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты раскладчика GridSnap — без X-сервера и без GTK.

Проверяют парсер zones.conf, перевод долей рабочей области в пиксели, стыковку
соседних зон и компенсацию рамки окна. Раскладка окон (EWMH) проверяется отдельно
поддельным X11, реальный дисплей не нужен.

Запуск:
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
    """Пишет временный zones.conf и убирает его после теста."""

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
        self.assertEqual(lx + lw, rx)          # граница общая
        self.assertEqual(rx + rw, wa[2])       # и доходит до края области

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
    """Поддельный X11: записывает вызовы, X-сервер не нужен."""

    def __init__(self):
        self.calls = []

    def set_state(self, win, action, first, second=0):
        self.calls.append(("state", win, action, first, second))

    def move_resize(self, win, x, y, w, h):
        self.calls.append(("move_resize", win, x, y, w, h))


class ClosableX11(FakeX11):
    """FakeX11 с учётом владения соединением — для проверки отсутствия утечки."""

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
        # apply_zone создал соединение сам (x11=None) — обязан его закрыть.
        original = zt.X11
        zt.X11 = ClosableX11
        self.addCleanup(setattr, zt, "X11", original)
        zt.apply_zone(0x1, (0, 0, 100, 100), x11=None, quiet=True)
        self.assertEqual(len(ClosableX11.instances), 1)
        self.assertTrue(ClosableX11.instances[0].closed)

    def test_apply_zone_does_not_close_passed_connection(self):
        # Переданное чужое соединение (соединение демона) трогать нельзя.
        fake = ClosableX11()
        zt.apply_zone(0x1, (0, 0, 100, 100), x11=fake, quiet=True)
        self.assertFalse(fake.closed)

    def test_apply_zone_closes_even_if_move_resize_fails(self):
        # Утечка не должна оставаться и на ошибке в середине раскладки.
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
        self.assertIn("зона 'main'", buf.getvalue())
        self.assertIn("0x1", buf.getvalue())


class TestX11Close(unittest.TestCase):
    """Реальный `X11.close()` без X-сервера: закрывает `dpy` ровно один раз.

    Тесты утечки выше подменяют `close()` собственным стабом `ClosableX11`, поэтому
    реальное поведение `X11.close()` (вызов `XCloseDisplay` и `dpy = None`) там
    не закреплено. Здесь объект создаётся в обход `__init__` (`object.__new__`),
    а LibX11-функция `XCloseDisplay` подменяется записывающей заглушкой.
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
        self.assertEqual(obj.x.closed, [0x1234])  # первый close — ровно один вызов
        self.assertIsNone(obj.dpy)
        obj.close()  # повторный close — no-op
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
        # Зона, которой нет ни в сетке стола, ни в общем [zones], — это расхождение
        # конфига и профиля, а не опечатка в аргументе: код 3 и перечень зон.
        r = self.run_cli("apply", "no-such-zone")
        self.assertEqual(r.returncode, 3)
        self.assertIn("нет зоны", r.stderr)
        self.assertIn("Есть:", r.stderr)

    def test_missing_conf_is_config_error(self):
        r = self.run_cli("list", "--conf", "/nonexistent/definitely.conf")
        self.assertEqual(r.returncode, 3)
        self.assertIn("не найден конфиг", r.stderr)

    def test_keys_command(self):
        path = self.conf(
            "[grid]\ncols = 1.0\nrows = 1.0\n[zones]\nmain = 0, 0, 1, 1\n"
            "[keys]\nmain = <Super><Alt>1\n"
        )
        r = self.run_cli("keys", "--conf", path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("main\t<Super><Alt>1", r.stdout)


class TestTabsArrangement(ConfMixin, unittest.TestCase):
    """`arrange`/`stack` кладут окна зоны на общую рамку вкладки, а не каскадом.

    Без X и GTK: список окон, метаданные, монитор и рабочая область
    подменены, `--dry-run` печатает рамки и ничего не двигает.
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
        """(код, stdout, stderr) — `main` в процессе, всегда с `--dry-run`."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = zt.main([*argv, "--dry-run", "--conf", self.path])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_arrange_puts_windows_of_one_zone_on_one_tab_frame(self):
        # `order`: двум окнам при одной зоне обоим достаётся `main`.
        code, out, _err = self.run_cli("arrange", "--by", "order")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("x=0 y=30 1000x970"), 2, out)

    def test_arrange_uses_strip_height_from_tabs_section(self):
        # 30 px из `[tabs] height`, а не умолчание 22: окно сдвинуто на высоту полосы.
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
        self.assertIn("вкладка 1 из 2", lines[0])
        self.assertIn("вкладка 2 из 2", lines[1])
        for line in lines:
            self.assertIn("x=0 y=30 1000x970", line)

    def test_stack_single_window_sits_exactly_on_zone(self):
        with mock.patch.object(zt, "read_client_list", lambda: [0x1]):
            code, out, _err = self.run_cli("stack", "main")
        self.assertEqual(code, 0)
        self.assertIn("x=0 y=0 1000x1000", out)


class TestScreenProfile(ConfMixin, unittest.TestCase):
    """Рабочие команды берут сетку из профиля **экрана** (`[monitors]`).

    Экран важнее стола, а отсутствующий файл профиля — не отказ укладки, а откат
    на сетку стола/активного конфига с предупреждением. Без X: мониторы, рамка
    окна и исключения подменены; всё считается на `--dry-run`.
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
            # Окно — на втором мониторе, в левой половине его области.
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
        # `[zones:MON2]` профиля, а не общий `[zones]` активного конфига.
        code, out, err = self.run_cli("apply", "right", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("right: x=1500 y=0 500x1000 (монитор 1: MON2)", out)

    def test_apply_without_monitor_takes_the_window_screen(self):
        # Без `--monitor` адресат — экран окна (по подмене `read_frame` оно на
        # MON2), значит и зона берётся из профиля этого экрана: молча уводить окно
        # на первый экран и искать зону в чужом профиле было бы неправдой.
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("apply", "right")
        self.assertEqual(code, 0, err)
        self.assertIn("right: x=1500 y=0 500x1000 (монитор 1: MON2)", out)

    def test_monitor_flag_overrides_the_window_screen(self):
        # `--monitor` важнее экрана окна: осознанный перенос на первый экран.
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("apply", "global", "--monitor", "MON1")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=0 y=0 1000x1000 (монитор 0: MON1)", out)

    def test_without_window_falls_back_to_the_first_screen(self):
        # Окна нет (—dry-run в скрипте): первый экран, как было раньше, а не отказ.
        with mock.patch.object(zt, "active_window_quiet", lambda: None):
            code, out, err = self.run_cli("apply", "global")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=0 y=0 1000x1000 (монитор 0: MON1)", out)

    def test_stack_without_monitor_takes_the_active_window_screen(self):
        with mock.patch.object(zt, "read_active_window", lambda: (0x1, None)):
            code, out, err = self.run_cli("stack", "right")
        self.assertEqual(code, 0, err)
        # Одно окно — зона профиля второго экрана целиком.
        self.assertIn("x=1500 y=0 500x1000", out)

    def test_apply_sees_zones_of_the_profile_default_set_too(self):
        # Зоны `big` нет в `[zones:MON2]`, но она есть в `[zones]` профиля —
        # Grid откатывается на этот набор, а не на active config.
        code, out, err = self.run_cli("apply", "big", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("big: x=1000 y=0 1000x1000", out)

    def test_apply_rejects_zone_of_another_screen(self):
        # MON1 не привязан к профилю: зоны `left` там нет — код 3, а не молчаливый промах.
        # Экран задаётся явно: без `--monitor` адресат — экран окна (MON2), где `left` есть.
        code, _out, err = self.run_cli("apply", "left", "--monitor", "MON1")
        self.assertEqual(code, 3)
        self.assertIn("нет зоны 'left'", err)
        self.assertIn("global", err)

    def test_apply_names_the_screen_profile_in_the_error(self):
        code, _out, err = self.run_cli("apply", "nosuch", "--monitor", "MON2")
        self.assertEqual(code, 3)
        self.assertIn("в сетке экрана MON2 (профиль «wide»)", err)

    def test_apply_falls_back_when_profile_file_is_gone(self):
        os.remove(self.profile_path)
        code, out, err = self.run_cli("apply", "global", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        self.assertIn("global: x=1000 y=0 1000x1000", out)
        self.assertIn("профиля «wide» нет на диске", err)
        self.assertIn("беру набор стола или общий", err)

    def test_stack_uses_the_screen_profile_zone(self):
        code, out, err = self.run_cli("stack", "right", "--monitor", "MON2")
        self.assertEqual(code, 0, err)
        # Одно окно — вся зона профиля на втором мониторе.
        self.assertIn("x=1500 y=0 500x1000", out)

    def test_move_finds_the_window_zone_in_the_screen_profile(self):
        code, out, err = self.run_cli("move", "right", "--window", "0x1")
        self.assertEqual(code, 0, err)
        # Окно на MON2 в зоне left профиля; сосед справа — right, а не global.
        self.assertIn("зона 'left' -> 'right'", out)
        self.assertIn("монитор 1: MON2", out)

    def test_arrange_uses_the_screen_profile(self):
        code, out, err = self.run_cli("arrange", "--by", "order")
        self.assertEqual(code, 0, err)
        # На MON2 — зоны профиля (left/right), а не `global` активного конфига;
        # одно окно в зоне занимает её целиком.
        self.assertIn("зона 'left': x=1000 y=0 500x1000", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

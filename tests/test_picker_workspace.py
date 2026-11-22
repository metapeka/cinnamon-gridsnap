#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты выбора сетки рабочего стола в пикере — без X-сервера.

Проверяют чистые помощники `zone-picker.py`, которыми пикер выбирает сетку
своего стола, экрана и набор зон монитора: `zones_from_grid` (имена, доли и
привязки из одной сетки — по ним же двигается окно), `screen_grid` (сетка
экрана тем же резолвером, что у раскладчика: `[monitors]` → `[workspaces]` →
`zones.conf`, и откат без профиля на диске), `workspace_caption`/`screen_caption`
(подпись в оверлее) и `describe_grid` (строка про сетку для `--dry-run`/`--shot`).
Плюс разбор `--workspace`: он переиспользует `zone-tiler.workspace_spec`, и
тест сторожит именно это, а не копию регулярки.

Модулю нужны `gi`/GTK и cairo (он импортирует их на уровне файла), поэтому в
CI без `python3-gi`, `gir1.2-gtk-3.0` и `python3-cairo` тест пропускается —
сетку стола там проверяет `tests/smoke-picker-workspace.sh`.

Запуск:
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

# Общий набор: три зоны, имена которых заведомо не совпадают с профилем.
BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.6, 1\ntop-right = 0.6, 0, 0.4, 0.5\nbottom = 0.6, 0.5, 0.4, 0.5\n"
    "[keys]\nmain = <Super><Alt>1\n"
)
# Профиль стола: свои имена зон и своё сочетание клавиш.
PROFILE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nvideo = 0, 0, 1, 0.75\nchat = 0, 0.75, 1, 0.25\n"
    "[keys]\nvideo = <Super><Alt>5\n"
)
# Набор зоны монитора: только часть зон, геометрия — своя.
MONITOR_SECTION = "[zones:HDMI-1]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
# Профиль экрана: имена, которых нет ни в общем наборе, ни в профиле стола.
SCREEN = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\ncode = 0, 0, 0.5, 1\nbrowser = 0.5, 0, 0.5, 1\n"
    "[keys]\ncode = <Super><Alt>9\n"
)
# Привязка экрана к профилю SCREEN.
BIND = "[monitors]\nHDMI-1 = studio\n"


@unittest.skipUnless(HAS_GUI, "нет python3-gi/gir1.2-gtk-3.0/python3-cairo: zone-picker.py не загрузить")
class PickerWorkspaceCase(unittest.TestCase):
    """Помощники пикера на временных конфигах; X и GTK-окна не нужны."""

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
        """Положить файл рядом с zones.conf (профили — в templates/)."""
        path = os.path.join(self.templates, name) if not os.path.isabs(name) else name
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ---- набор зон монитора

    def test_default_set_from_grid(self):
        """Без имени выхода — общий [zones]: те же зоны, доли и привязки."""
        path = self.write(self.conf, BASE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual([z.name for z in zones], ["main", "top-right", "bottom"])
        self.assertEqual(zones[0].as_tuple(), (0.0, 0.0, 0.6, 1.0))
        self.assertEqual(zones[0].binding, "<Super><Alt>1")
        self.assertEqual(zones[1].binding, "")

    def test_monitor_set_comes_with_its_geometry(self):
        """Набор монитора: имена [zones:<выход>] с долями того же набора."""
        path = self.write(self.conf, BASE + MONITOR_SECTION)
        grid = self.zt.Grid(path)
        zones = self.picker.zones_from_grid(grid, "HDMI-1")
        self.assertEqual([z.name for z in zones], ["left", "right"])
        self.assertEqual(zones[0].as_tuple(), (0.0, 0.0, 0.5, 1.0))
        # пиксельная рамка — из того же набора, что и имена
        self.assertEqual(grid.rect("right", (0, 0, 1000, 500), "HDMI-1"), (500, 0, 500, 500))

    def test_monitor_name_case_insensitive_like_grid(self):
        """Имя выхода в другом регистре ведёт себя как `Grid.zones_for`."""
        path = self.write(self.conf, BASE + MONITOR_SECTION)
        zones = self.picker.zones_from_grid(self.zt.Grid(path), "hdmi-1")
        self.assertEqual([z.name for z in zones], ["left", "right"])

    def test_profile_grid_gives_profile_zones(self):
        """Сетка профиля стола: имена и привязки — из профиля, не из [zones]."""
        self.write(self.conf, BASE)
        path = self.write("cinema.conf", PROFILE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual([z.name for z in zones], ["video", "chat"])
        self.assertEqual(zones[0].binding, "<Super><Alt>5")

    def test_grid_for_workspace_selects_profile(self):
        """Стол 2 (EWMH 1) в [workspaces] -> профиль; стол 1 -> общий набор."""
        self.write(self.conf, BASE + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        prof = self.picker.zones_from_grid(self.zt.grid_for_workspace(self.conf, 1))
        base = self.picker.zones_from_grid(self.zt.grid_for_workspace(self.conf, 0))
        self.assertEqual([z.name for z in prof], ["video", "chat"])
        self.assertEqual([z.name for z in base], ["main", "top-right", "bottom"])

    # ---- сетка экрана: [monitors] -> [workspaces] -> zones.conf

    def test_screen_binding_gives_screen_profile_zones(self):
        """Экран в [monitors] -> зоны профиля экрана, даже если у стола свой."""
        self.write(self.conf, BASE + BIND + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        path = self.write("studio.conf", SCREEN)
        self.assertTrue(os.path.exists(path))
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertEqual(screen, "studio")
        self.assertEqual(desk, "cinema")  # стол всё равно известен — для отката
        zones = self.picker.zones_from_grid(grid, "HDMI-1")
        self.assertEqual([z.name for z in zones], ["code", "browser"])
        self.assertEqual(zones[0].binding, "<Super><Alt>9")
        # геометрия — из того же профиля, что и имена
        self.assertEqual(grid.rect("browser", (0, 0, 1000, 500), "HDMI-1"), (500, 0, 500, 500))

    def test_screen_binding_is_case_insensitive(self):
        """Имя выхода в другом регистре тоже находит привязку (как resolve_profile)."""
        self.write(self.conf, BASE + BIND)
        self.write("studio.conf", SCREEN)
        grid, screen, _desk = self.picker.screen_grid(self.conf, "hdmi-1", 0)
        self.assertEqual(screen, "studio")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "hdmi-1")],
                         ["code", "browser"])

    def test_screen_without_binding_keeps_workspace_grid(self):
        """Привязки нет — всё как раньше: сетка стола, экран ничего не меняет."""
        self.write(self.conf, BASE + "[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertIsNone(screen)
        self.assertEqual(desk, "cinema")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["video", "chat"])

    def test_screen_without_binding_and_desktop_uses_common_set(self):
        """Ни привязки экрана, ни профиля стола — общий [zones]."""
        self.write(self.conf, BASE)
        grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertIsNone(screen)
        self.assertIsNone(desk)
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid)],
                         ["main", "top-right", "bottom"])

    def test_missing_screen_profile_falls_back_to_workspace_with_message(self):
        """Профиля экрана нет на диске — откат на стол и предупреждение в stderr."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = ghost\n[workspaces]\n2 = cinema\n")
        self.write("cinema.conf", PROFILE)  # profiles/ghost.conf не создаём
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 1)
        self.assertIsNone(screen)
        self.assertEqual(desk, "cinema")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["video", "chat"])
        warning = err.getvalue()
        self.assertIn("ghost", warning)
        self.assertIn("нет на диске", warning)
        self.assertIn("HDMI-1", warning)

    def test_missing_screen_profile_falls_back_to_common_set(self):
        """Того же нет и у стола — общий [zones], но оверлей всё равно рисует сетку."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = ghost\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            grid, screen, desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertIsNone(screen)
        self.assertIsNone(desk)
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid)],
                         ["main", "top-right", "bottom"])
        self.assertIn("нет на диске", err.getvalue())

    def test_workspace_conf_ignores_monitor_bindings(self):
        """`--workspace conf` — сам zones.conf: ни [monitors], ни [workspaces]."""
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
        """`[zones:<выход>]` сужает набор уже выбранного профиля экрана."""
        self.write(self.conf, BASE + "[monitors]\nHDMI-1 = studio\n")
        self.write("studio.conf", SCREEN + MONITOR_SECTION)
        grid, screen, _desk = self.picker.screen_grid(self.conf, "HDMI-1", 0)
        self.assertEqual(screen, "studio")
        self.assertEqual([z.name for z in self.picker.zones_from_grid(grid, "HDMI-1")],
                         ["left", "right"])

    # ---- подпись и строка про сетку

    def test_workspace_caption(self):
        caption = self.picker.workspace_caption
        self.assertEqual(caption(1, "cinema"), "стол 2 · профиль «cinema»")
        self.assertEqual(caption(0, None), "стол 1 · общий набор")
        self.assertEqual(caption(self.zt.WORKSPACE_CONF, None), "zones.conf, без профилей столов")
        # стол неизвестен (нет _NET_CURRENT_DESKTOP) — подписывать нечего
        self.assertEqual(caption(None, None), "")
        self.assertEqual(caption(self.zt.WORKSPACE_CURRENT, None), "")

    def test_screen_caption(self):
        """Подпись называет экран, если сетку дал его профиль, иначе — стол."""
        caption = self.picker.screen_caption
        self.assertEqual(caption(1, "cinema", "HDMI-1", "studio"),
                         "экран HDMI-1 · профиль «studio»")
        self.assertEqual(caption(1, "cinema", "HDMI-1", None), "стол 2 · профиль «cinema»")
        self.assertEqual(caption(0, None, "HDMI-1", None), "стол 1 · общий набор")
        self.assertEqual(caption(self.zt.WORKSPACE_CONF, None, "HDMI-1", None),
                         "zones.conf, без профилей столов")

    def test_describe_grid(self):
        path = self.write(self.conf, BASE)
        zones = self.picker.zones_from_grid(self.zt.Grid(path))
        self.assertEqual(
            self.picker.describe_grid(zones, "стол 1 · общий набор"),
            "стол 1 · общий набор · зон 3: main, top-right, bottom",
        )
        self.assertEqual(
            self.picker.describe_grid(zones[:1], ""),
            "зон 1: main",
        )

    # ---- --workspace разбирается тем же кодом, что у zone-tiler

    def test_workspace_flag_is_parsed_by_tiler(self):
        parse = self.picker.parse_args
        self.assertIsNone(parse([]).workspace)  # по умолчанию активный стол
        self.assertEqual(parse(["--workspace", "2"]).workspace, 2)
        self.assertEqual(parse(["--workspace", "current"]).workspace, self.zt.WORKSPACE_CURRENT)
        self.assertEqual(parse(["--workspace", "conf"]).workspace, self.zt.WORKSPACE_CONF)

    def test_bad_workspace_exits_with_usage_code(self):
        """Неверный стол — код 2 и сообщение из `zone-tiler.workspace_spec`."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self.picker.parse_args(["--workspace", "-1"])
        self.assertEqual(ctx.exception.code, 2)
        # текст тот же, что у zone-tiler.py: разбор не скопирован, а переиспользован
        self.assertIn("нужен номер стола", err.getvalue())


if __name__ == "__main__":
    unittest.main()

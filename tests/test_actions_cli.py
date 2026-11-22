#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты команды `action` — без X-сервера, GTK и живого терминала.

Проверяют то, что не требует дисплея: разбор записи `[action:<имя>]` и коды
ошибок (3 — конфиг, 1 — отказ по состоянию, 0 — успех и честный no-op), выбор
предмета действия (зона из записи, активное окно, окно вне зон), контекст
`zone_actions.ActionContext`, `--dry-run` для встроенного действия и для
команды, запуск внешней команды с `GRIDSNAP_*` и готовым `argv`, а также
встроенные `show-window`, `mirror`, `terminal` и `minimize-others`.

Окна, рамки, мониторы и запуск процессов подменяются: тест проверяет решения
раскладчика, а не muffin. Живые встроенные действия проверяет дымовой
`tests/smoke-actions.sh`.

Запуск:
    python3 tests/test_actions_cli.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_actions_test", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UI_HEIGHT = 30  # высота полосы вкладок в конфиге теста, а не умолчание 22

MONITOR = ("eDP-1", (0, 0, 1000, 1000))
LEFT = (0, 0, 500, 1000)
RIGHT = (500, 0, 500, 1000)
LEFT_TAB = (0, UI_HEIGHT, 500, 1000 - UI_HEIGHT)
RIGHT_TAB = (500, UI_HEIGHT, 500, 1000 - UI_HEIGHT)

TWO_HALVES = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nleft = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    f"[tabs]\nheight = {UI_HEIGHT}\n"
)
# Зона во весь экран: зеркало горизонтально — она сама, и партнёра нет.
FULL_SCREEN = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nfull = 0, 0, 1, 1\n"
    f"[tabs]\nheight = {UI_HEIGHT}\n"
)


class FakeX11:
    """Поддельное X-соединение: запоминает активации и закрытия."""

    def __init__(self):
        self.activated = []
        self.closed = 0

    def activate(self, win):
        self.activated.append(win)

    def close(self):
        self.closed += 1


class ActionCase(unittest.TestCase):
    """Временный конфиг, подменённые X-зависимости и подменённый `load_actions`."""

    config = TWO_HALVES

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-actions-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(self.config)
        self.zt = load_tiler()
        self.active = 0x11
        self.clients = []
        self.frames = {}
        self.desktop = None
        self.applied = []  # (окно, рамка, подпись)
        self.activated = []
        self.history = []  # (вид операции, записи)
        self.actions = {}
        self.popen = []
        for name, value in (
            ("monitor_outputs", lambda: [MONITOR]),
            ("monitors", lambda: [MONITOR[1]]),
            ("current_workspace", lambda: 0),
            ("window_workspace_for_zones", lambda win: self.desktop),
            ("active_window_quiet", lambda: self.active),
            ("read_client_list", lambda: list(self.clients)),
            ("frame_extents", lambda win: (0, 0, 0, 0)),
            ("read_frame", self._frame),
            ("read_window_meta", self._meta),
            ("window_is_excluded", lambda win, ignore=(): False),
            ("apply_zone", self._apply),
            ("deiconify", self._deiconify),
            ("snapshot_window", self._snapshot),
            ("remember", self._remember),
            ("X11", FakeX11),
            ("tabs_height", lambda grid: UI_HEIGHT),
        ):
            patcher = mock.patch.object(self.zt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        actions = mock.patch.object(zc, "load_actions", lambda conf: self.actions)
        actions.start()
        self.addCleanup(actions.stop)

    # ---- подмена X и процессов

    def _frame(self, window, extents):
        if window not in self.frames:
            raise OSError(f"нет окна 0x{window:x}")
        return self.frames[window]

    def _meta(self, window):
        return self.zt.WindowMeta("xterm", f"окно 0x{window:x}", ())

    def _apply(self, window, rect, label="", **_kwargs):
        self.applied.append((window, tuple(rect), label))
        self.frames[window] = tuple(rect)

    def _deiconify(self, window, x11=None):
        self.activated.append(window)

    def _snapshot(self, win, zone=None):
        return {"id": win, "frame": [1, 2, 3, 4], "wm_class": "", "zone": zone}

    def _remember(self, kind, records, quiet=False):
        self.history.append((kind, records))

    def _popen(self, argv, **kwargs):
        self.popen.append((argv, kwargs))
        return types.SimpleNamespace(pid=4242)

    def _spawn_window(self, window):
        """`Popen`, который «запускает» терминал: окно появляется в списке клиентов."""

        def spawn(argv, **kwargs):
            self.popen.append((argv, kwargs))
            self.place(window, rect=(0, 0, 100, 100))
            return types.SimpleNamespace(pid=4242)

        return spawn

    # ---- запуск

    def action(self, name, **record):
        self.actions = {name: dict(record)}

    def run_cli(self, *argv):
        """(код, stdout, stderr) — `main` в процессе, без подпроцесса и X."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.zt.main([*argv, "--conf", self.conf])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def place(self, *windows, rect):
        for win in windows:
            self.frames[win] = rect
            if win not in self.clients:
                self.clients.append(win)


class ActionRecordTest(ActionCase):
    """Имя действия, запись и коды ошибок конфига."""

    def test_missing_name_is_config_error(self):
        code, _out, err = self.run_cli("action")
        self.assertEqual(code, 3)
        self.assertIn("имя действия", err)

    def test_unknown_action_lists_existing(self):
        self.actions = {"probe": {"run": "/bin/true"}}
        code, _out, err = self.run_cli("action", "nope")
        self.assertEqual(code, 3)
        self.assertIn("нет действия 'nope'", err)
        self.assertIn("probe", err)

    def test_unknown_builtin_is_config_error(self):
        # `load_actions` в конфиге такого не пропустит, но ядро тоже обязано
        # сказать «такого встроенного нет», а не «команда не запустилась».
        self.actions = {"bad": {"run": "builtin:что-то"}}
        code, _out, err = self.run_cli("action", "bad")
        self.assertEqual(code, 3)
        self.assertIn("неизвестное встроенное действие", err)

    def test_unknown_placeholder_is_config_error(self):
        self.action("probe", run="/bin/echo {nope}")
        code, _out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("неизвестная подстановка", err)

    def test_empty_command_is_config_error(self):
        self.action("probe", run="")
        code, _out, err = self.run_cli("action", "probe")
        self.assertEqual(code, 3)
        self.assertIn("пустая команда", err)


class ActionSubjectTest(ActionCase):
    """Предмет действия: зона из записи, активное окно, окно вне зон."""

    def test_active_window_in_record_zone_is_the_subject(self):
        self.action("probe", run="/bin/echo {window}", zone="left")
        self.place(0x11, 0x12, rect=LEFT)
        self.active = 0x11
        code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("окно 0x11", out)
        self.assertIn("зона 'left'", out)
        self.assertIn("x=0 y=0 500x1000", out)
        self.assertIn("монитор eDP-1", out)
        self.assertIn("стол 1", out)

    def test_active_window_outside_zone_takes_topmost_in_client_list(self):
        # Список идёт снизу вверх: верхнее — последнее; активное окно в другой зоне.
        self.action("probe", run="/bin/echo {window}", zone="left")
        self.place(0x11, rect=RIGHT)
        self.place(0x12, 0x13, rect=LEFT)
        self.active = 0x11
        code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("окно 0x13", out)

    def test_empty_zone_has_no_window(self):
        self.action("probe", run="/bin/echo {window}", zone="left")
        self.place(0x11, rect=RIGHT)
        code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("окна нет", out)
        self.assertIn("зона 'left'", out)

    def test_zone_missing_in_grid_is_config_error(self):
        self.action("probe", run="/bin/echo", zone="nope")
        code, _out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("нет зоны 'nope'", err)

    def test_window_zone_found_by_tab_frame(self):
        self.action("probe", run="/bin/echo {zone}")
        self.place(0x11, rect=RIGHT_TAB)
        code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("зона 'right'", out)

    def test_window_outside_zones_has_no_zone(self):
        self.action("probe", run="/bin/echo {zone}")
        self.place(0x11, rect=(10, 10, 10, 10))
        code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("вне зон", out)

    def test_explicit_window_overrides_active(self):
        self.action("probe", run="/bin/echo {window}", zone="left")
        self.place(0x11, rect=RIGHT)
        self.place(0x12, rect=LEFT)
        self.active = 0x11
        code, out, err = self.run_cli("action", "probe", "--dry-run", "--window", "0x12")
        self.assertEqual(code, 0, err)
        self.assertIn("окно 0x12", out)

    def test_unknown_monitor_is_usage_error(self):
        self.action("probe", run="/bin/echo")
        code, _out, err = self.run_cli("action", "probe", "--monitor", "HDMI-9")
        self.assertEqual(code, 2)
        self.assertIn("монитора 'HDMI-9' нет", err)


class ActionCommandTest(ActionCase):
    """Внешняя команда: `argv`, `GRIDSNAP_*`, отсоединённый запуск."""

    def test_dry_run_prints_context_and_argv_without_spawning(self):
        self.action(
            "probe",
            run="/bin/sh -c 'echo {zone} {w} {h} {window}'",
            zone="left",
        )
        self.place(0x11, rect=LEFT)
        with mock.patch.object(self.zt.subprocess, "Popen", self._popen):
            code, out, err = self.run_cli("action", "probe", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("действие 'probe'", out)
        self.assertIn("план: argv = ", out)
        self.assertIn("echo left 500 1000 0x11", out)
        self.assertEqual(self.popen, [])

    def test_spawn_gets_argv_env_and_detaches(self):
        self.action("probe", run="/bin/echo {zone} {x}", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(self.zt.subprocess, "Popen", self._popen):
            code, out, err = self.run_cli("action", "probe")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.popen), 1)
        argv, kwargs = self.popen[0]
        self.assertEqual(argv, ["/bin/echo", "left", "0"])
        self.assertEqual(kwargs["stdin"], self.zt.subprocess.DEVNULL)
        self.assertIs(kwargs["start_new_session"], True)
        env = kwargs["env"]
        self.assertEqual(env["GRIDSNAP_WINDOW"], "0x11")
        self.assertEqual(env["GRIDSNAP_ZONE"], "left")
        self.assertEqual(env["GRIDSNAP_X"], "0")
        self.assertEqual(env["GRIDSNAP_W"], "500")
        self.assertEqual(env["GRIDSNAP_H"], "1000")
        self.assertEqual(env["GRIDSNAP_MONITOR"], "eDP-1")
        self.assertEqual(env["GRIDSNAP_WORKSPACE"], "0")  # EWMH: столы с нуля
        self.assertEqual(env["GRIDSNAP_CLASS"], "xterm")
        self.assertEqual(env["GRIDSNAP_CONF"], os.path.abspath(self.conf))
        self.assertIn("запущено", out)

    def test_quiet_spawn_prints_nothing(self):
        self.action("probe", run="/bin/true")
        with mock.patch.object(self.zt.subprocess, "Popen", self._popen):
            code, out, err = self.run_cli("action", "probe", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")

    def test_spawn_failure_is_exit_1(self):
        self.action("probe", run="/nonexistent/program")
        with mock.patch.object(
            self.zt.subprocess, "Popen", side_effect=OSError("нет такого файла")
        ):
            code, _out, err = self.run_cli("action", "probe")
        self.assertEqual(code, 1)
        self.assertIn("не удалось запустить действие", err)


class ActionShowWindowTest(ActionCase):
    """`builtin:show-window`: сосед по кольцу вкладок и no-op на пустой зоне."""

    def test_raises_next_tab_from_active(self):
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, 0x12, rect=LEFT_TAB)
        self.active = 0x11
        code, out, err = self.run_cli("action", "show")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.activated, [0x12])
        self.assertIn("0x12", out)

    def test_ring_wraps_to_first(self):
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, 0x12, rect=LEFT_TAB)
        self.active = 0x12
        code, _out, err = self.run_cli("action", "show", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.activated, [0x11])

    def test_single_window_is_shown(self):
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, rect=LEFT)
        self.active = 0x99  # активное окно вне стопки
        code, _out, err = self.run_cli("action", "show", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.activated, [0x11])

    def test_empty_zone_is_quiet_noop(self):
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, rect=RIGHT)
        code, out, err = self.run_cli("action", "show", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertEqual(self.activated, [])

    def test_empty_zone_reports_unless_quiet(self):
        self.action("show", run="builtin:show-window", zone="left")
        code, _out, err = self.run_cli("action", "show")
        self.assertEqual(code, 0)
        self.assertIn("зона 'left' пуста", err)

    def test_window_outside_zones_is_noop(self):
        self.action("show", run="builtin:show-window")
        self.place(0x11, rect=(10, 10, 10, 10))
        code, _out, err = self.run_cli("action", "show")
        self.assertEqual(code, 0)
        self.assertIn("вне зон", err)

    def test_dry_run_does_not_activate(self):
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, 0x12, rect=LEFT_TAB)
        code, out, err = self.run_cli("action", "show", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.activated, [])
        self.assertIn("план: поднять окно 0x12", out)

    def test_dry_run_on_empty_zone_prints_context_and_plan(self):
        # `--dry-run` обещан любому действию: и на пустой зоне он печатает
        # контекст и план (в stdout), а не молчит и не трогает окна.
        self.action("show", run="builtin:show-window", zone="left")
        self.place(0x11, rect=RIGHT)
        code, out, err = self.run_cli("action", "show", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("действие 'show'", out)
        self.assertIn("зона 'left' пуста", out)
        self.assertEqual(self.activated, [])


class ActionMirrorTest(ActionCase):
    """`builtin:mirror`: обмен окнами, рамки по числу окон, история."""

    def test_swaps_two_single_window_zones(self):
        self.action("mir", run="builtin:mirror", zone="left")
        self.place(0x11, rect=LEFT)
        self.place(0x22, rect=RIGHT)
        code, _out, err = self.run_cli("action", "mir", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertIn((0x22, LEFT, "left"), self.applied)
        self.assertIn((0x11, RIGHT, "right"), self.applied)
        self.assertEqual(self.frames[0x11], RIGHT)
        self.assertEqual(self.frames[0x22], LEFT)
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0][0], "mirror")
        self.assertEqual(
            sorted(record["id"] for record in self.history[0][1]), [0x11, 0x22]
        )

    def test_frames_follow_window_counts_after_swap(self):
        # Слева два окна, справа одно: предмет действия (0x11) уходит вправо,
        # правое — влево; в левой зоне снова двое — общая рамка вкладки, правой
        # хватает всей зоны.
        self.action("mir", run="builtin:mirror", zone="left")
        self.place(0x11, 0x12, rect=LEFT_TAB)
        self.place(0x22, rect=RIGHT)
        self.active = 0x11
        code, _out, err = self.run_cli("action", "mir", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.frames[0x11], RIGHT)
        self.assertEqual(self.frames[0x22], LEFT_TAB)
        self.assertEqual(self.frames[0x12], LEFT_TAB)

    def test_top_window_of_zone_moves_when_subject_is_outside(self):
        self.action("mir", run="builtin:mirror", zone="left")
        self.place(0x11, 0x12, rect=LEFT_TAB)
        self.place(0x22, rect=RIGHT)
        self.active = 0x99  # активное окно вне зон: предмета в зоне нет
        code, _out, err = self.run_cli("action", "mir", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.frames[0x12], RIGHT)

    def test_empty_zone_degenerates_to_move(self):
        self.action("mir", run="builtin:mirror", zone="left")
        self.place(0x22, rect=RIGHT)
        code, _out, err = self.run_cli("action", "mir", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.frames[0x22], LEFT)
        self.assertEqual(len(self.history[0][1]), 1)

    def test_both_empty_is_noop(self):
        self.action("mir", run="builtin:mirror", zone="left")
        code, _out, err = self.run_cli("action", "mir")
        self.assertEqual(code, 0)
        self.assertIn("пусты", err)
        self.assertEqual(self.applied, [])

    def test_both_empty_dry_run_prints_plan(self):
        self.action("mir", run="builtin:mirror", zone="left")
        code, out, err = self.run_cli("action", "mir", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("действие 'mir'", out)
        self.assertIn("пусты", out)
        self.assertEqual(self.applied, [])

    def test_no_mirror_zone_is_exit_1(self):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(FULL_SCREEN)
        self.action("mir", run="builtin:mirror", zone="full")
        self.place(0x11, 0x22, rect=(0, 0, 1000, 1000))
        code, _out, err = self.run_cli("action", "mir")
        self.assertEqual(code, 1)
        self.assertIn("нет зеркальной зоны для 'full'", err)

    def test_window_outside_zones_is_exit_1(self):
        self.action("mir", run="builtin:mirror")
        self.place(0x11, rect=(10, 10, 10, 10))
        code, _out, err = self.run_cli("action", "mir")
        self.assertEqual(code, 1)
        self.assertIn("вне зон", err)

    def test_dry_run_plans_and_does_nothing(self):
        self.action("mir", run="builtin:mirror", zone="left")
        self.place(0x11, rect=LEFT)
        self.place(0x22, rect=RIGHT)
        code, out, err = self.run_cli("action", "mir", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("план: обмен зон 'left' и 'right'", out)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.history, [])


class ActionTerminalTest(ActionCase):
    """`builtin:terminal`: запуск, ожидание нового окна, рамка по числу окон."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(self.zt, "ACTION_WAIT_TRIES", 2)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.zt, "ACTION_WAIT_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_window_is_put_on_tab_frame(self):
        self.action("term", run="builtin:terminal", command="x-terminal-emulator", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(
            self.zt.subprocess, "Popen", self._spawn_window(0x33)
        ):
            code, out, err = self.run_cli("action", "term")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.popen[0][0], ["x-terminal-emulator"])
        self.assertEqual(self.applied, [(0x33, LEFT_TAB, "left")])
        self.assertIn("0x33", out)

    def test_default_terminal_and_placeholders(self):
        self.action("term", run="builtin:terminal", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(
            self.zt.subprocess, "Popen", self._spawn_window(0x44)
        ):
            code, _out, err = self.run_cli("action", "term", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.popen[0][0], [self.zt.actions_module().DEFAULT_TERMINAL])

    def test_excluded_new_window_is_not_taken(self):
        self.action("term", run="builtin:terminal", zone="left")
        self.place(0x11, rect=LEFT)

        def spawn(argv, **kwargs):
            # Первым в списке появляется «splash» 0x33: он под исключением, и брать
            # его вместо терминала нельзя.
            self.popen.append((argv, kwargs))
            self.place(0x33, 0x44, rect=(0, 0, 100, 100))
            return types.SimpleNamespace(pid=4242)

        with mock.patch.object(self.zt.subprocess, "Popen", spawn), \
                mock.patch.object(
                    self.zt, "window_is_excluded", lambda win, ignore=(): win == 0x33
                ):
            code, _out, err = self.run_cli("action", "term", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied, [(0x44, LEFT_TAB, "left")])

    def test_no_window_appeared_is_exit_1(self):
        self.action("term", run="builtin:terminal", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(self.zt, "read_client_list", lambda: [0x11]), \
                mock.patch.object(self.zt.subprocess, "Popen", self._popen):
            code, _out, err = self.run_cli("action", "term")
        self.assertEqual(code, 1)
        self.assertIn("не появился", err)

    def test_zone_required(self):
        self.action("term", run="builtin:terminal")
        self.place(0x11, rect=(10, 10, 10, 10))
        code, _out, err = self.run_cli("action", "term")
        self.assertEqual(code, 1)
        self.assertIn("задайте zone в [action:term]", err)

    def test_dry_run_does_not_spawn(self):
        self.action("term", run="builtin:terminal", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(self.zt.subprocess, "Popen", self._popen):
            code, out, err = self.run_cli("action", "term", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("план: запустить", out)
        self.assertEqual(self.popen, [])


class ActionMinimizeOthersTest(ActionCase):
    """`builtin:minimize-others` переиспользует `focus`, а не копирует его."""

    def test_delegates_to_focus_with_zone_and_window(self):
        self.action("min", run="builtin:minimize-others", zone="left")
        self.place(0x11, rect=LEFT)
        seen = {}
        with mock.patch.object(self.zt, "cmd_focus", lambda grid, args: seen.update(vars(args))):
            code, _out, err = self.run_cli("action", "min")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["zone"], "left")
        self.assertEqual(seen["window"], 0x11)
        self.assertIs(seen["off"], False)
        self.assertIs(seen["dry_run"], False)
        self.assertIs(seen["quiet"], False)

    def test_window_is_none_in_empty_zone_so_focus_uses_active(self):
        self.action("min", run="builtin:minimize-others", zone="left")
        self.place(0x11, rect=RIGHT)
        seen = {}
        with mock.patch.object(self.zt, "cmd_focus", lambda grid, args: seen.update(vars(args))):
            code, _out, err = self.run_cli("action", "min", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertIsNone(seen["window"])
        self.assertIs(seen["quiet"], True)

    def test_dry_run_does_not_touch_focus(self):
        self.action("min", run="builtin:minimize-others", zone="left")
        self.place(0x11, rect=LEFT)
        with mock.patch.object(self.zt, "cmd_focus", side_effect=AssertionError("нельзя")):
            code, out, err = self.run_cli("action", "min", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("свёрнуты", out)

    def test_no_zone_in_record_means_this_window_only(self):
        self.action("min", run="builtin:minimize-others")
        self.place(0x11, rect=LEFT)
        seen = {}
        with mock.patch.object(self.zt, "cmd_focus", lambda grid, args: seen.update(vars(args))):
            code, _out, err = self.run_cli("action", "min")
        self.assertEqual(code, 0, err)
        self.assertIsNone(seen["zone"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты конфига действий: секции `[action:<имя>]` в `zone_config`.

Действие — секция `[action:<имя>]` (одна секция — один экземпляр, как
`[zones:<выход>]` — один набор на экран). Здесь проверяется разбор этой секции
(`zone_config.load_actions`: поля, `builtin:`, обязательные значения, опечатки —
код 3) и то, что секции действий переживают запись конфига: редактор их не
правит, а переносит дословно, как `[focus]`/`[tabs]`/`[move]`.

Без X-сервера и GTK: разбор конфига и запись раскладки — чистые функции, а
`profile use` вызывается через `main` раскладчика с `--no-hotkeys`, чтобы не
трогать gsettings и сочетания клавиш.

Запуск:
    python3 tests/test_actions_config.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_actions  # noqa: E402
import zone_config as zc  # noqa: E402

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1\n"
)

# Полная запись: все пять полей. Инлайн-комментарий после `run` — как в README,
# он не должен попасть в значение.
FULL = """[action:term-main]
run = builtin:terminal        ; обязательное поле
zone = main
command = x-terminal-emulator
key = <Super><Alt>t

[action:mirror]
run = builtin:mirror
axis = horizontal
"""

FULL_EXPECTED = {
    "term-main": {
        "run": "builtin:terminal",
        "zone": "main",
        "key": "<Super><Alt>t",
        "command": "x-terminal-emulator",
        "axis": None,
    },
    "mirror": {
        "run": "builtin:mirror",
        "zone": None,
        "key": None,
        "command": None,
        "axis": "horizontal",
    },
}


def load_tiler():
    """`zone-tiler.py` как модуль: `profile use` живёт там."""
    spec = importlib.util.spec_from_file_location("zone_tiler_actions_conf", TILER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ActionsSectionTest(unittest.TestCase):
    """Разбор `[action:<имя>]` в `zone_config` — без X и GTK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-actions-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def config_error(self, text, *needles):
        """Записать конфиг и ждать код 3 с подстроками в сообщении."""
        self.write(text)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            zc.load_actions(self.path)
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        message = err.getvalue()
        self.assertIn("zone_config:", message)
        for needle in needles:
            self.assertIn(needle, message)
        return message

    # ---------------------------------------------------------- пусто и минимум

    def test_missing_file_means_no_actions(self):
        self.assertEqual(zc.load_actions(os.path.join(self.dir, "нет.conf")), {})

    def test_file_without_action_sections_means_no_actions(self):
        self.write(BASE + "[focus]\nkey = <Super><Alt>f\n[tabs]\nkey = <Super>g\n")
        self.assertEqual(zc.load_actions(self.path), {})

    def test_empty_file_means_no_actions(self):
        self.write("")
        self.assertEqual(zc.load_actions(self.path), {})

    def test_minimal_record_has_none_for_missing_fields(self):
        self.write(BASE + "[action:own]\nrun = notify-send привет\n")
        self.assertEqual(
            zc.load_actions(self.path),
            {
                "own": {
                    "run": "notify-send привет",
                    "zone": None,
                    "key": None,
                    "command": None,
                    "axis": None,
                }
            },
        )

    # --------------------------------------------------------------- поля целиком

    def test_full_record(self):
        self.write(BASE + FULL)
        self.assertEqual(zc.load_actions(self.path), FULL_EXPECTED)

    def test_record_has_exactly_the_contract_fields(self):
        self.write(BASE + FULL)
        actions = zc.load_actions(self.path)
        self.assertEqual(tuple(actions["term-main"]), zc.ACTION_FIELDS)
        self.assertEqual(tuple(actions["mirror"]), zc.ACTION_FIELDS)

    def test_order_is_the_order_of_sections_in_the_file(self):
        self.write(
            BASE
            + "[action:third]\nrun = builtin:mirror\n"
            + "[action:first]\nrun = builtin:show-window\n"
            + "[action:second]\nrun = builtin:minimize-others\n"
        )
        self.assertEqual(list(zc.load_actions(self.path)), ["third", "first", "second"])

    def test_builtin_and_external_command_are_kept_as_written(self):
        self.write(
            BASE
            + "[action:builtin-one]\nrun = builtin:mirror\n"
            + '[action:external]\nrun = notify-send "окно в {zone}"\n'
        )
        actions = zc.load_actions(self.path)
        self.assertEqual(actions["builtin-one"]["run"], "builtin:mirror")
        self.assertEqual(actions["external"]["run"], 'notify-send "окно в {zone}"')

    def test_values_are_stripped(self):
        self.write(
            BASE + "[action: own ]\nrun =   builtin:mirror   \nzone =   main   \n"
            "key =   <Super><Alt>m   \n"
        )
        actions = zc.load_actions(self.path)
        self.assertEqual(list(actions), ["own"])
        self.assertEqual(actions["own"]["run"], "builtin:mirror")
        self.assertEqual(actions["own"]["zone"], "main")
        self.assertEqual(actions["own"]["key"], "<Super><Alt>m")

    def test_action_name_is_lowercased_and_prefix_is_case_insensitive(self):
        # Имя попадает в командную строку клавиши, поэтому оно всегда строчное;
        # префикс секции — такое же имя, регистр в нём не значим.
        self.write(BASE + "[Action:Term-Main]\nrun = builtin:terminal\n")
        self.assertEqual(list(zc.load_actions(self.path)), ["term-main"])

    def test_axis_is_normalized_to_lowercase(self):
        # `zone_actions.mirror_fractions` сравнивает ось без учёта регистра,
        # поэтому в записи она тоже приводится к нижнему регистру.
        self.write(BASE + "[action:mirror]\nrun = builtin:mirror\naxis = Vertical\n")
        self.assertEqual(zc.load_actions(self.path)["mirror"]["axis"], "vertical")

    def test_other_sections_are_ignored(self):
        self.write(
            BASE
            + "[monitors]\nHDMI-1 = work\n"
            + "[action:own]\nrun = builtin:mirror\n"
            + "[rules]\nChrome = main\n"
        )
        self.assertEqual(list(zc.load_actions(self.path)), ["own"])

    # ------------------------------------------------------------------ run

    def test_unknown_builtin_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:mirror-horizontal\n",
            "builtin:mirror-horizontal",
            "builtin:mirror",
        )

    def test_empty_builtin_name_is_config_error(self):
        # `builtin:` без имени — опечатка: известно ли имя, решает ядро действий.
        message = self.config_error(BASE + "[action:own]\nrun = builtin:\n", "builtin:")
        self.assertIn("встроенное действие", message)

    def test_empty_run_is_config_error(self):
        self.config_error(BASE + "[action:own]\nrun =\n", "run")

    def test_missing_run_is_config_error(self):
        self.config_error(BASE + "[action:own]\nzone = main\n", "run")

    # -------------------------------------------------------------- имена секций

    def test_case_differing_names_are_config_error(self):
        self.config_error(
            BASE
            + "[action:Main]\nrun = builtin:mirror\n"
            + "[action:main]\nrun = builtin:show-window\n",
            "main",
            "дважды",
        )

    def test_section_name_with_space_is_config_error(self):
        self.config_error(BASE + "[action:main left]\nrun = builtin:mirror\n", "main left")

    def test_section_name_with_dot_is_config_error(self):
        self.config_error(BASE + "[action:main.left]\nrun = builtin:mirror\n", "main.left")

    def test_empty_action_name_is_config_error(self):
        self.config_error(BASE + "[action:]\nrun = builtin:mirror\n", "пустое имя действия")

    # ------------------------------------------------------------------ поля

    def test_unknown_field_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:mirror\nzonee = main\n",
            "zonee",
            "run, zone, key, command, axis",
        )

    def test_empty_zone_is_config_error(self):
        self.config_error(BASE + "[action:own]\nrun = builtin:mirror\nzone =\n", "zone")

    def test_empty_key_is_config_error(self):
        self.config_error(BASE + "[action:own]\nrun = builtin:mirror\nkey =\n", "key")

    def test_empty_command_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:terminal\ncommand =\n", "command"
        )

    def test_command_at_mirror_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:mirror\ncommand = xterm\n",
            "command",
            "builtin:terminal",
        )

    def test_command_at_external_command_is_config_error(self):
        # Молча съеденная опечатка выглядела бы как «моя команда не работает».
        self.config_error(
            BASE + "[action:own]\nrun = notify-send привет\ncommand = xterm\n",
            "command",
            "builtin:terminal",
        )

    def test_axis_at_terminal_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:terminal\naxis = horizontal\n",
            "axis",
            "builtin:mirror",
        )

    def test_axis_at_external_command_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = notify-send привет\naxis = horizontal\n",
            "axis",
            "builtin:mirror",
        )

    def test_unknown_axis_is_config_error(self):
        self.config_error(
            BASE + "[action:own]\nrun = builtin:mirror\naxis = diagonal\n",
            "diagonal",
            "horizontal, vertical",
        )

    def test_empty_axis_is_config_error(self):
        # Пустая ось — не «умолчание», а опечатка: умолчание — отсутствие поля.
        self.config_error(BASE + "[action:own]\nrun = builtin:mirror\naxis =\n", "axis")


class ActionsSurviveSavingTest(unittest.TestCase):
    """Секции `[action:*]` — чужие для редактора: запись конфига их не теряет."""

    BODY = (
        "[action:term-main]\n"
        "# редактор зон этих строк не правит — переносит как есть\n"
        "run = builtin:terminal\n"
        "zone = main\n"
        "command = x-terminal-emulator\n"
        "key = <Super><Alt>t\n"
        "\n"
        "[action:mirror]\n"
        "run = builtin:mirror\n"
        "axis = vertical\n"
    )

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-actions-save-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "zones.conf")
        self.write(BASE + self.BODY)
        self.actions = zc.load_actions(self.path)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_sections_are_foreign_and_readable(self):
        extra = zc.load_extra_sections(self.path)
        self.assertIn("action:term-main", extra)
        self.assertIn("action:mirror", extra)
        self.assertEqual(self.actions["term-main"]["command"], "x-terminal-emulator")

    def test_save_zones_keeps_actions_verbatim(self):
        zc.save_zones(
            self.path, zc.load_zones(self.path), zc.load_extra_sections(self.path)
        )
        self.assertEqual(zc.load_actions(self.path), self.actions)
        text = self.read()
        # Тело секции переносится дословно — с комментарием и порядком строк.
        self.assertIn(
            "[action:term-main]\n"
            "# редактор зон этих строк не правит — переносит как есть\n"
            "run = builtin:terminal\nzone = main\n",
            text,
        )

    def test_save_zone_sets_keeps_actions(self):
        zc.save_zone_sets(
            self.path,
            [zc.Zone("main", 0.0, 0.0, 1.0, 1.0)],
            {},
            zc.load_extra_sections(self.path),
        )
        self.assertEqual(zc.load_actions(self.path), self.actions)

    def test_save_zones_for_a_monitor_keeps_actions(self):
        zc.save_zones(
            self.path,
            [zc.Zone("top", 0.0, 0.0, 1.0, 0.5)],
            zc.load_extra_sections(self.path),
            monitor="HDMI-1",
        )
        self.assertEqual(zc.load_actions(self.path), self.actions)

    def test_profile_use_keeps_actions(self):
        # `profile use` — та же запись конфига: профиль меняет зоны и правила, а
        # чужие секции активного конфига остаются. Клавиши не трогаем
        # (`--no-hotkeys`), иначе тест полез бы в gsettings.
        zc.save_profile(
            "work", [zc.Zone("wide", 0.0, 0.0, 1.0, 1.0)], None, self.path
        )
        tiler = load_tiler()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                tiler.main(["profile", "use", "work", "--conf", self.path, "--no-hotkeys"]),
                0,
            )
        self.assertIn("work", out.getvalue())
        self.assertEqual(zc.load_actions(self.path), self.actions)
        self.assertEqual([z.name for z in zc.load_zones(self.path)], ["wide"])


if __name__ == "__main__":
    unittest.main()

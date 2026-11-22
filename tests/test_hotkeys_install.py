#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты установки сочетаний `setup-hotkeys.py` — без Cinnamon и без gsettings.

Проверяют ровно то, на чём спотыкались руками в живой сессии:

* путь `--conf` в команде сочетания приводится к **абсолютному** — Cinnamon
  запускает сочетание не из каталога проекта, и относительный путь давал
  «не найден конфиг» (код 3), то есть молча неработающую клавишу;
* `custom-list` пишется **двумя разными значениями**: Cinnamon (`keybindings.js`)
  перечитывает записи только по изменению списка, а при переиспользовании id
  новый список совпадает со старым, и клавиша продолжает звать прежнюю команду.

`gsettings` и запуск процессов подменяются заглушками: тесты не трогают живую
сессию и не зависят от Cinnamon.

Запуск:
    python3 tests/test_hotkeys_install.py
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import io
import os
import sys
import unittest
from unittest import mock


try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SETUP_HOTKEYS = os.path.join(ROOT, "setup-hotkeys.py")


def load_module():
    spec = importlib.util.spec_from_file_location("setup_hotkeys_install_test", SETUP_HOTKEYS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WithConfTest(unittest.TestCase):
    """`with_conf`: в команду сочетания попадает только абсолютный путь."""

    def setUp(self):
        self.mod = load_module()

    def test_without_conf_command_is_unchanged(self):
        self.assertEqual(self.mod.with_conf("cmd"), "cmd")
        self.assertEqual(self.mod.with_conf("cmd", None), "cmd")

    def test_absolute_path_is_kept_as_is(self):
        self.assertEqual(
            self.mod.with_conf("cmd", "/tmp/zones.conf"), "cmd --conf /tmp/zones.conf"
        )

    def test_relative_path_becomes_absolute(self):
        command = self.mod.with_conf("cmd", "zones.conf")
        self.assertEqual(command, f"cmd --conf {os.path.abspath('zones.conf')}")
        self.assertNotIn("--conf zones.conf", command)

    def test_planned_commands_carry_absolute_conf(self):
        pairs = {(): [("main", "<Super><Alt>1")], ("--move",): []}

        def fake_pairs(conf=None, *flags):
            return pairs[flags]

        with mock.patch.object(self.mod, "zone_pairs", fake_pairs):
            items = self.mod.planned("zones.conf")
        self.assertTrue(items)
        want = os.path.abspath("zones.conf")
        for _name, _binding, command in items:
            self.assertTrue(command.endswith(want), command)


class InstallWriteTest(unittest.TestCase):
    """Что и в каком порядке пишется в `gsettings` при установке."""

    def setUp(self):
        self.mod = load_module()
        self.written = []
        self.resets = []

    def run_main(self, argv):
        """Прогнать `main()` с подменёнными gsettings, списком id и планом.

        Хранилище подменено маленькой моделью: `custom-list` и сброс ключа `name`
        меняют её так же, как это делает настоящий Cinnamon (после первого наша
        запись перестаёт быть нашей).
        """
        state = {"ids": ["custom0", "custom1"], "owned": {"custom0"}}

        def fake_gs(*args, get=False):
            self.written.append(list(args))
            if list(args[:2]) == [self.mod.LIST_SCHEMA, "custom-list"]:
                state["ids"] = [
                    part.strip().strip("'")
                    for part in str(args[2]).strip("[]").split(",")
                    if part.strip()
                ]
            return ""

        def fake_run(cmd, *args, **kwargs):
            self.resets.append(list(cmd))
            if list(cmd[:2]) == ["gsettings", "reset"] and cmd[3] == "name":
                state["owned"].discard(cmd[2].rstrip("/").rsplit("/", 1)[-1])
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod, "gs", fake_gs), \
                mock.patch.object(self.mod, "custom_ids", lambda: list(state["ids"])), \
                mock.patch.object(
                    self.mod, "cb_name",
                    lambda cid: f"{self.mod.NAME_PREFIX}main"
                    if cid in state["owned"] else "",
                ), \
                mock.patch.object(
                    self.mod, "planned",
                    lambda conf=None: [
                        ("main", "<Super><Alt>1", "cmd1"),
                        ("top-right", "<Super><Alt>2", "cmd2"),
                    ],
                ), \
                mock.patch.object(self.mod.subprocess, "run", fake_run), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                mock.patch.object(sys, "argv", ["setup-hotkeys.py", *argv]):
            return self.mod.main()

    def custom_list_writes(self):
        return [
            call[2] for call in self.written
            if call[:2] == [self.mod.LIST_SCHEMA, "custom-list"]
        ]

    def test_install_writes_the_list_twice_with_different_values(self):
        self.run_main([])
        lists = self.custom_list_writes()
        self.assertEqual(len(lists), 2, lists)
        # Одинаковый список Cinnamon не заметил бы — и оставил бы старые команды.
        self.assertNotEqual(lists[0], lists[1])
        # Первый — без наших id (custom0 был нашим и ушёл), второй — с новыми.
        self.assertNotIn("custom0", lists[0])
        self.assertIn("custom0", lists[1])
        self.assertIn("custom2", lists[1])

    def test_remove_leaves_the_list_without_our_ids(self):
        self.assertEqual(self.run_main(["--remove"]), 0)
        lists = self.custom_list_writes()
        self.assertTrue(lists)
        for value in lists:
            self.assertNotIn("custom0", value)

    def test_remove_writes_the_list_before_clearing_the_entries(self):
        # Порядок нарочный: список без наших id идёт первым — Cinnamon перестаёт
        # держать записи в памяти, и его перезапись списка их уже не вернёт.
        self.run_main(["--remove"])
        first_list = self.written[0]
        self.assertEqual(list(first_list[:2]), [self.mod.LIST_SCHEMA, "custom-list"])
        self.assertNotIn("custom0", first_list[2])
        self.assertTrue(self.resets, "записи должны сбрасываться")

    def test_remove_resets_every_key_separately(self):
        # `reset-recursively` для перемещаемой схемы на живом Cinnamon не сбрасывает
        # зарегистрированную запись (замер) — поэтому ключи сбрасываются по одному.
        self.run_main(["--remove"])
        self.assertFalse(
            [cmd for cmd in self.resets if "reset-recursively" in cmd], self.resets
        )
        self.assertEqual(
            sorted(cmd[3] for cmd in self.resets), ["binding", "command", "name"]
        )
        self.assertTrue(all("custom0" in cmd[2] for cmd in self.resets))

    def test_remove_reports_leftovers_instead_of_pretending(self):
        # Хранилище не поддаётся: наши id возвращаются в список сколько угодно раз.
        # Тогда команда обязана сказать об этом и вернуть код 1.
        mod = self.mod
        err = io.StringIO()
        with mock.patch.object(mod, "custom_ids", lambda: ["custom0"]), \
                mock.patch.object(
                    mod, "cb_name", lambda cid: f"{mod.NAME_PREFIX}test"
                ), \
                mock.patch.object(mod, "gs", lambda *a, **k: ""), \
                mock.patch.object(
                    mod.subprocess, "run",
                    lambda *a, **k: mock.Mock(returncode=0, stdout="", stderr=""),
                ), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", err), \
                mock.patch.object(sys, "argv", ["setup-hotkeys.py", "--remove"]):
            code = mod.main()
        self.assertEqual(code, mod.EXIT_FAIL)
        self.assertIn("custom0", err.getvalue())

    def test_entry_keys_are_written_before_the_list(self):
        # Записи заполняются до того, как список позовёт Cinnamon их читать: иначе
        # он зарегистрировал бы пустые сочетания.
        self.run_main([])
        kinds = [call[1] for call in self.written]
        first_list = kinds.index("custom-list")
        self.assertIn("binding", kinds[:first_list])
        self.assertIn("command", kinds[:first_list])
        self.assertIn("name", kinds[:first_list])

    def test_names_are_prefixed(self):
        self.run_main([])
        names = [call[2] for call in self.written if call[1] == "name"]
        self.assertEqual(names, ["'zone-tiler: main'", "'zone-tiler: top-right'"])


class PlannedActionsTest(unittest.TestCase):
    """Действия `[action:<имя>]` в плане сочетаний и снятие их по префиксу."""

    def setUp(self):
        self.mod = load_module()

    def planned(self, actions, conf=None):
        """`planned` без зон и прочих секций: только записи действий."""
        with mock.patch.object(
            self.mod, "zone_pairs", lambda conf=None, *flags: []
        ), mock.patch.object(
            self.mod.zone_config, "load_focus_keys", lambda path: None
        ), mock.patch.object(
            self.mod.zone_config,
            "load_tabs_config",
            lambda path: {"key": None, "height": 22},
        ), mock.patch.object(
            self.mod.zone_config, "load_actions", lambda path: actions
        ):
            return self.mod.planned(conf)

    def test_action_with_key_is_planned(self):
        items = self.planned({
            "term-main": {
                "run": "builtin:terminal",
                "zone": "main",
                "key": "<Super><Alt>t",
                "command": None,
                "axis": None,
            },
            "probe": {"run": "/bin/true", "key": None, "zone": None,
                      "command": None, "axis": None},
        })
        self.assertEqual(len(items), 1, items)
        name, binding, command = items[0]
        self.assertEqual(name, "action term-main")
        self.assertEqual(binding, "<Super><Alt>t")
        self.assertIn("action term-main --quiet", command)

    def test_action_without_key_is_skipped(self):
        self.assertEqual(
            self.planned({"probe": {"run": "/bin/true", "key": None}}), []
        )

    def test_action_command_carries_absolute_conf(self):
        items = self.planned(
            {"mirror": {"run": "builtin:mirror", "key": "<Super>m"}}, "zones.conf"
        )
        _name, _binding, command = items[0]
        self.assertTrue(command.endswith(os.path.abspath("zones.conf")), command)

    def test_default_conf_is_read_when_flag_missing(self):
        # Без `--conf` читается тот же файл, что у раскладчика; в команду сочетания
        # `--conf` при этом не попадает.
        seen = {}

        def fake_actions(path):
            seen["actions"] = path
            return {}

        with mock.patch.object(self.mod, "zone_pairs", lambda conf=None, *flags: []), \
                mock.patch.object(self.mod.zone_config, "load_focus_keys", lambda path: None), \
                mock.patch.object(
                    self.mod.zone_config,
                    "load_tabs_config",
                    lambda path: {"key": None, "height": 22},
                ), \
                mock.patch.object(self.mod.zone_config, "load_actions", fake_actions):
            self.mod.planned(None)
        self.assertEqual(seen["actions"], self.mod.zone_config.zt.default_conf())
        self.assertEqual(self.mod.conf_path(None), self.mod.zone_config.zt.default_conf())

    def test_drop_owned_removes_action_bindings_too(self):
        names = {
            "custom0": "zone-tiler: action mirror",
            "custom1": "Чужое сочетание",
        }
        calls = []
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(self.mod, "cb_name", lambda cid: names[cid]), \
                mock.patch.object(
                    self.mod.subprocess, "run",
                    lambda *args, **kwargs: calls.append(args[0]) or ok,
                ):
            kept = self.mod.drop_owned(["custom0", "custom1"])
        self.assertEqual(kept, ["custom1"])
        # Ключи нашей записи сброшены по одному, рекурсивного сброса нет вовсе.
        self.assertEqual(
            sorted(call[3] for call in calls), ["binding", "command", "name"]
        )
        self.assertTrue(all("custom0" in call[2] for call in calls))
        self.assertFalse([call for call in calls if "reset-recursively" in call])


if __name__ == "__main__":
    unittest.main(verbosity=2)

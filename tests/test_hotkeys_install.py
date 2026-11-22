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

    def run_main(self, argv):
        """Прогнать `main()` с подменёнными gsettings, списком id и планом."""
        def fake_gs(*args, get=False):
            self.written.append(list(args))
            return ""

        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(self.mod, "gs", fake_gs), \
                mock.patch.object(self.mod, "custom_ids", lambda: ["custom0", "custom1"]), \
                mock.patch.object(
                    self.mod, "cb_name",
                    lambda cid: f"{self.mod.NAME_PREFIX}main" if cid == "custom0" else "",
                ), \
                mock.patch.object(
                    self.mod, "planned",
                    lambda conf=None: [
                        ("main", "<Super><Alt>1", "cmd1"),
                        ("top-right", "<Super><Alt>2", "cmd2"),
                    ],
                ), \
                mock.patch.object(self.mod.subprocess, "run", lambda *a, **k: ok), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch.object(sys, "argv", ["setup-hotkeys.py", *argv]):
            self.mod.main()

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
        self.run_main(["--remove"])
        lists = self.custom_list_writes()
        self.assertTrue(lists)
        for value in lists:
            self.assertNotIn("custom0", value)

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
        with mock.patch.object(self.mod, "cb_name", lambda cid: names[cid]), \
                mock.patch.object(
                    self.mod.subprocess, "run",
                    lambda *args, **kwargs: calls.append(args[0]),
                ):
            kept = self.mod.drop_owned(["custom0", "custom1"])
        self.assertEqual(kept, ["custom1"])
        self.assertEqual(len(calls), 1)
        self.assertIn("reset-recursively", calls[0])
        self.assertTrue(any("custom0" in part for part in calls[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

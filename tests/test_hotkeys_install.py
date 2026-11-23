#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for installing the keybindings of `setup-hotkeys.py` — no Cinnamon, no gsettings.

They check exactly what we stumbled over by hand in a live session:

* the `--conf` path in the keybinding command is made **absolute** — Cinnamon
  runs the keybinding not from the project directory, and a relative path gave
  "config not found" (code 3), that is, a silently broken key;
* `custom-list` is written **twice with different values**: Cinnamon (`keybindings.js`)
  re-reads the entries only when the list changes, and when an id is reused
  the new list matches the old one, and the key keeps calling the previous command.

`gsettings` and process launching are replaced with stubs: the tests do not touch the live
session and do not depend on Cinnamon.

Running:
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
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
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
    """`with_conf`: only an absolute path reaches the keybinding command."""

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
    """What is written to `gsettings` on install, and in which order."""

    def setUp(self):
        self.mod = load_module()
        self.written = []
        self.resets = []

    def run_main(self, argv):
        """Run `main()` with gsettings, the id list and the plan replaced.

        The store is replaced by a small model: `custom-list` and resetting the `name` key
        change it the same way the real Cinnamon does (after the first one our
        entry stops being ours).
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
        # Cinnamon would not notice an identical list — and would keep the old commands.
        self.assertNotEqual(lists[0], lists[1])
        # The first has none of our ids (custom0 was ours and is gone), the second has the new ones.
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
        # The order is deliberate: the list without our ids goes first — Cinnamon stops
        # keeping the entries in memory, and its rewrite of the list will not bring them back.
        self.run_main(["--remove"])
        first_list = self.written[0]
        self.assertEqual(list(first_list[:2]), [self.mod.LIST_SCHEMA, "custom-list"])
        self.assertNotIn("custom0", first_list[2])
        self.assertTrue(self.resets, "записи должны сбрасываться")

    def test_remove_resets_every_key_separately(self):
        # On a live Cinnamon `reset-recursively` for a relocatable schema does not reset
        # a registered entry (measured) — so the keys are reset one by one.
        self.run_main(["--remove"])
        self.assertFalse(
            [cmd for cmd in self.resets if "reset-recursively" in cmd], self.resets
        )
        self.assertEqual(
            sorted(cmd[3] for cmd in self.resets), ["binding", "command", "name"]
        )
        self.assertTrue(all("custom0" in cmd[2] for cmd in self.resets))

    def test_remove_reports_leftovers_instead_of_pretending(self):
        # The store does not give in: our ids come back into the list any number of times.
        # Then the command has to say so and return code 1.
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
        # The entries are filled in before the list makes Cinnamon read them: otherwise
        # it would register empty keybindings.
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
    """`[action:<name>]` actions in the keybinding plan, and removing them by prefix."""

    def setUp(self):
        self.mod = load_module()

    def planned(self, actions, conf=None):
        """`planned` without zones and other sections: action entries only."""
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
        # Without `--conf` the same file as the tiler's is read; the keybinding command
        # does not get `--conf` into it in that case.
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
        # The keys of our entry are reset one by one, there is no recursive reset at all.
        self.assertEqual(
            sorted(call[3] for call in calls), ["binding", "command", "name"]
        )
        self.assertTrue(all("custom0" in call[2] for call in calls))
        self.assertFalse([call for call in calls if "reset-recursively" in call])


if __name__ == "__main__":
    unittest.main(verbosity=2)

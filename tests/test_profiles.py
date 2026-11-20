#!/usr/bin/env python3
"""Юнит-тесты профилей раскладок — чистый слой `zone_config` и CLI `profile`.

Без X и GTK: `zone_config` их не импортирует, поэтому профили проверяются
обычными юнит-тестами. Покрытие: каталог/список/сохранение/чтение/удаление,
активация (зоны, правила и клавиши меняются, `[daemon]` и прочие секции
остаются), отказ на плохом имени и CLI `zone-tiler.py profile …` без дисплея.

Запуск:
    python3 tests/test_profiles.py
"""

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

import zone_config as zc  # noqa: E402  # isort: skip


ACTIVE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\ntop = 0.5, 0, 0.5, 1\n"
    "[keys]\nmain = <Super><Alt>1\n"
    "[rules]\nChrome = main\nre:^(Navigator|firefox) = top\n"
    "[daemon]\nreflow = off   # off | neighbors\n"
)

PROFILE_ZONES = [
    zc.Zone("left", 0, 0, 0.5, 1, "<Super><Alt>1"),
    zc.Zone("right", 0.5, 0, 0.5, 1, "<Super><Alt>2"),
]
PROFILE_RULES = ["Firefox = right", "re:^VLC = left"]
MONITOR_ZONES = [zc.Zone("top", 0, 0, 1, 0.5), zc.Zone("bottom", 0, 0.5, 1, 0.5)]
ACTIVE_SETS = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\n"
    "[zones:HDMI-1]\nwide = 0, 0, 1, 1\n"
    "[daemon]\nreflow = off   # off | neighbors\n"
)


class ConfMixin:
    """Временный ``zones.conf`` и каталог профилей рядом с ним."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-profiles-")
        self.conf = os.path.join(self.dir, "zones.conf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_conf(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read_conf(self):
        with open(self.conf, encoding="utf-8") as fh:
            return fh.read()


def sections_in(text):
    return [line.strip()[1:-1] for line in text.splitlines() if line.strip().startswith("[")]


class TestProfileCatalog(ConfMixin, unittest.TestCase):
    def test_empty_directory_lists_nothing(self):
        self.assertEqual(zc.list_profiles(self.conf), [])

    def test_save_creates_file_and_lists_it(self):
        path = zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        self.assertEqual(path, os.path.join(self.dir, "templates", "work.conf"))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(zc.list_profiles(self.conf), ["work"])

    def test_saved_profile_has_only_zones_rules_keys(self):
        zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        text = zc.profile_text("work", self.conf)
        self.assertEqual(sections_in(text), ["zones", "rules", "keys"])

    def test_saved_profile_can_omit_rules(self):
        zc.save_profile("plain", PROFILE_ZONES, [], self.conf)
        self.assertEqual(sections_in(zc.profile_text("plain", self.conf)), ["zones", "keys"])

    def test_read_returns_zones_bindings_and_rules(self):
        zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        zones, rules = zc.read_profile("work", self.conf)
        self.assertEqual(
            [(z.name, z.as_tuple(), z.binding) for z in zones],
            [(z.name, z.as_tuple(), z.binding) for z in PROFILE_ZONES],
        )
        self.assertEqual(rules, PROFILE_RULES)

    def test_rules_case_is_preserved(self):
        zc.save_profile("work", PROFILE_ZONES, ["re:\\S+Deploy\\S* = left"], self.conf)
        _, rules = zc.read_profile("work", self.conf)
        self.assertEqual(rules, ["re:\\S+Deploy\\S* = left"])

    def test_list_is_sorted_and_ignores_non_conf(self):
        zc.save_profile("b", PROFILE_ZONES, conf_path=self.conf)
        zc.save_profile("a", PROFILE_ZONES, conf_path=self.conf)
        with open(os.path.join(self.dir, "templates", "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("не профиль")
        self.assertEqual(zc.list_profiles(self.conf), ["a", "b"])

    def test_remove_profile(self):
        zc.save_profile("work", PROFILE_ZONES, conf_path=self.conf)
        zc.remove_profile("work", self.conf)
        self.assertEqual(zc.list_profiles(self.conf), [])
        with self.assertRaises(FileNotFoundError):
            zc.remove_profile("work", self.conf)

    def test_read_missing_profile_raises(self):
        with self.assertRaises(FileNotFoundError):
            zc.read_profile("nope", self.conf)
        with self.assertRaises(FileNotFoundError):
            zc.profile_text("nope", self.conf)

    def test_empty_zones_rejected(self):
        with self.assertRaises(ValueError):
            zc.save_profile("empty", [], conf_path=self.conf)

    def test_bad_names_rejected(self):
        for bad in ("", "   ", ".", ".."):
            with self.assertRaises(ValueError):
                zc.profile_path(bad, self.conf)

    def test_bad_name_does_not_escape_templates(self):
        # `..` уходит в имя зоны-подобное (`..` после санитайза превращается в «..-», см. ниже),
        # но результат всё равно остаётся внутри каталога профилей.
        path = zc.profile_path("../evil", self.conf)
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, "templates"))
        self.assertNotIn(os.sep, os.path.basename(path))

    def test_name_is_sanitized(self):
        zc.save_profile("  My Profile!  ", PROFILE_ZONES, conf_path=self.conf)
        self.assertEqual(zc.list_profiles(self.conf), ["My-Profile-"])

    def test_default_dir_follows_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            self.assertEqual(
                zc.profiles_dir(), os.path.join(tmp, "gridsnap", "templates")
            )


class TestApplyProfile(ConfMixin, unittest.TestCase):
    def test_apply_swaps_zones_rules_keys_and_keeps_daemon(self):
        self.write_conf(ACTIVE)
        zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        zones = zc.apply_profile("work", self.conf)
        self.assertEqual([z.name for z in zones], ["left", "right"])
        text = self.read_conf()
        sections = sections_in(text)
        self.assertIn("grid", sections)  # из активного конфига
        self.assertIn("rules", sections)
        self.assertIn("keys", sections)
        self.assertIn("daemon", sections)
        self.assertIn("left = 0, 0, 0.5, 1", text)
        self.assertNotIn("main = 0, 0, 0.5, 1", text)
        self.assertIn("Firefox = right", text)
        self.assertIn("re:^VLC = left", text)
        self.assertIn("left = <Super><Alt>1", text)
        self.assertIn("right = <Super><Alt>2", text)
        self.assertIn("reflow = off   # off | neighbors", text)

    def test_apply_without_rules_drops_rules(self):
        self.write_conf(ACTIVE)
        zc.save_profile("plain", PROFILE_ZONES, [], self.conf)
        zc.apply_profile("plain", self.conf)
        self.assertNotIn("rules", sections_in(self.read_conf()))

    def test_apply_keeps_unknown_section(self):
        self.write_conf(ACTIVE + "[custom]\nключ = значение\n")
        zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        zc.apply_profile("work", self.conf)
        self.assertIn("[custom]\nключ = значение", self.read_conf())

    def test_apply_missing_profile_raises(self):
        self.write_conf(ACTIVE)
        with self.assertRaises(FileNotFoundError):
            zc.apply_profile("nope", self.conf)

    def test_apply_second_time_is_idempotent(self):
        self.write_conf(ACTIVE)
        zc.save_profile("work", PROFILE_ZONES, PROFILE_RULES, self.conf)
        zc.apply_profile("work", self.conf)
        once = self.read_conf()
        zc.apply_profile("work", self.conf)
        self.assertEqual(self.read_conf(), once)


class TestProfileZoneSets(ConfMixin, unittest.TestCase):
    """Профиль переносит все наборы зон: ``[zones]`` и ``[zones:<монитор>]``."""

    def test_save_profile_with_two_sets(self):
        zc.save_profile("multi", PROFILE_ZONES, PROFILE_RULES, self.conf,
                        monitor_sets={"HDMI-1": MONITOR_ZONES})
        text = zc.profile_text("multi", self.conf)
        self.assertEqual(sections_in(text), ["zones", "zones:HDMI-1", "rules", "keys"])
        self.assertIn("top = 0, 0, 1, 0.5", text)
        self.assertNotIn("grid", sections_in(text))

    def test_read_profile_sets_returns_all_sets(self):
        zc.save_profile("multi", PROFILE_ZONES, PROFILE_RULES, self.conf,
                        monitor_sets={"HDMI-1": MONITOR_ZONES})
        zones, rules, monitor_sets = zc.read_profile_sets("multi", self.conf)
        self.assertEqual([z.name for z in zones], ["left", "right"])
        self.assertEqual(rules, PROFILE_RULES)
        self.assertEqual([z.name for z in monitor_sets["HDMI-1"]], ["top", "bottom"])

    def test_read_profile_keeps_two_value_contract(self):
        # редактор распаковывает ``zones, rules = read_profile(...)`` — две величины
        zc.save_profile("multi", PROFILE_ZONES, PROFILE_RULES, self.conf,
                        monitor_sets={"HDMI-1": MONITOR_ZONES})
        zones, rules = zc.read_profile("multi", self.conf)
        self.assertEqual([z.name for z in zones], ["left", "right"])
        self.assertEqual(rules, PROFILE_RULES)

    def test_profile_may_have_only_monitor_sets(self):
        zc.save_profile("monitors", [], None, self.conf, monitor_sets={"HDMI-1": MONITOR_ZONES})
        self.assertEqual(sections_in(zc.profile_text("monitors", self.conf)), ["zones:HDMI-1"])
        self.assertEqual(zc.read_profile("monitors", self.conf), ([], []))

    def test_profile_without_any_zones_rejected(self):
        for monitor_sets in (None, {}, {"HDMI-1": []}):
            with self.assertRaises(ValueError):
                zc.save_profile("empty", [], None, self.conf, monitor_sets=monitor_sets)

    def test_apply_profile_brings_all_sets_and_keeps_grid_daemon(self):
        self.write_conf(ACTIVE_SETS)
        zc.save_profile("multi", PROFILE_ZONES, PROFILE_RULES, self.conf,
                        monitor_sets={"DP-2": MONITOR_ZONES})
        zones = zc.apply_profile("multi", self.conf)
        self.assertEqual([z.name for z in zones], ["left", "right"])
        text = self.read_conf()
        self.assertEqual(
            zc.list_zone_sets(self.conf),
            {None: ["left", "right"], "DP-2": ["top", "bottom"]},
        )
        self.assertNotIn("zones:HDMI-1", sections_in(text))  # наборы конфига заменены набором профиля
        self.assertIn("[grid]", text)
        self.assertIn("reflow = off   # off | neighbors", text)
        self.assertIn("Firefox = right", text)
        self.assertNotIn("main = 0, 0, 0.5, 1", text)

    def test_apply_profile_keeps_active_monitor_sets_when_profile_has_theirs(self):
        self.write_conf(ACTIVE_SETS)
        zc.save_profile("multi", PROFILE_ZONES, [], self.conf,
                        monitor_sets={"HDMI-1": MONITOR_ZONES})
        zc.apply_profile("multi", self.conf)
        self.assertEqual(
            zc.list_zone_sets(self.conf),
            {None: ["left", "right"], "HDMI-1": ["top", "bottom"]},
        )

    def test_apply_profile_is_idempotent_with_sets(self):
        self.write_conf(ACTIVE_SETS)
        zc.save_profile("multi", PROFILE_ZONES, PROFILE_RULES, self.conf,
                        monitor_sets={"DP-2": MONITOR_ZONES})
        zc.apply_profile("multi", self.conf)
        once = self.read_conf()
        zc.apply_profile("multi", self.conf)
        self.assertEqual(self.read_conf(), once)


class TestHeadless(unittest.TestCase):
    def run_python(self, code=None, *args, **kwargs):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        cmd = [sys.executable]
        if code is not None:
            cmd += ["-c", code]
        cmd += list(args)
        return subprocess.run(
            cmd,
            cwd=kwargs.pop("cwd", ROOT),
            env=env,
            text=True,
            capture_output=True,
        )

    def test_zone_config_imports_without_display(self):
        r = self.run_python("import zone_config; print(len(zone_config.list_profiles()))")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_cli_profile_works_without_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(ACTIVE)
            tiler = os.path.join(ROOT, "zone-tiler.py")

            def cli(*args):
                return self.run_python(None, tiler, *args)

            self.assertEqual(cli("profile", "list", "--conf", conf).stdout.strip(), "")
            r = cli("profile", "save", "demo", "--conf", conf)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(cli("profile", "list", "--conf", conf).stdout.strip(), "demo")
            r = cli("profile", "show", "demo", "--conf", conf)
            self.assertIn("[zones]", r.stdout)
            self.assertIn("demo", r.stdout.splitlines()[0])
            r = cli("profile", "use", "demo", "--no-hotkeys", "--conf", conf)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("применён", r.stdout)
            r = cli("profile", "rm", "demo", "--conf", conf)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(cli("profile", "list", "--conf", conf).stdout.strip(), "")

    def test_cli_bad_name_and_missing_profile_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(ACTIVE)
            tiler = os.path.join(ROOT, "zone-tiler.py")
            bad = self.run_python(None, tiler, "profile", "save", "..", "--conf", conf)
            self.assertEqual(bad.returncode, 2)
            missing = self.run_python(None, tiler, "profile", "show", "nope", "--conf", conf)
            self.assertEqual(missing.returncode, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

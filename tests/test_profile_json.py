#!/usr/bin/env python3
"""Юнит-тесты переносимого JSON-профиля (``gridsnap_profile``: 1).

Проверяется контракт формата: ``profile_to_dict`` (профиль → dict),
``profile_from_dict`` (dict → файл профиля), стабильный текст
(``dumps_profile``) и CLI ``zone-tiler.py profile export/import`` без дисплея.
Круг проверяется целиком: профиль → dict → файл → профиль сохраняет зоны,
наборы мониторов, правила и клавиши, а ошибки формата выходят кодом 3.

Запуск:
    python3 tests/test_profile_json.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402  # isort: skip

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

ZONES = [zc.Zone("main", 0, 0, 0.5, 1), zc.Zone("side", 0.5, 0, 0.5, 1, "<Super><Alt>2")]
RULES = ["Chrome = main", "re:^VLC = side"]
IGNORE = ["TelegramDesktop = диалоги Telegram"]
HDMI_ZONES = [zc.Zone("top", 0, 0, 1, 0.5), zc.Zone("bottom", 0, 0.5, 1, 0.5)]
KEYS = {"main": "<Super><Alt>1", "side": "<Super><Alt>2", "top": "<Super><F1>"}

GOOD = {
    "gridsnap_profile": 1,
    "name": "cinema",
    "zones": {"main": [0, 0, 0.5, 1], "side": [0.5, 0, 0.5, 1]},
    "monitors": {"HDMI-1": {"top": [0, 0, 1, 0.5]}},
    "rules": ["Chrome = main"],
    "keys": {"main": "<Super><Alt>1"},
}

TEMPLATES = ("templates",)


class ConfMixin:
    """Временный ``zones.conf`` и каталог профилей рядом с ним."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-json-")
        self.conf = os.path.join(self.dir, "zones.conf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def profile_path(self, name):
        return os.path.join(self.dir, *TEMPLATES, name + zc.PROFILE_EXT)

    def profile_text(self, name):
        with open(self.profile_path(name), encoding="utf-8") as fh:
            return fh.read()


def sorted_json(data):
    """Тот же текст, что пишет ``dumps_profile``: для сверки стабильности."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class TestProfileToDict(ConfMixin, unittest.TestCase):
    def test_contract_fields_of_full_profile(self):
        zones = [zc.Zone("main", 0, 0, 0.5, 1, "<Super><Alt>1"),
                 zc.Zone("side", 0.5, 0, 0.5, 1, "<Super><Alt>2")]
        monitor_zones = [zc.Zone("top", 0, 0, 1, 0.5, "<Super><F1>"),
                         zc.Zone("bottom", 0, 0.5, 1, 0.5)]
        zc.save_profile("cinema", zones, RULES, self.conf,
                        monitor_sets={"HDMI-1": monitor_zones})
        data = zc.profile_to_dict("cinema", self.conf)
        self.assertEqual(data["gridsnap_profile"], 1)
        self.assertEqual(data["name"], "cinema")
        self.assertEqual(data["zones"], {"main": [0, 0, 0.5, 1], "side": [0.5, 0, 0.5, 1]})
        self.assertEqual(data["monitors"], {"HDMI-1": {"top": [0, 0, 1, 0.5],
                                                       "bottom": [0, 0.5, 1, 0.5]}})
        self.assertEqual(data["rules"], RULES)
        self.assertEqual(data["keys"], {"main": "<Super><Alt>1", "side": "<Super><Alt>2",
                                        "top": "<Super><F1>"})

    def test_integral_fractions_are_ints_and_fractions_float(self):
        zc.save_profile("whole", [zc.Zone("all", 0, 0, 1, 1)], conf_path=self.conf)
        data = zc.profile_to_dict("whole", self.conf)
        self.assertEqual(data["zones"], {"all": [0, 0, 1, 1]})
        self.assertEqual([type(v) for v in data["zones"]["all"]], [int] * 4)
        zc.save_profile("half", [zc.Zone("left", 0, 0, 0.5, 1)], conf_path=self.conf)
        data = zc.profile_to_dict("half", self.conf)
        self.assertEqual(data["zones"], {"left": [0, 0, 0.5, 1]})
        self.assertIsInstance(data["zones"]["left"][2], float)

    def test_fractions_are_rounded_to_conf_precision(self):
        # ``.conf`` пишет 4 знака (``Zone.fmt``), JSON не мельче.
        zc.save_profile("third", [zc.Zone("third", 0, 0, 1 / 3, 1)], conf_path=self.conf)
        data = zc.profile_to_dict("third", self.conf)
        self.assertEqual(data["zones"]["third"], [0, 0, 0.3333, 1])

    def test_no_monitors_key_for_common_set_only(self):
        zc.save_profile("plain", ZONES, conf_path=self.conf)
        data = zc.profile_to_dict("plain", self.conf)
        self.assertEqual(
            sorted(data),
            ["gridsnap_profile", "ignore", "keys", "name", "rules", "zones"],
        )
        self.assertEqual(data["rules"], [])
        self.assertEqual(data["ignore"], [])
        self.assertEqual(data["keys"], {"side": "<Super><Alt>2"})
        self.assertNotIn("monitors", data)

    def test_monitor_only_profile_has_empty_zones(self):
        zc.save_profile("monitors", [], None, self.conf, monitor_sets={"HDMI-1": HDMI_ZONES})
        data = zc.profile_to_dict("monitors", self.conf)
        self.assertEqual(data["zones"], {})
        self.assertEqual(list(data["monitors"]), ["HDMI-1"])

    def test_missing_profile_raises(self):
        with self.assertRaises(FileNotFoundError):
            zc.profile_to_dict("nope", self.conf)


class TestRoundTrip(ConfMixin, unittest.TestCase):
    """Круг профиль → dict → файл → профиль ничего не теряет."""

    def test_full_profile_survives_round_trip(self):
        zc.save_profile("cinema", ZONES, RULES, self.conf,
                        monitor_sets={"HDMI-1": HDMI_ZONES})
        data = zc.profile_to_dict("cinema", self.conf)
        path = zc.profile_from_dict(data, conf_path=self.conf)
        self.assertEqual(path, self.profile_path("cinema"))
        zones, rules, monitor_sets, ignore = zc.read_profile_sets("cinema", self.conf)
        self.assertEqual([(z.name, z.as_tuple(), z.binding) for z in zones],
                         [("main", (0.0, 0.0, 0.5, 1.0), ""),
                          ("side", (0.5, 0.0, 0.5, 1.0), "<Super><Alt>2")])
        self.assertEqual(rules, RULES)
        self.assertEqual(ignore, [])
        # Порядок зон в наборе после JSON — по алфавиту (sort_keys), имена те же.
        self.assertEqual(sorted(z.name for z in monitor_sets["HDMI-1"]), ["bottom", "top"])
        self.assertEqual(zc.profile_to_dict("cinema", self.conf), data)

    def test_second_round_trip_is_byte_identical(self):
        zc.save_profile("cinema", ZONES, RULES, self.conf,
                        monitor_sets={"HDMI-1": HDMI_ZONES})
        zc.profile_from_dict(zc.profile_to_dict("cinema", self.conf), conf_path=self.conf)
        once = self.profile_text("cinema")
        zc.profile_from_dict(zc.profile_to_dict("cinema", self.conf), conf_path=self.conf)
        self.assertEqual(self.profile_text("cinema"), once)

    def test_common_set_only_round_trip_has_no_monitor_sections(self):
        zc.save_profile("plain", ZONES, RULES, self.conf)
        data = zc.profile_to_dict("plain", self.conf)
        zc.profile_from_dict(data, conf_path=self.conf)
        self.assertEqual(zc.list_zone_sets(self.profile_path("plain")),
                         {None: ["main", "side"]})
        self.assertEqual([line for line in self.profile_text("plain").splitlines()
                          if line.startswith("[zones:")], [])

    def test_monitor_only_round_trip(self):
        zc.save_profile("monitors", [], None, self.conf, monitor_sets={"HDMI-1": HDMI_ZONES})
        data = zc.profile_to_dict("monitors", self.conf)
        zc.profile_from_dict(data, conf_path=self.conf)
        self.assertEqual(zc.list_zone_sets(self.profile_path("monitors")),
                         {"HDMI-1": ["top", "bottom"]})
        self.assertEqual(zc.read_profile("monitors", self.conf), ([], [], []))

    def test_ignore_survives_json_round_trip(self):
        zc.save_profile("cinema", ZONES, RULES, self.conf, ignore=IGNORE)
        data = zc.profile_to_dict("cinema", self.conf)
        self.assertEqual(data["ignore"], IGNORE)
        zc.profile_from_dict(data, conf_path=self.conf)
        self.assertEqual(zc.read_profile("cinema", self.conf)[2], IGNORE)

    def test_import_under_new_name(self):
        path = zc.profile_from_dict(GOOD, "  My Cinema ", self.conf)
        self.assertEqual(path, self.profile_path("My-Cinema"))
        self.assertEqual(zc.list_profiles(self.conf), ["My-Cinema"])
        self.assertIn("My-Cinema", self.profile_text("My-Cinema").splitlines()[0])

    def test_import_uses_name_from_json(self):
        zc.profile_from_dict(GOOD, conf_path=self.conf)
        self.assertEqual(zc.list_profiles(self.conf), ["cinema"])

    def test_keys_to_unknown_zone_are_dropped(self):
        data = dict(GOOD, keys={"main": "<Super>1", "ghost": "<Super>g"})
        zc.profile_from_dict(data, conf_path=self.conf)
        self.assertEqual(zc.profile_to_dict("cinema", self.conf)["keys"],
                         {"main": "<Super>1"})


class TestProfileFromDictErrors(ConfMixin, unittest.TestCase):
    def assertJsonError(self, data, **kwargs):
        with self.assertRaises(zc.ProfileJsonError) as caught:
            zc.profile_from_dict(data, conf_path=self.conf, **kwargs)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(zc.list_profiles(self.conf), [])  # ничего не записано
        return str(caught.exception)

    def test_missing_version(self):
        data = {k: v for k, v in GOOD.items() if k != "gridsnap_profile"}
        self.assertIn("gridsnap_profile", self.assertJsonError(data))

    def test_unknown_version(self):
        self.assertIn("1", self.assertJsonError(dict(GOOD, gridsnap_profile=2)))
        self.assertJsonError(dict(GOOD, gridsnap_profile="1"))
        self.assertJsonError(dict(GOOD, gridsnap_profile=True))

    def test_zone_out_of_range(self):
        self.assertJsonError(dict(GOOD, zones={"main": [0, 0, 1.5, 1]}))
        self.assertJsonError(dict(GOOD, zones={"main": [0.5, 0, 0.8, 1]}))
        self.assertJsonError(dict(GOOD, zones={"main": [-0.1, 0, 0.5, 1]}))

    def test_zero_or_negative_size(self):
        self.assertJsonError(dict(GOOD, zones={"main": [0, 0, 0, 1]}))
        self.assertJsonError(dict(GOOD, zones={"main": [0, 0, 0.5, -1]}))

    def test_broken_zone_shape(self):
        self.assertJsonError(dict(GOOD, zones={"main": [0, 0, 1]}))
        self.assertJsonError(dict(GOOD, zones={"main": "0,0,1,1"}))
        self.assertJsonError(dict(GOOD, zones={"main": [0, 0, "0.5", 1]}))
        self.assertJsonError(dict(GOOD, zones={"": [0, 0, 1, 1]}))
        self.assertJsonError(dict(GOOD, zones=[0, 0, 1, 1]))

    def test_monitor_zone_out_of_range(self):
        self.assertJsonError(dict(GOOD, monitors={"HDMI-1": {"top": [0, 0, 1, 2]}}))

    def test_rule_to_unknown_zone(self):
        text = self.assertJsonError(dict(GOOD, rules=["Chrome = nope"]))
        self.assertIn("nope", text)
        # зона из набора монитора — существует
        zc.profile_from_dict(dict(GOOD, rules=["Chrome = top"]), conf_path=self.conf)

    def test_rule_without_equals(self):
        self.assertIn("селектор", self.assertJsonError(dict(GOOD, rules=["Chrome main"])))

    def test_rules_must_be_list(self):
        self.assertJsonError(dict(GOOD, rules="Chrome = main"))

    def test_empty_profile(self):
        self.assertJsonError(dict(GOOD, zones={}))
        self.assertJsonError(dict(GOOD, zones={}, monitors={"HDMI-1": {}}))
        self.assertJsonError(dict(GOOD, zones=None, monitors=None))

    def test_keys_must_be_object(self):
        self.assertJsonError(dict(GOOD, keys=["main"]))

    def test_not_an_object(self):
        self.assertJsonError(["cinema"])

    def test_bad_name_is_usage_error(self):
        data = dict(GOOD)
        data.pop("name")
        with self.assertRaises(ValueError) as caught:
            zc.profile_from_dict(data, conf_path=self.conf)
        self.assertNotIsInstance(caught.exception, zc.ProfileJsonError)


class TestJsonText(ConfMixin, unittest.TestCase):
    def test_dumps_is_sorted_indented_and_utf8(self):
        data = {"zones": {"зона": [0, 0, 1, 1]}, "gridsnap_profile": 1}
        text = zc.dumps_profile(data)
        self.assertEqual(text, sorted_json(data))
        self.assertTrue(text.endswith("\n"))
        self.assertIn('  "gridsnap_profile": 1,', text)
        self.assertIn("зона", text)  # ensure_ascii=False
        self.assertNotIn("\\u", text)

    def test_key_order_does_not_matter(self):
        first = zc.dumps_profile({"gridsnap_profile": 1, "name": "a", "zones": {}})
        second = zc.dumps_profile({"zones": {}, "name": "a", "gridsnap_profile": 1})
        self.assertEqual(first, second)

    def test_export_text_does_not_drift(self):
        zc.save_profile("cinema", ZONES, RULES, self.conf)
        one = zc.dumps_profile(zc.profile_to_dict("cinema", self.conf))
        two = zc.dumps_profile(zc.profile_to_dict("cinema", self.conf))
        self.assertEqual(one, two)

    def test_write_and_read_file(self):
        path = os.path.join(self.dir, "out", "cinema.json")
        zc.write_profile_json(GOOD, path)
        self.assertEqual(zc.read_profile_json(path), GOOD)

    def test_read_broken_json(self):
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        with self.assertRaises(zc.ProfileJsonError):
            zc.read_profile_json(path)
        with self.assertRaises(zc.ProfileJsonError):
            zc.read_profile_json(os.path.join(self.dir, "nope.json"))

    def test_zone_total_counts_all_sets(self):
        self.assertEqual(zc.profile_zone_total(GOOD), 3)
        self.assertEqual(zc.profile_zone_total({"zones": {}}), 0)


class TestCliJson(ConfMixin, unittest.TestCase):
    def cli(self, *args):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "zone-tiler.py")] + list(args),
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
        )

    def make_profile(self, name="cinema"):
        return zc.save_profile(name, ZONES, RULES, self.conf,
                               monitor_sets={"HDMI-1": HDMI_ZONES})

    def test_export_prints_json_to_stdout(self):
        self.make_profile()
        r = self.cli("profile", "export", "cinema", "--conf", self.conf)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data, zc.profile_to_dict("cinema", self.conf))
        self.assertEqual(data["gridsnap_profile"], 1)

    def test_export_with_json_flag_is_stdout(self):
        self.make_profile()
        plain = self.cli("profile", "export", "cinema", "--conf", self.conf)
        flagged = self.cli("profile", "export", "cinema", "--json", "--conf", self.conf)
        self.assertEqual(flagged.returncode, 0, flagged.stderr)
        self.assertEqual(flagged.stdout, plain.stdout)

    def test_export_to_file(self):
        self.make_profile()
        target = os.path.join(self.dir, "cinema.json")
        r = self.cli("profile", "export", "cinema", target, "--conf", self.conf)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(target, r.stdout)
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, zc.dumps_profile(zc.profile_to_dict("cinema", self.conf)))

    def test_export_json_and_file_conflict(self):
        self.make_profile()
        r = self.cli("profile", "export", "cinema", os.path.join(self.dir, "x.json"),
                     "--json", "--conf", self.conf)
        self.assertEqual(r.returncode, 2)

    def test_export_without_name_is_usage_error(self):
        self.assertEqual(self.cli("profile", "export", "--conf", self.conf).returncode, 2)

    def test_export_missing_profile_is_config_error(self):
        r = self.cli("profile", "export", "nope", "--conf", self.conf)
        self.assertEqual(r.returncode, 3)

    def test_import_and_rename(self):
        self.make_profile()
        exported = os.path.join(self.dir, "cinema.json")
        self.cli("profile", "export", "cinema", exported, "--conf", self.conf)
        self.cli("profile", "rm", "cinema", "--conf", self.conf)
        r = self.cli("profile", "import", exported, "--name", "home", "--conf", self.conf)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.profile_path("home"), r.stdout)
        self.assertIn("4 зон", r.stdout)
        self.assertEqual(self.cli("profile", "list", "--conf", self.conf).stdout.split(),
                         ["home"])
        self.assertEqual(
            zc.list_zone_sets(self.profile_path("home")),
            {None: ["main", "side"], "HDMI-1": ["bottom", "top"]},
        )

    def test_import_without_name_uses_json(self):
        exported = os.path.join(self.dir, "cinema.json")
        zc.write_profile_json(GOOD, exported)
        r = self.cli("profile", "import", exported, "--conf", self.conf)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(zc.list_profiles(self.conf), ["cinema"])

    def test_import_bad_version_is_config_error(self):
        path = os.path.join(self.dir, "v2.json")
        zc.write_profile_json(dict(GOOD, gridsnap_profile=2), path)
        r = self.cli("profile", "import", path, "--conf", self.conf)
        self.assertEqual(r.returncode, 3)
        self.assertIn("gridsnap_profile", r.stderr)

    def test_import_broken_json_is_config_error(self):
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        self.assertEqual(self.cli("profile", "import", path, "--conf", self.conf).returncode, 3)

    def test_existing_actions_still_work(self):
        self.make_profile()
        self.assertEqual(self.cli("profile", "show", "cinema", "--conf", self.conf).returncode, 0)
        r = self.cli("profile", "use", "cinema", "--no-hotkeys", "--conf", self.conf)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("применён", r.stdout)
        self.assertIn("2 зон", r.stdout)
        self.assertEqual(self.cli("profile", "rm", "cinema", "--conf", self.conf).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

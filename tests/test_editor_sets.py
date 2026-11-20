#!/usr/bin/env python3
"""Юнит-тесты наборов зон редактора — без X, GDK и GTK.

Покрыто: какой набор показать для монитора (`set_for_monitor`: свой из секции
или копия общего `[zones]`), текст статуса (`set_origin_text`), отпечаток набора
(`zone_set_signature`) и правило записи секций `[zones:<выход>]`
(`save_plan`): отличается от общего или монитор правили — пишем, совпал и не
правили — секция удаляется только у монитора на экране, отключённый выход не
трогаем. Сквозные проверки (`apply_save_plan`) идут через настоящий
`zone_config.save_zones`: чужие секции (`[rules]`, `[daemon]`), наборы соседних
и отключённых мониторов и `[keys]` без дублей.

Запуск:
    python3 -m unittest discover -s tests -v
    python3 tests/test_editor_sets.py
"""

import configparser
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402  # isort: skip
import zone_geom as geom  # noqa: E402  # isort: skip


def zone(name, rect=(0, 0, 1, 1), binding=""):
    x, y, w, h = rect
    return zc.Zone(name, x, y, w, h, binding)


BASE = "[grid]\ncols = 1.0\nrows = 1.0\n"

# Файл «как после ручной правки»: общий набор, свой набор монитора, копия
# общего в секции второго монитора (лишняя — её и надо убрать), набор
# отключённого выхода (остаться) и чужие секции.
CONF = (
    BASE
    + "[zones]\nmain = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    + "[zones:HDMI-1]\nmain = 0, 0, 1, 0.5\nbottom = 0, 0.5, 1, 0.5\n"
    + "[zones:DP-1]\nmain = 0, 0, 0.5, 1\nright = 0.5, 0, 0.5, 1\n"
    + "[zones:VGA-1]\nold = 0, 0, 1, 1\n"
    + "[rules]\nChrome = main\nre:^VLC = old\n"
    + "[keys]\nmain = <Super><Alt>1\nright = <Super><Alt>2\nold = <Super><Alt>9\n"
    + "[daemon]\nreflow = off   # off | neighbors\n"
)


class ConfCase(unittest.TestCase):
    """Временный каталог с `zones.conf`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-editor-sets-")
        self.conf = os.path.join(self.dir, "zones.conf")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)
        return self.conf

    def read(self):
        with open(self.conf, encoding="utf-8") as fh:
            return fh.read()


# ----------------------------------------------------------- какой набор показать


class TestSetForMonitor(unittest.TestCase):
    def setUp(self):
        self.default = [zone("main", (0, 0, 0.5, 1), "<Super><Alt>1"),
                        zone("right", (0.5, 0, 0.5, 1), "<Super><Alt>2")]
        self.mine = [zone("wide", (0, 0, 1, 1))]
        self.sets = {"HDMI-1": self.mine}

    def test_own_section_is_shown_as_own(self):
        zones, own = geom.set_for_monitor("HDMI-1", self.sets, self.default)
        self.assertTrue(own)
        self.assertEqual([z.name for z in zones], ["wide"])

    def test_own_section_is_a_copy(self):
        zones, _own = geom.set_for_monitor("HDMI-1", self.sets, self.default)
        zones[0].x = 0.7
        zones.append(zone("extra"))
        self.assertEqual(self.mine[0].x, 0.0)
        self.assertEqual(len(self.mine), 1)

    def test_monitor_without_section_inherits_default_copy(self):
        zones, own = geom.set_for_monitor("eDP-1", self.sets, self.default)
        self.assertFalse(own)
        self.assertEqual([z.name for z in zones], ["main", "right"])
        self.assertEqual(zones[0].binding, "<Super><Alt>1")

    def test_inherited_set_edit_does_not_touch_default(self):
        zones, _own = geom.set_for_monitor("eDP-1", self.sets, self.default)
        zones[0].w = 0.25
        self.assertEqual(self.default[0].w, 0.5)

    def test_empty_section_counts_as_inheritance(self):
        zones, own = geom.set_for_monitor("DP-1", {"DP-1": []}, self.default)
        self.assertFalse(own)
        self.assertEqual([z.name for z in zones], ["main", "right"])


class TestSetOriginText(unittest.TestCase):
    def test_shared_set(self):
        text = geom.set_origin_text("eDP-1", False, shared=True)
        self.assertIn("общий набор [zones]", text)

    def test_own_set_names_section(self):
        self.assertEqual(geom.set_origin_text("HDMI-1", True), "свой набор [zones:HDMI-1]")

    def test_inherited_set_points_to_default(self):
        self.assertEqual(geom.set_origin_text("DP-1", False), "наследует [zones]")

    def test_offline_monitor_is_marked(self):
        text = geom.set_origin_text("VGA-1", True, offline=True)
        self.assertIn("свой набор [zones:VGA-1]", text)
        self.assertIn("нет на экране", text)


class TestZoneSetSignature(unittest.TestCase):
    def test_same_zones_same_signature(self):
        a = [zone("main", (0, 0, 0.8358, 1))]
        b = [zone("main", (0, 0, 0.83580, 1.0))]
        self.assertEqual(geom.zone_set_signature(a), geom.zone_set_signature(b))

    def test_bindings_are_not_part_of_signature(self):
        # [keys] общая на файл: другая клавиша у той же зоны — тот же набор.
        a = [zone("main", (0, 0, 1, 1), "<Super><Alt>1")]
        b = [zone("main", (0, 0, 1, 1), "<Super><Alt>7")]
        self.assertEqual(geom.zone_set_signature(a), geom.zone_set_signature(b))

    def test_geometry_and_order_matter(self):
        a = [zone("main", (0, 0, 1, 1)), zone("right", (0.5, 0, 0.5, 1))]
        self.assertNotEqual(
            geom.zone_set_signature(a),
            geom.zone_set_signature([zone("main", (0, 0, 0.9, 1))]),
        )
        self.assertNotEqual(
            geom.zone_set_signature(a), geom.zone_set_signature(list(reversed(a)))
        )


# ----------------------------------------------- [keys] без повторов


class TestDedupeKeyLines(unittest.TestCase):
    def test_second_entry_for_same_zone_is_dropped(self):
        text = BASE + "[zones]\nmain = 0, 0, 1, 1\n[keys]\nmain = a\nmain = b\nright = c\n"
        fixed = geom.dedupe_key_lines(text)
        keys = fixed.split("[keys]", 1)[1]
        self.assertEqual(keys.count("main ="), 1)
        self.assertIn("main = a\n", keys)      # первая строка главнее
        self.assertIn("right = c\n", keys)

    def test_other_sections_and_comments_survive(self):
        text = (BASE + "# комментарий\n[zones]\nmain = 0, 0, 1, 1\n"
                "[keys]\nmain = a\n[daemon]\nmain = same name, other section\n")
        self.assertEqual(geom.dedupe_key_lines(text), text)




class TestSavePlan(unittest.TestCase):
    def setUp(self):
        self.default = [zone("main", (0, 0, 1, 1), "<Super><Alt>1")]
        self.other = [zone("wide", (0, 0, 0.5, 1), "<Super><Alt>2")]

    def plan(self, sets, edited=(), onscreen=("HDMI-1",), existing=("HDMI-1",)):
        return geom.save_plan(self.default, sets, edited, onscreen=onscreen, existing=existing)

    def test_differing_set_is_written(self):
        write, drop = self.plan({"HDMI-1": self.other})
        self.assertEqual(list(write), ["HDMI-1"])
        self.assertEqual([z.name for z in write["HDMI-1"]], ["wide"])
        self.assertEqual(drop, [])

    def test_equal_set_is_not_written(self):
        write, drop = self.plan({"HDMI-1": [zone("main", (0, 0, 1, 1))]}, existing=())
        self.assertEqual(write, {})
        self.assertEqual(drop, [])

    def test_equal_set_with_stale_section_is_dropped(self):
        write, drop = self.plan({"DP-1": [zone("main", (0, 0, 1, 1))]}, onscreen=("DP-1",),
                                existing=("DP-1",))
        self.assertEqual(write, {})
        self.assertEqual(drop, ["DP-1"])

    def test_equal_set_not_dropped_when_section_absent(self):
        write, drop = self.plan({"DP-1": [zone("main", (0, 0, 1, 1))]}, onscreen=("DP-1",),
                                existing=())
        self.assertEqual((write, drop), ({}, []))

    def test_equal_set_of_offline_monitor_is_left_alone(self):
        write, drop = self.plan(
            {"VGA-1": [zone("main", (0, 0, 1, 1))]},
            onscreen=("HDMI-1",), existing=("VGA-1",),
        )
        self.assertEqual((write, drop), ({}, []))

    def test_edited_set_is_written_even_if_equal(self):
        write, drop = self.plan(
            {"HDMI-1": [zone("main", (0, 0, 1, 1))]}, edited={"HDMI-1"}
        )
        self.assertEqual(list(write), ["HDMI-1"])
        self.assertEqual(drop, [])

    def test_edited_offline_set_is_written(self):
        write, drop = self.plan(
            {"VGA-1": [zone("main", (0, 0, 1, 1))]},
            edited={"VGA-1"}, onscreen=(), existing=("VGA-1",),
        )
        self.assertEqual(list(write), ["VGA-1"])
        self.assertEqual(drop, [])

    def test_empty_set_is_skipped(self):
        self.assertEqual(self.plan({"DP-1": []}), ({}, []))

    def test_plan_does_not_change_zone_sets(self):
        write, _drop = self.plan({"HDMI-1": self.other})
        write["HDMI-1"][0].x = 0.9
        write["HDMI-1"].append(zone("extra"))
        self.assertEqual(self.other[0].x, 0.0)
        self.assertEqual(len(self.other), 1)

    def test_binding_of_shared_name_is_not_duplicated(self):
        # [keys] одна на файл: у зоны с именем из общего набора привязка не
        # повторяется в секции монитора, у своей зоны — остаётся.
        monitor = [zone("main", (0, 0, 1, 1), "<Super><Alt>5"), zone("wide", (0, 0, 1, 1),
                                                                   "<Super><Alt>6")]
        write, _drop = self.plan({"HDMI-1": monitor})
        self.assertEqual(write["HDMI-1"][0].binding, "")
        self.assertEqual(write["HDMI-1"][1].binding, "<Super><Alt>6")
        self.assertEqual(monitor[0].binding, "<Super><Alt>5")

    def test_binding_of_name_shared_between_monitors_is_written_once(self):
        one = [zone("wide", (0, 0, 1, 1), "<Super><Alt>6")]
        two = [zone("wide", (0, 0, 0.5, 1), "<Super><Alt>6")]
        write, _drop = self.plan({"HDMI-1": one, "DP-1": two}, onscreen=("HDMI-1", "DP-1"),
                                 existing=())
        self.assertEqual(write["HDMI-1"][0].binding, "<Super><Alt>6")
        self.assertEqual(write["DP-1"][0].binding, "")


# ------------------------------------------------- запись файла по плану


class TestApplySavePlan(ConfCase):
    def setUp(self):
        super().setUp()
        self.write(CONF)
        self.extra = zc.load_extra_sections(self.conf)
        loaded = zc.load_zone_sets(self.conf)
        self.default = loaded[None]
        self.sets = {key: value for key, value in loaded.items() if key is not None}

    def save(self, sets, edited=(), onscreen=("HDMI-1", "DP-1"), existing=None):
        existing = set(self.sets) if existing is None else existing
        plan = geom.save_plan(self.default, sets, edited, onscreen=onscreen, existing=existing)
        geom.apply_save_plan(self.conf, self.default, plan, self.extra)
        return plan

    def sections(self):
        cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        cp.read(self.conf, encoding="utf-8")
        return cp

    def test_differing_and_offline_sections_written_stale_copy_dropped(self):
        self.save(self.sets)
        sections = self.sections()
        self.assertIn("zones", sections)
        self.assertIn("zones:HDMI-1", sections)
        self.assertIn("zones:VGA-1", sections)      # отключённый выход остался
        self.assertNotIn("zones:DP-1", sections)    # копия общего набора убрана
        self.assertEqual(
            [z.name for z in zc.load_zone_sets(self.conf)[None]], ["main", "right"]
        )

    def test_other_sections_survive(self):
        self.save(self.sets)
        text = self.read()
        self.assertIn("[rules]\nChrome = main\nre:^VLC = old\n", text)
        self.assertIn("[daemon]\nreflow = off   # off | neighbors\n", text)
        self.assertEqual(self.extra, zc.load_extra_sections(self.conf))

    def test_monitor_sets_reload_as_before(self):
        self.save(self.sets)
        sets = zc.load_zone_sets(self.conf)
        self.assertEqual([z.name for z in sets["HDMI-1"]], ["main", "bottom"])
        self.assertEqual([z.name for z in sets["VGA-1"]], ["old"])
        self.assertEqual(sets["HDMI-1"][0].binding, "<Super><Alt>1")

    def test_keys_have_one_entry_per_zone(self):
        self.save(self.sets)
        section = None
        names = []
        for line in self.read().splitlines():
            if line.startswith("["):
                section = line.strip("[]")
            elif section == "keys" and "=" in line:
                names.append(line.split("=")[0].strip())
        self.assertEqual(names.count("main"), 1, names)
        self.assertEqual(names.count("old"), 1, names)
        self.assertEqual(names.count("right"), 1, names)
        self.assertIn("old", names)

    def test_edited_offline_monitor_is_saved_and_kept(self):
        offline = [zone("old", (0, 0, 0.5, 1), "<Super><Alt>9"), zone("new", (0.5, 0, 0.5, 1))]
        sets = dict(self.sets)
        sets["VGA-1"] = offline
        plan = self.save(sets, edited={"VGA-1"}, onscreen=("HDMI-1", "DP-1"))
        self.assertIn("VGA-1", plan[0])
        reloaded = zc.load_zone_sets(self.conf)
        self.assertEqual([z.name for z in reloaded["VGA-1"]], ["old", "new"])
        self.assertEqual(reloaded["VGA-1"][0].binding, "<Super><Alt>9")

    def test_untouched_offline_section_is_byte_identical(self):
        before = [z.as_tuple() for z in self.sets["VGA-1"]]
        self.save(self.sets)
        self.assertIn("[zones:VGA-1]\nold = 0, 0, 1, 1\n", self.read())
        self.assertEqual([z.as_tuple() for z in zc.load_zone_sets(self.conf)["VGA-1"]], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

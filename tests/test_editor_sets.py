#!/usr/bin/env python3
"""Unit tests for the editor zone sets - without X, GDK and GTK.

Covered: which set to show for a monitor (`set_for_monitor`: its own from the section
or a copy of the common `[zones]`), the status text (`set_origin_text`), the set signature
(`zone_set_signature`) and the rule for writing the `[zones:<output>]` sections
(`save_plan`): it differs from the common one or the monitor was edited - we write it;
it matched and was not edited - the section is deleted only for the monitor on screen,
an output that is off is left alone. End-to-end checks (`apply_save_plan`) go through the real
`zone_config.save_zone_sets`: foreign sections (`[rules]`, `[daemon]`), the sets of neighbouring
and switched-off monitors, and `[keys]` without duplicates (a repeated name in two sets
does not give a duplicate in the section - that is the writer's job, not a text scrub).

Separately, the "Monitor profiles" dialog: the rows of `zone_config.monitor_dialog_rows`
(the order of the outputs, the mark of a missing one, "default set") and the writing of `[monitors]`
by these rows through `zone_config.save_monitor_map`. The pure function has moved to
`zone_config.py` (like its counterpart for workspaces), so these tests run without GTK -
in CI, where `python3-gi` is missing, they are no longer skipped.

Run:
    python3 -m unittest discover -s tests -v
    python3 tests/test_editor_sets.py
"""

import configparser
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

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

# The file "as after a manual edit": the default set, a monitor's own set, a copy of
# the default in the section of the second monitor (a surplus one - it is the one to remove), the set
# of an output that is off (to stay) and foreign sections.
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
    """A temporary directory with `zones.conf`."""

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


# ---------------------------------------------------- which set to show


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
        self.assertIn("default [zones] set", text)

    def test_own_set_names_section(self):
        self.assertEqual(geom.set_origin_text("HDMI-1", True), "own [zones:HDMI-1] set")

    def test_inherited_set_points_to_default(self):
        self.assertEqual(geom.set_origin_text("DP-1", False), "inherits [zones]")

    def test_offline_monitor_is_marked(self):
        text = geom.set_origin_text("VGA-1", True, offline=True)
        self.assertIn("own [zones:VGA-1] set", text)
        self.assertIn("not on screen", text)


class TestZoneSetSignature(unittest.TestCase):
    def test_same_zones_same_signature(self):
        a = [zone("main", (0, 0, 0.8358, 1))]
        b = [zone("main", (0, 0, 0.83580, 1.0))]
        self.assertEqual(geom.zone_set_signature(a), geom.zone_set_signature(b))

    def test_bindings_are_not_part_of_signature(self):
        # [keys] is common to the file: a different key for the same zone - the same set.
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


# ---------------------------------------------- [keys] without duplicates


def keys_body(text):
    """The body of the `[keys]` section line by line - without the hint comment headers."""
    body, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped == "[keys]"
            continue
        if inside:
            body.append(line)
    return [line for line in body if line.strip()]


class TestKeysWithoutDuplicates(ConfCase):
    """A repeated name in two sets does not give a duplicate in `[keys]`.

    The scrub lives in the writer itself (`zone_config`), not in the text of the file after
    the write: `[keys]` is collected without duplicates, so `ConfigParser` reads the file.
    """

    def test_same_name_in_two_sets_yields_one_key_line(self):
        default = [zone("main", (0, 0, 0.5, 1), "<Super><Alt>1")]
        monitors = {"HDMI-1": [zone("main", (0, 0, 1, 1), "<Super><Alt>1"),
                               zone("wide", (0, 0, 1, 0.5), "<Super><Alt>2")]}
        zc.save_zone_sets(self.conf, default, monitors)
        keys = keys_body(self.read())
        self.assertEqual(sum(1 for line in keys if line.startswith("main =")), 1, keys)
        self.assertIn("wide = <Super><Alt>2", keys)
        # the key comes from the default set - it is written first
        self.assertIn("main = <Super><Alt>1", keys)

    def test_broken_keys_file_reads_after_rewrite(self):
        self.write(
            BASE
            + "[zones]\nmain = 0, 0, 1, 1\n"
            + "[keys]\nmain = <Super><Alt>1\nmain = <Super><Alt>9\n"
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as caught:
            zc.load_zones(self.conf)  # a file broken by hand - code 3, not a traceback
        self.assertEqual(caught.exception.code, zc.zt.EXIT_CONFIG)
        zc.save_zone_sets(
            self.conf,
            [zone("main", (0, 0, 1, 1), "<Super><Alt>1")],
            {"HDMI-1": [zone("main", (0, 0, 1, 1), "<Super><Alt>1")]},
        )
        self.assertEqual([z.binding for z in zc.load_zones(self.conf)], ["<Super><Alt>1"])
        self.assertEqual(keys_body(self.read()), ["main = <Super><Alt>1"])

    def test_one_call_writes_all_sets_and_keeps_third(self):
        self.write(CONF)
        extra = zc.load_extra_sections(self.conf)
        zc.save_zone_sets(
            self.conf,
            [zone("main", (0, 0, 0.5, 1), "<Super><Alt>1"),
             zone("right", (0.5, 0, 0.5, 1), "<Super><Alt>2")],
            {
                "HDMI-1": [zone("main", (0, 0, 1, 0.5)), zone("bottom", (0, 0.5, 1, 0.5))],
                "VGA-1": [zone("old", (0, 0, 1, 1), "<Super><Alt>9")],
            },
            extra,
        )
        self.assertEqual(
            zc.list_zone_sets(self.conf),
            {
                None: ["main", "right"],
                "HDMI-1": ["main", "bottom"],
                "VGA-1": ["old"],
            },
        )
        self.assertEqual(self.read().count("[zones:VGA-1]"), 1)
        self.assertEqual(keys_body(self.read()),
                         ["main = <Super><Alt>1", "right = <Super><Alt>2",
                          "old = <Super><Alt>9"])




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
        # [keys] is one per file: for a zone with a name from the default set the binding is not
        # repeated in the monitor section, for its own zone it stays.
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


# -------------------------------------------- writing the file by plan


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
        self.assertIn("zones:VGA-1", sections)      # the switched-off output stayed
        self.assertNotIn("zones:DP-1", sections)    # the copy of the default set was removed
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


class WorkspaceDialogRowsTest(unittest.TestCase):
    """The rows of the "Workspace profiles" dialog - a pure function without GTK.

    The dialog shows as many rows as there are workspaces in Cinnamon, and does not lose
    a workspace record even if the profile is not on disk: it is visible in the list, and it
    stays in the config until saving.
    """

    def test_row_per_desktop(self):
        rows = zc.workspace_dialog_rows({}, ["work", "cinema"], 3)
        self.assertEqual([row[0] for row in rows], [1, 2, 3])

    def test_unread_desktops_falls_back_to_four(self):
        for value in (None, 0, -2, "нет"):
            rows = zc.workspace_dialog_rows({}, [], value)
            self.assertEqual(len(rows), zc.WORKSPACE_DESKTOPS_DEFAULT, value)
            self.assertEqual(len(rows), 4, value)

    def test_unmapped_desk_is_shared_set(self):
        number, profile, options, warning = zc.workspace_dialog_rows({}, ["work"], 1)[0]
        self.assertEqual(number, 1)
        self.assertIsNone(profile)
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "work"])
        self.assertEqual(warning, "")

    def test_mapped_desk_selects_its_profile(self):
        number, profile, options, warning = zc.workspace_dialog_rows(
            {2: "cinema"}, ["cinema", "work"], 2
        )[1]
        self.assertEqual((number, profile, warning), (2, "cinema", ""))
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "cinema", "work"])

    def test_missing_profile_warns_but_keeps_the_mapping(self):
        number, profile, options, warning = zc.workspace_dialog_rows(
            {2: "cinema"}, ["work"], 2
        )[1]
        self.assertEqual(profile, "cinema")
        self.assertIn("cinema", options)
        self.assertIn("cinema", warning)
        self.assertIn("is not on disk", warning)

    def test_mapping_beyond_desktops_is_shown(self):
        # Otherwise saving would silently lose the mapping of workspace 5.
        rows = zc.workspace_dialog_rows({5: "cinema"}, ["cinema"], 2)
        self.assertEqual([row[0] for row in rows], [1, 2, 5])

    def test_profile_names_from_disk_are_sorted_and_unique(self):
        options = zc.workspace_dialog_rows({}, ["work", "cinema", "work"], 1)[0][2]
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "cinema", "work"])


# ------------------------------------- "Monitor profiles": the dialog rows


class MonitorDialogRowsTest(unittest.TestCase):
    """The rows of the "Monitor profiles" dialog - a pure function without windows.

    The order of the rows matters: the profile of the first screen settles a dispute when merging
    (`profile use-monitors`), so the rows follow the order of `xrandr --listmonitors`,
    and the records of missing outputs come after them, in the order of the file. A profile name from
    `[monitors]` stays in the list even if the profile file is missing: otherwise the record
    could be neither corrected nor removed.
    """

    def rows(self, mapping, profiles=(), outputs=()):
        return zc.monitor_dialog_rows(mapping, list(profiles), list(outputs))

    def test_shared_label_is_the_same_as_for_desktops(self):
        self.assertEqual(zc.MONITOR_SHARED, zc.WORKSPACE_SHARED)

    def test_row_per_screen_output_in_xrandr_order(self):
        rows = self.rows({}, [], ["eDP-1", "HDMI-1"])
        self.assertEqual([row[0] for row in rows], ["eDP-1", "HDMI-1"])
        self.assertTrue(all(row[4] for row in rows))

    def test_screen_order_beats_file_order(self):
        # In the file HDMI-2 comes before HDMI-1, while on screen it is the other way round: xrandr sets the order.
        rows = self.rows(
            {"hdmi-2": "cinema", "hdmi-1": "work"}, ["cinema", "work"],
            ["HDMI-1", "HDMI-2"],
        )
        self.assertEqual([row[0] for row in rows], ["HDMI-1", "HDMI-2"])
        self.assertEqual([row[1] for row in rows], ["work", "cinema"])

    def test_screen_name_is_taken_from_xrandr(self):
        # From the file the key comes in lowercase (`ConfigParser`), on screen it is
        # `HDMI-1`: we show and write the output name, not the key from the file, and do not
        # start a second row for the same screen.
        rows = self.rows({"hdmi-1": "work"}, ["work"], ["HDMI-1"])
        self.assertEqual(len(rows), 1, rows)
        output, profile, _options, warning, on_screen = rows[0]
        self.assertEqual((output, profile, warning, on_screen), ("HDMI-1", "work", "", True))

    def test_unmapped_screen_is_shared_set(self):
        output, profile, options, warning, on_screen = self.rows(
            {}, ["cinema", "work"], ["eDP-1"]
        )[0]
        self.assertEqual(output, "eDP-1")
        self.assertIsNone(profile)
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "cinema", "work"])
        self.assertEqual(warning, "")
        self.assertTrue(on_screen)

    def test_absent_output_is_shown_last_with_a_note(self):
        rows = self.rows({"vga-1": "old"}, ["old"], ["HDMI-1"])
        self.assertEqual([row[0] for row in rows], ["HDMI-1", "vga-1"])
        output, profile, options, warning, on_screen = rows[1]
        self.assertEqual((output, profile), ("vga-1", "old"))
        self.assertFalse(on_screen)
        self.assertIn("vga-1", warning)
        self.assertIn("not on screen", warning)
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "old"])

    def test_absent_outputs_keep_file_order(self):
        rows = self.rows({"vga-1": "old", "dvi-1": "work"}, ["old", "work"], [])
        self.assertEqual([row[0] for row in rows], ["vga-1", "dvi-1"])
        self.assertFalse(any(row[4] for row in rows))

    def test_missing_profile_warns_but_keeps_the_mapping(self):
        output, profile, options, warning, _on = self.rows(
            {"hdmi-1": "cinema"}, ["work"], ["HDMI-1"]
        )[0]
        self.assertEqual(profile, "cinema")
        self.assertIn("cinema", options)
        self.assertIn("cinema", warning)
        self.assertIn("is not on disk", warning)

    def test_absent_output_with_missing_profile_has_both_notes(self):
        warning = self.rows({"vga-1": "cinema"}, [], ["HDMI-1"])[1][3]
        self.assertIn("is not on disk", warning)
        self.assertIn("not on screen", warning)

    def test_profile_names_from_disk_are_sorted_and_unique(self):
        options = self.rows({}, ["work", "cinema", "work"], ["eDP-1"])[0][2]
        self.assertEqual(options, [zc.WORKSPACE_SHARED, "cinema", "work"])

    def test_mapping_from_a_hand_built_dict_is_sanitized(self):
        self.assertEqual(self.rows({"HDMI-1": "my cinema"}, [], ["HDMI-1"])[0][1],
                         "my-cinema")

    def test_no_outputs_and_no_mapping_gives_no_rows(self):
        self.assertEqual(self.rows({}, ["work"], []), [])
        self.assertEqual(self.rows({}, ["work"], [None, ""]), [])


class MonitorMapSaveTest(ConfCase):
    """A choice in the dialog -> `save_monitor_map`: the order of the rows and whole foreign sections."""

    CONF = (
        BASE
        + "[zones]\nmain = 0, 0, 1, 1\n"
        + "[workspaces]\n1 = work\n"
        + "[monitors]\nHDMI-2 = cinema\nhdmi-1 = work\nvga-1 = old\n"
        + "[rules]\nChrome = main\n"
    )

    def monitors_body(self):
        """The rows of the `[monitors]` section in the file - the order in them is what we check."""
        body, inside = [], False
        for line in self.read().splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                inside = stripped == "[monitors]"
                continue
            if inside and stripped:
                body.append(stripped)
        return body

    def save_choices(self, picks, outputs=("HDMI-1", "HDMI-2"), profiles=("work", "cinema", "old")):
        """Walk the same steps as the dialog: rows -> choice -> `save_monitor_map`."""
        rows = zc.monitor_dialog_rows(
            zc.load_monitor_map(self.conf), list(profiles), list(outputs)
        )
        chosen = {}
        for output, profile, _options, _warning, _on_screen in rows:
            text = picks.get(output, profile if profile is not None else zc.MONITOR_SHARED)
            if text != zc.MONITOR_SHARED:
                chosen[output] = text
        zc.save_monitor_map(self.conf, chosen)
        return rows, chosen

    def test_saved_in_screen_order_and_shared_removes_the_binding(self):
        self.write(self.CONF)
        # vga-1 is not "on screen" - its row is visible in the dialog; "default set" removes the record.
        rows, chosen = self.save_choices(
            {"HDMI-1": "cinema", "HDMI-2": "work", "vga-1": zc.WORKSPACE_SHARED}
        )
        self.assertEqual([row[0] for row in rows], ["HDMI-1", "HDMI-2", "vga-1"])
        self.assertEqual(list(chosen), ["HDMI-1", "HDMI-2"])
        self.assertEqual(self.monitors_body(), ["HDMI-1 = cinema", "HDMI-2 = work"])
        self.assertEqual(list(zc.load_monitor_map(self.conf)), ["hdmi-1", "hdmi-2"])
        self.assertEqual(zc.load_monitor_map(self.conf)["hdmi-1"], "cinema")

    def test_absent_output_is_not_lost_when_left_as_is(self):
        # The switched-off output was not touched - its record stays, and not at the start of the section.
        self.write(self.CONF)
        _rows, chosen = self.save_choices({})
        self.assertEqual(list(chosen), ["HDMI-1", "HDMI-2", "vga-1"])
        self.assertEqual(
            self.monitors_body(),
            ["HDMI-1 = work", "HDMI-2 = cinema", "vga-1 = old"],
        )

    def test_all_shared_removes_the_section(self):
        self.write(self.CONF)
        _rows, chosen = self.save_choices(
            {"HDMI-1": zc.WORKSPACE_SHARED, "HDMI-2": zc.WORKSPACE_SHARED,
             "vga-1": zc.WORKSPACE_SHARED}
        )
        self.assertEqual(chosen, {})
        self.assertEqual(zc.load_monitor_map(self.conf), {})
        self.assertNotIn("[monitors]", self.read())

    def test_other_sections_survive_untouched(self):
        self.write(self.CONF)
        self.save_choices({"HDMI-1": "cinema", "HDMI-2": "work"})
        text = self.read()
        self.assertIn("[zones]\nmain = 0, 0, 1, 1\n", text)
        self.assertIn("[workspaces]\n1 = work\n", text)
        self.assertIn("[rules]\nChrome = main\n", text)
        self.assertEqual([z.name for z in zc.load_zones(self.conf)], ["main"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

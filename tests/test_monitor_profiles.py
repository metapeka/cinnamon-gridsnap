#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты привязки профиля к экрану: секция `[monitors]` и разбор «чей профиль».

Без X и GTK: и карта экранов, и резолвер живут в чистом конфиг-слое. Проверяется
главное: экран важнее стола, стол важнее активного конфига; имена выходов
сравниваются без учёта регистра (парсер опускает регистр ключей, как и у
`[zones:<выход>]`); порядок секции сохраняется, потому что профиль **первого**
экрана решает спор при слиянии в `profile use-monitors`; отсутствующий профиль
разбор не ломает — откат на следующий уровень делает вызывающий.

Запуск:
    python3 tests/test_monitor_profiles.py
    python3 -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402

BASE = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\nmain = 0, 0, 0.5, 1\nside = 0.5, 0, 0.5, 1\n"
)


class MonitorMapTest(unittest.TestCase):
    """Секция `[monitors]`: чтение, запись и её границы."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gridsnap-monitors-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "zones.conf")

    def write(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_section_is_empty_map(self):
        self.write(BASE)
        self.assertEqual(zc.load_monitor_map(self.conf), {})
        # Нет файла — тоже пустая карта, а не ошибка: секция необязательная.
        self.assertEqual(zc.load_monitor_map(os.path.join(self.dir, "нет.conf")), {})

    def test_reads_in_file_order_and_lowercases_keys(self):
        # Порядок значим: профиль первого экрана решает спор при слиянии.
        self.write(BASE + "[monitors]\nHDMI-2 = cinema\nHDMI-1 = work\n")
        self.assertEqual(
            zc.load_monitor_map(self.conf), {"hdmi-2": "cinema", "hdmi-1": "work"}
        )

    def test_empty_profile_is_config_error(self):
        self.write(BASE + "[monitors]\nHDMI-1 =\n")
        with self.assertRaises(SystemExit) as ctx:
            zc.load_monitor_map(self.conf)
        self.assertEqual(ctx.exception.code, 3)

    def test_save_and_read_round_trip(self):
        self.write(BASE)
        zc.save_monitor_map(self.conf, {"HDMI-1": "work", "HDMI-2": "cinema"})
        text = open(self.conf, encoding="utf-8").read()
        self.assertIn("[monitors]\nHDMI-1 = work\nHDMI-2 = cinema", text)
        self.assertEqual(
            zc.load_monitor_map(self.conf), {"hdmi-1": "work", "hdmi-2": "cinema"}
        )

    def test_save_keeps_other_sections_and_sanitizes_profile(self):
        self.write(BASE + "[workspaces]\n1 = work\n\n[keys]\nmain = <Super><Alt>1\n")
        zc.save_monitor_map(self.conf, {"HDMI-2": "my cinema"})
        text = open(self.conf, encoding="utf-8").read()
        self.assertIn("[workspaces]\n1 = work", text)
        self.assertIn("[keys]\nmain = <Super><Alt>1", text)
        self.assertIn("HDMI-2 = my-cinema", text)

    def test_save_empty_map_removes_section(self):
        self.write(BASE + "[monitors]\nHDMI-1 = work\n")
        zc.save_monitor_map(self.conf, {})
        self.assertNotIn("[monitors]", open(self.conf, encoding="utf-8").read())

    def test_save_creates_file_with_the_section(self):
        path = os.path.join(self.dir, "new", "zones.conf")
        zc.save_monitor_map(path, {"eDP-1": "work"})
        self.assertEqual(zc.load_monitor_map(path), {"edp-1": "work"})

    def test_save_rejects_empty_names(self):
        for mapping in ({"": "work"}, {"HDMI-1": "  "}):
            with self.assertRaises(ValueError):
                zc.save_monitor_map(self.conf, mapping)


class ResolveProfileTest(unittest.TestCase):
    """Разбор «чей профиль»: экран важнее стола, стол важнее активного конфига."""

    def test_screen_wins_over_desk(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "HDMI-1", 0), "studio"
        )

    def test_desk_is_used_when_no_screen_binding(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "eDP-1", 0), "work"
        )

    def test_desk_code_is_translated_from_ewmh_zero_based(self):
        # В `[workspaces]` номера столов с 1, как в интерфейсе Cinnamon, а в EWMH — с нуля.
        self.assertIsNone(zc.resolve_profile({}, {1: "work"}, "eDP-1", 1))
        self.assertEqual(zc.resolve_profile({}, {2: "cinema"}, "eDP-1", 1), "cinema")

    def test_nothing_bound_means_active_config(self):
        self.assertIsNone(zc.resolve_profile({}, {}, "HDMI-1", 0))
        self.assertIsNone(zc.resolve_profile(None, None, None, None))

    def test_output_name_is_case_insensitive_and_trimmed(self):
        for output in ("hdmi-1", "HDMI-1", "Hdmi-1", " hdmi-1 "):
            self.assertEqual(
                zc.resolve_profile({"hdmi-1": "studio"}, {}, output), "studio"
            )

    def test_string_keys_of_desk_map_are_accepted(self):
        # Диалог редактора и тесты могут собрать словарь со строковыми ключами.
        self.assertEqual(zc.resolve_profile({}, {"2": "cinema"}, None, 1), "cinema")

    def test_unknown_screen_falls_back_to_the_desk(self):
        self.assertEqual(
            zc.resolve_profile({"hdmi-1": "studio"}, {1: "work"}, "DP-3", 0), "work"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

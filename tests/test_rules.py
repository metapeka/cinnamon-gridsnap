#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты правил «класс/заголовок -> зона» в zone-tiler.

Без X-сервера и GTK: `match_rules` — чистая функция, разбор вывода xprop
проверяется через подмену subprocess.check_output, а `Grid.rules` — на временном
zones.conf.

Запуск:
    python3 tests/test_rules.py
"""

import importlib.util
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TILER = os.path.join(ROOT, "zone-tiler.py")


def load_tiler():
    spec = importlib.util.spec_from_file_location("zone_tiler_under_test", TILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()

BASE_GRID = "[grid]\ncols = 1.0\nrows = 1.0\n"
ZONES = "[zones]\nmain = 0, 0, 0.5, 1\ntop-right = 0.5, 0, 0.5, 0.5\n"


class ConfMixin:
    """Пишет временный zones.conf и убирает его после теста."""

    def conf(self, text):
        fd, path = tempfile.mkstemp(suffix=".conf", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path


class TestMatchRules(unittest.TestCase):
    def test_glob_class_case_insensitive(self):
        rules = [("FireFox*", "main")]
        self.assertEqual(zt.match_rules(rules, "firefox-esr", ""), "main")
        self.assertEqual(zt.match_rules(rules, "FIREFOX-ESR", ""), "main")

    def test_glob_does_not_match_title(self):
        rules = [("firefox*", "main")]
        self.assertIsNone(zt.match_rules(rules, "chromium", "firefox docs"))

    def test_regex_matches_class_or_title(self):
        rules = [("re:^(navigator|firefox)", "main")]
        self.assertEqual(zt.match_rules(rules, "Navigator", "любой"), "main")
        rules = [("re:docs\\.google\\.com", "top-right")]
        self.assertEqual(
            zt.match_rules(rules, "chromium", "Docs - docs.google.com"), "top-right"
        )

    def test_regex_case_insensitive(self):
        rules = [("re:firefox", "main")]
        self.assertEqual(zt.match_rules(rules, "FIREFOX", ""), "main")

    def test_first_match_wins(self):
        rules = [("re:firefox", "top-right"), ("*", "main")]
        self.assertEqual(zt.match_rules(rules, "firefox-esr", ""), "top-right")

    def test_falls_through_to_later_rule(self):
        rules = [("chrome", "top-right"), ("firefox*", "main")]
        self.assertEqual(zt.match_rules(rules, "firefox-esr", ""), "main")

    def test_no_match_returns_none(self):
        rules = [("chrome", "main")]
        self.assertIsNone(zt.match_rules(rules, "firefox-esr", "title"))

    def test_empty_rules(self):
        self.assertIsNone(zt.match_rules([], "firefox", "title"))

    def test_invalid_regex_is_skipped(self):
        rules = [("re:[unclosed", "top-right"), ("firefox*", "main")]
        self.assertEqual(zt.match_rules(rules, "firefox-esr", ""), "main")


class TestWindowClassTitle(unittest.TestCase):
    def _out(self, text):
        return mock.patch.object(zt.subprocess, "check_output", return_value=text)

    def test_single_line_xprop(self):
        out = (
            'WM_CLASS(STRING) = "Navigator", "firefox-esr"\n'
            '_NET_WM_NAME(UTF8_STRING) = "ThinkRail — Mozilla Firefox"\n'
        )
        with self._out(out):
            self.assertEqual(
                zt.window_class_title(0x2A00017),
                ("firefox-esr", "ThinkRail — Mozilla Firefox"),
            )

    def test_multiline_xprop(self):
        out = (
            'WM_CLASS(STRING) = "Navigator"\n'
            '"firefox-esr"\n'
            '_NET_WM_NAME(UTF8_STRING) = "Title here"\n'
        )
        with self._out(out):
            self.assertEqual(zt.window_class_title(0x1), ("firefox-esr", "Title here"))

    def test_single_class_string(self):
        out = 'WM_CLASS(STRING) = "solo"\n_NET_WM_NAME(UTF8_STRING) = "T"\n'
        with self._out(out):
            self.assertEqual(zt.window_class_title(0x1), ("solo", "T"))

    def test_wm_name_fallback(self):
        out = 'WM_CLASS(STRING) = "a", "term"\nWM_NAME(COMPOUND_TEXT) = "bash"\n'
        with self._out(out):
            self.assertEqual(zt.window_class_title(0x1), ("term", "bash"))

    def test_xprop_failure_gives_blanks(self):
        with mock.patch.object(
            zt.subprocess, "check_output", side_effect=OSError("no xprop")
        ):
            self.assertEqual(zt.window_class_title(0x1), ("", ""))


class TestGridRules(ConfMixin, unittest.TestCase):
    def test_rules_parsed_in_order(self):
        path = self.conf(
            BASE_GRID
            + ZONES
            + "[rules]\nChrome = main\nre:^(Navigator|firefox) = top-right\n"
        )
        grid = zt.Grid(path)
        self.assertEqual(
            grid.rules,
            [("Chrome", "main"), ("re:^(Navigator|firefox)", "top-right")],
        )

    def test_rules_selector_case_is_preserved(self):
        # ConfigParser по умолчанию приводит ключи к нижнему регистру, из-за чего
        # `re:\S+Deploy\S*` мутирует в `\s+deploy\s*` (\S -> \s), и правило
        # молча не срабатывает. Селектор должен сохраниться дословно.
        selector = r"re:\S+Deploy\S*"
        path = self.conf(BASE_GRID + ZONES + f"[rules]\n{selector} = main\n")
        grid = zt.Grid(path)
        self.assertEqual(grid.rules, [(selector, "main")])

    def test_regex_escape_class_stays_uppercase_and_matches(self):
        # `\S+Deploy\S*` должен матчить не-пробелы вокруг Deploy; если бы регистр
        # упал, `\s+deploy\s*` требовал бы пробелы и не совпал бы.
        path = self.conf(BASE_GRID + ZONES + "[rules]\nre:\\S+Deploy\\S* = main\n")
        grid = zt.Grid(path)
        self.assertEqual(zt.match_rules(grid.rules, "xterm", "webDeployProd"), "main")
        self.assertIsNone(zt.match_rules(grid.rules, "xterm", "  deploy  "))

    def test_glob_selector_case_is_preserved(self):
        path = self.conf(BASE_GRID + ZONES + "[rules]\nFireFox* = main\n")
        grid = zt.Grid(path)
        self.assertEqual(grid.rules, [("FireFox*", "main")])
        self.assertEqual(zt.match_rules(grid.rules, "firefox-esr", ""), "main")

    def test_no_rules_section_gives_empty_list(self):
        path = self.conf(BASE_GRID + ZONES)
        self.assertEqual(zt.Grid(path).rules, [])

    def test_unknown_zone_is_config_error(self):
        path = self.conf(BASE_GRID + ZONES + "[rules]\nChrome = nowhere\n")
        with self.assertRaises(SystemExit) as cm:
            zt.Grid(path)
        self.assertEqual(cm.exception.code, zt.EXIT_CONFIG)

    def test_parsed_rules_are_matchable(self):
        path = self.conf(BASE_GRID + ZONES + "[rules]\nfirefox* = top-right\n")
        grid = zt.Grid(path)
        self.assertEqual(zt.match_rules(grid.rules, "firefox-esr", ""), "top-right")


if __name__ == "__main__":
    unittest.main(verbosity=2)

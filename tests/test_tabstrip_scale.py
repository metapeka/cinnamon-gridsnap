#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты перевода рамки полосы из физических пикселей в логические.

Проверяют `window_rect` из `zone-tabstrip.py`: демон присылает физическую рамку
(координаты окна зоны), а окно GTK живёт в логических пикселях, поэтому при
`GDK_SCALE=2` без перевода полоса уезжает вдвое и растёт вдвое. Модуль грузит
GTK лениво (`load_gtk`), поэтому тест импортирует полосу без X и без GTK —
и заодно проверяет, что импорт их не требует.

Запуск:
    python3 tests/test_tabstrip_scale.py
    python3 -m unittest discover -s tests -v
"""

import contextlib
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
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_units as units  # noqa: E402

# Полоса — hyphen-файл: рядом с полосой он зовётся `zone-tabstrip.py`.
_spec = importlib.util.spec_from_file_location(
    "gridsnap_tabstrip_module", os.path.join(ROOT, "zone-tabstrip.py")
)
strip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strip)


def fixed(scale):
    """`scale_of` с постоянным масштабом: возвращает и запоминает точки."""
    seen = []

    def scale_of(point):
        seen.append(point)
        return scale

    return scale_of, seen


class TestWindowRect(unittest.TestCase):
    def test_scale_two_halves_place_and_size(self):
        # 1920x22 физических пикселей — это 960x11 логических: и место, и размер.
        self.assertEqual(strip.window_rect((0, 0, 1920, 22), lambda _p: 2), (0, 0, 960, 11))

    def test_scale_one_is_identity(self):
        self.assertEqual(strip.window_rect((10, 20, 300, 22), lambda _p: 1), (10, 20, 300, 22))
        # Нет масштаба вовсе (GDK не ответил) — тоже 1, а не ошибка.
        for value in (None, 0, -3, "x"):
            with self.subTest(value=value):
                self.assertEqual(
                    strip.window_rect((10, 20, 300, 22), lambda _p, v=value: v),
                    (10, 20, 300, 22),
                )

    def test_scale_is_asked_under_own_center(self):
        # Полоса на втором мониторе: масштаб — под её центром, а не первого.
        scale_of, seen = fixed(2)
        strip.window_rect((100, 200, 300, 22), scale_of)
        self.assertEqual(seen, [(100 + 300 // 2, 200 + 22 // 2)])

    def test_matches_zone_units_conversion(self):
        # Перевод — ровно `zone_units.logical_rect`, без своей арифметики: края
        # считаются от общих линий, чтобы соседние полосы не разошлись на 1 px.
        for rect in ((0, 0, 1920, 22), (1920, 40, 1921, 23), (37, 11, 101, 23)):
            for scale in (1, 2, 3):
                with self.subTest(rect=rect, scale=scale):
                    x, y, w, h = units.logical_rect(rect, scale)
                    self.assertEqual(
                        strip.window_rect(rect, lambda _p, s=scale: s),
                        (int(x), int(y), max(1, int(w)), max(1, int(h))),
                    )

    def test_size_never_below_one_pixel(self):
        # Узкая полоса на большом масштабе: окно нулевого размера не покажет ни
        # подписи, ни рамки вкладки.
        for rect in ((0, 0, 1, 1), (5, 5, 1, 22), (0, 0, 3, 3)):
            with self.subTest(rect=rect):
                _x, _y, w, h = strip.window_rect(rect, lambda _p: 4)
                self.assertGreaterEqual(w, 1)
                self.assertGreaterEqual(h, 1)

    def test_sizes_and_positions_are_integers(self):
        self.assertTrue(
            all(isinstance(v, int) for v in strip.window_rect((3, 5, 41, 21), lambda _p: 2))
        )

    def test_scale_lookup_failure_falls_back_to_one(self):
        # Сбой чтения масштаба не должен ронять полосу: масштаб 1, место прежнее.
        def broken(_point):
            raise RuntimeError("нет X")

        self.assertEqual(strip.window_rect((10, 20, 300, 22), broken), (10, 20, 300, 22))

    def test_fractional_rect_is_rounded_to_whole_pixels(self):
        # `nav.record_rect` отдаёт float — окну нужны целые.
        self.assertEqual(strip.window_rect((0.4, 21.6, 1919.5, 22.2), lambda _p: 1),
                         (0, 22, 1920, 22))


class TestStripScale(unittest.TestCase):
    def test_no_tiler_means_scale_one(self):
        # Раскладчика нет (не установлен, нет его зависимостей) — полоса как раньше.
        with mock.patch.object(strip, "tiler_module", lambda: None):
            self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_answers_scale(self):
        class Fake:
            @staticmethod
            def scale_at_point(_point):
                return 2

        with mock.patch.object(strip, "tiler_module", lambda: Fake()):
            self.assertEqual(strip.strip_scale((10, 10)), 2)

    def test_tiler_garbage_scale_is_one(self):
        class Fake:
            @staticmethod
            def scale_at_point(_point):
                return None

        with mock.patch.object(strip, "tiler_module", lambda: Fake()):
            self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_exit_and_error_do_not_kill_the_strip(self):
        # `zone-tiler.monitors` без X говорит `fail` (SystemExit) — полоса жива.
        class Exiting:
            @staticmethod
            def scale_at_point(_point):
                raise SystemExit(1)

        class Failing:
            @staticmethod
            def scale_at_point(_point):
                raise OSError("нет дисплея")

        for fake in (Exiting(), Failing()):
            with self.subTest(fake=type(fake).__name__):
                with mock.patch.object(strip, "tiler_module", lambda f=fake: f):
                    self.assertEqual(strip.strip_scale((10, 10)), 1)

    def test_tiler_module_is_loaded_once(self):
        # Снимок приходит на каждое изменение картины: загрузка раскладчика — раз.
        calls = []

        def fake_loader():
            calls.append(1)
            return None

        with mock.patch.object(strip, "tiler_module", fake_loader):
            strip.strip_scale((0, 0))
            strip.strip_scale((10, 10))
        self.assertEqual(len(calls), 2)  # патч считает вызовы самого `strip_scale`


import unittest.mock  # noqa: E402  (нужен в тестах выше по тексту)


class TestTilerLoading(unittest.TestCase):
    def test_module_import_needs_neither_gtk_nor_display(self):
        # Полоса грузится без GTK: `load_gtk` вызывается только из `main`.
        self.assertIsNone(strip.Gtk)
        self.assertIsNone(strip.cairo)

    def test_real_tiler_without_display_gives_scale_one(self):
        # Настоящий `zone-tiler.py` без DISPLAY: масштаба не спросить — 1.
        saved = strip._ZT, strip._ZT_LOADED
        strip._ZT, strip._ZT_LOADED = None, False
        env = {key: os.environ.pop(key) for key in ("DISPLAY", "GDK_SCALE") if key in os.environ}
        noise = io.StringIO()  # `fail` раскладчика пишет в stderr — это не наш вывод
        try:
            with contextlib.redirect_stderr(noise):
                self.assertEqual(strip.strip_scale((10, 10)), 1)
        finally:
            os.environ.update(env)
            strip._ZT, strip._ZT_LOADED = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)

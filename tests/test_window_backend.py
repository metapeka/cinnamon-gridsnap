#!/usr/bin/env python3
"""Контракт оконного слоя `window_backend` — сверка сигнатур, а не реализации.

Смысл теста: граница оконного слоя зафиксирована **до** переноса кода. Если
завтра кто-то возьмётся за Wayland-бэкенд, он должен реализовать ровно эти
операции; если перенос из `zone-tiler.py` потеряет функцию или поменяет у неё
число аргументов, тест это поймает — остальная программа зовёт оконный слой
именно так.

Тела функций проверяет не этот тест, а поведенческие тесты раскладчика, демона и
дымовые сценарии: здесь только интерфейс.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import window_backend as wb  # noqa: E402

# Чтение: имя -> число аргументов (кроме self).
READ_API = {
    "window_class_title": 1,
    "window_types": 1,
    "read_client_list": 0,
    "window_workspace": 1,
    "current_workspace": 0,
    "window_iconic": 1,
    "read_active_window": 0,
    "read_frame": 2,
    "read_window_center": 1,
    "frame_extents": 1,
    "read_session_id": 0,
    "gdk_monitors": 0,
    "monitors": 0,
    "monitor_scales": 0,
    "scale_at_point": 1,
    "monitor_outputs": 0,
    "xrandr_monitors": 0,
    "parse_xrandr_monitors": 1,
    "stitch_monitor_names": 2,
    "monitor_for_point": 2,
}

# Запись: методы X11 — аргументов без `self` (у `set_state` второй атом необязателен).
WRITE_API = {
    "close": 0,
    "atom": 1,
    "send": 3,
    "set_state": 4,
    "move_resize": 5,
    "iconify": 1,
    "activate": 1,
    "set_current_desktop": 1,
}


class TestWindowBackendContract(unittest.TestCase):
    def test_read_api_is_present(self):
        for name, arity in READ_API.items():
            self.assertTrue(hasattr(wb, name), f"нет window_backend.{name}")
            fn = getattr(wb, name)
            self.assertTrue(callable(fn), name)
            params = inspect.signature(fn).parameters
            self.assertEqual(
                len(params),
                arity,
                f"window_backend.{name}: {len(params)} аргументов, ожидалось {arity}",
            )

    def test_write_api_is_present(self):
        x11 = wb.X11
        self.assertTrue(inspect.isclass(x11), "window_backend.X11 — не класс")
        for name, arity in WRITE_API.items():
            self.assertTrue(hasattr(x11, name), f"нет X11.{name}")
            method = getattr(x11, name)
            params = list(inspect.signature(method).parameters.values())[1:]  # без self
            optional = sum(
                1 for p in params if p.default is not inspect.Parameter.empty
            )
            self.assertEqual(
                len(params) - optional,
                arity - 1 if name == "set_state" else arity,
                f"X11.{name}: обязательных аргументов {len(params) - optional}, "
                f"ожидалось {arity - 1 if name == 'set_state' else arity}",
            )

    def test_api_has_docstrings(self):
        for name in READ_API:
            self.assertTrue(
                (getattr(wb, name).__doc__ or "").strip(),
                f"window_backend.{name} без докстринга: интерфейс — это и описание",
            )
        for name in WRITE_API:
            self.assertTrue(
                (getattr(wb.X11, name).__doc__ or "").strip(),
                f"X11.{name} без докстринга",
            )

    def test_error_type_is_the_contract_for_failures(self):
        self.assertTrue(issubclass(wb.WindowBackendError, Exception))
        # Никаких `SystemExit` из оконного слоя: код возврата — дело CLI.
        self.assertFalse(issubclass(wb.WindowBackendError, SystemExit))

    def test_no_user_facing_strings(self):
        # Текст для пользователя и код возврата остаются у CLI: в оконном слое
        # не должно быть ни `_()`, ни своих сообщений в stderr.
        source = inspect.getsource(wb)
        self.assertNotIn("from zone_i18n", source)
        self.assertNotIn("print(", source)


if __name__ == "__main__":
    unittest.main()

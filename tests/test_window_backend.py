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

import contextlib
import importlib.util
import inspect
import io
import os
import sys
import unittest
from unittest import mock

try:
    import i18n_env  # noqa: F401  (тесты идут на исходном языке)
except ImportError:  # запуск вида `python3 -m unittest tests.<модуль>`
    from tests import i18n_env  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import window_backend as wb  # noqa: E402


def load_tiler():
    """Загрузить `zone-tiler.py` рядом: у него обёртки, а не реализация."""
    path = os.path.join(ROOT, "zone-tiler.py")
    spec = importlib.util.spec_from_file_location(
        "zone_tiler_window_backend_under_test", path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_tiler()


def load_daemon():
    """Загрузить `zone-daemon.py`: `read_session_id` — его обёртка."""
    path = os.path.join(ROOT, "zone-daemon.py")
    spec = importlib.util.spec_from_file_location(
        "zone_daemon_window_backend_under_test", path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

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


class TestTilerWrappers(unittest.TestCase):
    """Обёртки в `zone-tiler.py`: те же имена и аргументы, тексты — у CLI.

    Тест держит вторую половину шва: перенос тел в `window_backend` не должен
    сдвинуть ни одну сигнатуру, которую видят демон, пикер, `zone_config`,
    апплет и тесты, а сообщение с кодом возврата — остаться прежними.
    """

    def test_read_wrappers_keep_names_and_arity(self):
        # `read_session_id` — обёртка демона, её проверяет тест ниже.
        for name, arity in READ_API.items():
            if name == "read_session_id":
                continue
            self.assertTrue(hasattr(zt, name), f"нет zone-tiler.{name}")
            params = inspect.signature(getattr(zt, name)).parameters
            self.assertEqual(
                len(params),
                arity,
                f"zone-tiler.{name}: {len(params)} аргументов, ожидалось {arity}",
            )

    def test_daemon_read_session_id_is_a_wrapper(self):
        zd = load_daemon()
        self.assertEqual(len(inspect.signature(zd.read_session_id).parameters), 0)
        with mock.patch.object(wb, "read_session_id", return_value="0x2a"):
            self.assertEqual(zd.read_session_id(), "0x2a")

    def test_x11_name_is_the_backend_class(self):
        # `apply_zone` и тесты берут класс по имени `zone-tiler.X11`.
        self.assertIs(zt.X11, wb.X11)

    def test_monitors_failure_is_the_old_message_and_exit_code(self):
        with mock.patch.object(
            wb, "monitors", side_effect=wb.WindowBackendError("нет GDK")
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                zt.monitors()
        self.assertEqual(ctx.exception.code, zt.EXIT_FAIL)
        self.assertEqual(
            err.getvalue().strip(),
            "zone-tiler: не удалось определить рабочую область экрана",
        )

    def test_read_frame_missing_field_is_value_error_with_old_text(self):
        def boom(window, extents):
            raise wb.WindowBackendError("Width")

        with mock.patch.object(wb, "read_frame", boom):
            with self.assertRaises(ValueError) as ctx:
                zt.read_frame(0x1, (0, 0, 0, 0))
        self.assertEqual(str(ctx.exception), "xwininfo: нет поля 'Width'")

    def test_read_active_window_reasons_become_the_old_texts(self):
        cases = (
            (wb.ACTIVE_WINDOW_NO_XPROP, "zone-tiler: xprop не найден (пакет x11-utils)"),
            (wb.ACTIVE_WINDOW_NO_WINDOW, "zone-tiler: нет активного окна"),
        )
        for reason, text in cases:
            with mock.patch.object(
                wb, "read_active_window", return_value=(None, reason)
            ):
                self.assertEqual(zt.read_active_window(), (None, text))
        with mock.patch.object(wb, "read_active_window", return_value=(0x1, None)):
            self.assertEqual(zt.read_active_window(), (0x1, None))


if __name__ == "__main__":
    unittest.main()

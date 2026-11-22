#!/usr/bin/env python3
"""Юнит-тесты IPC GridSnap — без X-сервера, GTK и дисплея.

Проверяют чистый `zone_ipc.py`: кадрирование (`LineReader`), разбор строк
(`parse_command`), диспетчер с заглушками-обработчиками (`Dispatcher`),
обслуживание соединения и слушающий UNIX-сокет (`Server`). Отдельно —
диспетчер демона: `Daemon.build_dispatcher()` отвечает на `status`/`quit`
с подставленной рабочей областью, без реального X.

Запуск:
    python3 tests/test_ipc.py
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_ipc  # noqa: E402

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

CONF = (
    "[grid]\ncols = 1.0\nrows = 1.0\n"
    "[zones]\n"
    "left = 0, 0, 0.5, 1\n"
    "right = 0.5, 0, 0.5, 1\n"
    "[rules]\n"
    "firefox* = left\n"
    "[daemon]\n"
    "reflow = neighbors\n"
)


def load_daemon():
    """Загрузить `zone-daemon.py` (имя с дефисом) без обращения к X."""
    path = os.path.join(ROOT, "zone-daemon.py")
    spec = importlib.util.spec_from_file_location("zone_daemon_ipc_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- кадрирование


class TestFraming(unittest.TestCase):
    def test_encode_message_is_json_line(self):
        raw = zone_ipc.encode_message({"ok": True, "зона": "main"})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True, "зона": "main"})

    def test_reader_splits_complete_lines(self):
        reader = zone_ipc.LineReader()
        self.assertEqual(reader.feed(b'{"cmd":"a"}\n{"cmd":"b"}\n'), [b'{"cmd":"a"}', b'{"cmd":"b"}'])
        self.assertEqual(reader.tail(), b"")

    def test_reader_keeps_partial_line_until_next_chunk(self):
        reader = zone_ipc.LineReader()
        self.assertEqual(reader.feed(b'{"cmd":'), [])
        self.assertEqual(reader.tail(), b'{"cmd":')
        self.assertEqual(reader.feed(b'"x"}\n'), [b'{"cmd":"x"}'])

    def test_reader_rejects_overlong_line(self):
        reader = zone_ipc.LineReader(max_line=4)
        with self.assertRaises(zone_ipc.ProtocolError):
            reader.feed(b"12345")


# ------------------------------------------------------------------- разбор


class TestParseCommand(unittest.TestCase):
    def test_parses_object_with_cmd(self):
        self.assertEqual(
            zone_ipc.parse_command('{"cmd": "status"}'), {"cmd": "status"}
        )

    def test_accepts_bytes_and_strips(self):
        self.assertEqual(
            zone_ipc.parse_command(b'  {"cmd":"apply", "zone":"main"}\n'),
            {"cmd": "apply", "zone": "main"},
        )

    def test_bad_json_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command("{не json}")

    def test_non_object_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command("[1, 2, 3]")

    def test_missing_cmd_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command('{"zone": "main"}')

    def test_empty_cmd_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command('{"cmd": "   "}')

    def test_empty_line_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command("\n")

    def test_bad_utf8_raises(self):
        with self.assertRaises(zone_ipc.ProtocolError):
            zone_ipc.parse_command(b"\xff\xfe")


# --------------------------------------------------------------- диспетчер


class TestDispatcher(unittest.TestCase):
    def test_known_command_calls_handler(self):
        seen = []
        disp = zone_ipc.Dispatcher({"ping": lambda req: seen.append(req) or {"pong": 1}})
        self.assertEqual(disp.dispatch('{"cmd":"ping"}'), {"ok": True, "pong": 1})
        self.assertEqual(seen, [{"cmd": "ping"}])

    def test_none_handler_result_becomes_empty_ok(self):
        disp = zone_ipc.Dispatcher({"noop": lambda req: None})
        self.assertEqual(disp.dispatch('{"cmd":"noop"}'), {"ok": True})

    def test_unknown_command_is_reported(self):
        disp = zone_ipc.Dispatcher({"ping": lambda req: {}})
        response = disp.dispatch('{"cmd":"nope"}')
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "unknown_command")
        self.assertIn("nope", str(response["error"]))

    def test_broken_json_is_reported(self):
        disp = zone_ipc.Dispatcher({})
        response = disp.dispatch("{не json}")
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "bad_request")

    def test_handler_exception_is_caught(self):
        def boom(request):
            raise RuntimeError("упало")

        disp = zone_ipc.Dispatcher({"b": boom})
        response = disp.dispatch('{"cmd":"b"}')
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "handler_error")
        self.assertIn("упало", str(response["error"]))

    def test_handler_system_exit_is_caught(self):
        # `fail()` в layout-коде бросает SystemExit — он не подкласс Exception.
        def bail(request):
            raise SystemExit("нет активного окна")

        disp = zone_ipc.Dispatcher({"a": bail})
        response = disp.dispatch('{"cmd":"a"}')
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "handler_error")

    def test_handler_non_dict_is_reported(self):
        disp = zone_ipc.Dispatcher({"x": lambda req: 42})
        response = disp.dispatch('{"cmd":"x"}')
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "handler_error")

    def test_register_adds_handler(self):
        disp = zone_ipc.Dispatcher()
        disp.register("ping", lambda req: {"n": 1})
        self.assertTrue(disp.dispatch('{"cmd":"ping"}')["ok"])


# --------------------------------------------------------- соединение и сокет


class TestConnection(unittest.TestCase):
    def test_serve_connection_answers_each_command(self):
        client, server = socket.socketpair()
        try:
            disp = zone_ipc.Dispatcher(
                {"ping": lambda req: {"n": req.get("n", 0)}, "bad": None}
            )
            client.sendall(b'{"cmd":"ping","n":1}\n{"cmd":"ping","n":2}\n')
            client.shutdown(socket.SHUT_WR)
            responses = zone_ipc.serve_connection(server, disp)
            server.close()  # иначе клиент ждёт EOF бесконечно
            self.assertEqual([r["n"] for r in responses], [1, 2])
            data = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
            lines = [ln for ln in data.split(b"\n") if ln]
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0].decode("utf-8"))["n"], 1)
        finally:
            client.close()
            server.close()

    def test_serve_connection_tail_without_newline(self):
        client, server = socket.socketpair()
        try:
            disp = zone_ipc.Dispatcher({"ping": lambda req: {"pong": True}})
            client.sendall(b'{"cmd":"ping"}')  # без перевода строки
            client.shutdown(socket.SHUT_WR)
            responses = zone_ipc.serve_connection(server, disp)
            self.assertEqual(responses, [{"ok": True, "pong": True}])
        finally:
            client.close()
            server.close()

    def test_server_sets_permissions_and_serves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runtime", "gridsnap", "daemon.sock")
            disp = zone_ipc.Dispatcher({"ping": lambda req: {"pong": True}})
            server = zone_ipc.Server(path, disp)
            try:
                self.assertEqual(
                    os.stat(path).st_mode & 0o777, 0o600, "права сокета"
                )
                self.assertEqual(
                    os.stat(os.path.dirname(path)).st_mode & 0o777,
                    0o700,
                    "права каталога",
                )
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(path)
                client.sendall(b'{"cmd":"ping"}\n')
                client.shutdown(socket.SHUT_WR)
                responses = server.accept_all(conn_timeout=2.0)
                self.assertEqual(responses, [{"ok": True, "pong": True}])
                data = client.recv(4096)
                client.close()
                self.assertTrue(json.loads(data.decode("utf-8"))["ok"])
            finally:
                server.close()
            self.assertFalse(os.path.exists(path), "сокет убран при close")

    def test_server_refuses_live_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "daemon.sock")
            first = zone_ipc.Server(path, zone_ipc.Dispatcher())
            second = None
            try:
                with self.assertRaises(OSError):
                    second = zone_ipc.Server(path, zone_ipc.Dispatcher())
            finally:
                if second is not None:
                    second.close()
                first.close()

    def test_server_replaces_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "daemon.sock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("остаток от упавшего демона")
            server = zone_ipc.Server(path, zone_ipc.Dispatcher())
            try:
                self.assertTrue(os.path.exists(path))
            finally:
                server.close()

    def test_default_socket_path_follows_xdg(self):
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/4242"
        try:
            self.assertEqual(
                zone_ipc.default_socket_path(), "/run/user/4242/gridsnap/daemon.sock"
            )
        finally:
            if old is None:
                del os.environ["XDG_RUNTIME_DIR"]
            else:
                os.environ["XDG_RUNTIME_DIR"] = old

    def test_default_socket_path_falls_back_to_tmp(self):
        old = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            path = zone_ipc.default_socket_path()
            self.assertTrue(
                path.startswith(tempfile.gettempdir()), f"неожиданный путь: {path}"
            )
            self.assertTrue(path.endswith("daemon.sock"))
        finally:
            if old is not None:
                os.environ["XDG_RUNTIME_DIR"] = old

    def test_read_response_parses_object(self):
        client, server = socket.socketpair()
        try:
            server.sendall(b'{"ok": true, "n": 7}\n')
            self.assertEqual(zone_ipc.read_response(client), {"ok": True, "n": 7})
        finally:
            client.close()
            server.close()


# ------------------------------------------------------- диспетчер демона


class TestDaemonDispatcher(unittest.TestCase):
    """Проверка, что демон отдаёт наружу настоящие обработчики (без X)."""

    @classmethod
    def setUpClass(cls):
        cls.daemon = load_daemon()

    def make_daemon(self, tmp):
        conf = os.path.join(tmp, "zones.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(CONF)
        # Принудительная рабочая область: реальные мониторы (X) не нужны.
        return self.daemon.Daemon(conf, workarea=(0, 0, 1000, 800))

    def test_status_reports_zones_and_reflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            dae = self.make_daemon(tmp)
            disp = dae.build_dispatcher()
            response = disp.dispatch('{"cmd":"status"}')
            self.assertTrue(response["ok"], response)
            self.assertEqual(sorted(response["zones"]), ["left", "right"])
            self.assertEqual(response["reflow"], "neighbors")
            self.assertEqual(response["windows"], [])
            self.assertIsNone(response["profile"])
            self.assertEqual(response["monitors"], [[0, 0, 1000, 800]])
            self.assertEqual(response["zones_px"]["left"], [[0, 0, 500, 800]])

    def test_unknown_command_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            dae = self.make_daemon(tmp)
            response = dae.build_dispatcher().dispatch('{"cmd":"нет-такой"}')
            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "unknown_command")

    def test_quit_sets_stop_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            dae = self.make_daemon(tmp)
            self.assertFalse(dae.stop_requested)
            response = dae.build_dispatcher().dispatch('{"cmd":"quit"}')
            self.assertTrue(response["ok"])
            self.assertTrue(dae.stop_requested)

    def test_apply_unknown_zone_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dae = self.make_daemon(tmp)
            response = dae.build_dispatcher().dispatch(
                '{"cmd":"apply","zone":"нет","window":"0x1"}'
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], "handler_error")

    def test_apply_uses_injected_apply_and_tracker(self):
        calls = []

        def fake_apply(win, rect, label="", quiet=False):
            calls.append((win, rect, label))

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(CONF)
            dae = self.daemon.Daemon(
                conf, apply=fake_apply, workarea=(0, 0, 1000, 800)
            )
            response = dae.build_dispatcher().dispatch(
                '{"cmd":"apply","zone":"right","window":"0x2a"}'
            )
            self.assertTrue(response["ok"], response)
            self.assertEqual(calls, [(0x2A, (500, 0, 500, 800), "right")])
            self.assertEqual(dae.windows[0x2A], "right")
            self.assertEqual(dae.win_monitor[0x2A], (0, 0, 1000, 800))

    def test_reload_rereads_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(CONF)
            dae = self.daemon.Daemon(conf, workarea=(0, 0, 1000, 800))
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(CONF.replace("right = 0.5, 0, 0.5, 1\n", ""))
            response = dae.build_dispatcher().dispatch('{"cmd":"reload"}')
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["zones"], ["left"])
            self.assertEqual(sorted(dae.grid.zones), ["left"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Контракт событий демона: подписка и формы строк.

Смысл: внешний потребитель (скрипт, панель, апплет) подключается и слушает, что
делает демон. Формы событий зафиксированы здесь, потому что их читают снаружи —
апплет на JavaScript тоже, и менять поля задним числом нельзя.

Серверную часть (набор подписчиков и рассылку) проверяет дымовой сценарий
`tests/smoke-events.sh`; здесь — только протокол.
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zone_ipc as ipc  # noqa: E402


DAEMON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "zone-daemon.py")


def load_daemon():
    """Загрузить `zone-daemon.py` (имя с дефисом) без обращения к X."""
    spec = importlib.util.spec_from_file_location("zone_daemon_events_test", DAEMON)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parsed(line):
    return json.loads(line.decode("utf-8"))


class TestSubscription(unittest.TestCase):
    def test_no_events_means_everything(self):
        self.assertEqual(ipc.parse_subscription({"cmd": "subscribe"}), ipc.ALL_EVENT_GROUPS)

    def test_groups_are_kept_in_requested_order_without_dupes(self):
        groups = ipc.parse_subscription(
            {"cmd": "subscribe", "events": ["focus", "window", "focus"]}
        )
        self.assertEqual(groups, ("focus", "window"))

    def test_empty_list_is_an_error_not_everything(self):
        # Иначе опечатка в запросе молча давала бы поток, которого клиент не ждал.
        with self.assertRaises(ipc.ProtocolError):
            ipc.parse_subscription({"cmd": "subscribe", "events": []})

    def test_unknown_group_is_a_clear_error(self):
        with self.assertRaises(ipc.ProtocolError) as ctx:
            ipc.parse_subscription({"cmd": "subscribe", "events": ["windows"]})
        self.assertIn("window", str(ctx.exception))

    def test_not_a_list_is_an_error(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.parse_subscription({"cmd": "subscribe", "events": "window"})

    def test_answer_says_what_was_accepted(self):
        line = ipc.subscribe_answer(("window",))
        self.assertEqual(parsed(line), {"ok": True, "subscribed": ["window"]})


class TestEventShapes(unittest.TestCase):
    """Поля событий — контракт для внешних потребителей (в т.ч. апплета)."""

    def test_window_appeared(self):
        self.assertEqual(
            parsed(ipc.event_window_appeared(0x3A00007, "main", 0, [0, 0, 1920, 1080])),
            {
                "event": "window-appeared",
                "window": "0x3a00007",
                "zone": "main",
                "workspace": 0,
                "monitor": [0, 0, 1920, 1080],
            },
        )

    def test_window_appeared_without_zone(self):
        # Окно под [ignore] или без правила: появилось, но никуда не встало.
        event = parsed(ipc.event_window_appeared(0x1))
        self.assertEqual(event["zone"], None)
        self.assertEqual(event["window"], "0x1")

    def test_window_closed(self):
        self.assertEqual(
            parsed(ipc.event_window_closed(0x2, "right")),
            {"event": "window-closed", "window": "0x2", "zone": "right"},
        )

    def test_window_moved_names_both_zones(self):
        self.assertEqual(
            parsed(ipc.event_window_moved(0x3, "right", previous="main", workspace=1)),
            {
                "event": "window-moved",
                "window": "0x3",
                "zone": "right",
                "previous": "main",
                "workspace": 1,
                "monitor": None,
            },
        )

    def test_focus_on_and_off(self):
        self.assertEqual(
            parsed(ipc.event_focus_on("main", 0x4, minimized=3)),
            {"event": "focus-on", "zone": "main", "window": "0x4", "minimized": 3},
        )
        self.assertEqual(
            parsed(ipc.event_focus_off("main", restored=2, total=3)),
            {"event": "focus-off", "zone": "main", "restored": 2, "total": 3},
        )

    def test_unknown_event_kind_is_an_error(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.event_line("window-teleported", window="0x1")

    def test_lines_are_single_line_json(self):
        line = ipc.event_window_closed(0x2, "main")
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)


class TestFiltering(unittest.TestCase):
    def test_wants_event_by_group(self):
        window_only = ipc.parse_subscription({"cmd": "subscribe", "events": ["window"]})
        self.assertTrue(ipc.wants_event(window_only, "window-closed"))
        self.assertFalse(ipc.wants_event(window_only, "focus-on"))

    def test_all_groups_want_everything(self):
        for _group, kinds in ipc.EVENT_GROUPS.items():
            for kind in kinds:
                self.assertTrue(ipc.wants_event(ipc.ALL_EVENT_GROUPS, kind))

    def test_groups_cover_every_known_kind(self):
        covered = [k for kinds in ipc.EVENT_GROUPS.values() for k in kinds]
        self.assertEqual(len(covered), len(set(covered)), "вид события в двух группах")


class TestSubscriptionTransport(unittest.TestCase):
    """Подписка как свойство транспорта: соединение остаётся и служит рассылке."""

    def read_line(self, conn):
        """Прочитать одну строку протокола: `recv` может отдать сразу две."""
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        line, _sep, _rest = buf.partition(b"\n")
        return line

    def dispatcher(self):
        return ipc.Dispatcher(
            {
                "subscribe": lambda req: ipc.Subscription(
                    ipc.parse_subscription(req)
                ),
                "ping": lambda req: {"pong": True},
            }
        )

    def test_dispatcher_returns_subscription_untouched(self):
        response = self.dispatcher().dispatch('{"cmd": "subscribe"}')
        self.assertIsInstance(response, ipc.Subscription)
        self.assertEqual(response["subscribed"], list(ipc.ALL_EVENT_GROUPS))
        self.assertEqual(response.groups, ipc.ALL_EVENT_GROUPS)

    def test_serve_connection_hands_subscription_to_registry(self):
        client, server = socket.socketpair()
        subs = ipc.Subscribers()
        try:
            client.sendall(b'{"cmd": "subscribe", "events": ["focus"]}\n')
            responses = ipc.serve_connection(server, self.dispatcher(), subs)
            self.assertEqual(len(responses), 1, "на подписку — один ответ")
            self.assertTrue(subs.has(server), "соединение перешло в подписчики")
            self.assertEqual(subs.groups(server), ("focus",))
            self.assertEqual(parsed(self.read_line(client))["subscribed"], ["focus"])
            # Соединение не закрыто: рассылка доходит.
            self.assertEqual(subs.broadcast("focus-on", ipc.event_focus_on("main")), 1)
            self.assertEqual(parsed(self.read_line(client))["event"], "focus-on")
        finally:
            subs.close_all()
            client.close()
            server.close()

    def test_broadcast_filters_by_group(self):
        client, server = socket.socketpair()
        subs = ipc.Subscribers()
        try:
            subs.add(server, ("window",))
            self.assertFalse(subs.wants("focus-on"))
            self.assertTrue(subs.wants("window-closed"))
            self.assertEqual(subs.broadcast("focus-on", ipc.event_focus_on("main")), 0)
            self.assertEqual(subs.count(), 1)
            self.assertEqual(
                subs.broadcast("window-closed", ipc.event_window_closed(0x1, "main")),
                1,
            )
            self.assertEqual(parsed(client.recv(4096))["event"], "window-closed")
        finally:
            subs.close_all()
            client.close()
            server.close()

    def test_slow_subscriber_is_dropped_not_buffered(self):
        # Подписчик, который не читает: событие — новость, а не очередь, поэтому
        # демон его отбрасывает, а не копит буфер в своей памяти.
        client, server = socket.socketpair()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        subs = ipc.Subscribers()
        try:
            subs.add(server, ipc.ALL_EVENT_GROUPS)
            line = ipc.event_window_appeared(0x1, "main", 0, [0, 0, 1000, 1000])
            for _ in range(5000):
                if not subs.count():
                    break
                subs.broadcast("window-appeared", line)
            self.assertEqual(subs.count(), 0, "медленного подписчика не копим")
        finally:
            subs.close_all()
            client.close()
            server.close()

    def test_broken_subscriber_is_dropped(self):
        client, server = socket.socketpair()
        subs = ipc.Subscribers()
        try:
            subs.add(server, ipc.ALL_EVENT_GROUPS)
            client.close()
            subs.broadcast("window-closed", ipc.event_window_closed(0x1, "main"))
            self.assertEqual(subs.count(), 0)
        finally:
            subs.close_all()
            server.close()

    def test_poll_drops_subscriber_on_eof(self):
        client, server = socket.socketpair()
        subs = ipc.Subscribers()
        try:
            subs.add(server, ipc.ALL_EVENT_GROUPS)
            self.assertEqual(subs.filenos(), [server.fileno()])
            client.close()
            self.assertEqual(subs.poll(), 1)
            self.assertEqual(subs.count(), 0)
            self.assertEqual(subs.filenos(), [])
        finally:
            subs.close_all()
            server.close()

    def test_server_keeps_subscribed_connection_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "daemon.sock")
            subs = ipc.Subscribers()
            server = ipc.Server(path, self.dispatcher(), subscribers=subs)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(path)
                client.sendall(b'{"cmd": "subscribe"}\n')
                server.accept_all(conn_timeout=1.0)
                self.assertEqual(subs.count(), 1, "подписка не закрыта после accept")
                self.assertEqual(parsed(self.read_line(client))["subscribed"], list(ipc.ALL_EVENT_GROUPS))
                subs.broadcast(
                    "window-moved",
                    ipc.event_window_moved(0x2, "right", previous="main"),
                )
                self.assertEqual(parsed(self.read_line(client))["event"], "window-moved")
            finally:
                client.close()
                server.close()
            self.assertEqual(subs.count(), 0, "close() закрывает подписчиков")


class FakeSubscribers:
    """Подмена реестра подписчиков: запомнить события, не заводя сокета."""

    def __init__(self, groups=ipc.ALL_EVENT_GROUPS):
        self.groups = tuple(groups)
        self.events = []

    def add(self, conn, groups):
        self.groups = tuple(groups)
        return conn

    def has(self, conn):
        return True

    def count(self):
        return 1

    def filenos(self):
        return []

    def wants(self, kind):
        return ipc.wants_event(self.groups, kind)

    def broadcast(self, kind, line):
        self.events.append((kind, parsed(line)))
        return 1

    def poll(self):
        return 0

    def close_all(self):
        self.events = []

    def of(self, kind):
        return [fields for event, fields in self.events if event == kind]


class TestDaemonEvents(unittest.TestCase):
    """Демон шлёт события в точки укладки, закрытия, переезда и режима."""

    @classmethod
    def setUpClass(cls):
        cls.daemon = load_daemon()

    def setUp(self):
        # Без X `window_iconic` недоступен: подменяем состоянием «ничего не свёрнуто».
        patcher = mock.patch.object(self.daemon.tiler, "window_iconic", lambda win: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    CONF = (
        "[grid]\ncols = 1.0\nrows = 1.0\n"
        "[zones]\n"
        "left = 0, 0, 0.5, 1\n"
        "right = 0.5, 0, 0.5, 1\n"
        "[rules]\n"
        "firefox* = left\n"
    )
    WA = (0, 0, 1000, 800)

    class Meta:
        def __init__(self, wm_class="firefox", wm_title="Firefox"):
            self.wm_class = wm_class
            self.wm_title = wm_title
            self.window_type = ()

    def meta(self, win):
        return self.Meta()

    def make_daemon(self, subscribers=None, clients=None, meta=None):
        tmp = tempfile.mkdtemp(prefix="gridsnap-events-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        conf = os.path.join(tmp, "zones.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(self.CONF)
        subs = subscribers if subscribers is not None else FakeSubscribers()
        applied = []

        def apply(win, rect, label="", quiet=False):
            applied.append((win, rect, label))

        read_meta = meta if meta is not None else (lambda win: self.Meta())
        dae = self.daemon.Daemon(
            conf,
            apply=apply,
            read_meta=read_meta,
            workarea=lambda: self.WA,
            read_rect=lambda win: None,
            current_workspace=lambda: 0,
            window_workspace=lambda win: 0,
            window_center=lambda win: (10, 10),
            iconify=lambda win, x11=None, quiet=True: None,
            deiconify=lambda win, x11=None, quiet=True: None,
            read_clients=lambda: list(clients or []),
            active_window=lambda: None,
            subscribers=subs,
        )
        dae.applied = applied
        return dae, subs

    def test_subscribe_returns_subscription_with_groups(self):
        dae, _subs = self.make_daemon()
        response = dae.build_dispatcher().dispatch(
            '{"cmd": "subscribe", "events": ["focus"]}'
        )
        # Тот же класс, что у демона: он загружает `zone_ipc` своим модулем.
        self.assertIsInstance(response, self.daemon.zone_ipc.Subscription)
        self.assertEqual(response["subscribed"], ["focus"])
        self.assertEqual(response.groups, ("focus",))

    def test_subscribe_with_bad_group_is_rejected(self):
        dae, _subs = self.make_daemon()
        response = dae.build_dispatcher().dispatch(
            '{"cmd": "subscribe", "events": ["tabs"]}'
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "handler_error")
        self.assertIn("window", str(response["error"]))

    def test_appeared_event_has_zone_and_monitor(self):
        dae, subs = self.make_daemon()
        dae.reconcile([0x2A])
        appeared = subs.of("window-appeared")
        self.assertEqual(len(appeared), 1)
        self.assertEqual(appeared[0]["window"], "0x2a")
        self.assertEqual(appeared[0]["zone"], "left")
        self.assertEqual(appeared[0]["monitor"], [0, 0, 1000, 800])
        self.assertEqual(appeared[0]["workspace"], 0)

    def test_appeared_without_zone_for_unmatched_window(self):
        # Правило не сработало: окно появилось, но никуда не встало.
        dae, subs = self.make_daemon(meta=lambda win: self.Meta(wm_class="gimp"))
        dae.reconcile([0x2A])
        appeared = subs.of("window-appeared")
        self.assertEqual(len(appeared), 1)
        self.assertIsNone(appeared[0]["zone"])

    def test_closed_event_carries_last_zone(self):
        dae, subs = self.make_daemon()
        dae.reconcile([0x2A])
        dae.reconcile([])
        closed = subs.of("window-closed")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["window"], "0x2a")
        self.assertEqual(closed[0]["zone"], "left")

    def test_apply_command_emits_moved(self):
        dae, subs = self.make_daemon()
        dae.reconcile([0x2A])
        response = dae.build_dispatcher().dispatch(
            '{"cmd": "apply", "zone": "right", "window": "0x2a"}'
        )
        self.assertTrue(response["ok"], response)
        moved = subs.of("window-moved")
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["zone"], "right")
        self.assertEqual(moved[0]["previous"], "left")

    def test_focus_emits_on_and_off(self):
        # Второе окно без правила: оно не в зоне цели, поэтому режим его свернёт.
        def meta(win):
            return self.Meta(wm_class="firefox" if win == 0x2A else "gimp")

        dae, subs = self.make_daemon(clients=[0x2A, 0x2B], meta=meta)
        dae.reconcile([0x2A, 0x2B])
        response = dae.build_dispatcher().dispatch(
            '{"cmd": "focus", "zone": "left", "window": "0x2a"}'
        )
        self.assertTrue(response["ok"], response)
        on = subs.of("focus-on")
        self.assertEqual(len(on), 1)
        self.assertEqual(on[0]["zone"], "left")
        self.assertEqual(on[0]["window"], "0x2a")
        self.assertEqual(on[0]["minimized"], 1)
        response = dae.build_dispatcher().dispatch('{"cmd": "focus", "off": true}')
        self.assertTrue(response["ok"], response)
        off = subs.of("focus-off")
        self.assertEqual(len(off), 1)
        self.assertEqual(off[0]["zone"], "left")
        self.assertEqual(off[0]["restored"], 1)
        self.assertEqual(off[0]["total"], 1)

    def test_arrange_applies_and_emits_moved(self):
        dae, subs = self.make_daemon()
        dae.reconcile([0x2A])
        response = dae.build_dispatcher().dispatch(
            json.dumps(
                {
                    "cmd": "arrange",
                    "workspace": 0,
                    "placements": [
                        {
                            "window": "0x2a",
                            "zone": "right",
                            "monitor": [0, 0, 1000, 800],
                        }
                    ],
                }
            )
        )
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["arranged"], ["0x2a"])
        self.assertEqual(response["skipped"], [])
        self.assertEqual(dae.windows[0x2A], "right")
        moved = subs.of("window-moved")
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["zone"], "right")
        self.assertEqual(moved[0]["previous"], "left")

    def test_arrange_skips_unknown_zone_without_failing_all(self):
        dae, _subs = self.make_daemon()
        dae.reconcile([0x2A])
        response = dae.build_dispatcher().dispatch(
            json.dumps(
                {
                    "cmd": "arrange",
                    "placements": [
                        {"window": "0x2a", "zone": "нет-такой"},
                    ],
                }
            )
        )
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["arranged"], [])
        self.assertEqual(len(response["skipped"]), 1)
        self.assertIn("нет-такой", response["skipped"][0]["error"])

    def test_arrange_needs_placements(self):
        dae, _subs = self.make_daemon()
        response = dae.build_dispatcher().dispatch('{"cmd": "arrange"}')
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "handler_error")

    def test_group_filter_keeps_focus_events_out(self):
        dae, subs = self.make_daemon(subscribers=FakeSubscribers(("window",)))
        dae.reconcile([0x2A])
        dae.focus = {
            "zone": "left",
            "target": 0x2A,
            "workspace": 0,
            "monitor": self.WA,
            "minimized": set(),
        }
        dae.leave_focus()
        self.assertEqual(subs.of("focus-off"), [])
        self.assertEqual(len(subs.of("window-appeared")), 1)


if __name__ == "__main__":
    unittest.main()

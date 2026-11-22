#!/usr/bin/env python3
"""Контракт событий демона: подписка и формы строк.

Смысл: внешний потребитель (скрипт, панель, апплет) подключается и слушает, что
делает демон. Формы событий зафиксированы здесь, потому что их читают снаружи —
апплет на JavaScript тоже, и менять поля задним числом нельзя.

Серверную часть (набор подписчиков и рассылку) проверяет дымовой сценарий
`tests/smoke-events.sh`; здесь — только протокол.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zone_ipc as ipc  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

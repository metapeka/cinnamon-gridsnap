#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_tabs — the pure core of tabs inside a zone for GridSnap.

Tabs are when several windows stand in one zone: one is visible in full,
the rest lie on the same frame and wait to be switched to. All the arithmetic
of this lives here, together with both protocols the daemon, the strip and the tiler speak;
X, GTK and sockets are not here, so the module is covered by ordinary unit tests without an X server
(a project invariant, as with ``zone_geom``/``zone_reflow``/``zone_picker_nav``).

What is here:

* geometry — :func:`strip_rect` (where the tab strip is), :func:`tab_rect` (the frame
  of a tab window: the zone minus the strip) and :func:`stack_rect` (the same rule by the number of tabs:
  one — the whole zone). A narrow zone gets no strip: `strip_rect` returns ``None``, and
  `tab_rect` — the frame of the whole zone;
* window selection — :func:`tabs_of_zone` (which windows stand as tabs of this zone, in
  client list order) and :func:`next_tab` (the next one around the ring);
* the daemon → strip protocol — :func:`strip_record`/:func:`strips_payload`:
  line-by-line JSON into the stdin of the ``zone-tabstrip.py`` process;
* the strip/key → daemon protocol — :func:`tabs_switch_command` (a click on a
  tab) and :func:`tabs_next_command` (``zone-tiler.py tabs-next`` from a key):
  ordinary daemon socket requests, as in ``zone_ipc``.

Frames are pixel ``(x, y, w, h)`` in screen coordinates: the strip is drawn over
the desktop, not in fractions, so fractions are turned into pixels by the caller
(``Grid.rect``), as everywhere in the project.
"""

import json

# The height of the tab strip and the minimum that must be left to the window under it.
# Values with defaults, not dogmas: the height comes from ``[tabs] height``.
STRIP_HEIGHT = 22
MIN_WINDOW_H = 44

# The tolerance for comparing frames when looking for the tabs of a zone, px. The same meaning and the same
# value as ``PLACED_TOLERANCE`` in the daemon: the window manager places the frame,
# exact float equality is not enough.
FRAME_TOLERANCE = 8

# The names of the daemon socket commands that do not come from ``gridsnap-ctl``.
TABS_NEXT = "tabs-next"
TABS_SWITCH = "tabs-switch"

EPS = 1e-9


def strip_rect(zone_rect, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """The frame of the tab strip at the top of the zone — or ``None`` if the strip does not fit.

    The strip takes height from the window, so in a narrow zone (less than
    `min_window_h` is left to the window) it is not drawn at all. The tabs still work: the windows still
    gather on the shared frame and are switched by key, there is just no hint on
    the screen that there are several of them.
    """
    x, y, w, h = (float(v) for v in zone_rect)
    if h - float(height) < float(min_window_h):
        return None
    return (x, y, w, float(height))


def tab_rect(zone_rect, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """The frame of a tab window: the zone minus the strip; without a strip — the zone itself.

    It is computed through :func:`strip_rect`, so the strip and the window cannot
    drift apart: either there is a strip and the window is moved down by its height, or there is no
    strip and the window takes the whole zone.
    """
    x, y, w, h = (float(v) for v in zone_rect)
    if strip_rect(zone_rect, height, min_window_h) is None:
        return (x, y, w, h)
    return (x, y + float(height), w, h - float(height))


def stack_rect(zone_rect, count, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """The frame of the windows of a zone by the number of its tabs.

    One tab — the whole zone: there is no strip, and the window takes the whole zone, as before the tabs wave. Two and
    more — the tab frame (:func:`tab_rect`), because a strip has appeared on top. One rule for the
    daemon and the tiler (`apply`/`arrange`/`stack`), otherwise they drift apart on the same number of windows.
    """
    if count < 2:
        return tuple(float(v) for v in zone_rect)
    return tab_rect(zone_rect, height, min_window_h)


def _same_rect(left, right, tolerance):
    """Did the two frames match with `tolerance` on every side."""
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))


def tabs_of_zone(frames, tab_frame, tolerance=FRAME_TOLERANCE):
    """The windows of the zone as tabs — in bottom-to-top order.

    ``frames`` — ``{window: (x, y, w, h)}`` in the order in which the caller walked
    the windows; the daemon takes ``_NET_CLIENT_LIST``, and that is the order in which the windows appeared,
    not the z-order (the stack lives in ``_NET_CLIENT_LIST_STACKING``, and on a live muffin
    the two differ). ``tab_frame`` — the frame the tabs stand on
    (:func:`tab_rect`).

    Tabs have no state file: the membership is derived from what is visible on
    the screen — a window counts as a tab when its frame matched the shared one. That is why
    a daemon restart loses nothing, and dead XIDs need no cleaning. The frame was
    not read (the window vanished) — ``None`` in the dict, the window is skipped.
    """
    return [
        win
        for win, frame in frames.items()
        if frame is not None and _same_rect(frame, tab_frame, tolerance)
    ]


def next_tab(ids, current):
    """The next window of the ring after `current`; ``None`` — if there is nothing to switch to.

    The order is the same as in :func:`tabs_of_zone`: "next" raises the neighbour in the
    list, and from the last one it goes to the first. If `current` is not in the list (the window
    is gone, the focus is outside the zone), we start from the first.
    A single window and an empty list have nowhere to switch to.
    """
    ids = list(ids)
    if len(ids) < 2:
        return None
    if current in ids:
        return ids[(ids.index(current) + 1) % len(ids)]
    return ids[0]


def strip_record(zone, zone_rect, tabs, active=None, height=STRIP_HEIGHT,
                 min_window_h=MIN_WINDOW_H):
    """One snapshot record for the strip — or ``None`` if the strip has no place here.

    ``tabs`` — ``[(window, title), ...]`` in client list order,
    ``active`` — the window of the raised (visible) tab. The strip is not drawn in two cases: there is only one
    tab (the strip is a sign of a stack, a single window has none) and the strip does not fit
    into the zone (:func:`strip_rect`).
    """
    tabs = list(tabs)
    if len(tabs) < 2:
        return None
    rect = strip_rect(zone_rect, height, min_window_h)
    if rect is None:
        return None
    return {
        "zone": zone,
        "rect": [int(round(v)) for v in rect],
        "tabs": [
            {"window": win, "title": title, "active": win == active}
            for win, title in tabs
        ],
    }


def strips_payload(records):
    """The snapshot line for the strip: one JSON per line, with a trailing ``\\n``.

    The snapshot is complete, not a delta: there are few zones on the screen, but the strip does not have to remember
    the past state, and the daemon does not have to compute the difference. ``ensure_ascii=False`` —
    window titles are Russian. ``None`` records (nothing to draw) are filtered out.
    """
    return json.dumps(
        {"strips": [record for record in records if record is not None]},
        ensure_ascii=False,
    ) + "\n"


def tabs_switch_command(zone, window):
    """A request to the daemon from the strip: show the window `window` in the zone `zone`.

    A click on a tab goes to the same daemon socket as the ``gridsnap-ctl`` commands,
    not to a separate channel: ``zone_ipc`` is enough for the strip.
    """
    return {"cmd": TABS_SWITCH, "zone": zone, "window": int(window)}


def tabs_next_command():
    """A request to the daemon from a key: raise the next window of the stack.

    The daemon picks the zone itself — by the active window; a window outside the zones has nowhere to switch,
    and that is not an error.
    """
    return {"cmd": TABS_NEXT}

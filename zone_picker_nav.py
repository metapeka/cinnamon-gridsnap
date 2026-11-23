#!/usr/bin/env python3
"""zone_picker_nav — pure zone-selection navigation for the ``zone-picker.py`` overlay.

All the arithmetic of visual zone selection lives here: the initial selection,
moving the selection with the arrow keys over the zone geometry, the zone under
the cursor and the bounds of the list. The module deliberately imports neither GTK
nor cairo nor X: it can and should be checked with ordinary unit tests without an
X server (a project invariant).

A zone is anything with ``x, y, w, h`` attributes (like ``zone_config.Zone``) or a
``(x, y, w, h)`` tuple. Coordinates are work-area fractions (0..1), as in
``zones.conf``. The selection is an index into the list of zones; ``None`` is an
empty list.

A point is also either fractions (``zone_at``) or screen pixels together with the
monitor work area (``zone_at_point``): the HUD receives the cursor position from
outside in screen coordinates precisely.
"""

DIRECTIONS = ("left", "right", "up", "down")
EPS = 1e-9


def rect(zone):
    """Zone frame as ``(x, y, w, h)``: from an object with attributes and from a tuple alike."""
    if hasattr(zone, "x"):
        return float(zone.x), float(zone.y), float(zone.w), float(zone.h)
    x, y, w, h = zone
    return float(x), float(y), float(w), float(h)


def center(zone):
    """Zone centre in work-area fractions — directions are compared by it."""
    x, y, w, h = rect(zone)
    return (x + w / 2.0, y + h / 2.0)


def first_index(zones):
    """Initial selection: ``0`` for a non-empty list, otherwise ``None``."""
    return 0 if zones else None


def clamp_index(zones, index):
    """Index within ``[0, len)``; ``None``/garbage/out of range go to the nearest.

    For an empty list always ``None``: there is nothing to select then.
    """
    if not zones:
        return None
    if index is None:
        return 0
    try:
        i = int(index)
    except (TypeError, ValueError):
        return 0
    return min(max(0, i), len(zones) - 1)


def _overlap(a, b, axis):
    """Overlap length of two rectangles along the axis perpendicular to the move.

    It is needed so that "right" from a zone with a strip to its right picks the
    neighbour in the same row, not a random zone in another row with a nearby
    centre.
    """
    ax, ay, aw, ah = rect(a)
    bx, by, bw, bh = rect(b)
    if axis == 0:  # movement along X — count the overlap along Y
        lo, hi = max(ay, by), min(ay + ah, by + bh)
    else:  # movement along Y — the overlap along X
        lo, hi = max(ax, bx), min(ax + aw, bx + bw)
    return max(0.0, hi - lo)


def move_selection(zones, index, direction):
    """Index of the neighbouring zone in the direction; the same index if there is none.

    Only a zone whose centre lies in the wanted direction from the centre of the
    current one counts as a candidate. Among the candidates the winner is first the
    one that overlaps the current zone along the perpendicular axis, then the
    nearest perpendicular, then the nearest along the direction of movement. That
    way "right" from a wide zone leads into the strip on the right (not into the
    zone below-right), and from the middle of the grid — into the neighbouring
    cell. An unknown direction changes nothing.
    """
    if not zones:
        return None
    index = clamp_index(zones, index)
    if direction not in DIRECTIONS:
        return index
    sign = 1.0 if direction in ("right", "down") else -1.0
    axis = 0 if direction in ("left", "right") else 1
    cur = center(zones[index])
    best = None
    best_key = None
    for i, z in enumerate(zones):
        if i == index:
            continue
        c = center(z)
        along = sign * (c[axis] - cur[axis])
        if along <= EPS:  # the centre is not in that direction — not a neighbour
            continue
        perp = abs(c[1 - axis] - cur[1 - axis])
        has_overlap = _overlap(z, zones[index], axis) > EPS
        key = (0 if has_overlap else 1, perp, along, i)
        if best_key is None or key < best_key:
            best_key = key
            best = i
    return index if best is None else best


def zone_at(zones, fx, fy):
    """Index of the zone under the point ``(fx, fy)`` in work-area fractions.

    ``None`` if the point fell into no zone (into a "gap" between free rectangles,
    for example). Zones may overlap: the one under the cursor is the one drawn
    later (last in the list) — as in the editor.
    """
    for i in range(len(zones) - 1, -1, -1):
        x, y, w, h = rect(zones[i])
        if x - EPS <= fx < x + w + EPS and y - EPS <= fy < y + h + EPS:
            return i
    return None


def zone_at_point(zones, area, point):
    """Index of the zone under the screen point ``point = (x, y)`` in pixels.

    ``area`` is the monitor work area ``(x, y, w, h)`` in the same screen
    coordinates as the point: the overlay (and `--hud`) lives exactly in it. The
    conversion "screen pixels → work-area fractions" is the only thing here beyond
    :func:`zone_at`, so the zone under the cursor and the zone under the point sent
    by the watcher are picked by the same code
    (``zone-picker.Picker._initial_selection`` and the HUD).

    ``None`` means the point is outside the work area or in a gap between zones.
    """
    ax, ay, aw, ah = (float(v) for v in area)
    px, py = (float(v) for v in point)
    return zone_at(zones, (px - ax) / max(1.0, aw), (py - ay) / max(1.0, ah))

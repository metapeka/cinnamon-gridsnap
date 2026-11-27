#!/usr/bin/env python3
"""The layout menus' pure half: a zone set's shape and the numbers beside it.

The Templates and Profiles menus show a layout instead of its name alone: a thumbnail
built from the very rectangles the canvas paints, and one compact line of counts.
Both live here, without GTK or X, so the menu data is tested without a display, and the
editor keeps to widgets and events.

A zone is anything with ``x``, ``y``, ``w``, ``h`` fractions — :class:`zone_config.Zone`
with its bindings, a gallery card read through the same model, or a plain object with
the same fields.
"""

THUMB_INSET = 2.0  # the zones stay inside this frame, so the box outline reads as a frame
THUMB_GAP = 1.0    # a hair between neighbours: a 2×2 must not read as one block

EN_DASH = "\u2013"


def thumb_rects(zones, width, height, inset=THUMB_INSET, gap=THUMB_GAP):
    """``[(x, y, w, h)]`` of the zones inside a ``width × height`` box.

    Fractions map straight onto the box, and every rectangle loses ``gap`` pixels from
    its right and bottom edges: neighbouring zones are then separated by a hair instead
    of merging into one block. An empty set gives an empty list -- the caller draws the
    frame alone, which is what "blank slate" needs.
    """
    inner_w = max(1.0, width - 2 * inset)
    inner_h = max(1.0, height - 2 * inset)
    rects = []
    for zone in zones:
        x = inset + zone.x * inner_w
        y = inset + zone.y * inner_h
        w = max(1.0, zone.w * inner_w - gap)
        h = max(1.0, zone.h * inner_h - gap)
        rects.append((x, y, w, h))
    return rects


def compact_ranges(numbers):
    """``[1, 2, 3, 5]`` → ``"1–3, 5"``: workspace or monitor numbers without a wall of digits.

    Duplicates are dropped and the rest is sorted, so the label does not depend on the
    order the config happens to keep. An empty sequence gives an empty string — the
    caller then leaves the clause out instead of printing a bare dash.
    """
    items = sorted({int(number) for number in numbers})
    if not items:
        return ""
    parts = []
    start = previous = items[0]
    for number in items[1:]:
        if number == previous + 1:
            previous = number
            continue
        parts.append(_span(start, previous))
        start = previous = number
    parts.append(_span(start, previous))
    return ", ".join(parts)


def _span(start, end):
    """One run of numbers: ``"3"``, or ``"1–3"`` when it covers more than one."""
    if end > start:
        return f"{start}{EN_DASH}{end}"
    return str(start)

#!/usr/bin/env python3
"""zone_units — the single place that converts GTK logical pixels to physical X ones.

Why two spaces: GTK (and `Gdk.Monitor.get_workarea()`) counts in **logical**
pixels — with `GDK_SCALE=2` a 1920x1080 monitor looks like 960x540, — while raw
X11, which the tiler moves windows through, counts in **physical** ones. Zone
fractions are dimensionless, so it is not the fractions that diverge but what they
are counted from: 0.8358 of 960 gives 802, and of 1920 — 1605.

The rule: zone arithmetic is in physical pixels (that is what goes to X), while
logical ones are needed wherever GTK reads or sets coordinates — the picker overlay
window, the HUD window, the tabstrip. Both sides are counted by this module, so the
`round` in the conversion is one for the whole program.

Fractional scale: `GDK_SCALE` in GTK3 is an integer, and Cinnamon's fractional
scale on X11 is done by stretching the output, where `_NET_WORKAREA` and GDK are
already in the same (scaled) pixels and there is nothing to convert. That is why
there is no separate rounding rule for 1.5 here: edges are converted from common
lines — `round` from the left and from the right edge, the width being their
difference — and at any scale neighbouring zones stay gapless, by the same rule as
`Grid._px`.

The module is pure: no GTK, no X, no subprocesses — only arithmetic over
`(x, y, w, h)` tuples; the caller passes the scale in.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def scale_value(scale):
    """Scale as an integer of at least 1; `None`, 0 and garbage mean 1.

    No scale (plain screen, GDK did not answer) is exactly 1, not an error: the
    conversion then changes nothing, and the caller needs no special branch.
    """
    try:
        value = int(scale)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def physical_rect(rect, scale):
    """Logical frame `(x, y, w, h)` → physical.

    Edges are counted from the same lines: `x1 = round(x*s)`,
    `x2 = round((x+w)*s)`, the width being their difference. That way neighbouring
    frames that touch in logical pixels stay touching in physical ones too.
    """
    x, y, w, h = rect
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y), int(w), int(h))
    x1, x2 = round(x * s), round((x + w) * s)
    y1, y2 = round(y * s), round((y + h) * s)
    return (x1, y1, x2 - x1, y2 - y1)


def logical_rect(rect, scale):
    """Physical frame `(x, y, w, h)` → logical; inverse of :func:`physical_rect`."""
    x, y, w, h = rect
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y), int(w), int(h))
    x1, x2 = round(x / s), round((x + w) / s)
    y1, y2 = round(y / s), round((y + h) / s)
    return (x1, y1, x2 - x1, y2 - y1)


def physical_point(point, scale):
    """Logical point `(x, y)` → physical."""
    x, y = point
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y))
    return (round(x * s), round(y * s))


def logical_point(point, scale):
    """Physical point `(x, y)` → logical."""
    x, y = point
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y))
    return (round(x / s), round(y / s))


def scale_at(scales, index):
    """Scale of monitor `index` from the `scales` list; no such entry means 1.

    A list of scales shorter than the list of areas is the usual thing when GDK is
    unavailable (the `_NET_WORKAREA` fallback): then there is no scale and the
    conversion changes nothing.
    """
    try:
        if 0 <= index < len(scales):
            return scale_value(scales[index])
    except TypeError:
        pass
    return 1


def scale_for_point(areas, scales, point):
    """Scale of the monitor whose area the physical point fell into.

    A point outside every area (a seam between monitors, a window dragged past the
    edge) gets the scale of the one nearest by centre, the way
    `zone-tiler.monitor_for_point` picks a monitor; no areas at all mean 1.
    """
    if not areas:
        return 1
    x, y = point
    best, best_d = 0, None
    for i, (ax, ay, aw, ah) in enumerate(areas):
        if ax <= x < ax + aw and ay <= y < ay + ah:
            return scale_at(scales, i)
        dx = (ax + aw / 2.0) - x
        dy = (ay + ah / 2.0) - y
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = i, d
    return scale_at(scales, best)

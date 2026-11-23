#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_reflow — pure reflow geometry for GridSnap (no X11 and no GTK).

When a window placed by GridSnap closes and its zone is freed, a neighbouring
occupied zone grows into the freed area — if the union of the two zones is a
rectangle again. It works in work-area fractions (0..1), like `zones.conf`.

The public function is one: `reflow(zones, freed, occupied)`.
"""

# Tolerance for comparing fractions. Fractions come from the config and from 0..1
# arithmetic, so exact float equality is not enough.
EPS = 1e-9


def _close(a, b):
    """Are fractions `a` and `b` equal within EPS."""
    return abs(a - b) <= EPS


def _union(a, b):
    """Bounding box of two rects (x, y, w, h)."""
    x = min(a[0], b[0])
    y = min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    return (x, y, x2 - x, y2 - y)


def _shared_edge(freed, cand):
    """Length of the shared edge of `freed` and `cand`, if they are neighbours
    with a rectangular union; otherwise None.

    Vertical neighbours (one above the other) — x and w match, the y ranges meet;
    the shared edge is horizontal, its length equals w.
    Horizontal neighbours (side by side) — y and h match, the x ranges meet;
    the shared edge is vertical, its length equals h.
    """
    fx, fy, fw, fh = freed
    cx, cy, cw, ch = cand

    if _close(fx, cx) and _close(fw, cw):
        if _close(fy + fh, cy) or _close(cy + ch, fy):
            return fw

    if _close(fy, cy) and _close(fh, ch):
        if _close(fx + fw, cx) or _close(cx + cw, fx):
            return fh

    return None


def reflow(zones, freed, occupied):
    """Return {zone: (x, y, w, h)} — the new rects after `freed` is released.

    zones:    {name: (x, y, w, h)} in fractions 0..1
    freed:    name of the freed zone
    occupied: set of names of occupied zones

    A candidate is an occupied zone that shares an edge with `freed` and whose
    union with it is a rectangle. The candidate with the longest shared edge wins;
    on equal length the lexicographically smaller name does. v1 has no cascade:
    exactly one expanded rect is returned (or {}).
    """
    if freed not in zones or freed in occupied:
        return {}

    src = zones[freed]
    best_name = None
    best_edge = None

    for name in occupied:
        if name == freed or name not in zones:
            continue
        edge = _shared_edge(src, zones[name])
        if edge is None:
            continue
        if (
            best_name is None
            or edge > best_edge + EPS
            or (abs(edge - best_edge) <= EPS and name < best_name)
        ):
            best_name, best_edge = name, edge

    if best_name is None:
        return {}
    return {best_name: _union(src, zones[best_name])}

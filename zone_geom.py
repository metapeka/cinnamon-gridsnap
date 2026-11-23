#!/usr/bin/env python3
"""zone_geom — pure zone-editor geometry (no GTK and no X).

Moved out of `zone-editor.py` so that the calculations of moving, resizing,
snapping and bounds are checked by unit tests without an X server. Coordinates are
work-area fractions (0..1), as in `zones.conf`.

The module imports neither GTK nor `zone_config`: it works with anything that has
`x, y, w, h` attributes and with `(x, y, w, h)` tuples.

The pure rules of per-monitor zone sets live here too: which set to show for a
monitor (`set_for_monitor`), what to write in the status (`set_origin_text`) and
which `[zones:<output>]` sections to write (`save_plan`). They are not about
pixels, but they live in this module for the same reason: `zone-editor.py` pulls
in GTK, while these decisions are needed by unit tests without X and without GTK.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zone_i18n import _  # noqa: E402  (UI strings and their translation)

MIN_SIZE = 0.05          # minimum zone size (fraction of the screen)
SNAP_PX = 9              # snapping, pixels
MARGIN = 26              # editor canvas margins around the "screen", pixels
EDGE_PX = 7              # edge/corner grab band, pixels
SLACK_PX = 4             # tolerance for hitting a zone past its border, pixels
GUIDES = [0.0, 0.25, 1 / 3, 0.5, 2 / 3, 0.75, 1.0]


def _xywh(rect):
    """Frame as (x, y, w, h): from an object with attributes and from a tuple alike."""
    if hasattr(rect, "x"):
        return rect.x, rect.y, rect.w, rect.h
    return tuple(rect)


def screen_rect(canvas_w, canvas_h, workarea, margin=MARGIN):
    """The rectangle of the "screen" on the canvas: (sx, sy, sw, sh) with margins and centring."""
    _, _, mw, mh = workarea
    avail_w = max(50, canvas_w - 2 * margin)
    avail_h = max(50, canvas_h - 2 * margin)
    scale = min(avail_w / mw, avail_h / mh)
    w, h = mw * scale, mh * scale
    return ((canvas_w - w) / 2, (canvas_h - h) / 2, w, h)


def to_px(screen, rect):
    """Fractions -> canvas pixels."""
    sx, sy, sw, sh = screen
    x, y, w, h = _xywh(rect)
    return (sx + x * sw, sy + y * sh, w * sw, h * sh)


def to_frac(screen, px, py):
    """Canvas pixels -> fractions."""
    sx, sy, sw, sh = screen
    return ((px - sx) / sw, (py - sy) / sh)


def snap_tol(screen, axis, snap_px=SNAP_PX):
    """Snap tolerance in fractions along an axis (from pixels)."""
    _, _, sw, sh = screen
    return snap_px / (sw if axis == "x" else sh)


def snap_frac(frac, axis, tol, zones, skip=None):
    """Pull a fraction towards the nearest guide/zone edge within `tol`.

    `skip` is the index of the zone being dragged; its edges are not used as bait.
    """
    cand = list(GUIDES)
    for i, z in enumerate(zones):
        if skip is not None and i == skip:
            continue
        x, y, w, h = _xywh(z)
        if axis == "x":
            cand += [x, x + w]
        else:
            cand += [y, y + h]
    best = None
    for c in cand:
        d = abs(c - frac)
        if d <= tol and (best is None or d < abs(best - frac)):
            best = c
    return frac if best is None else best


def snap_move(pos, size, axis, tol, zones, skip=None):
    """The edge being moved: the left/top edge first, otherwise the right/bottom one."""
    left = snap_frac(pos, axis, tol, zones, skip)
    if abs(left - pos) > 1e-9:
        return min(max(0.0, left), 1.0 - size)
    right = snap_frac(pos + size, axis, tol, zones, skip)
    if abs(right - (pos + size)) > 1e-9:
        return min(max(0.0, right - size), 1.0 - size)
    return pos


def drag_rect(start, mode, edges, start_f, cur_f, zones, tol_x, tol_y,
              skip=None, min_size=MIN_SIZE):
    """The new frame (x, y, w, h) while dragging with the mouse.

    start is the frame at the moment of the press (tuple or object), mode is
    "move" or "resize", edges is a set of sides ("l", "r", "t", "b"),
    start_f/cur_f are the cursor fractions at the start and now.
    """
    x, y, w, h = _xywh(start)
    dx, dy = cur_f[0] - start_f[0], cur_f[1] - start_f[1]

    if mode == "move":
        nx = min(max(0.0, x + dx), 1.0 - w)
        ny = min(max(0.0, y + dy), 1.0 - h)
        return (
            snap_move(nx, w, "x", tol_x, zones, skip),
            snap_move(ny, h, "y", tol_y, zones, skip),
            w,
            h,
        )

    x1, y1, x2, y2 = x, y, x + w, y + h
    if "l" in edges:
        x1 = min(snap_frac(x1 + dx, "x", tol_x, zones, skip), x2 - min_size)
    if "r" in edges:
        x2 = max(snap_frac(x2 + dx, "x", tol_x, zones, skip), x1 + min_size)
    if "t" in edges:
        y1 = min(snap_frac(y1 + dy, "y", tol_y, zones, skip), y2 - min_size)
    if "b" in edges:
        y2 = max(snap_frac(y2 + dy, "y", tol_y, zones, skip), y1 + min_size)
    x1, y1 = max(0.0, x1), max(0.0, y1)
    x2, y2 = min(1.0, x2), min(1.0, y2)
    return (x1, y1, x2 - x1, y2 - y1)


def nudge(rect, dx, dy, resize=False, min_size=MIN_SIZE):
    """Move/resize from the keyboard; the same bounds as with the mouse."""
    x, y, w, h = _xywh(rect)
    if resize:
        w = min(1 - x, max(min_size, w + dx))
        h = min(1 - y, max(min_size, h + dy))
        return (x, y, w, h)
    x = min(max(0.0, x + dx), 1.0 - w)
    y = min(max(0.0, y + dy), 1.0 - h)
    return (x, y, w, h)


def scroll_resize(rect, delta, min_size=MIN_SIZE):
    """Zone width from the mouse wheel: bounds [min_size, 1 - x]."""
    x, y, w, h = _xywh(rect)
    return (x, y, min(1 - x, max(min_size, w + delta)), h)


def hit_test(px, py, screen, zones, edge_px=EDGE_PX, slack_px=SLACK_PX):
    """What is under the cursor: (index, "move"/"resize", sides) or (None, "", set()).

    Zones are checked top to bottom (the last drawn one first), `slack_px` is
    allowed past the border, and the edge/corner grab is `edge_px`.
    """
    for i in reversed(range(len(zones))):
        x, y, w, h = to_px(screen, zones[i])
        if x - slack_px <= px <= x + w + slack_px and y - slack_px <= py <= y + h + slack_px:
            edges = set()
            if abs(px - x) <= edge_px:
                edges.add("l")
            if abs(px - (x + w)) <= edge_px:
                edges.add("r")
            if abs(py - y) <= edge_px:
                edges.add("t")
            if abs(py - (y + h)) <= edge_px:
                edges.add("b")
            if x <= px <= x + w and y <= py <= y + h:
                return i, ("resize" if edges else "move"), edges
            return i, "move", set()
    return None, "", set()


# ------------------------------------------------- zone sets per monitor
#
# The default set lives in `[zones]`, a monitor set in `[zones:<output>]`.
# A monitor without its own section inherits the common set, so a section is only
# needed where the set really differs (otherwise the file grows copies of `[zones]`).

SIG_DIGITS = 4           # geometry comparison precision — as in Zone.fmt()


def _zone_fields(zone):
    """`(name, x, y, w, h, binding)` of a zone or tuple — no type requirements."""
    if hasattr(zone, "name"):
        return (
            str(zone.name),
            float(zone.x),
            float(zone.y),
            float(zone.w),
            float(zone.h),
            str(getattr(zone, "binding", "") or ""),
        )
    values = tuple(zone)
    if len(values) < 4:
        raise ValueError(_("zone_geom: the item in the set is not a zone: {zone!r}").format(zone=zone))
    name = str(values[0]) if len(values) > 4 else ""
    x, y, w, h = values[-4], values[-3], values[-2], values[-1]
    return name, float(x), float(y), float(w), float(h), ""


def zone_set_signature(zones):
    """Fingerprint of a set for comparison: names and geometry in file order.

    Bindings are not part of the fingerprint: `[keys]` is shared per file and lives
    by zone name, so a set with the same zones and different keys is the same set.
    """
    out = []
    for zone in zones:
        name, x, y, w, h, _binding = _zone_fields(zone)
        out.append((name, round(x, SIG_DIGITS), round(y, SIG_DIGITS),
                    round(w, SIG_DIGITS), round(h, SIG_DIGITS)))
    return tuple(out)


def set_for_monitor(monitor, monitor_sets, default_zones):
    """The set to show for a monitor: ``(zones, whether the section is its own)``.

    A monitor set exists — its copy and ``True`` (its own); it does not — a copy of
    the common ``[zones]`` and ``False`` (inherits). The copies are mandatory:
    editing one monitor's canvas must not silently change another monitor's set.
    """
    own = (monitor_sets or {}).get(monitor)
    if own:
        return [zone.copy() for zone in own], True
    return [zone.copy() for zone in default_zones], False


def set_origin_text(monitor, own, shared=False, offline=False):
    """What is shown on the canvas — one line for the editor status.

    ``shared`` is the common `[zones]` set, ``own`` is the monitor's own section,
    otherwise the set is inherited. A disabled output is marked separately.
    """
    if shared:
        text = _("default [zones] set - the other monitors inherit it")
    elif own:
        text = _("own [zones:{monitor}] set").format(monitor=monitor)
    else:
        text = _("inherits [zones]")
    if offline:
        return _("{text} · not on screen").format(text=text)
    return text


def _binding_of(zone):
    """Zone binding (``[keys]``) as a string; a tuple has none."""
    return str(getattr(zone, "binding", "") or "").strip()


def _without_seen_bindings(zones, seen):
    """Copies of zones whose name already appeared in `[keys]`: the binding is cleared.

    `[keys]` is one per file, so two sections with a zone of the same name must not
    write the key twice. The sets themselves are not changed — copies are written.
    """
    out = []
    for zone in zones:
        copy = zone.copy()
        if copy.name in seen:
            copy.binding = ""
        elif _binding_of(copy):
            seen.add(copy.name)
        out.append(copy)
    return out


def save_plan(default_zones, monitor_sets, edited=(), onscreen=None, existing=None):
    """Write plan: ``({monitor: zones}, [monitor])`` — what to write and what to delete.

    A ``[zones:<monitor>]`` section is written if the monitor's set differs from the
    common ``[zones]`` or the monitor was explicitly edited (``edited``); sections
    whose set matched the common one with no edits are deleted — but only for
    monitors on screen (``onscreen``) and only if the section is already in the file
    (``existing``). A disabled output is never deleted: its section is either
    written as is (if it was edited) or left alone. An empty monitor set is not
    written.

    Zones are returned as copies: zones whose name is already in the common set get
    their binding cleared — `[keys]` is shared, and there must be no duplicates in it.
    """
    default_sig = zone_set_signature(default_zones)
    onscreen = None if onscreen is None else set(onscreen)
    existing = set(existing or ())
    seen = {zone.name for zone in default_zones if _binding_of(zone)}
    write, drop = {}, []
    for monitor, zones in (monitor_sets or {}).items():
        if not zones:
            continue
        if monitor in edited or zone_set_signature(zones) != default_sig:
            write[monitor] = _without_seen_bindings(zones, seen)
        elif (onscreen is None or monitor in onscreen) and monitor in existing:
            drop.append(monitor)
    return write, drop


def apply_save_plan(path, default_zones, plan, extra=None):
    """Write `zones.conf` according to the :func:`save_plan` plan; returns the path.

    ``zone_config`` is imported here rather than at the top of the module: geometry
    must not pull in the config layer and `zone-tiler.py` just for an import. The
    file is rewritten in one pass through :func:`zone_config.save_zone_sets`: the
    monitor sets are read from the file once, the sections from ``drop`` are removed
    from them, and the ones to write from ``write`` are added. Foreign sections of
    the file itself (`[rules]`, `[daemon]`, `[zones:*]` of disabled outputs) are
    kept — `save_zone_sets` takes them from the file itself.

    There will be no repeats in `[keys]`: the writer filters them out, so no textual
    cleanup after writing is needed.
    """
    import zone_config as zc  # noqa: PLC0415 — lazy import, see the docstring

    write, drop = plan
    monitors = {
        monitor: zones
        for monitor, zones in zc.load_zone_sets(path).items()
        if monitor is not None
    }
    for monitor in drop:
        monitors.pop(monitor, None)
    for monitor, zones in write.items():
        if zones:
            monitors[monitor] = list(zones)
        else:
            monitors.pop(monitor, None)
    return zc.save_zone_sets(path, list(default_zones), monitors, extra)

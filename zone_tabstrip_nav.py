#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_tabstrip_nav — pure tabstrip layout for ``zone-tabstrip.py``.

Everything the tabstrip does with numbers and strings lives here: parsing the
line-wise snapshot from the daemon, tab frames by strip width, a label fitted to
the tab width and the tab under a click point. The module deliberately imports
neither GTK nor cairo nor X: it can and should be checked with ordinary unit tests
without an X server (a project invariant, as with ``zone_picker_nav``/``zone_tabs``).

What is here:

* :func:`parse_snapshot` — the snapshot line ``{"strips": [...]}`` from the daemon →
  a list of strips; ``None`` means the line is not a snapshot (it is skipped
  silently), ``[]`` is a "nothing to draw" snapshot, and these are different
  things;
* :func:`tab_frames` — tab frames by strip width: evenly, the last tab takes the
  remainder of the division, so tabs do not stick out of the zone;
* :func:`fit_title` — a label cut to the tab width (``…`` instead of the tail).
  There is no scroll and no second line in the strip: extra labels are cut;
* :func:`layout` — from a snapshot record, what is drawn: window, label, frame,
  activity;
* :func:`tab_at` — index of the tab under the click point (the click goes to the
  daemon socket).

Frames are pixel ``(x, y, w, h)`` in screen coordinates, as in
``zone_tabs.strip_record``: the strip is drawn over the desktop.
"""

import json
import math

# Gap between tabs and label padding from the tab edges, px. The strip is narrow,
# so the gap is one pixel: it separates the tabs without eating label width.
TAB_GAP = 1
TAB_PADDING = 8

# The label is cut "by eye", without Pango and cairo: the width of one character
# is estimated by the average for the strip font. An exact calculation would be
# the drawing side's business, and the cut has to happen before it, so as not to
# order a label wider than the tab.
CHAR_WIDTH = 7

# Ellipsis instead of the cut tail of the label.
ELLIPSIS = "…"

EPS = 1e-9


def _rect_values(value):
    """``(x, y, w, h)`` from anything resembling a frame; ``None`` means garbage."""
    try:
        x, y, w, h = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) for v in (x, y, w, h)):
        return None
    return x, y, w, h


def record_rect(record):
    """Strip frame from a snapshot record; ``None`` means there is nothing to draw by it.

    Zero or negative width is ``None`` too: a zero-size window will show no label,
    and drawing it "just in case" means breeding invisible windows over the desktop.
    """
    if not isinstance(record, dict):
        return None
    rect = _rect_values(record.get("rect"))
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect


def _tabs_of(record):
    tabs = record.get("tabs") if isinstance(record, dict) else None
    return [tab for tab in tabs if isinstance(tab, dict)] if isinstance(tabs, list) else []


def parse_snapshot(line):
    """Snapshot from a stdin line → list of strips; ``None`` means this is not a snapshot.

    The snapshot is full, not a delta, so ``[]`` (an empty list) means "there are no
    strips, remove the windows", while ``None`` means "the line was not parsed, do
    not change the state": the daemon may send garbage or a half-written line, and
    the strip must not disappear from the screen because of that. Records without
    tabs and with a broken frame are filtered out: there is nothing to draw by them.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(line, str) or not line.strip():
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    strips = payload.get("strips")
    if not isinstance(strips, list):
        return None
    return [
        record
        for record in strips
        if record_rect(record) is not None and _tabs_of(record)
    ]


def tab_frames(rect, count, gap=TAB_GAP):
    """Frames of ``count`` tabs in the strip `rect` — left to right, without leaving the zone.

    The strip width is divided evenly; the last tab takes the remainder of the
    division, so the sum of frames and gaps is exactly the strip width. When the
    strip is narrower than the gaps between tabs, the gaps are dropped: the tabs
    divide it entirely but still stay within its bounds.
    """
    count = int(count)
    if count <= 0:
        return []
    values = _rect_values(rect)
    if values is None:
        return []
    x, y, w, h = values
    if w <= 0 or h <= 0:
        return []
    gap = max(0.0, float(gap))
    avail = w - gap * (count - 1)
    if avail <= 0:
        gap, avail = 0.0, w
    step = avail / count
    frames, left = [], x
    for index in range(count):
        right = x + w if index == count - 1 else left + step
        frames.append((left, y, max(0.0, right - left), h))
        left = right + gap
    return frames


def label_width(frame, padding=TAB_PADDING):
    """How many pixels a label gets inside the tab `frame` (may be 0)."""
    return max(0.0, float(frame[2]) - 2 * max(0.0, float(padding)))


def fit_title(title, width, char_width=CHAR_WIDTH):
    """A label that fits into `width` pixels: the tail is replaced with ``…``.

    The character width is estimated by an average (`char_width`), not measured:
    exact text layout is the drawing side's business, while the decision "how many
    characters to keep" is made here, before it. If even the ellipsis does not fit,
    there is no label.
    """
    title = "" if title is None else str(title)
    width = float(width)
    if width <= 0 or char_width <= 0 or not title:
        return ""
    max_chars = int(width // char_width)
    if max_chars <= 0:
        return ""
    if len(title) <= max_chars:
        return title
    if max_chars == 1:
        return ELLIPSIS
    return title[: max_chars - 1] + ELLIPSIS


def layout(record):
    """What to draw by a snapshot record: ``[{window, title, label, active, rect}]``.

    ``[]`` means there is nothing to draw (a strip without tabs or its frame was not
    parsed). The labels are already cut to the width of their tab, so the drawing
    side has to count neither font width nor gaps.
    """
    rect = record_rect(record)
    if rect is None:
        return []
    tabs = _tabs_of(record)
    frames = tab_frames(rect, len(tabs))
    rows = []
    for tab, frame in zip(tabs, frames):
        title = tab.get("title")
        rows.append(
            {
                "window": tab.get("window"),
                "title": "" if title is None else str(title),
                "label": fit_title(title, label_width(frame)),
                "active": bool(tab.get("active")),
                "rect": frame,
            }
        )
    return rows


def tab_at(frames, point):
    """Index of the tab under the point ``point = (x, y)``; ``None`` means past the tabs.

    The point is in the same coordinates as the frames (screen pixels). Neighbouring
    tabs share a border, so the left edge of a tab belongs to it while the right one
    does not — as in ``zone_picker_nav.zone_at``: a click on the border cannot fall
    into two tabs at once.
    """
    if not frames or point is None:
        return None
    try:
        px, py = (float(v) for v in point)
    except (TypeError, ValueError):
        return None
    for index, (x, y, w, h) in enumerate(frames):
        if x - EPS <= px < x + w - EPS and y - EPS <= py < y + h - EPS:
            return index
    return None


def tab_under(record, point):
    """Window under the click point; ``None`` means the point is past the tabs or there is no window.

    One function per click: from a snapshot record it counts both the frames and the
    hit, so that ``zone-tabstrip.py`` does not assemble them itself piece by piece
    and drift away from the tests.
    """
    rows = layout(record)
    index = tab_at([row["rect"] for row in rows], point)
    return None if index is None else rows[index]["window"]

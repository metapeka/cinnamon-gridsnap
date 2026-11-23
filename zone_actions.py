#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_actions — the pure core of window and zone actions for GridSnap.

An action is what the user bound to a key: "minimize the others", "show the
zone window", "mirror", "terminal in the zone" — and in general any command of
theirs. The record lives in `zones.conf` in the ``[action:<name>]`` section (see
``zone_config.load_actions``), and is started by the ``zone-tiler.py action
<name>`` command from the keybinding that ``setup-hotkeys.py`` installs.

There is one contract for all actions: **an action gets a window and a zone** — the window, the zone,
its pixel frame, the monitor, the workspace, the window class and title, the path to the config.
It is handed over through two channels (:class:`ActionContext`): the environment variables
``GRIDSNAP_*`` and the ``{zone}``/``{window}``/``{x}``… substitutions in the command
arguments. Both channels are described in :meth:`ActionContext.env` and
:meth:`ActionContext.values` — that is the public API that the documentation
and other people's scripts rely on.

The module is pure: no X, no GTK, no socket — only frame arithmetic, command
parsing and substitutions. That is why it is covered by ordinary unit tests without
an X server, like ``zone_tabs``/``zone_reflow``/``zone_picker_nav``. Everything
that touches windows (minimize, raise, start a process) lives in ``zone-tiler.py``.
"""

import math
import re
import shlex
from dataclasses import dataclass

import zone_tabs

from zone_i18n import _  # interface strings; without a catalog — the source text

# Built-in actions are written into `[action:<name>] run` as `builtin:<name>`:
# so the same record can be both a command of its own and a ready-made action
# of the project, and the core does not confuse one with the other.
BUILTIN_PREFIX = "builtin:"

# Names of the built-in actions. `minimize-others` is the same mode as `focus`
# (the window into the zone, the rest of the workspace and monitor minimized, a second
# press restores it); `show-window` — show the window of the zone; `mirror` — swap
# with the mirror zone; `terminal` — a terminal in the zone.
MINIMIZE_OTHERS = "minimize-others"
SHOW_WINDOW = "show-window"
MIRROR = "mirror"
TERMINAL = "terminal"
BUILTINS = (MINIMIZE_OTHERS, SHOW_WINDOW, MIRROR, TERMINAL)

# The default `builtin:terminal` terminal: the Debian alternatives that stand for
# a desktop terminal. Overridden by the `command` field of the record.
DEFAULT_TERMINAL = "x-terminal-emulator"

# The mirror axis: `horizontal` — left↔right (the default), `vertical` — top↔bottom.
MIRROR_AXES = ("horizontal", "vertical")
DEFAULT_MIRROR_AXIS = "horizontal"

# The prefix of the environment variables and the substitution names — one list of names
# for both channels, so that they do not drift apart.
ENV_PREFIX = "GRIDSNAP_"
PLACEHOLDERS = (
    "window",
    "zone",
    "x",
    "y",
    "w",
    "h",
    "monitor",
    "workspace",
    "class",
    "title",
    "conf",
)

# A substitution in the command arguments: `{name}`. Only the known names are
# replaced (PLACEHOLDERS); an unknown `{something}` is a config error, not a silent
# emptiness: a typo in someone else's script would otherwise look like "an empty argument".
# A name is a word of letters, digits, `_` and `-` (Cyrillic letters count as letters): a name
# typed in Cyrillic gives the same error as `{zon}`. The pattern does not touch what is not
# a substitution: `$` before the brace is a shell variable (`${HOME}`), `{}` from
# `find -exec`, and `{print $1}` in `awk` is not a word (a space and `$`).
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([^\W\d][\w-]*)\}")


class ActionError(Exception):
    """An action refusal: an unknown built-in name, a broken command, no mirror.

    The config parsing wraps it into code 3 (`zone_config.load_actions`), and a
    refusal on the state of a window (no zone, nothing to show) is reported by the tiler
    itself — only what does not depend on X belongs here.
    """


@dataclass(frozen=True)
class ActionContext:
    """What an action is told about the window and the zone.

    The fields are empty (`None`/`""`) when unknown: a window outside the zones has no
    `zone` and `rect`; an action without a zone has no `monitor`; there is no window at
    all (an empty zone) — no `window`, `wm_class` and `wm_title`.

    `rect` — the pixel frame of the zone `(x, y, w, h)` on its monitor: what a
    command needs to place a window or start a terminal in the zone without counting
    the fractions of the zone itself.
    """

    window: int | None = None
    zone: str | None = None
    rect: tuple | None = None
    monitor: str | None = None
    workspace: int | None = None
    wm_class: str = ""
    wm_title: str = ""
    conf: str = ""

    def values(self):
        """``{substitution name: value}`` — for `{zone}`, `{window}` and the rest.

        An unknown value is an empty string, not "None": the substitution lands in
        someone else's command, and there must be "nothing" visible, not an invented word.
        The window is given in hex (`0x1a00007`) — in the same form the tiler and the
        daemon print it.
        """
        x, y, w, h = self.rect if self.rect else ("", "", "", "")
        return {
            "window": "" if self.window is None else hex(self.window),
            "zone": self.zone or "",
            "x": str(x),
            "y": str(y),
            "w": str(w),
            "h": str(h),
            "monitor": self.monitor or "",
            "workspace": "" if self.workspace is None else str(self.workspace),
            "class": self.wm_class or "",
            "title": self.wm_title or "",
            "conf": self.conf or "",
        }

    def env(self):
        """The environment for an external command: ``GRIDSNAP_*`` on top of the current one.

        The names are `GRIDSNAP_WINDOW`, `GRIDSNAP_ZONE`, `GRIDSNAP_X`, `GRIDSNAP_Y`,
        `GRIDSNAP_W`, `GRIDSNAP_H`, `GRIDSNAP_MONITOR`, `GRIDSNAP_WORKSPACE`,
        `GRIDSNAP_CLASS`, `GRIDSNAP_TITLE`, `GRIDSNAP_CONF`; the values are the same as
        in :meth:`values` (an empty string means "unknown").
        """
        return {ENV_PREFIX + name.upper(): value for name, value in self.values().items()}


def is_builtin(run):
    """Does ``run`` start with ``builtin:`` — a built-in action or a command.

    The name is not checked here: :func:`builtin_name` is responsible for it, so that
    "this is built-in" and "there is no such built-in" get different messages.
    """
    return str(run or "").strip().lower().startswith(BUILTIN_PREFIX)


def builtin_name(run):
    """The name of the built-in action from ``run``, or ``None`` if it is a command.

    ``"builtin:mirror"`` → ``"mirror"``; ``"notify-send …"`` → ``None``.
    An unknown name (``builtin:<something>``) is an :class:`ActionError` with the list of
    the known ones: a typo must not silently leave a person without an action.
    """
    text = str(run or "").strip()
    if not is_builtin(text):
        return None
    name = text[len(BUILTIN_PREFIX):].strip().lower()
    if name not in BUILTINS:
        raise ActionError(
            _(
                "unknown built-in action 'builtin:{name}': available {builtins}"
            ).format(
                name=name,
                builtins=", ".join(f"builtin:{builtin}" for builtin in BUILTINS),
            )
        )
    return name


def expand_command(command, context, splitter=shlex.split):
    """A command from the config → ``argv`` with the context substituted in.

    The parsing is `shlex.split` (there is no shell: no ``|``, no ``>``, no ``$VAR``; whoever
    needs a shell writes ``sh -c "…"`` themselves), then the substitutions in every
    argument. An empty command and an unknown substitution ``{something}`` are an
    :class:`ActionError`; missing values give an empty string.
    """
    text = str(command or "").strip()
    if not text:
        raise ActionError(_("the action has an empty command in run"))
    try:
        tokens = splitter(text)
    except ValueError as exc:
        raise ActionError(
            _("failed to parse the action command: {error}").format(error=exc)
        ) from None
    if not tokens:
        raise ActionError(_("the action has an empty command in run"))
    values = context.values()
    return [_expand_token(token, values) for token in tokens]


def _expand_token(token, values):
    """Substitute ``{name}`` in one argument; an unknown name is an `ActionError`."""

    def replace(match):
        name = match.group(1)
        if name not in values:
            raise ActionError(
                _(
                    "unknown substitution '{{{name}}}' in the action command: available {known}"
                ).format(
                    name=name,
                    known=", ".join("{" + known + "}" for known in PLACEHOLDERS),
                )
            )
        return values[name]

    return _PLACEHOLDER_RE.sub(replace, token)


def mirror_fractions(rect, axis=DEFAULT_MIRROR_AXIS):
    """The mirror frame in fractions: about the centre of the work area along the axis.

    ``horizontal`` (the default) — left↔right: ``(1 - x - w, y, w, h)``;
    ``vertical`` — top↔bottom: ``(x, 1 - y - h, w, h)``. An unknown axis is an
    :class:`ActionError`. The frame is needed to find the partner zone by geometry
    (:func:`find_mirror`), not by name: a person invents the zone names themselves.
    """
    axis = _mirror_axis(axis)
    x, y, w, h = (float(v) for v in rect)
    if axis == "vertical":
        return (x, 1.0 - y - h, w, h)
    return (1.0 - x - w, y, w, h)


def _mirror_axis(axis):
    """The mirror axis from the config: `horizontal`/`vertical`, otherwise an `ActionError`."""
    name = str(axis or DEFAULT_MIRROR_AXIS).strip().lower()
    if name not in MIRROR_AXES:
        raise ActionError(
            _("unknown mirror axis '{axis}': available {axes}").format(
                axis=name, axes=", ".join(MIRROR_AXES)
            )
        )
    return name


def find_mirror(zones, subject, axis=DEFAULT_MIRROR_AXIS):
    """The name of the mirror zone for ``subject``, or ``None`` if there is none.

    ``zones`` — ``{name: (x, y, w, h)}`` in fractions of the work area of one monitor,
    in grid order. The mirror frame is computed by :func:`mirror_fractions`, and the
    partner zone is the one with the **largest intersection area** with it; ties are
    decided by the closeness of the centres, then by the order of the zones.
    `subject` itself is not a candidate: the mirror of a full-screen zone is that same zone,
    and swapping with itself does nothing. No intersection with any zone — ``None``:
    "there is no mirror zone in this grid" is an honest refusal, not a guess.
    An unknown `subject` name is an :class:`ActionError`.
    """
    if subject not in zones:
        raise ActionError(
            _("no zone '{zone}' in the grid - nothing to mirror").format(zone=subject)
        )
    target = mirror_fractions(zones[subject], axis)
    tx, ty = _center(target)
    best = None
    for order, (name, rect) in enumerate(zones.items()):
        if name == subject:
            continue
        area = _intersection_area(rect, target)
        if area <= 0:
            continue
        cx, cy = _center(rect)
        # Smaller is better: first the area (with a minus), then the distance, then
        # the grid order: two equal candidates must be resolved the same way
        # in every run, and not the way the dict happens to lie.
        rank = (-area, math.hypot(cx - tx, cy - ty), order)
        if best is None or rank < best[0]:
            best = (rank, name)
    return None if best is None else best[1]


def _center(rect):
    """The centre of the frame in fractions: ``(x + w/2, y + h/2)``."""
    x, y, w, h = (float(v) for v in rect)
    return (x + w / 2.0, y + h / 2.0)


def _intersection_area(left, right):
    """The intersection area of two frames in fractions; they do not intersect — zero."""
    lx, ly, lw, lh = (float(v) for v in left)
    rx, ry, rw, rh = (float(v) for v in right)
    width = min(lx + lw, rx + rw) - max(lx, rx)
    height = min(ly + lh, ry + rh) - max(ly, ry)
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def zone_windows(frames, zone_rect, tab_rect, tolerance=zone_tabs.FRAME_TOLERANCE):
    """The windows standing in the zone — in the order of the given frame dict.

    A window counts as standing in the zone if its frame matched the zone frame as a
    whole (one window in the zone) **or** matched the tab frame (several windows of the
    zone stand on the shared frame). The comparison is per side with a ``tolerance``:
    the window manager places the frame, exact float equality is not enough (the same
    meaning and the same value as in `zone_tabs.FRAME_TOLERANCE`). The frame was not read
    (``None``) — the window is skipped.
    """
    found = []
    for win, frame in frames.items():
        if frame is None:
            continue
        if _same_rect(frame, zone_rect, tolerance) or _same_rect(frame, tab_rect, tolerance):
            found.append(win)
    return found


def _same_rect(left, right, tolerance):
    """Do two frames match with a ``tolerance`` on every side.

    A copy of the rule from `zone_tabs._same_rect` (it is private): here it is needed for
    both "the window in the zone" and "the zone of the window", and pulling a private
    function out of a neighbouring module is worse than duplicating two lines.
    """
    if left is None or right is None:
        return False
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))


def zone_of_frame(frame, zones_px, height=None, tolerance=zone_tabs.FRAME_TOLERANCE):
    """The name of the zone a window with the frame ``frame`` stands in, or ``None``.

    ``zones_px`` — ``{name: (x, y, w, h)}`` in pixels on one monitor, in grid
    order; ``height`` — the height of the tab strip (`[tabs] height`), so that a window
    on the shared tab frame is also taken for "in the zone". The first match in grid
    order — zones do not overlap, and there must not be two answers.
    """
    if frame is None:
        return None
    strip = zone_tabs.STRIP_HEIGHT if height is None else int(height)
    for name, rect in zones_px.items():
        tab = zone_tabs.stack_rect(tuple(float(v) for v in rect), 2, strip)
        if _same_rect(frame, rect, tolerance) or _same_rect(frame, tab, tolerance):
            return name
    return None

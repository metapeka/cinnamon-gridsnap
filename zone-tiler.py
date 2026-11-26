#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-tiler — lays the active window out over the zones of its own (drawn) grid.

Works in Cinnamon / Muffin (X11) through EWMH messages; external utilities
(wmctrl, xdotool) are not needed. Only python3 + libX11 (already on the system).

The grid is set in zones.conf in work-area fractions (panels excluded):

  [grid]
  cols = 0.5, 0.75, 1.0      # vertical lines: 3 columns (0..1, 1..2, 2..3)
  rows = 0.5, 1.0            # horizontal lines: 2 rows (0..1, 1..2)

  [zones]
  left      = 0:1, 0:2       # columns 0..1, rows 0..2
  top-right = 1:3, 0:1

Zones can be set per monitor — with a `[zones:<output>]` section:

  [zones:HDMI-1]
  main = 0, 0, 0.6, 1

The output name comes from `xrandr --listmonitors` (`eDP-1`, `HDMI-1`) and is stable
across reboots and cable reconnects; `[zones]` is mandatory and serves as the
default set for monitors without a section of their own. `[rules]` and `[keys]` are shared.

Moving the active window into a neighbouring zone is the `move <direction>` command, and
its bindings are in the optional `[move]` section (the window's zone is found by its
centre, the neighbour by the same rule as the arrows in the picker):

  [move]
  left  = <Super>Left
  right = <Super>Right
  up    = <Super>Up
  down  = <Super>Down

No section — these same bindings apply (muffin also takes them for its
`push-tile-*`, so we take them as the default). An unknown key is a config
error.

Tabs in a zone — the optional `[tabs]` section: `key` — the binding of "the next
window of the zone" (`tabs-next`), `height` — the height of the tab strip in pixels (22
by default). Several windows in one zone sit on one frame (the zone minus the
strip), the topmost one is visible; the strip is drawn by a separate process `zone-tabstrip.py`.

  [tabs]
  key    = <Super><Alt>g
  height = 22

There is no cascade any more: `--offset` and the stack shift were removed, a call with
`--offset` is an argument error (code 2).

Commands:
  zone-tiler.py list                      # show the zones and their pixel frames
  zone-tiler.py apply left                # put the active window into the zone "left"
  zone-tiler.py apply left --dry-run      # only compute the frame
  zone-tiler.py apply left --monitor 1    # a zone on another monitor
  zone-tiler.py apply left --monitor HDMI-1   # or by output name (takes priority over the number)
  zone-tiler.py apply left --window 0x3400007
  zone-tiler.py apply left --workspace 1  # the grid of desktop 1
  zone-tiler.py move left                 # the active window — into the neighbouring zone on the left
  zone-tiler.py keys --move               # move bindings (for setup-hotkeys.py)
  zone-tiler.py arrange                   # lay the windows out over the zones (by rules and the rest)
  zone-tiler.py arrange --by order        # lay the windows out over the zones in list order
  zone-tiler.py stack main                # gather the windows into the zone main as tabs
  zone-tiler.py tabs-next                 # raise the next window of the zone (a key)
  zone-tiler.py action mirror              # run an action from [action:<name>]
  zone-tiler.py action term-main --dry-run # what the action would do, doing nothing
  zone-tiler.py undo                      # bring the windows back from the last operation
  zone-tiler.py undo --all                # roll the whole layout history back

A grid of its own per workspace is a profile: the `[workspaces]` section maps
a desktop number (from 1) to a profile name from `templates/`. `list`/`apply`/`arrange`/`stack`
take the grid of the active desktop by default (`--workspace current`), `--workspace N`
addresses a desktop by EWMH number (from zero). The zone name stays global, `[keys]`
is shared for the file.

Layout profiles (own templates: zones + rules + keys) are stored in
`~/.config/gridsnap/templates/*.conf`, activation is `profile use <name>`:

  zone-tiler.py profile list              # saved profiles
  zone-tiler.py profile save work         # the current zones.conf — into the "work" profile
  zone-tiler.py profile use work          # activate the profile (and update the bindings)
  zone-tiler.py profile use work --no-hotkeys
  zone-tiler.py profile use-monitors      # a snapshot of the screen profiles from [monitors]
  zone-tiler.py profile use-monitors --dry-run   # show the config, write nothing
  zone-tiler.py profile show work
  zone-tiler.py profile rm work
  zone-tiler.py profile export work              # JSON to stdout (for pipes)
  zone-tiler.py profile export work work.json    # JSON to a file — share the layout
  zone-tiler.py profile import work.json         # JSON → templates/<name>.conf
  zone-tiler.py profile import work.json --name home  # the same, but under another name

A grid of its own per screen — a binding in the `[monitors]` section (`HDMI-1 = work`):
`profile use-monitors` builds a snapshot out of the screen profiles (`[zones]` of the first
screen plus `[zones:<output>]` of the others, merging `[rules]`/`[ignore]`/`[keys]`),
and `apply`/`move`/`arrange`/`stack` look for the zone in the screen profile (for `move`,
`arrange` — the screen of the window, for `apply`/`stack` — the one set by `--monitor`),
then the desktop, then `zones.conf`.

A JSON profile is portable exchange between machines (``gridsnap_profile``: 1);
the main one stays ``.conf``, JSON only for export and import.

An action is an `[action:<name>]` entry in `zones.conf`: `run = builtin:<name>` or
its own command, the optional `zone` (the subject zone) and `key` (the binding, it
is set by `setup-hotkeys.py`). The action receives the window and the zone: through the
`GRIDSNAP_*` variables and the `{zone}`/`{window}`/`{x}`… substitutions. Its own command
runs without a shell and without waiting; the builtins are `minimize-others`,
`show-window`, `mirror`, `terminal` (details — in the --help of the `action` command).

The layout history is JSON in the XDG state (`~/.local/state/gridsnap/history.json`),
every command reads and writes it in a separate process (details — in --help and in
`docs/specs/zone-tiler.md`).
"""

import argparse
import collections
import configparser
import contextlib
import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

from zone_i18n import _  # interface strings; without a catalog — the source text
import window_backend  # window layer: xprop/xwininfo/xrandr and libX11 — in one place
import zone_units  # converting GTK logical pixels to physical X ones — in one place

VERSION = "0.3.0"

# CLI return codes: 0 — success, 1 — runtime error, 2 — argument error,
# 3 — config error (argparse itself reports argument errors with code 2).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3


def fail(message, code=EXIT_FAIL) -> NoReturn:
    """Report an error in stderr and exit the process with the needed code."""
    print(message, file=sys.stderr)
    sys.exit(code)


HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY_CONF = os.path.join(HERE, "zones.conf")


def config_home():
    """XDG config directory (both Cinnamon and the other utilities respect it)."""
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def xdg_conf():
    return os.path.join(config_home(), "gridsnap", "zones.conf")


def default_conf():
    """Path to zones.conf.

    Priority — ~/.config/gridsnap/zones.conf; if it is missing, we read/write the file next
    to the code (compatibility with working installs). Overridden by the --conf flag.
    """
    path = xdg_conf()
    return path if os.path.exists(path) else LEGACY_CONF


CONF = default_conf()

# ------------------------------------------------------------- window rules


def window_class_title(win):
    """WM_CLASS (the class, the second line) and the window title — see `window_backend`."""
    return window_backend.window_class_title(win)


def selector_matches(selector, wm_class, wm_title):
    """Does the window fit one selector. A pure function — for the tests.

    The `re:<regex>` selector is searched in the `class|title` string, case-insensitively;
    a plain selector is a glob over WM_CLASS (also case-insensitively).
    An invalid regex matches nothing. One parse for `[rules]` and
    `[ignore]`, so that rules and exceptions are matched the same way.
    """
    if selector.lower().startswith("re:"):
        try:
            found = re.search(selector[3:], f"{wm_class}|{wm_title}", re.IGNORECASE)
        except re.error:
            return False
        return found is not None
    return fnmatch.fnmatch(wm_class.lower(), selector.lower())


def match_rules(rules, wm_class, wm_title):
    """The first matching zone for the window, or None. A pure function — for the tests."""
    for selector, zone in rules:
        if selector_matches(selector, wm_class, wm_title):
            return zone
    return None


def matches_selectors(selectors, wm_class, wm_title):
    """Whether at least one of the selectors matches. A pure function."""
    return any(selector_matches(s, wm_class, wm_title) for s in (selectors or ()))


# --------------------------------------------------- filters "which windows to move"

# The root property with the list of managed client windows (EWMH).
CLIENT_LIST_ATOM = "_NET_CLIENT_LIST"

# Window types that neither the daemon nor the arrange/stack commands touch: these are not
# standalone "document windows" but popup/service surfaces.
IGNORED_WINDOW_TYPES = frozenset(
    {
        "_NET_WM_WINDOW_TYPE_DIALOG",
        "_NET_WM_WINDOW_TYPE_SPLASH",
        "_NET_WM_WINDOW_TYPE_TOOLTIP",
        "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
        "_NET_WM_WINDOW_TYPE_UTILITY",
    }
)
# Short names of the same types — in case xprop/a test hands them over without the prefix.
IGNORED_TYPE_TAILS = frozenset(name.rsplit("_", 1)[-1] for name in IGNORED_WINDOW_TYPES)

# Window metadata needed by the filters and by the layout.
# `window_type` — a set of `_NET_WM_WINDOW_TYPE_*` names (may be empty).
WindowMeta = collections.namedtuple("WindowMeta", "wm_class wm_title window_type")


def _type_names(window_type):
    """Window type names out of a set/string; a short name is raised as it is."""
    if not window_type:
        return frozenset()
    if isinstance(window_type, str):
        found = re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", window_type)
        return frozenset(found) if found else frozenset({window_type.strip().upper()})
    return frozenset(window_type)


def is_ignored_type(window_type):
    """True if a service type (dialog, splash, etc.) is among the window types.

    The shared filter of the tiler and the daemon. Accepts a set of
    `_NET_WM_WINDOW_TYPE_*` names, a string with them or a short name (`DIALOG`, ...).
    An empty value — not a service window.
    """
    names = _type_names(window_type)
    if names & IGNORED_WINDOW_TYPES:
        return True
    return bool({name.rsplit("_", 1)[-1] for name in names} & IGNORED_TYPE_TAILS)


def window_types(win):
    """The window's `_NET_WM_WINDOW_TYPE` set (empty on error) — see `window_backend`."""
    return window_backend.window_types(win)


def window_excluded(wm_class, wm_title, window_type, ignore_selectors=()):
    """Whether to leave the window alone. A pure function — one source of truth for the filter.

    True if the window type is a service one (`is_ignored_type`) or the window matched at least
    one `[ignore]` selector (a glob over `WM_CLASS`, case-insensitively, or
    `re:<regex>` over the `class|title` string). One predicate for the daemon's auto-layout,
    `arrange`/`stack`, the explicit `apply`/`move` and the picker — that is why the filter does
    not diverge between the call sites.
    """
    if is_ignored_type(window_type):
        return True
    return matches_selectors(ignore_selectors, wm_class, wm_title)


# Delay before the single retry of reading the metadata: `_NET_WM_NAME` sometimes
# appears only after the window has got into `_NET_CLIENT_LIST`.
META_RETRY_DELAY = 0.2


def read_window_meta(win):
    """Window metadata: `WM_CLASS`/`_NET_WM_NAME` + the window type.

    If the class or the title has not appeared yet, re-reads them once after a
    short pause (the title sometimes arrives after the window itself).
    """
    wm_class, wm_title = window_class_title(win)
    if not wm_class or not wm_title:
        time.sleep(META_RETRY_DELAY)
        wm_class2, wm_title2 = window_class_title(win)
        wm_class = wm_class or wm_class2
        wm_title = wm_title or wm_title2
    return WindowMeta(wm_class, wm_title, window_types(win))


def window_is_excluded(win, ignore_selectors=()):
    """Whether the window `win` is excluded (`[ignore]`/service type). Reads X.

    One check for the explicit commands (`apply`, `move`, `undo`) and the picker: the fact for
    the caller, it prints the message itself. It is the same `window_excluded` as the one used
    by the auto-layout, so the filter cannot diverge between the call sites.
    """
    meta = read_window_meta(win)
    return window_excluded(meta.wm_class, meta.wm_title, meta.window_type, ignore_selectors)


def read_client_list():
    """Client windows of `_NET_CLIENT_LIST`, or None — see `window_backend`."""
    return window_backend.read_client_list()


# ---------------------------------------------------------------- grid config


class Grid:
    def __init__(self, path=CONF):
        cp = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            delimiters=("=",),  # the `re:<regex>` key contains ':', so the only delimiter is '='
        )
        try:
            read = cp.read(path, encoding="utf-8")
        except configparser.Error as exc:
            # A repeated section or key: code 3 with the file name, not a traceback.
            fail(
                _(
                    "zone-tiler: cannot parse config {path}: {error}"
                ).format(path=path, error=exc),
                EXIT_CONFIG,
            )
        if not read:
            fail(_("zone-tiler: config not found: {path}").format(path=path), EXIT_CONFIG)
        # The path is needed where the section is not read by Grid itself: `keys --move` takes
        # `[move]` through `zone_config.load_move_keys`, and `[move]` does not live in the desktop
        # profiles (it is shared for the file).
        self.path = path
        self.conf = cp
        self.cols = self._numlist(cp.get("grid", "cols", fallback="1.0"))
        self.rows = self._numlist(cp.get("grid", "rows", fallback="1.0"))
        self.zones = {}
        if cp.has_section("zones"):
            for name, spec in cp.items("zones"):
                self.zones[name.strip()] = self._parse_zone(name, spec)
        # Zones per monitor: [zones:<xrandr output>], for example [zones:HDMI-1].
        # The section name is case-sensitive (ConfigParser does not lowercase sections),
        # so that it matches the output name as xrandr prints it.
        self.monitor_zones = {}
        for section in cp.sections():
            if not section.startswith("zones:"):
                continue
            output = section.split(":", 1)[1].strip()
            if not output:
                fail(
                    _("zone-tiler: empty monitor name in section [{section}]").format(section=section),
                    EXIT_CONFIG,
                )
            zones = {}
            for name, spec in cp.items(section):
                zones[name.strip()] = self._parse_zone(name, spec)
            if not zones:
                fail(
                    _("zone-tiler: section [{section}] has no zones").format(section=section),
                    EXIT_CONFIG,
                )
            self.monitor_zones[output] = zones
        if not self.zones:
            if self.monitor_zones:
                fail(
                    _(
                        "zone-tiler: only [zones:<output>] sections exist, but the required [zones] is missing; [zones] defines the default zone set"
                    ),
                    EXIT_CONFIG,
                )
            fail(_("zone-tiler: no zones in section [zones] of zones.conf"), EXIT_CONFIG)
        # `[rules]` is read by a separate parser: by default ConfigParser
        # lowercases the keys (optionxform=str.lower), which would turn the
        # selector `re:\S+Deploy\S*` into `re:\s+deploy\s*` —
        # `\S` (non-space) would become `\s` (space) and the rule would silently stop
        # firing. The `[zones]`/`[keys]` sections stay with the lowercasing `cp`.
        rules_cp = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
            delimiters=("=",),
        )
        rules_cp.optionxform = str  # type: ignore  # keep the selector case (`re:`)
        rules_cp.read(path, encoding="utf-8")
        self.rules = []
        # The rule is checked against the union of the sets: a zone may be described
        # only for some monitors (then the shared [zones] is taken on the others).
        known = set(self.all_zone_names())
        if rules_cp.has_section("rules"):
            for selector, zone in rules_cp.items("rules"):
                selector, zone = selector.strip(), zone.strip()
                if zone not in known:
                    fail(
                        _("zone-tiler: rule '{selector}' refers to unknown zone '{zone}'").format(
                            selector=selector, zone=zone
                        ),
                        EXIT_CONFIG,
                    )
                self.rules.append((selector, zone))
        # `[ignore]` — the same selectors as in `[rules]`, but without a target: the value is
        # a free-form note for a human/the log and does not affect matching.
        # The section is read by the same parser with `optionxform = str`, so the case of the
        # `re:` regex is kept; the line order is kept for the log.
        self.ignore = []
        if rules_cp.has_section("ignore"):
            for selector, _note in rules_cp.items("ignore"):
                selector = selector.strip()
                if selector:
                    self.ignore.append(selector)

    def all_zone_names(self):
        """Zone names in the union of all sets (the shared one and the per-monitor ones).

        The order is `[zones]` as in the file first, then the zones from `[zones:<output>]`
        that the shared set does not have. Needed to check rules/keys and to pick
        `--monitor` by name.
        """
        names = list(self.zones)
        seen = set(names)
        for zones in self.monitor_zones.values():
            for name in zones:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def zones_for(self, monitor_name=None):
        """The zone set of a monitor: `[zones:<output>]`, or the shared `[zones]`.

        The output name is compared as it is, and on a miss — case-insensitively
        (it is easy to write `[zones:hdmi-1]` in the config). No section for the monitor —
        the default set is returned, that is, the previous behaviour.
        """
        if monitor_name:
            zones = self.monitor_zones.get(monitor_name)
            if zones is not None:
                return zones
            for output, zones in self.monitor_zones.items():
                if output.casefold() == str(monitor_name).casefold():
                    return zones
        return self.zones

    def zone_spec(self, zone, monitor_name=None):
        """The zone description with the monitor in mind: the monitor set, else the shared `[zones]`.

        A rule/key may reference a zone that the set of a specific monitor does not
        have; then, when laying out on that monitor, the zone is taken from the
        shared set. Neither here nor there — a config error, not a traceback.
        """
        spec = self.zones_for(monitor_name).get(zone)
        if spec is None:
            spec = self.zones.get(zone)
        if spec is None:
            if monitor_name:
                fail(
                    _(
                        "zone-tiler: zone '{zone}' is in neither monitor set '{monitor}' nor [zones]"
                    ).format(zone=zone, monitor=monitor_name),
                    EXIT_CONFIG,
                )
            fail(
                _("zone-tiler: zone '{zone}' is not in [zones] and not in any monitor set").format(
                    zone=zone
                ),
                EXIT_CONFIG,
            )
        return spec

    @staticmethod
    def _numlist(value):
        nums = [float(x) for x in re.split(r"[,;\s]+", value.strip()) if x]
        if not nums or abs(nums[-1] - 1.0) > 1e-6:
            fail(_("zone-tiler: the last line in cols/rows must be 1.0"), EXIT_CONFIG)
        lines = [0.0] + nums
        if any(b <= a for a, b in zip(lines, lines[1:])):
            fail(_("zone-tiler: lines in cols/rows must increase"), EXIT_CONFIG)
        return lines

    def _parse_zone(self, name, spec):
        spec = spec.strip()
        if ":" in spec:  # the zone as a range of grid cells: "0:1, 0:2"
            try:
                cols_part, rows_part = [p.strip() for p in spec.split(",")]
                c0, c1 = (int(v) for v in cols_part.split(":"))
                r0, r1 = (int(v) for v in rows_part.split(":"))
            except Exception:
                fail(
                    _("zone-tiler: zone '{name}' must be 'c0:c1, r0:r1' or 'x,y,w,h'").format(
                        name=name
                    ),
                    EXIT_CONFIG,
                )
            ncols, nrows = len(self.cols) - 1, len(self.rows) - 1
            if not (0 <= c0 < c1 <= ncols and 0 <= r0 < r1 <= nrows):
                fail(
                    _(
                        "zone-tiler: zone '{name}' goes beyond the grid ({ncols} columns, {nrows} rows)"
                    ).format(name=name, ncols=ncols, nrows=nrows),
                    EXIT_CONFIG,
                )
            return ("grid", c0, c1, r0, r1)
        # a zone as a free rectangle in fractions: "x,y,w,h"
        nums = [p for p in re.split(r"[,\s]+", spec) if p]
        try:
            x, y, w, h = (float(v) for v in nums)
        except Exception:
            fail(
                _("zone-tiler: zone '{name}' must be 'c0:c1, r0:r1' or 'x,y,w,h'").format(
                    name=name
                ),
                EXIT_CONFIG,
            )
        if not (
            0 <= x < 1
            and 0 <= y < 1
            and w > 0
            and h > 0
            and x + w <= 1.0001
            and y + h <= 1.0001
        ):
            fail(
                _("zone-tiler: zone '{name}' goes beyond the screen (fractions 0..1)").format(
                    name=name
                ),
                EXIT_CONFIG,
            )
        return ("rect", x, y, w, h)

    def fractions(self, zone, monitor_name=None):
        """The zone in work-area fractions: (x, y, w, h)."""
        spec = self.zone_spec(zone, monitor_name)
        if spec[0] == "rect":
            return spec[1:]
        _unused, c0, c1, r0, r1 = spec
        return (
            self.cols[c0],
            self.rows[r0],
            self.cols[c1] - self.cols[c0],
            self.rows[r1] - self.rows[r0],
        )

    def rect(self, zone, wa, monitor_name=None):
        """The pixel frame of the zone inside the work area wa=(x,y,w,h).

        `monitor_name` — the output name: a monitor may have a zone set of its own
        (`[zones:<output>]`), and then the description is taken from there.
        """
        spec = self.zone_spec(zone, monitor_name)
        if spec[0] == "rect":
            _unused, fx, fy, fw, fh = spec
            return self._px(wa, fx, fy, fx + fw, fy + fh)
        _unused, c0, c1, r0, r1 = spec
        return self._px(wa, self.cols[c0], self.rows[r0], self.cols[c1], self.rows[r1])

    @staticmethod
    def _px(wa, fx1, fy1, fx2, fy2):
        wx, wy, ww, wh = wa
        # the edges are computed from the same lines -> neighbouring zones meet without gaps
        x1, x2 = wx + round(ww * fx1), wx + round(ww * fx2)
        y1, y2 = wy + round(wh * fy1), wy + round(wh * fy2)
        return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


# ------------------------------------------------------------- monitors (Gdk)


def read_window_center(win):
    """The centre of the window (px, py) through xwininfo; None on error — see `window_backend`."""
    return window_backend.read_window_center(win)


def output_at_point(outputs, point):
    """The index and the name of the output whose work area `point` (the window centre) fell into.

    The shared piece of `move` and of "no `--monitor`" in `apply`/`stack`: the monitor is taken by
    the centre of the window frame (`monitor_for_point`) rather than by number 0 — otherwise the window
    would travel to the first screen while the zone is looked for in a foreign profile.
    """
    areas = [area for _unused, area in outputs]
    wa = monitor_for_point(areas, point)
    index = areas.index(wa)
    return index, outputs[index][0], wa


def window_output(outputs, window, warn=True):
    """Where a command without `--monitor` is addressed: by the screen of the window, else the first.

    The window frame cannot be read (the window is gone, no window is given) — the first screen; `warn`
    decides whether to write about it in stderr: the caller may warn itself, and
    repeating one line twice is not needed (see `target_output`).
    """
    if window is None:
        return 0, outputs[0][0], outputs[0][1]
    try:
        frame = read_frame(window, frame_extents(window))
    except Exception as exc:
        if warn:
            print(
                _(
                    "zone-tiler: failed to read the frame of window 0x{window:x}: {error} - using the first monitor"
                ).format(window=window, error=exc),
                file=sys.stderr,
            )
        return 0, outputs[0][0], outputs[0][1]
    return output_at_point(
        outputs, (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2)
    )


def monitor_for_point(monitors, point):
    """The work area of the monitor of the point (else the nearest one) — see `window_backend`."""
    return window_backend.monitor_for_point(monitors, point)


def window_monitor(win, mons):
    """The work area of the monitor of the window, or None if it cannot be determined."""
    point = read_window_center(win)
    if point is None:
        return None
    return monitor_for_point(mons, point)


def window_workspace(win):
    """The desktop number of the window (`_NET_WM_DESKTOP`), or None — see `window_backend`."""
    return window_backend.window_workspace(win)


def current_workspace():
    """The number of the active desktop (`_NET_CURRENT_DESKTOP`), or None — see `window_backend`.

    Converting the EWMH numbering (from zero) into the keys of ``[workspaces]`` (from one) is
    still in :func:`grid_for_workspace`.
    """
    return window_backend.current_workspace()


def current_workspace_or_first():
    """The active desktop, and without `_NET_CURRENT_DESKTOP` — 0 (if X answers at all).

    Needed where the desktop has to be a number (`focus` mode, the daemon): in a Wayland session
    Cinnamon has no such property until the desktop is switched for the first time, and a failure here broke `focus`
    entirely (measurement: ``docs/RESEARCH-wayland-recon.md``). ``None`` is left only
    for "X does not answer" — then, as before, the shared `zones.conf` is used.

    `current_workspace()` is asked first, not an internal read: it is the point
    of substitution in the tests and in the daemon (``current_workspace=lambda: …``), and it must
    not be bypassed.
    """
    desk = current_workspace()
    if desk is not None:
        return desk
    return 0 if window_backend.x_alive() else None


def window_workspace_for_zones(win):
    """The desktop of the window for picking the grid: ``None`` for "sticky" and unknown ones.

    ``_NET_WM_DESKTOP = 0xFFFFFFFF`` means "on all desktops" per EWMH. The decision:
    such windows live in the default grid (``zones.conf``) rather than in the profile
    of a specific desktop, — otherwise a sticky window would change the grid together with the desktop.
    """
    workspace = window_workspace(win)
    if workspace is None or workspace >= 0xFFFFFFF0:
        return None
    return workspace


def grid_for_workspace(conf_path, workspace=None):
    """The grid for the workspace: the profile from ``[workspaces]``, or the config itself.

    ``workspace`` — the EWMH number (from zero); ``None`` — take the active desktop
    (`current_workspace()`). If the desktop is not mapped to a profile, the desktop is unknown,
    or the profile is not on disk, the plain ``Grid(conf_path)`` is returned: silent
    degradation to the shared zone set is better than a refusal — the user expects to get
    the layout on a desktop without a profile anyway.
    """
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in the editor

    if workspace == WORKSPACE_CONF:
        return Grid(conf_path)
    if workspace is None:
        workspace = current_workspace()
    if workspace is None:
        return Grid(conf_path)
    profile = zc.load_workspace_map(conf_path).get(workspace + 1)
    if profile is None:
        return Grid(conf_path)
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        return Grid(conf_path)
    if not os.path.exists(path):
        return Grid(conf_path)
    return Grid(path)


WORKSPACE_CURRENT = "current"
# The value of `--workspace conf`: take the grid straight from zones.conf, ignoring
# `[workspaces]`. The editor needs it: it draws and edits `zones.conf` itself, so
# "check the zone" must count the same grid as the canvas, not the desktop profile.
WORKSPACE_CONF = "conf"


def workspace_spec(text):
    """Parse `--workspace`: ``current`` (and empty) or an EWMH desktop number.

    Returns ``WORKSPACE_CURRENT`` or ``int``; the number cannot be negative,
    so `-1` is an argument error. The parsing goes on in `argparse` (the argument
    type), so that a wrong value gives code 2 and a clear message.
    """
    value = "" if text is None else str(text).strip()
    if not value or value.lower() == WORKSPACE_CURRENT:
        return WORKSPACE_CURRENT
    if value.lower() == WORKSPACE_CONF:
        return WORKSPACE_CONF
    if re.fullmatch(r"\d+", value):
        return int(value)
    raise argparse.ArgumentTypeError(
        _(
            "invalid workspace '{text}': a workspace number (0, 1, ...), {current} or {conf} is required"
        ).format(text=text, current=WORKSPACE_CURRENT, conf=WORKSPACE_CONF)
    )


def resolve_workspace(spec):
    """The desktop number for asking about the grid: ``current``/``None`` — the active desktop.

    The desktop is looked up through `current_workspace_or_first`: in a Wayland session Cinnamon
    has no `_NET_CURRENT_DESKTOP` property until the desktop is switched for the first time, and earlier
    that turned into a failure of the whole command. ``None`` is now only for "X does not
    answer" — then, as before, the shared `zones.conf` is used.
    """
    if spec is None or spec == WORKSPACE_CURRENT:
        return current_workspace_or_first()
    if spec == WORKSPACE_CONF:
        return WORKSPACE_CONF
    return int(spec)


def workspace_profile(conf_path, workspace):
    """The profile name of the desktop from ``[workspaces]``, or ``None`` if there is none.

    Answers only the question "which grid is on this desktop": there is no mapping,
    the profile name does not pass the sanitiser, or the profile file is not on disk — ``None``, that is,
    the desktop lives in the shared zone set. The same degradation logic as in
    :func:`grid_for_workspace`, — so that the label in `list` does not diverge from what
    the command actually showed.
    """
    if workspace is None or workspace == WORKSPACE_CONF:
        return None
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in the editor

    profile = zc.load_workspace_map(conf_path).get(workspace + 1)
    if profile is None:
        return None
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        return None
    return profile if os.path.exists(path) else None


def grid_for_output(conf_path, output, fallback=None, workspace=None, quiet=False):
    """The grid of a screen: the profile from ``[monitors]`` → ``[workspaces]`` → the active config.

    ``output`` — the output name of the window (``HDMI-1``); ``workspace`` — the desktop number from EWMH
    (from zero), ``None`` — the active one. The resolution order is
    :func:`zone_config.resolve_profile`: the screen is more important than the desktop, the desktop more
    important than the active config. ``fallback`` — the already computed grid of the desktop: the screen lives on it if
    the profile is not on disk or is not found. A fallback with a warning, not a refusal:
    the user expects the layout on a screen without a profile anyway.

    Returns ``(Grid, profile name | None)``: the name — to say in the message which
    profile the grid was taken from. ``--workspace conf`` reads no profiles at all —
    the editor must count the same grid as the canvas.
    """
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in grid_for_workspace

    if workspace is None:
        workspace = current_workspace()
    if workspace == WORKSPACE_CONF:
        return (fallback if fallback is not None else Grid(conf_path)), None
    base = fallback if fallback is not None else Grid(conf_path)
    monitor_map = zc.load_monitor_map(conf_path)
    profile = zc.resolve_profile(
        monitor_map,
        zc.load_workspace_map(conf_path),
        output,
        workspace,
    )
    if profile is None:
        return base, None
    # The profile could come from the desktop rather than from the screen: the desktop grid already carries
    # base, and the label "screen …" in the messages would be a lie.
    bound_to_screen = any(
        str(key).strip().casefold() == str(output or "").strip().casefold()
        for key in monitor_map
    )
    if not bound_to_screen:
        return base, None
    try:
        path = zc.profile_path(profile, conf_path)
    except ValueError:
        path = None
    if path is None or not os.path.exists(path):
        if not quiet:
            if output:
                print(
                    _(
                        "zone-tiler: profile \"{profile}\" is not on disk: monitor {output} is laid out without it: using the workspace set or the default one"
                    ).format(profile=zc.sanitize_profile_name(profile), output=output),
                    file=sys.stderr,
                )
            else:
                print(
                    _(
                        "zone-tiler: profile \"{profile}\" is not on disk: the window's monitor is laid out without it: using the workspace set or the default one"
                    ).format(profile=zc.sanitize_profile_name(profile)),
                    file=sys.stderr,
                )
        return base, None
    return Grid(path), profile


def monitor_bindings(conf_path):
    """Whether the config has screen bindings ``[monitors]`` (without asking X)."""
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in grid_for_output

    return bool(zc.load_monitor_map(conf_path))


def require_zone(grid, zone, workspace=None, profile=None, output=None):
    """Check that the zone is in the grid set; otherwise a config error (code 3).

    The zone is looked up in the union of the sets of the shown grid (the shared `[zones]` and
    `[zones:<output>]`) — that is, exactly where the layout will find it. The list
    of available zones is printed in full: it shows that the desktop got the wrong
    profile rather than only that a name was typed with a mistake. ``output`` — the screen whose
    profile gave the grid: a message about the "desktop grid" on a screen profile would be a lie.
    """
    known = grid.all_zone_names()
    if zone in known:
        return
    have = ", ".join(sorted(known))
    if output and profile:
        fail(
            _(
                "zone-tiler: no zone '{zone}' in the grid of monitor {output} (profile \"{profile}\"). Known: {known}"
            ).format(zone=zone, output=output, profile=profile, known=have),
            EXIT_CONFIG,
        )
    if profile and workspace is not None:
        fail(
            _(
                "zone-tiler: no zone '{zone}' in the grid of workspace {workspace} (profile \"{profile}\"). Known: {known}"
            ).format(zone=zone, workspace=workspace + 1, profile=profile, known=have),
            EXIT_CONFIG,
        )
    if profile:
        fail(
            _(
                "zone-tiler: no zone '{zone}' in the grid of profile \"{profile}\". Known: {known}"
            ).format(zone=zone, profile=profile, known=have),
            EXIT_CONFIG,
        )
    fail(
        _("zone-tiler: no zone '{zone}' in [zones]. Known: {known}").format(
            zone=zone, known=have
        ),
        EXIT_CONFIG,
    )


def gdk_monitors():
    """``[(logical work area, scale)]`` from GDK — see `window_backend`."""
    return window_backend.gdk_monitors()


def monitors():
    """The work areas of the monitors in **physical** X pixels.

    See `window_backend.monitors`: it answers with `WindowBackendError`, while the message
    and the return code stay here — the same as before the window layer was moved out.
    """
    try:
        return window_backend.monitors()
    except window_backend.WindowBackendError:
        fail(_("zone-tiler: failed to determine the work area of the monitor"), EXIT_FAIL)


def monitor_scales():
    """The monitor scales in the same order as `monitors()` — see `window_backend`."""
    return window_backend.monitor_scales()


def scale_at_point(point):
    """The scale of the monitor whose physical work area the point fell into."""
    return window_backend.scale_at_point(point)


def monitor_outputs():
    """[(output name, (x, y, w, h))] — one per monitor.

    See `window_backend.monitor_outputs`: the bodies of the stitching live in the window layer and
    are gathered here so that the substitutions of `monitors`/`xrandr_monitors` in the tests
    and in the rest of the code keep working.
    """
    return window_backend.stitch_monitor_names(xrandr_monitors(), monitors())


def parse_xrandr_monitors(text):
    """[(name, (x, y, w, h))] from the output of `xrandr --listmonitors` — see `window_backend`."""
    return window_backend.parse_xrandr_monitors(text)


def xrandr_monitors():
    """The monitors from `xrandr --listmonitors`; [] if the utility is missing — see `window_backend`."""
    return window_backend.xrandr_monitors()


def overlap_area(a, b):
    """The area of the intersection of the rectangles a and b (0 if they do not intersect)."""
    return window_backend.overlap_area(a, b)


def stitch_monitor_names(outputs, areas):
    """Stitch the output names to the work areas: [(name, area)].

    See `window_backend.stitch_monitor_names`: the counts match — by order,
    otherwise — by the largest intersection, without names — `monitor-<i>`.
    """
    return window_backend.stitch_monitor_names(outputs, areas)


def resolve_monitor(spec, outputs=None):
    """(index, name) of the monitor by `--monitor`: the output name takes priority over the number.

    `spec` — a name (`HDMI-1`) or a number (`1`); `outputs` — the result of
    `monitor_outputs()` (can be substituted in the tests). An unknown name or
    a nonexistent number — an argument error listing the available monitors.
    """
    outputs = monitor_outputs() if outputs is None else outputs
    text = "" if spec is None else str(spec).strip()
    for index, (name, _unused) in enumerate(outputs):
        if name == text:
            return index, name
    number = int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    if number is not None and 0 <= number < len(outputs):
        return number, outputs[number][0]
    available = ", ".join(f"{i} — {name}" for i, (name, _unused) in enumerate(outputs))
    fail(
        _("zone-tiler: no monitor '{monitor}'. Available: {available}").format(
            monitor=text, available=available or _("none")
        ),
        EXIT_USAGE,
    )


def target_output(args, outputs=None):
    """The output name that `apply`/`stack` refers to, or ``None``.

    An explicit `--monitor` is more important; without it the target is the **screen of the window** (the active one or
    the one given by `--window`), exactly as in the commands themselves: otherwise the grid would come from
    one screen while the frame is computed on another, and the zone would be looked for in a foreign
    profile. It does not raise an error but answers ``None`` if there are no monitors or the
    target does not resolve: the zone check goes first, and the person must see
    "no zone", while a clear message about the monitor will be given by `resolve_monitor` in
    the command itself anyway. It does not warn about an unread window frame — the command will
    do that (`warn=False`).
    """
    outputs = monitor_outputs() if outputs is None else outputs
    if not outputs:
        return None
    if args.monitor is None:
        window = getattr(args, "window", None) or active_window_quiet()
        _index, output, _wa = window_output(outputs, window, warn=False)
        return output
    text = str(args.monitor).strip()
    for name, _wa in outputs:
        if name == text:
            return name
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text)
        if 0 <= number < len(outputs):
            return outputs[number][0]
    return None


def frame_extents(win):
    """The frame/titlebar thickness of the window manager — see `window_backend`."""
    return window_backend.frame_extents(win)


def client_size(rect, extents):
    """The client geometry inside the target window frame (compensating the WM frame).

    The WM draws a frame/titlebar around the client, so the client is given the zone minus
    _NET_FRAME_EXTENTS. Width and height are at least 1 px. A pure function — for the tests.
    """
    x, y, w, h = rect
    left, right, top, bottom = extents
    return (x, y, max(1, w - left - right), max(1, h - top - bottom))


def read_active_window():
    """The active window and the error text: ``(window | None, reason | None)``.

    The read is in `window_backend` (it returns a reason code), while the text for a human
    stays here: `active_window` fails with code 1 when there is no window (that is
    right for the commands that need a window), and `active_window_quiet` returns
    ``None`` without messages in stderr — for the one for whom a missing window is not an error.
    """
    window, reason = window_backend.read_active_window()
    if window is not None:
        return window, None
    if reason == window_backend.ACTIVE_WINDOW_NO_XPROP:
        return None, _("zone-tiler: xprop not found (x11-utils package)")
    return None, _("zone-tiler: no active window")


def active_window():
    """The active window; none — a message in stderr and code 1."""
    window, error = read_active_window()
    if window is None:
        fail(error, EXIT_FAIL)
    return window


def active_window_quiet():
    """The active window, or ``None`` — without messages in stderr.

    Needed where a missing window is not an error: picking the monitor by the screen of the window,
    `apply --dry-run` in a script, `stack` without windows. Then the first screen is taken —
    as it was before addressing by screen.
    """
    return read_active_window()[0]


# ----------------------------------------------------------------------- X11


# The connection to the X server and the EWMH commands live entirely in the window layer. The name is
# kept here for the callers and the tests: `apply_zone` opens the connection
# through `X11()`, and the tests substitute a fake connection for `zone-tiler.X11`.
X11 = window_backend.X11

# EWMH/ICCCM values that are visible outside the class (the tests compare them).
SOURCE_PAGER = window_backend.SOURCE_PAGER
ICONIC_STATE = window_backend.ICONIC_STATE


def _x11_failed():
    """Report that there is no X connection and exit with code 1.

    Earlier `X11.__init__` did it itself through `fail`; now the connection is opened by the
    window layer, which answers `WindowBackendError`, while the text and the return code stayed
    here — at the CLI boundary (see `main`).
    """
    fail(_("zone-tiler: failed to connect to the X server (DISPLAY)"), EXIT_FAIL)


# --------------------------------------------- minimising ("this zone only" mode)


def window_iconic(win):
    """Whether the window is minimised now (`Iconic`/`_NET_WM_STATE_HIDDEN`) — see `window_backend`."""
    return window_backend.window_iconic(win)


def iconify(window, x11=None, quiet=True):
    """Minimise the window. Closes its own X connection, leaves a foreign one alone.

    The only working way: muffin ignores both `XMapWindow` and
    `WM_CHANGE_STATE` with `NormalState`, and `_NET_WM_STATE_HIDDEN` belongs to the WM
    and the client cannot remove it (checked by smoke). `x11` is substituted in the tests;
    the daemon passes its own long-lived connection.
    """
    own = x11 is None
    if own:
        x11 = X11()
    try:
        x11.iconify(window)
        if not quiet:
            print(_("window 0x{window:x} -> minimized").format(window=window))
    finally:
        if own:
            x11.close()


def deiconify(window, x11=None):
    """Unminimise the window; it also raises and focuses it.

    That is why it is called for one window at a time, and the last one is the window that should
    keep the attention (the target of the mode). `x11` — as in :func:`iconify`.
    """
    own = x11 is None
    if own:
        x11 = X11()
    try:
        x11.activate(window)
    finally:
        if own:
            x11.close()


def focus_candidates(entries, target, workspace, wa):
    """The windows that "this zone only" mode minimises. A pure function.

    `entries` — `(window, desktop, work area)` in `_NET_CLIENT_LIST` order
    (the daemon hands over the same thing it keeps in `win_workspace`/`win_monitor`). The windows
    of **the same work area** and of the same desktop are taken, except the target; a sticky window
    (`workspace is None`) is visible everywhere and therefore fits. The active desktop has not been
    read (`workspace is None`) — only sticky windows fit: minimising
    the windows of other desktops blindly is worse than minimising nothing.

    The caller sifts out the exclusions (`window_excluded`) and the already minimised ones: it
    has the window metadata and `window_iconic`, while the pure function does not.
    """
    out = []
    for win, entry_workspace, entry_wa in entries:
        if win == target or entry_wa != wa:
            continue
        if workspace is None:
            if entry_workspace is not None:
                continue
        elif entry_workspace is not None and entry_workspace != workspace:
            continue
        out.append(win)
    return out


def focus_records(windows):
    """Window records for `focus.json`: `WM_CLASS` and the desktop are taken right now.

    The desktop is read for **every** window rather than taken from one field of the record: the file
    brings together windows of different desktops (the CLI remainder and the daemon remainder), while
    unminimising is possible only on the active desktop — so the desktop is needed per window.
    `WM_CLASS` is read here too: records without a class dodge the protection against
    a reused XID, and reading the class at write time is cheaper than
    unminimising a foreign window later.
    """
    return [
        {
            "window": hex(win),
            "wm_class": window_class_title(win)[0],
            "workspace": window_workspace_for_zones(win),
        }
        for win in dict.fromkeys(windows)
    ]


def focus_entries(client_ids, monitors_):
    """`(window, desktop, work area)` for every client — the input of `focus_candidates`.

    Reads `_NET_WM_DESKTOP` and the window centre (`xwininfo`). The monitor was not read —
    the window comes in with `None` and will not become a candidate: minimising a window that
    is not visible blindly is worse than not minimising. It reads X but makes no
    decisions — the sifting is done by the pure `focus_candidates`.
    """
    return [
        (win, window_workspace_for_zones(win), window_monitor(win, monitors_))
        for win in client_ids
    ]


# How many times to re-read the window state after a request, and with what pause: both unminimising
# and a desktop switch are client messages, the WM does not apply them instantly.
FOCUS_VERIFY_TRIES = 5
FOCUS_VERIFY_DELAY = 0.2


def switch_workspace(desktop, x11=None, read_current=None):
    """Switch the active desktop and wait for it to become active.

    Needed **before** unminimising: muffin ignores `_NET_ACTIVE_WINDOW` for a window that is not
    on the active desktop (checked by smoke), and the windows of the mode lie on their own desktop.
    `read_current` is substituted in the tests. `True` — the desktop is the right one (it was or became).
    """
    reader = read_current or current_workspace
    own = x11 is None
    if own:
        x11 = X11()
    try:
        if reader() == desktop:
            return True
        x11.set_current_desktop(desktop)
        for _unused in range(FOCUS_VERIFY_TRIES):
            time.sleep(FOCUS_VERIFY_DELAY)
            if reader() == desktop:
                return True
        return reader() == desktop
    finally:
        if own:
            x11.close()


def deiconify_all(windows, x11=None, deiconify_fn=None):
    """Unminimise the windows and re-read their state; returns the ones that **did not** unminimise.

    `deiconify` is a client message, and the WM does not process it instantly,
    so the state (`window_iconic`) is re-read with pauses. A window that stays
    minimised is almost certainly on an inactive desktop: muffin ignores the
    activation of such windows (checked by smoke). The caller decides what to do with the
    remainder — silently counting it unminimised is not allowed.

    `deiconify_fn` is substituted in the tests (and the daemon passes its own injection): with it
    no X connection is opened at all.
    """
    pending = list(dict.fromkeys(windows))
    if not pending:
        return []
    send = deiconify_fn or deiconify
    own = deiconify_fn is None and x11 is None
    if own:
        x11 = X11()
    try:
        for win in pending:
            send(win, x11=x11)
        for _unused in range(FOCUS_VERIFY_TRIES):
            time.sleep(FOCUS_VERIFY_DELAY)
            pending = [win for win in pending if window_iconic(win)]
            if not pending:
                return []
        return pending
    finally:
        if own:
            x11.close()


# --------------------------------------------- layout and history (without X)

# How many of the latest operations to keep in the layout history.
HISTORY_LIMIT = 20


def movable_windows(client_ids, metas, ignore=None):
    """Window ids that can be moved, in `_NET_CLIENT_LIST` order.

    The same filters as the daemon has: a non-empty `WM_CLASS` is needed and the window must not be
    excluded — `window_excluded` (a service type or an `[ignore]` selector).
    `override_redirect` windows are not in the EWMH client list, so no such
    check is needed there. A pure function — for the tests.
    """
    out = []
    for win in client_ids:
        meta = metas.get(win)
        if meta is None:
            continue
        if not meta.wm_class:
            continue
        if window_excluded(meta.wm_class, meta.wm_title, meta.window_type, ignore):
            continue
        out.append(win)
    return out


def choose_assignments(client_ids, metas, zones, rules, by="auto", ignore=None):
    """Match the windows to the zones. A pure function — for the tests.

    client_ids — ids in `_NET_CLIENT_LIST` order (bottom to top);
    metas — {id: WindowMeta};
    zones — zone names in file order (it matters for order/auto);
    rules — [(selector, zone)] from `[rules]`;
    ignore — `[ignore]` selectors (windows under them are not moved);
    by — the mode:
      `rules` — only the windows whose rule fired, each into its own zone;
      `order` — all movable windows in order into the zones in order; the extra windows
                (over the number of zones) get the last zone (there they are
                as tabs);
      `auto`  — the rules first, then the free (not taken by any rule)
                zones in order for the windows without a rule; the extras are left alone.

    Returns a list of (id, zone) in layout order; unassigned windows do not get
    into it.
    """
    windows = movable_windows(client_ids, metas, ignore)
    if by == "rules":
        result = []
        for win in windows:
            zone = match_rules(rules, metas[win].wm_class, metas[win].wm_title)
            if zone is not None:
                result.append((win, zone))
        return result
    if by == "order":
        if not zones:
            return []
        return [
            (win, zones[i] if i < len(zones) else zones[-1])
            for i, win in enumerate(windows)
        ]
    # auto
    by_rule = {}
    used = set()
    for win in windows:
        zone = match_rules(rules, metas[win].wm_class, metas[win].wm_title)
        if zone is not None:
            by_rule[win] = zone
            used.add(zone)
    free = [zone for zone in zones if zone not in used]
    result = []
    nxt = 0
    for win in windows:
        if win in by_rule:
            result.append((win, by_rule[win]))
        elif nxt < len(free):
            result.append((win, free[nxt]))
            nxt += 1
    return result


def zone_tabs_module():
    """The `zone_tabs` module: the tab arithmetic in a zone lives only there."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_tabs  # noqa: PLC0415 — lazy import, next to the tiler

    return zone_tabs


def in_zone_rect(zone_rect, count, height=None):
    """The window frame in a zone: one window — exactly the zone, several — the tab frame.

    `count` — how many windows ended up in the zone, `height` — the height of the tab strip
    (`[tabs] height`, `zone_tabs.STRIP_HEIGHT` by default). Several windows
    share one tab frame (`zone_tabs.tab_rect`: the zone minus the strip) rather than
    shifting in a cascade: the shift left the pieces of all windows visible at once and broke
    the frame of the zone. In a narrow zone there is no strip and the tab frame is the zone itself, but the tabs
    work all the same. A pure function — for the tests. `arrange` and `stack` place the
    windows by the same rule.
    """
    tabs = zone_tabs_module()
    strip = tabs.STRIP_HEIGHT if height is None else int(height)
    # The rule "one tab — the whole zone, two or more — the tab frame" lives
    # in the core (`zone_tabs.stack_rect`): one for the daemon and the tiler, otherwise
    # `apply` and `arrange` would diverge on the same number of windows.
    rect = tabs.stack_rect(zone_rect, int(count), strip)
    # Window frames are pixels: the core computes floats, while EWMH and the `--dry-run` output
    # expect integers, as in `Grid.rect`.
    return tuple(int(round(v)) for v in rect)


def tabs_height(grid):
    """The tab strip height from `[tabs] height`; no section — the strip default.

    The parsing of the section lives in `zone_config.load_tabs_config` (the error for
    garbage in the height is there too), so that the tiler and the strip do not parse `[tabs]` in their own ways.
    The import is lazy: `zone_config` itself loads this file as a module.
    """
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in the editor

    return zc.load_tabs_config(grid.path)["height"]


def state_home():
    """XDG state directory (not config and not cache)."""
    return os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )


def history_path():
    """Path to the layout history file.

    The history is state: undo runs in a separate process, so it
    must survive the exit; but it is not config and not something a cache
    cleaner has the right to wipe. Hence XDG_STATE_HOME
    (`~/.local/state/gridsnap/history.json`) — the place exactly for such state.
    """
    return os.path.join(state_home(), "gridsnap", "history.json")


class History:
    """The persistent history of layout operations (JSON, up to HISTORY_LIMIT pieces)."""

    def __init__(self, path=None, limit=HISTORY_LIMIT):
        self.path = path or history_path()
        self.limit = max(1, int(limit))

    def load(self):
        """The operations from the file (old → new). A broken/foreign file — an empty list."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        ops = data.get("operations") if isinstance(data, dict) else None
        if not isinstance(ops, list):
            return []
        return [op for op in ops if isinstance(op, dict)]

    def save(self, operations):
        """Write the operations, trimmed to the limit, atomically. Raises OSError."""
        ops = list(operations)[-self.limit :]
        directory = os.path.dirname(self.path)
        tmp = f"{self.path}.tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "operations": ops}, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def record(self, operation):
        """Append an operation, trimming the history to the limit. Raises OSError."""
        ops = self.load()
        ops.append(operation)
        self.save(ops)


# --------------------------------------------------- "this zone only" mode

# The version of `focus.json` — the mode state for the branch without a daemon.
FOCUS_STATE_VERSION = 1


def focus_path():
    """The path of the "this zone only" mode state (`$XDG_STATE_HOME/gridsnap/focus.json`).

    The file is needed only by the branch without a daemon: with a running daemon the mode lives in
    memory. It is state (it survives the exit of the process), not config and not cache.
    """
    return os.path.join(state_home(), "gridsnap", "focus.json")


def load_focus_state(path=None):
    """The mode state from the file; `{}` — the mode is not active.

    A broken file, a foreign version and an incomplete record are read as "the mode is not
    active": corrupted state must not bring the layout down. A reopened
    window (a new XID) stays in the record and is sifted out when unminimising — by
    `wm_class`, the way the daemon's memory of the layouts does it.
    """
    path = path or focus_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != FOCUS_STATE_VERSION:
        return {}
    if not isinstance(data.get("windows"), list):
        return {}
    return data


def save_focus_state(state, path=None):
    """Write the mode state atomically (`.tmp` + `os.replace`). Raises OSError."""
    path = path or focus_path()
    payload = dict(state)
    payload["version"] = FOCUS_STATE_VERSION
    directory = os.path.dirname(path)
    tmp = f"{path}.tmp"
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_focus_state(path=None):
    """Remove the mode state file (leaving the mode without a daemon). Idempotent."""
    path = path or focus_path()
    with contextlib.suppress(OSError):
        os.unlink(path)


def undo_targets(operations, all_=False):
    """Which windows to bring back and into which frames. A pure function — for the tests.

    `all_=False` — the last operation; `all_=True` — the whole history, and for
    every window the frame is taken from the earliest operation it appears in
    (so the window returns to the state "before all layouts"). Returns a list of
    `(id, frame, wm_class)` in the order the windows first appear.
    """
    if not operations:
        return []
    ops = operations if all_ else operations[-1:]
    first = {}
    order = []
    for op in ops:
        for item in op.get("windows") or []:
            if not isinstance(item, dict):
                continue
            win = item.get("id")
            frame = item.get("frame")
            if win is None or not isinstance(frame, (list, tuple)) or len(frame) != 4:
                continue
            if win not in first:
                first[win] = (list(frame), item.get("wm_class"))
                order.append(win)
    return [(win, first[win][0], first[win][1]) for win in order]


def remember(kind, records, quiet=False):
    """Write an operation into the history without breaking the layout because of a write error."""
    if not records:
        return
    operation = {"op": kind, "time": time.time(), "windows": records}
    try:
        History().record(operation)
    except OSError as exc:
        if not quiet:
            print(
                _("zone-tiler: failed to save history: {error}").format(error=exc),
                file=sys.stderr,
            )


def snapshot_window(win, zone=None):
    """A window snapshot for the history: the previous frame, the class, the workspace.

    Raises an exception if the geometry cannot be read (the window is gone) — the caller
    decides whether to record the operation.
    """
    extents = frame_extents(win)
    frame = read_frame(win, extents)
    wm_class, _title = window_class_title(win)
    return {
        "id": win,
        "frame": list(frame),
        "wm_class": wm_class,
        "workspace": window_workspace(win),
        "zone": zone,
    }


# -------------------------------------------------------------------- commands


def cmd_list(grid, args, profile=None):
    """Show the zones of the grid: `[zones:<output>]` of the monitor, or the shared `[zones]`.

    `profile` — the desktop profile name the grid was shown from (`None` — the shared
    `zones.conf`); it is appended to the first line so that it is visible why the
    zone set is exactly this one. The first line holds the monitor index, its work
    area `x,y WxH` and, when needed, the output and profile marks; the Cinnamon
    applet parses exactly the numbers in it (`N: x,y WxH`), while the words are translatable.
    """
    outputs = monitor_outputs()
    index, output = resolve_monitor(monitor_arg(args), outputs)
    wa = outputs[index][1]
    area = {"index": index, "x": wa[0], "y": wa[1], "w": wa[2], "h": wa[3]}
    named = output != f"monitor-{index}"
    if not named and profile:
        line = _(
            "monitor {index} work area: {x},{y} {w}x{h}  (profile \"{profile}\")"
        ).format(profile=profile, **area)
    elif not named:
        line = _("monitor {index} work area: {x},{y} {w}x{h}").format(**area)
    elif profile:
        line = _(
            "monitor {index} work area: {x},{y} {w}x{h}  (output {output})  (profile \"{profile}\")"
        ).format(output=output, profile=profile, **area)
    else:
        line = _("monitor {index} work area: {x},{y} {w}x{h}  (output {output})").format(
            output=output, **area
        )
    print(line)
    zones = grid.zones_for(output)
    width = max(len(z) for z in zones)
    for name in sorted(zones):
        x, y, w, h = grid.rect(name, wa, monitor_name=output)
        print(f"  {name:<{width}}  x={x:<5} y={y:<5} {w}x{h}")


def read_frame(window, extents):
    """The actual window frame (x, y, w, h) through xwininfo, taking the WM frame into account.

    The read is in `window_backend.read_frame`; it reports only the name of the field that
    was not in the output, while the human-readable `ValueError` text is built here.
    """
    try:
        return window_backend.read_frame(window, extents)
    except window_backend.WindowBackendError as exc:
        raise ValueError(
            _("xwininfo: no field '{label}'").format(label=str(exc))
        ) from None


def apply_zone(window, rect, label="", x11=None, verify=False, quiet=False):
    """Put the window into the pixel frame rect=(x, y, w, h). Without global state.

    Removes maximisation/fullscreen and compensates the WM frame. `x11` can be substituted
    (in the tests) — then no X server is needed. The connection opened by the function itself
    (`x11 is None`) is also closed by it; a foreign one passed in is left alone, the caller
    owns it (a long-lived daemon reuses one connection).
    With `verify` it returns the actual frame.
    """
    x, y, w, h = rect
    extents = frame_extents(window)
    own = x11 is None
    if own:
        x11 = X11()
    try:
        # remove maximisation/fullscreen/half-tiling, otherwise the WM would put its own back
        x11.set_state(window, 0, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
        x11.set_state(window, 0, "_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_FULLSCREEN")
        time.sleep(0.05)
        x11.move_resize(window, *client_size(rect, extents))
        time.sleep(0.05)
        if not quiet:
            if label:
                print(
                    _("window 0x{window:x} -> zone '{zone}': x={x} y={y} {w}x{h}").format(
                        window=window, zone=label, x=x, y=y, w=w, h=h
                    )
                )
            else:
                print(
                    _("window 0x{window:x} -> zone: x={x} y={y} {w}x{h}").format(
                        window=window, x=x, y=y, w=w, h=h
                    )
                )
        if verify:
            try:
                actual = read_frame(window, extents)
            except Exception:
                return None
            if not quiet:
                if actual == (x, y, w, h):
                    print(
                        _("actual frame: x={x} y={y} {w}x{h} - matches the zone").format(
                            x=actual[0], y=actual[1], w=actual[2], h=actual[3]
                        )
                    )
                else:
                    print(
                        _("actual frame: x={x} y={y} {w}x{h} - does not match!").format(
                            x=actual[0], y=actual[1], w=actual[2], h=actual[3]
                        )
                    )
            return actual
        return None
    finally:
        # we do not close a foreign connection: the caller owns it (the daemon)
        if own:
            x11.close()


def cmd_apply(grid, args):
    """Put the window into a zone; without `--monitor` — on the screen of the window itself.

    The screen of the window sets the zone set too: the zone is looked for in the profile of the screen where the window
    is (`grid_for_output`), — like `move` and like the daemon in the IPC `apply`.
    """
    outputs = monitor_outputs()
    # The window is read in advance: without `--monitor` it sets the screen, and its profile —
    # the zone set. `--dry-run` survives the absence of a window: then it is the first screen,
    # as it was before addressing by screen.
    window = args.window or active_window_quiet()
    if args.monitor is None:
        index, output, wa = window_output(outputs, window)
    else:
        index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[index][1]
    rect = grid.rect(args.zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(
            _("{zone}: x={x} y={y} {w}x{h} (monitor {index}: {output})").format(
                zone=args.zone, x=x, y=y, w=w, h=h, index=index, output=output
            )
        )
        return
    if window is None:
        fail(_("zone-tiler: no active window"), EXIT_FAIL)
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: window 0x{window:x} is excluded ([ignore]/utility window type) - not moving it"
                ).format(window=window),
                file=sys.stderr,
            )
        return
    before = None
    try:
        before = snapshot_window(window, zone=args.zone)
    except Exception:
        before = None  # the geometry cannot be read — no recording into the history
    apply_zone(window, rect, label=args.zone, verify=args.verify, quiet=args.quiet)
    if before is not None:
        remember("apply", [before], quiet=args.quiet)


# -------------------------------------------------- "this zone only" mode (CLI)

# How long to wait for the daemon's answer, seconds (the same order as in `gridsnap-ctl`).
DAEMON_TIMEOUT = 5.0
# Laying out by `arrange` is not an instant request: the daemon places every window itself and
# re-reads its frame, so on a large desktop 5 s may not be enough for the answer,
# although the daemon is alive and working.
DAEMON_ARRANGE_TIMEOUT = 30.0


def daemon_request(request, timeout=DAEMON_TIMEOUT):
    """Send one command to the live daemon; `None` — there is no daemon.

    `zone_ipc` is imported lazily (like `picker_nav`): without a daemon the tiler does not
    need this module. A dead socket/a missing daemon (`OSError`) — that is
    `None`: the caller will do the same thing one-off. The daemon is alive but did not answer within
    `timeout`, or answered unintelligibly — that is no longer "there is no daemon", and a second owner
    of the mode is not started: a refusal with a clear message.
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_ipc  # noqa: PLC0415 — lazy import, next to the tiler

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(zone_ipc.default_socket_path())
    except OSError:
        conn.close()
        return None
    try:
        conn.sendall(zone_ipc.encode_message(request))
        return zone_ipc.read_response(conn)
    except TimeoutError:
        fail(
            _(
                "zone-tiler: the daemon did not answer within {timeout:g} s - not switching the mode (a running daemon cannot be bypassed from outside)"
            ).format(timeout=timeout),
            EXIT_FAIL,
        )
    except zone_ipc.ProtocolError as exc:
        fail(_("zone-tiler: the daemon gave an unclear answer: {error}").format(error=exc), EXIT_FAIL)
    finally:
        conn.close()


def cmd_tabs_next(args):
    """Raise the next tab of the zone of the active window (by the `[tabs] key`).

    The tabs are owned by the daemon: it alone knows which windows sit on the tab frame, and
    only it can activate a window (`_NET_ACTIVE_WINDOW` + raise). Therefore
    the command lays out nothing itself but sends it `zone_tabs.tabs_next_command()`
    over the same socket as `gridsnap-ctl` (the lazy client — `daemon_request`).
    No daemon — that is code 1 with a clear message, not a traceback: without a daemon
    there are no tabs, yet the key binding remains. A window outside the zones the daemon counts as a silent
    no-op (code 0): there is nothing to switch there, and that is not an error.
    """
    request = zone_tabs_module().tabs_next_command()
    response = daemon_request(request)
    if response is None:
        fail(
            _(
                "zone-tiler: the daemon is not running - it switches tabs in a zone (start gridsnap-daemon)"
            ),
            EXIT_FAIL,
        )
    if not response.get("ok", True):
        reason = response.get("error") or _("reason not given")
        fail(_("zone-tiler: the daemon refused: {reason}").format(reason=reason), EXIT_FAIL)
    if args.quiet:
        return
    window = response.get("window")
    if window is None:
        print(_("zone-tiler: nothing to switch - the zone of the active window has a single window"))
        return
    print(_("zone-tiler: next window of the zone - {window}").format(window=window))


def cmd_action(grid, args):
    """Run an action from `[action:<name>]` — what hangs on a key.

    The action name comes as the first positional argument (as with `profile`: for the
    `action` command the name, not a zone, lies in `args.zone`). The record is read by
    `zone_config.load_actions`.

    **What the action gets** (`zone_actions.ActionContext`): the window, the zone, its
    pixel frame, the monitor, the desktop, the class and title of the window, the path to the config —
    through the `GRIDSNAP_*` environment variables and the `{zone}`/`{window}`/`{x}`… substitutions.
    An external command is parsed by `shlex` and started **without a shell** and without
    waiting (`start_new_session`): a key must not be bound to `gnome-terminal`,
    which lives until the window is closed.

    **The subject** — the zone from the record (`zone = main`), and the window is looked for in it; without a zone —
    the active window and the zone it stands in. `--window` sets the window explicitly,
    `--monitor` — the screen to look for the zone on (without it: the screen of the window, then of the active
    window, then the first one).

    **The builtins** (`run = builtin:<name>`): `minimize-others` — the same mode as
    `focus` (delegates to the daemon if it is alive); `show-window` — raise/unminimise
    a window of the zone (tabs — the one next to the active one); `mirror` — exchange with the mirror
    zone; `terminal` — start a terminal and put its window into the zone.

    `--dry-run` prints the parsed context and the plan (a builtin) or the ready
    argv (a command), doing nothing. Codes: 3 — config error (no such
    action, a broken field, an unknown substitution), 1 — a refusal on state, 0 —
    success **and** an honest no-op ("nothing to show", "the mirror zone is the same
    zone") with a message in stderr.

    With `terminal` the last word belongs to the action: the window may already have got under the daemon's
    `[rules]` and stood by a rule, but the action's layout is more important, so
    the zone frame is set once more. `show-window` on an empty zone — a no-op with code 0:
    the key must not complain that there is nothing to show (like `tabs-next`).
    """
    za = actions_module()
    name = (args.zone or "").strip()
    if not name:
        fail(
            _(
                "zone-tiler: action - specify an action name, for example: zone-tiler.py action mirror"
            ),
            EXIT_CONFIG,
        )
    actions = load_actions(args.conf)
    if name not in actions:
        known = ", ".join(actions) or _("none")
        fail(
            _("zone-tiler: no action '{name}'. Known: {known}").format(
                name=name, known=known
            ),
            EXIT_CONFIG,
        )
    record = actions[name]
    run = (record.get("run") or "").strip()
    try:
        builtin = za.builtin_name(run)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)

    # The screen — the target of the action: an explicit `--monitor`, else the screen of the window, else the first.
    # The candidate window for picking the screen — `--window` or the active one: the subject zone from
    # the record still has to be computed in pixels, and for that the grid of the screen is needed.
    outputs = monitor_outputs()
    if args.monitor is not None:
        index, output = resolve_monitor(args.monitor, outputs)
    else:
        index, output, _wa = window_output(outputs, args.window or active_window_quiet())
    wa = outputs[index][1]
    workspace = resolve_workspace(getattr(args, "workspace", None))
    grid, profile = grid_for_output(
        args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
    )

    window, zone = _action_subject(
        grid, args, record, output, wa, workspace=workspace, profile=profile
    )
    ctx = _action_context(grid, args, window, zone, output, wa)

    if builtin is None:
        _action_command(args, name, run, ctx, za)
        return
    handlers = {
        za.MINIMIZE_OTHERS: _action_minimize_others,
        za.SHOW_WINDOW: _action_show_window,
        za.MIRROR: _action_mirror,
        za.TERMINAL: _action_terminal,
    }
    handlers[builtin](grid, args, name, record, ctx, wa)


def actions_module():
    """The `zone_actions` module: the context, the substitutions and the mirror live only there."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_actions  # noqa: PLC0415 — lazy import, next to the tiler

    return zone_actions


def zone_config_module():
    """The `zone_config` module: parsing `[action:<name>]` lives only there."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_config  # noqa: PLC0415 — lazy import, next to the tiler

    return zone_config


def load_actions(conf):
    """The config actions through `zone_config.load_actions` (a broken config — code 3)."""
    return zone_config_module().load_actions(conf)


# How long to wait for the terminal window: 50 tries of 0.1 s — about 5 seconds. A terminal
# on a cold start (profile loading, first launch) easily does not make it in less.
ACTION_WAIT_TRIES = 50
ACTION_WAIT_DELAY = 0.1


def _action_subject(grid, args, record, output, wa, workspace=None, profile=None):
    """The window and the zone of the action — the "subject" the context receives.

    The zone from the record sets the subject itself: the window is the active one (`--window`) if it
    stands in that zone, otherwise the topmost by `_NET_CLIENT_LIST` (the list goes from the bottom
    up, so "the topmost" is the last one); an empty zone — no window. Without a zone in the
    record the window is `--window` or the active one, and the zone is the one whose frame (or tab
    frame) its frame got into (`zone_actions.zone_of_frame`); outside the zones — `None`.

    The zone from the record is looked for in the union of the grid sets (`require_zone`, code 3),
    as with `apply`: a name from a foreign profile is a config error, not an "empty zone".
    """
    client_ids = read_client_list() or []
    height = tabs_height(grid)
    preferred = args.window or active_window_quiet()
    zone = record.get("zone")
    if zone:
        require_zone(grid, zone, workspace=workspace, profile=profile, output=output)
        rect = grid.rect(zone, wa, monitor_name=output)
        return _action_window_in_zone(rect, client_ids, height, preferred), zone
    zones_px = {
        name: grid.rect(name, wa, monitor_name=output)
        for name in grid.zones_for(output)
    }
    frame = _action_frame(preferred)
    return preferred, actions_module().zone_of_frame(frame, zones_px, height)


def _action_frame(window):
    """The window frame with `_NET_FRAME_EXTENTS` compensated; cannot be read — `None`."""
    if window is None:
        return None
    try:
        return read_frame(window, frame_extents(window))
    except Exception:
        return None


def _action_frames(client_ids):
    """`{window: frame}` for the `_NET_CLIENT_LIST` clients (could not be read — `None`).

    The frames are read once per action: both `show-window` and `mirror` look at the
    windows of two zones, and a second pass of `xwininfo` over the same windows would add nothing,
    while a window could disappear in the meantime.
    """
    return {win: _action_frame(win) for win in client_ids}


def _action_windows_in_zone(zone_rect, frames, height):
    """The windows of a zone by its pixel frame — the core rule `zone_actions.zone_windows`.

    A window is in the zone if its frame matched the frame of the zone entirely or the frame of the
    tab (the zone minus the `[tabs] height` strip); the comparison — with the tolerance of the core.
    """
    tab_rect = in_zone_rect(zone_rect, 2, height)
    return actions_module().zone_windows(frames, zone_rect, tab_rect)


def _action_window_in_zone(zone_rect, client_ids, height, preferred=None):
    """The topmost window of the zone, and if `preferred` is already in it — `preferred` itself.

    "The topmost" — the last in `_NET_CLIENT_LIST`: the list goes from the bottom up. An empty
    zone — `None` (the action decides whether that is a no-op or a refusal).
    """
    frames = _action_frames(client_ids)
    if preferred is not None and _action_windows_in_zone(
        zone_rect, {preferred: frames.get(preferred)}, height
    ):
        return preferred
    found = _action_windows_in_zone(zone_rect, frames, height)
    return found[-1] if found else None


def _action_context(grid, args, window, zone, output, wa):
    """Build `zone_actions.ActionContext` for the window and the zone of the action.

    `rect` — the pixel frame of the zone on its monitor (that is what a foreign command expects),
    `conf` — the **absolute** path: the action is started by a foreign process, not by the CLI, and
    a relative `--conf zones.conf` in it would mean another file or none at all.
    The desktop — the desktop of the window; for a sticky window and a window without a desktop — the active one.
    """
    rect = grid.rect(zone, wa, monitor_name=output) if zone else None
    meta = read_window_meta(window) if window is not None else None
    workspace = window_workspace_for_zones(window) if window is not None else None
    if workspace is None:
        workspace = current_workspace()
    return actions_module().ActionContext(
        window=window,
        zone=zone,
        rect=rect,
        monitor=output,
        workspace=workspace,
        wm_class=meta.wm_class if meta is not None else "",
        wm_title=meta.wm_title if meta is not None else "",
        conf=os.path.abspath(args.conf),
    )


def _action_plan_line(name, run, ctx):
    """The first line of `--dry-run`: what the action received (window, zone, frame, screen)."""
    window = _("no window") if ctx.window is None else _("window 0x{window:x}").format(
        window=ctx.window
    )
    where = _("outside zones") if not ctx.zone else _("zone '{zone}'").format(zone=ctx.zone)
    if ctx.rect:
        where += ": x={} y={} {}x{}".format(*ctx.rect)
    screen = ctx.monitor or _("monitor not determined")
    desk = _("unknown") if ctx.workspace is None else str(ctx.workspace + 1)
    print(
        _(
            "action '{name}' ({run}): {window} -> {where} (monitor {screen}, workspace {desk})"
        ).format(
            name=name,
            run=run or "?",
            window=window,
            where=where,
            screen=screen,
            desk=desk,
        )
    )


def _action_noop(args, message):
    """An honest no-op: a message in stderr (unless `--quiet`) and code 0.

    The key must not complain that there is nothing to show or to exchange:
    `tabs-next` behaves the same way in a zone with a single window.
    """
    if not args.quiet:
        print(f"zone-tiler: {message}", file=sys.stderr)


def _action_dry_run_noop(args, name, record, ctx, message):
    """An empty action subject: a no-op, and under `--dry-run` — the context and the plan.

    `--dry-run` is promised to every action, so even "nothing to show" it
    prints the parsed context and explains the plan in stdout rather than staying silent: otherwise
    an empty output would not tell "the zone is empty" from "the action was not found".
    """
    if not args.dry_run:
        _action_noop(args, message)
        return
    _action_plan_line(name, record.get("run"), ctx)
    print(_("plan: {message} - doing nothing").format(message=message))


def _spawn(argv, ctx):
    """Start an action command detached: without a shell and without waiting.

    `start_new_session` — so that the terminal survives the key press (and so that
    a signal of our group does not reach the process); `stdin` — `/dev/null`, otherwise
    a process started from a key would share the input with it. Returns `Popen`.
    """
    try:
        return subprocess.Popen(
            argv,
            env={**os.environ, **ctx.env()},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        fail(_("zone-tiler: failed to run the action: {error}").format(error=exc), EXIT_FAIL)


def _action_command(args, name, run, ctx, za):
    """An external action command: `argv` from the config, started without a shell.

    An unknown substitution and an empty command — an `ActionError` of the core, that is,
    a config error (code 3) rather than "the command did not start": a typo in `{something}`
    is seen by the person right away, not as an empty argument of a foreign script.
    """
    try:
        argv = za.expand_command(run, ctx)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if args.dry_run:
        _action_plan_line(name, run, ctx)
        print(_("plan: argv = {argv}").format(argv=argv))
        return
    _spawn(argv, ctx)
    if not args.quiet:
        print(
            _("zone-tiler: action '{name}' started: {command}").format(
                name=name, command=argv[0]
            )
        )


def _action_minimize_others(grid, args, name, record, ctx, wa):
    """`builtin:minimize-others` — the "this zone only" mode, the same as `focus`.

    The mode has no copy of its own: we assemble `cmd_focus` and its arguments, and it does
    everything else — including handing the work to the live daemon, which alone can
    pull in the windows that appeared once the mode was on. The zone — from the record (no zone —
    "this window only"), the window — the subject of the action, else `--window`.
    """
    zone = record.get("zone")
    window = ctx.window if ctx.window is not None else args.window
    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        # `None` is printed as before: the template and the substitution are not translated.
        shown = window if window is None else hex(window)
        if zone:
            print(
                _(
                    "plan: window {window} -> zone '{zone}', the other windows of the workspace and monitor are minimized (a second press brings them back)"
                ).format(window=shown, zone=zone)
            )
        else:
            print(
                _(
                    "plan: window {window} -> stays in place, the other windows of the workspace and monitor are minimized (a second press brings them back)"
                ).format(window=shown)
            )
        return
    cmd_focus(
        grid,
        argparse.Namespace(
            zone=zone, off=False, window=window, dry_run=False, quiet=args.quiet
        ),
    )


def _action_show_window(grid, args, name, record, ctx, wa):
    """`builtin:show-window` — raise a window of the zone (in a stack — the ring neighbour).

    The windows of the zone — `zone_actions.zone_windows` by the client frames and the tab frame;
    the target — `zone_tabs.next_tab` from the active window (a single window or the active one outside the
    stack — the first in list order). `deiconify` is `_NET_ACTIVE_WINDOW`:
    unminimise, raise, focus. There is no zone or it is empty — a no-op (code 0).
    """
    if not ctx.zone:
        _action_dry_run_noop(args, name, record, ctx, _("the window is outside zones - nothing to show"))
        return
    base = grid.rect(ctx.zone, wa, monitor_name=ctx.monitor)
    height = tabs_height(grid)
    frames = _action_frames(read_client_list() or [])
    stack = _action_windows_in_zone(base, frames, height)
    if not stack:
        _action_dry_run_noop(
            args,
            name,
            record,
            ctx,
            _("zone '{zone}' is empty - nothing to show").format(zone=ctx.zone),
        )
        return
    target = zone_tabs_module().next_tab(stack, active_window_quiet()) or stack[0]
    if args.dry_run:
        ring = ", ".join(f"0x{win:x}" for win in stack)
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _("plan: raise window 0x{window:x} of zone '{zone}' (tabs: {ring})").format(
                window=target, zone=ctx.zone, ring=ring
            )
        )
        return
    deiconify(target)
    if not args.quiet:
        print(_("zone-tiler: window of zone '{zone}' - 0x{window:x}").format(zone=ctx.zone, window=target))


def _action_mirror(grid, args, name, record, ctx, wa):
    """`builtin:mirror` — exchange windows with the mirror zone.

    The mirror is looked for by geometry (`zone_actions.find_mirror` over the screen zone fractions),
    not by name: the person invents the zone names themselves. The subject window goes into
    the mirror zone, the window of the mirror — into the subject; an empty zone degenerates the exchange
    into the move of one window, both empty — a no-op (code 0). After the exchange **every** zone
    gets a frame set by the number of windows that ended up in it, and it is applied to all
    the windows of the zone: otherwise the tabs would fall apart (in a stack of two the window stands on the tab
    frame, not on the whole zone).
    """
    za = actions_module()
    zone = ctx.zone
    if not zone:
        fail(_("zone-tiler: the window is outside zones - nothing to mirror"), EXIT_FAIL)
    axis = record.get("axis") or za.DEFAULT_MIRROR_AXIS
    fractions = {
        name_: grid.fractions(name_, monitor_name=ctx.monitor)
        for name_ in grid.zones_for(ctx.monitor)
    }
    try:
        partner = za.find_mirror(fractions, zone, axis)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if partner is None:
        fail(
            _("zone-tiler: this grid has no mirror zone for '{zone}'").format(zone=zone),
            EXIT_FAIL,
        )

    height = tabs_height(grid)
    client_ids = read_client_list() or []
    frames = _action_frames(client_ids)
    rect = grid.rect(zone, wa, monitor_name=ctx.monitor)
    mirror_rect = grid.rect(partner, wa, monitor_name=ctx.monitor)
    here = _action_windows_in_zone(rect, frames, height)
    there = _action_windows_in_zone(mirror_rect, frames, height)
    # What moves is the subject of the action (the window the context received), and if it is no longer
    # in the zone — the topmost window of the zone: "the topmost" is the visible one.
    window = ctx.window if ctx.window in here else (here[-1] if here else None)
    other = there[-1] if there else None
    if window is None and other is None:
        _action_dry_run_noop(
            args,
            name,
            record,
            ctx,
            _("zones '{zone}' and '{partner}' are empty - nothing to swap").format(
                zone=zone, partner=partner
            ),
        )
        return
    final_here = [win for win in here if win != window]
    final_there = [win for win in there if win != other]
    if other is not None:
        final_here.append(other)
    if window is not None:
        final_there.append(window)
    here_frame = in_zone_rect(rect, len(final_here), height)
    there_frame = in_zone_rect(mirror_rect, len(final_there), height)

    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _(
                "plan: swap zones '{zone}' and '{partner}' ({axis}): {here} -> '{partner}', {there} -> '{zone}'; frames x={hx} y={hy} {hw}x{hh} and x={tx} y={ty} {tw}x{th}"
            ).format(
                zone=zone,
                partner=partner,
                axis=axis,
                here=_action_hex(window),
                there=_action_hex(other),
                hx=here_frame[0],
                hy=here_frame[1],
                hw=here_frame[2],
                hh=here_frame[3],
                tx=there_frame[0],
                ty=there_frame[1],
                tw=there_frame[2],
                th=there_frame[3],
            )
        )
        return

    # The previous frames — into the history, so that `undo` brings the windows back. The snapshot is taken before
    # the first move: after it the geometry is already foreign.
    records = []
    for win, target_zone in ((window, partner), (other, zone)):
        if win is None:
            continue
        try:
            records.append(snapshot_window(win, zone=target_zone))
        except Exception:
            continue  # the geometry cannot be read — we move the window anyway
    x11 = X11()
    try:
        for win in final_here:
            apply_zone(win, here_frame, label=zone, x11=x11, quiet=True)
        for win in final_there:
            apply_zone(win, there_frame, label=partner, x11=x11, quiet=True)
    finally:
        x11.close()
    remember("mirror", records, quiet=args.quiet)
    if not args.quiet:
        print(
            _(
                "zone-tiler: windows of zones '{zone}' and '{partner}' swapped (windows in zones: {here} and {there})"
            ).format(zone=zone, partner=partner, here=len(final_here), there=len(final_there))
        )


def _action_hex(window):
    """The window to print: `0x…` or "empty zone" — in the `--dry-run` plan."""
    if window is None:
        return _("empty zone")
    return _("window 0x{window:x}").format(window=window)


def _action_terminal(grid, args, name, record, ctx, wa):
    """`builtin:terminal` — a terminal in the zone (its own window takes the zone frame).

    The command — the `command` of the record, or `zone_actions.DEFAULT_TERMINAL`; the parsing and the
    substitutions — `zone_actions.expand_command`. The start is detached: a terminal
    lives until its window is closed, and it cannot be waited for. The new window is looked for in
    `_NET_CLIENT_LIST` for up to ~5 s (windows under `[ignore]`/service types are not taken) and
    is placed on the frame for the number of windows of the zone plus one — that is, with the zone taken, on
    the tab frame. If the window did not appear — code 1 with a clear message.
    """
    za = actions_module()
    if not ctx.zone:
        fail(
            _(
                "zone-tiler: action '{name}' has no zone - set zone in [action:{name}]"
            ).format(name=name),
            EXIT_FAIL,
        )
    base = grid.rect(ctx.zone, wa, monitor_name=ctx.monitor)
    height = tabs_height(grid)
    client_ids = read_client_list() or []
    count = len(_action_windows_in_zone(base, _action_frames(client_ids), height))
    rect = in_zone_rect(base, count + 1, height)
    command = record.get("command") or za.DEFAULT_TERMINAL
    try:
        argv = za.expand_command(command, ctx)
    except za.ActionError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    if args.dry_run:
        _action_plan_line(name, record.get("run"), ctx)
        print(
            _(
                "plan: run {argv} and put its window in zone '{zone}': x={x} y={y} {w}x{h} (windows in the zone: {count} + 1)"
            ).format(
                argv=argv,
                zone=ctx.zone,
                x=rect[0],
                y=rect[1],
                w=rect[2],
                h=rect[3],
                count=count,
            )
        )
        return

    before = set(client_ids)
    _spawn(argv, ctx)
    window = _wait_new_window(before, grid)
    if window is None:
        fail(
            _(
                "zone-tiler: the terminal in zone '{zone}' did not appear within {seconds:g} s - window not found"
            ).format(
                zone=ctx.zone, seconds=ACTION_WAIT_TRIES * ACTION_WAIT_DELAY
            ),
            EXIT_FAIL,
        )
    # The last word belongs to the action: the window may already have got under the daemon's `[rules]` and
    # stood by a rule; the frame of the action zone is more important, so it is set once more.
    apply_zone(window, rect, label=ctx.zone, quiet=args.quiet)
    if not args.quiet:
        print(
            _("zone-tiler: terminal in zone '{zone}': window 0x{window:x}").format(
                zone=ctx.zone, window=window
            )
        )


def _wait_new_window(before, grid, tries=ACTION_WAIT_TRIES, delay=ACTION_WAIT_DELAY):
    """The new `_NET_CLIENT_LIST` window (not excluded), or `None` on timeout.

    `before` — the windows before the command was started. A window under `[ignore]`/of a service type is not
    taken: that is not a terminal but a splash or a dialog, and it must not be moved.
    The first check is immediate: a fast terminal makes it before the pause.
    """
    for attempt in range(max(1, int(tries))):
        if attempt:
            time.sleep(delay)
        for win in read_client_list() or []:
            if win in before:
                continue
            if window_is_excluded(win, grid.ignore):
                continue
            return win
    return None


def cmd_focus(grid, args):
    """The "this zone only" mode: the window into the zone, the other windows of the monitor — minimised.

    Entering and leaving are **one** command (toggle): a repeated `focus` with the mode active
    unminimises the windows the mode minimised, and nothing more; a window minimised
    by hand stays minimised. `--off` — an explicit exit for scripts and a second
    key. The zone is optional: without it the window stays where it is, and the mode
    means "this window only" (that is the form of the `[focus]` key without `zone`). The area is
    the desktop and the monitor of the target window: other screens and desktops are not touched, and
    the exclusions (`window_excluded`) and dialogs — even less so.

    With a live daemon the command **delegates to it**: the mode must have one
    owner, and only the daemon can minimise the windows that appear after entering.
    Without a daemon it works one-off and keeps the list of minimised ones in `focus.json`
    (see `focus_path`) — new windows are not tracked in such a mode.
    """
    request = {"cmd": "focus"}
    if args.off:
        request["off"] = True
    elif args.zone:
        request["zone"] = args.zone
    if args.window is not None:
        request["window"] = args.window

    state = load_focus_state()
    if args.dry_run:
        _focus_dry_run(grid, args, state)
        return

    # Toggle: `--off` or an already active mode — an exit, not an entry. Decided before
    # the delegation, because entering and leaving go over the socket as different commands.
    exiting = bool(request.get("off")) or bool(state)
    # The CLI record is unminimised by the CLI itself: the daemon may have been started already in the mode, and
    # then those windows are "minimised not by it" for it. The unminimising goes before the command
    # to the daemon, otherwise it would take them for already minimised and would not remember them.
    restored, remaining = _focus_restore(state) if state else ([], [])
    if state and not remaining:
        # The record is removed only when the unminimising is confirmed: otherwise the window
        # would stay minimised without an owner.
        clear_focus_state()
    if state and remaining:
        _focus_keep(state, remaining)
    if remaining and not args.quiet:
        print(
            _(
                "zone-tiler: failed to unminimize {count} windows - the mode record is kept, repeat focus --off"
            ).format(count=len(remaining)),
            file=sys.stderr,
        )

    if exiting:
        # The daemon has its own mode with its own ledger: we ask it to leave and add up the count.
        response = daemon_request({"cmd": "focus", "off": True})
        if response is not None:
            if not response.get("ok"):
                fail(
                    _("zone-tiler: {error}").format(
                        error=response.get("error", _("the daemon rejected focus"))
                    ),
                    EXIT_FAIL,
                )
            restored += _focus_ids(response.get("restored"))
        if not args.quiet:
            print(
                _("focus mode off: windows unminimized - {count}").format(
                    count=len(restored)
                )
            )
        return

    # A live daemon is the only owner of the mode: it sees `_NET_CLIENT_LIST`
    # and can turn the windows that appeared after entering. Without a daemon — one-off.
    response = daemon_request(request)
    if response is not None:
        if not response.get("ok"):
            fail(
                _("zone-tiler: {error}").format(
                    error=response.get("error", _("the daemon rejected focus"))
                ),
                EXIT_FAIL,
            )
        _print_focus_response(response, quiet=args.quiet)
        return

    _focus_enter(grid, args)


def _focus_ids(values):
    """Window ids from the daemon answer (`["0x2", …]`) — numbers; garbage is dropped."""
    out = []
    for value in values or []:
        win = _focus_window_id({"window": value})
        if win is not None:
            out.append(win)
    return out


def _focus_dry_run(grid, args, state):
    """Show what `focus` would do, without moving or minimising anything.

    A live daemon is the owner of the mode and its window ledger is not visible to the CLI, so with an
    available socket `status` is asked: otherwise `--dry-run` would lie about
    "zero minimised" — both on entering and on leaving.
    """
    if not state:
        response = daemon_request({"cmd": "status"})
        mode = (response or {}).get("focus") if response and response.get("ok") else None
        if mode:
            print(
                _(
                    "focus mode: held by the daemon (zone '{zone}', target {window}, minimized {count}) - would turn off"
                ).format(
                    zone=mode.get("zone") or _("without a zone"),
                    window=mode.get("window"),
                    count=mode.get("minimized"),
                )
            )
            return
    if args.off or state:
        windows = state.get("windows") or []
        print(
            _("focus mode: would turn off, windows to unminimize - {count}").format(
                count=len(windows)
            )
        )
        return
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    wa = window_monitor(window, monitors())
    if wa is None:
        fail(
            _("zone-tiler: failed to determine the monitor of window 0x{window:x}").format(
                window=window
            ),
            EXIT_FAIL,
        )
    candidates = focus_candidates(
        focus_entries(read_client_list() or [], monitors()), window,
        current_workspace_or_first(), wa,
    )
    todo = [
        win
        for win in candidates
        if not window_is_excluded(win, grid.ignore) and not window_iconic(win)
    ]
    if args.zone:
        x, y, w, h = grid.rect(args.zone, wa)
        print(
            _(
                "focus mode: window 0x{window:x} -> zone '{zone}' x={x} y={y} {w}x{h}, windows to minimize - {count}"
            ).format(
                window=window, zone=args.zone, x=x, y=y, w=w, h=h, count=len(todo)
            )
        )
        return
    print(
        _(
            "focus mode: window 0x{window:x} -> stays in place, windows to minimize - {count}"
        ).format(window=window, count=len(todo))
    )


def _focus_enter(grid, args):
    """A one-off entry into the mode without a daemon: the target into the zone (if given), the rest — minimised."""
    window = args.window or active_window()
    if args.zone:
        require_zone(grid, args.zone)
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: window 0x{window:x} is excluded ([ignore]/utility window type) - not enabling the mode"
                ).format(window=window),
                file=sys.stderr,
            )
        return
    # `current_workspace_or_first`, not `current_workspace`: in a Wayland session Cinnamon has no
    # `_NET_CURRENT_DESKTOP` until the first desktop switch, and a refusal here broke the mode.
    workspace = current_workspace_or_first()
    if workspace is None:
        fail(_("zone-tiler: failed to determine the active workspace"), EXIT_FAIL)
    monitors_ = monitors()
    wa = window_monitor(window, monitors_)
    if wa is None:
        fail(
            _("zone-tiler: failed to determine the monitor of window 0x{window:x}").format(
                window=window
            ),
            EXIT_FAIL,
        )
    # The zone is checked on this work area — before anything has moved.
    rect = grid.rect(args.zone, wa) if args.zone else None
    entries = focus_entries(read_client_list() or [], monitors_)
    todo = [
        win
        for win in focus_candidates(entries, window, workspace, wa)
        if not window_is_excluded(win, grid.ignore) and not window_iconic(win)
    ]

    before = None
    if rect is not None:
        try:
            before = snapshot_window(window, zone=args.zone)
        except Exception:
            before = None  # the geometry cannot be read — no recording into the history

    # One X connection for the whole operation: `iconify` does not close a foreign connection.
    x11 = X11()
    minimized = []
    try:
        if rect is not None:
            apply_zone(window, rect, label=args.zone, x11=x11, quiet=True)
        # The state is written BEFORE minimising: if it was not written, no window is minimised
        # yet, and the command fails honestly instead of leaving the windows
        # minimised without a record (on a full disk or an unreachable
        # XDG_STATE_HOME). The list of windows is known in advance, so the record does not lie
        # about anything: the extra windows are simply activated when unminimising.
        save_focus_state(
            {
                "zone": args.zone,
                "workspace": workspace,
                "monitor": list(wa),
                "target": focus_records([window])[0],
                "windows": focus_records(todo),
            }
        )
        for win in todo:
            iconify(win, x11=x11, quiet=True)
            minimized.append(win)
    except OSError as exc:
        fail(
            _("zone-tiler: failed to write the mode state: {error}").format(error=exc),
            EXIT_FAIL,
        )
    finally:
        x11.close()
    if before is not None:
        remember("apply", [before], quiet=args.quiet)

    if not args.quiet:
        if args.zone:
            print(
                _(
                    "focus mode: window 0x{window:x} -> zone '{zone}', windows minimized - {count}"
                ).format(window=window, zone=args.zone, count=len(minimized))
            )
        else:
            print(
                _(
                    "focus mode: window 0x{window:x}, windows minimized - {count}"
                ).format(window=window, count=len(minimized))
            )


def _focus_restore(state, x11=None):
    """Unminimise the windows from the `focus.json` record; returns `(unminimised, remainder)`.

    The windows are unminimised **per desktop of the record**: muffin ignores `_NET_ACTIVE_WINDOW`
    for a window that is not on the active desktop (checked by smoke), while the file may
    bring together windows of different desktops — every window has its own desktop (`focus_records`), and
    the shared `workspace` of the record is only a fallback. Before every group the desktop
    is made active.

    A window that is no longer in `_NET_CLIENT_LIST`, and a window with another `WM_CLASS`
    (the XID was reused by another program) are skipped: unminimising a foreign window
    is worse than not unminimising our own. The target is activated last — it is the one that should
    keep the focus. No records — X is not needed either.

    `remainder` — the windows that stayed minimised: the caller is obliged to
    keep their record rather than count them unminimised on trust.
    """
    records = [
        record
        for record in (state.get("windows") or [])
        if _focus_window_id(record) is not None
    ]
    target = state.get("target") if isinstance(state.get("target"), dict) else None
    target_id = _focus_window_id(target) if target else None
    if not records and target_id is None:
        return [], []

    alive = read_client_list()
    alive = set(alive) if alive is not None else None
    own = x11 is None
    if own:
        x11 = X11()
    order, remaining = [], []
    try:
        groups = {}
        for record in records:
            win = _focus_window_id(record)
            if win is None or win == target_id or not _focus_alive(win, record, alive):
                continue
            groups.setdefault(_focus_workspace(record, state), []).append(win)
        if target_id is not None and _focus_alive(target_id, target, alive):
            groups.setdefault(_focus_workspace(target, state), []).append(target_id)
        for workspace in sorted(groups, key=lambda ws: (ws is not None, ws or 0)):
            if workspace is not None:
                switch_workspace(workspace, x11=x11)
            order += groups[workspace]
            remaining += deiconify_all(groups[workspace], x11=x11)
    finally:
        if own:
            x11.close()
    return [win for win in order if win not in remaining], remaining


def _focus_workspace(record, state):
    """The desktop of the window from the record: its own, else the shared desktop of the record; `None` — unknown."""
    value = record.get("workspace", state.get("workspace"))
    return value if isinstance(value, int) else None


def _focus_keep(state, remaining):
    """Leave only the not-unminimised ones in `focus.json` — with the current desktop of every window.

    The window records are built by `focus_records`: it reads `WM_CLASS` and **the desktop of every
    window** anew. Without that a window would have no desktop of its own (or would keep a
    foreign one from the old record), while unminimising is possible only on the active desktop —
    the next `focus --off` would again switch to the wrong one. If it was not written, the old
    record stays whole; a repeated unminimising is idempotent, so it cannot get
    worse.
    """
    try:
        save_focus_state(
            {
                "zone": state.get("zone"),
                "workspace": state.get("workspace"),
                "monitor": state.get("monitor"),
                "target": None,
                "windows": focus_records(remaining),
            }
        )
    except OSError as exc:
        print(
            _("zone-tiler: failed to rewrite the mode record: {error}").format(error=exc),
            file=sys.stderr,
        )


def _focus_window_id(record):
    """The window id from the `focus.json` record (a number or `0x…`); otherwise None."""
    value = record.get("window") if isinstance(record, dict) else record
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _focus_alive(win, record, alive):
    """Whether this is still our window: in the client list and with the same `WM_CLASS`."""
    if alive is not None and win not in alive:
        return False
    expected = record.get("wm_class") if isinstance(record, dict) else ""
    if not expected:
        return True
    return window_class_title(win)[0] == expected


def _print_focus_response(response, quiet=False):
    """Print the daemon answer to `focus` in a human way (`--quiet` — silently)."""
    if quiet:
        return
    if response.get("off"):
        count = len(response.get("restored") or [])
        print(
            _("focus mode off: windows unminimized - {count}").format(
                count=count
            )
        )
        return
    count = len(response.get("minimized") or [])
    print(
        _(
            "focus mode: window {window} -> zone '{zone}', windows minimized - {count}"
        ).format(window=response.get("window"), zone=response.get("zone"), count=count)
    )


def monitor_arg(args):
    """`--monitor` from the arguments: not given — 0.

    That is left only for `list`: it shows the work area and the zones of the first
    screen until `--monitor` says otherwise (the applet parses the first line by
    the numbers `N: x,y WxH`, so its words must not be relied on).
    `apply`/`stack`/`move` without `--monitor` work on the screen of the window.
    """
    return "0" if args.monitor is None else args.monitor


def picker_nav():
    """The `zone_picker_nav` module: the rules of picking a neighbour live only there."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import zone_picker_nav  # noqa: PLC0415 — lazy import, next to the tiler

    return zone_picker_nav


def nearest_zone(zones, fx, fy):
    """The index of the zone with the centre nearest to `(fx, fy)`; `None` — there are no zones.

    Needed for a window that floats freely past all zones: without it `move` from
    such a window would silently do nothing ("no zone under the centre"). A pure
    function — for the tests.
    """
    nav = picker_nav()
    best, best_dist = None, None
    for index, zone in enumerate(zones):
        cx, cy = nav.center(zone)
        dist = (cx - fx) ** 2 + (cy - fy) ** 2
        if best_dist is None or dist < best_dist:
            best, best_dist = index, dist
    return best


def move_zone_index(zones, direction, fx, fy):
    """`(from, to)` for `move`; equal indices — no neighbour.

    `zones` — the zones of the set in file order as `(x, y, w, h)` tuples in work-area
    fractions, `(fx, fy)` — the window centre in the same fractions. The window zone is the one
    whose centre it is (`zone_picker_nav.zone_at`), and if the centre is past all
    zones — the nearest by centre (`nearest_zone`). The neighbour is picked by a foreign rule,
    `zone_picker_nav.move_selection`: the picker and `move` must behave the same
    way rather than as two similar implementations. A pure function — for the tests.
    """
    nav = picker_nav()
    if not zones:
        return None, None
    index = nav.zone_at(zones, fx, fy)
    if index is None:
        index = nearest_zone(zones, fx, fy)
    return index, nav.move_selection(zones, index, direction)


def cmd_move(grid, args):
    """Throw the window into the neighbouring zone in a direction (left/right/up/down).

    The window zone — by the centre of its frame in the work-area fractions of **its own** monitor
    (the `[zones:<output>]` set, else the shared `[zones]`); the neighbour — the same one the
    picker would highlight (`move_zone_index`). A window outside the zones — the nearest by centre
    zone. At the edge of the grid there is no neighbour: that is not an error — we report it in stderr and
    exit with code 0. A successful move (not `--dry-run`) is written into the history
    as a `move` operation, so `undo` brings the window back to its previous place.

    The grid — the profile of the **screen** of the window: `[monitors]` → `[workspaces]` → `zones.conf`
    (`grid_for_output`); the profile is not on disk — a fallback to the desktop grid with
    a warning rather than a refusal of the layout.
    """
    nav = picker_nav()
    direction = (args.zone or "").strip().lower()
    if direction not in nav.DIRECTIONS:
        fail(
            _("zone-tiler: move - specify a direction: {directions}").format(
                directions=" | ".join(nav.DIRECTIONS)
            ),
            EXIT_USAGE,
        )
    outputs = monitor_outputs()
    window = args.window or active_window()
    extents = frame_extents(window)
    try:
        frame = read_frame(window, extents)
    except Exception as exc:
        fail(
            _("zone-tiler: failed to read the frame of window 0x{window:x}: {error}").format(
                window=window, error=exc
            ),
            EXIT_FAIL,
        )
    if args.monitor is None:
        # The monitor of the window — by the centre of its frame (the panel is already subtracted in the work area).
        index, output, wa = output_at_point(
            outputs, (frame[0] + frame[2] // 2, frame[1] + frame[3] // 2)
        )
    else:
        index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[index][1]
    # The zone is looked for in the profile of the screen of the window, not only in the profile of its desktop.
    grid, _profile = grid_for_output(
        args.conf,
        output,
        fallback=grid,
        workspace=resolve_workspace(args.workspace),
        quiet=args.quiet,
    )
    if window_is_excluded(window, grid.ignore):
        if not args.quiet:
            print(
                _(
                    "zone-tiler: window 0x{window:x} is excluded ([ignore]/utility window type) - not moving it"
                ).format(window=window),
                file=sys.stderr,
            )
        return EXIT_OK
    names = list(grid.zones_for(output))
    zones = [grid.fractions(name, monitor_name=output) for name in names]
    wx, wy, ww, wh = wa
    fx = (frame[0] + frame[2] / 2.0 - wx) / ww
    fy = (frame[1] + frame[3] / 2.0 - wy) / wh
    here, there = move_zone_index(zones, direction, fx, fy)
    if here is None:
        fail(
            _("zone-tiler: monitor {index} ({output}) has no zones").format(
                index=index, output=output
            ),
            EXIT_CONFIG,
        )
    if there == here:
        if not args.quiet:
            print(
                _("zone-tiler: zone '{zone}' has no neighbour to the {direction} - not moving the window").format(
                    zone=names[here], direction=direction
                ),
                file=sys.stderr,
            )
        return EXIT_OK
    zone = names[there]
    rect = grid.rect(zone, wa, monitor_name=output)
    if args.dry_run:
        x, y, w, h = rect
        print(
            _(
                "window 0x{window:x}: zone '{here}' -> '{zone}': x={x} y={y} {w}x{h} (monitor {index}: {output})"
            ).format(
                window=window,
                here=names[here],
                zone=zone,
                x=x,
                y=y,
                w=w,
                h=h,
                index=index,
                output=output,
            )
        )
        return EXIT_OK
    before = None
    try:
        before = snapshot_window(window, zone=zone)
    except Exception:
        before = None  # the geometry cannot be read — no recording into the history
    apply_zone(window, rect, label=zone, verify=args.verify, quiet=args.quiet)
    if before is not None:
        remember("move", [before], quiet=args.quiet)
    return EXIT_OK


def group_windows_by_monitor(client_ids, outputs):
    """[(name, area, [window id])] — the windows by monitors, the order preserved.

    The monitor of a window — by its centre (the same `window_monitor` as the daemon has). Windows
    without a centre/monitor do not get into the result. Needed to apply the zones of
    their own monitor (`[zones:<output>]`) within the group.
    """
    areas = [wa for _output, wa in outputs]
    groups = collections.OrderedDict()
    for win in client_ids:
        wa = window_monitor(win, areas)
        if wa is None:
            continue
        groups.setdefault(wa, []).append(win)
    name_of = {wa: name for name, wa in outputs}
    return [(name_of.get(wa), wa, wins) for wa, wins in groups.items()]


def arrange_via_daemon(assignments, workspace, quiet=False):
    """Lay the windows out with the daemon's help: it applies them and sends the events out.

    The algorithm of picking the windows and the zones and the `undo` memory stay here
    (`choose_assignments` + `remember`), while the daemon applies and remembers the layouts:
    it alone keeps the tracker by which closing a window triggers a reflow, and it alone
    sends `window-moved` to the subscribers. Without a daemon `False` — the caller
    lays the windows out itself, as before.

    The "before" snapshots are taken **before** the command is sent: after it the windows have already moved, and
    `undo` would bring them back to the wrong place. Only the windows the daemon
    actually moved go into the history (the `arranged` answer); refusals for individual windows
    are printed rather than drowned in silence.
    """
    placements = []
    records = []
    for win, zone, _output, wa, _grid in assignments:
        placements.append(
            {
                "window": hex(win),
                "zone": zone,
                "monitor": [int(v) for v in wa],
            }
        )
        try:
            before = snapshot_window(win, zone=zone)
        except Exception:
            before = None
        if before is not None:
            records.append(before)
    response = daemon_request(
        {"cmd": "arrange", "workspace": workspace, "placements": placements},
        timeout=DAEMON_ARRANGE_TIMEOUT,
    )
    if response is None:
        return False
    if not response.get("ok"):
        fail(
            _("zone-tiler: {error}").format(
                error=response.get("error", _("the daemon rejected arrange"))
            ),
            EXIT_FAIL,
        )
    applied = set()
    for value in response.get("arranged") or []:
        try:
            applied.add(int(value, 0))
        except (TypeError, ValueError):
            continue
    for item in response.get("skipped") or []:
        if quiet or not isinstance(item, dict):
            continue
        print(
            _("window {window}: the daemon did not move it ({error})").format(
                window=item.get("window"), error=item.get("error")
            ),
            file=sys.stderr,
        )
    remember(
        "arrange",
        [record for record in records if record["id"] in applied],
        quiet=quiet,
    )
    return True


def cmd_arrange(grid, args):
    """Lay the `_NET_CLIENT_LIST` client windows out over the zones.

    The `--by` mode (auto/rules/order) picks the windows by the pure `choose_assignments`;
    the windows are grouped by monitor, and in every group the grid of the **screen** works
    (`grid_for_output`: the profile from `[monitors]` → `[workspaces]` → `zones.conf`),
    in it — the `[zones:<output>]` set or the shared `[zones]`. Several windows that
    ended up in one zone of one monitor get a shared tab frame
    (`in_zone_rect`): the zone minus the `[tabs] height` strip; a single window in a zone
    is placed exactly on the zone. The previous frames are written into the history for `undo`.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail(
            _("zone-tiler: failed to read _NET_CLIENT_LIST (DISPLAY and xprop are required)"),
            EXIT_FAIL,
        )
    metas = {win: read_window_meta(win) for win in client_ids}
    outputs = monitor_outputs()
    groups = group_windows_by_monitor(client_ids, outputs)
    workspace = resolve_workspace(args.workspace)
    assignments = []
    for output, wa, wins in groups:
        screen_grid, _profile = grid_for_output(
            args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
        )
        zones = list(screen_grid.zones_for(output))
        for win, zone in choose_assignments(
            wins, metas, zones, screen_grid.rules, args.by, ignore=screen_grid.ignore
        ):
            assignments.append((win, zone, output, wa, screen_grid))
    if not groups:
        if not args.quiet:
            print(
                _("zone-tiler: failed to determine a monitor for any window"),
                file=sys.stderr,
            )
        return
    if not assignments:
        if not args.quiet:
            print(_("zone-tiler: no windows suitable for layout"))
        return
    if not args.dry_run and not args.verify:
        # A live daemon is the single owner of the layout: it applies the layout, updates its
        # tracker (otherwise closing a window would trigger a reflow to the wrong place) and sends
        # `window-moved` to the subscribers. `--verify` and `--dry-run` stay local.
        if arrange_via_daemon(assignments, workspace, quiet=args.quiet):
            return
    x11 = None if args.dry_run else X11()
    totals = collections.Counter((wa, zone) for _win, zone, _output, wa, _grid in assignments)
    height = tabs_height(grid)
    records = []
    try:
        for win, zone, output, wa, screen_grid in assignments:
            base = screen_grid.rect(zone, wa, monitor_name=output)
            rect = in_zone_rect(base, totals[(wa, zone)], height)
            if args.dry_run:
                x, y, w, h = rect
                print(
                    _("window 0x{window:x} -> zone '{zone}': x={x} y={y} {w}x{h}").format(
                        window=win, zone=zone, x=x, y=y, w=w, h=h
                    )
                )
                continue
            try:
                before = snapshot_window(win, zone=zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(
                        _("window 0x{window:x}: failed to move ({error})").format(
                            window=win, error=exc
                        ),
                        file=sys.stderr,
                    )
                continue
            if before is not None:
                records.append(before)
    finally:
        if x11 is not None:
            x11.close()
    if not args.dry_run:
        remember("arrange", records, quiet=args.quiet)


def cmd_stack(grid, args):
    """Gather the movable client windows into one zone as tabs.

    The windows go onto one tab frame (`in_zone_rect`): the zone minus the
    `[tabs] height` strip, the last window of the list on top. A single window in a zone is placed
    exactly on the zone, without the strip. Switching tabs — `tabs-next`/a click on the strip.
    """
    client_ids = read_client_list()
    if client_ids is None:
        fail(
            _("zone-tiler: failed to read _NET_CLIENT_LIST (DISPLAY and xprop are required)"),
            EXIT_FAIL,
        )
    metas = {win: read_window_meta(win) for win in client_ids}
    windows = movable_windows(client_ids, metas, grid.ignore)
    if not windows:
        if not args.quiet:
            print(_("zone-tiler: no windows for tabs"))
        return
    outputs = monitor_outputs()
    if args.monitor is None:
        # Without `--monitor` we gather into a zone on the screen of the active window: where
        # the user is looking, and with the profile of that screen.
        mon_index, output, wa = window_output(outputs, active_window_quiet())
    else:
        mon_index, output = resolve_monitor(args.monitor, outputs)
        wa = outputs[mon_index][1]
    base = grid.rect(args.zone, wa, monitor_name=output)
    rect = in_zone_rect(base, len(windows), tabs_height(grid))
    x11 = None if args.dry_run else X11()
    records = []
    try:
        for index, win in enumerate(windows):
            if args.dry_run:
                x, y, w, h = rect
                print(
                    _(
                        "window 0x{window:x} -> zone '{zone}' (tab {tab} of {total}): x={x} y={y} {w}x{h}"
                    ).format(
                        window=win,
                        zone=args.zone,
                        tab=index + 1,
                        total=len(windows),
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                    )
                )
                continue
            try:
                before = snapshot_window(win, zone=args.zone)
            except Exception:
                before = None
            try:
                apply_zone(win, rect, label=args.zone, x11=x11, quiet=args.quiet, verify=args.verify)
            except Exception as exc:
                if not args.quiet:
                    print(
                        _("window 0x{window:x}: failed to put into tabs ({error})").format(
                            window=win, error=exc
                        ),
                        file=sys.stderr,
                    )
                continue
            if before is not None:
                records.append(before)
    finally:
        if x11 is not None:
            x11.close()
    if not args.dry_run:
        remember("stack", records, quiet=args.quiet)


def cmd_undo(grid, args):
    """Bring the windows back to their previous frames from the history (the last one or all)."""
    history = History()
    operations = history.load()
    if not operations:
        if not args.quiet:
            print(_("zone-tiler: layout history is empty"))
        return
    targets = undo_targets(operations, all_=args.all)
    x11 = X11() if targets else None
    restored = 0
    skipped = 0

    def warn(message):
        nonlocal skipped
        skipped += 1
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        for win, frame, wm_class in targets:
            current_class, current_title = window_class_title(win)
            if wm_class and current_class and current_class != wm_class:
                # the id could be reused by another application after the old one exited
                warn(
                    _(
                        "window 0x{window:x}: class mismatch ({actual} != {expected}) - skipping"
                    ).format(window=win, actual=current_class, expected=wm_class)
                )
                continue
            if wm_class and not current_class:
                warn(
                    _("window 0x{window:x}: no longer exists - skipping").format(window=win)
                )
                continue
            if window_excluded(current_class, current_title, window_types(win), grid.ignore):
                warn(
                    _(
                        "window 0x{window:x}: excluded ([ignore]/utility window type) - skipping"
                    ).format(window=win)
                )
                continue
            try:
                apply_zone(win, tuple(frame), x11=x11, quiet=args.quiet, verify=args.verify)
                restored += 1
            except (Exception, SystemExit) as exc:
                warn(
                    _("window 0x{window:x}: failed to restore ({error})").format(
                        window=win, error=exc
                    )
                )
    finally:
        if x11 is not None:
            x11.close()
    try:
        history.save([] if args.all else operations[:-1])
    except OSError as exc:
        if not args.quiet:
            print(
                _("zone-tiler: failed to update history: {error}").format(error=exc),
                file=sys.stderr,
            )
    if not args.quiet:
        if skipped:
            print(
                _("zone-tiler: windows restored: {restored}, skipped: {skipped}").format(
                    restored=restored, skipped=skipped
                )
            )
        else:
            print(_("zone-tiler: windows restored: {restored}").format(restored=restored))


def cmd_keys(grid, move=False, conf=None):
    """Print the 'zone<TAB>binding' pairs from the [keys] section (for setup-hotkeys.py).

    `[keys]` is shared for all monitors, while a zone may be described only in the
    section of a specific monitor — that is why the check goes over the union of the sets.

    `move=True` — print the move directions instead of the zones
    (`direction<TAB>binding`) from the optional `[move]` section: the source
    for `setup-hotkeys.py`, which registers `move <direction>`. The parsing of the
    section and the defaults — in `zone_config.load_move_keys` (an unknown key —
    a config error, code 3).
    """
    if move:
        import zone_config as zc  # noqa: PLC0415 — lazy import, as in the editor

        keys = zc.load_move_keys(conf or grid.path)
        for direction in zc.MOVE_DIRECTIONS:
            print(f"{direction}\t{keys[direction]}")
        return
    if not grid.conf.has_section("keys"):
        return
    known = set(grid.all_zone_names())
    for zone, binding in grid.conf.items("keys"):
        zone, binding = zone.strip(), binding.strip()
        if zone not in known:
            print(
                _(
                    "zone-tiler: in [keys] zone '{zone}' is missing from [zones] and [zones:<output>]"
                ).format(zone=zone),
                file=sys.stderr,
            )
            continue
        if binding:
            print(f"{zone}\t{binding}")


# ---------------------------------------------------------------- profiles

PROFILE_ACTIONS = (
    "list",
    "show",
    "save",
    "use",
    "use-monitors",
    "rm",
    "export",
    "import",
    "gallery",
    "from-gallery",
)

# The `profile` actions that take no positional word: for them the first word after
# the action is also counted as extra. `use-monitors` is not here: it has
# its own name check (`_profile_use_monitors`) with a more precise hint
# about the `[monitors]` section.
PROFILE_NO_ARG_ACTIONS = ("list", "gallery")


def profile_extra_word(args, action):
    """The extra positional word of a `profile` action — it is the reason for the refusal too.

    An action has no more words than the help describes: for `export` the second word is a
    file, `list`/`gallery` have no words at all, the rest have only one (a name or an
    id). A silently swallowed argument is a mistake in the command that looks like
    success: `profile from-gallery work mine` created the profile "work", while `mine`
    went nowhere.
    """
    if action == "export":
        words = ()
    elif action in PROFILE_NO_ARG_ACTIONS:
        words = (args.name, args.target)
    else:
        words = (args.target,)
    for word in words:
        if (word or "").strip():
            return word.strip()
    return ""


def _run_hotkeys(conf, quiet=False):
    """Re-read [keys] from conf and update the Cinnamon bindings (best-effort)."""
    script = os.path.join(HERE, "setup-hotkeys.py")
    try:
        r = subprocess.run(
            [sys.executable, script, "--conf", conf], text=True, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            _("zone-tiler: shortcuts not updated: {error}").format(error=exc),
            file=sys.stderr,
        )
        return
    if r.returncode:
        tail = (r.stderr or r.stdout).strip()
        print(
            _("zone-tiler: shortcuts not updated ({details})").format(details=tail),
            file=sys.stderr,
        )
        return
    if not quiet:
        first = (r.stdout or r.stderr).strip().splitlines()
        line = first[0] if first else _("shortcuts updated")
        print(_("zone-tiler: {message}").format(message=line))


def _profile_export(zc, args, name, conf):
    """``profile export``: the JSON of the profile to stdout or to a file."""
    target = (args.target or "").strip()
    if target and args.json:
        fail(
            _("zone-tiler: profile export - either --json or a file, not both"),
            EXIT_USAGE,
        )
    data = zc.profile_to_dict(name, conf)
    if not target:
        sys.stdout.write(zc.dumps_profile(data))
        return
    path = zc.write_profile_json(data, target)
    print(
        _("zone-tiler: profile \"{profile}\" exported: {path} ({zones} zones)").format(
            profile=zc.sanitize_profile_name(name),
            path=path,
            zones=zc.profile_zone_total(data),
        )
    )


def _profile_import(zc, args, path, conf):
    """``profile import``: a JSON file → the ``templates/<name>.conf`` profile."""
    data = zc.read_profile_json(path)
    written = zc.profile_from_dict(data, (args.as_name or "").strip() or None, conf)
    name = os.path.basename(written)[: -len(zc.PROFILE_EXT)]
    print(
        _("zone-tiler: profile \"{profile}\" imported: {path} ({zones} zones)").format(
            profile=name,
            path=written,
            zones=zc.profile_zone_total(data),
        )
    )


def section_pairs(lines):
    """``(key, value)`` pairs from the body of a section in line order.

    The body — the lines as ``zone_config.load_extra_sections`` gives them (or ready-made
    pairs): comments, empty lines and lines without ``=`` are skipped, an inline
    comment after a space is dropped, as in ``ConfigParser``. Needed for the
    merging of ``[rules]``/``[ignore]``: the selector has to be matched, not the text of a line.
    """
    pairs = []
    for item in lines or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            key, value = str(item[0]).strip(), str(item[1]).strip()
        else:
            line = str(item).strip()
            if not line or line[0] in "#;":
                continue
            line = re.sub(r"\s[#;].*$", "", line)
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
        if key:
            pairs.append((key, value))
    return pairs


def monitor_screen_map(conf_path):
    """``[(output, profile)]`` from ``[monitors]`` in line order, the case — from the file.

    :func:`zone_config.load_monitor_map` validates the section and sanitises the profile
    names, but ``ConfigParser`` lowercases the keys, while the sections
    ``[zones:<output>]`` are conventionally written the way ``xrandr`` prints the output
    (``HDMI-1``). That is why the name is taken from the text of the section (the public
    ``load_extra_sections``), and the profile — from the validated map; the comparison —
    case-insensitively, as in the resolver. An empty section — ``[]``.
    """
    import zone_config as zc  # noqa: PLC0415 — lazy import, as in grid_for_workspace

    mapping = zc.load_monitor_map(conf_path)
    if not mapping:
        return []
    by_lower = {str(key).casefold(): name for key, name in mapping.items()}
    rows, seen = [], set()
    body = zc.load_extra_sections(conf_path, sections=[zc.MONITORS_SECTION]).get(
        zc.MONITORS_SECTION, []
    )
    for key, _value in section_pairs(body):
        output = key.strip()
        lowered = output.casefold()
        profile = by_lower.get(lowered)
        if profile is None or lowered in seen:
            continue
        seen.add(lowered)
        rows.append((output, profile))
    for lowered, profile in mapping.items():
        if lowered not in seen:
            rows.append((lowered, profile))
    return rows


def _render_planned_config(zc, conf, default, monitor_sets, extra):
    """The text ``use-monitors`` would write, without writing into the real config.

    We write into a temporary file through the same ``zone_config._write_config``: otherwise
    `--dry-run` would have to repeat its assembly rules by hand, and what is shown
    would diverge from what would really land on disk.
    """
    fd, tmp = tempfile.mkstemp(prefix="gridsnap-plan-", suffix=".conf")
    try:
        with open(conf, encoding="utf-8") as src:
            data = src.read()
        with os.fdopen(fd, "w", encoding="utf-8") as dst:
            fd = None
            dst.write(data)
        zc._write_config(tmp, default, monitor_sets, extra)
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _profile_screen_zone_set(output, zones, monitor_sets):
    """The zone set of a screen from the profile: ``[zones:<output>]``, else the shared ``[zones]``."""
    for name, chunk in (monitor_sets or {}).items():
        if str(name).casefold() == str(output).casefold():
            return list(chunk)
    return list(zones or [])


def _profile_zone_chunks(zones, monitor_sets):
    """The zone sets of a profile in file order: the shared one, then ``[zones:<output>]``."""
    return [zones or []] + list((monitor_sets or {}).values())


def merge_monitor_profiles(screens):
    """Merge the screen profiles into a snapshot of the active config (a pure function).

    ``screens`` — ``[(output, profile, zones, rules, monitor_sets, ignore)]`` in
    ``[monitors]`` line order; the zones — ``Zone`` with the ``[keys]`` bindings.

    The default set is ``[zones]`` of the **first** screen that has zones (its
    ``[zones:<output>]`` if the profile has no shared set). ``[zones:<output>]``
    is written for every screen: the set of its profile, else its shared ``[zones]``.
    ``[rules]``, ``[ignore]`` and ``[keys]`` are merged over all screens: where a selector
    or a zone name diverges, the first screen wins, and the divergence gets into
    ``overrides`` (nothing is lost silently). A rule for a zone that is not in
    the resulting union of sets is dropped into ``dropped_rules``: otherwise
    ``Grid`` would refuse with code 3 and the config would become unreadable entirely.
    """
    default = None
    monitor_sets = {}
    zone_names = set()
    bindings = {}  # zone name -> (output, profile, binding)
    key_reported = set()
    raw_rules = []  # (selector, zone, output, profile)
    raw_ignore = []  # (selector, note, output, profile)
    overrides = []
    usable = []
    for output, profile, zones, rules, sets, ignore in screens:
        own = _profile_screen_zone_set(output, zones, sets)
        if not own:
            continue
        usable.append((output, profile))
        monitor_sets[output] = [zone.copy() for zone in own]
        if default is None:
            default = [zone.copy() for zone in (list(zones or []) or own)]
        for chunk in _profile_zone_chunks(zones, sets):
            for zone in chunk:
                zone_names.add(zone.name)
                binding = (zone.binding or "").strip()
                if not binding:
                    continue
                owner = bindings.get(zone.name)
                if owner is None:
                    bindings[zone.name] = (output, profile, binding)
                elif owner[2] != binding and zone.name not in key_reported:
                    key_reported.add(zone.name)
                    overrides.append(
                        _(
                            "zone key \"{zone}\": \"{first_key}\" (profile \"{first_profile}\", monitor {first_output}) kept instead of \"{key}\" (profile \"{profile}\", monitor {output}) - the first one wins"
                        ).format(
                            zone=zone.name,
                            first_key=owner[2],
                            first_profile=owner[1],
                            first_output=owner[0],
                            key=binding,
                            profile=profile,
                            output=output,
                        )
                    )
        for selector, target in section_pairs(rules):
            raw_rules.append((selector, target, output, profile))
        for selector, note in section_pairs(ignore):
            raw_ignore.append((selector, note, output, profile))

    dropped, seen_dropped = [], set()
    merged_rules = []
    rule_owner = {}
    for selector, target, output, profile in raw_rules:
        if target not in zone_names:
            if (selector, target) not in seen_dropped:
                seen_dropped.add((selector, target))
                dropped.append((selector, target))
            continue
        owner = rule_owner.get(selector)
        if owner is None:
            rule_owner[selector] = (target, output, profile)
            merged_rules.append((selector, target))
        elif owner[0] != target:
            overrides.append(
                _(
                    "rule for \"{selector}\": zone \"{first_zone}\" (profile \"{first_profile}\", monitor {first_output}) kept instead of \"{zone}\" (profile \"{profile}\", monitor {output}) - the first one wins"
                ).format(
                    selector=selector,
                    first_zone=owner[0],
                    first_profile=owner[2],
                    first_output=owner[1],
                    zone=target,
                    profile=profile,
                    output=output,
                )
            )

    merged_ignore = []
    ignore_owner = {}
    for selector, note, output, profile in raw_ignore:
        owner = ignore_owner.get(selector)
        if owner is None:
            ignore_owner[selector] = (note, output, profile)
            merged_ignore.append((selector, note))
        elif owner[0] != note:
            overrides.append(
                _(
                    "exclusion \"{selector}\": note \"{first_note}\" (profile \"{first_profile}\", monitor {first_output}) kept instead of \"{note}\" (profile \"{profile}\", monitor {output}) - the first one wins"
                ).format(
                    selector=selector,
                    first_note=owner[0],
                    first_profile=owner[2],
                    first_output=owner[1],
                    note=note,
                    profile=profile,
                    output=output,
                )
            )

    # `[keys]` is written by the first appearance of a zone name: we put the winning
    # binding into every written set, otherwise the losing screen would override it.
    for chunk in [default] + list(monitor_sets.values()):
        for zone in chunk:
            owner = bindings.get(zone.name)
            zone.binding = owner[2] if owner else ""
    return {
        "default": default,
        "monitor_sets": monitor_sets,
        "rules": merged_rules,
        "ignore": merged_ignore,
        "dropped_rules": dropped,
        "overrides": overrides,
        "screens": usable,
    }


def _profile_use_monitors(zc, args, conf):
    """``profile use-monitors``: a snapshot of the screen profiles from ``[monitors]``.

    Writes into the **active** config what everyone who does not read profiles sees
    (the picker, the editor, the applet): ``[zones]`` of the first screen, ``[zones:<output>]``
    of every screen, the merged ``[rules]``/``[ignore]``/``[keys]``. The other sections
    (``[grid]``, ``[monitors]``, ``[workspaces]``, ``[daemon]``, …) survive the
    write verbatim. A screen without a profile file is skipped with a warning:
    its ``[monitors]`` line stays and will work once the profile is brought back.
    """
    if (args.name or "").strip():
        fail(
            _(
                "zone-tiler: profile use-monitors takes no profile name: monitors and profiles are set by the [monitors] section"
            ),
            EXIT_USAGE,
        )
    if not os.path.exists(conf):
        # An empty [monitors] is read from a nonexistent file too, but there is nowhere to write
        # the snapshot: a typo in --conf must not look like "nothing to change".
        fail(_("zone-tiler: config not found: {path}").format(path=conf), EXIT_CONFIG)
    monitor_map = monitor_screen_map(conf)
    if not monitor_map:
        if not args.quiet:
            print(
                _("zone-tiler: [monitors] has no monitors - not changing the active config")
            )
        return
    screens = []
    for output, name in monitor_map:
        safe = zc.sanitize_profile_name(name)
        try:
            path = zc.profile_path(name, conf)
        except ValueError:
            path = None
        if path is None or not os.path.exists(path):
            print(
                _(
                    "zone-tiler: profile \"{profile}\" is not on disk: monitor {output} skipped, the [monitors] line stays and will work once the profile is back"
                ).format(profile=safe, output=output),
                file=sys.stderr,
            )
            continue
        zones, rules, sets, ignore = zc.read_profile_sets(name, conf)
        if not zones and not sets:
            print(
                _(
                    "zone-tiler: profile \"{profile}\" has no zones: monitor {output} skipped"
                ).format(profile=safe, output=output),
                file=sys.stderr,
            )
            continue
        screens.append((output, name, zones, rules, sets, ignore))
    if not screens:
        if not args.quiet:
            print(
                _(
                    "zone-tiler: no monitor from [monitors] found its profile - not changing the active config"
                )
            )
        return
    plan = merge_monitor_profiles(screens)
    if not args.quiet:
        for message in plan["overrides"]:
            print(_("zone-tiler: overridden: {message}").format(message=message))
    for selector, zone in plan["dropped_rules"]:
        print(
            _(
                "zone-tiler: rule \"{selector} = {zone}\" skipped: zone '{zone}' is not in the resulting set - otherwise the config would become invalid"
            ).format(selector=selector, zone=zone),
            file=sys.stderr,
        )
    host = zc.load_extra_sections(conf)
    host.pop("rules", None)
    host.pop("ignore", None)
    if plan["rules"]:
        host["rules"] = plan["rules"]
    if plan["ignore"]:
        host["ignore"] = plan["ignore"]
    if args.dry_run:
        if not args.quiet:
            sys.stdout.write(
                _render_planned_config(
                    zc, conf, plan["default"], plan["monitor_sets"], host
                )
            )
        return
    zc._write_config(conf, plan["default"], plan["monitor_sets"], host)
    if not args.quiet:
        for output, name in plan["screens"]:
            print(
                _(
                    "zone-tiler: monitor {output} - profile \"{profile}\": {zones} zones"
                ).format(
                    output=output,
                    profile=zc.sanitize_profile_name(name),
                    zones=len(plan["monitor_sets"][output]),
                )
            )
        print(
            _(
                "zone-tiler: active config updated: monitors {screens}, rules {rules}, exclusions {ignore}"
            ).format(
                screens=len(plan["screens"]),
                rules=len(plan["rules"]),
                ignore=len(plan["ignore"]),
            )
        )
    if not args.no_hotkeys:
        _run_hotkeys(conf, quiet=args.quiet)


def _profile_gallery(zc, args):
    """`profile gallery` — the list of ready-made layouts from the distribution.

    One line per layout is printed (id, title, how many zones, description), or
    JSON under `--json` — as in the other `profile` commands: the id is for scripts, the title
    and the description — for the person.
    """
    layouts = zc.gallery_layouts()
    if not layouts:
        fail(_("zone-tiler: the layout gallery is empty"), EXIT_FAIL)
    if args.json:
        print(json.dumps(layouts, ensure_ascii=False, indent=2))
        return
    for item in layouts:
        print(
            _("{id}\t{title}\tzones: {count}\t{description}").format(
                id=item["id"],
                title=item["title"],
                count=len(item["zones"]),
                description=item["description"],
            )
        )


def _profile_from_gallery(zc, args, conf):
    """`profile from-gallery <id> [--name <name>]` — copy a layout into a profile of your own.

    A copy, not a link: afterwards the person edits it as an ordinary profile (and can
    export/move it). An existing profile is not overwritten (even with the same
    name) — it is a foreign file, and a silent loss of the layout would be an unpleasant
    surprise: choose another name or remove the old one (`profile rm <name>`).
    """
    gid = (args.name or "").strip()
    if not gid:
        fail(
            _("zone-tiler: profile from-gallery - name the layout id (see profile gallery)"),
            EXIT_USAGE,
        )
    try:
        path = zc.install_gallery_layout(
            gid,
            name=(args.as_name or None),
            force=False,
            conf_path=conf,
        )
    except zc.GalleryError as exc:
        fail(_("zone-tiler: {error}").format(error=exc), EXIT_CONFIG)
    name = os.path.splitext(os.path.basename(path))[0]
    print(
        _("profile '{name}' created from the gallery layout '{id}': {path}").format(
            name=name, id=gid, path=path
        )
    )
    print(_("apply it with: zone-tiler.py profile use {name}").format(name=name))


def cmd_profile(args):
    """Layout profiles: list/show/save/use/rm/export/import (zone_config lazily)."""
    import zone_config as zc

    action = (args.zone or "").strip()
    if action not in PROFILE_ACTIONS:
        fail(
            _(
                "zone-tiler: profile - name an action: list | show <name> | save <name> | use <name> | use-monitors | rm <name> | export <name> [file | --json] | import <file> [--name <name>] | gallery | from-gallery <id> [--name <name>]"
            ),
            EXIT_USAGE,
        )
    conf = args.conf
    extra = profile_extra_word(args, action)
    if extra:
        hint = _("; the profile name is set by --name") if action == "from-gallery" else ""
        fail(
            _(
                "zone-tiler: profile {action} - extra word '{extra}': the action takes no such argument{hint}"
            ).format(action=action, extra=extra, hint=hint),
            EXIT_USAGE,
        )
    if action == "list":
        for name in zc.list_profiles(conf):
            print(name)
        return
    if action == "gallery":
        _profile_gallery(zc, args)
        return
    if action == "from-gallery":
        _profile_from_gallery(zc, args, conf)
        return
    if action == "use-monitors":
        try:
            _profile_use_monitors(zc, args, conf)
        except ValueError as exc:
            fail(f"zone-tiler: {exc}", EXIT_USAGE)
        except FileNotFoundError as exc:
            fail(f"zone-tiler: {exc}", EXIT_CONFIG)
        return

    name = (args.name or "").strip()
    if not name:
        what = _("JSON profile file") if action == "import" else _("profile name")
        fail(
            _("zone-tiler: profile {action} - specify {what}").format(action=action, what=what),
            EXIT_USAGE,
        )

    try:
        if action == "show":
            sys.stdout.write(zc.profile_text(name, conf))
        elif action == "save":
            zones = zc.load_zones(conf)
            extra = zc.load_extra_sections(conf, sections=["rules", "ignore"])
            path = zc.save_profile(
                name, zones, extra.get("rules", []), conf, ignore=extra.get("ignore")
            )
            print(
                _("zone-tiler: profile \"{profile}\" saved: {path}").format(
                    profile=zc.sanitize_profile_name(name), path=path
                )
            )
        elif action == "rm":
            zc.remove_profile(name, conf)
            print(
                _("zone-tiler: profile \"{profile}\" removed").format(
                    profile=zc.sanitize_profile_name(name)
                )
            )
        elif action == "export":
            _profile_export(zc, args, name, conf)
        elif action == "import":
            _profile_import(zc, args, name, conf)
        else:  # use
            zones = zc.apply_profile(name, conf)
            print(
                _("zone-tiler: profile \"{profile}\" applied: {zones} zones").format(
                    profile=zc.sanitize_profile_name(name), zones=len(zones)
                )
            )
            if not args.no_hotkeys:
                _run_hotkeys(conf, quiet=args.quiet)
    except zc.ProfileJsonError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)
    except ValueError as exc:
        fail(f"zone-tiler: {exc}", EXIT_USAGE)
    except FileNotFoundError as exc:
        fail(f"zone-tiler: {exc}", EXIT_CONFIG)


def main(argv=None):
    """The CLI entry point: an X server refusal stays a message and code 1.

    Earlier `X11.__init__` took care of it through `fail`; now the connection is
    opened by the window layer, which answers `WindowBackendError`, so the conversion into
    the previous text and return code — here, at the CLI boundary.
    """
    try:
        return _main(argv)
    except window_backend.WindowBackendError:
        _x11_failed()


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description=_("Window layout across grid zones (Cinnamon/X11)"),
        # No abbreviations: otherwise the removed `--offset` matches `--off` and
        # instead of "unknown flag" the person gets an error about the command '20'.
        allow_abbrev=False,
        epilog=_(
            "Examples: zone-tiler.py arrange --by order; zone-tiler.py stack main; zone-tiler.py tabs-next; zone-tiler.py undo --all; zone-tiler.py action mirror; zone-tiler.py profile use work; zone-tiler.py profile use-monitors"
        ),
    )
    ap.add_argument(
        "command",
        choices=[
            "list",
            "apply",
            "move",
            "focus",
            "action",
            "keys",
            "arrange",
            "stack",
            "tabs-next",
            "undo",
            "profile",
        ],
        help=_(
            "list/apply/keys - as before; move - throw the window into the neighbouring zone; focus - focus mode (window into a zone, the rest minimized; repeat to leave); action - run an action from [action:<name>] (built-in or a custom command); arrange - lay windows out across zones; stack - gather windows into a zone as tabs; tabs-next - raise the next tab of the zone ([tabs] key); undo - restore frames from history; profile - your own layout profiles (zones + rules + keys)"
        ),
    )
    ap.add_argument(
        "zone",
        nargs="?",
        help=_("zone name for apply/stack, direction for move, or a profile action"),
    )
    ap.add_argument(
        "name", nargs="?", help=_("profile name or JSON file for profile import")
    )
    ap.add_argument(
        "target",
        nargs="?",
        help=_("profile export: file for JSON (without it and without --json - stdout)"),
    )
    ap.add_argument(
        "--monitor",
        default=None,
        help=_(
            "monitor for list/apply/stack/move: output name (HDMI-1) or index (without the flag - the window's monitor for apply/stack/move, the first one for list)"
        ),
    )
    ap.add_argument(
        "--workspace",
        metavar="N|current",
        default=None,
        type=workspace_spec,
        help=_(
            "workspace for list/apply/move/arrange/stack: number (EWMH, zero-based), current (the active workspace's grid - default) or conf (always the zones.conf grid, without workspace profiles)"
        ),
    )
    ap.add_argument(
        "--window", type=lambda s: int(s, 0), help=_("window id (the active one by default)")
    )
    ap.add_argument(
        "--by",
        choices=["auto", "rules", "order"],
        default="auto",
        help=_(
            "arrange: window selection - rules and the rest (auto), rules only, or by order"
        ),
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help=_("undo: roll back the whole history, not just the last operation"),
    )
    ap.add_argument(
        "--no-hotkeys",
        action="store_true",
        help=_(
            "profile use/use-monitors: do not register hotkeys after activation"
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help=_(
            "profile export/gallery: print JSON (export - to stdout instead of a file)"
        ),
    )
    ap.add_argument(
        "--name",
        dest="as_name",
        metavar=_("NAME"),
        help=_("profile import/from-gallery: profile name (import defaults to the JSON one)"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=_(
            "only compute the frame (for profile use-monitors - show the config), changing nothing"
        ),
    )
    ap.add_argument(
        "--off",
        action="store_true",
        help=_(
            "focus: explicitly leave focus mode (unminimize what the mode minimized)"
        ),
    )
    ap.add_argument(
        "--move",
        action="store_true",
        help=_("keys: shortcuts for moving to the neighbouring zone from the [move] section"),
    )
    ap.add_argument(
        "--verify", action="store_true", help=_("re-read the real geometry")
    )
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--version", action="version", version=f"zone-tiler {VERSION}")
    # `--offset` was removed together with the cascade. Without an explicit check argparse eats
    # its value as a command and complains about "20" rather than about the flag: the person
    # sees the reason only in this message.
    for value in sys.argv[1:] if argv is None else argv:
        if value == "--offset" or value.startswith("--offset="):
            ap.error(
                _(
                    "--offset removed: windows of one zone are no longer cascaded, they sit as tabs on a shared frame (switching - tabs-next)"
                )
            )
    args = ap.parse_args(argv)

    if args.command == "profile":
        cmd_profile(args)
        return EXIT_OK
    # `[keys]` is shared for the file, and undo brings the previous frames back: the desktop profile
    # has no say over them, so zones.conf itself is read rather than the grid of the workspace.
    if args.command == "keys":
        cmd_keys(Grid(args.conf), move=args.move, conf=args.conf)
        return EXIT_OK
    if args.command == "undo":
        cmd_undo(Grid(args.conf), args)
        return EXIT_OK
    # `tabs-next` needs neither config nor grid: the daemon picks the zone itself by
    # the active window, and the strip height does not concern it.
    if args.command == "tabs-next":
        cmd_tabs_next(args)
        return EXIT_OK

    # list/apply/arrange/stack work with the grid of their desktop: `--workspace N` or
    # the active desktop by default; the grid is taken through grid_for_workspace, so
    # a desktop without a profile silently lives in the shared [zones]. The screen is more important than the desktop: if
    # the config has `[monitors]` bindings, the target of apply/stack is the screen of the window
    # (`target_output`), and the grid is taken from the profile of that screen. The monitors are read
    # only when there are bindings: without them it is an extra trip to X (and that before the
    # zone check).
    workspace = resolve_workspace(args.workspace)
    profile = workspace_profile(args.conf, workspace)
    grid = grid_for_workspace(args.conf, workspace)
    output = None
    if args.command in ("apply", "stack") and monitor_bindings(args.conf):
        output = target_output(args)
        if output is not None:
            grid, profile = grid_for_output(
                args.conf, output, fallback=grid, workspace=workspace, quiet=args.quiet
            )
    if args.command == "list":
        cmd_list(grid, args, profile=profile)
        return EXIT_OK
    if args.command == "arrange":
        cmd_arrange(grid, args)
        return EXIT_OK
    if args.command == "move":
        cmd_move(grid, args)
        return EXIT_OK
    if args.command == "focus":
        cmd_focus(grid, args)
        return EXIT_OK
    # `action` — like `focus`: it requires neither a zone nor a window from the CLI (the zone may be in
    # the action record, and the action may be without a zone at all).
    if args.command == "action":
        cmd_action(grid, args)
        return EXIT_OK
    if not args.zone:
        fail(
            _("zone-tiler: specify a zone, for example: zone-tiler.py apply left"), EXIT_USAGE
        )
    # The zone is looked for in the set of the shown grid (screen + desktop + shared [zones]); if it is not
    # there — that is a config error: the config and the profile have diverged.
    require_zone(grid, args.zone, workspace=workspace, profile=profile, output=output)
    if args.command == "stack":
        cmd_stack(grid, args)
        return EXIT_OK
    cmd_apply(grid, args)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-hotcorners — a window drag watcher: it drives the ``zone-picker --hud`` HUD
and places the window into the zone under the release point (Cinnamon/Muffin, X11).

EWMH has no "the user is dragging a window" event: ``_NET_WM_MOVERESIZE`` only
starts a drag, and dragging by the title bar is muffin's internal grab. So the
watcher listens to XInput2 raw events on the root: they arrive "at all times, even
when the device is grabbed by another client". Neither XGrabPointer, nor XTest, nor
XRecord is needed or used.

The loop:

1. ``RawButtonPress`` (button 1) — remember that the button is down, and "arm":
   did the press point land in the title bar band of the active window (the band height is
   ``_NET_FRAME_EXTENTS.top`` plus slack). A press outside the title bar is an ordinary click
   in the application, the watcher does nothing.
2. ``RawMotion`` with the button down and "arm" — the point (taken with ``XQueryPointer``:
   raw values of relative devices are offsets, not coordinates) is fed
   to the HUD no more than ~30 times a second. The HUD is a child ``zone-picker.py --hud
   --follow`` on the monitor under the point; when the point moves to another monitor it
   is restarted there. While the HUD is alive, muffin edge-tiling is turned off: our drop
   is the main one, and its OSD on edge tiling gets in the way.
3. ``RawButtonRelease`` — the release point is converted to fractions of the monitor
   work area (``zone_picker_nav.zone_at``); inside a zone — the window is placed
   through ``zone-tiler.apply_zone``, off the zones — nothing is done. The HUD is stopped,
   edge-tiling is restored to its previous value.

If XI2 did not come up (no libXi, an old server, ``XIQueryVersion`` refused),
the watcher goes to the fallback: polling ``XQueryPointer`` at ~30 Hz for the position and the
button mask. The behaviour is the same, only instead of events — a comparison of two samples.

Restoring edge-tiling is mandatory: it is done on a drop, on a cancel, in ``finally``
and on ``SIGTERM``/``SIGINT``. Other Cinnamon settings are not touched.

Usage:
  zone-hotcorners.py                # listen to drags until Ctrl-C
  zone-hotcorners.py --dry-run      # print what it would place, but not move the window
  zone-hotcorners.py --no-hud       # no overlay: only the drop on release
  zone-hotcorners.py --verbose      # write to stderr what is happening
  zone-hotcorners.py --conf PATH

The pure part (no X and no gsettings) is at the top of the file: hitting the title bar,
point throttling, "point → monitor → zone", argument parsing and the edge-tiling
state as data. It is covered by ``tests/test_hotcorners.py`` without a
display; X and process launching are in ``Hotcorners``/``XClient``/``HudProcess``.
"""

import argparse
import ctypes
import ctypes.util
import importlib.util
import os
import select
import signal
import subprocess
import sys
import time

VERSION = "0.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zone_i18n import _  # noqa: E402  (same place: interface strings and their translation)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

PICKER = os.path.join(HERE, "zone-picker.py")


def _load_sibling(name, filename):
    """Load a neighbouring hyphen module (zone-tiler.py) without depending on sys.path."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(_("failed to load module {}").format(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_hotcorners_tiler", "zone-tiler.py")
nav = _load_sibling("gridsnap_hotcorners_nav", "zone_picker_nav.py")


# ------------------------------------------------------------------ the pure part

# Slack for the title bar band: a press a little below it still counts as "on the title bar".
# It is easy to land 1-2 px lower with a mouse, and "arm" is permission to drag a
# window, not an exact measurement; a false positive costs one extra HUD, a miss —
# a drag that does not work.
TITLE_SLACK = 8

# If `_NET_FRAME_EXTENTS` was not read (no xprop, a window without a frame), the title
# bar band must exist anyway: on muffin it is around 30 px.
TITLE_FALLBACK_TOP = 30

# Points to the HUD — no more than 30 times a second: the cursor does not move
# perceptibly more often, and every message is an overlay redraw.
POINT_INTERVAL = 1.0 / 30.0

# Polling period in the fallback mode (XI2 did not come up).
POLL_INTERVAL = 1.0 / 30.0

# Time comparison tolerance: a point exactly one interval later must not be lost.
EPS = 1e-9

# Waiting for the window activation: muffin activates a window on a press, and the raw event
# may arrive before `_NET_ACTIVE_WINDOW` is updated.
ARM_RETRY_DELAY = 0.05


def normalize_bool_text(text):
    """``'true'``/``'false'`` (as gsettings prints it) or ``None``.

    A pure function: this way the original edge-tiling value is stored and checked
    as data, without running gsettings.
    """
    value = ("" if text is None else str(text)).strip().lower()
    return value if value in ("true", "false") else None


def title_band(top, slack=TITLE_SLACK):
    """The height of the title bar band with slack, in pixels (not less than the fallback)."""
    try:
        height = int(top)
    except (TypeError, ValueError):
        height = 0
    if height <= 0:
        height = TITLE_FALLBACK_TOP
    return height + max(0, int(slack))


def is_title_bar_press(point, frame, top, slack=TITLE_SLACK):
    """Did the press ``point`` land in the title bar band of the window with frame ``frame``?

    ``frame`` — the outer frame of the window ``(x, y, w, h)`` in screen pixels (given by
    ``zone-tiler.read_frame``: the client plus ``_NET_FRAME_EXTENTS``); ``top`` —
    the top frame from ``frame_extents``, that is the title bar height without slack.
    The band goes from the top edge of the frame down by :func:`title_band`, over the whole
    window width. A press above or below the band, left or right of the window — not "arm".
    A pure function — for the tests.
    """
    if frame is None or point is None:
        return False
    px, py = point
    fx, fy, fw, fh = (float(v) for v in frame)
    if fw <= 0 or fh <= 0:
        return False
    return fx <= px < fx + fw and fy <= py < fy + title_band(top, slack)


def should_emit(last, now, interval=POINT_INTERVAL):
    """Is it time to feed the next point to the HUD (throttling ~30 Hz).

    ``last`` — the time of the last fed point (``None`` — there were no points yet).
    A pure function: it does not read the time, so a test sets it itself.
    The ``EPS`` tolerance — so that a point exactly one interval later is not lost on
    float error (``1000 + 1/30 - 1000`` is a little less than ``1/30``).
    """
    if last is None:
        return True
    return now - last >= interval - EPS


def poll_events(prev, now):
    """Drag events from two ``XQueryPointer`` samples.

    A sample is ``(x, y, button1_down)``; ``now=None`` — the poll failed. Returns a
    list of ``('press'|'motion'|'release', (x, y))``. The first sample with the button already
    down gives ``press`` (the watcher was started in the middle of a drag), after that
    neighbouring samples are compared. A pure function — for the tests and for the fallback.
    """
    if now is None:
        return []
    x, y, down = now
    if prev is None:
        return [("press", (x, y))] if down else []
    px, py, pdown = prev
    events = []
    if down and not pdown:
        # The press is the event of this tick: nobody knows the "when it was pressed" point
        # more precisely than the current one, and a separate motion with the same point is noise.
        events.append(("press", (x, y)))
    elif down and (x, y) != (px, py):
        events.append(("motion", (x, y)))
    if pdown and not down:
        events.append(("release", (x, y)))
    return events


def monitor_index_for_point(outputs, point):
    """Index of the monitor under the point; ``None`` — there are no monitors.

    ``outputs`` — ``[(output name, (x, y, w, h))]`` as in
    ``zone-tiler.monitor_outputs``. A point outside all areas (the gap between
    monitors, the panel) goes to the monitor nearest by centre — by the same
    rule as in the tiler (``zone-tiler.monitor_for_point``), so that the
    watcher and the HUD do not diverge in choosing the screen.
    """
    if not outputs:
        return None
    areas = [wa for _, wa in outputs]
    target = tiler.monitor_for_point(areas, point)
    return areas.index(target)


def zone_rects(grid, monitor_name=None):
    """``[(zone name, fractions (x, y, w, h))]`` for the zone set of a monitor.

    The order is as in the config file: ``zone_picker_nav.zone_at`` on overlap
    chooses the last drawn zone, and this must match the HUD.
    """
    zones = grid.zones_for(monitor_name)
    return [(name, grid.fractions(name, monitor_name)) for name in zones]


def zone_under(rects, area, point):
    """``(name, fractions)`` of the zone under a screen point, or ``None`` — off the zones.

    The point is converted to fractions of the monitor work area ``area`` and looked up with
    the same ``zone_picker_nav.zone_at_point`` as in the HUD: the highlighted zone and the zone
    of the drop are the same code.
    """
    index = nav.zone_at_point([fractions for _, fractions in rects], area, point)
    if index is None:
        return None
    return rects[index]


class EdgeTiling:
    """Muffin edge-tiling state: remember it, turn it off, restore it.

    The setting value lives here as data (``original`` — ``'true'`` /
    ``'false'`` / ``None``), and gsettings is run through ``runner``: in tests
    it is a recording stub, in the field — `subprocess.run`. The decision "turn it off or not"
    is pure too: if the setting is already off, it is not touched at all, so as not to
    "turn it on" later by mistake.
    """

    SCHEMA = "org.cinnamon.muffin"
    KEY = "edge-tiling"

    def __init__(self, original=None, runner=None):
        self.original = normalize_bool_text(original)
        self.runner = runner or run_gsettings
        self.touched = False

    @classmethod
    def get_args(cls):
        return ["gsettings", "get", cls.SCHEMA, cls.KEY]

    @classmethod
    def set_args(cls, value):
        return ["gsettings", "set", cls.SCHEMA, cls.KEY, value]

    @property
    def restore_value(self):
        """Where to restore to: the original value, and without one — ``'true'``.

        An unknown/empty original value is treated as "it was on":
        leaving edge-tiling off after us is worse than turning it on where
        it was on by default anyway.
        """
        return self.original or "true"

    def read(self):
        """Read the current value (``self.original``); ``None`` on a failure."""
        code, out = self.runner(self.get_args())
        self.original = normalize_bool_text(out) if code == 0 else None
        return self.original

    def disable(self):
        """Turn edge-tiling off; ``False`` — it was not needed or did not work."""
        if self.original == "false":  # already off: do not touch it at all
            return False
        code, _ = self.runner(self.set_args("false"))
        if code != 0:
            return False
        self.touched = True
        return True

    def restore(self):
        """Restore the original value; ``False`` — we changed nothing."""
        if not self.touched:
            return False
        code, _ = self.runner(self.set_args(self.restore_value))
        if code == 0:
            self.touched = False
        return code == 0


def run_gsettings(argv):
    """``(return code, stdout)`` for a gsettings argv; a launch failure — ``(1, '')``."""
    try:
        done = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


# ------------------------------------------------------------------ HUD and drop


class HudProcess:
    """The child ``zone-picker.py --hud --follow`` on one monitor.

    Points are written to its stdin as ``X Y`` lines — the same format as
    ``--follow``; closing stdin (EOF) finishes its main loop, so
    stopping is a soft EOF first, then ``terminate``/``kill``.
    """

    def __init__(self, conf, picker=PICKER, python=None, verbose=False):
        self.conf = conf
        self.picker = picker
        self.python = python or sys.executable
        self.verbose = verbose
        self.proc = None
        self.index = None
        self.name = ""
        self.failed = False

    def command(self, index):
        return [
            self.python,
            self.picker,
            "--hud",
            "--follow",
            "--conf",
            self.conf,
            "--monitor",
            str(index),
        ]

    def start(self, index, name=""):
        """Start the HUD on monitor ``index``; one already running — leave it alone.

        A failed start (no ``python3-gi``, no ``zone-picker.py``) must not
        bring the watcher down: the drop works without an overlay too, so the failure is
        remembered and never retried.
        """
        if self.proc is not None and self.index == index:
            return False
        if self.failed:
            return False
        self.stop()
        try:
            self.proc = subprocess.Popen(
                self.command(index),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if self.verbose else subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.failed = True
            if self.verbose:
                print(
                    _("zone-hotcorners: HUD failed to start: {}").format(exc),
                    file=sys.stderr,
                    flush=True,
                )
            return False
        self.index = index
        self.name = name
        return True

    def feed(self, point):
        """Feed a point to the HUD; ``False`` — the channel is closed (the HUD has exited)."""
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(f"{int(point[0])} {int(point[1])}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.stop()
            return False
        return True

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Stop the HUD: EOF to stdin, then terminate/kill. Idempotent."""
        proc, self.proc = self.proc, None
        self.index = None
        self.name = ""
        if proc is None:
            return False
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        return True


class Hotcorners:
    """The core of the watcher: button/motion events -> the HUD and the drop.

    The dependencies are injected (as in ``zone-daemon.Daemon``), so the whole
    drag scenario is covered by unit tests without X, without a HUD and without
    gsettings:

      active_window  — the id of the active window;
      frame_extents  — the thickness of the window frame ``(left, right, top, bottom)``;
      read_frame     — the outer frame of the window ``(x, y, w, h)``;
      outputs        — ``[(output name, work area)]``;
      grid_for       — ``Grid`` for the window (by its workspace);
      apply          — place the window into a pixel frame;
      hud            — :class:`HudProcess` or ``None`` (``--no-hud``);
      edge           — :class:`EdgeTiling`;
      clock/sleep    — time and pause (controllable in tests).
    """

    def __init__(
        self,
        conf,
        dry_run=False,
        no_hud=False,
        verbose=False,
        active_window=None,
        frame_extents=None,
        read_frame=None,
        outputs=None,
        grid_for=None,
        apply=None,
        is_excluded=None,
        hud=None,
        edge=None,
        clock=time.monotonic,
        sleep=time.sleep,
        out=None,
        err=None,
    ):
        self.conf = conf
        self.dry_run = dry_run
        self.no_hud = no_hud
        self.verbose = verbose
        self.active_window = active_window or tiler.active_window
        self.frame_extents = frame_extents or tiler.frame_extents
        self.read_frame = read_frame or tiler.read_frame
        self.outputs = outputs or tiler.monitor_outputs
        self.grid_for = grid_for or self.default_grid
        self.apply = apply or (lambda win, rect, label: tiler.apply_zone(win, rect, label=label))
        self.is_excluded = is_excluded or tiler.window_is_excluded
        self.hud = None if no_hud else (hud if hud is not None else HudProcess(conf, verbose=verbose))
        self.edge = edge or EdgeTiling()
        self.clock = clock
        self.sleep = sleep
        self.out = out or (lambda text: print(text, flush=True))
        self.err = err or (lambda text: print(text, file=sys.stderr, flush=True))
        self.reset()

    # ---- drag state

    def reset(self):
        """Forget the current drag (without HUD/gsettings side effects)."""
        self.pressed = False
        self.armed = False
        self.drag_win = None
        self.grid = None
        self.monitors = []
        self.title_top = 0
        self.last_point = None
        self.rects = {}

    def log(self, text):
        if self.verbose:
            self.err(f"zone-hotcorners: {text}")

    def default_grid(self, win):
        """The window grid: the profile of its workspace (``grid_for_workspace``)."""
        workspace = tiler.window_workspace_for_zones(win)
        return tiler.grid_for_workspace(self.conf, workspace)

    def read_monitors(self):
        """The work areas of the monitors, once per drag.

        ``zone-tiler.monitor_outputs`` runs xrandr and GDK, and points come up to
        30 times a second — so the list is read on "arm", not on every
        motion. A read failure does not bring the watcher down: without monitors there is no drop.
        """
        try:
            return list(self.outputs())
        except SystemExit:  # tiler.fail: failed to determine the work area
            self.err(_("zone-hotcorners: failed to read monitor work areas"))
            return []
        except Exception as exc:
            self.err(_("zone-hotcorners: monitors not read: {}").format(exc))
            return []

    def zones_for(self, index, name):
        """The zones of a monitor (cached for the time of one drag)."""
        if index not in self.rects:
            self.rects[index] = zone_rects(self.grid, name)
        return self.rects[index]

    # ---- "arm": a press in the title bar

    def arm_at(self, point):
        """The window whose title bar covers the press point, or ``None``.

        The active window may update a little later than the press itself (muffin
        activates a window on a press), so a miss is repeated once after
        a short pause — but only a miss: having hit, we do not wait for nothing.
        """
        for attempt in (0, 1):
            try:
                win = self.active_window()
            except SystemExit:  # tiler.fail: no active window in this session
                self.log(_("no active window - this is a normal click"))
                win = None
            if win:
                extents = self.frame_extents(win)
                try:
                    frame = self.read_frame(win, extents)
                except Exception as exc:  # xwininfo did not read the geometry
                    self.log(_("frame of window 0x{:x} not read: {}").format(win, exc))
                    frame = None
                if is_title_bar_press(point, frame, extents[2]):
                    return win, extents[2]
            if attempt == 0:
                self.sleep(ARM_RETRY_DELAY)
        return None

    def on_press(self, point, button=1):
        """The button is pressed: remember the press and, if it is the title bar, "arm"."""
        if button != 1 or point is None:
            return False
        self.finish_drag()  # the previous drag must not leak
        self.pressed = True
        found = self.arm_at(point)
        if found is None:
            self.log(_("press {} outside the title bar of the active window - a normal click").format(point))
            return False
        self.drag_win, self.title_top = found
        self.armed = True
        try:
            self.grid = self.grid_for(self.drag_win)
        except SystemExit:  # tiler.fail: broken config or no config
            self.err(_("zone-hotcorners: zone grid not read, dragging abandoned"))
            self.finish_drag()
            return False
        if self.is_excluded(self.drag_win, self.grid.ignore):
            self.log(
                _(
                    "window 0x{:x} is excluded ([ignore]/utility window type) - not tracking the drag"
                ).format(self.drag_win)
            )
            self.finish_drag()
            return False
        self.monitors = self.read_monitors()
        self.rects = {}
        self.last_point = None
        self.log(
            _("arm: window 0x{:x}, title bar {} px, press {}").format(
                self.drag_win, title_band(self.title_top), point
            )
        )
        return True

    # ---- motion: HUD

    def hud_point(self, point):
        """Feed a point to the HUD (starting/restarting it on the right monitor)."""
        if self.hud is None or point is None:
            return False
        outputs = self.monitors
        index = monitor_index_for_point(outputs, point)
        if index is None:
            return False
        name, _area = outputs[index]
        if self.hud.start(index, name):
            self.log(_("HUD on monitor {}: {}").format(index, name))
            self.disable_edge_tiling()
        return self.hud.feed(point)

    def motion_due(self):
        """Is it worth polling the position on the next RawMotion at all.

        RawMotion comes at the rate of the device (up to 1000 Hz for gaming mice), and
        the position is only needed while the HUD is alive: before "arm", without a HUD and
        more often than 30 Hz there is no point in asking ``XQueryPointer``. The predicate
        is passed to ``XClient.read_events`` and decides this BEFORE the request to the server;
        the decision "feed the point" itself stays in :meth:`on_motion`.
        """
        if self.hud is None or not (self.pressed and self.armed):
            return False
        return should_emit(self.last_point, self.clock())

    def on_motion(self, point):
        """Motion with the button down and "arm": the point to the HUD (no more than 30 Hz)."""
        if not (self.pressed and self.armed) or point is None:
            return False
        now = self.clock()
        if not should_emit(self.last_point, now):
            return False
        self.last_point = now
        return self.hud_point(point)

    # ---- release: the drop

    def target_at(self, point):
        """``(name, fractions, area, output name, index)`` of the zone under the point."""
        if point is None or not self.monitors:
            return None
        outputs = self.monitors
        index = monitor_index_for_point(outputs, point)
        if index is None:
            return None
        name, area = outputs[index]
        try:
            found = zone_under(self.zones_for(index, name), area, point)
        except SystemExit:  # broken [zones:<output>] section
            self.err(_("zone-hotcorners: zones of monitor {} not read").format(name))
            return None
        if found is None:
            return None
        return found[0], found[1], area, name, index

    def on_release(self, point, button=1):
        """The button is released: restore the settings, place the window into the zone under the point."""
        if button != 1:
            return False
        was_armed = self.pressed and self.armed
        win = self.drag_win
        grid = self.grid
        if not was_armed or win is None or grid is None:
            self.finish_drag()
            return False
        target = self.target_at(point)
        rect = None
        if target is not None:
            zone, _fractions, area, output, index = target
            rect = grid.rect(zone, area, monitor_name=output)
        # The HUD and edge-tiling are turned off BEFORE placing: otherwise muffin may tile the window
        # by its own edge rule after our move/resize.
        self.finish_drag()
        if rect is None:
            self.log(_("released in {} - off the zones, not moving the window").format(point))
            return False
        x, y, w, h = rect
        if self.dry_run:
            self.out(
                _(
                    "window 0x{:x} -> zone '{}': x={} y={} {}x{} (monitor {}: {}) - dry-run, not moving the window"
                ).format(win, zone, x, y, w, h, index, output)
            )
            return True
        self.log(
            _("window 0x{:x} -> zone '{}': x={} y={} {}x{}").format(win, zone, x, y, w, h)
        )
        try:
            self.apply(win, rect, zone)
        except (SystemExit, OSError, subprocess.SubprocessError) as exc:
            # A long-lived watcher must not die because of one window or
            # a lost X connection: report it and keep listening.
            self.err(
                _("zone-hotcorners: window 0x{:x} not placed: {}").format(
                    win, exc or _("X failure")
                )
            )
            return False
        return True

    # ---- edge-tiling

    def disable_edge_tiling(self):
        """Turn edge-tiling off for the time of the HUD (without a HUD the setting is not touched).

        In ``--dry-run`` the setting is not touched either: a dry run promises to change
        nothing but its own output.
        """
        if self.hud is None or self.edge.touched:
            return False
        if self.dry_run:
            self.log(_("dry-run: leaving edge-tiling alone"))
            return False
        if self.edge.original is None:
            value = self.edge.read()
            self.log(_("edge-tiling right now: {}").format(value or _("not read")))
        if self.edge.disable():
            self.log(
                _("edge-tiling disabled for the HUD (was {})").format(
                    self.edge.restore_value
                )
            )
            return True
        return False

    def finish_drag(self):
        """Stop the HUD, restore edge-tiling and forget the drag.

        It is called on a drop, on a cancel (a signal) and in ``finally`` — so
        the setting is restored on any exit, not only on a successful drop.
        """
        if self.hud is not None:
            self.hud.stop()
        if self.edge.touched:
            if self.edge.restore():
                self.log(_("edge-tiling restored: {}").format(self.edge.restore_value))
            else:
                self.err(_("zone-hotcorners: failed to restore edge-tiling in gsettings"))
        self.reset()


# ------------------------------------------------------------------ X11 (ctypes)

GENERIC_EVENT = 35

XI_RawButtonPress = 15
XI_RawButtonRelease = 16
XI_RawMotion = 17

XI_ALL_MASTER_DEVICES = 1

BUTTON1 = 1
# The button mask of XQueryPointer: Button1Mask (X.h).
BUTTON1_MASK = 1 << 8

# XEvent is a 192-byte union on LP64: XNextEvent writes the whole event into
# it, so the buffer cannot be smaller than XEvent itself.
EVENT_BUFFER = ctypes.c_char * 192


class XGenericEventCookie(ctypes.Structure):
    """The prefix of ``XGenericEventCookie``: this is how Xlib delivers XI2 events."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int),
        ("evtype", ctypes.c_int),
        ("cookie", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    ]


class XIRawEvent(ctypes.Structure):
    """The prefix of ``XIRawEvent`` — up to ``detail``/``flags`` inclusive.

    The coordinates of a raw event are deliberately not read: for relative devices
    ``raw_values`` are offsets, not a position. The position is always taken with
    ``XQueryPointer``, so it is the same in XI2 and in the fallback.
    """

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int),
        ("evtype", ctypes.c_int),
        ("time", ctypes.c_ulong),
        ("deviceid", ctypes.c_int),
        ("sourceid", ctypes.c_int),
        ("detail", ctypes.c_int),
        ("flags", ctypes.c_int),
    ]


class XIEventMask(ctypes.Structure):
    """``XIEventMask``: which XI2 events to listen to and on which devices."""

    _fields_ = [
        ("deviceid", ctypes.c_int),
        ("mask_len", ctypes.c_int),
        ("mask", ctypes.POINTER(ctypes.c_ubyte)),
    ]


RAW_EVENT_KINDS = {
    XI_RawButtonPress: "press",
    XI_RawButtonRelease: "release",
    XI_RawMotion: "motion",
}

# An Xlib error handler: ``int (*)(Display *, XErrorEvent *)``. It is needed to
# make sure XISelectEvents was accepted by the server and did not fail asynchronously.
X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class XClient:
    """The X connection: XQueryPointer always, XI2 raw events — if they came up."""

    def __init__(self, display=None):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        self.x = ctypes.CDLL(name)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        shown = display if display is not None else os.environ.get("DISPLAY") or ""
        self.dpy = self.x.XOpenDisplay(shown.encode() or None)
        if not self.dpy:
            raise RuntimeError(_("failed to connect to the X server (DISPLAY)"))
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.root = self.x.XDefaultRootWindow(self.dpy)
        self.x.XConnectionNumber.restype = ctypes.c_int
        self.x.XConnectionNumber.argtypes = [ctypes.c_void_p]
        self.x.XPending.restype = ctypes.c_int
        self.x.XPending.argtypes = [ctypes.c_void_p]
        self.x.XNextEvent.restype = ctypes.c_int
        self.x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.x.XSync.restype = ctypes.c_int
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XCloseDisplay.restype = ctypes.c_int
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.x.XQueryExtension.restype = ctypes.c_int
        self.x.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x.XQueryPointer.restype = ctypes.c_int
        self.x.XGetEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(XGenericEventCookie)]
        self.x.XGetEventData.restype = ctypes.c_int
        self.x.XFreeEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(XGenericEventCookie)]
        self.x.XFreeEventData.restype = None
        self.x.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
        self.x.XSetErrorHandler.restype = X_ERROR_HANDLER
        self.xi = None
        self.xi_opcode = 0
        self.xi_version = None
        self.raw_events = False

    # ---- the ordinary pointer

    def pointer_state(self):
        """``(x, y, button1_down)`` in root coordinates; ``None`` on a failure."""
        root_ret = ctypes.c_ulong()
        child_ret = ctypes.c_ulong()
        root_x, root_y = ctypes.c_int(), ctypes.c_int()
        win_x, win_y = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        ok = self.x.XQueryPointer(
            self.dpy,
            self.root,
            ctypes.byref(root_ret),
            ctypes.byref(child_ret),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(win_x),
            ctypes.byref(win_y),
            ctypes.byref(mask),
        )
        if not ok:
            return None
        return root_x.value, root_y.value, bool(mask.value & BUTTON1_MASK)

    def pointer_point(self):
        state = self.pointer_state()
        return None if state is None else (state[0], state[1])

    def fd(self):
        return self.x.XConnectionNumber(self.dpy)

    # ---- XI2 raw events

    def enable_raw_events(self):
        """Subscribe the root to XI_RawMotion|RawButtonPress|RawButtonRelease.

        Returns the ``(major, minor)`` XInput version the server agreed to,
        or ``None`` — then the watcher goes to the XQueryPointer fallback. No
        grabs at all: raw events arrive even under a foreign grab, that is the whole point.
        """
        opcode = ctypes.c_int()
        event = ctypes.c_int()
        error = ctypes.c_int()
        if not self.x.XQueryExtension(
            self.dpy,
            b"XInputExtension",
            ctypes.byref(opcode),
            ctypes.byref(event),
            ctypes.byref(error),
        ):
            return None
        try:
            self.xi = ctypes.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        except OSError:
            return None
        self.xi.XIQueryVersion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.xi.XIQueryVersion.restype = ctypes.c_int
        self.xi.XISelectEvents.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(XIEventMask),
            ctypes.c_int,
        ]
        self.xi.XISelectEvents.restype = ctypes.c_int
        major, minor = ctypes.c_int(2), ctypes.c_int(1)
        if self.xi.XIQueryVersion(self.dpy, ctypes.byref(major), ctypes.byref(minor)) != 0:
            return None
        # raw events appeared in XI2.0, but we ask for version 2.1 (the stable
        # contract of raw events); the server may answer with its own, usually 2.4.
        if (major.value, minor.value) < (2, 1):
            return None
        mask = (ctypes.c_ubyte * 4)()
        for evtype in RAW_EVENT_KINDS:
            mask[evtype // 8] |= 1 << (evtype % 8)
        masks = XIEventMask()
        masks.deviceid = XI_ALL_MASTER_DEVICES
        masks.mask_len = len(mask)
        masks.mask = ctypes.cast(mask, ctypes.POINTER(ctypes.c_ubyte))
        # An X error arrives asynchronously: we catch it with the handler and sync
        # before returning, otherwise "the subscription failed" would look like a working XI2.
        errors = []

        def on_error(_dpy, _event):
            errors.append(True)
            return 0

        trap = X_ERROR_HANDLER(on_error)
        previous = self.x.XSetErrorHandler(trap)
        try:
            self.xi.XISelectEvents(self.dpy, self.root, ctypes.byref(masks), 1)
            self.x.XSync(self.dpy, 0)
        finally:
            self.x.XSetErrorHandler(previous)
        if errors:
            return None
        self.xi_opcode = opcode.value
        self.xi_version = (major.value, minor.value)
        self.raw_events = True
        return self.xi_version

    def read_events(self, query_motion=None):
        """Drag events from the X queue; ``[('press'|'motion'|'release', (x, y), button)]``.

        Events that are not ours (MappingNotify, XI2 not from the root and so on) are skipped.
        The data of a generic event must be freed through XFreeEventData —
        otherwise Xlib accumulates it in memory.

        ``query_motion`` — an optional predicate: whether to ask for the position on
        the next ``RawMotion``. Without it we always ask. Press and
        release are always polled: their point is the result itself.
        """
        events = []
        while self.x.XPending(self.dpy):
            buf = EVENT_BUFFER()
            self.x.XNextEvent(self.dpy, ctypes.byref(buf))
            cookie = ctypes.cast(ctypes.byref(buf), ctypes.POINTER(XGenericEventCookie))
            if cookie.contents.type != GENERIC_EVENT:
                continue
            if not self.x.XGetEventData(self.dpy, cookie):
                continue
            try:
                if cookie.contents.extension != self.xi_opcode:
                    continue
                raw = ctypes.cast(cookie.contents.data, ctypes.POINTER(XIRawEvent)).contents
                kind = RAW_EVENT_KINDS.get(raw.evtype)
                if kind is None:
                    continue
                if kind == "motion" and query_motion is not None and not query_motion():
                    continue
                point = self.pointer_point()
                events.append((kind, point, raw.detail))
            finally:
                self.x.XFreeEventData(self.dpy, cookie)
        return events

    def poll_events(self, state):
        """Fallback: one comparison of XQueryPointer samples (``state`` — the previous one)."""
        now = self.pointer_state()
        events = [(kind, point, BUTTON1) for kind, point in poll_events(state, now)]
        return events, (state if now is None else now)

    def close(self):
        if getattr(self, "dpy", None):
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None


class StopFlag:
    """A stop flag + a self-pipe: a signal wakes ``select`` instead of waiting for a timeout.

    The signal handler only sets the flag and writes a byte into the pipe: doing anything
    else in it (stopping the HUD, calling gsettings) is not allowed. ``select`` in the
    main loop watches the read end, so termination on SIGTERM is
    instant and deterministic.
    """

    def __init__(self):
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.reader, False)
        os.set_blocking(self.writer, False)
        self.stopped = False

    def handler(self, _signum=None, _frame=None):
        self.stopped = True
        try:
            os.write(self.writer, b"x")
        except OSError:
            pass

    def drain(self):
        try:
            os.read(self.reader, 4096)
        except (BlockingIOError, OSError):
            pass

    def close(self):
        for fd in (self.reader, self.writer):
            try:
                os.close(fd)
            except OSError:
                pass


def install_signal_handlers(stop):
    """SIGTERM/SIGINT/SIGHUP — into ``StopFlag`` (a tidy shutdown)."""
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(signum, stop.handler)
        except (ValueError, OSError):  # not the main thread / the signal is unavailable
            pass


def serve(watcher, client, stop):
    """The watcher loop: ``select`` on the X fd and the self-pipe, events -> ``Hotcorners``.

    In XI2 mode we wait for X events; in the fallback — a ``POLL_INTERVAL`` timeout and one
    ``XQueryPointer`` poll per iteration. The exit is by the ``stop`` flag (a signal).
    """
    watch = [stop.reader]
    if client.raw_events:
        watch.append(client.fd())
    timeout = None if client.raw_events else POLL_INTERVAL
    state = None
    while not stop.stopped:
        try:
            ready, _, _ = select.select(watch, [], [], timeout)
        except OSError:  # EINTR on some platforms
            continue
        if stop.stopped or stop.reader in ready:
            stop.drain()
            break
        if client.raw_events:
            if client.fd() not in ready:
                continue
            events = client.read_events(getattr(watcher, "motion_due", None))
        else:
            events, state = client.poll_events(state)
        for kind, point, button in events:
            if stop.stopped:
                break
            if kind == "press":
                watcher.on_press(point, button)
            elif kind == "motion":
                watcher.on_motion(point)
            else:
                watcher.on_release(point, button)


# ------------------------------------------------------------------------- CLI


def default_conf():
    return tiler.default_conf()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="zone-hotcorners.py",
        description=_(
            "Dragging a window by its title bar places it into a GridSnap zone (Cinnamon/X11)"
        ),
        epilog=_(
            "The watcher listens to XInput2 raw events on the root (it makes no grabs), drives the zone-picker --hud HUD and places the active window into the zone under the release point. If XI2 is unavailable - XQueryPointer polling at ~30 Hz."
        ),
    )
    ap.add_argument(
        "--conf",
        metavar="PATH",
        default=None,
        help=_("path to zones.conf (XDG or next to the code by default)"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=_("do not move the window and do not change settings - only print what it would do"),
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help=_("print to stderr what is happening (arm, HUD, edge-tiling, drop)"),
    )
    ap.add_argument(
        "--no-hud",
        action="store_true",
        help=_("no overlay: only the drop on release (edge-tiling untouched)"),
    )
    ap.add_argument("--version", action="version", version=f"zone-hotcorners {VERSION}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    conf = args.conf or default_conf()
    if not os.path.exists(conf):
        print(_("zone-hotcorners: config not found: {}").format(conf), file=sys.stderr)
        return EXIT_CONFIG

    stop = StopFlag()
    install_signal_handlers(stop)
    try:
        client = XClient()
    except (OSError, RuntimeError) as exc:
        print(f"zone-hotcorners: {exc}", file=sys.stderr)
        stop.close()
        return EXIT_FAIL

    watcher = None
    try:
        version = client.enable_raw_events()
        if version:
            mode = f"XI2 raw events {version[0]}.{version[1]}"
        else:
            mode = _("fallback: XQueryPointer polling at {:.0f} Hz").format(1.0 / POLL_INTERVAL)
        print(
            _("zone-hotcorners: {}; config {}").format(mode, conf),
            file=sys.stderr,
            flush=True,
        )
        watcher = Hotcorners(
            conf,
            dry_run=args.dry_run,
            no_hud=args.no_hud,
            verbose=args.verbose,
        )
        serve(watcher, client, stop)
        return EXIT_OK
    finally:
        if watcher is not None:
            watcher.finish_drag()  # the HUD is stopped, edge-tiling is restored
        client.close()
        stop.close()


if __name__ == "__main__":
    sys.exit(main())

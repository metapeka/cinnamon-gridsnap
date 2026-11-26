#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zone-daemon — the long-lived GridSnap daemon: it places new windows into zones
by the `[rules]` rules and (optionally) reflows the layout when a window closes.

Runs in Cinnamon / Muffin (X11) through the same ctypes/libX11 and EWMH as
`zone-tiler.py`; external utilities (wmctrl, xdotool) are not needed.

The source of truth about clients is the EWMH list `_NET_CLIENT_LIST` on root: muffin
REPARENTS client windows into frame windows, so `SubstructureNotify` on root
yields `MapNotify` with a frame window that has no `WM_CLASS`. Instead the daemon subscribes to
`PropertyChangeMask` and on every `PropertyNotify` about `_NET_CLIENT_LIST` reads the
list through `xprop -root _NET_CLIENT_LIST` and reconciles the state:
windows that appeared are placed by the rules, vanished ones are dropped (and reflow runs).
Iconifying a window does not remove it from `_NET_CLIENT_LIST`, so reflow does not
fire, and that is the desired behaviour.

Workspaces and screens: the daemon remembers the active workspace (`_NET_CURRENT_DESKTOP`) and
the workspace of every placed window (`_NET_WM_DESKTOP`). The zone grid of a window comes from
its screen: `[monitors]` beats `[workspaces]`, `[workspaces]` beats the common
`[zones]` (`zone_config.resolve_profile`); the workspace profile is
`templates/<name>.conf` from `[workspaces]`. A "sticky" window (`0xFFFFFFFF`) lives in
the common set. Windows of invisible workspaces never move: reflow works
only on the current workspace.

The config is the same `zones.conf` (by default the tiler's path,
`zone-tiler.default_conf()`):

  [rules]
  Chrome = main                       # glob over WM_CLASS (case-insensitive)
  re:^(Navigator|firefox) = main      # regex over the "class|title" string

  [daemon]
  reflow = off                        # off | neighbors (off by default)

Usage:
  zone-daemon.py                 # listen for events until Ctrl-C (and raise the IPC socket)
  zone-daemon.py --verbose       # print to stdout what the daemon does
  zone-daemon.py --once          # handle one _NET_CLIENT_LIST change and exit
  zone-daemon.py --conf PATH
  zone-daemon.py --socket PATH   # a custom path for the IPC UNIX socket
  zone-daemon.py --no-socket     # no IPC (auto-placement only)

IPC is a local UNIX socket (by default `$XDG_RUNTIME_DIR/gridsnap/daemon.sock`,
mode 0600): one JSON command per connection, the answer is a JSON line too.
Commands: `status`, `apply`, `focus`, `reload`, `quit`; the client that talks to the socket is
`gridsnap-ctl`. Framing and the dispatcher live in the pure `zone_ipc.py`.

The "this zone only" mode (`focus`): the target window is placed in the zone, and the other windows
of its workspace and monitor are iconified. The mode lives here, in the daemon, because
only it sees `_NET_CLIENT_LIST` and therefore can iconify windows that appeared
after entering; a repeated command is a toggle, `{"off": true}` is an explicit exit.

Tabs in a zone: several windows of one zone stand on the shared frame (the zone minus
the strip), the top one is fully visible. The owner is the daemon too. A window that lands in a zone
becomes a tab itself; the second tab takes the strip height away from the windows and
repositions the window already standing there. The composition of the stack is derived from
the tracker and the frames (there is no state file), and the strip is drawn by a separate
process `zone-tabstrip.py` (GTK is not added to the daemon): on every change a snapshot
is written to it — `zone_tabs.strips_payload`. The commands `tabs-next` (key) and `tabs-switch`
(click on the strip) arrive at the same socket as the `gridsnap-ctl` commands.
"""

import argparse
import collections
import ctypes
import importlib.util
import json
import os
import re
import select
import subprocess
import sys
import time
from typing import cast

from zone_i18n import _
import window_backend  # the window layer: xprop/xwininfo/libX11 in one place

VERSION = "0.2.0"

HERE = os.path.dirname(os.path.abspath(__file__))

REFLOW_OFF = "off"
REFLOW_NEIGHBORS = "neighbors"

# Delay before the single retry of reading metadata: `_NET_WM_NAME` sometimes
# appears only after the window has landed in `_NET_CLIENT_LIST`.
META_RETRY_DELAY = 0.2

# X11 event codes.
PROPERTY_NOTIFY = 28

# The active workspace (EWMH, on root). Read by the same PropertyNotify loop
# as the client list: the PropertyChangeMask on root is already set.
CURRENT_DESKTOP_ATOM = "_NET_CURRENT_DESKTOP"

# PropertyChangeMask: PropertyNotify about root properties (including _NET_CLIENT_LIST).
PROPERTY_CHANGE_MASK = 1 << 22

# How often a generated `theme.conf` is checked against the wallpaper. The daemon's loop is
# `select()`, not a GLib main loop, so GSettings change signals are not available: the setting is
# polled instead. A wallpaper change is rare, and the check costs one file read plus one dconf
# lookup until the palette is out of date.
THEME_POLL_SECONDS = 5.0

# Timeout for waiting for the single PropertyNotify in `--once` mode.
ONCE_TIMEOUT = 2.0

# XEvent is a 192-byte union on LP64.
EVENT_BUFFER = ctypes.c_char * 192


def _load_sibling(name, filename):
    """Load the adjacent hyphen module (zone-tiler.py) without depending on sys.path."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(_("failed to load module {path}").format(path=path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tiler = _load_sibling("gridsnap_zone_tiler", "zone-tiler.py")
zone_reflow = _load_sibling("gridsnap_zone_reflow", "zone_reflow.py")
zone_ipc = _load_sibling("gridsnap_zone_ipc", "zone_ipc.py")
zone_tabs = _load_sibling("gridsnap_zone_tabs", "zone_tabs.py")
import zone_theme  # noqa: E402  (the palette: a generated theme.conf follows the wallpaper)

# `zone_config` is the pure profile core (`[monitors]`, `[workspaces]`,
# `resolve_profile`). The daemon needs it only when bindings exist, so
# it is loaded lazily: the daemon start must not depend on an extra module.
_zone_config = None


def zone_config_module():
    """Lazily load `zone_config` (the profile parsing core) next to the daemon."""
    global _zone_config
    if _zone_config is None:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import zone_config as module  # noqa: PLC0415 — lazy import, like the tiler's
        _zone_config = module
    return _zone_config

# The tabs strip is a separate GTK process next to the daemon (like `--hud` of
# the hot edges): GTK is not added to the daemon, and for the tab snapshot the
# strip is happy with stdin.
TABSTRIP = os.path.join(HERE, "zone-tabstrip.py")

# How long to check only a freshly started strip process, to tell a launch
# failure ("no GTK": python starts, `zone-tabstrip.py` dies on import with
# code 1) from a working strip. The check is short and only at start: the daemon
# does not wait for the strip, and tabs work without it.
START_VERIFY_DELAY = 0.3
START_POLL_INTERVAL = 0.02


# ------------------------------------------------------------------ metadata


# Filters and EWMH reads shared with the tiler are one source of truth, so that
# `arrange`/`stack` in zone-tiler.py and the daemon filter windows the same way.
IGNORED_WINDOW_TYPES = tiler.IGNORED_WINDOW_TYPES
IGNORED_TYPE_TAILS = tiler.IGNORED_TYPE_TAILS
CLIENT_LIST_ATOM = tiler.CLIENT_LIST_ATOM
window_types = tiler.window_types
window_excluded = tiler.window_excluded
read_client_list = tiler.read_client_list
read_window_center = tiler.read_window_center
monitor_for_point = tiler.monitor_for_point

# The window metadata the daemon needs. `window_type` is a set of
# `_NET_WM_WINDOW_TYPE_*` names (may be empty). Namedtuple: both a tuple and attributes.
Meta = collections.namedtuple("Meta", "wm_class wm_title window_type")


def read_window_meta(win):
    """Default window metadata: WM_CLASS/_NET_WM_NAME + the window type.

    If the class or the title has not appeared yet, re-reads them once after
    a short pause (the title sometimes arrives only after the window appears in the list).
    """
    wm_class, wm_title = tiler.window_class_title(win)
    if not wm_class or not wm_title:
        time.sleep(META_RETRY_DELAY)
        wm_class2, wm_title2 = tiler.window_class_title(win)
        wm_class = wm_class or wm_class2
        wm_title = wm_title or wm_title2
    return Meta(wm_class, wm_title, window_types(win))


class TabStripProcess:
    """The child ``zone-tabstrip.py`` — the tabs strip above the zones.

    The tab snapshot is written to stdin as one JSON line per change (the protocol
    ``zone_tabs.strips_payload``); EOF on stdin ends the main loop of the strip,
    so the stop is a soft EOF first, then ``terminate``/``kill``.

    A launch failure does not bring the daemon down: tabs work without the strip, there is just no
    hint on screen that a zone holds several windows. Repeating a launch that cannot
    succeed (no ``zone-tabstrip.py`` or no GTK) makes no sense: ``failed``.

    "No GTK" does not look like an `OSError` from `Popen`: python starts fine, but
    ``zone-tabstrip.py`` dies on import with code 1, and such a child is no longer
    restarted on every change of the picture. Telling a launch failure from
    the death of a strip that had been working is helped by the short check right
    after `Popen` (`_launch_failure`): a strip that dies later stays restartable.
    """

    def __init__(self, conf, script=None, python=None, verbose=False):
        self.conf = conf
        self.script = script or TABSTRIP
        self.python = python or sys.executable
        self.verbose = verbose
        self.proc = None
        self.failed = False

    def log(self, message):
        if self.verbose:
            print(f"zone-daemon: {message}", file=sys.stderr, flush=True)

    def command(self):
        return [self.python, self.script, "--conf", self.conf]

    def start(self):
        """Start the strip; a running one is not touched, a hopeless one is not retried."""
        if self.failed or self.running():
            return False
        if not os.path.exists(self.script):
            self.failed = True
            self.log(
                _("no tab bar: {script} not found").format(script=self.script)
            )
            return False
        try:
            self.proc = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None if self.verbose else subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.failed = True
            self.log(_("the tab bar failed to start: {exc}").format(exc=exc))
            return False
        code = self._launch_failure()
        if code is not None:
            self.failed = True
            self.stop()  # close the dead child's stdin
            self.log(
                _(
                    "the tab bar failed to start: {script} exited with code {code}"
                ).format(script=self.script, code=code)
            )
            return False
        self.log(_("tab bar started: {script}").format(script=self.script))
        return True

    def _launch_failure(self):
        """The exit code if the child died right after the start, otherwise ``None``.

        `poll()` right after `Popen` is usually still `None`: python has only just
        started. So we wait briefly and poll: a `zone-tabstrip.py` that died on import
        (no GTK) exits almost at once, while a live strip
        survives to the end of the check. The death of a strip that had been working is not a launch
        failure: its `feed` restarts it as before.
        """
        proc = self.proc
        if proc is None:
            return None
        if START_VERIFY_DELAY <= 0:
            return proc.poll()
        deadline = time.monotonic() + START_VERIFY_DELAY
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(START_POLL_INTERVAL)

    def feed(self, line):
        """Hand the strip a snapshot; ``False`` — it did not accept it (died/did not come up)."""
        if self.failed:
            return False
        if not self.running() and not self.start():
            return False
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.stop()
            return False
        return True

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Shut the strip down: EOF on stdin, then terminate/kill. Idempotent."""
        proc, self.proc = self.proc, None
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


def window_rect(win):
    """The window frame with `_NET_FRAME_EXTENTS` compensation (for the placement memory).

    The same geometry that `apply_zone` reads under `--verify`, so the comparison
    "the window is in place / left the zone" does not depend on the window manager frame.
    """
    return tiler.read_frame(win, tiler.frame_extents(win))


# ---------------------------------------------------------------------- config


def read_reflow(conf):
    """The reflow mode from `[daemon]`. No section/value or garbage -> `off`."""
    try:
        value = (conf.get("daemon", "reflow", fallback="off") or "").strip().lower()
    except Exception:
        return REFLOW_OFF
    return value if value in (REFLOW_OFF, REFLOW_NEIGHBORS) else REFLOW_OFF


def read_tabs_height(conf):
    """The tabs strip height from `[tabs]`; no section/value or garbage -> the default.

    The height is a value with a default (``zone_tabs.STRIP_HEIGHT``), not a dogma:
    it is read from the same config as the zones, but the daemon parses it itself — the parsing
    of the ``[tabs]`` section lives entirely in ``zone_config`` at the strip and the tiler.
    """
    try:
        raw = conf.get("tabs", "height", fallback=None)
    except Exception:
        return zone_tabs.STRIP_HEIGHT
    if raw is None:
        return zone_tabs.STRIP_HEIGHT
    try:
        height = int(str(raw).strip())
    except (TypeError, ValueError):
        return zone_tabs.STRIP_HEIGHT
    # 0/negative/absurdly large height — not a strip but garbage in the config.
    if not 0 < height < 200:
        return zone_tabs.STRIP_HEIGHT
    return height


# --------------------------------------------------------------- placement memory

# The memory of where the daemon put a window lives across runs: otherwise the startup
# reconciliation after a restart would drag back into the zone a window that
# the user moved out of it. It is state, not config (it is not migrated) and not
# a cache (a cleaner may wipe it) — hence `XDG_STATE_HOME`.
PLACED_STATE_VERSION = 1

# How far the window frame may differ from the remembered one for the window to still
# count as standing in its zone. A couple of pixels is window/application jitter after
# the placement; a real move by the user is noticeably larger.
PLACED_TOLERANCE = 8

# `_NET_MOVERESIZE_WINDOW` is a client message, the WM does not apply it instantly.
# If the first read caught the old frame, we wait this long and read once more,
# otherwise the window would be considered "out of the zone" right away.
PLACED_RETRY_DELAY = 0.15


def placed_path():
    """The placement memory path: `$XDG_STATE_HOME/gridsnap/placed.json`."""
    return os.path.join(tiler.state_home(), "gridsnap", "placed.json")


def read_session_id():
    """The X session id (`_NET_SUPPORTING_WM_CHECK`) or None — see `window_backend`.

    The placement memory must not carry over into a new X session: XIDs are handed out
    anew there, and an old record could "hit" a foreign window of the same class.
    Not read — `None`, then the check is off.
    """
    return window_backend.read_session_id()


def frame_matches(actual, placed, tolerance=PLACED_TOLERANCE):
    """Whether the frames match within the tolerance. A pure function — for tests."""
    if actual is None or placed is None or len(actual) != 4 or len(placed) != 4:
        return False
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(actual, placed))


def _state_window_id(raw):
    """The XID from a state file key; not a number — `None`."""
    try:
        win = int(raw)
    except (TypeError, ValueError):
        return None
    return win if win > 0 else None


def _state_frame(value):
    """A frame `[x, y, w, h]` from JSON; a malformed one — `None`."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError):
        return None


def _state_record(record):
    """A placement record from JSON; a malformed one — `None`.

    `frame` and a non-empty `zone` are mandatory: without a zone the record is useless — there is nowhere
    to return the window. `monitor`/`workspace` are optional (a sticky window,
    an unidentified workspace), `wm_class` is needed to tell a reused XID.
    """
    if not isinstance(record, dict):
        return None
    frame = _state_frame(record.get("frame"))
    zone = record.get("zone")
    if frame is None or not isinstance(zone, str) or not zone:
        return None
    monitor = _state_frame(record.get("monitor"))
    workspace = record.get("workspace")
    if workspace is not None:
        try:
            workspace = int(workspace)
        except (TypeError, ValueError):
            workspace = None
    return {
        "frame": frame,
        "wm_class": str(record.get("wm_class") or ""),
        "zone": zone,
        "monitor": monitor,
        "workspace": workspace,
    }


def load_placed_state(path, session=None):
    """Placement memory from the file: `{XID: record}`; no file/broken — `{}`.

    The state file is auxiliary: the daemon must come up without it too, so
    read errors are not raised upwards, and malformed records are dropped one by one.
    Records of another X session (if both the file and the argument know its id) are dropped
    entirely: XIDs are handed out anew in a new session.
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != PLACED_STATE_VERSION:
        return {}
    file_session = data.get("session")
    if session is not None and file_session is not None and file_session != session:
        return {}
    windows = data.get("windows")
    if not isinstance(windows, dict):
        return {}
    result = {}
    for raw_win, record in windows.items():
        win = _state_window_id(raw_win)
        parsed = _state_record(record)
        if win is not None and parsed is not None:
            result[win] = parsed
    return result


def save_placed_state(path, state, session=None):
    """Write the placement memory atomically (`.tmp` + `os.replace`)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = {"version": PLACED_STATE_VERSION, "windows": state}
    if session is not None:
        data["session"] = session
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# -------------------------------------------------------------------- geometry


def fractions_to_pixels(fx, fy, fw, fh, wa):
    """Work-area fractions -> a pixel frame (x, y, w, h).

    The edges are counted from the same lines (`round(ww*(fx+fw))`), as in
    `Grid.rect` — neighbouring zones join without a gap. Width/height >= 1 px.
    """
    wx, wy, ww, wh = wa
    x1, x2 = wx + round(ww * fx), wx + round(ww * (fx + fw))
    y1, y2 = wy + round(wh * fy), wy + round(wh * (fy + fh))
    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


def workspace_label(workspace):
    """The workspace for the log: the EWMH number (from zero) or an "unknown" marker."""
    return _("unknown") if workspace is None else f"#{workspace}"


# ----------------------------------------------------------------------- daemon


class Daemon:
    """The daemon core without X: all event reaction is in `on_map`/`on_destroy`/`on_unmap`.

    Dependencies are injected, so scenarios run under unit tests without
    a display:

      conf_path  — the zones.conf path (by default — the tiler's);
      apply      — callable(window, rect, label=..., quiet=...) (by default —
                   `zone-tiler.apply_zone` with the shared connection `self.x11`,
                   which `serve` sets; a foreign one passed in is not closed);
      read_meta  — callable(window) -> Meta (by default `read_window_meta`);
      workarea   — a forced single work area: callable() -> (x, y, w, h)
                   or the tuple itself (single-screen mode and tests). With `None`
                   the window monitor is determined by `monitors` and `window_center`;
      monitors   — callable() -> a list of (x, y, w, h) monitor work areas
                   (by default `tiler.monitors`, called lazily);
      window_center — callable(window) -> (px, py) or None (by default
                   `read_window_center`, called lazily);
      current_workspace — callable() -> the active workspace number (by default
                   `tiler.current_workspace`); read once in `__init__`
                   and then on every PropertyNotify about `_NET_CURRENT_DESKTOP`;
      window_workspace — callable(window) -> the window's workspace number or None
                   (by default `tiler.window_workspace_for_zones`; None is also
                   what a "sticky" window gives — it lives in the common zone set);
      read_clients — callable() -> a list of ids from `_NET_CLIENT_LIST` or None
                   (`read_client_list`); tabs need the window order bottom to top;
      active_window — callable() -> the active window id (`tiler.active_window`):
                   `tabs-next` switches the stack of its zone, and the strip highlights
                   the active tab;
      strip      — the tabs strip process (`TabStripProcess` or a stand-in in
                   tests) or `None` — there is no strip; its failure does not bring the daemon down;
      refresh_theme — callable() -> the written path or None: rebuilds a generated
                   `theme.conf` from the wallpaper (by default
                   `zone_theme.refresh_from_wallpaper` on `conf_path`). Only called when
                   `theme_interval` is set, and only a file that carries the marker is touched;
      theme_interval — seconds between `refresh_theme` calls (`None` — off; `main` sets
                   `THEME_POLL_SECONDS` unless `--once`: a probe must not write the palette);
      verbose    — whether to print what the daemon does.

    `windows` is the tracker `window id -> zone name`: it also filters out repeated
    appearances of a window (iconify/restore do not move a window). `win_monitor`
    holds the work area the window was placed in; `win_workspace` — the workspace
    it was placed on (`None` — sticky/unknown workspace); `filled` —
    the covered zones by workspace and monitor. The workspace counts wherever
    occupancy is computed: a window of workspace 2 does not make a zone taken for reflow on workspace 1.

    The entry point from the X11 side is `reconcile(client_ids)`: the list of ids of the current
    clients from `_NET_CLIENT_LIST`. New ids (not in `seen`) go
    through `on_map`, ones that vanished from the tracker — through `on_destroy`. `seen` keeps
    all ids of the last list, including those that did not match the rules — so their
    metadata is not re-read on every reconciliation.

    Closing a window (`on_destroy`) drops it from the tracker and, if
    `reflow=neighbors` is on, reflows the layout — but only when the workspace
    of the closed window is visible (`self.workspace`); otherwise the state is cleaned, and windows
    on other workspaces do not move. Hiding a window (`on_unmap`: iconifying,
    a workspace switch) changes nothing in `_NET_CLIENT_LIST`, so
    reconciliation does not see it: the window stays in the tracker, we do not move the neighbours.
    A change of the active workspace (`on_workspace_change`) updates only
    `self.workspace` — windows are not moved.

    The "this zone only" mode (`self.focus`, the `focus` command): the target window is placed
    in the zone, and the windows of the same workspace and monitor are iconified; their ids land in
    `minimized`, and only they are restored on exit (a window iconified by hand
    stays iconified). The mode drops itself when the target vanished, the
    active workspace changed or the daemon terminates — and then the windows come back. The tabs
    of the target zone the mode does not iconify: they are part of that zone the owner
    asked to keep.

    Tabs in a zone (`_zone_stack`, `_settle_in_zone`, `tabs_picture`): windows
    of one zone on one workspace and monitor stand on the shared frame. A single window
    occupies the whole zone, two or more — the zone minus the strip
    (`zone_tabs.tab_rect`); the second tab moves the first. The snapshot of the stacks
    goes to `self.strip` as one `zone_tabs.strips_payload` line.
    """

    def __init__(
        self,
        conf_path=None,
        apply=None,
        read_meta=None,
        workarea=None,
        monitors=None,
        monitor_scales=None,
        window_center=None,
        current_workspace=None,
        window_workspace=None,
        state_path=None,
        read_rect=None,
        session_id=None,
        focus_path=None,
        iconify=None,
        deiconify=None,
        read_clients=None,
        active_window=None,
        strip=None,
        monitor_outputs=None,
        subscribers=None,
        refresh_theme=None,
        theme_interval=None,
        verbose=False,
    ):
        self.conf_path = conf_path or tiler.default_conf()
        self.grid = tiler.Grid(self.conf_path)
        self.x11 = None  # the connection is set by serve(); apply gets a foreign one
        self.apply = apply or self._default_apply
        self.read_meta = read_meta or read_window_meta
        self.workarea = workarea
        self.monitors = monitors if monitors is not None else tiler.monitors
        # Monitor scales: they show that the scale was changed on the fly and the
        # work-area cache is stale.
        self.monitor_scales = (
            monitor_scales if monitor_scales is not None else tiler.monitor_scales
        )
        # [(output name, work area)] — the name addresses the profile in `[monitors]`.
        self.monitor_outputs = (
            monitor_outputs if monitor_outputs is not None else tiler.monitor_outputs
        )
        self.window_center = window_center or read_window_center
        self.read_current_workspace = current_workspace or tiler.current_workspace_or_first
        self.read_window_workspace = window_workspace or tiler.window_workspace_for_zones
        # The window frame is read by the placement memory (the "in place/gone" comparison).
        self.read_rect = read_rect or window_rect
        # Iconify/restore and the client list are injected like apply/read_meta:
        # tests must not touch X, while the running daemon calls the shared primitives
        # of the tiler (`tiler.iconify`/`tiler.deiconify`).
        self.iconify = iconify or tiler.iconify
        self.deiconify = deiconify or tiler.deiconify
        self.read_clients = read_clients or read_client_list
        # The active window is read by tabs: it selects the stack for
        # `tabs-next` and marks the visible tab in the snapshot for the strip.
        self.active_window = active_window or tiler.active_window
        # The tabs strip is a separate GTK process (`TabStripProcess`) or None:
        # tabs work without it, there is just no hint on screen.
        self.strip = strip
        # Event subscribers (`zone_ipc.Subscribers`): the same registry
        # holds the listening socket (`main` passes it to `Server`) and the fan-out
        # (`_emit`). The injection is for unit tests without a socket.
        self.subscribers = (
            subscribers if subscribers is not None else zone_ipc.Subscribers()
        )
        # The strip height from `[tabs]`; no section or garbage — the core default.
        self.tabs_height = read_tabs_height(self.grid.conf)
        # The last snapshot handed to the strip: identical ones are not written again.
        self._strips_last = None
        # The state file path; `None` — in memory only (tests, utility runs).
        self.state_path = state_path
        # The `focus.json` path — where the unrestored remainder of the mode goes on
        # daemon termination. `None` (tests, utility runs) — to the log only.
        self.focus_path = focus_path
        # The X session id: a record from another session (the same XIDs, but foreign)
        # is not picked up. `None` — the check is off.
        self.session_id = session_id
        self.verbose = verbose
        self.reflow = read_reflow(self.grid.conf)
        self.windows = {}
        # The work area the window was placed in: window id -> (x, y, w, h).
        # Reflow needs it so as not to expand a neighbour from another monitor.
        self.win_monitor = {}
        # The workspace the window was placed on: window id -> workspace number or None
        # (a sticky window). A window of another workspace does not make a zone taken on this one.
        self.win_workspace = {}
        # The frame the daemon applied: window id -> (x, y, w, h). It shows
        # that the user moved the window out of the zone (it is no longer where it was put).
        self.placed = {}
        # `WM_CLASS` of the placed window — a state record must tell apart
        # a reused XID when adopting after a restart.
        self.win_class = {}
        # Windows the user moved out of their zones: window id -> record.
        # The daemon no longer moves them until the window is reopened (a new XID).
        self.detached = {}
        # The previous run's memory: resolved on the very first reconciliation.
        self._pending = load_placed_state(state_path, session_id) if state_path else {}
        # Zones covered by an expanded neighbour (reflow=neighbors): a triple
        # (workspace, work area, freed zone) -> the holder window.
        # Coverage is keyed by workspace and monitor: the same zone on different
        # workspaces and screens is independent, and reflow of one workspace does not touch another.
        self.filled = {}
        # Workspace grids computed from `[workspaces]`: workspace -> Grid. The cache
        # is reset by `reload`, so a new profile is picked up without a restart.
        self._grids = {}
        # The active workspace (EWMH numbering from zero); None — cannot be determined.
        self.workspace = self._read_current_workspace()
        # All ids from the last `_NET_CLIENT_LIST` (both placed and without a rule) —
        # so that metadata is not re-read on every reconciliation.
        self.seen = set()
        # The "this zone only" mode (None — off): a dict with the fields
        # `zone`, `target`, `workspace`, `monitor` and the set `minimized` —
        # the windows the mode itself iconified (those and only those it restores).
        self.focus = None
        self._wa_cache = None
        self._mons_cache = None
        # The scale fingerprint `_mons_cache` was computed with.
        self._mons_scale = None
        # [(output name, work area)] from `xrandr --listmonitors`: the output name
        # addresses the screen profile in `[monitors]`. Cached for the life of the config.
        self._outputs_cache = None
        # `[monitors]`/`[workspaces]` of the active config: parsed once per
        # reload — bindings change only by editing the file.
        self._monitor_map_cache = None
        self._workspace_map_cache = None
        # The `quit` command over the socket: the `serve` loop checks the flag and exits.
        self.stop_requested = False
        # A generated `theme.conf` follows the wallpaper: `poll_theme` calls this from the
        # `serve` loop every `theme_interval` seconds (None — never) and restarts the tab strip
        # when the file was rewritten.
        self.refresh_theme = (
            refresh_theme
            if refresh_theme is not None
            else lambda: zone_theme.refresh_from_wallpaper(self.conf_path)
        )
        self.theme_interval = theme_interval
        self._theme_checked = None
        self._theme_error = None

    # -- helpers

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    def poll_theme(self, now=None):
        """Rebuild a generated `theme.conf` when the wallpaper behind it changed.

        Called from the `serve` loop; throttled by `theme_interval`, because X events are many
        while a wallpaper change is rare. Returns the written path or `None`. The tab strip read
        the palette when it started, so a rewrite restarts it; the picker and the editor read the
        file themselves. A failure (an unreadable image, a broken GSettings) is logged once per
        distinct message: a verbose daemon must not repeat it every interval.
        """
        if not self.theme_interval:
            return None
        now = time.monotonic() if now is None else now
        if self._theme_checked is not None and now - self._theme_checked < self.theme_interval:
            return None
        self._theme_checked = now
        try:
            path = self.refresh_theme()
        except (Exception, SystemExit) as exc:
            message = _("theme.conf was not refreshed: {exc}").format(exc=exc)
            if message != self._theme_error:
                self._theme_error = message
                self.log(message)
            return None
        self._theme_error = None
        if not path:
            return None
        self.log(_("theme.conf rebuilt from the wallpaper: {path}").format(path=path))
        self._restart_strip_after_theme()
        return path

    def _restart_strip_after_theme(self):
        """The tab strip took its colours at start: a rewritten palette needs a new process."""
        if self.strip is None:
            return
        was_running = self.strip.running()
        self.strip.stop()
        # The next snapshot has to be written again: the new process has seen nothing yet.
        self._strips_last = None
        if was_running:
            self.sync_strips()

    # -- events to the outside

    def _emit(self, kind, build):
        """Emit an event to the subscribers; without them the line is not even built.

        `build` is a closure that assembles the protocol line: events fire for every
        window, and there usually are no subscribers, so the JSON is assembled only
        when there is someone to send it to. A fan-out failure (a foreign client) does not bring the daemon down.
        """
        subscribers = getattr(self, "subscribers", None)
        if subscribers is None or not subscribers.wants(kind):
            return 0
        try:
            return subscribers.broadcast(kind, build())
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to broadcast event '{kind}': {exc}").format(
                    kind=kind, exc=exc
                )
            )
            return 0

    def _emit_appeared(self, win):
        """A window appeared in `_NET_CLIENT_LIST`; `zone` is where it was placed (or None)."""
        self._emit(
            "window-appeared",
            lambda: zone_ipc.event_window_appeared(
                win,
                self.windows.get(win),
                self.win_workspace.get(win),
                self.win_monitor.get(win),
            ),
        )

    def _emit_closed(self, win, zone):
        """A window left the client list; `zone` is where it stood last."""
        self._emit("window-closed", lambda: zone_ipc.event_window_closed(win, zone))

    # -- workspaces

    def _read_current_workspace(self):
        """The active workspace; `None` only when X does not answer.

        Read through `tiler.current_workspace_or_first`: in a Wayland session Cinnamon
        does not publish `_NET_CURRENT_DESKTOP` until the workspace is switched for the first time
        (measured: ``docs/RESEARCH-wayland-recon.md``), and without that margin the daemon would treat
        the workspace as unknown and would not reflow. A substituted reader may also raise:
        a workspace is no reason to crash, with an unknown workspace reflow simply does not move windows.
        """
        try:
            return self.read_current_workspace()
        except (Exception, SystemExit) as exc:
            self.log(_("failed to determine the active workspace: {exc}").format(exc=exc))
            return None

    def _window_workspace(self, win):
        """The workspace of a window for choosing the grid; `None` — "sticky" or not identified."""
        try:
            return self.read_window_workspace(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to determine the workspace of window 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return None

    def _visible(self, workspace):
        """Whether the window's workspace is visible now — that is, whether this window may be moved.

        `None` (a sticky window or an unidentified workspace) — always visible. An unknown
        active workspace (`self.workspace is None`) makes nobody visible:
        moving windows blindly is worse than not moving them.
        """
        if workspace is None:
            return True
        return workspace == self.workspace

    def _grid_for(self, workspace=None, output=None):
        """The grid of a window: the profile of the screen, of the workspace, or the common `zones.conf` set.

        `output` is the output name of the window's monitor (`HDMI-2`), `workspace` is the window's
        workspace from EWMH (zero-based). `None`/`None` (a sticky or unidentified window) — the usual
        `Grid(conf_path)`: the common zone set, as fixed by the `grid_for_workspace`
        contract. Bindings are resolved in the order `[monitors]` →
        `[workspaces]` → the active config (`zone_config.resolve_profile`), the fallback
        to the next level is in `_profile_grid`.

        Grids are cached by the pair (workspace, output), so the config and the profile are not
        re-read for every window; `reload` resets the cache. A broken config, profile
        or bindings (including `SystemExit` from `fail()`) do not bring the daemon down: the common
        set is taken.
        """
        if workspace is None and output is None:
            return self.grid
        key = (workspace, output)
        cached = self._grids.get(key)
        if cached is not None:
            return cached
        try:
            grid = self._profile_grid(workspace, output)
        except (Exception, SystemExit) as exc:
            self.log(
                _(
                    "failed to take the profile grid ({desk}, monitor {output}): {exc} - using the default set"
                ).format(
                    desk=workspace_label(workspace),
                    output=output or _("unknown"),
                    exc=exc,
                )
            )
            grid = self.grid
        self._grids[key] = grid
        return grid

    def _profile_grid(self, workspace, output):
        """The grid by bindings: the screen beats the workspace, the workspace beats the active config.

        The screen profile is looked up in `[monitors]`, the workspace profile in `[workspaces]`;
        the resolution order is one for all — `zone_config.resolve_profile` (the EWMH workspace
        number it also translates into the config numbering from 1). No profile on disk —
        **fallback to the next level** with a log entry: a layout on a screen
        without a profile is still needed, refusing to work is worse than the common zone set.
        """
        zc = zone_config_module()
        monitor_map = self._monitor_map()
        workspace_map = self._workspace_map()
        levels = []
        if output:
            levels.append(
                (output, workspace, _("of monitor {output}").format(output=output))
            )
        if workspace is not None:
            desk = _("of workspace {desk}").format(desk=workspace_label(workspace))
            levels.append((None, workspace, desk))
        tried = set()
        for monitor, desk, where in levels:
            name = zc.resolve_profile(monitor_map, workspace_map, monitor, desk)
            if not name or name in tried:
                continue
            tried.add(name)
            try:
                path = zc.profile_path(name, self.conf_path)
            except ValueError as exc:
                self.log(
                    _(
                        "bad profile name \"{name}\" ({where}): {exc} - falling back to the next level"
                    ).format(name=name, where=where, exc=exc)
                )
                continue
            if not os.path.exists(path):
                self.log(
                    _(
                        "profile \"{name}\" ({where}) is not on disk: {path} - falling back to the next level"
                    ).format(name=name, where=where, path=path)
                )
                continue
            return tiler.Grid(path)
        return self.grid

    def _monitor_map(self):
        """`[monitors]` of the active config (output → profile); cached per reload."""
        if self._monitor_map_cache is None:
            try:
                self._monitor_map_cache = dict(
                    zone_config_module().load_monitor_map(self.conf_path)
                )
            except (Exception, SystemExit) as exc:
                self.log(
                    _(
                        "failed to read [monitors]: {exc} - monitors are not bound"
                    ).format(exc=exc)
                )
                self._monitor_map_cache = {}
        return self._monitor_map_cache

    def _workspace_map(self):
        """`[workspaces]` of the active config (workspace from 1 → profile); cached per reload."""
        if self._workspace_map_cache is None:
            try:
                self._workspace_map_cache = dict(
                    zone_config_module().load_workspace_map(self.conf_path)
                )
            except (Exception, SystemExit) as exc:
                self.log(
                    _(
                        "failed to read [workspaces]: {exc} - using the default set"
                    ).format(exc=exc)
                )
                self._workspace_map_cache = {}
        return self._workspace_map_cache

    def _outputs(self):
        """[(output name, work area)] with caching; a failure gives an empty list.

        The output name (`HDMI-1`) addresses the profile in `[monitors]`, and the work area
        is what the daemon already knows about the window's monitor. `monitor_outputs` through
        `fail()` raises `SystemExit` when the X server refuses (not a subclass of
        `Exception`), so both are caught; a failure is not cached.
        """
        if self._outputs_cache is None:
            try:
                pairs = self.monitor_outputs()
                self._outputs_cache = [
                    (str(name), tuple(int(v) for v in area)) for name, area in pairs
                ]
            except (Exception, SystemExit) as exc:
                self.log(
                    _("failed to match monitor outputs: {exc}").format(
                        exc=exc
                    )
                )
                return []
        return self._outputs_cache

    def _output_for(self, wa):
        """The output name of the monitor with work area `wa` (`HDMI-1`); `None` if there is none.

        `None` — the screen is not bound (`[monitors]` is empty) or the output did not match
        the work area. Then `xrandr` is not called at all, and the profile is resolved
        by the next level (workspace/active config).
        """
        if wa is None or not self._monitor_map():
            return None
        try:
            rect = tuple(int(v) for v in wa)
        except (TypeError, ValueError):
            return None
        for name, area in self._outputs():
            if area == rect:
                return name
        return None

    def _forced_workarea(self):
        """A forced single work area; `None` if it cannot be obtained."""
        if self._wa_cache is None:
            try:
                wa = self.workarea() if callable(self.workarea) else self.workarea
                self._wa_cache = tuple(cast("tuple[int, int, int, int]", wa))
            except (Exception, SystemExit) as exc:
                self.log(
                    _("failed to get the work area: {exc}").format(exc=exc)
                )
                return None
        return self._wa_cache

    def _monitor_scale_now(self):
        """The fingerprint of monitor scales; `None` — they cannot be asked.

        Read cheaply (GDK, `Gdk.Monitor.get_scale_factor()`) and without
        subprocesses, unlike the work areas themselves, and therefore good for
        a check on every cache access: the scale changes in the screen settings,
        and the user has no reason to wait for `reload`. A scale read failure is
        not an error: then there is no fingerprint and the cache lives on as it did.
        """
        try:
            scales = (
                self.monitor_scales()
                if callable(self.monitor_scales)
                else self.monitor_scales
            )
        except (Exception, SystemExit):
            return None
        if scales is None:
            return None
        try:
            return tuple(int(v) for v in scales)
        except (TypeError, ValueError):
            return None

    def _drop_stale_caches(self):
        """Forget the work areas if the monitor scale changed on the fly.

        The GDK work area changes together with the scale (`GDK_SCALE=2` — half
        as much), while the window moves in X physical pixels: without the reset the daemon would
        place windows by the old pixels until `reload`. The `workarea` stand-in is no
        exception here — it has the same single cache `_wa_cache`.
        """
        scale_now = self._monitor_scale_now()
        if scale_now is None:
            return
        if self._mons_scale is None:
            self._mons_scale = scale_now  # no caches yet — remember the fingerprint
            return
        if scale_now == self._mons_scale:
            return
        self.log(_("screen scale changed — re-reading work areas"))
        self._wa_cache = None
        self._mons_cache = None
        self._mons_scale = scale_now

    def _monitors(self):
        """The list of monitor work areas, cached.

        `tiler.monitors()` through `fail()` raises `SystemExit` when the
        X server refuses — that is not a subclass of `Exception`. The daemon survives the error:
        it logs it and returns `None`, and the caller skips the placement.
        A failure is not cached, a success is. `KeyboardInterrupt` is not caught.

        A scale changed on the fly resets the cache (see `_drop_stale_caches`).
        """
        self._drop_stale_caches()
        if self._mons_cache is None:
            try:
                mons = cast(
                    "list[tuple[int, int, int, int]]",
                    self.monitors() if callable(self.monitors) else self.monitors,
                )
                self._mons_cache = [tuple(wa) for wa in mons]
            except (Exception, SystemExit) as exc:
                self.log(_("failed to get monitors: {exc}").format(exc=exc))
                return None
        return self._mons_cache

    def _monitor_of(self, win):
        """The work area of the monitor of a window; `None` if it cannot be determined.

        With `workarea` set it is always returned (single-screen mode).
        Otherwise the area containing the window centre is taken; if the centre fell into none
        of them — the nearest by centre. An unknown window position (`xwininfo`
        did not answer) gives `None`: it is better not to touch the window than to guess monitor 0.
        """
        if self.workarea is not None:
            self._drop_stale_caches()
            return self._forced_workarea()
        monitors = self._monitors()
        if not monitors:
            return None
        try:
            point = self.window_center(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to determine the position of window 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return None
        if point is None:
            return None
        return monitor_for_point(monitors, point)

    def _default_apply(self, win, rect, label="", quiet=False):
        """The default closure: places a window through the daemon's shared X connection.

        `self.x11` may be set later (`serve` sets it before
        reconciliation), so it is taken from the attribute at call time rather than
        remembered. A substituted `apply` does not touch this closure.
        """
        tiler.apply_zone(win, rect, label=label, quiet=quiet, x11=self.x11)

    def _apply(self, win, rect, zone):
        """Place a window without bringing the long-lived daemon down over one error.

        `SystemExit` is caught too: `fail()` in the layout code raises it when the
        X server refuses (for example, connections are exhausted), and it is not a subclass of
        `Exception`. `KeyboardInterrupt` is not caught. A successful placement
        is remembered: later its frame shows that the window left the zone.

        `window-moved` is not sent from here: this funnel has no new window
        coordinates (the caller writes the monitor and the workspace to the tracker after `_apply`), and
        for most calls the zone does not change either — aligning tabs, returning the
        reflow holder, returning the whole zone to a single tab. A move
        is reported by `_settle_in_zone`: it knows both the previous zone and the new one.
        """
        try:
            self.apply(win, rect, label=zone, quiet=True)
        except (Exception, SystemExit) as exc:  # the window could have vanished / X could have refused
            self.log(
                _("failed to place window 0x{win:x} into zone '{zone}': {exc}").format(
                    win=win, zone=zone, exc=exc
                )
            )
            return False
        self._remember_placement(win, rect)
        return True

    # -- placement memory

    def _read_rect(self, win):
        """The current frame of a window; `None` if it cannot be read (no X, the window is gone)."""
        try:
            rect = self.read_rect(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to read the frame of window 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return None
        if rect is None:
            return None
        try:
            return tuple(int(v) for v in rect)
        except (TypeError, ValueError):
            return None

    def _remember_placement(self, win, rect):
        """Remember the placement frame: it shows that the window left the zone.

        The actual frame is taken (the WM may have corrected the frame), and if it cannot be
        read — the requested one. The WM applies `_NET_MOVERESIZE_WINDOW` not
        instantly, so a frame that does not match is re-read once more: otherwise a window
        the daemon has just placed would be considered "out of the zone" right away.
        The state file is written by the callers, once the tracker is already updated.
        """
        frame = self._read_rect(win)
        if frame is not None and not frame_matches(frame, rect):
            time.sleep(PLACED_RETRY_DELAY)
            again = self._read_rect(win)
            if again is not None:
                frame = again
        self.placed[win] = frame or tuple(int(v) for v in rect)
        self.detached.pop(win, None)

    def _placement_record(self, win, frame):
        """A placement record for the state file."""
        wa = self.win_monitor.get(win)
        return {
            "frame": [int(v) for v in frame],
            "wm_class": self.win_class.get(win, ""),
            "zone": self.windows.get(win),
            "monitor": [int(v) for v in wa] if wa else None,
            "workspace": self.win_workspace.get(win),
        }

    def _save_state(self):
        """Save the placement memory (a write failure does not bring the daemon down)."""
        if not self.state_path:
            return
        state = {
            str(win): self._placement_record(win, frame)
            for win, frame in self.placed.items()
        }
        for win, record in self.detached.items():
            state[str(win)] = dict(record)
        try:
            save_placed_state(self.state_path, state, self.session_id)
        except OSError as exc:
            self.log(
                _("failed to save the memory of placements: {exc}").format(exc=exc)
            )

    def _detach(self, win):
        """Drop a window from the tracker: it is no longer in its zone (the user moved it out).

        The zone is freed (`filled` records pointing at the holder are dropped), the window
        goes to `detached` — it will not come back by itself, neither on a new appearance
        nor after a daemon restart (until the XID changes).
        """
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        workspace = self.win_workspace.pop(win, None)
        frame = self.placed.pop(win, None)
        wm_class = self.win_class.pop(win, None)
        self._drop_coverage(win)
        if frame is not None:
            self.detached[win] = {
                "frame": [int(v) for v in frame],
                "wm_class": wm_class or "",
                "zone": zone,
                "monitor": [int(v) for v in wa] if wa else None,
                "workspace": workspace,
            }
        self._save_state()
        self.log(
            _("window 0x{win:x} left zone '{zone}' - not moving it anymore").format(
                win=win, zone=zone
            )
        )

    def _drop_coverage(self, win):
        """Drop the coverages held by `win`: the window was moved out of the zone.

        `filled` is the book of "who expanded over someone else's zone". The holder was moved
        (the stack put it on the shared frame, it stayed the only
        tab, it left the zone or it was closed) — the record of the covered zone
        is no longer true, and `status.filled` must not show it. The geometry
        does not drift because of this: the record would be dropped later anyway, when the zone is taken again.
        """
        dropped = False
        for key, holder in list(self.filled.items()):
            if holder == win:
                del self.filled[key]
                dropped = True
        return dropped

    def _refresh_detached(self):
        """Notice the windows the user moved out of their zones.

        The current frame of the window is compared with the one the daemon applied: it differs
        by more than the tolerance — the window is gone. Its records are dropped, the zone is freed for
        reflow, and the daemon does not place it again. The frame cannot be read — we assume
        the window is in place (not moving is worse than missing a move).
        """
        for win in list(self.windows):
            if not self._visible(self.win_workspace.get(win)):
                continue  # we do not move a window of an invisible workspace — nor count it as gone
            placed = self.placed.get(win)
            if placed is None:
                continue
            actual = self._read_rect(win)
            if actual is None:
                continue
            if not frame_matches(actual, placed):
                self._detach(win)

    def _zone_rect_for(self, win, record):
        """The frame of a record's zone on its monitor; `None` — cannot be computed.

        Needed to tell "the window stands in its zone" from "the window is expanded
        by reflow onto the neighbour": in the second case the recorded frame is a union, not
        a zone.
        """
        wa = tuple(record["monitor"]) if record.get("monitor") else None
        if wa is None:
            wa = self._monitor_of(win)
        if wa is None:
            return None
        try:
            grid = self._grid_for(record.get("workspace"), self._output_for(wa))
            return grid.rect(record["zone"], wa)
        except (Exception, SystemExit) as exc:
            self.log(
                _(
                    "failed to compute zone '{zone}' of record 0x{win:x}: {exc}"
                ).format(zone=record["zone"], win=win, exc=exc)
            )
            return None

    def _adopt_placement(self, win, record):
        """Take a record of the previous run into account: adopt the window or mark it gone.

        The frame matched the remembered one — the window is still where it was put: we take it
        under watch without `apply`. It differs — the user moved it out, and the daemon
        no longer moves the window. If the recorded frame is not a zone but a union with
        the neighbour (a neighbour expanded by reflow), the window is returned to its zone
        through `on_map` too. `False` — the record is not about this window (the XID was reused
        by another application or the class did not match): the window takes the usual path
        `on_map`.
        """
        try:
            meta = self.read_meta(win)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to read metadata of 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return False
        wm_class = getattr(meta, "wm_class", "") or ""
        recorded_class = record.get("wm_class") or ""
        if recorded_class and wm_class and recorded_class != wm_class:
            return False
        actual = self._read_rect(win)
        if actual is not None and frame_matches(actual, record["frame"]):
            zone_rect = self._zone_rect_for(win, record)
            if zone_rect is not None and not self._in_zone_frame(actual, zone_rect):
                # The frame is not its own zone: this is a neighbour expanded by reflow, and the
                # book of coverages (`filled`) does not live in the state file. We let the window
                # take the usual path `on_map` — the rule returns it into the zone from
                # the config, and the new window does not land on top.
                self.log(
                    _(
                        "window 0x{win:x} is not in its zone '{zone}' - laying it out by the rule"
                    ).format(win=win, zone=record["zone"])
                )
                return False
            self.windows[win] = record["zone"]
            if record.get("monitor"):
                self.win_monitor[win] = tuple(record["monitor"])
            self.win_workspace[win] = record.get("workspace")
            self.placed[win] = tuple(record["frame"])
            self.win_class[win] = wm_class or recorded_class
            self.log(
                _(
                    "window 0x{win:x} stayed in zone '{zone}' - taking it under watch"
                ).format(win=win, zone=record["zone"])
            )
            return True
        self.detached[win] = dict(record)
        self.log(
            _("window 0x{win:x} left zone '{zone}' - it will not come back on its own").format(
                win=win, zone=record["zone"]
            )
        )
        return True

    def _in_zone_frame(self, actual, zone_rect):
        """Whether the window stands in its zone: on the zone frame or on the shared frame of tabs.

        The tab frame is the same zone minus the strip. Without this check the placement
        memory would decide that a tab window was moved out of the zone and would return it
        back to the zone frame (and the whole stack with it).
        """
        if frame_matches(actual, zone_rect):
            return True
        return frame_matches(actual, self._tab_frame(zone_rect))

    def _apply_pending(self, current):
        """Resolve the memory of the previous run against the current list of clients."""
        pending, self._pending = self._pending, {}
        if not pending:
            return
        for win, record in pending.items():
            if win not in current:
                continue  # the window closed while the daemon was not running
            if not self._adopt_placement(win, record):
                self.log(
                    _("the record of 0x{win:x} no longer fits - laying it out by the rule").format(
                        win=win
                    )
                )

    def _prune_state(self, current):
        """Forget the windows that are no longer in `_NET_CLIENT_LIST`."""
        dropped = False
        for win in list(self.placed):
            if win not in current:
                self.placed.pop(win, None)
                self.win_class.pop(win, None)
                dropped = True
        for win in list(self.detached):
            if win not in current:
                del self.detached[win]
                dropped = True
        if dropped:
            self._save_state()

    def _window_for_zone(self, zone, wa=None):
        """A window in zone `zone` on a visible workspace; with `wa` — exactly in this area.

        Windows of other workspaces are not candidates: reflow moves only the visible workspace.
        """
        for win, occupied in self.windows.items():
            if occupied != zone:
                continue
            if wa is not None and self.win_monitor.get(win) != wa:
                continue
            if not self._visible(self.win_workspace.get(win)):
                continue
            return win
        return None

    def _restore_holder(self, workspace, wa, zone):
        """Return the neighbour that expanded onto `zone` to its frame from the config.

        `zone` in work area `wa` is taken by a new window of workspace `workspace`,
        so the coverage is no longer needed. Coverage lives on the visible workspace
        (reflow goes there and only there), so a window of another workspace does not touch it.
        If the holder window is no longer in the tracker, the record is dropped without
        any moves. On a failed `apply` the record stays: we will try again on
        the next window.
        """
        if not self._visible(workspace):
            return  # a window of an invisible workspace: the visible workspace's coverage is not ours
        key = (self.workspace, wa, zone)
        holder = self.filled.get(key)
        if holder is None:
            return
        holder_zone = self.windows.get(holder)
        holder_workspace = self.win_workspace.get(holder)
        # The profile is that of the screen where the holder window stands (reflow lives on `wa`).
        grid = self._grid_for(holder_workspace, self._output_for(wa))
        if holder_zone is None or holder_zone not in grid.all_zone_names():
            # No holder, or its zone is no longer in the set — we drop the coverage.
            del self.filled[key]
            return
        # The return frame follows the tab count of the stack: a tab window returns to
        # the shared frame of the zone, not to its whole area.
        count = len(self._zone_stack(holder_zone, wa, holder_workspace))
        rect = self._stack_rect(holder_zone, wa, holder_workspace, count, grid=grid)
        if self._apply(holder, rect, holder_zone):
            del self.filled[key]
            self._save_state()
            self.log(
                _("returning zone '{zone}' to its original frame").format(
                    zone=holder_zone
                )
            )

    # -- tabs in a zone

    def _client_order(self, client_ids=None):
        """Client ids in `_NET_CLIENT_LIST` order — bottom to top.

        The list is read by the daemon (only it sees it); if unavailable — the tracker
        order is taken, so that one xprop failure does not tear stacks apart.
        `client_ids` is an already read list (`reconcile` reads the same one):
        then there is no extra request to X.
        """
        clients = []
        try:
            source = self.read_clients() if client_ids is None else client_ids
            clients = [int(win) for win in (source or [])]
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to read the client list: {exc}").format(exc=exc)
            )
        order = list(dict.fromkeys(clients))
        order.extend(win for win in self.windows if win not in order)
        return order

    def _tracked_order(self, client_ids=None):
        """Placed windows of the visible workspace in `_NET_CLIENT_LIST` order (bottom to top)."""
        return [
            win
            for win in self._client_order(client_ids)
            if win in self.windows and self._visible(self.win_workspace.get(win))
        ]

    def _frames(self, windows):
        """`{window: frame}` in the given order; an unreadable frame is `None`.

        A dict in Python keeps insertion order, so `zone_tabs.tabs_of_zone`
        sees the windows exactly in `_NET_CLIENT_LIST` order.
        """
        return {win: self._read_rect(win) for win in windows}

    def _zone_pixel_rect(self, zone, wa, workspace=None, grid=None):
        """The pixel frame of a zone on monitor `wa` (the grid comes from the screen or is given).

        Without an explicit grid the profile is resolved for **this** monitor: a screen
        may have its own profile, and foreign geometry does not fit here.
        """
        grid = grid or self._grid_for(workspace, self._output_for(wa))
        return tuple(int(round(v)) for v in grid.rect(zone, wa))

    def _tab_frame(self, zone_rect):
        """The frame of a tab window: the zone minus the strip (a narrow zone — the zone itself)."""
        return tuple(
            int(round(v)) for v in zone_tabs.tab_rect(zone_rect, self.tabs_height)
        )

    def _stack_rect(self, zone, wa, workspace=None, count=1, grid=None):
        """The frame of a zone window: the whole zone up to two tabs, beyond that the zone minus the strip.

        The strip takes height away from the windows only when there is more than one tab: a single
        window occupies the whole zone, as before tabs.
        """
        rect = self._zone_pixel_rect(zone, wa, workspace, grid)
        # The "one tab — the whole zone" rule comes from the core — the same as in
        # the tiler (`zone-tiler.in_zone_rect`), otherwise `apply` and `arrange`
        # would diverge on the very same number of windows.
        return tuple(
            int(round(v)) for v in zone_tabs.stack_rect(rect, count, self.tabs_height)
        )

    def _zone_stack(self, zone, wa, workspace=None, exclude=()):
        """The windows of the stack of zone `zone` — in `_NET_CLIENT_LIST` order (bottom to top).

        A stack is windows of one zone, one monitor and one workspace: without that a
        window of a neighbouring screen or of another workspace would get into it, and a switch
        would raise a foreign one. A "sticky"/unidentified window (`workspace is None`) does not
        clash with the zone workspace — it lives in the default set and is visible everywhere.
        `wa is None` — the monitor is not given (tests, a window without a work area).
        """
        skip = set(exclude)
        out = []
        for win in self._client_order():
            if self.windows.get(win) != zone or win in skip:
                continue
            if wa is not None and self.win_monitor.get(win) != wa:
                continue
            other = self.win_workspace.get(win)
            if workspace is not None and other is not None and other != workspace:
                continue
            out.append(win)
        return out

    def _align_stack(self, stack, rect):
        """Move the already standing tabs onto the shared frame: the strip appeared/disappeared.

        A moved window stops being a coverage holder: the coverage zone
        is no longer behind it, and the record is dropped right here (`_drop_coverage`).
        """
        for win in stack:
            if self._apply(win, rect, self.windows.get(win)):
                self._drop_coverage(win)
                self.log(
                    _(
                        "tabs: window 0x{win:x} took the shared frame of zone '{zone}'"
                    ).format(win=win, zone=self.windows.get(win))
                )

    def _settle_in_zone(self, win, zone, wa, workspace, wm_class="", grid=None):
        """Place a window as a tab in a zone and align the stack.

        The frame is computed from the tab count that will be there after the placement: the first
        window — the whole zone, the second — the shared tab frame, and right then the
        window already standing there is moved (the strip appeared, the window got lower by its height).
        A window that moved in from another zone leaves a single remaining
        tab behind — it gets the whole zone back (`_collapse_stack`).

        Returns the frame the window landed in, or `None` — the placement did not work
        (then the tracker is not touched: the zone is not considered taken).
        """
        previous = (
            self.windows.get(win),
            self.win_monitor.get(win),
            self.win_workspace.get(win),
        )
        stack = self._zone_stack(zone, wa, workspace, exclude=(win,))
        rect = self._stack_rect(zone, wa, workspace, len(stack) + 1, grid=grid)
        if not self._apply(win, rect, zone):
            return None
        # The window moved — the coverages it held are no longer its own.
        self._drop_coverage(win)
        self.windows[win] = zone
        self.win_monitor[win] = wa
        self.win_workspace[win] = workspace
        self.win_class[win] = wm_class
        self._align_stack(stack, rect)
        if previous[0] is not None and previous[0] != zone:
            # The window really changed zone (by an `apply`/`focus` command,
            # `arrange`) — that is a `window-moved`. Aligning tabs and
            # a reflow expansion do not change the zone, so they stay silent.
            self._emit(
                "window-moved",
                lambda: zone_ipc.event_window_moved(
                    win, zone, previous=previous[0], workspace=workspace, monitor=wa
                ),
            )
            self._collapse_stack(previous[0], previous[1], previous[2])
        self._save_state()
        self._restore_holder(workspace, wa, zone)
        return rect

    def _collapse_stack(self, zone, wa, workspace):
        """Give the whole zone back to the single remaining tab.

        The strip marks a stack: the next-to-last tab is gone — there is no strip, and
        the window occupies the whole zone again. `False` — the stack does not hold one tab (or
        moving is not allowed: an invisible workspace, no monitor).
        """
        if wa is None or not self._visible(workspace):
            return False
        remaining = self._zone_stack(zone, wa, workspace)
        if len(remaining) != 1:
            return False
        win = remaining[0]
        rect = self._zone_pixel_rect(zone, wa, workspace)
        if not self._apply(win, rect, zone):
            return False
        # The single tab moved onto the whole zone — it has stopped being a coverage
        # holder (`_drop_coverage`), otherwise `status.filled` lies.
        self._drop_coverage(win)
        self._save_state()
        self.log(
            _(
                "zone '{zone}' has a single tab left - window 0x{win:x} fills the zone"
            ).format(zone=zone, win=win)
        )
        return True

    def tabs_in_zone(self, zone, wa, workspace=None, frames=None, grid=None):
        """The tab windows of a zone by frames (bottom to top) — `zone_tabs.tabs_of_zone`.

        `frames` — `{window: frame}` in `_NET_CLIENT_LIST` order; by default
        the frames of the placed windows of the visible workspace are read. A tab is a window whose frame
        matched the shared one (the zone minus the strip, tolerance `zone_tabs.FRAME_TOLERANCE`):
        a single window stands on the whole-zone frame and is not counted as a stack.
        """
        if frames is None:
            frames = self._frames(self._tracked_order())
        tab_frame = self._stack_rect(zone, wa, workspace, 2, grid)
        return zone_tabs.tabs_of_zone(frames, tab_frame)

    def _window_title(self, win):
        """The window title for the strip; not read — an empty string."""
        try:
            return getattr(self.read_meta(win), "wm_title", "") or ""
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to read the title of 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return ""

    def _active_window(self):
        """The active window (`_NET_ACTIVE_WINDOW`); `None` — cannot be read."""
        try:
            active = self.active_window()
        except (Exception, SystemExit) as exc:
            self.log(_("failed to determine the active window: {exc}").format(exc=exc))
            return None
        return None if active is None else int(active)

    def tabs_picture(self, client_ids=None):
        """Snapshot records for the strip — only zones of the current workspace.

        On other workspaces no windows are visible, and the "a stack lives here" hint is not needed there.
        The active tab is the one holding the active window; an active window outside
        the stack — the top one (the strip has to highlight something anyway). The records
        are built by `zone_tabs.strip_record`: a single tab and a narrow zone give `None`
        (no strip), and such records do not make it into the snapshot.
        """
        workspace = self.workspace
        monitors = self._monitor_list()
        if not monitors:
            return []
        frames = self._frames(self._tracked_order(client_ids))
        active = self._active_window()
        records = []
        for wa in monitors:
            # Every screen has its own profile, hence its own set of zones.
            grid = self._grid_for(workspace, self._output_for(wa))
            for zone in sorted(grid.zones):
                zone_rect = self._zone_pixel_rect(zone, wa, workspace, grid)
                tabs = [
                    win
                    for win in self.tabs_in_zone(zone, wa, workspace, frames, grid)
                    if self.win_monitor.get(win) == wa
                ]
                if not tabs:
                    continue
                titles = [(win, self._window_title(win)) for win in tabs]
                shown = active if active in tabs else tabs[-1]
                record = zone_tabs.strip_record(
                    zone, zone_rect, titles, active=shown, height=self.tabs_height
                )
                if record is not None:
                    records.append(record)
        return records

    def _normalize_stacks(self, client_ids=None):
        """Bring stack frames in line with the tab count — over the zones of the visible workspace.

        The "one tab left — it gets the whole zone" rule lives in the core
        (`zone_tabs.stack_rect`), but it applies not only on placement and
        closing: a window leaves the stack by hand (`_refresh_detached`) or the second
        tab does not come back after a daemon restart (`_adopt_placement` and
        `_in_zone_frame`, which counts the tab frame as "the window is in the zone"). Then
        the remaining window stands on the tab frame — a strip height lower, with an empty
        strip above. One pass over the zones instead of a scatter of
        `_collapse_stack` calls gives such windows their frame by the tab count.

        Only windows standing on the tab frame with a single
        tab in the zone are moved: a window on its own zone and an expanded reflow holder (its
        frame is a union of zones) are not touched. `True` — something was moved.
        """
        if self.workspace is None:
            return False
        stacks = {}
        for win in self._client_order(client_ids):
            zone = self.windows.get(win)
            wa = self.win_monitor.get(win)
            if zone is None or wa is None:
                continue
            if not self._visible(self.win_workspace.get(win)):
                continue
            # The zone is looked up in the profile of the screen where the window stands.
            grid = self._grid_for(self.workspace, self._output_for(wa))
            if zone not in grid.all_zone_names():
                continue
            stacks.setdefault((zone, wa), []).append(win)
        moved = False
        for (zone, wa), stack in stacks.items():
            if len(stack) != 1 or wa is None:
                continue
            win = stack[0]
            grid = self._grid_for(self.workspace, self._output_for(wa))
            zone_rect = self._zone_pixel_rect(zone, wa, self.workspace, grid)
            tab_frame = self._tab_frame(zone_rect)
            if tab_frame == zone_rect:
                continue  # a narrow zone: no strip, the tab frame and the zone frame matched
            # We compare with the placement memory, not with an X read: before this pass
            # `_refresh_detached` has already compared the fact with the memory, and since the window is in
            # the tracker — it is where the daemon put it (or it cannot be read).
            actual = self.placed.get(win)
            if actual is None:
                actual = self._read_rect(win)
            if actual is None or not frame_matches(actual, tab_frame):
                continue  # the window is not on the tab frame — not our business
            if not self._apply(win, zone_rect, zone):
                continue
            self._drop_coverage(win)
            moved = True
            self.log(
                _(
                    "zone '{zone}' has a single tab left - window 0x{win:x} fills the zone"
                ).format(zone=zone, win=win)
            )
        if moved:
            self._save_state()
        return moved

    def sync_strips(self, client_ids=None):
        """Hand the strip a full tab snapshot; a strip failure does not bring the daemon down.

        The snapshot is one `zone_tabs.strips_payload` line per change
        (not a delta): the strip does not need to remember the past. Identical snapshots are not
        written twice — events are many, and the picture changes less often. Without a strip
        (`strip is None`) the method costs nothing and reads neither X nor the config.
        """
        if self.strip is None:
            return False
        try:
            records = self.tabs_picture(client_ids)
        except (Exception, SystemExit) as exc:
            self.log(_("failed to build the tab snapshot: {exc}").format(exc=exc))
            return False
        payload = zone_tabs.strips_payload(records)
        if payload == self._strips_last:
            return False
        self._strips_last = payload
        if not records and not self.strip.running():
            return False  # there is nothing to draw and nobody to draw for
        try:
            accepted = self.strip.feed(payload)
        except (Exception, SystemExit) as exc:
            self.log(_("tab bar: {exc}").format(exc=exc))
            accepted = False
        if not accepted:
            # The strip did not accept the snapshot (died/did not start): next time
            # we will hand the same snapshot again — to a new process already.
            self._strips_last = None
            self.log(_("the tab bar did not accept the snapshot"))
        return accepted

    def _raise_window(self, win):
        """Show a window: `_NET_ACTIVE_WINDOW` through the daemon's shared connection."""
        try:
            self.deiconify(win, x11=self.x11)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to raise window 0x{win:x}: {exc}").format(win=win, exc=exc)
            )
            return False
        self.log(_("window 0x{win:x} raised").format(win=win))
        return True

    # -- IPC: socket command handlers

    def ipc_subscribe(self, request=None):
        """An event subscription: after it the connection is read-only.

        The groups are parsed by the pure `zone_ipc.parse_subscription`: without the `events`
        field we subscribe to everything (`window` + `focus`), an empty list is a refusal (otherwise
        a typo would silently give a stream the client did not expect). Returned is
        `zone_ipc.Subscription`: the transport keeps the connection open by it
        and puts it into `self.subscribers`, and the daemon only writes there. Unsubscribing is
        closing the connection.
        """
        groups = zone_ipc.parse_subscription(request or {})
        self.log(_("event subscription: {groups}").format(groups=", ".join(groups)))
        return zone_ipc.Subscription(groups)

    def ipc_arrange(self, request):
        """Lay the windows out into the zones the client named (the `arrange` command).

        Which windows go where is decided by the tiler (`zone-tiler.py arrange` — the `--by`
        choice and the `undo` history live there too), and the daemon **applies them**: otherwise its
        tracker would drift from the layout (closing a window would launch reflow to
        the wrong place), and the subscribers would not see `window-moved`. The geometry is computed by
        `_settle_in_zone` — exactly as on a window appearing by a rule, so
        tabs and the strip are counted against the same tracker.

        Request: `{"cmd": "arrange", "workspace": N, "placements": [{"window":
        "0x…", "zone": …, "monitor": [x, y, w, h]}, …]}` (`monitor` and
        `workspace` are optional — they come from the window and the current one). Answer:
        `{"arranged": [hex…], "skipped": [{"window": …, "error": …}]}`.
        """
        placements = request.get("placements")
        if not isinstance(placements, list) or not placements:
            raise ValueError(
                _("arrange: a non-empty 'placements' list is required")
            )
        workspace = request.get("workspace")
        if not isinstance(workspace, int):
            workspace = self.workspace
        arranged = []
        skipped = []
        for item in placements:
            window, error = self._arrange_one(item, workspace)
            if error is not None:
                skipped.append(
                    {
                        "window": hex(window) if window is not None else None,
                        "error": error,
                    }
                )
            else:
                arranged.append(hex(window))
        if arranged:
            self.sync_strips()
            self._save_state()
        return {"arranged": arranged, "skipped": skipped}

    def _arrange_one(self, item, workspace):
        """Place one window from `arrange`: `(window, None)` or `(window, refusal)`.

        A refusal of one window does not cancel the others: at the tiler windows could
        vanish between reading the list and the command. The second element is the refusal text
        (or `None` if the window landed in the zone).
        """
        if not isinstance(item, dict):
            return None, _("a placement entry must be an object")
        raw = item.get("window")
        try:
            window = int(raw, 0) if isinstance(raw, str) else int(raw)
        except (TypeError, ValueError):
            return None, _("cannot read window id '{raw}'").format(raw=raw)
        zone = item.get("zone")
        wa = item.get("monitor")
        try:
            wa = tuple(int(v) for v in wa) if wa else self._monitor_of(window)
        except (TypeError, ValueError):
            wa = None
        if wa is None:
            return window, _(
                "failed to determine the monitor of window 0x{window:x}"
            ).format(window=window)
        grid = self._grid_for(workspace, self._output_for(wa))
        if not isinstance(zone, str) or zone not in grid.all_zone_names():
            return window, _("no such zone '{zone}'").format(zone=zone)
        try:
            meta = self.read_meta(window)
        except (Exception, SystemExit) as exc:
            return window, _("failed to read window 0x{window:x}: {exc}").format(
                window=window, exc=exc
            )
        wm_class = getattr(meta, "wm_class", "") or ""
        if window_excluded(
            wm_class,
            getattr(meta, "wm_title", "") or "",
            getattr(meta, "window_type", ()),
            grid.ignore or self.grid.ignore,
        ):
            return window, _(
                "window 0x{window:x} is excluded ([ignore]/utility window type) - not moving it"
            ).format(window=window)
        if self._settle_in_zone(
            window, zone, wa, workspace, wm_class, grid=grid
        ) is None:
            return window, _(
                "failed to place window 0x{window:x} into zone '{zone}'"
            ).format(window=window, zone=zone)
        return window, None

    def build_dispatcher(self):
        """The socket command dispatcher with the daemon handlers injected.

        The specific handlers can be substituted in tests; the dispatcher itself
        (`zone_ipc.Dispatcher`) knows neither about X nor about the config.
        """
        return zone_ipc.Dispatcher(
            {
                "status": self.ipc_status,
                "apply": self.ipc_apply,
                "focus": self.ipc_focus,
                "reload": self.ipc_reload,
                "quit": self.ipc_quit,
                "subscribe": self.ipc_subscribe,
                "arrange": self.ipc_arrange,
                zone_tabs.TABS_NEXT: self.ipc_tabs_next,
                zone_tabs.TABS_SWITCH: self.ipc_tabs_switch,
            }
        )

    def _monitor_list(self):
        """Work areas for IPC commands: the forced one or all monitors."""
        if self.workarea is not None:
            wa = self._forced_workarea()
            return [wa] if wa else []
        return self._monitors() or []

    def ipc_status(self, request=None):
        """The daemon state: zones, window→zone, workspace, monitor, profile, reflow.

        Pixel zone frames are given per monitor (`zones_px`), because
        a zone is a share of the work area, and a monitor has its own area.
        **Fill is computed over the resolved profiles**, not over the active
        `zones.conf`: every screen has its own profile (see `_grid_for`), so
        `zones` is the union of the sets of the screens (in their order), and
        `zones_px[name][i]` is the zone frame in the profile of monitor `i` (or `None`
        if that profile has no such zone: there is nothing to draw there). The shape
        of the answer stays the same.

        The `windows[].zone/window/monitor` fields are unchanged: the applet and the tests read
        them as before; `workspace` (the active workspace number and the workspace of every window and
        coverage) is added next to them, not instead. `focus` is the "this
        zone only" mode: `None` or `{zone, window, minimized (count)}`.
        """
        monitors = self._monitor_list()
        # The grid per monitor — exactly the one the daemon places windows by.
        grids = [
            self._grid_for(self.workspace, self._output_for(wa)) for wa in monitors
        ]
        zones = {}
        for grid in grids or [self.grid]:
            for name in sorted(grid.zones):
                zones.setdefault(name, list(grid.fractions(name)))
        zones_px = {}
        for name in zones:
            zones_px[name] = [
                list(grid.rect(name, wa)) if name in grid.zones else None
                for grid, wa in zip(grids, monitors)
            ]
        windows = []
        for win, zone in sorted(self.windows.items()):
            wa = self.win_monitor.get(win)
            windows.append(
                {
                    "window": hex(win),
                    "zone": zone,
                    "monitor": list(wa) if wa else None,
                    "workspace": self.win_workspace.get(win),
                }
            )
        filled = [
            {
                "workspace": workspace,
                "monitor": list(wa),
                "zone": zone,
                "holder": hex(holder),
            }
            for (workspace, wa, zone), holder in sorted(
                self.filled.items(),
                key=lambda item: (
                    item[0][2],
                    -1 if item[0][0] is None else item[0][0],
                    item[1],
                ),
            )
        ]
        return {
            "version": VERSION,
            "conf": self.conf_path,
            "reflow": self.reflow,
            # The active workspace (EWMH numbering from zero); None — unknown.
            "workspace": self.workspace,
            # Layout profiles — wave 3; the field is there in advance for compatibility.
            "profile": None,
            "monitors": [list(wa) for wa in monitors],
            "zones": zones,
            "zones_px": zones_px,
            "windows": windows,
            "filled": filled,
            # The "this zone only" mode: None or the zone, the target and the iconified count.
            "focus": None
            if self.focus is None
            else {
                "zone": self.focus["zone"],
                "window": hex(self.focus["target"]),
                "minimized": len(self.focus["minimized"]),
            },
        }

    def _monitor_for_command(self, request, window):
        """The work area for `apply`: an explicit monitor number or the monitor of the window.

        Without an explicit `monitor` (and without a readable window position) the first
        monitor is taken — the user gave the command, and a refusal is worse than an approximate
        choice.
        """
        monitors = self._monitor_list()
        index = request.get("monitor")
        if index is not None:
            index = int(index)
            if not 0 <= index < len(monitors):
                raise ValueError(
                    _("no monitor {index} ({total} in total)").format(
                        index=index, total=len(monitors)
                    )
                )
            return monitors[index]
        wa = self._monitor_of(window)
        if wa is not None:
            return wa
        return monitors[0] if monitors else None

    def _known_zone_names(self):
        """Zone names available to explicit commands: the active config and the screen profiles.

        A command arrives with a zone name from the applet or the tiler, and they
        show zones of the **resolved** profile — a screen may have its own.
        So a name cannot be checked against the active `zones.conf` alone: a zone
        of the second screen's profile would be rejected as nonexistent.
        """
        names = set(self.grid.all_zone_names())
        for wa in self._monitor_list():
            grid = self._grid_for(self.workspace, self._output_for(wa))
            names |= set(grid.all_zone_names())
        return names

    def ipc_apply(self, request):
        """Place a window in a zone by a socket command (by default — the active one).

        A successful placement is remembered in the daemon tracker: closing the window then
        triggers reflow as usual instead of leaving a ghost. The zone grid comes from the profile
        of the screen where the window stands (see `_grid_for`).
        """
        zone = request.get("zone")
        known = self._known_zone_names()
        if not isinstance(zone, str) or zone not in known:
            raise ValueError(
                _("no zone '{zone}'. Known: {known}").format(
                    zone=zone, known=", ".join(sorted(known))
                )
            )
        window = request.get("window")
        if window is None:
            window = tiler.active_window()  # no active window -> SystemExit
        elif isinstance(window, str):
            window = int(window, 0)
        else:
            window = int(window)
        wa = self._monitor_for_command(request, window)
        if wa is None:
            raise ValueError(
                _("failed to determine the monitor of window 0x{window:x}").format(
                    window=window
                )
            )
        grid = self._grid_for(self.workspace, self._output_for(wa))
        meta = self.read_meta(window)
        wm_class = getattr(meta, "wm_class", "") or ""
        if window_excluded(
            wm_class,
            getattr(meta, "wm_title", "") or "",
            getattr(meta, "window_type", ()),
            grid.ignore or self.grid.ignore,
        ):
            raise ValueError(
                _(
                    "window 0x{window:x} is excluded ([ignore]/utility window type) - not moving it"
                ).format(window=window)
            )
        # A window is placed where the user is now: occupancy is counted over the
        # active workspace, and the zone geometry comes from the profile of the window's screen.
        rect = self._settle_in_zone(
            window, zone, wa, self.workspace, wm_class, grid=grid
        )
        if rect is None:
            raise ValueError(
                _("failed to place window 0x{window:x} into zone '{zone}'").format(
                    window=window, zone=zone
                )
            )
        self.sync_strips()
        return {
            "window": hex(window),
            "zone": zone,
            "monitor": list(wa),
            "rect": list(rect),
        }

    def ipc_tabs_next(self, request=None):
        """Raise the next tab of the stack of the active window's zone (`tabs-next`).

        The daemon chooses the zone itself — by the active window. A window outside the zones, a stack of
        one tab and an unreadable active window are not an error but an honest answer
        `{"switched": false, ...}`: a key must not complain that there is
        nothing to switch.

        Return: `{"switched": true, "zone", "window" (hex), "previous" (hex)}`
        or `{"switched": false, "zone", "window": None, "reason"}`.
        """
        active = self._active_window()
        if active is None:
            return {
                "switched": False,
                "zone": None,
                "window": None,
                "reason": _("the active window is not read - nothing to switch"),
            }
        zone = self.windows.get(active)
        if zone is None:
            return {
                "switched": False,
                "zone": None,
                "window": None,
                "reason": _("window 0x{active:x} is outside zones - nothing to switch").format(
                    active=active
                ),
            }
        stack = self._zone_stack(
            zone,
            self.win_monitor.get(active),
            self.win_workspace.get(active),
        )
        target = zone_tabs.next_tab(stack, active)
        if target is None:
            return {
                "switched": False,
                "zone": zone,
                "window": None,
                "reason": _("zone '{zone}' has a single tab - nothing to switch").format(
                    zone=zone
                ),
            }
        if not self._raise_window(target):
            raise ValueError(
                _("failed to raise window 0x{target:x} of zone '{zone}'").format(
                    target=target, zone=zone
                )
            )
        self.sync_strips()
        return {
            "switched": True,
            "zone": zone,
            "window": hex(target),
            "previous": hex(active),
        }

    def ipc_tabs_switch(self, request):
        """Show a zone window on a click in the strip (`tabs-switch`).

        The click comes from the strip to the same socket as the `gridsnap-ctl` commands.
        A window that is not among the tabs of this zone (already closed, moved to another
        zone or moved out) is not raised: a `ValueError` instead of a foreign window
        on top of the zone.
        """
        request = request or {}
        zone = request.get("zone")
        if not isinstance(zone, str) or not zone:
            raise ValueError(_("no zone in the tabs-switch command"))
        window = request.get("window")
        if window is None:
            raise ValueError(_("no window in the tabs-switch command"))
        window = int(window, 0) if isinstance(window, str) else int(window)
        stack = self._zone_stack(
            zone, self.win_monitor.get(window), self.win_workspace.get(window)
        )
        if window not in stack:
            raise ValueError(
                _("window 0x{window:x} is not a tab of zone '{zone}' - not raising it").format(
                    window=window, zone=zone
                )
            )
        if not self._raise_window(window):
            raise ValueError(
                _("failed to raise window 0x{window:x} of zone '{zone}'").format(
                    window=window, zone=zone
                )
            )
        self.sync_strips()
        return {"zone": zone, "window": hex(window)}

    def _focus_covers(self, win, workspace, wa=None):
        """Whether the active mode iconifies this window: the workspace and monitor of the mode.

        The selection rule is the same as in `tiler.focus_candidates` (a pure function —
        one source of truth), a single record: the window itself, its workspace and its work
        area. `wa` is passed when the monitor has already been read. If the window's monitor cannot
        be read — we do not iconify: guessing and hiding a foreign window is worse.

        The tabs of the target zone are not iconified — neither those that were in the stack on entering
        the mode (`_focus_candidates`), nor those the rule put there already during
        the mode: the mode is called "this zone only", and hiding a window of that very
        zone would contradict its meaning (besides, the strip would show
        an iconified tab, while `ipc_apply` does not iconify a window into the same zone).
        """
        state = self.focus
        if state is None:
            return False
        if wa is None:
            wa = self._monitor_of(win)
        if wa is None:
            return False
        entries = [(win, workspace, wa)]
        if not tiler.focus_candidates(
            entries, state["target"], state["workspace"], state["monitor"]
        ):
            return False
        if win in self._focus_zone_tabs(state["target"]):
            self.log(
                _("window 0x{win:x} is a tab of the mode's zone - not minimizing it").format(
                    win=win
                )
            )
            return False
        return True

    def _focus_zone_tabs(self, target):
        """The tabs of the target zone — the mode does not iconify them: they are part of the zone.

        The stack is looked up in the tracker (the target is already placed in the zone before the selection, or was
        placed earlier). The target is always in the set: it has nothing to iconify
        for itself.
        """
        keep = {target}
        zone = self.windows.get(target)
        if zone is None:
            return keep
        keep.update(
            self._zone_stack(
                zone, self.win_monitor.get(target), self.win_workspace.get(target)
            )
        )
        return keep

    def _focus_candidates(self, target, wa):
        """The windows the mode iconifies right now, in `_NET_CLIENT_LIST` order.

        The client list is read by the daemon (`read_clients`): only it sees windows
        that appeared after entering the mode. The selection of windows of the same workspace and monitor is
        `tiler.focus_candidates`; exceptions are filtered out here as well
        (`window_excluded`: dialogs, `[ignore]`, utility types stay
        visible — that is a product requirement) and windows iconified by hand: such a window
        was not iconified by the mode and will not be restored on exit. The tabs of the target zone are
        excluded from the selection (`_focus_zone_tabs`): they are part of that very zone
        the owner asked to keep.
        """
        try:
            client_ids = self.read_clients() or []
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to read the client list: {exc}").format(exc=exc)
            )
            return []
        entries = [
            (win, self._window_workspace(win), self._monitor_of(win))
            for win in client_ids
        ]
        keep = self._focus_zone_tabs(target)
        out = []
        for win in tiler.focus_candidates(entries, target, self.workspace, wa):
            if win in out:
                continue  # a duplicate in the client list
            if win in keep:
                self.log(
                    _("window 0x{win:x} is a tab of the mode's zone - stays visible").format(
                        win=win
                    )
                )
                continue
            try:
                meta = self.read_meta(win)
            except (Exception, SystemExit) as exc:
                self.log(
                    _("failed to read metadata of 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                continue
            if window_excluded(
                getattr(meta, "wm_class", "") or "",
                getattr(meta, "wm_title", "") or "",
                getattr(meta, "window_type", ()),
                self.grid.ignore,
            ):
                self.log(
                    _(
                        "window 0x{win:x} is excluded - stays visible in the mode"
                    ).format(win=win)
                )
                continue
            try:
                iconic = tiler.window_iconic(win)
            except (Exception, SystemExit) as exc:
                self.log(
                    _("failed to learn the state of window 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                iconic = False
            if iconic:
                self.log(
                    _("window 0x{win:x} was minimized not by the mode - leaving it alone").format(
                        win=win
                    )
                )
                continue
            out.append(win)
        return out

    def _focus_minimize(self, win):
        """Iconify a window and remember that the mode did it; `False` — it did not work."""
        try:
            self.iconify(win, x11=self.x11, quiet=True)
        except (Exception, SystemExit) as exc:
            self.log(
                _("failed to minimize window 0x{win:x}: {exc}").format(
                    win=win, exc=exc
                )
            )
            return False
        if self.focus is not None:
            self.focus["minimized"].add(win)
        self.log(
            _("window 0x{win:x} minimized by focus mode").format(win=win)
        )
        return True

    def leave_focus(self, switch=False):
        """Leave the mode: restore what it iconified itself.

        Returns `(restored, remainder, mode state)`: the state is already
        taken from `self.focus`, the caller needs it to hand the remainder to
        `focus.json` (`_focus_handoff`).

        `tiler.deiconify_all` re-reads the state, so the remainder holds the windows
        that stayed iconified. Most often these are windows not on the active
        workspace: muffin ignores activation of such windows (verified by smoke). What
        to do with the remainder is decided by the caller — `_focus_handoff` hands it to
        `focus.json` rather than losing it.

        `switch=True` — switch to the mode's workspace first: that is what an **explicit** exit does
        (toggle/`off`), since the user asked to bring the windows back. Implicit ones (the target
        vanished, the daemon is terminating) do not touch the workspace: pulling the user
        around because of a foreign window is worse than leaving a CLI list.
        """
        state, self.focus = self.focus, None
        if state is None:
            return [], [], None
        if switch and self.x11 is not None and isinstance(state.get("workspace"), int):
            try:
                tiler.switch_workspace(state["workspace"], x11=self.x11)
            except (Exception, SystemExit) as exc:  # the workspace is wanted but not required
                self.log(
                    _("failed to switch the workspace: {exc}").format(exc=exc)
                )
        order = [win for win in sorted(state["minimized"]) if win != state["target"]]
        try:
            remaining = tiler.deiconify_all(
                order + [state["target"]], x11=self.x11, deiconify_fn=self.deiconify
            )
        except (Exception, SystemExit) as exc:
            self.log(_("failed to unminimize the mode's windows: {exc}").format(exc=exc))
            remaining = list(order) + [state["target"]]
        restored = [
            win
            for win in order
            if win in state["minimized"] and win not in remaining
        ]
        self.log(
            _(
                "focus mode off (zone '{zone}'): restored {restored} of {total}"
            ).format(
                zone=state["zone"] or _("without a zone"),
                restored=len(restored),
                total=len(state["minimized"]),
            )
        )
        # `focus-off` is sent from the single exit funnel: toggle/`off`,
        # the loss of the target and the daemon shutdown all arrive here.
        self._emit(
            "focus-off",
            lambda: zone_ipc.event_focus_off(
                state.get("zone"),
                restored=len(restored),
                total=len(state["minimized"]),
            ),
        )
        return restored, remaining, state

    def _focus_records(self, windows):
        """Window records for `focus.json` — through the daemon's injections, not through X directly.

        The workspace and `WM_CLASS` are read **now**: every window has its own workspace (in the file
        windows of different workspaces could meet), and without the class a record would dodge
        the protection against a reused XID.
        """
        records = []
        for win in dict.fromkeys(windows):
            try:
                wm_class = getattr(self.read_meta(win), "wm_class", "") or ""
            except (Exception, SystemExit) as exc:
                self.log(
                    _("failed to read metadata of 0x{win:x}: {exc}").format(
                        win=win, exc=exc
                    )
                )
                wm_class = ""
            records.append(
                {
                    "window": hex(win),
                    "wm_class": wm_class,
                    "workspace": self._window_workspace(win),
                }
            )
        return records

    def _focus_handoff(self, remaining, state):
        """Hand the unrestored remainder to `focus.json` — `focus --off` will finish it.

        A window on an inactive workspace cannot be restored (`_NET_ACTIVE_WINDOW` muffin
        ignores it for such windows), and losing it is even worse: the daemon no longer
        holds the mode, and without the record it would stay iconified forever. The file
        is read by the CLI, and `focus --off` switches to the recorded workspace itself.

        The records are built by `tiler.focus_records`: it reads `WM_CLASS` and the **workspace
        of every window** right now. Otherwise a window from the remainder would inherit a foreign
        `workspace` from the old record (windows of another workspace could have stayed in it) — and
        the restore would switch to the wrong workspace, and the window would stay iconified.
        Only windows that are still iconified are kept from the old record: already
        restored ones no longer need restoring.
        """
        if not remaining:
            return
        path = getattr(self, "focus_path", None)
        if not path:
            self.log(
                _(
                    "{count} mode windows could not be unminimized, but there is no state file - unminimize them manually"
                ).format(count=len(remaining))
            )
            return
        old = tiler.load_focus_state(path)
        still_iconic = []
        for record in old.get("windows") or []:
            if not isinstance(record, dict):
                continue
            try:
                win = int(record.get("window"), 0)
            except (TypeError, ValueError):
                continue
            if tiler.window_iconic(win):
                still_iconic.append(win)
        windows = still_iconic + [
            win for win in remaining if win not in still_iconic
        ]
        records = self._focus_records(windows)
        target = None
        if state["target"] in windows:
            target = self._focus_records([state["target"]])[0]
        try:
            tiler.save_focus_state(
                {
                    "zone": state.get("zone"),
                    "workspace": state.get("workspace"),
                    "monitor": list(state.get("monitor") or []),
                    "target": target,
                    "windows": records,
                },
                path,
            )
        except OSError as exc:
            self.log(_("failed to save the rest of the mode: {exc}").format(exc=exc))
            return
        self.log(
            _(
                "{count} mode windows stayed minimized - record in {path}: unminimize with focus --off"
            ).format(count=len(remaining), path=path)
        )

    def ipc_focus(self, request):
        """The "this zone only" mode: enter/toggle/exit.

        `focus <zone>` places the active window (or the `window` from the request) in the zone and
        iconifies the other windows **of the same workspace and the same monitor**; the command
        with the mode already active is a **toggle**: it turns the mode off and restores
        the windows it iconified itself. `{"off": true}` — an explicit exit without entering.

        Only the windows from `self.focus["minimized"]` are restored (a window iconified
        by hand stays iconified), the target last: `deiconify` raises and
        focuses the window. Exceptions (`window_excluded`: `[ignore]`, utility
        types, dialogs) and already iconified windows are not touched and do not
        get into `minimized`.

        The active workspace is not read — a refusal: iconifying windows of foreign workspaces
        blindly is worse than not enabling the mode.

        Return:

        - enter — `{"zone", "window" (hex), "monitor" ([x,y,w,h]), "minimized"
          ([hex] — what was iconified)}`;
        - exit — `{"off": True, "restored": [hex]}`.

        The mode appears in `status` as the `focus` field: `null` or
        `{"zone", "window", "minimized" (count)}`.

        `zone` is optional: without it (a `[focus]` section in `zones.conf` with no zone)
        the target stays where it stands, and only the surroundings are iconified — there is no
        placement and no tracker record then, and `self.focus["zone"]` holds `None`.
        The monitor is needed in this case too: it defines the iconification area.
        """
        request = request or {}
        if request.get("off") or self.focus is not None:
            # An explicit exit and a repeated command (toggle) are the same: drop the mode
            # and restore only what it iconified itself. We switch the workspace (if
            # the mode is not on the active one): that way windows on another workspace come back too.
            restored, remaining, state = self.leave_focus(switch=True)
            self._focus_handoff(remaining, state)
            # `leave_focus` restores the windows and activates the target last:
            # the active window (and the highlighted tab with it) could have changed,
            # so the strip snapshot is rebuilt here too, not only on entry.
            self.sync_strips()
            return {"off": True, "restored": [hex(w) for w in restored]}
        zone = request.get("zone")
        known = self._known_zone_names()
        if zone is not None and (not isinstance(zone, str) or zone not in known):
            raise ValueError(
                _("no zone '{zone}'. Known: {known}").format(
                    zone=zone, known=", ".join(sorted(known))
                )
            )
        window = request.get("window")
        if window is None:
            window = tiler.active_window()  # no active window -> SystemExit
        elif isinstance(window, str):
            window = int(window, 0)
        else:
            window = int(window)
        meta = self.read_meta(window)
        wm_class = getattr(meta, "wm_class", "") or ""
        if window_excluded(
            wm_class,
            getattr(meta, "wm_title", "") or "",
            getattr(meta, "window_type", ()),
            self.grid.ignore,
        ):
            raise ValueError(
                _(
                    "window 0x{window:x} is excluded ([ignore]/utility window type) - not enabling focus mode"
                ).format(window=window)
            )
        if self.workspace is None:
            raise ValueError(
                _(
                    "the active workspace is not read - not enabling focus mode: minimizing windows of other workspaces blindly is worse than not enabling it"
                )
            )
        # The monitor is taken from the target only and is not guessed: the mode iconifies windows
        # of the same work area, and `_monitor_for_command` with an unreadable
        # position falls back to the first monitor — and the mode would hide windows of the neighbouring
        # screen. The CLI refuses in the same case.
        wa = self._monitor_of(window)
        if wa is None:
            raise ValueError(
                _(
                    "failed to determine the monitor of window 0x{window:x} - not enabling focus mode: its area depends on the target's monitor"
                ).format(window=window)
            )
        # The zone geometry comes from the profile of the screen where the target stands.
        grid = self._grid_for(self.workspace, self._output_for(wa))
        if zone is not None:
            # The target becomes a tab of the zone itself: if a window was already there, both
            # stand on the shared frame, and the strip gets a new snapshot.
            rect = self._settle_in_zone(
                window, zone, wa, self.workspace, wm_class, grid=grid
            )
            if rect is None:
                raise ValueError(
                    _("failed to place window 0x{window:x} into zone '{zone}'").format(
                        window=window, zone=zone
                    )
                )
        self.focus = {
            "zone": zone,
            "target": window,
            "workspace": self.workspace,
            "monitor": wa,
            "minimized": set(),
        }
        for win in self._focus_candidates(window, wa):
            self._focus_minimize(win)
        self.sync_strips()
        minimized = len(self.focus["minimized"])
        # `focus-on` is sent after the iconification: the counter is already final.
        self._emit(
            "focus-on",
            lambda: zone_ipc.event_focus_on(zone, window, minimized=minimized),
        )
        return {
            "zone": zone,
            "window": hex(window),
            "monitor": list(wa),
            "minimized": [hex(w) for w in sorted(self.focus["minimized"])],
        }

    def ipc_reload(self, request=None):
        """Re-read zones.conf: the grid, the rules, the screen/workspace bindings and reflow.

        All caches are reset, including the (workspace, output) pairs and the parsed
        `[monitors]`/`[workspaces]`: a new screen profile is picked up without
        a daemon restart.
        """
        self.grid = tiler.Grid(self.conf_path)
        self.reflow = read_reflow(self.grid.conf)
        self.tabs_height = read_tabs_height(self.grid.conf)
        self._grids = {}
        self._wa_cache = None
        self._mons_cache = None
        self._mons_scale = None
        self._outputs_cache = None
        self._monitor_map_cache = None
        self._workspace_map_cache = None
        self.log(_("config re-read: {path}").format(path=self.conf_path))
        self.sync_strips()
        return {
            "conf": self.conf_path,
            "zones": sorted(self.grid.zones),
            "reflow": self.reflow,
        }

    def ipc_quit(self, request=None):
        """Ask the daemon loop to finish after the current event."""
        self.stop_requested = True
        self.log(_("got the quit command - exiting"))
        return {"bye": True}

    # -- events

    def reconcile(self, client_ids):
        """Reconcile the state with the client list `_NET_CLIENT_LIST`.

        New windows are placed by the rules (`on_map`), vanished ones are dropped
        from the tracker (`on_destroy`, with reflow). New ones are handled before
        vanished ones: if a new window took the freed zone, reflow already
        sees it as taken and will not move the neighbours.
        """
        current = set(client_ids)
        # Zones at the start of the pass: `on_destroy` and `_detach` clean the tracker, but
        # `window-closed` needs the last known zone of the window (for a window moved out
        # by hand it is already different in the tracker — hence the snapshot before everything).
        zones_before = dict(self.windows)
        # The previous run's memory and the user's moves — before the placement: a window
        # that was moved out of its zone must not be placed again.
        self._apply_pending(current)
        self._refresh_detached()
        previous_seen = set(self.seen)
        new = [win for win in dict.fromkeys(client_ids) if win not in self.seen]
        for win in new:
            self.log(_("new window 0x{win:x} in _NET_CLIENT_LIST").format(win=win))
            self.on_map(win)
            # The event — after the placement: it already carries the zone, workspace and monitor of the window.
            # It is sent for a window without a zone too (under `[ignore]`, with no rule).
            self._emit_appeared(win)
        for win in list(self.windows):
            if win not in current:
                self.on_destroy(win)
        # Windows iconified by the mode may have no zone (there was no rule) — they are not in
        # `windows`, but they must be forgotten: there is nothing to restore a vanished one with.
        for win in list(self.focus["minimized"]) if self.focus is not None else []:
            if win not in current and win not in self.windows:
                self.on_destroy(win)
        # The target may not have been placed in a zone either (a `focus` section with no zone): then
        # the first pass will not see it, and there is no point in holding a mode without a target.
        if self.focus is not None and self.focus["target"] not in current:
            self.on_destroy(self.focus["target"])
        # `window-closed` — for every window that vanished from the list, including those that were not
        # in the tracker (no rule): what matters to a subscriber is that the window is gone.
        for win in sorted(previous_seen):
            if win not in current:
                self._emit_closed(win, zones_before.get(win))
        self._prune_state(current)
        self.seen = current
        # The composition of stacks could change without a placement (a window was moved out by hand, the second
        # tab did not come back after a restart): we bring the frames in line in one pass.
        self._normalize_stacks(client_ids)
        # The tab picture changed (windows came/went): the strip gets a new snapshot.
        # The client list has already been read — we pass it so as not to ask X twice.
        self.sync_strips(client_ids)

    def on_workspace_change(self):
        """The active workspace changed (`_NET_CURRENT_DESKTOP`): remember the new one.

        We do not move windows: placement and reflow run only on window events, and
        invisible workspaces stay as they are. Returns the new workspace (the previous
        one if it could not be read).
        """
        previous = self.workspace
        workspace = self._read_current_workspace()
        if workspace is None:
            self.log(
                _("the active workspace could not be read - keeping {desk}").format(
                    desk=workspace_label(previous)
                )
            )
            return previous
        self.workspace = workspace
        if workspace == previous:
            self.log(
                _("the active workspace did not change: {desk}").format(
                    desk=workspace_label(workspace)
                )
            )
        else:
            self.log(
                _(
                    "active workspace: {desk} (was {previous}) - not moving windows"
                ).format(
                    desk=workspace_label(workspace),
                    previous=workspace_label(previous),
                )
            )
        if self.focus is not None and workspace != self.focus["workspace"]:
            # The mode lives on its workspace and leaving does NOT drop it: the windows of that workspace
            # are invisible anyway, and on return you will see the mode as it was. It cannot be dropped on
            # leaving: muffin ignores `_NET_ACTIVE_WINDOW` for a window that is not on the
            # active workspace (verified by smoke), and the windows would stay iconified without
            # an owner. The exit works on its own workspace or switches to it itself.
            self.log(
                _(
                    "focus mode lives on workspace {desk} - leaving it in place"
                ).format(desk=workspace_label(self.focus["workspace"]))
            )
        # Strips — only for the zones of the current workspace: the snapshot is rebuilt on a change.
        self.sync_strips()
        return workspace

    def on_map(self, win, override_redirect=False):
        """A new client window: place it in a zone by the rule if it fits.

        The grid comes from the window's screen: `[monitors]` beats `[workspaces]`,
        `[workspaces]` beats the active `zones.conf` (see `_grid_for`). The rules
        and `[ignore]` are read from the **same** profile — they arrive inside
        `Grid`, and a profile without its own rules (a snapshot of zones alone) falls back to
        the rules of the active config. A "sticky" window and a window with an unidentified screen
        live in the set of the next level. A zone name is global: if the screen's
        set has no such zone, the window is not placed (better not at all than into a foreign
        geometry). A window that lands in the area of the active "this zone only" mode
        (the same workspace and monitor as the target) is placed by the rule and is iconified
        on top of that: that way, after leaving the mode, it turns out to be already in
        its own zone.
        """
        if win in self.windows:
            return  # the window is already placed (a repeated appearance in the list)
        if win in self.detached:
            return  # the user moved the window out of the zone — it will not come back by itself
        if override_redirect:
            return  # a WM menu/tooltip — not a window of its own
        meta = self.read_meta(win)
        wm_class = getattr(meta, "wm_class", "") or ""
        wm_title = getattr(meta, "wm_title", "") or ""
        if not wm_class:
            return  # without WM_CLASS the rule cannot be checked
        # The workspace and the screen are needed before the rules: a screen profile has its own rules and its own
        # exceptions, and the active `zones.conf` has no say here. Without `[monitors]`
        # `_output_for` stays silent and does not go for `xrandr` — the resolution goes by workspace.
        workspace = self._window_workspace(win)
        wa = self._monitor_of(win)
        grid = self._grid_for(workspace, self._output_for(wa))
        if window_excluded(
            wm_class,
            wm_title,
            getattr(meta, "window_type", ()),
            grid.ignore or self.grid.ignore,
        ):
            self.log(
                _(
                    "window 0x{win:x} is excluded ([ignore]/utility window type) - leaving it alone"
                ).format(win=win)
            )
            return
        zone = tiler.match_rules(
            grid.rules or self.grid.rules, wm_class, wm_title
        )
        if zone is None:
            # No rule — in the mode the window is hidden anyway (otherwise it would land
            # on top of the target), but there is nowhere to place it: we simply iconify it.
            if self._focus_covers(win, workspace):
                self._focus_minimize(win)
            return
        if zone not in grid.all_zone_names():
            self.log(
                _(
                    "no zone '{zone}' in the set (workspace {desk}, monitor {output}) - not placing window 0x{win:x}"
                ).format(
                    zone=zone,
                    desk=workspace_label(workspace),
                    output=self._output_for(wa) or _("unknown"),
                    win=win,
                )
            )
            if self._focus_covers(win, workspace):
                self._focus_minimize(win)
            return
        if wa is None:
            return  # the window monitor is unavailable: we do not crash and do not touch the window
        # The wave's design rule: one window occupies the whole zone, two or more
        # stand on the shared tab frame — and then the one already standing is moved as well.
        if self._settle_in_zone(win, zone, wa, workspace, wm_class, grid=grid) is None:
            return  # not remembered: the window is not placed, the zone is not counted as taken
        self.log(
            _("window 0x{win:x} -> zone '{zone}' (workspace {desk})").format(
                win=win, zone=zone, desk=workspace_label(workspace)
            )
        )
        # The "this zone only" mode: the window is already in its zone — now we hide it,
        # so that after the exit it ends up exactly there and not wherever it happens to be.
        if self._focus_covers(win, workspace, wa):
            self._focus_minimize(win)

    def on_destroy(self, win):
        """A window vanished from `_NET_CLIENT_LIST` — drop it from the tracker and, if needed, reflow.

        A rebuild happens only when the workspace of the closed window is visible (`self.workspace`):
        windows of invisible workspaces never move, the state of such a workspace
        is simply cleaned.

        The "this zone only" mode is dropped right here: its target vanished — so there is
        no point in keeping the other windows iconified. A vanished iconified window
        is removed from `minimized` (there is nothing to restore), but with a live target
        the mode stays.
        """
        state = self.focus
        if state is not None:
            state["minimized"].discard(win)
            if win == state["target"]:
                self.log(
                    _(
                        "the focus mode target 0x{win:x} is gone - leaving the mode"
                    ).format(win=win)
                )
                _unused, remaining, consumed = self.leave_focus()
                self._focus_handoff(remaining, consumed)
        zone = self.windows.pop(win, None)
        if zone is None:
            return
        wa = self.win_monitor.pop(win, None)
        workspace = self.win_workspace.pop(win, None)
        self.log(
            _("window 0x{win:x} left zone '{zone}'").format(win=win, zone=zone)
        )
        # A zone is taken while it holds at least one tab: the last one is gone — below
        # reflow sees the zone as free, and while the stack holds one — it gets
        # the whole zone back (a single window has no strip).
        self._collapse_stack(zone, wa, workspace)
        # The holder is gone: drop the records of the zones it was covering.
        self._drop_coverage(win)
        if self.reflow != REFLOW_NEIGHBORS or wa is None:
            return
        if not self._visible(workspace):
            self.log(
                _("workspace {desk} is not visible (currently {current}) - not doing reflow").format(
                    desk=workspace_label(workspace),
                    current=workspace_label(self.workspace),
                )
            )
            return
        # reflow — within the monitor of the vanished window and the visible workspace: windows on
        # other screens and workspaces are not counted in taken zones and are not moved.
        occupied = {
            other_zone
            for other, other_zone in self.windows.items()
            if self.win_monitor.get(other) == wa
            and self._visible(self.win_workspace.get(other))
        }
        grid = self._grid_for(self.workspace, self._output_for(wa))
        zones = {name: grid.fractions(name) for name in grid.zones}
        changes = zone_reflow.reflow(zones, zone, occupied)
        if not changes:
            return
        for name, frac in changes.items():
            if len(self._zone_stack(name, wa, self.workspace)) > 1:
                # The zone holds a stack of tabs: they live on the shared frame, and taking
                # one window out of it means breaking up the stack. We do not
                # expand such a zone, and we do not count the zone as covered.
                self.log(
                    _("reflow: zone '{zone}' has tabs - not expanding").format(
                        zone=name
                    )
                )
                continue
            target = self._window_for_zone(name, wa)
            if target is None:
                continue
            fx, fy, fw, fh = frac
            if not self._apply(target, fractions_to_pixels(fx, fy, fw, fh, wa), name):
                continue  # the window did not move: the zone is not counted as covered
            self.filled[(self.workspace, wa, zone)] = target
            self.log(
                _("reflow: window 0x{win:x} -> zone '{zone}'").format(
                    win=target, zone=name
                )
            )
        self._save_state()

    def on_unmap(self, win):
        """A window is hidden (iconified/the workspace changed) — no-op.

        Such a window stays in `_NET_CLIENT_LIST`, so reconciliation does not
        drop it from the tracker: on restore the window is not placed again, and
        the neighbours do not expand onto a taken zone. Dropping from the tracker and reflow happen
        only when the window disappears from `_NET_CLIENT_LIST` (`on_destroy`), and the
        "the window's workspace is visible" check sits there too: there is nothing to rebuild here.
        """
        self.log(_("window 0x{win:x} is hidden - keeping it in the tracker").format(win=win))


# ------------------------------------------------------------------ X11 side


class XPropertyEvent(ctypes.Structure):
    # Field order and types follow Xlib.h (XPropertyEvent).
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("atom", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("state", ctypes.c_int),
    ]


def handle_property_event(dae, buf, client_list_atom, read_clients, desktop_atom=None):
    """PropertyNotify: `_NET_CLIENT_LIST` — reconcile, `_NET_CURRENT_DESKTOP` — the workspace.

    Returns True if the event was about one of these properties. If the
    list could not be read (`read_clients()` returned `None`), reconciliation is
    skipped: the tracker and `seen` are not touched.

    `desktop_atom` is the `_NET_CURRENT_DESKTOP` atom: the daemon updates the
    active workspace on it (`dae.on_workspace_change()`), but does not move windows: placement and
    reflow go only by window events. `None` — the atom was not passed, the workspace event is
    skipped (the old four-argument call).
    """
    ev = ctypes.cast(buf, ctypes.POINTER(XPropertyEvent)).contents
    if desktop_atom is not None and ev.atom == desktop_atom:
        dae.on_workspace_change()
        return True
    if ev.atom != client_list_atom:
        return False
    client_ids = read_clients()
    if client_ids is None:
        log = getattr(dae, "log", None)
        if log is not None:
            log(_("_NET_CLIENT_LIST is unavailable - skipping reconciliation"))
        return True
    dae.reconcile(client_ids)
    return True


def _reconcile_if_known(dae, read_clients):
    """Read the client list and reconcile the state; `None` — skip."""
    client_ids = read_clients()
    if client_ids is None:
        dae.log(_("_NET_CLIENT_LIST is unavailable - skipping reconciliation"))
        return
    dae.reconcile(client_ids)


def _bind_event_functions(x):
    """Declare signatures for the libX11 functions missing from `zone-tiler.X11`."""
    x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
    x.XSelectInput.restype = ctypes.c_int
    x.XConnectionNumber.argtypes = [ctypes.c_void_p]
    x.XConnectionNumber.restype = ctypes.c_int
    x.XPending.argtypes = [ctypes.c_void_p]
    x.XPending.restype = ctypes.c_int
    x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x.XNextEvent.restype = ctypes.c_int
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.restype = ctypes.c_int


def _serve_ipc(dae, ipc):
    """Accept and serve socket commands; errors do not bring the daemon down.

    Answers and errors go to the log (`--verbose`). An exception of accepting/parsing
    is logged and swallowed: the socket is an auxiliary channel, the X loop matters more.
    """
    try:
        responses = ipc.accept_all()
    except (Exception, SystemExit) as exc:
        dae.log(_("IPC socket error: {exc}").format(exc=exc))
        return
    for response in responses:
        if response.get("ok"):
            dae.log("IPC: " + json.dumps(response, ensure_ascii=False))
        else:
            dae.log(
                _("IPC error: {error}").format(error=response.get("error", "?"))
            )


def _restore_focus_on_exit(dae):
    """Restore the windows of the "this zone only" mode on daemon exit (best-effort).

    Called from the `finally` of `serve` — before `XCloseDisplay`, otherwise there will be
    nowhere left to send. If there is no X connection (early exit, X server refusal), we simply
    drop the state: that way nobody is left with iconified windows
    without an owner after the daemon exits.
    """
    if getattr(dae, "focus", None) is None:
        return
    log = getattr(dae, "log", None)
    if getattr(dae, "x11", None) is None:
        dae.focus = None
        if log is not None:
            log(_("no X connection - not unminimizing focus mode windows"))
        return
    try:
        _unused, remaining, state = dae.leave_focus()
        dae._focus_handoff(remaining, state)
    except (Exception, SystemExit) as exc:
        if log is not None:
            log(
                _("failed to leave focus mode: {exc}").format(
                    exc=exc
                )
            )


def serve(dae, x11=None, once=False, read_clients=None, once_timeout=ONCE_TIMEOUT, ipc=None):
    """The X11 event loop: PropertyNotify about `_NET_CLIENT_LIST` -> reconciliation.

    The subscription is `PropertyChangeMask` on root. At start the current client list
    is read once and the already open windows are placed; then the same happens on
    every PropertyNotify about `_NET_CLIENT_LIST`. A PropertyNotify about
    `_NET_CURRENT_DESKTOP` updates only the daemon's active workspace.

    `x11` and `read_clients` can be substituted (then no real display is needed).
    `serve` puts its own connection into `dae.x11`, so that `apply_zone` reuses
    it for all windows and does not open a new one per `apply` (a connection leak).
    `None` returned by `read_clients` (the list is unavailable) is a signal to skip
    reconciliation without touching the tracker.
    `once=True` — handle one PropertyNotify and exit; if there are no events —
    wait `once_timeout` seconds and exit (for smoke checks).
    `ipc` is the listening socket `zone_ipc.Server`: its descriptor joins the same
    `select`, and commands are served between X events; the `quit` command sets
    `dae.stop_requested`, and the loop ends.

    Subscriber descriptors (`dae.subscribers`) join the same `select`, and
    what they send is parsed by `Subscribers.poll`: that way a dropped subscriber
    is noticed between X events and the daemon forgets it instead of writing into the void.
    Without `once` the wait is bounded by `dae.theme_interval` (`None` — unbounded, as before), and
    every pass calls `dae.poll_theme()`: the palette of a generated `theme.conf` follows the
    wallpaper. `--once` never refreshes the palette — a probe must not write it.
    """
    if x11 is None:
        x11 = tiler.X11()
    if read_clients is None:
        read_clients = read_client_list
    dae.x11 = x11  # the shared connection: apply_zone must not close it
    _bind_event_functions(x11.x)
    x11.x.XSelectInput(x11.dpy, x11.root, PROPERTY_CHANGE_MASK)
    x11.x.XSync(x11.dpy, 0)
    client_list_atom = x11.atom(CLIENT_LIST_ATOM)
    # The same PropertyChangeMask on root also delivers the active workspace change.
    desktop_atom = x11.atom(CURRENT_DESKTOP_ATOM)
    fd = x11.x.XConnectionNumber(x11.dpy)
    watch = [fd]
    if ipc is not None:
        watch.append(ipc.fileno())
    # `once` keeps its short probe wait; otherwise the loop wakes up for the palette check.
    wait = once_timeout if once else getattr(dae, "theme_interval", None)
    dae.log(
        _("the daemon listens on X11 (root 0x{root:x}, reflow={reflow})").format(
            root=x11.root, reflow=dae.reflow
        )
    )
    if ipc is not None:
        dae.log(_("IPC socket: {path}").format(path=getattr(ipc, "path", "?")))
    try:
        # We place the windows already open at start (entering a ready session).
        _reconcile_if_known(dae, read_clients)
        while True:
            subscribers = getattr(dae, "subscribers", None)
            watch_now = list(watch)
            if subscribers is not None:
                watch_now.extend(subscribers.filenos())
            if not x11.x.XPending(x11.dpy):
                ready, _unused, _unused_again = select.select(watch_now, [], [], wait)
                if not ready and once:
                    break  # once: no event ever arrived
            handled = False
            while x11.x.XPending(x11.dpy):
                buf = EVENT_BUFFER()
                x11.x.XNextEvent(x11.dpy, ctypes.byref(buf))
                etype = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
                if etype == PROPERTY_NOTIFY:
                    handled = (
                        handle_property_event(
                            dae, buf, client_list_atom, read_clients, desktop_atom
                        )
                        or handled
                    )
            if ipc is not None:
                _serve_ipc(dae, ipc)
            if subscribers is not None:
                # A dropped subscriber is not an X event: we notice it here.
                subscribers.poll()
            if not once:
                # Throttled inside: a burst of X events does not read the setting every time.
                poll_theme = getattr(dae, "poll_theme", None)
                if poll_theme is not None:
                    poll_theme()
            if getattr(dae, "stop_requested", False):
                break
            if once and handled:
                break
    except KeyboardInterrupt:
        pass
    finally:
        # The "this zone only" mode must not outlive the daemon: otherwise iconified windows
        # would stay iconified without an owner. The tabs strip also lives exactly
        # as long as the daemon: EOF on stdin ends its main loop.
        _restore_focus_on_exit(dae)
        strip = getattr(dae, "strip", None)
        if strip is not None:
            strip.stop()
        x11.x.XCloseDisplay(x11.dpy)


# ------------------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=_(
            "GridSnap automatic window layout by [rules] and reflow (Cinnamon/X11)"
        )
    )
    ap.add_argument(
        "--conf",
        default=None,
        help=_("path to zones.conf (XDG or next to the code by default)"),
    )
    ap.add_argument(
        "--verbose", action="store_true", help=_("print what the daemon does")
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help=_("handle one _NET_CLIENT_LIST change and exit"),
    )
    ap.add_argument(
        "--socket",
        default=None,
        help=_(
            "UNIX IPC socket path ($XDG_RUNTIME_DIR/gridsnap/daemon.sock by default)"
        ),
    )
    ap.add_argument(
        "--no-socket", action="store_true", help=_("do not raise the IPC socket")
    )
    ap.add_argument("--version", action="version", version=f"zone-daemon {VERSION}")
    args = ap.parse_args(argv)

    conf_path = args.conf or tiler.default_conf()
    dae = Daemon(
        conf_path,
        state_path=placed_path(),
        session_id=read_session_id(),
        focus_path=tiler.focus_path(),
        strip=TabStripProcess(conf_path, verbose=args.verbose),
        theme_interval=None if args.once else THEME_POLL_SECONDS,
        verbose=args.verbose,
    )
    server = None
    if not args.no_socket:
        path = args.socket or zone_ipc.default_socket_path()
        try:
            server = zone_ipc.Server(
                path, dae.build_dispatcher(), subscribers=dae.subscribers
            )
        except OSError as exc:
            print(
                "zone-daemon: " + _("IPC socket: {exc}").format(exc=exc),
                file=sys.stderr,
            )
            return tiler.EXIT_FAIL
    try:
        serve(dae, once=args.once, ipc=server)
    finally:
        if server is not None:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""window_backend — the only place where the product talks to the window system.

Today the window system is X11/EWMH: the window list, frames, `_NET_*` properties,
`XSendEvent` commands, the work area of the monitors. All the calls to `xprop`,
`xwininfo`, `xrandr` and the class working with `libX11` move here as well (right now they live in `zone-tiler.py`
mixed with the layout logic), so that the window layer has **one** boundary.

Why: Cinnamon is getting a full Wayland session (Cinnamon 6.8 / Linux Mint 23,
release around Christmas 2026), where the window layer is different — there is no EWMH, and window
management works differently. While the whole conversation with windows is scattered over the tiler and the daemon,
any Wayland path is a rewrite; with a boundary it is a separate implementation of the same
interface. Reconnaissance (the "seam + reconnaissance" wave) finds by measurement what of this
interface is available at all under muffin as a Wayland compositor.

State of the transition: the function bodies moved here from `zone-tiler.py`/`zone-daemon.py`
without a change of behaviour, and `zone-tiler`/`zone-daemon` themselves keep wrappers with the former
names — the callers (daemon, picker, applet, tests) notice nothing. The substitutions in
the tests (`zt.subprocess.check_output`, `zt.X11`, `zt.monitors`) keep working:
it is the same `subprocess` module and the same names that the wrappers call.

What is deliberately **not** here:

* texts for the user — the message and the return code stay with the CLI (the wrappers in
  `zone-tiler.py`); from here only an exception with a reason or a reason code;
* a change observer (a subscription to `PropertyNotify` in the daemon loop) — the most
  risky part of the seam, it is moved in a separate step: this wave has reading and writing;
* hotkeys: `gsettings` is not the window layer, and on Wayland the mechanism is different.

Rules for implementations: do not change the behaviour and the texts of the exceptions (the
existing tests check them), the X server may be absent — no call may fail
with a traceback instead of a clear exception.
"""

import ctypes
import ctypes.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_units  # noqa: E402  (unit conversion: GTK logical pixels ↔ physical X)

# The error of the window layer: "there is no window/work area, or the system did not answer".
# The text is the technical reason; the user message and the return code are set by the CLI.
class WindowBackendError(RuntimeError):
    """The window system did not answer or cannot do the needed operation."""


# The reasons why `read_active_window` did not return a window. This is not text: what
# to show a person and with which code to exit is decided by the CLI (`zone-tiler.py`),
# otherwise the window layer would have to be translated separately from the interface.
ACTIVE_WINDOW_NO_XPROP = "xprop"
ACTIVE_WINDOW_NO_WINDOW = "no-active-window"

# The root property with the list of managed client windows (EWMH).
CLIENT_LIST_ATOM = "_NET_CLIENT_LIST"


# --------------------------------------------------------------- reading windows


def _xprop_props(text):
    """Parse xprop output into {property name: [value lines]}.

    Continuation lines (a value moved to the next line) are glued
    to the current property, so that the same parsing suits both the one-line
    form `WM_CLASS(STRING) = "a", "b"` and the multi-line one.
    """
    props = {}
    current = None
    for raw in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s*=\s*(.*)$", raw)
        if m:
            current = m.group(1)
            props.setdefault(current, []).append(m.group(2))
        elif current is not None and raw.strip():
            props[current].append(raw.strip())
    return props


def _strings(value):
    """The strings of an xprop value: in quotes — as is, otherwise split by comma."""
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    if quoted:
        return [re.sub(r"\\(.)", r"\1", s) for s in quoted]
    return [s.strip() for s in value.split(",") if s.strip()]


def window_class_title(win):
    """WM_CLASS (the class, the second string) and the window title through xprop.

    The title is taken from _NET_WM_NAME, and without it — from WM_NAME. Any
    error (no xprop, no window, no access) gives ("", "") — the caller simply finds
    no matches. The pure parsing of the output is moved out into `_xprop_props`/`_strings`.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "WM_CLASS", "_NET_WM_NAME", "WM_NAME"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        props = _xprop_props(out)
        wm_class = ""
        classes = _strings(" ".join(props.get("WM_CLASS", [])))
        if len(classes) >= 2:
            wm_class = classes[1]  # instance, class -> take the class
        elif classes:
            wm_class = classes[0]
        wm_title = ""
        for prop in ("_NET_WM_NAME", "WM_NAME"):
            names = _strings(" ".join(props.get(prop, [])))
            if names and names[0]:
                wm_title = names[0]
                break
        return (wm_class, wm_title)
    except Exception:
        return ("", "")


def window_types(win):
    """The set of `_NET_WM_WINDOW_TYPE` of the window through xprop (empty on error)."""
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_WM_WINDOW_TYPE"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return frozenset()
    return frozenset(re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z_]+", out))


def read_client_list():
    """The client windows from `_NET_CLIENT_LIST` (EWMH) through `xprop -root`.

    Returns a list of ids in the order of the property (bottom to top), or `None` if
    the list is unavailable: no xprop/display, the property itself is absent (xprop
    prints `no such atom on any window.`) or the output has no marker
    `window id #`. An empty list is a valid state "there are no clients", it
    must not be confused with an error. The function can be substituted in the tests.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", CLIENT_LIST_ATOM],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    # `_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0x600005`
    if "window id #" not in out:
        return None
    _unused, _unused2, value = out.partition("#")
    return [int(h, 16) for h in re.findall(r"0x[0-9a-fA-F]+", value)]


def window_workspace(win):
    """The number of the virtual desktop of the window (`_NET_WM_DESKTOP`), or None."""
    try:
        out = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_WM_DESKTOP"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"=\s*(-?\d+)", out)
    return int(found.group(1)) if found else None


def _current_workspace_raw():
    """``(desktop number | None, does X answer)`` — one `xprop` command.

    The split is needed to tell "X does not answer" (no `xprop`, no display —
    the command fails) from "X is alive, but there is no property". The second is a Wayland
    session of Cinnamon before the first desktop switch: `xprop` prints "no such atom on any
    window" and exits with zero (checked on the machine), that is, the command succeeds.
    Without this distinction either Wayland gets a refusal instead of a desktop, or "no X"
    silently becomes the first desktop — and those are different things.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_CURRENT_DESKTOP"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None, False
    found = re.search(r"=\s*(-?\d+)", out)
    if not found:
        return None, True
    number = int(found.group(1))
    return (number if number >= 0 else None), True


def current_workspace():
    """The number of the active desktop (`_NET_CURRENT_DESKTOP`), or None.

    Zero is the first desktop: that is how EWMH numbers it. In ``[workspaces]`` the keys are
    one greater (as in the Cinnamon interface), the conversion is the caller's
    (`zone-tiler.grid_for_workspace`).
    """
    return _current_workspace_raw()[0]


def x_alive():
    """Does the X server answer: `_NET_SUPPORTED` is read (a live WM has it).

    Needed where "X does not answer" (no `xprop`, no display) has to be told from
    "X is alive, but the needed property is absent" — the second happens in a Wayland session
    of Cinnamon (measurement: ``docs/RESEARCH-wayland-recon.md``). It is asked only in a rare
    case (there is no desktop property and there is no substitution): in an ordinary session —
    not a single extra subprocess.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_SUPPORTED"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return "_NET_SUPPORTED" in out and "no such atom" not in out


def current_workspace_or_first():
    """The number of the active desktop; without the property, but with a live X — 0 (the first desktop).

    In a Wayland session of Cinnamon (measurement: ``docs/RESEARCH-wayland-recon.md``) the
    `_NET_CURRENT_DESKTOP` property is absent until the first real desktop switch, although the
    desktop exists and windows lie on it: without this margin `focus` refused ("failed to
    determine the active workspace"), and `wmctrl -d` did not work at all.

    `None` is left exactly for the case "X does not answer": then the desktop is as before
    unknown, and calling it zero is not allowed — on X11 the property does not disappear, so
    the parsing changes nothing there.
    """
    desk = current_workspace()
    if desk is not None:
        return desk
    return 0 if x_alive() else None


def window_iconic(win):
    """Whether the window is minimised now — `WM_STATE: Iconic` or `_NET_WM_STATE_HIDDEN`.

    muffin sets both marks on `WM_CHANGE_STATE` (verified by smoke on
    Cinnamon/muffin), so the real EWMH/ICCCM mark is read, not a
    guess from the geometry. If the property cannot be read, the window counts as visible.

    ``Iconic`` means two different things: the user minimised the window, **or** the window
    sits on a desktop that is not active. Measured on X11 as well as under the nested
    Wayland session (``docs/RESEARCH-wayland-recon.md``): ``xdotool set_desktop_for_window``
    turns ``WM_STATE`` into ``Iconic`` and switching to that desktop turns it back. One
    ``_NET_ACTIVE_WINDOW`` does not bring such a window back, the desktop has to be switched
    first. Callers that must tell the two apart switch the desktop themselves —
    ``zone-tiler._focus_restore`` groups the windows by desktop for exactly that reason —
    rather than asking a second question of every window.
    """
    try:
        txt = subprocess.check_output(
            ["xprop", "-id", hex(win), "WM_STATE", "_NET_WM_STATE"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return "Iconic" in txt or "_NET_WM_STATE_HIDDEN" in txt


def read_active_window():
    """The active window and the error text: ``(window | None, reason | None)``.

    The reason for a refusal is a code (`ACTIVE_WINDOW_NO_XPROP`, `ACTIVE_WINDOW_NO_WINDOW`),
    and not ready-made text: the message and the return code are set by the CLI, so the parsing is
    one for all. `active_window` in `zone-tiler.py` fails with code 1 when there is no window
    (that is right for the commands that need a window), and `active_window_quiet`
    returns ``None`` without messages on stderr.
    """
    try:
        txt = subprocess.check_output(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None, ACTIVE_WINDOW_NO_XPROP
    m = re.search(r"(0x[0-9a-fA-F]+)", txt)
    if not m or int(m.group(1), 16) == 0:
        return None, ACTIVE_WINDOW_NO_WINDOW
    return int(m.group(1), 16), None


def read_frame(window, extents):
    """The actual frame of the window (x, y, w, h) from xwininfo, taking the WM frame into account.

    If a needed field is not in the output (`Absolute upper-left X`, `Width`, ...) —
    `WindowBackendError`, `args[0]` is the field name: the text for a person is assembled
    by the wrapper in `zone-tiler.py`. Other xwininfo failures (no utility, the window
    disappeared) are passed through as is — the callers show them.
    """
    out = subprocess.check_output(
        ["xwininfo", "-id", hex(window)], text=True, stderr=subprocess.DEVNULL
    )

    def num(label):
        found = re.search(rf"{label}:\s+(-?\d+)", out)
        if found is None:
            raise WindowBackendError(label)
        return int(found.group(1))

    geom = {
        k: num(k)
        for k in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
    }
    left, right, top, bottom = extents
    return (
        geom["Absolute upper-left X"] - left,
        geom["Absolute upper-left Y"] - top,
        geom["Width"] + left + right,
        geom["Height"] + top + bottom,
    )


def read_window_center(win):
    """The centre of the window (px, py) by the absolute `xwininfo` geometry; None on error.

    Needed only to pick the monitor of the window, that is why the client
    geometry is taken whole: the WM frame shifts it by a few pixels and does not
    affect the choice of the monitor.
    """
    try:
        out = subprocess.check_output(
            ["xwininfo", "-id", hex(win)], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None

    def num(label):
        found = re.search(rf"{label}:\s+(-?\d+)", out)
        return int(found.group(1)) if found else None

    x = num("Absolute upper-left X")
    y = num("Absolute upper-left Y")
    w = num("Width")
    h = num("Height")
    if x is None or y is None or w is None or h is None:
        return None
    return (x + w // 2, y + h // 2)


def frame_extents(win):
    """The thickness of the window manager frame/titlebar: (left, right, top, bottom)."""
    try:
        txt = subprocess.check_output(
            ["xprop", "-id", hex(win), "_NET_FRAME_EXTENTS"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        nums = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
        if len(nums) == 4 and all(0 <= n < 200 for n in nums):
            return tuple(nums)
    except Exception:
        pass
    return (0, 0, 0, 0)


def read_session_id():
    """The identifier of the X session: the `_NET_SUPPORTING_WM_CHECK` window (or None).

    The memory of tiled layouts must not move into a new X session: there XIDs are handed
    out anew, and an old entry may "hit" a foreign window of the same class.
    If it cannot be read — `None`, and then the check is off.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
    return found.group(1) if found else None


# ------------------------------------------------------------- monitors


def prefer_x11_gdk():
    """Make GDK go through X11 (Xwayland) — before its first initialisation.

    In a Wayland session GDK picks the wayland backend by default: then the work areas
    and the scale come from Wayland (where there are no X11 struts), while we move
    windows in Xwayland coordinates — the program starts measuring the world with two rulers.
    Measurement and analysis — `docs/RESEARCH-wayland-recon.md`.

    Call it **before** the first access to GDK/GTK: in GTK programs (picker, HUD,
    tab strip) — before `import gi`/`Gtk.init()`. In an X11 session the call changes
    nothing, `GDK_BACKEND` is not overridden from outside (`setdefault`).
    """
    os.environ.setdefault("GDK_BACKEND", "x11")


def gdk_monitors():
    """``[(logical work area, scale)]`` from GDK; ``[]`` — GDK is unavailable.

    One parsing for `monitors()` and `monitor_scales()`, so that the order and the set
    of monitors do not diverge between them. The area here is still logical: GTK counts in
    logical pixels, and `monitors()` converts it into physical ones.

    Before accessing GDK the X11 backend is enabled (`prefer_x11_gdk`): otherwise in a
    Wayland session the areas would come from a different world than the window coordinates.
    """
    prefer_x11_gdk()
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk  # type: ignore

        dpy = Gdk.Display.get_default()
        out = []
        for i in range(dpy.get_n_monitors() if dpy else 0):
            mon = dpy.get_monitor(i)
            a = mon.get_workarea()
            out.append(((a.x, a.y, a.width, a.height), mon.get_scale_factor()))
        return out
    except Exception:
        # Neither GDK nor a display: the tiler manages on `_NET_WORKAREA` alone.
        return []


def monitors():
    """The work areas of the monitors in physical X pixels.

    GDK gives them in logical ones: with `GDK_SCALE=2` a 1920x1080 monitor looks like
    960x540, while the window moves in physical X coordinates. Without the conversion a
    zone would land half as big and twice as close to the top-left corner — the share 0.8358
    would give 802 instead of 1605.

    `WindowBackendError` — neither GDK nor the `_NET_WORKAREA` fallback answered; the text
    for a person and the return code are set by the wrapper in `zone-tiler.py`.
    """
    out = [zone_units.physical_rect(area, scale) for area, scale in gdk_monitors()]
    if out:
        return out
    # fallback: EWMH `_NET_WORKAREA` (the first desktop) — already physical pixels
    try:
        txt = subprocess.check_output(
            ["xprop", "-root", "_NET_WORKAREA"], text=True, stderr=subprocess.DEVNULL
        )
        nums = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
        if len(nums) == 4 and nums[2] > 0:
            return [tuple(nums)]
    except Exception:
        pass
    raise WindowBackendError("monitors: no GDK display and no _NET_WORKAREA")


def monitor_scales():
    """The scales of the monitors in the same order as `monitors()`/`monitor_outputs()`.

    Needed by those who live in GTK logical pixels — the overlay window of the picker, the
    HUD window and the tab strip: they convert a physical frame into it with this number.
    Without GDK the list is empty, and `zone_units.scale_at` gives the same consumers 1 —
    exactly as in the `_NET_WORKAREA` fallback, where the scale cannot be asked for either.

    It is read without subprocesses: the daemon compares this fingerprint on the fly to see
    whether the screen scale has changed.
    """
    return [scale for _area, scale in gdk_monitors()]


def scale_at_point(point):
    """The scale of the monitor whose physical work area contains the point."""
    return zone_units.scale_for_point(monitors(), monitor_scales(), point)


def monitor_outputs():
    """[(output name, (x, y, w, h))] — one per monitor.

    The name (`eDP-1`, `HDMI-1`) is stable across reboots and reconnections of the cable,
    so it is the name that addresses a zone set (`[zones:<output>]`) and `--monitor`.
    The geometry is the former GDK `get_workarea()` (the panel is already subtracted in it),
    but in physical X pixels (`monitors()`), not the xrandr geometry: xrandr gives
    the full screen of the output, without panels. The substitutions in the tests
    (`zone-tiler.monitors`, `zone-tiler.xrandr_monitors`) are honoured by the wrapper in `zone-tiler.py` itself.
    """
    return stitch_monitor_names(xrandr_monitors(), monitors())


# A line of `xrandr --listmonitors`: ` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`.
# The `+*` flags refer to the state of the monitor (`+` — automatic, `*` — primary) and
# are not part of the name.
_XRANDR_MONITOR_RE = re.compile(
    r"^\s*\d+:\s+[+*]*(?P<fallback>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    r"(?:\s+(?P<name>\S+))?\s*$"
)


def parse_xrandr_monitors(text):
    """[(name, (x, y, w, h))] from the output of `xrandr --listmonitors`, in its order.

    The name is the last field of the line (` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`).
    If there is no name in the line (an old xrandr), the field after `N:` without the
    `+*` flags is taken. Lines without geometry (the `Monitors:` header, garbage) are skipped.
    A pure function — for the tests.
    """
    out = []
    for raw in text.splitlines():
        m = _XRANDR_MONITOR_RE.match(raw)
        if not m:
            continue
        name = (m.group("name") or m.group("fallback") or "").lstrip("+*")
        if not name:
            continue
        rect = (
            int(m.group("x")),
            int(m.group("y")),
            int(m.group("w")),
            int(m.group("h")),
        )
        out.append((name, rect))
    return out


def xrandr_monitors():
    """The monitors from `xrandr --listmonitors`; [] if there is no utility or it failed."""
    try:
        text = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    return parse_xrandr_monitors(text)


def overlap_area(a, b):
    """The area of the intersection of the rectangles a and b (0 if they do not intersect)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0


def stitch_monitor_names(outputs, areas):
    """Stitch output names with work areas: [(name, area)].

    The counts matched — the names in order: GDK and xrandr have the same order of
    monitors. Otherwise — by the maximum intersection of areas (GDK may give one
    area for several outputs or the other way round), and areas without an intersection
    get the name `monitor-<i>`. Without names (no xrandr) — only `monitor-<i>`, that
    is, the former behaviour. A pure function — for the tests.
    """
    if not outputs:
        return [(f"monitor-{i}", wa) for i, wa in enumerate(areas)]
    if len(outputs) == len(areas):
        return [(outputs[i][0], areas[i]) for i in range(len(areas))]
    stitched = []
    used = set()
    for i, wa in enumerate(areas):
        best = None
        best_area = 0
        for j, (_unused, rect) in enumerate(outputs):
            if j in used:
                continue
            area = overlap_area(wa, rect)
            if area > best_area:
                best, best_area = j, area
        if best is None:
            stitched.append((f"monitor-{i}", wa))
        else:
            used.add(best)
            stitched.append((outputs[best][0], wa))
    return stitched


def monitor_for_point(monitors, point):
    """The work area of the monitor containing the point; otherwise the nearest by centre.

    The point may fall into no area at all (a window under the panel, in the gap between
    monitors) — then the area with the nearest centre is taken, not monitor 0.
    `monitors` is a non-empty list of rectangles (x, y, w, h).
    """
    px, py = point
    for wa in monitors:
        x, y, w, h = wa
        if x <= px < x + w and y <= py < y + h:
            return wa
    return min(
        monitors,
        key=lambda wa: (wa[0] + wa[2] / 2 - px) ** 2 + (wa[1] + wa[3] / 2 - py) ** 2,
    )


# ------------------------------------------------------------- writing (X11)


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        ("pad", ctypes.c_long * 15),  # so that the structure is not smaller than sizeof(XEvent)
    ]


CLIENT_MESSAGE = 33
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # Notify | Redirect
X_FLAGS = 1 | 2 | 4 | 8  # X, Y, Width, Height
GRAVITY_NORTH_WEST = 1
SOURCE_PAGER = 2
# `WM_CHANGE_STATE` with the value IconicState (ICCCM) — a request for the WM to iconify the window.
ICONIC_STATE = 3


class X11:
    """A connection to the X server and commands to the window manager through `XSendEvent`.

    `move_resize` (`_NET_MOVERESIZE_WINDOW`), `set_state` (`_NET_WM_STATE`),
    `iconify` (`WM_CHANGE_STATE`), `activate` (`_NET_ACTIVE_WINDOW`),
    `set_current_desktop` (`_NET_CURRENT_DESKTOP`), `atom`, `send`, `close`.

    A connection failure — `WindowBackendError`; a foreign connection is not
    closed (its owner is the caller).
    """

    def __init__(self):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        self.x = ctypes.CDLL(name)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        dpy_name = (os.environ.get("DISPLAY") or "").encode() or None
        self.dpy = self.x.XOpenDisplay(dpy_name)
        if not self.dpy:
            raise WindowBackendError("X11: XOpenDisplay failed (no DISPLAY?)")
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.root = self.x.XDefaultRootWindow(self.dpy)
        self.x.XInternAtom.restype = ctypes.c_ulong
        self.x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(XClientMessageEvent),
        ]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XCloseDisplay.restype = ctypes.c_int

    def close(self):
        """Close the X connection (paired with `XOpenDisplay`). Idempotent."""
        if getattr(self, "dpy", None):
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    def atom(self, name):
        """An atom by name."""
        return self.x.XInternAtom(self.dpy, name.encode(), 0)

    def send(self, win, msg_type, values):
        """Send a client message to the window (`XSendEvent`)."""
        ev = XClientMessageEvent()
        ev.type = CLIENT_MESSAGE
        ev.send_event = 1
        ev.display = self.dpy
        ev.window = win
        ev.message_type = self.atom(msg_type)
        ev.format = 32
        for i, v in enumerate(values):
            ev.data[i] = v
        self.x.XSendEvent(self.dpy, self.root, 0, SUBSTRUCTURE_MASK, ctypes.byref(ev))
        self.x.XSync(self.dpy, 0)

    def set_state(self, win, action, first, second=None):
        """`_NET_WM_STATE`: action: 0 = remove, 1 = add, 2 = toggle.

        `second` — the second state property (for example
        `_NET_WM_STATE_MAXIMIZED_HORZ`); empty — without it.
        """
        self.send(
            win,
            "_NET_WM_STATE",
            [action, self.atom(first), self.atom(second) if second else 0, SOURCE_PAGER, 0],
        )

    def move_resize(self, win, x, y, w, h):
        """`_NET_MOVERESIZE_WINDOW`: move and resize the window."""
        flags = (SOURCE_PAGER << 12) | (X_FLAGS << 8) | GRAVITY_NORTH_WEST
        self.send(win, "_NET_MOVERESIZE_WINDOW", [flags, x, y, w, h])

    def iconify(self, win):
        """Iconify the window: `WM_CHANGE_STATE` with the value `IconicState`."""
        self.send(win, "WM_CHANGE_STATE", [ICONIC_STATE, 0, 0, 0, 0])

    def activate(self, win):
        """Raise and activate the window: `_NET_ACTIVE_WINDOW` on behalf of the pager.

        `data.l[0] = 2` (source indication, pager) and a zero timestamp: muffin
        accepts activation from the pager, while a stale non-zero timestamp it may
        reject (focus-stealing prevention) — and the window would have stayed iconified.

        Important: for a window **not on the active desktop** muffin ignores this
        message (verified by smoke) — `set_current_desktop` is needed first.
        """
        self.send(win, "_NET_ACTIVE_WINDOW", [SOURCE_PAGER, 0, 0, 0, 0])

    def set_current_desktop(self, desktop):
        """Switch the active desktop: `_NET_CURRENT_DESKTOP` on root.

        `ev.window` — the root itself, the way wmctrl/xdotool do it: muffin accepts
        such a request with a zero timestamp (verified by smoke).
        """
        self.send(self.root, "_NET_CURRENT_DESKTOP", [desktop, 0, 0, 0, 0])

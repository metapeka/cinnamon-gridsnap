#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone-tabstrip.py — tab strip above a zone: it draws it, hands clicks to the daemon.

The daemon (``zone-daemon.py``) starts the process for all zones at once:

    python3 zone-tabstrip.py --conf <zones.conf>

After that it is a filter: the daemon writes line-wise **full** snapshots to stdin
(``zone_tabs.strips_payload``), and the strip draws one window per record:

    {"strips": [{"zone": "main", "rect": [0, 0, 960, 22],
                 "tabs": [{"window": 12345, "title": "Firefox", "active": true}]}]}

The strip window is override-redirect (``Gtk.WindowType.POPUP``): it sits above the zone
windows, takes no focus, does not appear in the task list and does not intercept input. The zone
window sits **below** the strip (zone minus strip, ``zone_tabs.tab_rect``), which is why
the strip does not get in the way of dragging a window by its title — it never covers it.
A click on a tab goes to the daemon socket (``zone_ipc``) as a request
``zone_tabs.tabs_switch_command``, the same way ``gridsnap-ctl`` does it: the strip has
no channel of its own.

Two coordinate spaces: the ``rect`` frame in the snapshot is **physical** X pixels
(those are the coordinates of the zone window), while the GTK window lives in **logical** ones. The conversion is single,
in :func:`window_rect` via ``zone_units.logical_rect``, with the monitor scale
under the centre of the strip itself: with ``GDK_SCALE=2`` and no conversion the strip would move twice as far
from the zone and grow twice as large. The same scale converts local coordinates:
cairo in the window draws in logical pixels (:func:`local_rect`), and the click event
arrives in them as well, while tab frames in the snapshot are physical (:func:`local_point`).
Without that, at scale 2 the tabs would be drawn twice as wide as the strip, and a single one
would remain in the window, stretched over its whole width.

An empty snapshot (``{"strips": []}``) removes all strip windows, EOF on stdin means
the process ends: the daemon closed the stream, so there is nobody left to show strips to.

The pure part lives in ``zone_tabstrip_nav`` (snapshot parsing, tab frames, captions,
the tab under the click point), without GTK; here there are only windows, cairo and the socket. Without GTK
(``python3-gi``, ``gir1.2-gtk-3.0``) the process says so honestly on stderr and
exits with code 1: silently not drawing the strip is not allowed — then it is unclear why
the tabs are not visible.
"""

import argparse
import importlib.util
import os
import signal
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_tabstrip_nav as nav  # noqa: E402  (looked up next to itself, the way it is installed)
import zone_units  # noqa: E402  (the only conversion of logical px to physical ones)
import zone_theme  # noqa: E402  (one palette for every surface: theme.conf next to zones.conf)
import window_backend  # noqa: E402  (window layer: GDK X11 backend and scale)
from zone_i18n import _  # noqa: E402  (same place: interface strings and their translation)

# The strip places itself by X coordinates, so GDK has to be on the X11 backend —
# otherwise in a Wayland session `move()` does nothing (measurement: docs/RESEARCH-wayland-recon.md).
# The call comes before the first `load_gtk()`: after GDK initialisation the backend cannot be switched.
window_backend.prefer_x11_gdk()

VERSION = "0.3.0"

EXIT_OK = 0
EXIT_FAIL = 1

# How long to wait for the daemon answer to a click, seconds. The click is handled in the GTK
# main loop, so the wait is short: a hung daemon must not freeze
# strip redraws, and "there is no daemon" is visible from connect anyway.
DAEMON_TIMEOUT = 1.0

# The strip paints from the same tokens as the picker and the editor; `main` replaces this palette
# with the theme next to the config (an absent theme keeps exactly the colours the strip had).
THEME = zone_theme.default_theme()
ACCENT_H = 2

# GTK/cairo are pulled in lazily (`load_gtk`) so that without them one can tell
# "no library" from "no display" and tell the user exactly what is missing.
Gtk = Gdk = GLib = Pango = PangoCairo = cairo = None


def fail(message, code=EXIT_FAIL):
    print(message, file=sys.stderr)
    sys.exit(code)


def tabs_module():
    """The `zone_tabs` module: the click request lives only there (wave contract)."""
    import zone_tabs  # noqa: PLC0415 — lazy import, next to the strip

    return zone_tabs


# The tiler (`zone-tiler.py`) is needed by the strip only for the monitor scale: frames,
# captions and tab order come in the snapshot. It is loaded once and lazily: without
# X and GDK it cannot answer, and reloading it on every snapshot would mean
# reading xrandr and GDK again on every strip redraw.
_ZT = None
_ZT_LOADED = False


def tiler_module():
    """The tiler next to the strip — or ``None`` if it did not load.

    The neighbouring hyphen module is taken by its own path, not via ``sys.path``: the strip
    is installed both next to the sources and in ``/usr/lib/gridsnap``. A load failure is not
    a strip crash: without the tiler the scale cannot be asked for, and the strip behaves as
    it did before the DPI wave (scale 1).
    """
    global _ZT, _ZT_LOADED
    if not _ZT_LOADED:
        _ZT_LOADED = True
        try:
            spec = importlib.util.spec_from_file_location(
                "gridsnap_tabstrip_tiler", os.path.join(HERE, "zone-tiler.py")
            )
            if spec is None or spec.loader is None:
                raise ImportError(_("zone-tiler.py could not be loaded"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            _ZT = mod
        except Exception:  # no module, no its dependencies — the scale will be 1
            _ZT = None
    return _ZT


def strip_scale(point):
    """Monitor scale under a **physical** point; 1 — there is nobody to ask.

    The scale comes from the tiler (`zone-tiler.scale_at_point`): monitor work areas
    and GDK scales live there, and the strip keeps no monitor parsing
    of its own. If X or GDK is missing, the tiler may not answer at all — up to
    ``SystemExit`` from `monitors` (`fail`) — so it is caught here too: a failure
    to read the scale must not bring the strip down.
    """
    zt = tiler_module()
    if zt is None:
        return 1
    try:
        return zone_units.scale_value(zt.scale_at_point(point))
    except (Exception, SystemExit):
        return 1


def strip_center(rect):
    """Centre of the physical strip frame — the monitor scale is taken under it."""
    x, y, w, h = (int(round(float(v))) for v in rect)
    return (x + w // 2, y + h // 2)


def window_rect(rect, scale_of=strip_scale):
    """Physical strip frame from the snapshot → GTK window frame, in logical px.

    The daemon sends the strip frame in **physical** X pixels (those are the coordinates
    of the zone window), while GTK counts in **logical** ones: with ``GDK_SCALE=2`` a monitor
    1920x1080 looks like 960x540. Without the conversion the strip would move twice as far from the zone
    and grow twice as large — `set_size_request(1920, 22)` would order a 3840x44 window.

    The scale is asked under the centre of the strip itself (`scale_of`), not under the first
    monitor: the strip may sit on a screen with a different scale. After the conversion
    the sizes are whole numbers and at least 1 px: a window of zero size would show neither
    a caption nor a tab frame. The point and the sizes are counted in physical px, so
    the centre is taken before the conversion.
    """
    x, y, w, h = (int(round(float(v))) for v in rect)
    try:
        scale = zone_units.scale_value(scale_of(strip_center(rect)))
    except Exception:  # the scale was not asked — as before, at scale 1
        scale = 1
    lx, ly, lw, lh = zone_units.logical_rect((x, y, w, h), scale)
    return int(lx), int(ly), max(1, int(lw)), max(1, int(lh))


def local_rect(rect, origin, scale):
    """Tab frame from the snapshot (physical px) → local logical px of the window.

    The GTK window lives in logical pixels, and cairo in it is logical too: at
    scale 2 a physical offset inside the strip is twice as many
    logical pixels, so offsets and sizes are divided by the scale. The origin
    of coordinates is the top left corner of the strip (`origin`, the physical frame from the snapshot).
    """
    x, y, w, h = rect
    s = zone_units.scale_value(scale)
    return (x - origin[0]) / s, (y - origin[1]) / s, w / s, h / s


def local_point(point, origin, scale):
    """Click point from logical px of the window → physical px of the snapshot.

    The inverse of :func:`local_rect`: `nav.tab_under` and tab frames count in
    physical screen pixels, while the GTK event arrives in logical coordinates
    of the strip window.
    """
    s = zone_units.scale_value(scale)
    return origin[0] + point[0] * s, origin[1] + point[1] * s


def load_gtk():
    """Import GTK 3 and cairo; without them — a message on stderr and code 1."""
    global Gtk, Gdk, GLib, Pango, PangoCairo, cairo
    try:
        import cairo as cairo_module
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")

        from gi.repository import Gdk as gdk_module
        from gi.repository import GLib as glib_module
        from gi.repository import Gtk as gtk_module
        from gi.repository import Pango as pango_module
        from gi.repository import PangoCairo as pangocairo_module
    except (ImportError, ValueError) as exc:
        fail(
            _(
                "zone-tabstrip: GTK 3 for python3 is required (python3-gi and gir1.2-gtk-3.0) - without it the tab bar cannot be drawn: {}"
            ).format(exc),
            EXIT_FAIL,
        )
    Gtk, Gdk, GLib = gtk_module, gdk_module, glib_module
    Pango, PangoCairo, cairo = pango_module, pangocairo_module, cairo_module


def send_request(request, path=None, timeout=DAEMON_TIMEOUT):
    """Send a request to the daemon; `None` — there is no daemon or it did not answer.

    The socket is the same as `gridsnap-ctl` uses (`zone_ipc`): a tab click is an ordinary
    daemon command, not a new channel. Network errors are not a crash here: the strip window
    stays on screen, and the reason goes to the daemon stderr (its own stderr).
    """
    import zone_ipc  # noqa: PLC0415 — lazy import, next to the strip

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(path or zone_ipc.default_socket_path())
    except OSError as exc:
        conn.close()
        return None, _("the daemon does not answer: {}").format(exc)
    try:
        conn.sendall(zone_ipc.encode_message(request))
        return zone_ipc.read_response(conn), None
    except OSError as exc:
        return None, _("the daemon socket refused: {}").format(exc)
    except zone_ipc.ProtocolError as exc:
        return None, _("the daemon gave an unclear answer: {}").format(exc)
    finally:
        conn.close()


class Strip:
    """One strip window above a zone: override-redirect, without focus, with a click.

    The window is ``Gtk.WindowType.POPUP``: on X11 that is override-redirect, so
    muffin does not decorate it, does not put it in the task list and does not give it focus. The strip
    gets its size and place from the snapshot, not from the window manager, — that is how
    it stays glued to the top edge of the zone.
    """

    def __init__(self, zone, owner):
        self.zone = zone
        self.owner = owner
        self.record = None
        self.rows = []
        self.rect = None
        self.scale = 1
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_title(_("Gridsnap: tabs of zone {}").format(zone))
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        # Focus and keyboard belong to others: the zone window holds them, the strip only draws.
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_app_paintable(True)
        self.win.connect("draw", self.on_draw)
        self.win.connect("button-press-event", self.on_button_press)
        self.win.connect("destroy", self.on_destroy)
        self.shown = False

    # ---- snapshot

    def show_record(self, record):
        """Show (or update) the strip from a snapshot record; without a frame — remove it."""
        rect = nav.record_rect(record)
        if rect is None or self.win is None:
            self.destroy()
            return
        self.record = record
        self.rows = nav.layout(record)
        self.rect = rect
        # `self.rect` and tab frames are in physical X pixels, as in the snapshot;
        # the GTK window lives in logical ones, so both the window position and the local
        # drawing and click coordinates are converted by one and the same scale.
        # It is read once per snapshot: `scale_at_point` means GDK and xrandr.
        self.scale = zone_units.scale_value(strip_scale(strip_center(rect)))
        x, y, w, h = window_rect(rect, lambda _point: self.scale)
        # The size goes through `set_size_request`, not `resize`/`set_default_size`:
        # a POPUP window is born 200x200 and after `show_all` ignores resize
        # (checked under Xvfb: `xwininfo` shows 200 px in height). A strip
        # 200 px high would cover the zone window and eat its clicks.
        self.win.set_size_request(w, h)
        self.win.move(x, y)
        if not self.shown:
            self.win.show_all()
            self.shown = True
        self.win.queue_draw()

    def destroy(self):
        """Remove the strip window (the zone disappeared, the snapshot became empty, EOF). Idempotent."""
        win, self.win = self.win, None
        self.shown = False
        self.record, self.rows, self.rect, self.scale = None, [], None, 1
        if win is not None:
            win.destroy()

    def on_destroy(self, _widget):
        self.win = None
        self.owner.forget(self)

    # ---- click

    def on_button_press(self, _widget, event):
        """Click on a tab → a command to the daemon "show this window in this zone"."""
        if self.win is None or self.rect is None or event.button != 1:
            return False
        # The event arrives in logical coordinates of the strip window, while tab frames
        # and `nav.tab_under` are in physical screen ones: we convert with the strip scale
        # so that the hit is counted by the same code as in the tests.
        point = local_point((event.x, event.y), self.rect, self.scale)
        window = nav.tab_under(self.record, point)
        if window is None:
            return False
        request = tabs_module().tabs_switch_command(self.zone, window)
        response, error = send_request(request, self.owner.socket_path)
        if response is None:
            print(
                _("zone-tabstrip: tab not switched ({})").format(error),
                file=sys.stderr,
            )
        elif not response.get("ok", True):
            reason = response.get("error") or _("reason not given")
            print(_("zone-tabstrip: the daemon refused: {}").format(reason), file=sys.stderr)
        return True

    # ---- drawing

    def on_draw(self, widget, cr):
        if self.rect is None:
            return False
        alloc = widget.get_allocation()
        # OPERATOR_SOURCE: the background replaces the whole frame, without leftovers of the past one.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgb(*THEME.rgb("bg_panel"))
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        for row in self.rows:
            x, y, w, h = self.local_rect(row)
            cr.set_source_rgb(*(THEME.rgb("bg_hover") if row["active"] else THEME.rgb("bg_card")))
            cr.rectangle(x, y, w, h)
            cr.fill()
            if row["active"]:
                # The active tab is not marked by colour alone: an accent stripe on top.
                cr.set_source_rgb(*THEME.rgb("accent"))
                cr.rectangle(x, y, w, min(ACCENT_H / self.scale, h))
                cr.fill()
            self.draw_label(cr, row)
        return False

    def local_rect(self, row):
        """Tab frame in local logical px of the window (for cairo)."""
        return local_rect(row["rect"], self.rect, self.scale)

    def draw_label(self, cr, row):
        """Tab caption inside its frame: clipped by width, centred by height."""
        x, y, w, h = self.local_rect(row)
        pad = nav.TAB_PADDING / self.scale
        inner = max(1, int(w) - 2 * pad)
        layout = PangoCairo.create_layout(cr)
        layout.set_text(row["label"], -1)
        layout.set_font_description(Pango.FontDescription(THEME.font_desc(8)))
        # The caption is already clipped by the average character width (nav.fit_title); here
        # is the second, exact guard: Pango will put the ellipsis itself if the font is wider.
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_width(inner * Pango.SCALE)
        _lw, lh = layout.get_pixel_size()
        cr.set_source_rgb(*(THEME.rgb("fg") if row["active"] else THEME.rgb("fg_muted")))
        cr.save()
        cr.rectangle(x + pad, y, inner, h)
        cr.clip()
        cr.move_to(x + pad, y + max(0.0, (h - lh) / 2.0))
        PangoCairo.show_layout(cr, layout)
        cr.restore()


class Tabstrip:
    """Strips of all zones: one process, one strip window per snapshot record."""

    def __init__(self, socket_path=None):
        self.socket_path = socket_path
        self.strips = {}  # zone name -> Strip
        self.buf = ""

    def apply(self, records):
        """Bring the strip windows in line with the snapshot: new ones — create, gone ones — remove.

        The snapshot is full, so "the zone disappeared" is its absence from the snapshot, not
        a separate message. The order of records is the order of zones in the config; on screen
        it affects nothing, the windows are independent.
        """
        seen = set()
        for record in records:
            name = str(record.get("zone") or "")
            seen.add(name)
            strip = self.strips.get(name)
            if strip is None or strip.win is None:
                strip = Strip(name, self)
                self.strips[name] = strip
            strip.show_record(record)
        for name in [key for key in self.strips if key not in seen]:
            self.strips.pop(name).destroy()

    def feed(self, data):
        """Feed a chunk of stdin: parse whole lines and apply the snapshots.

        An incomplete line waits for the rest, a garbage one is skipped: the strip must not
        disappear because of one broken line (``nav.parse_snapshot`` will return
        ``None``), and an empty snapshot ``{"strips": []}`` is a legitimate command to remove
        all strips.
        """
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        self.buf += str(data)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            records = nav.parse_snapshot(line)
            if records is not None:
                self.apply(records)

    def forget(self, strip):
        """Remove a strip from the bookkeeping (its window was destroyed from outside)."""
        if self.strips.get(strip.zone) is strip:
            self.strips.pop(strip.zone, None)

    def close(self):
        """Remove all strip windows (EOF on stdin, a signal, the end of work)."""
        for strip in list(self.strips.values()):
            strip.destroy()
        self.strips.clear()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=_("Tab bar over a zone (Cinnamon/X11)"),
    )
    ap.add_argument(
        "--conf",
        default=None,
        help=_(
            "path to zones.conf: this is how the daemon starts the bar; the bar itself does not read the config - frames and captions come in the snapshot"
        ),
    )
    ap.add_argument(
        "--socket",
        default=None,
        help=_("daemon socket path (the same as gridsnap-ctl uses by default)"),
    )
    ap.add_argument("--version", action="version", version=f"zone-tabstrip {VERSION}")
    args = ap.parse_args(argv)

    # The palette next to the config; the daemon passes `--conf` when it starts the strip.
    global THEME
    THEME = zone_theme.load(args.conf)

    load_gtk()
    try:
        ready = Gtk.init_check()[0]
    except Exception as exc:  # GTK is there but did not start (no display, etc.)
        fail(_("zone-tabstrip: GTK failed to start: {}").format(exc), EXIT_FAIL)
    if not ready:
        fail(
            _("zone-tabstrip: no display - the tab bar needs X11 (DISPLAY)"),
            EXIT_FAIL,
        )

    strips = Tabstrip(args.socket)

    def on_readable(fd, _condition):
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            print(_("zone-tabstrip: cannot read stdin: {}").format(exc), file=sys.stderr)
            chunk = b""
        if not chunk:  # EOF or HUP: the daemon closed the stream, there is nobody left to feed
            strips.close()
            Gtk.main_quit()
            return False
        strips.feed(chunk)
        return True

    def on_signal():
        strips.close()
        Gtk.main_quit()
        return False

    GLib.io_add_watch(
        0,
        GLib.PRIORITY_DEFAULT,
        GLib.IOCondition.IN | GLib.IOCondition.HUP,
        on_readable,
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, on_signal)

    try:
        Gtk.main()
    finally:
        strips.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

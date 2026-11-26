#!/usr/bin/env python3
"""zone-picker.py — a visual zone picker for GridSnap (like gTile).

On a hotkey (in Cinnamon it is bound to ``Super+Z``, see README) an overlay
with the zones from ``zones.conf`` appears over the desktop. The zone under
the cursor and the selection are highlighted; the arrows move the selection, ``Enter``
places the active window into the chosen zone, ``Esc`` cancels.

    python3 zone-picker.py                 # the interactive overlay
    python3 zone-picker.py --shot out.png  # render the overlay into a PNG and exit
    python3 zone-picker.py --dry-run       # pick a zone but do not move the window
    python3 zone-picker.py --workspace 2   # the grid of desktop 2 (the profile from [workspaces])
    python3 zone-picker.py --hud --at 100,100        # HUD grid: the zone under the point, then exit
    python3 zone-picker.py --hud --follow            # HUD grid: "X Y" points from stdin

The ``--hud`` mode is a passive grid for dragging a window: the outlines of
all zones are highlighted, and so is the zone under the point the client (the drag
watcher) sends. The HUD takes no focus, does not grab the keyboard and is transparent
for input: during a drag muffin keeps the input and we only draw.

The grid is taken for the screen of the window/cursor by the same resolver as in the tiler
(``zone-tiler.grid_for_output``): the ``[monitors]`` profile → the ``[workspaces]`` profile
→ the active ``zones.conf``. Thus the overlay shows exactly the zones the daemon/``apply`` will
place, not the set of the active config; inside a grid the monitor can narrow the set with
its own ``[zones:<output>]``. A screen without a binding lives by the grid of its own
workspace, as before.

The pure navigation logic lives in ``zone_picker_nav`` (no GTK and no X), window
placement goes through ``zone-tiler.apply_zone``/``Grid.rect``. GTK and cairo live only here.

Two pixel spaces (see ``zone_units``): zone arithmetic, ``monitors()``,
the ``--at`` point and the stdin lines of ``--follow`` are **physical** X pixels, while
everything the GTK window sets (``move``/``set_default_size``) and the GDK pointer
reports is **logical**. Only ``zone_units`` converts one way into the other; here it is
called in three places: the frame of the overlay/HUD window, the first selection under
the cursor and the monitor under the cursor.
"""

import argparse
import math
import os
import signal
import sys

import cairo

# The X11 GDK backend is enabled before the first call into GDK (see `window_backend`):
# in a Wayland session the overlay and the HUD must live in Xwayland coordinates, otherwise
# `move()` becomes a no-op and another ruler counts the window size.
import window_backend

window_backend.prefer_x11_gdk()

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402  # type: ignore

import zone_config as zc  # noqa: E402
import zone_picker_nav as nav  # noqa: E402
import zone_units as zu  # noqa: E402  (converts GTK logical pixels to physical X ones)
import zone_theme  # noqa: E402  (one palette for every surface: theme.conf next to zones.conf)
from zone_i18n import _  # noqa: E402  (the same place: interface strings and their translation)

zt = zc.zt  # zone-tiler.py loaded by zone_config (the config parser and the layout)

# The palette of the grid, the backdrop and the accent; `main` replaces it with the theme that
# sits next to the config (an absent theme keeps exactly these colours).
THEME = zone_theme.default_theme()
HINT = _("arrows - select · Enter - place · Esc - cancel")
# The HUD backdrop and the pause of the one-shot draw: the grid is visible and so is
# the desktop, while the process itself does not hang after the frame.
HUD_ALPHA = 0.25
HUD_HOLD_MS = 500

EXIT_OK = 0
EXIT_FAIL = 1


def fail(message, code=EXIT_FAIL):
    print(message, file=sys.stderr)
    sys.exit(code)


# ----------------------------------------------------------- workspace grid


def zone_bindings(grid):
    """Zone bindings from the ``[keys]`` of the grid: zone name -> key combination.

    ``[keys]`` lives in the same file as the zone set, so a workspace profile
    brings its own combinations too. No section — no bindings: the zone label simply
    shows no key.
    """
    if not grid.conf.has_section("keys"):
        return {}
    return {name.strip(): value.strip() for name, value in grid.conf.items("keys")}


def zones_from_grid(grid, output=None):
    """The zones of a monitor's set from an already chosen grid (with the ``[keys]`` bindings).

    Names and fractions come from one and the same ``Grid``: a monitor may have its own
    ``[zones:<output>]`` set, a workspace has its own profile, and the overlay must
    draw exactly what ``list`` prints. Reading the file separately will not do here:
    the zone names would diverge from the geometry the picker moves the window by.
    """
    keys = zone_bindings(grid)
    zones = []
    for name in grid.zones_for(output):
        x, y, w, h = grid.fractions(name, output)
        zones.append(zc.Zone(name, x, y, w, h, keys.get(name, "")))
    return zones


def screen_grid(conf, output, workspace):
    """The grid of a screen and the profiles that gave it: ``(Grid, screen, workspace)``.

    The resolution order is not duplicated but taken from the tiler
    (:func:`zone-tiler.grid_for_output`): ``[monitors]`` → ``[workspaces]`` →
    the active ``zones.conf``. The workspace grid (``grid_for_workspace``) comes as a
    ready-made ``fallback``: a screen without a binding lives by it, as it lived before
    profiles were bound to screens. With no screen profile on disk, ``grid_for_output``
    prints a warning to stderr by itself and returns the workspace grid — the overlay does not refuse.

    ``screen`` is the name of the screen profile if it really gave the grid, otherwise
    ``None``; ``desk`` is the workspace profile from ``[workspaces]``. The captions are built
    from them: without the "screen …" and "workspace …" split a caption would lie about the origin.
    """
    desk = zt.workspace_profile(conf, workspace)
    grid, screen = zt.grid_for_output(
        conf,
        output,
        fallback=zt.grid_for_workspace(conf, workspace),
        workspace=workspace,
    )
    return grid, screen, desk


def workspace_caption(workspace, profile):
    """The caption of a grid: which workspace gave it and why the zone set is such.

    Empty if the workspace is unknown (no ``_NET_CURRENT_DESKTOP``): then the common
    ``[zones]`` works and there is nothing to caption.
    """
    if workspace == zt.WORKSPACE_CONF:
        return _("zones.conf, without workspace profiles")
    if not isinstance(workspace, int):
        return ""
    if profile:
        return _("workspace {} · profile \"{}\"").format(workspace + 1, profile)
    return _("workspace {} · default set").format(workspace + 1)


def screen_caption(workspace, profile, output, screen_profile):
    """The caption of a grid: the screen profile if it gave the grid, else the workspace caption.

    A screen beats a workspace (:func:`zone_config.resolve_profile`), so the caption
    names the screen first: by it ``--shot``/``--dry-run`` show that the overlay drew
    the zones of the same profile the tiler will place on this screen.
    """
    if screen_profile:
        return _("monitor {} · profile \"{}\"").format(output, screen_profile)
    return workspace_caption(workspace, profile)


def hud_caption(profile, output=None, screen_profile=None):
    """The HUD caption: whose profile gave the grid — a screen's or a workspace's.

    The HUD has no key hint (it does not listen to keys), so the corner keeps
    only the answer to "which grid is this" — the same profile line as in the
    interactive overlay, but without the workspace number: there is nothing more to draw.
    """
    if screen_profile:
        return _("monitor {} · profile \"{}\"").format(output, screen_profile)
    return _("profile \"{}\"").format(profile) if profile else ""


def describe_grid(zones, caption):
    """A line about the grid shown: the workspace caption and the list of zones.

    Needed by ``--dry-run`` and ``--shot``: by it one sees which grid is drawn without
    looking at the overlay (that is what ``tests/smoke-picker-workspace.sh`` checks).
    """
    if caption:
        return _("{caption} · zones {count}: {names}").format(
            caption=caption, count=len(zones), names=", ".join(z.name for z in zones)
        )
    return _("zones {count}: {names}").format(
        count=len(zones), names=", ".join(z.name for z in zones)
    )


# ---------------------------------------------------------------------- drawing


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _label(cr, x, y, text, size, bold, rgba, width=None):
    layout = PangoCairo.create_layout(cr)
    layout.set_text(text, -1)
    layout.set_font_description(Pango.FontDescription(THEME.font_desc(size, bold)))
    if width:
        layout.set_width(int(max(1, width) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def draw_zones(cr, width, height, zones, selected, caption="", hint=HINT):
    """Draw a grid without a backdrop: zones, labels, highlight, caption and hint.

    Coordinates are in pixels of the local overlay (which is also the monitor work
    area), zones are given in fractions, so ``zone_px`` simply multiplies them by the
    sizes. The backdrop is drawn by the caller: the interactive overlay needs an opaque
    one, the HUD a translucent one. An empty ``hint`` means no key hint (the HUD does not listen to keys).
    """
    # large zones first, small ones on top — as in the editor
    order = sorted(range(len(zones)), key=lambda i: zones[i].w * zones[i].h, reverse=True)
    for i in order:
        z = zones[i]
        x, y, w, h = z.x * width, z.y * height, z.w * width, z.h * height
        col = THEME.zone_rgb(i)
        _rounded(cr, x + 2, y + 2, max(1, w - 4), max(1, h - 4), THEME.radius)
        cr.set_source_rgba(col[0], col[1], col[2], 0.20)
        cr.fill_preserve()
        cr.set_line_width(1.4)
        cr.set_source_rgba(col[0], col[1], col[2], 0.75)
        cr.stroke()

        cr.save()
        cr.rectangle(x + 3, y + 3, max(1, w - 6), max(1, h - 6))
        cr.clip()
        pad = 10
        if w > 40 and h > 24:
            _label(cr, x + pad, y + pad - 3, z.name, 12, True, (1, 1, 1, 0.94), w - 2 * pad)
        if w > 40 and h > 48:
            sub = f"{z.w:.0%} × {z.h:.0%}"
            if getattr(z, "binding", ""):
                sub += f" · {z.binding}"
            _label(cr, x + pad, y + pad + 15, sub, 9, False, (1, 1, 1, 0.62), w - 2 * pad)
        cr.restore()

    if selected is not None and 0 <= selected < len(zones):
        z = zones[selected]
        x, y, w, h = z.x * width, z.y * height, z.w * width, z.h * height
        _rounded(cr, x + 2, y + 2, max(1, w - 4), max(1, h - 4), THEME.radius)
        cr.set_source_rgba(*THEME.rgb("accent"), 0.16)
        cr.fill_preserve()
        cr.set_line_width(3.0)
        cr.set_source_rgba(*THEME.rgb("accent"), 1.0)
        cr.stroke()

    # why the grid is such: the workspace and its profile from `[workspaces]`
    if caption:
        _label(cr, 16, height - (48 if hint else 30), caption, 11, True, (*THEME.rgb("accent"), 0.92))
    if hint:
        _label(cr, 16, height - 30, hint, 11, False, (1, 1, 1, 0.72))


def draw_overlay(cr, width, height, zones, selected, caption=""):
    """The whole interactive overlay: an opaque backdrop and the grid on top of it."""
    cr.set_source_rgba(*THEME.rgb("bg"), 0.90)
    cr.paint()
    draw_zones(cr, width, height, zones, selected, caption)


def write_shot(path, width, height, zones, selected, caption="", hint=HINT):
    """Render the overlay (or the HUD if ``hint`` is empty) into a PNG — a check without interaction."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*THEME.rgb("bg"))  # an opaque backdrop so that the snapshot reads
    cr.paint()
    draw_zones(cr, width, height, zones, selected, caption, hint)
    surface.write_to_png(path)


# --------------------------------------------------- picking the monitor and window


def active_window_quiet():
    """The active window as an id, or None if there is none/X is unavailable (no SystemExit)."""
    try:
        return zt.active_window()
    except SystemExit:
        return None


def pointer_point():
    """The absolute position of the cursor (x, y), or None if it cannot be asked."""
    try:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pos = seat.get_pointer().get_position()
        return (int(pos[-2]), int(pos[-1]))
    except Exception:
        return None


def logical_areas(monitors, scales):
    """The monitor work areas in GTK logical pixels, in the same order.

    ``monitors()`` is physical now, and GTK windows live in logical pixels,
    so the overlay and HUD windows need this same area, converted
    (:func:`zone_units.logical_rect`). The list of scales is shorter than the list of areas —
    a common case without GDK: ``zone_units.scale_at`` gives 1, and the area does not change.
    """
    return [zu.logical_rect(wa, zu.scale_at(scales, i)) for i, wa in enumerate(monitors)]


def pointer_physical(point, monitors, scales):
    """A GDK logical point → a physical X point, or ``None``.

    The GDK pointer arrives in logical pixels (``pointer_point``), while the monitors,
    ``--at`` and the stdin lines are in physical ones. The monitor is looked up by logical
    areas, not by one common coefficient: monitors may have their own scale, and a common
    number would lie on a neighbouring screen.
    """
    if point is None:
        return None
    areas = logical_areas(monitors, scales)
    index = areas.index(zt.monitor_for_point(areas, point))
    return zu.physical_point(point, zu.scale_at(scales, index))


def pick_monitor(monitors, scales, win_id, args):
    """The monitor index: an explicit ``--monitor``, else the window monitor, else under the cursor.

    GDK reports the cursor in logical pixels while ``monitors`` are physical, so the
    monitor under the cursor is looked up by logical areas (:func:`logical_areas`).
    The window monitor — the other way round, by the physical window frame (``window_monitor``).
    """
    if args.monitor is not None:
        return args.monitor
    if win_id is not None:
        wa = zt.window_monitor(win_id, monitors)
        if wa is not None:
            return monitors.index(wa)
    point = pointer_point()
    if point is not None:
        areas = logical_areas(monitors, scales)
        return areas.index(zt.monitor_for_point(areas, point))
    return 0


def select_by_name(zones, name):
    """The index of the zone named ``name``; the first one if there is none."""
    if name:
        for i, z in enumerate(zones):
            if z.name == name:
                return i
    return nav.first_index(zones)


# -------------------------------------------------------- the interactive overlay


class Picker:
    def __init__(
        self, zones, grid, wa, output, mon_index, args, win_id, caption="", scale=1
    ):
        self.zones = zones
        self.grid = grid
        self.wa = wa
        self.output = output  # the output name: a monitor may have its own zone set
        self.mon_index = mon_index
        self.scale = zu.scale_value(scale)  # the monitor scale: wa is physical
        self.args = args
        self.win_id = win_id
        self.caption = caption
        self.exit_code = EXIT_OK

        width, height = wa[2], wa[3]
        self.selected = self._initial_selection(width, height)
        self._seat = None
        self._grab_tries = 0

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        title = _("Gridsnap - zone picker")
        if caption:
            title += f" ({caption})"
        self.win.set_title(title)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(True)
        self.win.set_focus_on_map(True)
        self.win.set_app_paintable(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        # A GTK window is in logical pixels: `move`/`set_default_size` would double a physical
        # frame at scale 2, and the overlay would move to the neighbouring screen.
        win_x, win_y, win_w, win_h = zu.logical_rect(wa, self.scale)
        self.win.move(win_x, win_y)
        self.win.set_default_size(win_w, win_h)
        self.win.add_events(
            Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        self.win.connect("draw", self.on_draw)
        self.win.connect("key-press-event", self.on_key)
        self.win.connect("motion-notify-event", self.on_motion)
        self.win.connect("button-press-event", self.on_click)
        self.win.connect("destroy", self.on_destroy)
        self.win.show_all()
        self.win.present()
        GLib.timeout_add(80, self.try_grab)

    # ---- state

    def _initial_selection(self, width, height):
        """The zone under the cursor; ``width``/``height`` are the physical sizes of the area.

        The GDK cursor is logical while ``width``/``height`` and ``wa`` are physical: without
        the conversion, (0,1) would come out twice as close to the top left corner at scale 2
        instead of the middle of the screen, and the first selection would slip from the cursor.
        """
        if self.args.select:
            return select_by_name(self.zones, self.args.select)
        point = pointer_point()
        if point is not None:
            px, py = zu.physical_point(point, self.scale)
            fx = (px - self.wa[0]) / float(max(1, width))
            fy = (py - self.wa[1]) / float(max(1, height))
            under = nav.zone_at(self.zones, fx, fy)
            if under is not None:
                return under
        return nav.first_index(self.zones)

    # ---- the keyboard

    def try_grab(self):
        """Take the keyboard for the overlay (the WM gives no focus to a DOCK window)."""
        self._grab_tries += 1
        try:
            display = Gdk.Display.get_default()
            seat = display.get_default_seat()
            status = seat.grab(self.win.get_window(), Gdk.SeatCapabilities.KEYBOARD, True)
            if status == Gdk.GrabStatus.SUCCESS:
                self._seat = seat
                return False
        except Exception as exc:  # the window may not be ready — try again
            if self._grab_tries >= 10:
                print(_("zone-picker: failed to grab the keyboard: {}").format(exc), file=sys.stderr)
        return self._grab_tries < 10

    def release_grab(self):
        try:
            if self._seat is not None:
                self._seat.ungrab()
        except Exception as exc:
            print(_("zone-picker: failed to release the keyboard: {}").format(exc), file=sys.stderr)
        self._seat = None

    def on_key(self, _widget, ev):
        key = ev.keyval
        direction = {
            Gdk.KEY_Left: "left",
            Gdk.KEY_Right: "right",
            Gdk.KEY_Up: "up",
            Gdk.KEY_Down: "down",
        }.get(key)
        if direction is not None:
            self.selected = nav.move_selection(self.zones, self.selected, direction)
            self.win.queue_draw()
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.quit(apply=True)
            return True
        if key == Gdk.KEY_Escape:
            self.quit(apply=False)
            return True
        return False

    # ---- the mouse

    def on_motion(self, widget, ev):
        alloc = widget.get_allocation()
        fx = ev.x / float(max(1, alloc.width))
        fy = ev.y / float(max(1, alloc.height))
        under = nav.zone_at(self.zones, fx, fy)
        if under is not None and under != self.selected:
            self.selected = under
            widget.queue_draw()
        return False

    def on_click(self, _widget, ev):
        if getattr(ev, "button", 1) == 1:
            self.quit(apply=True)
            return True
        if getattr(ev, "button", 0) == 3:
            self.quit(apply=False)
            return True
        return False

    def on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        draw_overlay(cr, alloc.width, alloc.height, self.zones, self.selected, self.caption)
        return False

    def on_destroy(self, _widget):
        Gtk.main_quit()

    # ---- applying and exiting

    def apply_selected(self):
        if self.selected is None:
            return
        name = self.zones[self.selected].name
        # geometry from the same set as the drawn zones (the workspace monitor)
        rect = self.grid.rect(name, self.wa, self.output)
        x, y, w, h = rect
        if self.args.dry_run:
            print(
                _("{}: x={} y={} {}x{} (monitor {})").format(
                    name, x, y, w, h, self.mon_index
                )
            )
            return
        win = self.win_id if self.win_id is not None else active_window_quiet()
        if win is None:
            print(_("zone-picker: no active window"), file=sys.stderr)
            self.exit_code = EXIT_FAIL
            return
        if zt.window_is_excluded(win, self.grid.ignore):
            print(
                _(
                    "zone-picker: window 0x{:x} is excluded ([ignore]/utility window type) - not moving it"
                ).format(win),
                file=sys.stderr,
            )
            self.exit_code = EXIT_FAIL
            return
        before = None
        try:
            before = zt.snapshot_window(win, zone=name)
        except Exception:
            before = None  # the geometry cannot be read — no history record
        zt.apply_zone(win, rect, label=name, verify=self.args.verify, quiet=self.args.quiet)
        if before is not None:
            zt.remember("apply", [before], quiet=self.args.quiet)

    def quit(self, apply=False):
        if apply:
            self.apply_selected()
        self.release_grab()
        self.win.destroy()
        Gtk.main_quit()


# ------------------------------------------------------------------- HUD overlay


def parse_point(text):
    """A point ``(x, y)`` from a ``X,Y`` or ``X Y`` string; ``None`` if it is not a pair.

    One parser for two inputs: ``--at X,Y`` and the ``X Y`` lines on stdin of ``--follow``.
    The drag watcher prints the cursor position as a string, hence
    the space-separated form is the main one and the comma is a convenience for a manual run.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not text:
        return None
    parts = str(text).replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0], 10), int(parts[1], 10)
    except ValueError:
        return None


def point_spec(text):
    """``--at X,Y``: a point, or a clear argument error (code 2)."""
    point = parse_point(text)
    if point is None:
        raise argparse.ArgumentTypeError(
            _("screen coordinates X,Y are required, not {!r}").format(text)
        )
    return point


class Hud:
    """A passive grid overlay for dragging: it draws but takes no input.

    It differs from :class:`Picker` in three places: the window takes no focus
    (``set_accept_focus(False)``/``set_focus_on_map(False)``), the keyboard is not
    grabbed (there is no ``Gdk.Seat.grab`` — muffin holds it) and the window is
    transparent for input, while the highlight point is sent by the client (``--at`` or stdin).
    The backdrop is translucent so that the grid does not cover the window being dragged.
    """

    def __init__(self, zones, wa, caption="", scale=1):
        self.zones = zones
        # The monitor work area is in physical X pixels: both the points from the watcher
        # (`set_point`) and the `--shot` snapshot come in them.
        self.wa = wa
        self.scale = zu.scale_value(scale)  # the monitor scale: the window is logical
        self.caption = caption
        self.selected = None
        self._buf = ""
        self.input_mode = _("none")

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        title = _("Gridsnap - grid")
        if caption:
            title += f" ({caption})"
        self.win.set_title(title)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        # Focus and keyboard belong to others: muffin owns the drag.
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_app_paintable(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        # A GTK window is in logical pixels, `wa` is physical: a physical size
        # in `set_default_size` would give a window twice the monitor.
        win_x, win_y, win_w, win_h = zu.logical_rect(wa, self.scale)
        self.win.move(win_x, win_y)
        self.win.set_default_size(win_w, win_h)
        # Without an RGBA visual the translucent backdrop is not composited and turns black.
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual() if screen is not None else None
        if visual is not None and screen.is_composited():
            self.win.set_visual(visual)
        self.win.connect("draw", self.on_draw)
        self.win.connect("destroy", lambda _widget: Gtk.main_quit())
        # First onto the screen, then the input shape: the input shape is set on a live
        # window (and GDK does not overwrite it on mapping).
        self.win.show_all()
        self.input_mode = self.make_input_transparent()

    # ---- the input is not ours

    def make_input_transparent(self):
        """Make the window transparent for input: X events pass it by.

        Not everything the name promises works: it was checked under Xvfb that
        ``Gdk.Window.set_pass_through(True)`` only sets the GDK flag
        (``get_pass_through()`` — True) while the X input shape stays full and the window
        keeps catching events. That is why an empty input shape is set first
        (``input_shape_combine_region`` with an empty ``cairo.Region``) — it is the only
        mechanism the X server sees (the SHAPE extension) — and only then, on top, the
        ``set_pass_through`` flag: it tells GDK itself
        to look for the window under the cursor further down. Without SHAPE at least that one remains.
        Returns what worked: GDK does not let one ask the input shape back.
        """
        gdk_win = self.win.get_window()
        if gdk_win is None:
            print(_("zone-picker: the HUD has no Gdk window - input cannot be disabled"), file=sys.stderr)
            return _("no window")
        used = []
        try:
            gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)
            used.append("input_shape_combine_region")
        except Exception as exc:  # an X server without SHAPE
            print(_("zone-picker: failed to set the input shape: {}").format(exc), file=sys.stderr)
        try:
            gdk_win.set_pass_through(True)
            used.append("set_pass_through")
        except Exception as exc:  # an old GTK
            print(_("zone-picker: set_pass_through did not work: {}").format(exc), file=sys.stderr)
        return " + ".join(used) if used else _("none")

    # ---- a point from outside

    def set_point(self, point):
        """Highlight the zone under a screen point (``None`` clears the highlight)."""
        index = None if point is None else nav.zone_at_point(self.zones, self.wa, point)
        if index != self.selected:
            self.selected = index
            self.win.queue_draw()
        return index

    def apply_point(self, point):
        """Highlight the point and say on stdout what exactly is highlighted.

        The line is for the watcher and the smoke test: by it one sees that the process is alive
        and the point arrived without looking at the window.
        """
        index = self.set_point(point)
        name = self.zones[index].name if index is not None else _("off the zones")
        print(f"zone-picker: {point[0]},{point[1]} -> {name}", flush=True)
        return index

    def feed(self, data):
        """Feed a chunk of stdin: parse whole ``X Y`` lines and redraw.

        An incomplete line waits for more (the watcher may write in chunks),
        garbage lines are skipped — the highlight does not blink on them.
        """
        self._buf += data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            point = parse_point(line)
            if point is not None:
                self.apply_point(point)

    # ---- drawing

    def on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        # OPERATOR_SOURCE: the backdrop replaces the window contents entirely. With a compositor
        # the desktop shows through HUD_ALPHA, without one — an even dark backdrop
        # rather than the leftovers of the previous frame.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(*THEME.rgb("bg"), HUD_ALPHA)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        # No key hint: the HUD does not listen to keys.
        draw_zones(cr, alloc.width, alloc.height, self.zones, self.selected, self.caption, hint="")
        return False


def follow_stdin(hud):
    """Feed the HUD points from stdin until EOF; the window lives in the GTK main loop.

    No timers that move the window or take input: the only source of
    new points is the ``X Y`` lines from the watcher, and the end of the stream (EOF, SIGTERM)
    finishes the main loop. Restarting the process for every frame would cause flicker.
    """

    def on_readable(fd, _condition):
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            print(_("zone-picker: cannot read stdin: {}").format(exc), file=sys.stderr)
            chunk = b""
        if not chunk:  # EOF or HUP: there is no one left to feed
            Gtk.main_quit()
            return False
        hud.feed(chunk)
        return True

    GLib.io_add_watch(
        0, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, on_readable
    )

    def on_quit(_signum=None):
        Gtk.main_quit()
        return False

    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, on_quit)


# ------------------------------------------------------------------------- CLI


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="zone-picker.py",
        description=_(
            "Visual zone picker overlay: arrows, Enter - place, Esc - cancel."
        ),
    )
    ap.add_argument(
        "--conf",
        metavar="PATH",
        help=_("zone config (default ~/.config/gridsnap/zones.conf)"),
    )
    ap.add_argument(
        "--shot",
        metavar="FILE.png",
        help=_("render the overlay to PNG and exit (non-interactive)"),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=_("do not move the window, only show the selected zone"),
    )
    ap.add_argument(
        "--window",
        type=lambda s: int(s, 0),
        metavar="XID",
        help=_("window to place (the active one by default)"),
    )
    ap.add_argument(
        "--monitor",
        type=int,
        metavar="N",
        help=_("monitor (the active window's monitor by default)"),
    )
    ap.add_argument(
        "--select", metavar=_("ZONE"), help=_("initial selection by zone name")
    )
    ap.add_argument(
        "--workspace",
        metavar="N|current|conf",
        default=None,
        type=zt.workspace_spec,
        help=_(
            "workspace for the grid: number (EWMH, zero-based), current (the active workspace's grid - default) or conf (always zones.conf, without workspace profiles)"
        ),
    )
    ap.add_argument(
        "--verify", action="store_true", help=_("verify the window frame after placing")
    )
    ap.add_argument(
        "--quiet", action="store_true", help=_("do not print the placement line")
    )
    ap.add_argument(
        "--hud",
        action="store_true",
        help=_(
            "passive grid with the zone under the pointer highlighted (for dragging: no focus or input)"
        ),
    )
    ap.add_argument(
        "--at",
        metavar="X,Y",
        type=point_spec,
        help=_("--hud: highlight point in screen coordinates (one frame and exit)"),
    )
    ap.add_argument(
        "--follow",
        action="store_true",
        help=_(
            "--hud: read \"X Y\" points from stdin and redraw the highlight (EOF/SIGTERM - exit)"
        ),
    )
    ap.add_argument("--version", action="version", version=f"zone-picker {zt.VERSION}")
    args = ap.parse_args(argv)
    validate_args(ap, args)
    return args


# Interactive-mode flags: in the HUD they make no sense — the HUD selects nothing,
# places nothing and prints nothing about placing (--quiet only keeps silent, so it is harmless).
HUD_INCOMPATIBLE = ("select", "window", "dry_run", "verify")


def validate_args(ap, args):
    """Check the combinations of modes; a bad one is an argument error (code 2).

    ``--hud`` is a separate passive mode: it does not move windows (hence there is no
    ``--select``/``--window``/``--dry-run``/``--verify``) and does not work without
    a point source. ``ap.error`` prints usage and exits with code 2 — the same
    contract as the other parse errors.
    """
    if not args.hud:
        if args.at is not None or args.follow:
            ap.error(_("zone-picker: --at and --follow only work together with --hud"))
        return
    bad = [name for name in HUD_INCOMPATIBLE if getattr(args, name)]
    if bad:
        flags = ", ".join("--" + name.replace("_", "-") for name in bad)
        ap.error(
            _("zone-picker: --hud is incompatible with {}: the HUD only draws the grid").format(
                flags
            )
        )
    if args.at is not None and args.follow:
        ap.error(
            _(
                "zone-picker: --at and --follow are mutually exclusive: --at - one frame, --follow - a stream"
            )
        )
    if args.at is None and not args.follow:
        ap.error(_("zone-picker: --hud needs a point source: --at X,Y or --follow"))
    if args.follow and args.shot:
        ap.error(
            _(
                "zone-picker: --shot does not work with --follow: a snapshot is one frame (--at)"
            )
        )


def main(argv=None):
    args = parse_args(argv)
    conf = args.conf or zt.default_conf()
    if not os.path.exists(conf):
        fail(_("zone-picker: config not found: {}").format(conf))
    # The palette next to the config: an absent or broken theme keeps the colours above.
    global THEME
    THEME = zone_theme.load(conf)
    # The workspace for resolving profiles: `--workspace N`, otherwise the active one. The grid
    # itself is computed after the monitor is picked — it depends on the screen (grid_for_output),
    # and not only on the workspace. A workspace without a profile silently lives in the common [zones]
    # (the same degradation as in zone-tiler.grid_for_workspace), so the workspace caption
    # and the grid shown do not diverge.
    workspace = zt.resolve_workspace(args.workspace)

    try:
        Gtk.init()
    except Exception as exc:
        fail(_("zone-picker: failed to open the display: {}").format(exc))
    # The output names are needed for the monitor zone set `[zones:<output>]`; the order
    # of the areas is the same as in zt.monitors() — it is what `--monitor` is numbered by.
    outputs = zt.monitor_outputs()
    monitors = [wa for _unused, wa in outputs]
    if not monitors:
        fail(_("zone-picker: failed to get monitor work areas"))
    # The scales are in the same order as the areas (an empty list without GDK = scale 1).
    scales = zt.monitor_scales()
    win_id = args.window
    if win_id is None and not args.shot and not args.hud:
        win_id = active_window_quiet()
    if args.hud and args.at is not None and args.monitor is None:
        # Without --monitor the HUD lives on the monitor of the received point, not under the cursor:
        # the client sets the point, and it is on its monitor that the highlight must go.
        mon_index = monitors.index(zt.monitor_for_point(monitors, args.at))
    else:
        mon_index = pick_monitor(monitors, scales, win_id, args)
    if not 0 <= mon_index < len(monitors):
        fail(_("zone-picker: no monitor {}").format(mon_index))
    wa = monitors[mon_index]
    output = outputs[mon_index][0]
    # The scale of the picked monitor: one for the overlay window and the HUD window.
    scale = zu.scale_at(scales, mon_index)
    # The screen grid — by the same resolver as in the tiler: otherwise the overlay would show
    # the zones of the workspace profile while the daemon would place the window into the zones of the screen profile.
    grid, screen_profile, desk_profile = screen_grid(conf, output, workspace)
    caption = screen_caption(workspace, desk_profile, output, screen_profile)
    hud_heading = hud_caption(desk_profile, output, screen_profile)
    zones = zones_from_grid(grid, output)
    if not zones:
        fail(_("zone-picker: the config has no zones for monitor {}").format(output))

    if args.shot:
        if args.hud:
            # A HUD snapshot is the same PNG, only without the hint and with the point highlighted:
            # it is what shows that the highlight works (different points — different files).
            selected = nav.zone_at_point(zones, wa, args.at)
            write_shot(
                args.shot,
                wa[2],
                wa[3],
                zones,
                selected,
                hud_heading,
                hint="",
            )
            point = _("off the zones") if selected is None else zones[selected].name
            print(
                _(
                    "zone-picker: HUD snapshot written to {} ({}x{}), point {},{} -> {} - {}"
                ).format(
                    args.shot,
                    wa[2],
                    wa[3],
                    args.at[0],
                    args.at[1],
                    point,
                    describe_grid(zones, caption),
                )
            )
            return EXIT_OK
        selected = select_by_name(zones, args.select)
        write_shot(args.shot, wa[2], wa[3], zones, selected, caption)
        print(
            _("zone-picker: overlay snapshot written to {} ({}x{}) - {}").format(
                args.shot, wa[2], wa[3], describe_grid(zones, caption)
            )
        )
        return EXIT_OK

    if args.hud:
        hud = Hud(zones, wa, hud_heading, scale)
        # Diagnostics: the only proof that the window is transparent for input.
        print(
            _("zone-picker: the HUD does not take input ({})").format(hud.input_mode),
            file=sys.stderr,
        )
        if args.at is not None:
            hud.apply_point(args.at)
        if args.follow:
            follow_stdin(hud)
        else:
            # --at without --shot: give the window one frame and exit, without a main loop "forever".
            GLib.timeout_add(HUD_HOLD_MS, lambda: (Gtk.main_quit(), False)[1])
        Gtk.main()
        return EXIT_OK

    if args.dry_run:
        print(_("zone-picker: grid {}").format(describe_grid(zones, caption)))

    picker = Picker(zones, grid, wa, output, mon_index, args, win_id, caption, scale)
    Gtk.main()
    return picker.exit_code


if __name__ == "__main__":
    sys.exit(main())

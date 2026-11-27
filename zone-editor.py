#!/usr/bin/env python3
"""
zone-editor.py — a visual zone grid editor for zone-tiler (Cinnamon, X11).

What it does:
  * shows the work area of the monitor and the zones at natural scale;
  * a zone can be dragged with the mouse, pulled by the edge/corner — size;
  * double click on empty space — a new zone, Delete — remove;
  * snapping to the screen edges, to other zones and to the 1/4, 1/3, 1/2, 2/3, 3/4 lines;
  * layout templates (2 columns, 1+2, 2×2, 3 columns, main + stack);
  * a set of zones per monitor: its own (`[zones:<output>]`) or the shared `[zones]`;
  * saving to zones.conf and registering the Cinnamon hotkeys;
  * binding a profile to a screen (`[monitors]`) and to a workspace (`[workspaces]`);
  * a gallery of ready-made layouts ("Work", "Cinema", "Stream", …): a choice
    creates a profile from the `gallery/` catalog instead of drawing a grid from scratch;
  * the "Place the active window into the selected zone" button — puts the active window into a zone at once.

Usage: zone-editor.py [--conf PATH] [--shot FILE.png]
"""

import argparse
import math
import os
import re
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
# PyGObject does not ship type-checking stubs — import without a check.
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402  # type: ignore

import zone_geom as geom  # noqa: E402
import zone_menu  # noqa: E402  (the thumbnails and the counts of the layout menus)
import zone_theme  # noqa: E402  (one palette for every surface: theme.conf next to zones.conf)
from zone_config import (  # noqa: E402
    GalleryError,
    MONITORS_SECTION,
    MONITOR_OFFLINE_NOTE,
    MONITOR_SHARED,
    WORKSPACE_SHARED,
    WORKSPACES_SECTION,
    Zone,
    gallery_layouts,
    install_gallery_layout,
    list_profiles,
    load_extra_sections,
    load_gallery_layout,
    load_monitor_map,
    load_workspace_map,
    load_zone_sets,
    load_zones,
    missing_rule_targets,
    monitor_dialog_rows,
    profile_path,
    profiles_overview,
    read_profile,
    remove_profile,
    sanitize_name,
    sanitize_profile_name,
    save_monitor_map,
    save_profile,
    save_workspace_map,
    unique_name,
    workspace_dialog_rows,
    zt,
)
from zone_i18n import _  # noqa: E402  (the interface strings and their translation live there too)

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")
HOTKEYS = os.path.join(HERE, "setup-hotkeys.py")

# The canvas paints from the theme of the window (`Editor.theme`) — the same tokens as the picker
# and the tab strip; without `theme.conf` these are the colours the editor always had.


CONF = zt.default_conf()

# Thumbnails of the layout menus, in logical pixels: a template tile, a profile row, a
# gallery card. The picture is the shape of the set, so the sizes only have to stay
# readable — the geometry itself comes from `zone_menu.thumb_rects`.
TEMPLATE_THUMB = (104, 60)
PROFILE_THUMB = (76, 42)
GALLERY_THUMB = (96, 54)


def desktops_count():
    """The number of workspaces from ``_NET_NUMBER_OF_DESKTOPS``, or ``None``.

    Read only when the dialog is opened: the property is changed by the Cinnamon
    settings, and there is no point in keeping it in the status line.
    """
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_NUMBER_OF_DESKTOPS"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    found = re.search(r"=\s*(\d+)", out)
    return int(found.group(1)) if found else None


def templates():
    Q = "q"

    def mk(spec):
        out = []
        for i, (name, x, y, w, h) in enumerate(spec, start=1):
            out.append(Zone(name, x, y, w, h, f"<Super><Alt>{i}"))
        return out

    return {
        _("2 columns"): mk([("left", 0, 0, 0.5, 1), ("right", 0.5, 0, 0.5, 1)]),
        "1 + 2": mk([
            ("main", 0, 0, 0.5, 1), ("tr", 0.5, 0, 0.5, 0.5), ("br", 0.5, 0.5, 0.5, 0.5),
        ]),
        "2 × 2": mk([
            (f"{Q}1", 0, 0, 0.5, 0.5), (f"{Q}2", 0.5, 0, 0.5, 0.5),
            (f"{Q}3", 0, 0.5, 0.5, 0.5), (f"{Q}4", 0.5, 0.5, 0.5, 0.5),
        ]),
        _("3 columns"): mk([
            ("left", 0, 0, 1 / 3, 1), ("mid", 1 / 3, 0, 1 / 3, 1), ("right", 2 / 3, 0, 1 / 3, 1),
        ]),
        _("main + stack"): mk([
            ("main", 0, 0, 0.66, 1), ("r1", 0.66, 0, 0.34, 1 / 3),
            ("r2", 0.66, 1 / 3, 0.34, 1 / 3), ("r3", 0.66, 2 / 3, 0.34, 1 / 3),
        ]),
        _("blank slate"): [],
    }


# ------------------------------------------------------- rule checking

DANGLING_SHOWN = 3  # how many dangling rules to list in the status, the rest by count


def _dangling_rules_status(dangling):
    """Status text: why we are not saving and which rules are in the way."""
    shown = ", ".join(f"{sel} → {zone}" for sel, zone in dangling[:DANGLING_SHOWN])
    rest = len(dangling) - DANGLING_SHOWN
    if rest > 0:
        return _(
            "not saving: rules from [rules] refer to missing zones - {shown} and {rest} more. Restore the zones or fix [rules] in zones.conf."
        ).format(shown=shown, rest=rest)
    return _(
        "not saving: rules from [rules] refer to missing zones - {shown}. Restore the zones or fix [rules] in zones.conf."
    ).format(shown=shown)


# -------------------------------------------------------------- drawing


def rounded_path(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def small_attrs(points=9, bold=False):
    """Pango attributes for the line under a name: one step below the body text."""
    attrs = Pango.AttrList()
    if bold:
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    attrs.insert(Pango.attr_size_new(int(points * Pango.SCALE)))
    return attrs


class Canvas(Gtk.DrawingArea):
    MARGIN = 26

    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.set_size_request(680, 460)
        self.set_can_focus(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self.connect("draw", self.on_draw)
        self.drag = None  # (index, mode, edges, start_rect, start_frac)
        self.hover = (None, "")

    # ---- geometry (the maths lives in zone_geom, without GTK)

    def screen_rect(self):
        a = self.get_allocation()
        return geom.screen_rect(a.width, a.height, self.editor.workarea(), self.MARGIN)

    def to_px(self, z):
        return geom.to_px(self.screen_rect(), z)

    def to_frac(self, px, py):
        return geom.to_frac(self.screen_rect(), px, py)

    def hit(self, px, py):
        return geom.hit_test(px, py, self.screen_rect(), self.editor.zones)

    # ---- events

    def on_draw(self, widget, cr):
        theme = self.editor.theme
        cr.set_source_rgb(*theme.rgb("bg"))
        cr.paint()

        sx, sy, sw, sh = self.screen_rect()
        # "screen": one material step above the window, outlined instead of filled in a hue
        rounded_path(cr, sx - 6, sy - 6, sw + 12, sh + 12, theme.radius)
        cr.set_source_rgb(*theme.rgb("bg_panel"))
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(*theme.rgb("border"), 0.9)
        cr.stroke()

        # guide lines
        cr.set_line_width(1)
        for g in geom.GUIDES[1:-1]:
            cr.set_source_rgba(*theme.rgb("fg"), 0.05)
            cr.move_to(sx + g * sw, sy)
            cr.line_to(sx + g * sw, sy + sh)
            cr.move_to(sx, sy + g * sh)
            cr.line_to(sx + sw, sy + g * sh)
            cr.stroke()

        zones = self.editor.zones
        selected = self.editor.selected
        # large zones are drawn first, small ones on top — so the labels are not lost
        order = sorted(range(len(zones)), key=lambda i: zones[i].w * zones[i].h, reverse=True)
        for i in order:
            z = zones[i]
            x, y, w, h = self.to_px(z)
            if i == selected:
                fill = (*theme.rgb("accent"), 0.10)
                stroke = (*theme.rgb("accent"), 1.0)
                width = 1.8
            else:
                fill = (*theme.rgb("fg"), 0.04)
                stroke = (*theme.rgb("border"), 0.9)
                width = 1.0
            rounded_path(cr, x + 1.5, y + 1.5, max(1, w - 3), max(1, h - 3), max(0, theme.radius - 2))
            cr.set_source_rgba(*fill)
            cr.fill_preserve()
            cr.set_line_width(width)
            cr.set_source_rgba(*stroke)
            cr.stroke()
            cr.save()
            cr.rectangle(x + 2, y + 2, max(1, w - 4), max(1, h - 4))
            cr.clip()
            self.draw_label(cr, z, x, y, w, h)
            cr.restore()

        # the work area frame
        cr.set_line_width(1.4)
        cr.set_source_rgba(*theme.rgb("border"), 0.9)
        cr.rectangle(sx, sy, sw, sh)
        cr.stroke()

        # legend: one short line; the monitor and its size live in the header chip
        cr.set_source_rgba(*theme.rgb("fg_muted"), 1)
        lay = widget.create_pango_layout(
            _("Drag a zone · resize from an edge · double-click to add")
        )
        lay.set_font_description(Pango.FontDescription(theme.font_desc(9)))
        cr.move_to(sx, sy + sh + 8)
        PangoCairo.show_layout(cr, lay)

    def draw_label(self, cr, z, x, y, w, h):
        if w < 34 or h < 22:
            return
        theme = self.editor.theme
        pad = 9
        inner = max(10, w - 2 * pad)
        layout = self.create_pango_layout("")
        layout.set_width(int(inner * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_font_description(Pango.FontDescription(theme.font_desc(11, bold=True)))
        layout.set_text(z.name, -1)
        cr.set_source_rgba(*theme.rgb("fg"), 0.92)
        cr.move_to(x + pad, y + pad - 2)
        PangoCairo.show_layout(cr, layout)

        if h > 46 and z.binding:
            layout2 = self.create_pango_layout("")
            layout2.set_width(int(inner * Pango.SCALE))
            layout2.set_ellipsize(Pango.EllipsizeMode.END)
            layout2.set_font_description(Pango.FontDescription(theme.font_desc(9)))
            layout2.set_text(z.binding, -1)
            cr.set_source_rgba(*theme.rgb("fg_muted"), 1)
            cr.move_to(x + pad, y + pad + 16)
            PangoCairo.show_layout(cr, layout2)

    # ---- mouse

    def on_button_press(self, widget, ev):
        self.grab_focus()
        if getattr(ev, "button", 1) != 1:
            return False
        idx, mode, edges = self.hit(ev.x, ev.y)
        double = ev.type == Gdk.EventType._2BUTTON_PRESS
        if idx is None:
            if double:
                self.editor.add_zone_at(*self.to_frac(ev.x, ev.y))
            else:
                self.editor.select(None)
                self.queue_draw()
            return True
        self.editor.select(idx)
        if not double:
            self.begin_drag(idx, mode, edges, *self.to_frac(ev.x, ev.y))
        return True

    def on_motion(self, widget, ev):
        if self.drag is None:
            idx, mode, edges = self.hit(ev.x, ev.y)
            self.set_cursor(mode, edges)
            return False
        return self.drag_to(*self.to_frac(ev.x, ev.y))

    def on_button_release(self, widget, ev):
        return self.end_drag()

    # ---- drag logic (separate from the events: that makes it testable)

    def begin_drag(self, index, mode, edges, fx, fy):
        z = self.editor.zones[index]
        self.drag = (index, mode, set(edges), z.copy(), (fx, fy))
        self.queue_draw()

    def drag_to(self, fx, fy):
        if self.drag is None:
            return False
        idx, mode, edges, start, start_f = self.drag
        screen = self.screen_rect()
        z = self.editor.zones[idx]
        z.x, z.y, z.w, z.h = geom.drag_rect(
            start, mode, edges, start_f, (fx, fy), self.editor.zones,
            geom.snap_tol(screen, "x"), geom.snap_tol(screen, "y"), skip=idx,
        )
        self.editor.update_geom_label()
        self.queue_draw()
        return True

    def end_drag(self):
        if self.drag is None:
            return False
        self.drag = None
        self.editor.refresh_panel()
        self.editor.set_dirty(True)
        return True

    def set_cursor(self, mode, edges):
        if mode == "resize":
            if {"l", "t"} <= edges or {"r", "b"} <= edges:
                name = "nwse-resize"
            elif {"r", "t"} <= edges or {"l", "b"} <= edges:
                name = "nesw-resize"
            elif edges & {"l", "r"}:
                name = "ew-resize"
            else:
                name = "ns-resize"
        elif mode == "move":
            name = "fleur"
        else:
            name = ""
        window = self.get_window()
        if window is None:
            return
        cache = getattr(self, "_cursors", None)
        if cache is None:
            cache = self._cursors = {}
        if name not in cache:
            cache[name] = Gdk.Cursor.new_from_name(self.get_display(), name) if name else None
        window.set_cursor(cache[name])

    # ---- keyboard

    def on_key_press(self, widget, ev):
        ed = self.editor
        step = 1 / 96 if ev.state & Gdk.ModifierType.SHIFT_MASK else 1 / 48
        if ev.keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            ed.remove_selected()
            return True
        if ev.keyval in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down):
            if ed.selected is None:
                return False
            z = ed.zones[ed.selected]
            dx = -step if ev.keyval == Gdk.KEY_Left else step if ev.keyval == Gdk.KEY_Right else 0
            dy = -step if ev.keyval == Gdk.KEY_Up else step if ev.keyval == Gdk.KEY_Down else 0
            resize = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
            z.x, z.y, z.w, z.h = geom.nudge((z.x, z.y, z.w, z.h), dx, dy, resize=resize)
            ed.refresh_panel()
            ed.set_dirty(True)
            self.queue_draw()
            return True
        return False

    def on_scroll(self, widget, ev):
        # the mouse wheel changes the width of the selected zone
        if self.editor.selected is None:
            return False
        z = self.editor.zones[self.editor.selected]
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            d = 0.01 if ev.delta_y < 0 else -0.01
        else:
            d = 0.01 if ev.direction == Gdk.ScrollDirection.UP else -0.01
        z.x, z.y, z.w, z.h = geom.scroll_resize((z.x, z.y, z.w, z.h), d)
        self.editor.set_dirty(True)
        self.queue_draw()
        return True


# ------------------------------------------------------------------- window


class Editor(Gtk.Window):
    def __init__(self, conf_path, theme=None):
        super().__init__(title=_("Gridsnap - window grid"))
        self.conf_path = conf_path
        self.theme = theme if theme is not None else zone_theme.load(conf_path)
        self._providers = []
        provider = apply_theme(self, self.theme)
        if provider is not None:
            self._providers.append(provider)
        self.set_icon_name("gridsnap")
        # Monitors are keyed by output names: the name addresses the zone set `[zones:<output>]`.
        self.outputs = zt.monitor_outputs()
        self.monitors = [rect for _name, rect in self.outputs]
        self.fallback_area = self.monitors[0] if self.monitors else (0, 0, 1920, 1080)
        sets = load_zone_sets(conf_path)
        self.default_zones = [z.copy() for z in sets.get(None) or []]
        self.file_own = {name for name in sets if name is not None}
        self.sets = {name: zones for name, zones in sets.items() if name is not None}
        self.edited = set()
        if not self.default_zones:
            # A config made of nothing but monitor sections: the shared set is started from the first
            # monitor, otherwise saving would write an empty (and unreadable) [zones].
            for name in [name for name, _rect in self.outputs] + list(self.sets):
                if self.sets.get(name):
                    self.default_zones = [z.copy() for z in self.sets[name]]
                    break
        # The monitor whose canvas is the shared `[zones]` set itself: the first on screen without
        # its own section. Edits on it change the shared set instead of making a copy.
        self.default_monitor = next(
            (name for name, _rect in self.outputs if name not in self.sets), None
        )
        self.combo_entries = [(name, rect) for name, rect in self.outputs]
        self.combo_entries += [(name, None) for name in self.sets if name not in dict(self.outputs)]
        self.monitor_index = 0
        self.current_monitor = None
        self.zones = []
        self.extra_sections = load_extra_sections(conf_path)
        self.selected = None
        self.dirty = False
        # The profile the canvas was loaded from, if any: the mark in the Profiles menu.
        # Cleared by the first edit — a changed draft is nobody's profile any more.
        self.profile_source = None
        self.message = ""
        self._loading = False
        self.set_default_size(1120, 700)
        self.set_border_width(10)
        self.connect("key-press-event", self.on_key)
        # Back in the window — the workspace may have been switched: update the workspace note
        # here, not in every update_status (that is an xprop call).
        self.connect("focus-in-event", self.on_focus)
        self.workspace_note = self.workspace_hint()
        self.build_ui()
        if self.combo_entries:
            self.show_monitor(self.combo_entries[0][0])
        else:
            self.refresh_panel()
        self.canvas.queue_draw()

    # ---- layout

    def build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)
        root.pack_start(self.build_header(), False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        body.set_border_width(12)
        root.pack_start(body, True, True, 0)

        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.canvas = Canvas(self)
        self.canvas.connect("button-press-event", self.canvas.on_button_press)
        self.canvas.connect("button-release-event", self.canvas.on_button_release)
        self.canvas.connect("motion-notify-event", self.canvas.on_motion)
        self.canvas.connect("key-press-event", self.canvas.on_key_press)
        self.canvas.connect("scroll-event", self.canvas.on_scroll)
        frame.add(self.canvas)
        body.pack_start(frame, True, True, 0)

        body.pack_start(self.build_sidebar(), False, False, 0)
        root.pack_start(self.build_status_bar(), False, False, 0)

    # ---- layout widgets: the thumbnails and the rows of the Templates/Profiles menus

    def _thumb(self, zones, size):
        """A layout thumbnail: the set's rectangles drawn in a small box, from the theme tokens."""
        width, height = size
        area = Gtk.DrawingArea()
        area.set_size_request(width, height)
        area.connect("draw", self._draw_thumb, zones)
        return area

    def _draw_thumb(self, widget, cr, zones):
        """The frame plus the zone rectangles; an empty set is a dashed frame ("blank slate")."""
        a = widget.get_allocation()
        width, height = a.width, a.height
        theme = self.theme
        rounded_path(cr, 0.5, 0.5, max(1, width - 1), max(1, height - 1), 5)
        cr.set_line_width(1)
        if not zones:
            # An empty solid box would read as one zone; the dashed frame says "nothing here".
            cr.set_dash([4, 3])
            cr.set_source_rgba(*theme.rgba("fg_muted", 0.7))
            cr.stroke()
            cr.set_dash([])
            return False
        cr.set_source_rgba(*theme.rgba("border", 0.9))
        cr.stroke()
        for x, y, w, h in zone_menu.thumb_rects(zones, width, height):
            rounded_path(cr, x, y, w, h, 2)
            cr.set_source_rgba(*theme.rgba("fg", 0.10))
            cr.fill_preserve()
            cr.set_line_width(1)
            cr.set_source_rgba(*theme.rgba("fg", 0.35))
            cr.stroke()
        return False

    def _from_popover(self, popover, callback):
        """Close the menu before the action changes the canvas behind it."""
        if popover is not None:
            popover.popdown()
        callback()

    def _tile(self, title, zones, callback):
        """One template: the picture and its name, a flat button for the 2-column grid."""
        button = Gtk.Button()
        button.get_style_context().add_class("layout-tile")
        button.set_relief(Gtk.ReliefStyle.NONE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(self._thumb(zones, TEMPLATE_THUMB), False, False, 0)
        box.pack_start(Gtk.Label(label=title), False, False, 0)
        button.add(box)
        button.connect(
            "clicked",
            lambda _w: self._from_popover(getattr(self, "tpl_pop", None), callback),
        )
        return button

    def _layout_row(self, zones, title, meta, callback, active=False):
        """One profile: a thumbnail, the name, the counts and the "on the canvas" mark.

        ``meta`` is a list of lines: the counts and the bindings are split so neither is
        clipped by the width of a popover, and a profile without bindings keeps one line.
        """
        button = Gtk.Button()
        context = button.get_style_context()
        context.add_class("layout-row")
        if active:
            context.add_class("current")
        button.set_relief(Gtk.ReliefStyle.NONE)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(self._thumb(zones, PROFILE_THUMB), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name = Gtk.Label(label=title, xalign=0)
        name.set_attributes(small_attrs(10, bold=True))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(26)
        text.pack_start(name, False, False, 0)
        for line in meta:
            if not line:
                continue
            note = Gtk.Label(label=line, xalign=0)
            note.set_attributes(small_attrs())
            note.set_ellipsize(Pango.EllipsizeMode.END)
            note.set_max_width_chars(46)
            note.get_style_context().add_class("dim-label")
            text.pack_start(note, False, False, 0)
        box.pack_start(text, True, True, 0)
        if active:
            mark = Gtk.Label(label="✓")
            mark.get_style_context().add_class("layout-current-mark")
            box.pack_end(mark, False, False, 0)
        button.add(box)
        button.connect(
            "clicked",
            lambda _w: self._from_popover(getattr(self, "prof_pop", None), callback),
        )
        return button

    def _menu_item(self, label, callback, style_class=None, icon=None):
        """One popover row: flat, left-aligned, with an optional leading icon."""
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("menu-row")
        if style_class:
            button.get_style_context().add_class(style_class)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if icon is not None:
            box.pack_start(
                Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0
            )
        box.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
        button.add(box)
        button.connect("clicked", lambda _w: callback())
        return button

    def _profile_meta(self, row):
        """The counts under a profile name: one line of totals, one of bindings.

        Split rather than truncated: "Workspaces: 1–2" is the reason a profile exists, and
        a clipped ellipsis there costs more than the second line.
        """
        lines = [_("Zones: {}").format(row["total"])]
        if row["monitors"]:
            lines[0] += " · " + _("Monitor sets: {}").format(row["monitors"])
        bindings = []
        if row["rules"]:
            bindings.append(_("Rules: {}").format(row["rules"]))
        workspaces = zone_menu.compact_ranges(row["workspaces"])
        if workspaces:
            bindings.append(_("Workspaces: {}").format(workspaces))
        if row["outputs"]:
            # `[monitors]` comes back lowercased (ConfigParser), the chip names are the real
            # outputs: show the spelling the person sees in the monitor list.
            known = {name.casefold(): name for name, _rect in self.combo_entries}
            names = [known.get(output.casefold(), output) for output in row["outputs"]]
            bindings.append(_("Monitors: {}").format(", ".join(names)))
        if bindings:
            lines.append(" · ".join(bindings))
        return lines

    def _own_set_row(self):
        """The first row of Profiles: the file's own set for this monitor — the way back."""
        name = self.current_monitor
        if name is None:
            return None
        try:
            zones = load_zones(self.conf_path, monitor=name)
        except (SystemExit, ValueError, OSError):
            zones = []
        meta = [_("Zones: {}").format(len(zones)) + " · " + name]
        return self._layout_row(
            zones,
            _("Monitor's own set"),
            meta,
            self.revert_to_file,
            active=self.profile_source is None,
        )

    def revert_to_file(self):
        """Put the file's own set of this monitor back on the canvas.

        The way out of a profile draft: the zones, the rules and the exclusions come from
        `zones.conf` again. Unsaved edits of this monitor are dropped — that is what the
        "own set" means, and Save would otherwise write the profile back.
        """
        name = self.current_monitor
        before = geom.zone_set_signature(self.zones)
        try:
            zones = load_zones(self.conf_path, monitor=name)
            extra = load_extra_sections(self.conf_path, sections=["rules", "ignore"])
        except (SystemExit, ValueError, OSError) as exc:
            self.set_status(_("own set not read: {}").format(exc))
            return
        self.set_zones([z.copy() for z in zones])
        self.edited.discard(name)
        self._set_extra("rules", extra.get("rules"))
        self._set_extra("ignore", extra.get("ignore"))
        self.selected = 0 if self.zones else None
        self.select(self.selected)
        if before != geom.zone_set_signature(self.zones):
            self.set_dirty(True)
        self.profile_source = None
        self.canvas.queue_draw()
        self.set_status(_("monitor's own set: {} zones").format(len(self.zones)))

    def _set_extra(self, section, lines):
        """Replace one carried-over section: lines present — keep them, nothing — drop it."""
        if lines:
            self.extra_sections[section] = lines
        else:
            self.extra_sections.pop(section, None)

    def build_header(self):
        """One row: what the canvas shows, the two frequent menus, Save and everything else.

        The old toolbar carried twelve controls in a row — seven icon buttons, one of them
        destructive, plus five text menus; a person had to read all of them to find Save. The
        common path stays on top (monitor, templates, profiles, a filled Save) and the rest moves
        one level down into "…".
        """
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("editor-header")

        title = Gtk.Label(label=_("Layout"), xalign=0)
        title.get_style_context().add_class("editor-heading")
        bar.pack_start(title, False, False, 0)

        self.mon_combo = Gtk.ComboBoxText()
        for name, rect in self.combo_entries:
            if rect is None:
                # The [zones:<output>] section is left over from a previous connection — it
                # can be viewed and fixed, but not deleted.
                self.mon_combo.append_text(_("{} - not on screen").format(name))
            else:
                self.mon_combo.append_text(f"{name} — {rect[2]}×{rect[3]}")
        self.mon_combo.set_active(0 if self.combo_entries else -1)
        self.mon_combo.set_tooltip_text(_("The monitor whose set is on the canvas"))
        self.mon_combo.connect("changed", self.on_monitor_changed)
        bar.pack_start(self.mon_combo, False, False, 0)
        bar.pack_start(Gtk.Box(), True, True, 0)

        self.tpl_btn = Gtk.MenuButton()
        self.tpl_btn.set_tooltip_text(_("Layout templates"))
        self.tpl_btn.set_label(_("Templates"))
        tpl_pop = Gtk.Popover()
        self.tpl_pop = tpl_pop
        # Two columns of tiles: a template is a picture, so its name is the caption, not the item.
        tpl_grid = Gtk.Grid(row_spacing=2, column_spacing=2)
        tpl_grid.set_border_width(6)
        tpl_grid.set_column_homogeneous(True)
        for index, (name, zones) in enumerate(templates().items()):
            tpl_grid.attach(
                self._tile(name, zones, lambda n=name, t=zones: self.apply_template(t, n)),
                index % 2,
                index // 2,
                1,
                1,
            )
        tpl_pop.add(tpl_grid)
        tpl_grid.show_all()
        self.tpl_btn.set_popover(tpl_pop)
        bar.pack_start(self.tpl_btn, False, False, 0)

        self.prof_btn = Gtk.MenuButton()
        self.prof_btn.set_tooltip_text(_("Layout profiles (zones + rules + keys)"))
        self.prof_btn.set_label(_("Profiles"))
        self.prof_pop = Gtk.Popover()
        self.prof_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.prof_box.set_border_width(6)
        self.prof_pop.add(self.prof_box)
        self.prof_btn.set_popover(self.prof_pop)
        self.prof_btn.connect("clicked", lambda _b: self.rebuild_profiles_menu())
        bar.pack_start(self.prof_btn, False, False, 0)

        self.save_btn = Gtk.Button(label=_("Save"))
        self.save_btn.set_tooltip_text(_("Save to zones.conf (Ctrl+S)"))
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda _b: self.on_save())
        self.save_btn.set_sensitive(False)
        bar.pack_start(self.save_btn, False, False, 0)

        bar.pack_start(self.build_more_menu(), False, False, 0)
        return bar

    def build_more_menu(self):
        """Everything off the common path, grouped by what it does, the destructive item apart."""
        self.more_btn = Gtk.MenuButton()
        self.more_btn.set_label("⋯")
        self.more_btn.set_tooltip_text(
            _("More: gallery, theme, placement, bindings, clear")
        )
        self.more_pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(6)
        self.more_pop.add(box)
        self.more_btn.set_popover(self.more_pop)

        def item(label, callback, style_class=None, icon=None):
            """One row of the menu; the popover closes before the callback runs."""
            row = self._menu_item(
                label,
                lambda f=callback: self._run_from_menu(f),
                style_class,
                icon,
            )
            box.pack_start(row, False, False, 0)
            return row

        def heading(text):
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("dim-label")
            label.set_margin_top(6)
            label.set_margin_start(8)
            box.pack_start(label, False, False, 0)

        item(_("From the gallery…"), self.gallery_dialog)
        heading(_("Theme"))
        item(_("From the wallpaper…"), self.on_theme_from_wallpaper)
        item(_("Window controls…"), self.on_theme_controls)
        item(_("Reload theme.conf"), self.on_theme_reload)
        box.pack_start(Gtk.Separator(), False, False, 4)
        heading(_("Placement"))
        item(_("Place the window on another monitor…"), self.open_target_menu)
        item(_("Place the active window into the selected zone"), self.on_test)
        heading(_("Bindings"))
        item(_("Workspace profiles…"), self.workspace_profiles_dialog)
        item(_("Monitor profiles…"), self.monitor_profiles_dialog)
        item(_("Apply hotkeys in Cinnamon"), self.on_hotkeys)
        # The one irreversible row: its own gap and a warning icon, never under the pointer
        # that was aiming at the last harmless item.
        gap = Gtk.Separator()
        gap.set_margin_top(8)
        box.pack_start(gap, False, False, 0)
        item(
            _("Clear all…"),
            self.on_clear_confirmed,
            "destructive-action",
            icon="dialog-warning-symbolic",
        )

        self.target_pop = Gtk.Popover()
        self.target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.target_box.set_border_width(6)
        self.target_pop.add(self.target_box)

        box.show_all()
        return self.more_btn

    def _run_from_menu(self, callback):
        self.more_pop.popdown()
        callback()

    def open_target_menu(self):
        """The second-level "on another monitor" choice: monitor → the zones of its set."""
        self.rebuild_target_menu()
        self.target_pop.set_relative_to(self.more_btn)
        self.target_pop.show_all()
        self.target_pop.popup()

    def on_clear_confirmed(self):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Clear all zones?"),
        )
        dlg.format_secondary_text(
            _("The set on the canvas becomes empty until you save another layout.")
        )
        answer = dlg.run()
        dlg.destroy()
        if answer == Gtk.ResponseType.YES:
            self.on_clear()

    def build_sidebar(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.set_size_request(300, -1)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=_("Zones"), xalign=0)
        label.get_style_context().add_class("sidebar-heading")
        head.pack_start(label, False, False, 0)
        head.pack_start(Gtk.Box(), True, True, 0)
        add = Gtk.Button()
        add.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        add.set_tooltip_text(_("Add zone"))
        add.get_style_context().add_class("flat")
        add.connect("clicked", lambda _b: self.on_add())
        head.pack_end(add, False, False, 0)
        side.pack_start(head, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(200)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self.on_row_selected)
        scroller.add(self.listbox)
        side.pack_start(scroller, True, True, 0)

        self.inspector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label=_("Selected zone"), xalign=0)
        heading.get_style_context().add_class("sidebar-heading")
        self.inspector.pack_start(heading, False, False, 0)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        grid.attach(Gtk.Label(label=_("Name"), xalign=0), 0, 0, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.connect("changed", self.on_name_changed)
        grid.attach(self.name_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Keys"), xalign=0), 0, 1, 1, 1)
        self.key_entry = Gtk.Entry()
        self.key_entry.set_placeholder_text("<Super><Alt>5")
        self.key_entry.connect("changed", self.on_key_changed)
        grid.attach(self.key_entry, 1, 1, 1, 1)
        self.geom_label = Gtk.Label(xalign=0)
        self.geom_label.get_style_context().add_class("dim-label")
        self.geom_label.set_line_wrap(True)
        grid.attach(self.geom_label, 0, 2, 2, 1)
        self.inspector.pack_start(grid, False, False, 0)
        side.pack_start(self.inspector, False, False, 0)

        self.empty_hint = Gtk.Label(
            label=_("Select a zone in the list to edit it, or double-click the canvas to add one."),
            xalign=0,
        )
        self.empty_hint.set_line_wrap(True)
        self.empty_hint.get_style_context().add_class("dim-label")
        # `show_all()` runs after construction and would show it unconditionally; visibility here
        # follows the selection (refresh_panel), not the tree walk.
        self.empty_hint.set_no_show_all(True)
        side.pack_start(self.empty_hint, False, False, 0)
        return side

    def build_status_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("status-bar")
        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim-label")
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        bar.pack_start(self.status, True, True, 0)
        return bar


    # ---- state

    def workarea(self):
        """The work area of the current monitor; for a disconnected one — the first on screen."""
        if not self.combo_entries:
            return self.fallback_area
        _name, rect = self.combo_entries[self.monitor_index]
        return rect or self.fallback_area

    def set_status(self, text):
        """A message on one line; the status always shows whose set is on the canvas."""
        self.message = text
        self.update_status()

    def update_status(self):
        """One line: the last event, or whose set is on the canvas.

        The workspace note is no longer prose in the status bar: it is the tooltip of the monitor
        chip, where the question "which set am I looking at" actually originates.
        """
        if getattr(self, "status", None) is None:
            return
        self.status.set_text(self.message or self.set_hint())
        chip = getattr(self, "mon_combo", None)
        if chip is not None:
            note = getattr(self, "workspace_note", "")
            chip.set_tooltip_text(
                note or _("The monitor whose set is on the canvas")
            )

    # ---- theme

    def reload_theme(self):
        """Re-read theme.conf and repaint the editor without a restart."""
        self.theme = zone_theme.load(self.conf_path)
        css = self.theme.gtk_css().encode("utf-8")
        for provider in self._providers:
            provider.load_from_data(css)
        self.canvas.queue_draw()

    def follow_controls_theme(self):
        """A generated controls theme follows the palette; a hand-written one is never touched.

        The daemon does the same on its poll, but the editor may run without it, and a user who
        just picked **From the wallpaper…** or **Reload theme.conf** expects the files to match
        what is in `theme.conf`.
        """
        try:
            return zone_theme.refresh_gtk_theme(self.conf_path, theme=self.theme)
        except (OSError, ValueError) as exc:
            self.set_status(_("controls theme not refreshed: {}").format(exc))
            return None

    def on_theme_reload(self):
        self.reload_theme()
        self.follow_controls_theme()
        self.set_status(
            _("theme reloaded: {}").format(self.theme.path or _("default palette"))
        )

    def on_theme_from_wallpaper(self):
        image = zone_theme.wallpaper_image()
        if not image:
            self.set_status(_("no wallpaper found in the desktop settings"))
            return
        try:
            theme = zone_theme.from_wallpaper(image)
        except ValueError as exc:
            self.set_status(_("cannot read the wallpaper: {}").format(exc))
            return
        target = zone_theme.theme_path(self.conf_path)
        zone_theme.write_theme(theme, target, source=image)
        self.reload_theme()
        self.follow_controls_theme()
        self.set_status(_("theme written: {}").format(target))

    def on_theme_controls(self):
        directory = os.path.expanduser(zone_theme.GTK_THEME_DIR)
        zone_theme.write_gtk_theme(directory, self.theme)
        self.set_status(
            _("controls theme written: {} (Cinnamon Settings, Themes, Controls)").format(directory)
        )

    def workspace_hint(self):
        """The note about the active workspace: which profile it uses.

        In EWMH workspaces are counted from zero, in ``[workspaces]`` — from 1. The "place
        the active window" button goes to the grid of ``zones.conf`` itself
        (``--workspace conf``), so without this note it is unclear why on a
        workspace with a profile the editor's zones are not found.
        """
        workspace = zt.current_workspace()
        if workspace is None:
            return ""
        number = workspace + 1
        try:
            profile = load_workspace_map(self.conf_path).get(number)
        except (SystemExit, ValueError, OSError):
            return ""
        if not profile:
            return ""
        try:
            exists = os.path.exists(profile_path(profile, self.conf_path))
        except ValueError:
            exists = False
        tail = (
            ""
            if exists
            else _(" (no profile file - the workspace is laid out by the default set)")
        )
        return _(
            "workspace {} uses profile \"{}\"{}; the editor edits the shared zones.conf"
        ).format(number, profile, tail)

    def refresh_workspace_hint(self):
        """Recompute the workspace note and refresh the status."""
        self.workspace_note = self.workspace_hint()
        # A focus event can arrive before build_ui — then there is no status yet.
        if getattr(self, "status", None) is not None:
            self.update_status()

    def set_hint(self):
        """Its own set, inheritance or the shared `[zones]` — by the current monitor."""
        name = self.current_monitor
        if name is None:
            return _("no set selected")
        rect = dict(self.combo_entries).get(name)
        return geom.set_origin_text(
            name,
            self._is_own(name),
            shared=(name == self.default_monitor),
            offline=rect is None,
        )

    def _is_own(self, monitor):
        """Whether the monitor has a set of its own: a section in the file or edits in the editor."""
        return monitor in self.file_own or monitor in self.edited

    def set_dirty(self, val):
        self.dirty = val
        if val:
            self.profile_source = None
            # Any edit cancels the previous "saved…" in the status.
            self.message = ""
        if val and self.current_monitor is not None and self.current_monitor != self.default_monitor:
            # An edit of an inherited set makes it its own: on saving, the
            # monitor gets a section of its own, [zones:<output>].
            self.edited.add(self.current_monitor)
        self.set_title(("● " if val else "") + _("Gridsnap - window grid"))
        save = getattr(self, "save_btn", None)
        if save is not None:
            save.set_sensitive(val)
        self.update_status()

    def show_monitor(self, name):
        """Show the set of the monitor: the section from the file or a copy of the shared `[zones]`."""
        index = next(
            (i for i, (item, _rect) in enumerate(self.combo_entries) if item == name), 0
        )
        if name == self.default_monitor:
            self.zones = self.default_zones
        elif name in self.sets:
            self.zones = self.sets[name]
        else:
            zones, _own = geom.set_for_monitor(name, self.sets, self.default_zones)
            self.zones = self.sets[name] = zones
        self.current_monitor = name
        self.profile_source = None
        self.monitor_index = index
        self.selected = 0 if self.zones else None
        self.message = ""
        self._loading = True
        self.mon_combo.set_active(index)
        self._loading = False
        self.refresh_panel()
        self.refresh_workspace_hint()
        self.canvas.queue_draw()

    def set_zones(self, zones):
        """Replace the contents of the current set in place (the link to the draft stays alive)."""
        self.zones[:] = list(zones)

    def select_monitor_index(self, index):
        """Show the monitor by its number in the combo — the ``--monitor`` argument."""
        if index is None or not 0 <= index < len(self.combo_entries):
            return
        self.show_monitor(self.combo_entries[index][0])

    def _draft(self, monitor):
        """The draft of a monitor's set: the shown one, the one from the file or a copy of the shared set."""
        if monitor == self.default_monitor:
            return self.default_zones
        if monitor in self.sets:
            return self.sets[monitor]
        return [z.copy() for z in self.default_zones]

    def _all_drafts(self):
        """The sets of all monitors — both on screen and disconnected (their sections live on)."""
        return {name: self._draft(name) for name, _rect in self.combo_entries}

    def _all_zone_names(self):
        """The zone names of all sets — the targets of the `[rules]` rules are checked against them."""
        names = set()
        for zones in self._all_drafts().values():
            names.update(z.name for z in zones)
        return names

    def select(self, index):
        self.selected = index
        self._loading = True
        self.listbox.unselect_all()
        if index is not None and index < len(self.zones):
            row = self.listbox.get_row_at_index(index)
            if row is not None:
                self.listbox.select_row(row)
        self._loading = False
        self.refresh_panel()
        self.canvas.queue_draw()

    def refresh_panel(self):
        self._loading = True
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        for i, z in enumerate(self.zones):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(2)
            # The zone colour is identity here (and in the picker); the canvas stays neutral.
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(10, 10)
            swatch.set_valign(Gtk.Align.CENTER)
            col = self.theme.zone_rgb(i)
            swatch.connect("draw", self._draw_swatch, col)
            name = Gtk.Label(label=z.name, xalign=0)
            key = Gtk.Label(label=z.binding or "", xalign=1)
            key.get_style_context().add_class("dim-label")
            box.pack_start(swatch, False, False, 0)
            box.pack_start(name, True, True, 0)
            box.pack_end(key, False, False, 0)
            row.add(box)
            row.show_all()
            self.listbox.add(row)
        if self.selected is not None and self.selected < len(self.zones):
            row = self.listbox.get_row_at_index(self.selected)
            if row:
                self.listbox.select_row(row)
            z = self.zones[self.selected]
            self.name_entry.set_text(z.name)
            self.key_entry.set_text(z.binding)
            self.name_entry.set_sensitive(True)
            self.key_entry.set_sensitive(True)
            self.update_geom_label()
        else:
            self.name_entry.set_text("")
            self.key_entry.set_text("")
            self.name_entry.set_sensitive(False)
            self.key_entry.set_sensitive(False)
            self.geom_label.set_text("—")
        inspector = getattr(self, "inspector", None)
        if inspector is not None:
            inspector.set_visible(self.selected is not None)
        hint = getattr(self, "empty_hint", None)
        if hint is not None:
            hint.set_visible(self.selected is None)
        self._loading = False

    def update_geom_label(self):
        if self.selected is None or self.selected >= len(self.zones):
            self.geom_label.set_text("—")
            return
        z = self.zones[self.selected]
        _x, _y, ww, wh = self.workarea()
        self.geom_label.set_text(
            _("{} % × {} % · {} × {} px").format(
                round(z.w * 100),
                round(z.h * 100),
                round(ww * z.w),
                round(wh * z.h),
            )
        )

    @staticmethod
    def _draw_swatch(widget, cr, col):
        a = widget.get_allocation()
        rounded_path(cr, 1, 1, a.width - 2, a.height - 2, 3)
        cr.set_source_rgb(*col)
        cr.fill()

    # ---- panel handlers

    def on_row_selected(self, _lb, row):
        if self._loading or row is None:
            return
        self.selected = row.get_index()
        self.refresh_panel()
        self.canvas.queue_draw()

    def on_name_changed(self, entry):
        if self._loading or self.selected is None:
            return
        z = self.zones[self.selected]
        new = sanitize_name(entry.get_text())
        if not new or any(o.name == new for o in self.zones if o is not z):
            return
        z.name = new
        self.set_dirty(True)
        self.refresh_panel()
        self.canvas.queue_draw()

    def on_key_changed(self, entry):
        if self._loading or self.selected is None:
            return
        z = self.zones[self.selected]
        value = entry.get_text().strip()
        z.binding = value
        # `[keys]` is one per file: a zone's key is one key in every set that has this name.
        for zones in self._all_drafts().values():
            for other in zones:
                if other is not z and other.name == z.name:
                    other.binding = value
        self.set_dirty(True)
        self.canvas.queue_draw()

    def on_monitor_changed(self, combo):
        if self._loading:
            return
        index = combo.get_active()
        if not 0 <= index < len(self.combo_entries):
            return
        self.show_monitor(self.combo_entries[index][0])

    def on_key(self, _w, ev):
        if ev.state & Gdk.ModifierType.CONTROL_MASK and ev.keyval == Gdk.KEY_s:
            self.on_save()
            return True
        return False

    def on_focus(self, *_args):
        """The window is focused again — refresh the note about the active workspace."""
        self.refresh_workspace_hint()
        return False

    # ---- actions

    def _unique_name(self, base="zone"):
        return unique_name(self.zones, base)

    def add_zone_at(self, fx, fy):
        w = h = 1 / 3
        x = min(max(0.0, fx - w / 2), 1 - w)
        y = min(max(0.0, fy - h / 2), 1 - h)
        used = {z.binding for z in self.zones}
        n = 1
        while f"<Super><Alt>{n}" in used and n < 10:
            n += 1
        z = Zone(self._unique_name(), x, y, w, h, f"<Super><Alt>{n}" if n < 10 else "")
        self.zones.append(z)
        self.select(len(self.zones) - 1)
        self.set_dirty(True)
        self.canvas.queue_draw()

    def on_add(self):
        self.add_zone_at(0.5, 0.5)

    def on_remove(self):
        self.remove_selected()

    def remove_selected(self):
        if self.selected is None or not self.zones:
            return
        del self.zones[self.selected]
        self.selected = min(self.selected, len(self.zones) - 1) if self.zones else None
        self.select(self.selected)
        self.set_dirty(True)
        self.canvas.queue_draw()

    def on_duplicate(self):
        if self.selected is None:
            return
        z = self.zones[self.selected].copy()
        z.name = self._unique_name()
        z.binding = ""
        z.x = min(1 - z.w, z.x + 0.03)
        z.y = min(1 - z.h, z.y + 0.03)
        self.zones.append(z)
        self.select(len(self.zones) - 1)
        self.set_dirty(True)
        self.canvas.queue_draw()

    def on_clear(self):
        self.set_zones([])
        self.selected = None
        self.select(None)
        self.set_dirty(True)
        self.canvas.queue_draw()

    def apply_template(self, zones, name):
        self.set_zones([z.copy() for z in zones])
        self.selected = 0 if self.zones else None
        self.select(self.selected)
        self.set_dirty(True)
        self.canvas.queue_draw()
        self.set_status(
            _("template \"{}\": {} zones (don't forget to save)").format(name, len(self.zones))
        )

    # ---- profiles

    def rebuild_profiles_menu(self):
        """Rebuild the "Profiles" menu: the file's own set, the saved profiles, then the actions.

        Every profile row is a picture plus the counts under its name: the geometry is the
        default set of the profile, the counts come from :func:`zone_config.profiles_overview`
        (all sets, the ``[rules]`` pairs and the ``[workspaces]``/``[monitors]`` bindings).
        """
        for child in self.prof_box.get_children():
            self.prof_box.remove(child)
        own = self._own_set_row()
        if own is not None:
            self.prof_box.pack_start(own, False, False, 0)
        for row in profiles_overview(self.conf_path):
            if row["broken"]:
                broken = Gtk.Label(
                    label=_("{} — the file cannot be read").format(row["name"]), xalign=0
                )
                broken.get_style_context().add_class("dim-label")
                broken.set_margin_top(4)
                broken.set_margin_start(8)
                self.prof_box.pack_start(broken, False, False, 0)
                continue
            self.prof_box.pack_start(
                self._layout_row(
                    row["zones"],
                    row["name"],
                    self._profile_meta(row),
                    lambda n=row["name"]: self.load_profile(n),
                    active=row["name"] == self.profile_source,
                ),
                False,
                False,
                0,
            )
        self.prof_box.pack_start(Gtk.Separator(), False, False, 2)

        def action(label, callback):
            """A row that puts the popover away before the dialog it opens comes up."""

            def run():
                self.prof_pop.popdown()
                callback()

            self.prof_box.pack_start(self._menu_item(label, run), False, False, 0)

        action(_("Workspace profiles…"), self.workspace_profiles_dialog)
        action(_("Monitor profiles…"), self.monitor_profiles_dialog)
        action(_("Save the current layout as a profile…"), self.save_profile_dialog)
        action(_("Delete profile…"), self.remove_profile_dialog)
        self.prof_box.show_all()

    # ---- the gallery of ready-made layouts

    def _gallery_zones(self, gid):
        """The shape a gallery card draws: its default set, or the first non-empty monitor set."""
        try:
            layout = load_gallery_layout(gid)
        except (GalleryError, SystemExit, ValueError, OSError):
            return []
        if layout["zones"]:
            return layout["zones"]
        for zones in layout["monitors"].values():
            if zones:
                return zones
        return []

    def gallery_dialog(self):
        """The "Layout gallery" dialog: a ready-made scenario → a profile → the canvas.

        The list is built from `zone_config.gallery_layouts()` — the title, the
        description and how many zones. A choice **copies** the layout into a profile
        (`install_gallery_layout`) instead of editing the catalog: after that it is edited
        as one's own, and the gallery stays a gallery.
        """
        try:
            layouts = gallery_layouts()
        except (GalleryError, OSError) as exc:
            self.set_status(_("gallery could not be read: {}").format(exc))
            return
        if not layouts:
            self.set_status(_("the gallery has no layouts"))
            return
        dlg = Gtk.Dialog(title=_("Layout gallery"), transient_for=self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Add to profiles"), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.add(
            Gtk.Label(
                label=_(
                    "A ready-made layout is zones, hotkeys and application rules. Choosing it creates a profile; from then on you edit the profile as your own."
                ),
                xalign=0,
            )
        )
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for item in layouts:
            row = Gtk.ListBoxRow()
            holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            holder.set_border_width(6)
            picture = self._gallery_zones(item["id"])
            holder.pack_start(self._thumb(picture, GALLERY_THUMB), False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            head = Gtk.Label(
                label=_("{} — {} zones").format(item["title"], len(item["zones"])),
                xalign=0,
            )
            # The title in bold (Pango directly, without markup): in a list of
            # eight rows you cannot otherwise see where a row ends and the next begins.
            weight = Pango.AttrList()
            weight.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
            head.set_attributes(weight)
            text.pack_start(head, False, False, 0)
            desc = Gtk.Label(label=item["description"], xalign=0)
            desc.set_line_wrap(True)
            desc.set_max_width_chars(54)
            desc.get_style_context().add_class("dim-label")
            text.pack_start(desc, False, False, 0)
            holder.pack_start(text, True, True, 0)
            row.add(holder)
            listbox.add(row)
        listbox.select_row(listbox.get_row_at_index(0))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(560, 420)
        scroller.add(listbox)
        box.add(scroller)
        dlg.show_all()
        resp = dlg.run()
        chosen = listbox.get_selected_row()
        # Take the index before destroy: for a detached row it can become -1.
        index = chosen.get_index() if chosen is not None else None
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if index is None or index < 0:
            self.set_status(_("no layout selected"))
            return
        self.install_gallery_dialog(layouts[index])

    def install_gallery_dialog(self, item):
        """Create a profile from a gallery layout and offer to load it."""
        gid = item["id"]
        name = self._ask_text(
            _("Add the layout to profiles"),
            _("Profile name for the layout \"{}\"").format(item["title"]),
            gid,
        )
        if not name:
            return
        safe = sanitize_profile_name(name)
        if not safe:
            self.set_status(_("empty profile name"))
            return
        # The profile already exists — ask instead of overwriting silently: the name could
        # be taken by a layout of one's own, and a choice from the gallery must not wipe it.
        force = os.path.exists(profile_path(safe, self.conf_path))
        if force and not self._ask_yes_no(
            _("Profile \"{}\" already exists. Overwrite it with the layout \"{}\"?").format(
                safe, item["title"]
            )
        ):
            self.set_status(
                _("layout not added: profile \"{}\" already exists").format(safe)
            )
            return
        try:
            path = install_gallery_layout(
                gid, name=safe, conf_path=self.conf_path, force=force
            )
        except (GalleryError, OSError) as exc:
            self.set_status(_("layout not added: {}").format(exc))
            return
        self.set_status(
            _("layout \"{}\" → profile \"{}\": {}").format(item["title"], safe, path)
        )
        if self._ask_yes_no(_("Load the layout \"{}\" into the canvas?").format(item["title"])):
            self.load_profile(safe)

    def _ask_yes_no(self, question):
        """Ask "yes/no" with a modal dialog; ``True`` — "Yes" was pressed.

        The button labels come from GTK itself (`YES_NO`): they are already translated into the
        system language, and they are the same words as in the other dialogs.
        """
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
        )
        dlg.set_property("text", question)
        dlg.set_default_response(Gtk.ResponseType.YES)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    def _ask_text(self, title, label, initial=""):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Save"), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.add(Gtk.Label(label=label, xalign=0))
        entry = Gtk.Entry()
        entry.set_text(initial)
        entry.set_activates_default(True)
        box.add(entry)
        dlg.show_all()
        resp = dlg.run()
        text = entry.get_text()
        dlg.destroy()
        return text.strip() if resp == Gtk.ResponseType.OK else ""

    def _ask_choice(self, title, label, options):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Delete"), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.add(Gtk.Label(label=label, xalign=0))
        combo = Gtk.ComboBoxText()
        for opt in options:
            combo.append_text(opt)
        combo.set_active(0)
        box.add(combo)
        dlg.show_all()
        resp = dlg.run()
        text = combo.get_active_text() or ""
        dlg.destroy()
        return text if resp == Gtk.ResponseType.OK else ""

    def load_profile(self, name):
        """Load the zones, rules and keys of a profile into the canvas as a draft."""
        try:
            zones, rules, ignore = read_profile(name, self.conf_path)
        except (ValueError, OSError, SystemExit) as exc:
            self.set_status(_("profile not read: {}").format(exc))
            return
        self.set_zones([z.copy() for z in zones])
        self.selected = 0 if self.zones else None
        self._set_extra("rules", rules)
        self._set_extra("ignore", ignore)
        self.select(self.selected)
        self.set_dirty(True)
        # A draft of this profile, not somebody's own layout: the Profiles menu marks it
        # until the first edit (set_dirty above cleared the mark, so set it afterwards).
        self.profile_source = name
        self.canvas.queue_draw()
        self.set_status(
            _("profile \"{}\": {} zones (don't forget to save)").format(name, len(self.zones))
        )

    def save_profile_dialog(self):
        if not self.zones:
            self.set_status(_("no zones - nothing to save"))
            return
        name = self._ask_text(_("Save profile"), _("Profile name"))
        if not name:
            return
        safe = sanitize_profile_name(name)
        if not safe:
            self.set_status(_("empty profile name"))
            return
        rules = self.extra_sections.get("rules", [])
        ignore = self.extra_sections.get("ignore", [])
        try:
            path = save_profile(safe, self.zones, rules, self.conf_path, ignore=ignore)
        except (ValueError, OSError) as exc:
            self.set_status(_("profile not saved: {}").format(exc))
            return
        self.set_status(_("profile \"{}\" saved: {}").format(safe, path))

    def workspace_profiles_dialog(self):
        """The "Workspace profiles" dialog: which profile each workspace uses.

        There are as many rows as there are workspaces in Cinnamon (`_NET_NUMBER_OF_DESKTOPS`),
        without X — four (:data:`zone_config.WORKSPACE_DESKTOPS_DEFAULT`). In
        the drop-down list — the saved profiles and the "default set" (no profile).
        Only the `[workspaces]` section is written, through `save_workspace_map`: foreign
        sections of the file stay in place, and `extra_sections` is re-read
        so that the next Ctrl+S does not roll it back.
        """
        try:
            mapping = load_workspace_map(self.conf_path)
        except (SystemExit, ValueError, OSError) as exc:
            self.set_status(_("workspace profiles not read: {}").format(exc))
            return
        rows = workspace_dialog_rows(mapping, list_profiles(self.conf_path), desktops_count())
        dlg = Gtk.Dialog(title=_("Workspace profiles"), transient_for=self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Save"), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, border_width=10)
        dlg.get_content_area().add(grid)
        hint = Gtk.Label(
            label=_(
                "A profile gives a workspace its zone grid; \"default set\" - a workspace without a profile."
            ),
            xalign=0,
        )
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 0, 0, 2, 1)
        combos = []
        line = 1
        for number, profile, options, warning in rows:
            grid.attach(Gtk.Label(label=_("Workspace {}").format(number), xalign=0), 0, line, 1, 1)
            combo = Gtk.ComboBoxText()
            for option in options:
                combo.append_text(option)
            combo.set_active(options.index(profile if profile is not None else WORKSPACE_SHARED))
            grid.attach(combo, 1, line, 1, 1)
            combos.append((number, combo))
            line += 1
            if warning:
                note = Gtk.Label(label=warning, xalign=0)
                note.set_line_wrap(True)
                note.set_max_width_chars(60)
                note.get_style_context().add_class("dim-label")
                grid.attach(note, 1, line, 1, 1)
                line += 1
        dlg.show_all()
        resp = dlg.run()
        chosen = {}
        for number, combo in combos:
            text = combo.get_active_text() or WORKSPACE_SHARED
            if text != WORKSPACE_SHARED:
                chosen[number] = text
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        try:
            save_workspace_map(self.conf_path, chosen)
        except (ValueError, OSError) as exc:
            self.set_status(_("workspace profiles not saved: {}").format(exc))
            return
        self.sync_workspace_extra()
        self.refresh_workspace_hint()
        self.set_status(
            _("workspace profiles saved: {} - bindings: {}").format(
                self.conf_path, len(chosen)
            )
        )

    def monitor_names(self):
        """The output names in the order of `xrandr --listmonitors` — the row order of the dialog.

        Read anew on every opening: a screen could have been connected after the editor
        started, and `self.outputs` is a snapshot from the moment of start. Without X and without xrandr
        the snapshot stays: the dialog shows the records from `[monitors]` anyway.
        """
        try:
            names = [name for name, _rect in zt.monitor_outputs()]
        except (SystemExit, OSError):
            names = []
        return names or [name for name, _rect in self.outputs]

    def monitor_profiles_dialog(self):
        """The "Monitor profiles" dialog: which profile each output uses.

        There are as many rows as there are outputs `xrandr --listmonitors` sees, plus the records
        from `[monitors]` about outputs that are not there right now: they are visible and can be fixed
        or removed. In the drop-down list — the saved profiles and the "default set"
        (no binding). The rows go in xrandr order, and that is the order the section is written in:
        the profile of the first screen settles the dispute on a merge (`profile use-monitors`).
        Only `[monitors]` is written, through `save_monitor_map`; foreign sections of the file
        stay in place, and `extra_sections` is re-read so that the next Ctrl+S
        does not roll it back. "Cancel" writes nothing.
        """
        try:
            mapping = load_monitor_map(self.conf_path)
        except (SystemExit, ValueError, OSError) as exc:
            self.set_status(_("monitor profiles not read: {}").format(exc))
            return
        rows = monitor_dialog_rows(
            mapping, list_profiles(self.conf_path), self.monitor_names()
        )
        if not rows:
            self.set_status(_("no monitor in sight - nothing to bind a profile to"))
            return
        dlg = Gtk.Dialog(title=_("Monitor profiles"), transient_for=self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Save"), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, border_width=10)
        dlg.get_content_area().add(grid)
        hint = Gtk.Label(
            label=_(
                "A profile gives a monitor its zone grid; \"default set\" - a monitor without a binding. The topmost monitor settles a dispute if profiles differ."
            ),
            xalign=0,
        )
        hint.set_line_wrap(True)
        hint.set_max_width_chars(60)
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 0, 0, 2, 1)
        combos = []
        line = 1
        for output, profile, options, warning, on_screen in rows:
            title = output if on_screen else f"{output} — {MONITOR_OFFLINE_NOTE}"
            grid.attach(Gtk.Label(label=title, xalign=0), 0, line, 1, 1)
            combo = Gtk.ComboBoxText()
            for option in options:
                combo.append_text(option)
            combo.set_active(options.index(profile if profile is not None else MONITOR_SHARED))
            grid.attach(combo, 1, line, 1, 1)
            combos.append((output, combo))
            line += 1
            if warning:
                note = Gtk.Label(label=warning, xalign=0)
                note.set_line_wrap(True)
                note.set_max_width_chars(60)
                note.get_style_context().add_class("dim-label")
                grid.attach(note, 1, line, 1, 1)
                line += 1
        dlg.show_all()
        resp = dlg.run()
        chosen = {}
        for output, combo in combos:
            text = combo.get_active_text() or MONITOR_SHARED
            if text != MONITOR_SHARED:
                chosen[output] = text
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        try:
            save_monitor_map(self.conf_path, chosen)
        except (ValueError, OSError) as exc:
            self.set_status(_("monitor profiles not saved: {}").format(exc))
            return
        self.sync_monitor_extra()
        self.set_status(
            _("monitor profiles saved: {} - bindings: {}").format(
                self.conf_path, len(chosen)
            )
        )

    def sync_workspace_extra(self):
        """Re-read `[workspaces]` into the "foreign" sections — so that Ctrl+S does not roll it back.

        The other sections are not touched in memory: `[rules]` may hold a
        draft loaded from a profile and not saved yet.
        """
        self.sync_extra_section(WORKSPACES_SECTION)

    def sync_monitor_extra(self):
        """Re-read `[monitors]` into the "foreign" sections — so that Ctrl+S does not roll it back."""
        self.sync_extra_section(MONITORS_SECTION)

    def sync_extra_section(self, section):
        """Update one foreign section in memory from the file, leaving the others alone."""
        try:
            body = load_extra_sections(self.conf_path, sections=[section]).get(section)
        except OSError:
            return
        if body:
            self.extra_sections[section] = body
        else:
            self.extra_sections.pop(section, None)

    def remove_profile_dialog(self):
        names = list_profiles(self.conf_path)
        if not names:
            return
        name = self._ask_choice(_("Delete profile"), _("Profile"), names)
        if not name:
            return
        try:
            remove_profile(name, self.conf_path)
        except (ValueError, OSError) as exc:
            self.set_status(_("profile not deleted: {}").format(exc))
            return
        self.set_status(_("profile \"{}\" deleted").format(name))

    def on_save(self):
        """Save the shared set and the monitor sections.

        ``False`` — the config was not written (rules refer to missing zones).
        """
        if not self.default_zones:
            self.set_status(_(
                "the default set [zones] has no zones - add a zone on a monitor that inherits it (or in its sections)"
            ))
            return False
        dangling = missing_rule_targets(self.extra_sections, self._all_zone_names())
        if dangling:
            self.set_status(_dangling_rules_status(dangling))
            return False
        plan = geom.save_plan(
            self.default_zones,
            self._all_drafts(),
            self.edited,
            onscreen={name for name, rect in self.combo_entries if rect is not None},
            existing=self.file_own,
        )
        try:
            geom.apply_save_plan(self.conf_path, self.default_zones, plan, self.extra_sections)
        except OSError as exc:
            self.set_status(_("write error: {}").format(exc))
            return False
        write, drop = plan
        self.file_own = (self.file_own | set(write)) - set(drop)
        self.edited.clear()
        self.set_dirty(False)
        self.set_status(
            _(
                "saved: {} - [zones]: {} zones, own sets: {}. Hotkeys are applied with the \"hotkeys\" button."
            ).format(self.conf_path, len(self.default_zones), len(write))
        )
        return True

    def on_hotkeys(self):
        if not self.on_save():
            return
        r = subprocess.run(
            [sys.executable, HOTKEYS, "--conf", self.conf_path],
            text=True,
            capture_output=True,
        )
        msg = (r.stdout or r.stderr).strip().splitlines()
        self.set_status(_("shortcuts: {}").format(msg[0] if msg else _("without output")))

    def rebuild_target_menu(self):
        """The "To monitor…" menu: a monitor → the zones of its set."""
        for child in self.target_box.get_children():
            self.target_box.remove(child)
        shown = 0
        for name, rect in self.combo_entries:
            if rect is None:
                continue  # a window cannot be placed into a disconnected output
            zones = self._draft(name)
            head = Gtk.Label(xalign=0)
            head.set_markup(f"<b>{name}</b> — {rect[2]}×{rect[3]}")
            self.target_box.pack_start(head, False, False, 0)
            if not zones:
                empty = Gtk.Label(label=_("   no zones"), xalign=0)
                empty.get_style_context().add_class("dim-label")
                self.target_box.pack_start(empty, False, False, 0)
            for z in zones:
                btn = Gtk.ModelButton()
                btn.set_label(f"{z.name}   {z.w:.0%}×{z.h:.0%}")
                btn.connect(
                    "clicked", lambda _w, zn=z.name, mn=name: self.apply_on_monitor(zn, mn)
                )
                self.target_box.pack_start(btn, False, False, 0)
            shown += 1
        if not shown:
            self.target_box.pack_start(Gtk.Label(label=_("no monitors"), xalign=0), False, False, 0)
        self.target_box.show_all()

    def apply_on_monitor(self, zone, monitor):
        """Place the active window into a zone of the monitor's set through ``apply --monitor``."""
        if self.dirty and not self.on_save():
            return
        self._apply(zone, monitor)

    def _apply(self, zone, monitor=None):
        """Run ``zone-tiler.py apply`` (with the monitor, if given) and report the result.

        ``--conf`` — the file opened in the editor, and ``--workspace conf``
        suppresses the "workspace → profile" mapping: the canvas shows the zones of
        ``zones.conf`` itself, so the check must count the same grid, otherwise the window
        would go to a zone from the profile of the current workspace.
        """
        cmd = [sys.executable, ZONEBIN, "apply", zone, "--verify",
               "--conf", self.conf_path, "--workspace", "conf"]
        if monitor:
            cmd += ["--monitor", monitor]
        r = subprocess.run(cmd, text=True, capture_output=True)
        out = (r.stdout or r.stderr).strip().replace("\n", " | ")
        if monitor:
            self.set_status(_("check \"{zone}\" on {monitor}: {out}").format(
                zone=zone, monitor=monitor, out=out))
        else:
            self.set_status(_("check \"{zone}\": {out}").format(zone=zone, out=out))

    def on_test(self):
        if self.selected is None:
            self.set_status(_("choose a zone"))
            return
        if not self.on_save():
            return
        self._apply(self.zones[self.selected].name)


# ---------------------------------------------------------------------- main


def theme_provider(theme):
    provider = Gtk.CssProvider()
    provider.load_from_data(theme.gtk_css().encode("utf-8"))
    return provider


def apply_theme(window, theme):
    """The editor's GTK sheet from the same tokens. A full GTK theme is a different product.

    A provider attached to a widget's style context styles that widget only, so the real window
    takes the sheet on its screen (all descendants follow). The `--shot` path moves the contents
    into a `Gtk.OffscreenWindow`, which is not on any screen — there the sheet goes on every
    widget of the tree (see :func:`apply_theme_tree`). The returned provider is kept by the
    editor, so a new palette is applied by reloading it instead of stacking a second one.
    """
    provider = theme_provider(theme)
    screen = window.get_screen()
    if screen is None:
        return None
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    return provider


def apply_theme_tree(root, theme):
    """The same sheet on every widget of a tree: what an offscreen snapshot needs."""
    provider = theme_provider(theme)
    stack = [root]
    while stack:
        widget = stack.pop()
        widget.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        if isinstance(widget, Gtk.Container):
            stack.extend(widget.get_children())


def main():
    Gtk.Window.set_default_icon_name("gridsnap")
    ap = argparse.ArgumentParser(description=_("Gridsnap - visual zone grid editor"))
    ap.add_argument("--conf", default=CONF)
    ap.add_argument(
        "--shot",
        help=_("save an interface snapshot to PNG and exit (for checking)"),
    )
    ap.add_argument("--monitor", type=int, default=0)
    args = ap.parse_args()
    theme = zone_theme.load(args.conf)

    if args.shot:
        win = Gtk.OffscreenWindow()
        win.set_size_request(1140, 720)
        ed = Editor(args.conf, theme)
        ed.select_monitor_index(args.monitor)
        # the editor is a window of its own; for a snapshot we move its contents offscreen
        child = ed.get_child()
        ed.remove(child)
        win.add(child)
        win.show_all()
        # OffscreenWindow is not on a screen, so the screen-wide sheet never reaches it: paint the
        # tree itself, which is also what the snapshot shows.
        apply_theme_tree(win, theme)
        for _unused in range(40):
            while Gtk.events_pending():
                Gtk.main_iteration()
        pixbuf = win.get_pixbuf()
        pixbuf.savev(args.shot, "png", [], [])
        print(_("snapshot saved: {}").format(args.shot))
        return

    ed = Editor(args.conf, theme)
    ed.select_monitor_index(args.monitor)
    ed.show_all()
    ed.connect("destroy", Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()

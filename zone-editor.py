#!/usr/bin/env python3
"""
zone-editor.py — визуальный редактор сетки зон для zone-tiler (Cinnamon, X11).

Что умеет:
  * показывает рабочую область монитора и зоны в натуральном масштабе;
  * зону можно тащить мышью, тянуть за край/угол — размер;
  * двойной клик по пустому месту — новая зона, Delete — удалить;
  * прилипание к краям экрана, к другим зонам и к линиям 1/4, 1/3, 1/2, 2/3, 3/4;
  * шаблоны раскладок (2 колонки, 1+2, 2×2, 3 колонки, главное + стек);
  * сохранение в zones.conf и регистрация горячих клавиш Cinnamon;
  * кнопка «Проверить на активном окне» — сразу вкладывает активное окно в зону.

Запуск: zone-editor.py [--conf ПУТЬ] [--shot ФАЙЛ.png]
"""

import argparse
import math
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
# PyGObject не поставляет стабы для проверки типов — импорт без проверки.
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402  # type: ignore

import zone_geom as geom  # noqa: E402
from zone_config import (  # noqa: E402
    Zone,
    list_profiles,
    load_extra_sections,
    load_zones,
    missing_rule_targets,
    read_profile,
    remove_profile,
    sanitize_name,
    sanitize_profile_name,
    save_profile,
    save_zones,
    unique_name,
    zt,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")
HOTKEYS = os.path.join(HERE, "setup-hotkeys.py")

PALETTE = [
    (0.36, 0.62, 0.92), (0.42, 0.78, 0.58), (0.92, 0.66, 0.36),
    (0.80, 0.50, 0.85), (0.42, 0.80, 0.85), (0.92, 0.52, 0.52),
    (0.72, 0.76, 0.40), (0.62, 0.62, 0.92),
]
ACCENT = (1.0, 0.71, 0.33)


CONF = zt.default_conf()


def templates():
    Q = "q"

    def mk(spec):
        out = []
        for i, (name, x, y, w, h) in enumerate(spec, start=1):
            out.append(Zone(name, x, y, w, h, f"<Super><Alt>{i}"))
        return out

    return {
        "2 колонки": mk([("left", 0, 0, 0.5, 1), ("right", 0.5, 0, 0.5, 1)]),
        "1 + 2": mk([
            ("main", 0, 0, 0.5, 1), ("tr", 0.5, 0, 0.5, 0.5), ("br", 0.5, 0.5, 0.5, 0.5),
        ]),
        "2 × 2": mk([
            (f"{Q}1", 0, 0, 0.5, 0.5), (f"{Q}2", 0.5, 0, 0.5, 0.5),
            (f"{Q}3", 0, 0.5, 0.5, 0.5), (f"{Q}4", 0.5, 0.5, 0.5, 0.5),
        ]),
        "3 колонки": mk([
            ("left", 0, 0, 1 / 3, 1), ("mid", 1 / 3, 0, 1 / 3, 1), ("right", 2 / 3, 0, 1 / 3, 1),
        ]),
        "главное + стек": mk([
            ("main", 0, 0, 0.66, 1), ("r1", 0.66, 0, 0.34, 1 / 3),
            ("r2", 0.66, 1 / 3, 0.34, 1 / 3), ("r3", 0.66, 2 / 3, 0.34, 1 / 3),
        ]),
        "чистый лист": [],
    }


# ------------------------------------------------------- проверка правил

DANGLING_SHOWN = 3  # сколько висячих правил перечислять в статусе, остальные — счётом


def _dangling_rules_status(dangling):
    """Текст статуса: почему не сохраняем и какие правила мешают."""
    shown = ", ".join(f"{sel} → {zone}" for sel, zone in dangling[:DANGLING_SHOWN])
    rest = len(dangling) - DANGLING_SHOWN
    tail = f" и ещё {rest}" if rest > 0 else ""
    return ("не сохраняю: правила из [rules] ссылаются на отсутствующие зоны — "
            f"{shown}{tail}. Верните зоны или поправьте [rules] в zones.conf.")


# -------------------------------------------------------------- рисование


def rounded_path(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


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

    # ---- геометрия (расчёты — в zone_geom, без GTK)

    def screen_rect(self):
        a = self.get_allocation()
        return geom.screen_rect(a.width, a.height, self.editor.workarea(), self.MARGIN)

    def to_px(self, z):
        return geom.to_px(self.screen_rect(), z)

    def to_frac(self, px, py):
        return geom.to_frac(self.screen_rect(), px, py)

    def hit(self, px, py):
        return geom.hit_test(px, py, self.screen_rect(), self.editor.zones)

    # ---- события

    def on_draw(self, widget, cr):
        cr.set_source_rgb(0.11, 0.12, 0.14)
        cr.paint()

        sx, sy, sw, sh = self.screen_rect()
        # «экран»
        rounded_path(cr, sx - 6, sy - 6, sw + 12, sh + 12, 10)
        cr.set_source_rgb(0.17, 0.18, 0.21)
        cr.fill()

        # подсказки-линии
        cr.set_line_width(1)
        for g in geom.GUIDES[1:-1]:
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.move_to(sx + g * sw, sy)
            cr.line_to(sx + g * sw, sy + sh)
            cr.move_to(sx, sy + g * sh)
            cr.line_to(sx + sw, sy + g * sh)
            cr.stroke()

        zones = self.editor.zones
        # крупные зоны рисуем первыми, мелкие поверх — так подписи не теряются
        order = sorted(range(len(zones)), key=lambda i: zones[i].w * zones[i].h, reverse=True)
        for i in order:
            z = zones[i]
            x, y, w, h = self.to_px(z)
            col = PALETTE[i % len(PALETTE)]
            rounded_path(cr, x + 1.5, y + 1.5, max(1, w - 3), max(1, h - 3), 8)
            cr.set_source_rgba(col[0], col[1], col[2], 0.30)
            cr.fill_preserve()
            cr.set_line_width(1.2)
            cr.set_source_rgba(col[0], col[1], col[2], 0.85)
            cr.stroke()
            cr.save()
            cr.rectangle(x + 2, y + 2, max(1, w - 4), max(1, h - 4))
            cr.clip()
            self.draw_label(cr, z, x, y, w, h)
            cr.restore()

        if self.editor.selected is not None and self.editor.selected < len(zones):
            z = zones[self.editor.selected]
            x, y, w, h = self.to_px(z)
            rounded_path(cr, x + 1.5, y + 1.5, max(1, w - 3), max(1, h - 3), 8)
            cr.set_line_width(2.4)
            cr.set_source_rgba(*ACCENT, 1)
            cr.stroke()

        # рамка рабочей области
        cr.set_line_width(1.6)
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.rectangle(sx, sy, sw, sh)
        cr.stroke()

        # легенда
        cr.set_source_rgba(1, 1, 1, 0.45)
        lay = widget.create_pango_layout(
            f"рабочая область {self.editor.workarea()[2]}×{self.editor.workarea()[3]} px · "
            "тащите зону мышью, тяните за край — размер, двойной клик — новая зона"
        )
        lay.set_font_description(Pango.FontDescription("Sans 9"))
        cr.move_to(sx, sy + sh + 8)
        PangoCairo.show_layout(cr, lay)

    def draw_label(self, cr, z, x, y, w, h):
        if w < 34 or h < 22:
            return
        pad = 9
        inner = max(10, w - 2 * pad)
        layout = self.create_pango_layout("")
        layout.set_width(int(inner * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_font_description(Pango.FontDescription("Sans Bold 11"))
        layout.set_text(z.name, -1)
        cr.set_source_rgba(1, 1, 1, 0.92)
        cr.move_to(x + pad, y + pad - 2)
        PangoCairo.show_layout(cr, layout)

        if h > 46:
            sub = f"{z.w:.0%} × {z.h:.0%}"
            if z.binding:
                sub += f" · {z.binding}"
            layout2 = self.create_pango_layout("")
            layout2.set_width(int(inner * Pango.SCALE))
            layout2.set_ellipsize(Pango.EllipsizeMode.END)
            layout2.set_font_description(Pango.FontDescription("Sans 8"))
            layout2.set_text(sub, -1)
            cr.set_source_rgba(1, 1, 1, 0.62)
            cr.move_to(x + pad, y + pad + 16)
            PangoCairo.show_layout(cr, layout2)

    # ---- мышь

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

    # ---- логика перетаскивания (отдельно от событий: так её можно тестировать)

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

    # ---- клавиатура

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
        # колесо мыши меняет ширину выделенной зоны
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


# ------------------------------------------------------------------- окно


class Editor(Gtk.Window):
    def __init__(self, conf_path):
        super().__init__(title="Gridsnap — сетка окон")
        self.conf_path = conf_path
        self.set_icon_name("gridsnap")
        self.monitors = zt.monitors()
        self.monitor_index = 0
        self.zones = load_zones(conf_path)
        self.extra_sections = load_extra_sections(conf_path)
        self.selected = 0 if self.zones else None
        self.dirty = False
        self._loading = False
        self.set_default_size(1120, 700)
        self.set_border_width(10)
        self.connect("key-press-event", self.on_key)
        self.build_ui()
        self.refresh_panel()
        self.canvas.queue_draw()

    # ---- разметка

    def build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(root)

        bar = Gtk.Toolbar()
        bar.set_style(Gtk.ToolbarStyle.ICONS)
        defs = [
            ("list-add-symbolic", "Добавить зону", self.on_add),
            ("list-remove-symbolic", "Удалить зону (Delete)", self.on_remove),
            ("edit-copy-symbolic", "Дублировать зону", self.on_duplicate),
            ("edit-clear-all-symbolic", "Очистить всё", self.on_clear),
            ("document-save-symbolic", "Сохранить в zones.conf (Ctrl+S)", self.on_save),
            ("emblem-synchronizing-symbolic", "Применить сочетания клавиш в Cinnamon", self.on_hotkeys),
            ("media-playback-start-symbolic", "Вложить активное окно в выделенную зону", self.on_test),
        ]
        for icon, tip, cb in defs:
            btn = Gtk.ToolButton()
            btn.set_icon_name(icon)
            btn.set_tooltip_text(tip)
            btn.connect("clicked", lambda _b, f=cb: f())
            bar.insert(btn, -1)
        bar.insert(Gtk.SeparatorToolItem(), -1)
        self.tpl_btn = Gtk.MenuButton()
        self.tpl_btn.set_tooltip_text("Шаблоны раскладок")
        self.tpl_btn.set_label("Шаблоны")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        for name, zones in templates().items():
            b = Gtk.ModelButton()
            b.set_label(name)
            b.connect("clicked", lambda _w, t=zones, n=name: self.apply_template(t, n))
            box.pack_start(b, False, False, 0)
        pop.add(box)
        box.show_all()
        self.tpl_btn.set_popover(pop)
        item = Gtk.ToolItem()
        item.add(self.tpl_btn)
        bar.insert(item, -1)
        self.prof_btn = Gtk.MenuButton()
        self.prof_btn.set_tooltip_text("Профили раскладок (зоны + правила + клавиши)")
        self.prof_btn.set_label("Профили")
        self.prof_pop = Gtk.Popover()
        self.prof_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.prof_box.set_border_width(8)
        self.prof_pop.add(self.prof_box)
        self.prof_btn.set_popover(self.prof_pop)
        self.prof_btn.connect("clicked", lambda _b: self.rebuild_profiles_menu())
        pitem = Gtk.ToolItem()
        pitem.add(self.prof_btn)
        bar.insert(pitem, -1)
        root.pack_start(bar, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        root.pack_start(body, True, True, 0)

        frame = Gtk.Frame(label="Экран")
        frame.set_shadow_type(Gtk.ShadowType.IN)
        self.canvas = Canvas(self)
        self.canvas.connect("button-press-event", self.canvas.on_button_press)
        self.canvas.connect("button-release-event", self.canvas.on_button_release)
        self.canvas.connect("motion-notify-event", self.canvas.on_motion)
        self.canvas.connect("key-press-event", self.canvas.on_key_press)
        self.canvas.connect("scroll-event", self.canvas.on_scroll)
        frame.add(self.canvas)
        body.pack_start(frame, True, True, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.set_size_request(300, -1)
        body.pack_start(side, False, False, 0)

        side.pack_start(Gtk.Label(label="Зоны", xalign=0), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(220)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self.on_row_selected)
        scroller.add(self.listbox)
        side.pack_start(scroller, True, True, 0)

        props = Gtk.Frame(label="Выделенная зона")
        pbox = Gtk.Grid(row_spacing=6, column_spacing=8, border_width=8)
        props.add(pbox)

        pbox.attach(Gtk.Label(label="Имя", xalign=0), 0, 0, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.connect("changed", self.on_name_changed)
        pbox.attach(self.name_entry, 1, 0, 1, 1)

        pbox.attach(Gtk.Label(label="Клавиши", xalign=0), 0, 1, 1, 1)
        self.key_entry = Gtk.Entry()
        self.key_entry.set_placeholder_text("<Super><Alt>5")
        self.key_entry.connect("changed", self.on_key_changed)
        pbox.attach(self.key_entry, 1, 1, 1, 1)

        pbox.attach(Gtk.Label(label="Монитор", xalign=0), 0, 2, 1, 1)
        self.mon_combo = Gtk.ComboBoxText()
        for i, (mx, my, mw, mh) in enumerate(self.monitors):
            self.mon_combo.append_text(f"{i}: {mw}×{mh} в {mx},{my}")
        self.mon_combo.set_active(0)
        self.mon_combo.connect("changed", self.on_monitor_changed)
        pbox.attach(self.mon_combo, 1, 2, 1, 1)

        self.geom_label = Gtk.Label(xalign=0)
        pbox.attach(self.geom_label, 0, 3, 2, 1)
        side.pack_start(props, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim-label")
        root.pack_start(self.status, False, False, 0)

    # ---- состояние

    def workarea(self):
        return self.monitors[self.monitor_index]

    def set_status(self, text):
        self.status.set_text(text)

    def set_dirty(self, val):
        self.dirty = val
        self.set_title(("● " if val else "") + "Gridsnap — сетка окон")

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
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.set_border_width(4)
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(10, 10)
            col = PALETTE[i % len(PALETTE)]
            swatch.connect("draw", self._draw_swatch, col)
            name = Gtk.Label(label=z.name, xalign=0)
            size = Gtk.Label(label=f"{z.w:.0%}×{z.h:.0%}")
            size.get_style_context().add_class("dim-label")
            box.pack_start(swatch, False, False, 0)
            box.pack_start(name, True, True, 0)
            box.pack_end(size, False, False, 0)
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
        self._loading = False

    def update_geom_label(self):
        if self.selected is None or self.selected >= len(self.zones):
            self.geom_label.set_text("—")
            return
        z = self.zones[self.selected]
        _, _, ww, wh = self.workarea()
        self.geom_label.set_text(
            f"x={z.x:.3f} y={z.y:.3f}  {z.w:.3f}×{z.h:.3f}\n"
            f"≈ {round(ww * z.x)},{round(wh * z.y)} {round(ww * z.w)}×{round(wh * z.h)} px"
        )

    @staticmethod
    def _draw_swatch(widget, cr, col):
        a = widget.get_allocation()
        rounded_path(cr, 1, 1, a.width - 2, a.height - 2, 3)
        cr.set_source_rgb(*col)
        cr.fill()

    # ---- обработчики панели

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
        self.zones[self.selected].binding = entry.get_text().strip()
        self.set_dirty(True)
        self.canvas.queue_draw()

    def on_monitor_changed(self, combo):
        if self._loading:
            return
        self.monitor_index = combo.get_active()
        self.canvas.queue_draw()
        self.refresh_panel()

    def on_key(self, _w, ev):
        if ev.state & Gdk.ModifierType.CONTROL_MASK and ev.keyval == Gdk.KEY_s:
            self.on_save()
            return True
        return False

    # ---- действия

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
        self.zones = []
        self.selected = None
        self.select(None)
        self.set_dirty(True)
        self.canvas.queue_draw()

    def apply_template(self, zones, name):
        self.zones = [z.copy() for z in zones]
        self.selected = 0 if self.zones else None
        self.select(self.selected)
        self.set_dirty(True)
        self.canvas.queue_draw()
        self.set_status(f"шаблон «{name}»: {len(self.zones)} зон (не забудьте сохранить)")

    # ---- профили

    def rebuild_profiles_menu(self):
        """Пересобрать меню «Профили»: список сохранённых плюс сохранение и удаление."""
        for child in self.prof_box.get_children():
            self.prof_box.remove(child)
        names = list_profiles(self.conf_path)
        for name in names:
            b = Gtk.ModelButton()
            b.set_label(name)
            b.connect("clicked", lambda _w, n=name: self.load_profile(n))
            self.prof_box.pack_start(b, False, False, 0)
        if names:
            self.prof_box.pack_start(Gtk.Separator(), False, False, 2)
        save_btn = Gtk.ModelButton()
        save_btn.set_label("Сохранить текущую раскладку как профиль…")
        save_btn.connect("clicked", lambda _w: self.save_profile_dialog())
        self.prof_box.pack_start(save_btn, False, False, 0)
        if names:
            rm_btn = Gtk.ModelButton()
            rm_btn.set_label("Удалить профиль…")
            rm_btn.connect("clicked", lambda _w: self.remove_profile_dialog())
            self.prof_box.pack_start(rm_btn, False, False, 0)
        self.prof_box.show_all()

    def _ask_text(self, title, label, initial=""):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dlg.add_button("Сохранить", Gtk.ResponseType.OK)
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
        dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dlg.add_button("Удалить", Gtk.ResponseType.OK)
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
        """Загрузить зоны, правила и клавиши профиля в холст как черновик."""
        try:
            zones, rules = read_profile(name, self.conf_path)
        except (ValueError, OSError, SystemExit) as exc:
            self.set_status(f"профиль не прочитан: {exc}")
            return
        self.zones = [z.copy() for z in zones]
        self.selected = 0 if self.zones else None
        if rules:
            self.extra_sections["rules"] = rules
        else:
            self.extra_sections.pop("rules", None)
        self.select(self.selected)
        self.set_dirty(True)
        self.canvas.queue_draw()
        self.set_status(f"профиль «{name}»: {len(self.zones)} зон (не забудьте сохранить)")

    def save_profile_dialog(self):
        if not self.zones:
            self.set_status("нет зон — сохранять нечего")
            return
        name = self._ask_text("Сохранить профиль", "Имя профиля")
        if not name:
            return
        safe = sanitize_profile_name(name)
        if not safe:
            self.set_status("пустое имя профиля")
            return
        rules = self.extra_sections.get("rules", [])
        try:
            path = save_profile(safe, self.zones, rules, self.conf_path)
        except (ValueError, OSError) as exc:
            self.set_status(f"профиль не сохранён: {exc}")
            return
        self.set_status(f"профиль «{safe}» сохранён: {path}")

    def remove_profile_dialog(self):
        names = list_profiles(self.conf_path)
        if not names:
            return
        name = self._ask_choice("Удалить профиль", "Профиль", names)
        if not name:
            return
        try:
            remove_profile(name, self.conf_path)
        except (ValueError, OSError) as exc:
            self.set_status(f"профиль не удалён: {exc}")
            return
        self.set_status(f"профиль «{name}» удалён")

    def on_save(self):
        """Сохранить раскладку. ``False`` — конфиг не записан (нет зон или висячие правила)."""
        if not self.zones:
            self.set_status("нет зон — сохранять нечего")
            return False
        dangling = missing_rule_targets(self.extra_sections, {z.name for z in self.zones})
        if dangling:
            self.set_status(_dangling_rules_status(dangling))
            return False
        try:
            save_zones(self.conf_path, self.zones, self.extra_sections)
        except OSError as exc:
            self.set_status(f"ошибка записи: {exc}")
            return False
        self.set_dirty(False)
        self.set_status(f"сохранено: {self.conf_path} ({len(self.zones)} зон). "
                        "Клавиши применяются кнопкой «сочетания клавиш».")
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
        self.set_status(f"сочетания: {msg[0] if msg else 'без вывода'}")

    def on_test(self):
        if self.selected is None:
            self.set_status("выберите зону")
            return
        if not self.on_save():
            return
        z = self.zones[self.selected]
        r = subprocess.run(
            [sys.executable, ZONEBIN, "apply", z.name, "--verify"],
            text=True,
            capture_output=True,
        )
        out = (r.stdout or r.stderr).strip().replace("\n", " | ")
        self.set_status(f"проверка «{z.name}»: {out}")


# ---------------------------------------------------------------------- main


def main():
    Gtk.Window.set_default_icon_name("gridsnap")
    ap = argparse.ArgumentParser(description="Gridsnap — визуальный редактор сетки зон")
    ap.add_argument("--conf", default=CONF)
    ap.add_argument("--shot", help="сохранить снимок интерфейса в PNG и выйти (для проверки)")
    ap.add_argument("--monitor", type=int, default=0)
    args = ap.parse_args()

    if args.shot:
        win = Gtk.OffscreenWindow()
        win.set_size_request(1140, 720)
        ed = Editor(args.conf)
        # редактор — отдельное окно; для снимка переносим его содержимое в offscreen
        child = ed.get_child()
        ed.remove(child)
        win.add(child)
        win.show_all()
        for _ in range(40):
            while Gtk.events_pending():
                Gtk.main_iteration()
        pixbuf = win.get_pixbuf()
        pixbuf.savev(args.shot, "png", [], [])
        print(f"снимок сохранён: {args.shot}")
        return

    ed = Editor(args.conf)
    ed.show_all()
    ed.connect("destroy", Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()

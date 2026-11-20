#!/usr/bin/env python3
"""zone-picker.py — визуальный выбор зоны для GridSnap (как gTile).

По горячей клавише (в Cinnamon заводится на ``Super+Z``, см. README) поверх
рабочего стола показывается оверлей с зонами из ``zones.conf``. Зона под
курсором и выделение подсвечиваются; стрелки двигают выделение, ``Enter``
вкладывает активное окно в выбранную зону, ``Esc`` отменяет.

    python3 zone-picker.py                 # интерактивный оверлей
    python3 zone-picker.py --shot out.png  # отрисовать оверлей в PNG и выйти
    python3 zone-picker.py --dry-run       # выбрать зону, но окно не двигать

Чистая логика навигации — в ``zone_picker_nav`` (без GTK и X), применение окна —
через ``zone-tiler.apply_zone``/``Grid.rect``. GTK и cairo живут только здесь.
"""

import argparse
import math
import os
import sys

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402  # type: ignore

import zone_config as zc  # noqa: E402
import zone_picker_nav as nav  # noqa: E402

zt = zc.zt  # zone-tiler.py, загруженный zone_config (парсер конфига и раскладка)

PALETTE = [
    (0.36, 0.62, 0.92), (0.42, 0.78, 0.58), (0.92, 0.66, 0.36),
    (0.80, 0.50, 0.85), (0.42, 0.80, 0.85), (0.92, 0.52, 0.52),
    (0.72, 0.76, 0.40), (0.62, 0.62, 0.92),
]
ACCENT = (1.0, 0.71, 0.33)
BACKDROP = (0.07, 0.08, 0.10)
HINT = "стрелки — выбор · Enter — вложить · Esc — отмена"

EXIT_OK = 0
EXIT_FAIL = 1


def fail(message, code=EXIT_FAIL):
    print(message, file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------- рисование


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
    layout.set_font_description(Pango.FontDescription(("Sans Bold " if bold else "Sans ") + str(size)))
    if width:
        layout.set_width(int(max(1, width) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def draw_overlay(cr, width, height, zones, selected):
    """Нарисовать оверлей: фон, зоны, подпись каждой, подсветка выделения и подсказка.

    Координаты — в пикселях локального оверлея (он же — рабочая область монитора),
    зоны заданы долями, поэтому ``zone_px`` просто умножает их на размеры.
    """
    cr.set_source_rgba(*BACKDROP, 0.90)
    cr.paint()

    # крупные зоны первыми, мелкие поверх — как в редакторе
    order = sorted(range(len(zones)), key=lambda i: zones[i].w * zones[i].h, reverse=True)
    for i in order:
        z = zones[i]
        x, y, w, h = z.x * width, z.y * height, z.w * width, z.h * height
        col = PALETTE[i % len(PALETTE)]
        _rounded(cr, x + 2, y + 2, max(1, w - 4), max(1, h - 4), 10)
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
        _rounded(cr, x + 2, y + 2, max(1, w - 4), max(1, h - 4), 10)
        cr.set_source_rgba(*ACCENT, 0.16)
        cr.fill_preserve()
        cr.set_line_width(3.0)
        cr.set_source_rgba(*ACCENT, 1.0)
        cr.stroke()

    _label(cr, 16, height - 30, HINT, 11, False, (1, 1, 1, 0.72))


def write_shot(path, width, height, zones, selected):
    """Отрисовать оверлей в PNG заданного размера — проверка без интерактива."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*BACKDROP)  # непрозрачная подложка, чтобы снимок читался
    cr.paint()
    draw_overlay(cr, width, height, zones, selected)
    surface.write_to_png(path)


# ------------------------------------------------------- выбор монитора и окна


def active_window_quiet():
    """Активное окно как id или None, если его нет/X недоступен (без SystemExit)."""
    try:
        return zt.active_window()
    except SystemExit:
        return None


def pointer_point():
    """Абсолютная позиция курсора (x, y) или None, если её не спросить."""
    try:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pos = seat.get_pointer().get_position()
        return (int(pos[-2]), int(pos[-1]))
    except Exception:
        return None


def pick_monitor(monitors, win_id, args):
    """Индекс монитора: явный ``--monitor``, иначе монитор окна, иначе под курсором."""
    if args.monitor is not None:
        return args.monitor
    if win_id is not None:
        wa = zt.window_monitor(win_id, monitors)
        if wa is not None:
            return monitors.index(wa)
    point = pointer_point()
    if point is not None:
        return monitors.index(zt.monitor_for_point(monitors, point))
    return 0


def select_by_name(zones, name):
    """Индекс зоны с именем ``name``; если такой нет — первый."""
    if name:
        for i, z in enumerate(zones):
            if z.name == name:
                return i
    return nav.first_index(zones)


# ------------------------------------------------------------ интерактивный оверлей


class Picker:
    def __init__(self, zones, grid, wa, mon_index, args, win_id):
        self.zones = zones
        self.grid = grid
        self.wa = wa
        self.mon_index = mon_index
        self.args = args
        self.win_id = win_id
        self.exit_code = EXIT_OK

        width, height = wa[2], wa[3]
        self.selected = self._initial_selection(width, height)
        self._seat = None
        self._grab_tries = 0

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("Gridsnap — выбор зоны")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(True)
        self.win.set_focus_on_map(True)
        self.win.set_app_paintable(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.win.move(wa[0], wa[1])
        self.win.set_default_size(width, height)
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

    # ---- состояние

    def _initial_selection(self, width, height):
        if self.args.select:
            return select_by_name(self.zones, self.args.select)
        point = pointer_point()
        if point is not None:
            fx = (point[0] - self.wa[0]) / float(max(1, width))
            fy = (point[1] - self.wa[1]) / float(max(1, height))
            under = nav.zone_at(self.zones, fx, fy)
            if under is not None:
                return under
        return nav.first_index(self.zones)

    # ---- клавиатура

    def try_grab(self):
        """Взять клавиатуру на оверлей (DOCK-окно фокус WM не отдаёт)."""
        self._grab_tries += 1
        try:
            display = Gdk.Display.get_default()
            seat = display.get_default_seat()
            status = seat.grab(self.win.get_window(), Gdk.SeatCapabilities.KEYBOARD, True)
            if status == Gdk.GrabStatus.SUCCESS:
                self._seat = seat
                return False
        except Exception as exc:  # окно могло быть не готово — пробуем ещё раз
            if self._grab_tries >= 10:
                print(f"zone-picker: клавиатуру взять не удалось: {exc}", file=sys.stderr)
        return self._grab_tries < 10

    def release_grab(self):
        try:
            if self._seat is not None:
                self._seat.ungrab()
        except Exception as exc:
            print(f"zone-picker: не удалось отпустить клавиатуру: {exc}", file=sys.stderr)
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

    # ---- мышь

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
        draw_overlay(cr, alloc.width, alloc.height, self.zones, self.selected)
        return False

    def on_destroy(self, _widget):
        Gtk.main_quit()

    # ---- применение и выход

    def apply_selected(self):
        if self.selected is None:
            return
        name = self.zones[self.selected].name
        rect = self.grid.rect(name, self.wa)
        x, y, w, h = rect
        if self.args.dry_run:
            print(f"{name}: x={x} y={y} {w}x{h} (монитор {self.mon_index})")
            return
        win = self.win_id if self.win_id is not None else active_window_quiet()
        if win is None:
            print("zone-picker: нет активного окна", file=sys.stderr)
            self.exit_code = EXIT_FAIL
            return
        before = None
        try:
            before = zt.snapshot_window(win, zone=name)
        except Exception:
            before = None  # геометрию не прочитать — без записи в историю
        zt.apply_zone(win, rect, label=name, verify=self.args.verify, quiet=self.args.quiet)
        if before is not None:
            zt.remember("apply", [before], quiet=self.args.quiet)

    def quit(self, apply=False):
        if apply:
            self.apply_selected()
        self.release_grab()
        self.win.destroy()
        Gtk.main_quit()


# ------------------------------------------------------------------------- CLI


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="zone-picker.py",
        description="Оверлей визуального выбора зоны: стрелки, Enter — вложить, Esc — отмена.",
    )
    ap.add_argument("--conf", metavar="PATH", help="конфиг зон (по умолчанию ~/.config/gridsnap/zones.conf)")
    ap.add_argument("--shot", metavar="FILE.png", help="отрисовать оверлей в PNG и выйти (без интерактива)")
    ap.add_argument("--dry-run", action="store_true", help="не двигать окно, только показать выбранную зону")
    ap.add_argument("--window", type=lambda s: int(s, 0), metavar="XID", help="окно для вложения (по умолчанию активное)")
    ap.add_argument("--monitor", type=int, metavar="N", help="монитор (по умолчанию — монитор активного окна)")
    ap.add_argument("--select", metavar="ЗОНА", help="начальное выделение по имени зоны")
    ap.add_argument("--verify", action="store_true", help="сверить рамку окна после вложения")
    ap.add_argument("--quiet", action="store_true", help="не печатать строку о вложении")
    ap.add_argument("--version", action="version", version=f"zone-picker {zt.VERSION}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    conf = args.conf or zt.default_conf()
    if not os.path.exists(conf):
        fail(f"zone-picker: не найден конфиг {conf}")
    grid = zt.Grid(conf)
    zones = zc.load_zones(conf)
    if not zones:
        fail("zone-picker: в конфиге нет ни одной зоны")

    try:
        Gtk.init()
    except Exception as exc:
        fail(f"zone-picker: не удалось открыть дисплей: {exc}")
    monitors = zt.monitors()
    win_id = args.window
    if win_id is None and not args.shot:
        win_id = active_window_quiet()
    mon_index = pick_monitor(monitors, win_id, args)
    if not 0 <= mon_index < len(monitors):
        fail(f"zone-picker: монитора {mon_index} нет")
    wa = monitors[mon_index]

    if args.shot:
        selected = select_by_name(zones, args.select)
        write_shot(args.shot, wa[2], wa[3], zones, selected)
        print(f"zone-picker: снимок оверлея записан в {args.shot} ({wa[2]}x{wa[3]}, зон {len(zones)})")
        return EXIT_OK

    picker = Picker(zones, grid, wa, mon_index, args, win_id)
    Gtk.main()
    return picker.exit_code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""zone-picker.py — визуальный выбор зоны для GridSnap (как gTile).

По горячей клавише (в Cinnamon заводится на ``Super+Z``, см. README) поверх
рабочего стола показывается оверлей с зонами из ``zones.conf``. Зона под
курсором и выделение подсвечиваются; стрелки двигают выделение, ``Enter``
вкладывает активное окно в выбранную зону, ``Esc`` отменяет.

    python3 zone-picker.py                 # интерактивный оверлей
    python3 zone-picker.py --shot out.png  # отрисовать оверлей в PNG и выйти
    python3 zone-picker.py --dry-run       # выбрать зону, но окно не двигать
    python3 zone-picker.py --workspace 2   # сетка рабочего стола 2 (профиль из [workspaces])
    python3 zone-picker.py --hud --at 100,100        # HUD-сетка: зона под точкой, и выход
    python3 zone-picker.py --hud --follow            # HUD-сетка: точки «X Y» из stdin

Режим ``--hud`` — пассивная сетка для перетаскивания окна: подсвечены контуры
всех зон и зона под точкой, которую присылает клиент (watcher перетаскивания).
HUD не берёт фокус, не захватывает клавиатуру и прозрачен для ввода: во время
перетаскивания ввод держит muffin, а мы только рисуем.

Сетка берётся для экрана окна/курсора тем же резолвером, что у раскладчика
(``zone-tiler.grid_for_output``): профиль из ``[monitors]`` → профиль
``[workspaces]`` → активный ``zones.conf``. Так оверлей показывает ровно те зоны,
которые положит демон/``apply``, а не набор активного конфига; внутри сетки
монитор может сузить набор своим ``[zones:<выход>]``. Экран без привязки живёт
сеткой своего стола, как раньше.

Чистая логика навигации — в ``zone_picker_nav`` (без GTK и X), применение окна —
через ``zone-tiler.apply_zone``/``Grid.rect``. GTK и cairo живут только здесь.

Два пространства пикселей (см. ``zone_units``): арифметика зон, ``monitors()``,
точка ``--at`` и строки stdin у ``--follow`` — **физические** пиксели X, а всё,
что задаёт GTK-окно (``move``/``set_default_size``) и отдаёт указатель GDK, —
**логические**. Перевод в одну сторону делает только ``zone_units``; здесь он
вызывается в трёх местах: рамка окна оверлея/HUD, первое выделение под курсором
и монитор под курсором.
"""

import argparse
import math
import os
import signal
import sys

import cairo

# X11-бэкенд GDK включается до первого обращения к GDK (см. `window_backend`):
# в Wayland-сессии оверлей и HUD обязаны жить в координатах Xwayland, иначе
# `move()` превращается в ничто, а размер окна считает другая линейка.
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
import zone_units as zu  # noqa: E402  (перевод логических пикселей GTK в физические X)
from zone_i18n import _  # noqa: E402  (там же: строки интерфейса и их перевод)

zt = zc.zt  # zone-tiler.py, загруженный zone_config (парсер конфига и раскладка)

PALETTE = [
    (0.36, 0.62, 0.92), (0.42, 0.78, 0.58), (0.92, 0.66, 0.36),
    (0.80, 0.50, 0.85), (0.42, 0.80, 0.85), (0.92, 0.52, 0.52),
    (0.72, 0.76, 0.40), (0.62, 0.62, 0.92),
]
ACCENT = (1.0, 0.71, 0.33)
BACKDROP = (0.07, 0.08, 0.10)
HINT = _("arrows - select · Enter - place · Esc - cancel")
# Подложка HUD и пауза однократной отрисовки: сетку видно, рабочий стол — тоже,
# а сам процесс после кадра не висит.
HUD_ALPHA = 0.25
HUD_HOLD_MS = 500

EXIT_OK = 0
EXIT_FAIL = 1


def fail(message, code=EXIT_FAIL):
    print(message, file=sys.stderr)
    sys.exit(code)


# -------------------------------------------------------- сетка рабочего стола


def zone_bindings(grid):
    """Привязки зон из ``[keys]`` сетки: имя зоны -> сочетание клавиш.

    ``[keys]`` лежит в том же файле, что и набор зон, поэтому профиль стола
    приносит и свои сочетания. Секции нет — привязок нет: подпись зоны просто
    не покажет клавишу.
    """
    if not grid.conf.has_section("keys"):
        return {}
    return {name.strip(): value.strip() for name, value in grid.conf.items("keys")}


def zones_from_grid(grid, output=None):
    """Зоны набора монитора из уже выбранной сетки (с привязками ``[keys]``).

    Имена и доли берутся у одного и того же ``Grid``: у монитора может быть свой
    набор ``[zones:<выход>]``, а у стола — свой профиль, и оверлей обязан рисовать
    ровно то, что печатает ``list``. Отдельное чтение файла тут не годится: имена
    зон разошлись бы с геометрией, которой пикер двигает окно.
    """
    keys = zone_bindings(grid)
    zones = []
    for name in grid.zones_for(output):
        x, y, w, h = grid.fractions(name, output)
        zones.append(zc.Zone(name, x, y, w, h, keys.get(name, "")))
    return zones


def screen_grid(conf, output, workspace):
    """Сетка экрана и профили, которые её дали: ``(Grid, экран, стол)``.

    Порядок разбора не дублируется, а берётся у раскладчика
    (:func:`zone-tiler.grid_for_output`): ``[monitors]`` → ``[workspaces]`` →
    активный ``zones.conf``. Сетка стола (``grid_for_workspace``) идёт готовым
    ``fallback``: экран без привязки живёт ею, как жил до привязки профилей к
    экранам. Профиля экрана нет на диске — ``grid_for_output`` сам печатает
    предупреждение в stderr и отдаёт сетку стола, то есть оверлей не отказывает.

    ``screen`` — имя профиля экрана, если он реально дал сетку, иначе ``None``;
    ``desk`` — профиль стола из ``[workspaces]``. По ним строятся подписи: без
    разделения «экран …» и «стол …» подпись врала бы о происхождении сетки.
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
    """Подпись к сетке: какой стол её дал и почему набор зон такой.

    Пусто, если стол неизвестен (нет ``_NET_CURRENT_DESKTOP``): тогда работает
    общий ``[zones]``, и подписывать нечего.
    """
    if workspace == zt.WORKSPACE_CONF:
        return _("zones.conf, without workspace profiles")
    if not isinstance(workspace, int):
        return ""
    if profile:
        return _("workspace {} · profile \"{}\"").format(workspace + 1, profile)
    return _("workspace {} · default set").format(workspace + 1)


def screen_caption(workspace, profile, output, screen_profile):
    """Подпись к сетке: профиль экрана, если он дал сетку, иначе подпись стола.

    Экран важнее стола (:func:`zone_config.resolve_profile`), поэтому и подпись
    называет экран первым: по ней в ``--shot``/``--dry-run`` видно, что оверлей
    нарисовал зоны того же профиля, что положит раскладчик на этом экране.
    """
    if screen_profile:
        return _("monitor {} · profile \"{}\"").format(output, screen_profile)
    return workspace_caption(workspace, profile)


def hud_caption(profile, output=None, screen_profile=None):
    """Подпись HUD: чей профиль дал сетку — экрана или стола.

    В HUD нет подсказки про клавиши (их он не слушает), поэтому в углу остаётся
    только ответ на вопрос «какая это сетка» — та же строка профиля, что и в
    интерактивном оверлее, но без номера стола: рисовать больше нечего.
    """
    if screen_profile:
        return _("monitor {} · profile \"{}\"").format(output, screen_profile)
    return _("profile \"{}\"").format(profile) if profile else ""


def describe_grid(zones, caption):
    """Строка про показанную сетку: подпись стола и перечень зон.

    Нужна ``--dry-run`` и ``--shot``: по ней видно, какая сетка нарисована, не
    разглядывая оверлей (это и проверяет ``tests/smoke-picker-workspace.sh``).
    """
    if caption:
        return _("{caption} · zones {count}: {names}").format(
            caption=caption, count=len(zones), names=", ".join(z.name for z in zones)
        )
    return _("zones {count}: {names}").format(
        count=len(zones), names=", ".join(z.name for z in zones)
    )


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


def draw_zones(cr, width, height, zones, selected, caption="", hint=HINT):
    """Нарисовать сетку без фона: зоны, подписи, подсветка, стол и подсказка.

    Координаты — в пикселях локального оверлея (он же — рабочая область монитора),
    зоны заданы долями, поэтому ``zone_px`` просто умножает их на размеры. Фон
    рисует вызывающий: интерактивному оверлею нужна непрозрачная подложка, а HUD —
    полупрозрачная. Пустой ``hint`` — подсказки про клавиши нет (HUD их не слушает).
    """
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

    # почему сетка такая: стол и его профиль из `[workspaces]`
    if caption:
        _label(cr, 16, height - (48 if hint else 30), caption, 11, True, (*ACCENT, 0.92))
    if hint:
        _label(cr, 16, height - 30, hint, 11, False, (1, 1, 1, 0.72))


def draw_overlay(cr, width, height, zones, selected, caption=""):
    """Интерактивный оверлей целиком: непрозрачная подложка и сетка поверх неё."""
    cr.set_source_rgba(*BACKDROP, 0.90)
    cr.paint()
    draw_zones(cr, width, height, zones, selected, caption)


def write_shot(path, width, height, zones, selected, caption="", hint=HINT):
    """Отрисовать оверлей (или HUD, если ``hint`` пуст) в PNG — проверка без интерактива."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
    cr = cairo.Context(surface)
    cr.set_source_rgb(*BACKDROP)  # непрозрачная подложка, чтобы снимок читался
    cr.paint()
    draw_zones(cr, width, height, zones, selected, caption, hint)
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


def logical_areas(monitors, scales):
    """Рабочие области мониторов в логических пикселях GTK, в том же порядке.

    ``monitors()`` теперь физический, а GTK-окошки живут в логических пикселях,
    поэтому окну оверлея и HUD нужна эта же область, переведённая
    (:func:`zone_units.logical_rect`). Список масштабов короче списка областей —
    обычное дело без GDK: ``zone_units.scale_at`` даёт 1, и область не меняется.
    """
    return [zu.logical_rect(wa, zu.scale_at(scales, i)) for i, wa in enumerate(monitors)]


def pointer_physical(point, monitors, scales):
    """Логическая точка GDK → физическая точка X или ``None``.

    Указатель GDK приходит в логических пикселях (``pointer_point``), а мониторы,
    ``--at`` и строки stdin — в физических. Монитор ищется по логическим областям,
    а не по одному общему коэффициенту: у мониторов бывает свой масштаб, и общее
    число соврало бы на соседнем экране.
    """
    if point is None:
        return None
    areas = logical_areas(monitors, scales)
    index = areas.index(zt.monitor_for_point(areas, point))
    return zu.physical_point(point, zu.scale_at(scales, index))


def pick_monitor(monitors, scales, win_id, args):
    """Индекс монитора: явный ``--monitor``, иначе монитор окна, иначе под курсором.

    Курсор GDK отдаёт в логических пикселях, а ``monitors`` — физические, поэтому
    монитор под курсором ищется по логическим областям (:func:`logical_areas`).
    Монитор окна — наоборот, по физической рамке окна (``window_monitor``).
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
    """Индекс зоны с именем ``name``; если такой нет — первый."""
    if name:
        for i, z in enumerate(zones):
            if z.name == name:
                return i
    return nav.first_index(zones)


# ------------------------------------------------------------ интерактивный оверлей


class Picker:
    def __init__(
        self, zones, grid, wa, output, mon_index, args, win_id, caption="", scale=1
    ):
        self.zones = zones
        self.grid = grid
        self.wa = wa
        self.output = output  # имя выхода: у монитора может быть свой набор зон
        self.mon_index = mon_index
        self.scale = zu.scale_value(scale)  # масштаб монитора: wa физическая
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
        # Окно GTK — в логических пикселях: `move`/`set_default_size` физическую
        # рамку на масштабе 2 удвоили бы, и оверлей уехал бы на соседний экран.
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

    # ---- состояние

    def _initial_selection(self, width, height):
        """Зона под курсором; ``width``/``height`` — физические размеры области.

        Курсор GDK логический, а ``width``/``height`` и ``wa`` физические: без
        перевода (0,1) вместо середины экрана получалось бы на масштабе 2 вдвое
        ближе к левому верхнему углу, и первое выделение уезжало бы от курсора.
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
        draw_overlay(cr, alloc.width, alloc.height, self.zones, self.selected, self.caption)
        return False

    def on_destroy(self, _widget):
        Gtk.main_quit()

    # ---- применение и выход

    def apply_selected(self):
        if self.selected is None:
            return
        name = self.zones[self.selected].name
        # геометрия из того же набора, что и нарисованные зоны (монитор стола)
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


# ------------------------------------------------------------------ HUD-оверлей


def parse_point(text):
    """Точка ``(x, y)`` из строки ``X,Y`` или ``X Y``; ``None`` — если это не пара.

    Один разбор на два входа: ``--at X,Y`` и строки ``X Y`` в stdin у ``--follow``.
    Watcher перетаскивания печатает позицию курсора именно строкой, поэтому
    формат с пробелом — основной, а запятая — удобство для ручного запуска.
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
    """``--at X,Y``: точка или понятная ошибка аргументов (код 2)."""
    point = parse_point(text)
    if point is None:
        raise argparse.ArgumentTypeError(
            _("screen coordinates X,Y are required, not {!r}").format(text)
        )
    return point


class Hud:
    """Пассивный оверлей сетки для перетаскивания: рисует, но не берёт ввод.

    Отличие от :class:`Picker` — в трёх местах: окно не берёт фокус
    (``set_accept_focus(False)``/``set_focus_on_map(False)``), клавиатура не
    захватывается (нет ``Gdk.Seat.grab`` — её держит muffin) и окно прозрачно
    для ввода, а точку подсветки присылает клиент (``--at`` или stdin). Фон
    полупрозрачный, чтобы сетка не закрывала перетаскиваемое окно.
    """

    def __init__(self, zones, wa, caption="", scale=1):
        self.zones = zones
        # Рабочая область монитора — физические пиксели X: в них приходят и
        # точки от watcher'а (`set_point`), и снимок `--shot`.
        self.wa = wa
        self.scale = zu.scale_value(scale)  # масштаб монитора: окно — логическое
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
        # Фокус и клавиатура — чужие: перетаскиванием владеет muffin.
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_app_paintable(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.DOCK)
        # Окно GTK — в логических пикселях, `wa` — физическая: физический размер
        # в `set_default_size` дал бы окно вдвое больше монитора.
        win_x, win_y, win_w, win_h = zu.logical_rect(wa, self.scale)
        self.win.move(win_x, win_y)
        self.win.set_default_size(win_w, win_h)
        # Без RGBA-визуала полупрозрачный фон не скомпонуется и станет чёрным.
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual() if screen is not None else None
        if visual is not None and screen.is_composited():
            self.win.set_visual(visual)
        self.win.connect("draw", self.on_draw)
        self.win.connect("destroy", lambda _widget: Gtk.main_quit())
        # Сначала на экран, потом форма ввода: input shape ставится на живое окно
        # (и GDK не перезатирает его при отображении).
        self.win.show_all()
        self.input_mode = self.make_input_transparent()

    # ---- ввод не наш

    def make_input_transparent(self):
        """Сделать окно прозрачным для ввода: события X идут мимо него.

        Работает не всё, что обещает имя: проверено под Xvfb, что
        ``Gdk.Window.set_pass_through(True)`` выставляет только флаг GDK
        (``get_pass_through()`` — True), а X input shape остаётся полным, и окно
        продолжает ловить события. Поэтому сначала ставится пустой input shape
        (``input_shape_combine_region`` с пустым ``cairo.Region``) — это
        единственный механизм, который видит X-сервер (расширение SHAPE), — и
        только потом, поверх, флаг ``set_pass_through``: он говорит самому GDK,
        что искать окно под курсором надо глубже. Без SHAPE остаётся хотя бы он.
        Возвращает, что сработало: запросить обратно input shape GDK не даёт.
        """
        gdk_win = self.win.get_window()
        if gdk_win is None:
            print(_("zone-picker: the HUD has no Gdk window - input cannot be disabled"), file=sys.stderr)
            return _("no window")
        used = []
        try:
            gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)
            used.append("input_shape_combine_region")
        except Exception as exc:  # X-сервер без SHAPE
            print(_("zone-picker: failed to set the input shape: {}").format(exc), file=sys.stderr)
        try:
            gdk_win.set_pass_through(True)
            used.append("set_pass_through")
        except Exception as exc:  # старый GTK
            print(_("zone-picker: set_pass_through did not work: {}").format(exc), file=sys.stderr)
        return " + ".join(used) if used else _("none")

    # ---- точка снаружи

    def set_point(self, point):
        """Подсветить зону под экранной точкой (``None`` — снять подсветку)."""
        index = None if point is None else nav.zone_at_point(self.zones, self.wa, point)
        if index != self.selected:
            self.selected = index
            self.win.queue_draw()
        return index

    def apply_point(self, point):
        """Подсветить точку и сказать в stdout, что именно подсвечено.

        Строка нужна watcher'у и дымовому тесту: по ней видно, что процесс жив
        и точка дошла, не разглядывая окно.
        """
        index = self.set_point(point)
        name = self.zones[index].name if index is not None else _("off the zones")
        print(f"zone-picker: {point[0]},{point[1]} -> {name}", flush=True)
        return index

    def feed(self, data):
        """Скормить кусок stdin: разобрать целые строки ``X Y`` и перерисовать.

        Неполная строка ждёт продолжения (watcher может писать кусками),
        мусорные строки пропускаются — подсветка от них не мигает.
        """
        self._buf += data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            point = parse_point(line)
            if point is not None:
                self.apply_point(point)

    # ---- рисование

    def on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        # OPERATOR_SOURCE: подложка заменяет содержимое окна целиком. С композитором
        # виден рабочий стол сквозь HUD_ALPHA, без него — ровная тёмная подложка, а
        # не остатки предыдущего кадра.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(*BACKDROP, HUD_ALPHA)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        # Подсказки про клавиши нет: HUD их не слушает.
        draw_zones(cr, alloc.width, alloc.height, self.zones, self.selected, self.caption, hint="")
        return False


def follow_stdin(hud):
    """Подавать HUD точки из stdin до EOF; окно живёт в главном цикле GTK.

    Никаких таймеров, двигающих окно или забирающих ввод: единственный источник
    новых точек — строки ``X Y`` от watcher'а, а конец потока (EOF, SIGTERM)
    завершает главный цикл. Перезапуск процесса на каждый кадр дал бы мигание.
    """

    def on_readable(fd, _condition):
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            print(_("zone-picker: cannot read stdin: {}").format(exc), file=sys.stderr)
            chunk = b""
        if not chunk:  # EOF или HUP: кормить больше некому
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


# Флаги интерактивного режима: в HUD они не имеют смысла — HUD ничего не выбирает,
# не вкладывает и не печатает о вложении (--quiet только молчит, поэтому безвреден).
HUD_INCOMPATIBLE = ("select", "window", "dry_run", "verify")


def validate_args(ap, args):
    """Сверить сочетания режимов; негодное — ошибка аргументов (код 2).

    ``--hud`` — отдельный пассивный режим: он не двигает окна (поэтому нет
    ``--select``/``--window``/``--dry-run``/``--verify``) и не работает без
    источника точки. ``ap.error`` печатает usage и выходит с кодом 2 — тот же
    контракт, что у остальных ошибок разбора.
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
    # Стол для разбора профилей: `--workspace N`, иначе активный. Сама сетка
    # считается после выбора монитора — она зависит от экрана (grid_for_output),
    # а не только от стола. Стол без профиля молча живёт в общем [zones] (та же
    # деградация, что в zone-tiler.grid_for_workspace), поэтому подпись стола
    # и показанная сетка не расходятся.
    workspace = zt.resolve_workspace(args.workspace)

    try:
        Gtk.init()
    except Exception as exc:
        fail(_("zone-picker: failed to open the display: {}").format(exc))
    # Имена выходов нужны для набора зон монитора `[zones:<выход>]`; порядок
    # областей тот же, что у zt.monitors(), — по нему и нумеруется `--monitor`.
    outputs = zt.monitor_outputs()
    monitors = [wa for _unused, wa in outputs]
    if not monitors:
        fail(_("zone-picker: failed to get monitor work areas"))
    # Масштабы — в том же порядке, что области (пустой список без GDK = масштаб 1).
    scales = zt.monitor_scales()
    win_id = args.window
    if win_id is None and not args.shot and not args.hud:
        win_id = active_window_quiet()
    if args.hud and args.at is not None and args.monitor is None:
        # HUD без --monitor живёт на мониторе присланной точки, а не под курсором:
        # точку задаёт клиент, и именно на его мониторе надо подсвечивать.
        mon_index = monitors.index(zt.monitor_for_point(monitors, args.at))
    else:
        mon_index = pick_monitor(monitors, scales, win_id, args)
    if not 0 <= mon_index < len(monitors):
        fail(_("zone-picker: no monitor {}").format(mon_index))
    wa = monitors[mon_index]
    output = outputs[mon_index][0]
    # Масштаб выбранного монитора: один на окно оверлея и окно HUD.
    scale = zu.scale_at(scales, mon_index)
    # Сетка экрана — тем же резолвером, что у раскладчика: иначе оверлей показал
    # бы зоны профиля стола, а демон положил бы окно в зоны профиля экрана.
    grid, screen_profile, desk_profile = screen_grid(conf, output, workspace)
    caption = screen_caption(workspace, desk_profile, output, screen_profile)
    hud_heading = hud_caption(desk_profile, output, screen_profile)
    zones = zones_from_grid(grid, output)
    if not zones:
        fail(_("zone-picker: the config has no zones for monitor {}").format(output))

    if args.shot:
        if args.hud:
            # Снимок HUD — тот же PNG, только без подсказки и с подсветкой точки:
            # именно по нему видно, что подсветка работает (разные точки — разные файлы).
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
        # Диагностика: единственное доказательство, что окно прозрачно для ввода.
        print(
            _("zone-picker: the HUD does not take input ({})").format(hud.input_mode),
            file=sys.stderr,
        )
        if args.at is not None:
            hud.apply_point(args.at)
        if args.follow:
            follow_stdin(hud)
        else:
            # --at без --shot: дать окну один кадр и выйти, без главного цикла «навсегда».
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

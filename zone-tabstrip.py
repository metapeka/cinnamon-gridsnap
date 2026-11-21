#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone-tabstrip.py — полоса вкладок поверх зоны: рисует, клик отдаёт демону.

Процесс запускает демон (``zone-daemon.py``) на все зоны сразу:

    python3 zone-tabstrip.py --conf <zones.conf>

Дальше это фильтр: демон пишет в stdin построчные **полные** снимки
(``zone_tabs.strips_payload``), полоса рисует по одному окну на запись:

    {"strips": [{"zone": "main", "rect": [0, 0, 960, 22],
                 "tabs": [{"window": 12345, "title": "Firefox", "active": true}]}]}

Окно полосы — override-redirect (``Gtk.WindowType.POPUP``): оно выше окон зоны,
не берёт фокус, не попадает в список задач и не перехватывает ввод. Окно зоны
стоит **ниже** полосы (зона минус полоса, ``zone_tabs.tab_rect``), поэтому
полоса не мешает тащить окно за его заголовок — она его не перекрывает.
Клик по вкладке уходит в сокет демона (``zone_ipc``) запросом
``zone_tabs.tabs_switch_command``, как у ``gridsnap-ctl``: отдельного канала у
полосы нет.

Пустой снимок (``{"strips": []}``) убирает все окна полос, EOF в stdin —
завершение процесса: демон закрыл поток, значит полос больше некому показывать.

Чистая часть — в ``zone_tabstrip_nav`` (разбор снимка, рамки вкладок, подписи,
вкладка под точкой клика), без GTK; здесь только окна, cairo и сокет. Без GTK
(``python3-gi``, ``gir1.2-gtk-3.0``) процесс честно говорит об этом в stderr и
выходит с кодом 1: тихо не рисовать полосу нельзя — тогда непонятно, почему
вкладок не видно.
"""

import argparse
import os
import signal
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_tabstrip_nav as nav  # noqa: E402  (ищем рядом с собой, как устанавливается)

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_FAIL = 1

# Сколько ждать ответа демона на клик, секунд. Клик обрабатывается в главном
# цикле GTK, поэтому ожидание короткое: подвисший демон не должен замораживать
# перерисовку полос, а «демона нет» и так видно по connect.
DAEMON_TIMEOUT = 1.0

# Цвета полосы: подложка, вкладка, активная вкладка, подписи, акцент сверху.
STRIP_BG = (0.13, 0.14, 0.17)
TAB_BG = (0.20, 0.22, 0.26)
TAB_ACTIVE_BG = (0.32, 0.36, 0.44)
TAB_FG = (0.78, 0.80, 0.84)
TAB_ACTIVE_FG = (1.0, 1.0, 1.0)
ACCENT = (1.0, 0.71, 0.33)
ACCENT_H = 2
FONT = "Sans 8"

# GTK/cairo подтягиваются лениво (`load_gtk`), чтобы без них можно было отличить
# «нет библиотеки» от «нет дисплея» и сказать человеку, чего именно не хватает.
Gtk = Gdk = GLib = Pango = PangoCairo = cairo = None


def fail(message, code=EXIT_FAIL):
    print(message, file=sys.stderr)
    sys.exit(code)


def tabs_module():
    """Модуль `zone_tabs`: запрос клика живёт только там (контракт волны)."""
    import zone_tabs  # noqa: PLC0415 — ленивый импорт, рядом с полосой

    return zone_tabs


def load_gtk():
    """Импортировать GTK 3 и cairo; без них — сообщение в stderr и код 1."""
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
            "zone-tabstrip: нужен GTK 3 для python3 (python3-gi и gir1.2-gtk-3.0) "
            f"— без него полосу вкладок не нарисовать: {exc}",
            EXIT_FAIL,
        )
    Gtk, Gdk, GLib = gtk_module, gdk_module, glib_module
    Pango, PangoCairo, cairo = pango_module, pangocairo_module, cairo_module


def send_request(request, path=None, timeout=DAEMON_TIMEOUT):
    """Отправить запрос демону; `None` — демона нет или он не ответил.

    Сокет тот же, что у `gridsnap-ctl` (`zone_ipc`): клик по вкладке — обычная
    команда демона, а не новый канал. Ошибки сети здесь не падение: окно полосы
    остаётся на экране, а причина уходит в stderr демона (его же stderr).
    """
    import zone_ipc  # noqa: PLC0415 — ленивый импорт, рядом с полосой

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(path or zone_ipc.default_socket_path())
    except OSError as exc:
        conn.close()
        return None, f"демон не отвечает: {exc}"
    try:
        conn.sendall(zone_ipc.encode_message(request))
        return zone_ipc.read_response(conn), None
    except OSError as exc:
        return None, f"сокет демона отказал: {exc}"
    except zone_ipc.ProtocolError as exc:
        return None, f"демон ответил непонятно: {exc}"
    finally:
        conn.close()


class Strip:
    """Одно окно-полоса над зоной: override-redirect, без фокуса, с кликом.

    Окно — ``Gtk.WindowType.POPUP``: на X11 это override-redirect, поэтому
    muffin его не оформляет, не ставит в список задач и не даёт ему фокус. Свой
    размер и место полоса получает из снимка, а не из оконного менеджера, — так
    она и остаётся приклеенной к верхней кромке зоны.
    """

    def __init__(self, zone, owner):
        self.zone = zone
        self.owner = owner
        self.record = None
        self.rows = []
        self.rect = None
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_title(f"Gridsnap: вкладки зоны {zone}")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        # Фокус и клавиатура — чужие: их держит окно зоны, полоса только рисует.
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_app_paintable(True)
        self.win.connect("draw", self.on_draw)
        self.win.connect("button-press-event", self.on_button_press)
        self.win.connect("destroy", self.on_destroy)
        self.shown = False

    # ---- снимок

    def show_record(self, record):
        """Показать (или обновить) полосу по записи снимка; без рамки — убрать."""
        rect = nav.record_rect(record)
        if rect is None or self.win is None:
            self.destroy()
            return
        self.record = record
        self.rows = nav.layout(record)
        self.rect = rect
        x, y, w, h = (int(round(v)) for v in rect)
        # Размер — через `set_size_request`, а не `resize`/`set_default_size`:
        # окно POPUP рождается 200x200 и после `show_all` resize игнорирует
        # (проверено под Xvfb: `xwininfo` показывает 200 px в высоту). Полоса
        # высотой 200 накрыла бы окно зоны и съела бы его клики.
        self.win.set_size_request(w, h)
        self.win.move(x, y)
        if not self.shown:
            self.win.show_all()
            self.shown = True
        self.win.queue_draw()

    def destroy(self):
        """Убрать окно полосы (зона пропала, снимок стал пустым, EOF). Идемпотентно."""
        win, self.win = self.win, None
        self.shown = False
        self.record, self.rows, self.rect = None, [], None
        if win is not None:
            win.destroy()

    def on_destroy(self, _widget):
        self.win = None
        self.owner.forget(self)

    # ---- клик

    def on_button_press(self, _widget, event):
        """Клик по вкладке → команда демону «показать это окно в этой зоне»."""
        if self.win is None or self.rect is None or event.button != 1:
            return False
        # Событие приходит в координатах окна полосы, а рамки вкладок — в
        # экранных: переводим, чтобы попадание считалось одним кодом с тестами.
        point = (self.rect[0] + event.x, self.rect[1] + event.y)
        window = nav.tab_under(self.record, point)
        if window is None:
            return False
        request = tabs_module().tabs_switch_command(self.zone, window)
        response, error = send_request(request, self.owner.socket_path)
        if response is None:
            print(f"zone-tabstrip: вкладка не переключена ({error})", file=sys.stderr)
        elif not response.get("ok", True):
            reason = response.get("error") or "причина не названа"
            print(f"zone-tabstrip: демон отказал: {reason}", file=sys.stderr)
        return True

    # ---- рисование

    def on_draw(self, widget, cr):
        if self.rect is None:
            return False
        alloc = widget.get_allocation()
        # OPERATOR_SOURCE: подложка заменяет кадр целиком, без остатков прошлого.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgb(*STRIP_BG)
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        ox, oy = self.rect[0], self.rect[1]
        for row in self.rows:
            x, y, w, h = row["rect"]
            cr.set_source_rgb(*(TAB_ACTIVE_BG if row["active"] else TAB_BG))
            cr.rectangle(x - ox, y - oy, w, h)
            cr.fill()
            if row["active"]:
                # Активная вкладка — не только цветом: сверху акцентная полоска.
                cr.set_source_rgb(*ACCENT)
                cr.rectangle(x - ox, y - oy, w, min(ACCENT_H, h))
                cr.fill()
            self.draw_label(cr, row, ox, oy)
        return False

    def draw_label(self, cr, row, ox, oy):
        """Подпись вкладки внутри её рамки: обрезается по ширине, по центру по высоте."""
        x, y, w, h = row["rect"]
        pad = nav.TAB_PADDING
        inner = max(1, int(w) - 2 * pad)
        layout = PangoCairo.create_layout(cr)
        layout.set_text(row["label"], -1)
        layout.set_font_description(Pango.FontDescription(FONT))
        # Подпись уже обрезана по средней ширине символа (nav.fit_title); здесь
        # вторая, точная защита: Pango сам поставит многоточие, если шрифт шире.
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_width(inner * Pango.SCALE)
        _lw, lh = layout.get_pixel_size()
        cr.set_source_rgb(*(TAB_ACTIVE_FG if row["active"] else TAB_FG))
        cr.save()
        cr.rectangle(x - ox + pad, y - oy, inner, h)
        cr.clip()
        cr.move_to(x - ox + pad, y - oy + max(0.0, (h - lh) / 2.0))
        PangoCairo.show_layout(cr, layout)
        cr.restore()


class Tabstrip:
    """Полосы всех зон: один процесс, одна полоса-окно на запись снимка."""

    def __init__(self, socket_path=None):
        self.socket_path = socket_path
        self.strips = {}  # имя зоны -> Strip
        self.buf = ""

    def apply(self, records):
        """Привести окна полос к снимку: новые — создать, пропавшие — убрать.

        Снимок полный, поэтому «зона пропала» — это её отсутствие в снимке, а не
        отдельное сообщение. Порядок записей — порядок зон в конфиге; на экране
        он ни на что не влияет, окна независимы.
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
        """Скормить кусок stdin: разобрать целые строки и применить снимки.

        Неполная строка ждёт продолжения, мусорная пропускается: полоса не
        должна исчезать из-за одной битой строки (``nav.parse_snapshot`` вернёт
        ``None``), а пустой снимок ``{"strips": []}`` — законная команда убрать
        все полосы.
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
        """Убрать полосу из учёта (её окно уничтожили снаружи)."""
        if self.strips.get(strip.zone) is strip:
            self.strips.pop(strip.zone, None)

    def close(self):
        """Убрать все окна полос (EOF в stdin, сигнал, конец работы)."""
        for strip in list(self.strips.values()):
            strip.destroy()
        self.strips.clear()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Полоса вкладок поверх зоны (Cinnamon/X11)",
    )
    ap.add_argument(
        "--conf",
        default=None,
        help=(
            "путь к zones.conf: так полосу запускает демон; сама полоса конфиг не "
            "читает — рамки и подписи приходят в снимке"
        ),
    )
    ap.add_argument(
        "--socket",
        default=None,
        help="путь сокета демона (по умолчанию — как у gridsnap-ctl)",
    )
    ap.add_argument("--version", action="version", version=f"zone-tabstrip {VERSION}")
    args = ap.parse_args(argv)

    load_gtk()
    try:
        ready = Gtk.init_check()[0]
    except Exception as exc:  # GTK есть, но не поднялся (нет дисплея и т. п.)
        fail(f"zone-tabstrip: GTK не запустился: {exc}", EXIT_FAIL)
    if not ready:
        fail("zone-tabstrip: нет дисплея — полосе вкладок нужен X11 (DISPLAY)", EXIT_FAIL)

    strips = Tabstrip(args.socket)

    def on_readable(fd, _condition):
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            print(f"zone-tabstrip: stdin не читается: {exc}", file=sys.stderr)
            chunk = b""
        if not chunk:  # EOF или HUP: демон закрыл поток, кормить больше некому
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

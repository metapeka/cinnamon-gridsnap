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

Два пространства координат: рамка ``rect`` в снимке — **физические** пиксели X
(это координаты окна зоны), а окно GTK живёт в **логических**. Перевод — один,
в :func:`window_rect` через ``zone_units.logical_rect``, с масштабом монитора
под центром самой полосы: при ``GDK_SCALE=2`` без него полоса уехала бы вдвое
дальше зоны и выросла бы вдвое. Тот же масштаб переводит и локальные координаты:
cairo в окне рисует в логических пикселях (:func:`local_rect`), а событие клика
приходит в них же, тогда как рамки вкладок в снимке — физические (:func:`local_point`).
Без этого при масштабе 2 вкладки рисовались бы вдвое шире полосы, и в окне
оставалась бы одна, растянутая на всю ширину.

Пустой снимок (``{"strips": []}``) убирает все окна полос, EOF в stdin —
завершение процесса: демон закрыл поток, значит полос больше некому показывать.

Чистая часть — в ``zone_tabstrip_nav`` (разбор снимка, рамки вкладок, подписи,
вкладка под точкой клика), без GTK; здесь только окна, cairo и сокет. Без GTK
(``python3-gi``, ``gir1.2-gtk-3.0``) процесс честно говорит об этом в stderr и
выходит с кодом 1: тихо не рисовать полосу нельзя — тогда непонятно, почему
вкладок не видно.
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

import zone_tabstrip_nav as nav  # noqa: E402  (ищем рядом с собой, как устанавливается)
import zone_units  # noqa: E402  (единственный перевод логических px в физические)
import window_backend  # noqa: E402  (оконный слой: X11-бэкенд GDK и масштаб)
from zone_i18n import _  # noqa: E402  (там же: строки интерфейса и их перевод)

# Полоса ставит себя по координатам X, поэтому GDK обязан быть на X11-бэкенде —
# иначе в Wayland-сессии `move()` ничего не делает (замер: docs/RESEARCH-wayland-recon.md).
# Вызов до первого `load_gtk()`: после инициализации GDK бэкенд не переключить.
window_backend.prefer_x11_gdk()

VERSION = "0.2.0"

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


# Раскладчик (`zone-tiler.py`) нужен полосе только за масштабом монитора: рамки,
# подписи и порядок вкладок приходят в снимке. Загружается один раз и лениво: без
# X и GDK он ответить не может, а повторять его загрузку на каждый снимок — это
# заново читать xrandr и GDK на каждой перерисовке полосы.
_ZT = None
_ZT_LOADED = False


def tiler_module():
    """Раскладчик рядом с полосой — или ``None``, если он не загрузился.

    Соседний hyphen-модуль берётся по своему пути, а не по ``sys.path``: полоса
    ставится и рядом с исходниками, и в ``/usr/lib/gridsnap``. Сбой загрузки — не
    падение полосы: без раскладчика масштаба не спросить, и полоса ведёт себя как
    до волны DPI (масштаб 1).
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
        except Exception:  # нет модуля, нет его зависимостей — масштаб будет 1
            _ZT = None
    return _ZT


def strip_scale(point):
    """Масштаб монитора под **физической** точкой; 1 — спросить не у кого.

    Масштаб берётся у раскладчика (`zone-tiler.scale_at_point`): рабочие области
    мониторов и масштабы GDK живут там, и своего разбора мониторов полоса не
    держит. Если X или GDK нет, раскладчик может не ответить вовсе — вплоть до
    ``SystemExit`` из `monitors` (`fail`), — поэтому здесь ловится и он: сбой
    чтения масштаба не должен ронять полосу.
    """
    zt = tiler_module()
    if zt is None:
        return 1
    try:
        return zone_units.scale_value(zt.scale_at_point(point))
    except (Exception, SystemExit):
        return 1


def strip_center(rect):
    """Центр физической рамки полосы — под ним берётся масштаб монитора."""
    x, y, w, h = (int(round(float(v))) for v in rect)
    return (x + w // 2, y + h // 2)


def window_rect(rect, scale_of=strip_scale):
    """Физическая рамка полосы из снимка → рамка окна GTK, в логических px.

    Демон присылает рамку полосы в **физических** пикселях X (это координаты
    окна зоны), а GTK считает в **логических**: при ``GDK_SCALE=2`` монитор
    1920x1080 виден как 960x540. Без перевода полоса уехала бы вдвое дальше зоны
    и выросла бы вдвое — `set_size_request(1920, 22)` заказал бы окно 3840x44.

    Масштаб спрашивается под центром самой полосы (`scale_of`), а не под первым
    монитором: полоса может стоять на экране с другим масштабом. Размеры после
    перевода — целые и не меньше 1 px: окно нулевого размера не покажет ни
    подписи, ни рамки вкладки. Точка и размеры считаются в физических px, поэтому
    центр берётся до перевода.
    """
    x, y, w, h = (int(round(float(v))) for v in rect)
    try:
        scale = zone_units.scale_value(scale_of(strip_center(rect)))
    except Exception:  # масштаб не спросили — как раньше, на масштабе 1
        scale = 1
    lx, ly, lw, lh = zone_units.logical_rect((x, y, w, h), scale)
    return int(lx), int(ly), max(1, int(lw)), max(1, int(lh))


def local_rect(rect, origin, scale):
    """Рамка вкладки из снимка (физические px) → локальные логические px окна.

    Окно GTK живёт в логических пикселях, и cairo в нём тоже логический: при
    масштабе 2 физическое смещение внутри полосы — это вдвое большее число
    логических пикселей, поэтому смещения и размеры делятся на масштаб. Начало
    координат — левый верхний угол полосы (`origin`, физическая рамка из снимка).
    """
    x, y, w, h = rect
    s = zone_units.scale_value(scale)
    return (x - origin[0]) / s, (y - origin[1]) / s, w / s, h / s


def local_point(point, origin, scale):
    """Точка клика из логических px окна → физические px снимка.

    Обратна :func:`local_rect`: `nav.tab_under` и рамки вкладок считают в
    физических пикселях экрана, а событие GTK приходит в логических координатах
    окна полосы.
    """
    s = zone_units.scale_value(scale)
    return origin[0] + point[0] * s, origin[1] + point[1] * s


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
            _(
                "zone-tabstrip: GTK 3 for python3 is required (python3-gi and gir1.2-gtk-3.0) - without it the tab bar cannot be drawn: {}"
            ).format(exc),
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
        self.scale = 1
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_title(_("Gridsnap: tabs of zone {}").format(zone))
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
        # `self.rect` и рамки вкладок — в физических пикселях X, как в снимке;
        # окно же GTK живёт в логических, поэтому и место окна, и локальные
        # координаты рисования с кликом переводятся одним и тем же масштабом.
        # Читается он раз на снимок: `scale_at_point` — это GDK и xrandr.
        self.scale = zone_units.scale_value(strip_scale(strip_center(rect)))
        x, y, w, h = window_rect(rect, lambda _point: self.scale)
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
        self.record, self.rows, self.rect, self.scale = None, [], None, 1
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
        # Событие приходит в логических координатах окна полосы, а рамки вкладок
        # и `nav.tab_under` — в физических экранных: переводим масштабом полосы,
        # чтобы попадание считалось одним кодом с тестами.
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
        for row in self.rows:
            x, y, w, h = self.local_rect(row)
            cr.set_source_rgb(*(TAB_ACTIVE_BG if row["active"] else TAB_BG))
            cr.rectangle(x, y, w, h)
            cr.fill()
            if row["active"]:
                # Активная вкладка — не только цветом: сверху акцентная полоска.
                cr.set_source_rgb(*ACCENT)
                cr.rectangle(x, y, w, min(ACCENT_H / self.scale, h))
                cr.fill()
            self.draw_label(cr, row)
        return False

    def local_rect(self, row):
        """Рамка вкладки в локальных логических px окна (для cairo)."""
        return local_rect(row["rect"], self.rect, self.scale)

    def draw_label(self, cr, row):
        """Подпись вкладки внутри её рамки: обрезается по ширине, по центру по высоте."""
        x, y, w, h = self.local_rect(row)
        pad = nav.TAB_PADDING / self.scale
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
        cr.rectangle(x + pad, y, inner, h)
        cr.clip()
        cr.move_to(x + pad, y + max(0.0, (h - lh) / 2.0))
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

    load_gtk()
    try:
        ready = Gtk.init_check()[0]
    except Exception as exc:  # GTK есть, но не поднялся (нет дисплея и т. п.)
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

#!/usr/bin/env python3
"""window_backend — единственное место, где продукт говорит с оконной системой.

Сегодня оконная система — X11/EWMH: список окон, рамки, свойства `_NET_*`, команды
`XSendEvent`, рабочая область мониторов. Сюда же переезжают все вызовы `xprop`,
`xwininfo`, `xrandr` и класс работы с `libX11` (сейчас они живут в `zone-tiler.py`
вперемешку с логикой раскладки), чтобы у оконного слоя была **одна** граница.

Зачем: у Cinnamon появится полноценная Wayland-сессия (Cinnamon 6.8 / Linux Mint 23,
выход ~Рождество 2026), где оконный слой другой — EWMH нет, а управление окнами
устроено иначе. Пока весь разговор с окнами разбросан по раскладчику и демону,
любой Wayland-путь — это переписывание; с границей это отдельная реализация того же
интерфейса. Разведка (волна «шов + разведка») замером выясняет, что из этого
интерфейса вообще доступно под muffin в роли Wayland-композитора.

Состояние перехода: тела функций переехали сюда из `zone-tiler.py`/`zone-daemon.py`
без смены поведения, а сами `zone-tiler`/`zone-daemon` держат обёртки с прежними
именами — вызывающие (демон, пикер, апплет, тесты) ничего не замечают. Подмены в
тестах (`zt.subprocess.check_output`, `zt.X11`, `zt.monitors`) по-прежнему работают:
это тот же модуль `subprocess` и те же имена, что зовут обёртки.

Чего здесь намеренно **нет**:

* текстов для пользователя — сообщение и код возврата остаются у CLI (обёртки в
  `zone-tiler.py`); отсюда только исключение с причиной либо код причины;
* наблюдателя за изменениями (подписка на `PropertyNotify` в цикле демона) — самая
  рискованная часть шва, её переносим отдельным шагом: в этой волне чтение и запись;
* горячих клавиш: `gsettings` — не оконный слой, и на Wayland механизм другой.

Правила для реализаций: поведение и тексты исключений не менять (их сверяют
существующие тесты), X-сервер может отсутствовать — ни один вызов не должен падать
трассировкой вместо понятного исключения.
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

import zone_units  # noqa: E402  (перевод единиц: логические пиксели GTK ↔ физические X)

# Ошибка оконного слоя: «окна/рабочей области нет или система не ответила».
# Текст — техническая причина; сообщение пользователю и код возврата задаёт CLI.
class WindowBackendError(RuntimeError):
    """Оконная система не ответила или не умеет нужную операцию."""


# Причины, по которым `read_active_window` не отдал окно. Это не текст: что
# показать человеку и с каким кодом выйти, решает CLI (`zone-tiler.py`), иначе
# оконный слой пришлось бы переводить отдельно от интерфейса.
ACTIVE_WINDOW_NO_XPROP = "xprop"
ACTIVE_WINDOW_NO_WINDOW = "no-active-window"

# Свойство root со списком управляемых клиентских окон (EWMH).
CLIENT_LIST_ATOM = "_NET_CLIENT_LIST"


# --------------------------------------------------------------- чтение окон


def _xprop_props(text):
    """Разобрать вывод xprop в {имя_свойства: [строки значения]}.

    Строки продолжения (значение перенесено на следующую строку) приклеиваются
    к текущему свойству, чтобы один и тот же разбор годился и для однострочной
    формы `WM_CLASS(STRING) = "a", "b"`, и для многострочной.
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
    """Строки из значения xprop: в кавычках — как есть, иначе — по запятой."""
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    if quoted:
        return [re.sub(r"\\(.)", r"\1", s) for s in quoted]
    return [s.strip() for s in value.split(",") if s.strip()]


def window_class_title(win):
    """WM_CLASS (класс, вторая строка) и заголовок окна через xprop.

    Заголовок берётся из _NET_WM_NAME, при его отсутствии — из WM_NAME. Любая
    ошибка (нет xprop, окна, доступа) даёт ("", "") — вызывающий просто не найдёт
    совпадений. Чистый разбор вывода вынесен в `_xprop_props`/`_strings`.
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
            wm_class = classes[1]  # instance, class -> берём class
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
    """Набор `_NET_WM_WINDOW_TYPE` окна через xprop (пустой при ошибке)."""
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
    """Клиентские окна из `_NET_CLIENT_LIST` (EWMH) через `xprop -root`.

    Возвращает список id в порядке свойства (снизу вверх) или `None`, если
    список недоступен: нет xprop/дисплея, отсутствует само свойство (xprop
    печатает `no such atom on any window.`) или в выводе нет маркера
    `window id #`. Пустой список — валидное состояние «клиентов нет», его
    нельзя путать с ошибкой. Функцию можно подменить в тестах.
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
    """Номер виртуального рабочего стола окна (`_NET_WM_DESKTOP`) или None."""
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
    """``(номер стола | None, отвечает ли X)`` — одна команда `xprop`.

    Разделение нужно, чтобы отличить «X не отвечает» (нет `xprop`, нет дисплея —
    команда падает) от «X жив, а свойства нет». Второе — это Wayland-сессия
    Cinnamon до первого переключения стола: `xprop` печатает «no such atom on any
    window» и выходит с нулём (проверено на машине), то есть команда успешна.
    Без этого различия либо Wayland получает отказ вместо стола, либо «нет X»
    молча становится первым столом — а это разные вещи.
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
    """Номер активного рабочего стола (`_NET_CURRENT_DESKTOP`) или None.

    Ноль — первый стол: так его нумерует EWMH. В ``[workspaces]`` ключи на
    единицу больше (как в интерфейсе Cinnamon), перевод — у вызывающего
    (`zone-tiler.grid_for_workspace`).
    """
    return _current_workspace_raw()[0]


def x_alive():
    """Отвечает ли X-сервер: читается `_NET_SUPPORTED` (у живого WM он есть).

    Нужно там, где надо отличить «X не отвечает» (нет `xprop`, нет дисплея) от
    «X жив, а нужного свойства нет» — второе бывает в Wayland-сессии Cinnamon
    (замер: ``docs/RESEARCH-wayland-recon.md``). Спрашивается только в редком
    случае (свойства стола нет и подмены нет): в обычной сессии — ни одного
    лишнего подпроцесса.
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
    """Номер активного стола; если свойства нет, а X жив — 0 (первый стол).

    В Wayland-сессии Cinnamon (замер: ``docs/RESEARCH-wayland-recon.md``) свойства
    `_NET_CURRENT_DESKTOP` нет до первого настоящего переключения стола, хотя стол
    есть и окна на нём лежат: без этого запаса `focus` отказывал («не удалось
    определить активный рабочий стол»), а `wmctrl -d` не работал вообще.

    `None` остаётся ровно для случая «X не отвечает»: тогда стол по-прежнему
    неизвестен, и звать его нулевым нельзя — на X11 свойство не пропадает, так что
    там разбор ничего не меняет.
    """
    desk = current_workspace()
    if desk is not None:
        return desk
    return 0 if x_alive() else None


def window_on_other_desktop(win):
    """Окно лежит на другом столе — это не то же самое, что «свёрнуто».

    Замер (``docs/RESEARCH-wayland-recon.md``): в Wayland-сессии Cinnamon окно,
    уехавшее на другой стол, получает `WM_STATE=Iconic`, хотя пользователь его не
    сворачивал, и одним `_NET_ACTIVE_WINDOW` его не вернуть — сначала надо
    переключить стол. ``window_iconic`` в этой ситуации отвечает «свёрнуто», а
    вот этот вопрос — «просто на другом столе».

    ``None`` — стол окна или активный стол не прочитать, либо окно «липкое»
    (``_NET_WM_DESKTOP = 0xFFFFFFFF``: оно на всех столах).
    """
    desk = window_workspace(win)
    if desk is None or desk >= 0xFFFFFFF0:
        return None
    current = current_workspace()
    if current is None:
        return None
    return desk != current


def window_iconic(win):
    """Свёрнуто ли окно сейчас — `WM_STATE: Iconic` или `_NET_WM_STATE_HIDDEN`.

    muffin при `WM_CHANGE_STATE` ставит оба признака (проверено дымом на
    Cinnamon/muffin), поэтому читается настоящий EWMH/ICCCM-признак, а не
    догадка по геометрии. Не прочитать свойство — окно считается видимым.
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
    """Активное окно и текст ошибки: ``(окно | None, причина | None)``.

    Причина отказа — код (`ACTIVE_WINDOW_NO_XPROP`, `ACTIVE_WINDOW_NO_WINDOW`), а
    не готовый текст: сообщение и код возврата задаёт CLI, поэтому разбор один
    на всех. `active_window` в `zone-tiler.py` падает с кодом 1, когда окна нет
    (это правильно для команд, которым окно нужно), а `active_window_quiet`
    отдаёт ``None`` без сообщений в stderr.
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
    """Фактическая рамка окна (x, y, w, h) по xwininfo с учётом рамки WM.

    Нет в выводе нужного поля (`Absolute upper-left X`, `Width`, ...) —
    `WindowBackendError`, `args[0]` — имя поля: текст для человека собирает
    обёртка в `zone-tiler.py`. Остальные сбои xwininfo (нет утилиты, окно
    исчезло) пробрасываются как есть — их показывают вызывающие.
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
    """Центр окна (px, py) по абсолютной геометрии `xwininfo`; None при ошибке.

    Нужен только чтобы выбрать монитор окна, поэтому берётся клиентская
    геометрия целиком: рамка WM сдвигает её на считаные пиксели и на выбор
    монитора не влияет.
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
    """Толщина рамки/титлбара оконного менеджера: (left, right, top, bottom)."""
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
    """Идентификатор X-сессии: окно `_NET_SUPPORTING_WM_CHECK` (или None).

    Память об укладках не должна переезжать в новую X-сессию: там XID раздаются
    заново, и старая запись может «попасть» на чужое окно того же класса.
    Не читается — `None`, тогда проверка выключена.
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


# ------------------------------------------------------------- мониторы


def prefer_x11_gdk():
    """Заставить GDK идти через X11 (Xwayland) — до первого его инициализирования.

    В Wayland-сессии GDK по умолчанию выбирает wayland-бэкенд: тогда рабочие
    области и масштаб приезжают из Wayland (где нет X11-струтов), а окна мы
    двигаем в координатах Xwayland — программа начинает мерить мир двумя линейками.
    Замер и разбор — `docs/RESEARCH-wayland-recon.md`.

    Вызывать **до** первого обращения к GDK/GTK: у GTK-программ (пикер, HUD,
    полоса вкладок) — до `import gi`/`Gtk.init()`. В X11-сессии вызов ничего не
    меняет, `GDK_BACKEND` снаружи не перебивается (`setdefault`).
    """
    os.environ.setdefault("GDK_BACKEND", "x11")


def gdk_monitors():
    """``[(логическая рабочая область, масштаб)]`` из GDK; ``[]`` — GDK недоступен.

    Один разбор на `monitors()` и `monitor_scales()`, чтобы порядок и состав
    мониторов у них не разошлись. Область здесь ещё логическая: GTK считает в
    логических пикселях, а в физические её переводит `monitors()`.

    Перед обращением к GDK включается X11-бэкенд (`prefer_x11_gdk`): иначе в
    Wayland-сессии области придут из другого мира, чем координаты окон.
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
        # Ни GDK, ни дисплея: раскладчик умеет и на одном `_NET_WORKAREA`.
        return []


def monitors():
    """Рабочие области мониторов в физических пикселях X.

    GDK отдаёт их в логических: при `GDK_SCALE=2` монитор 1920x1080 виден как
    960x540, а окно двигается в физических координатах X. Без перевода зона
    встала бы вдвое меньше и вдвое ближе к левому верхнему углу — доля 0.8358
    дала бы 802 вместо 1605.

    `WindowBackendError` — ни GDK, ни резерв `_NET_WORKAREA` не ответили; текст
    для человека и код возврата задаёт обёртка в `zone-tiler.py`.
    """
    out = [zone_units.physical_rect(area, scale) for area, scale in gdk_monitors()]
    if out:
        return out
    # резерв: EWMH `_NET_WORKAREA` (первый рабочий стол) — уже физические пиксели
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
    """Масштабы мониторов в том же порядке, что `monitors()`/`monitor_outputs()`.

    Нужны тем, кто живёт в логических пикселях GTK — окну оверлея пикера, окну
    HUD и полосе вкладок: физическую рамку они переводят в неё этим числом. Без
    GDK список пуст, и `zone_units.scale_at` даёт тем же потребителям 1 — ровно
    как в резерве `_NET_WORKAREA`, где масштаба тоже не спросить.

    Читается без подпроцессов: этим отпечатком демон сверяет на лету, не
    сменился ли масштаб экрана.
    """
    return [scale for _area, scale in gdk_monitors()]


def scale_at_point(point):
    """Масштаб монитора, в чью физическую рабочую область попала точка."""
    return zone_units.scale_for_point(monitors(), monitor_scales(), point)


def monitor_outputs():
    """[(имя выхода, (x, y, w, h))] — по одному на монитор.

    Имя (`eDP-1`, `HDMI-1`) стабильно между перезагрузками и переподключениями
    кабеля, поэтому именно оно адресует набор зон (`[zones:<выход>]`) и `--monitor`.
    Геометрия — прежняя GDK `get_workarea()` (в ней уже вычтена панель), но в
    физических пикселях X (`monitors()`), а не геометрия xrandr: xrandr даёт
    полный экран выхода, без панелей. Подмены в тестах (`zone-tiler.monitors`,
    `zone-tiler.xrandr_monitors`) обёртка в `zone-tiler.py` соблюдает сама.
    """
    return stitch_monitor_names(xrandr_monitors(), monitors())


# Строка `xrandr --listmonitors`: ` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`.
# Флаги `+*` относятся к состоянию монитора (`+` — авто, `*` — основной) и
# частью имени не являются.
_XRANDR_MONITOR_RE = re.compile(
    r"^\s*\d+:\s+[+*]*(?P<fallback>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    r"(?:\s+(?P<name>\S+))?\s*$"
)


def parse_xrandr_monitors(text):
    """[(имя, (x, y, w, h))] из вывода `xrandr --listmonitors`, в его порядке.

    Имя — последнее поле строки (` 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1`).
    Если имени в строке нет (старый xrandr), берётся поле после `N:` без флагов
    `+*`. Строки без геометрии (заголовок `Monitors:`, мусор) пропускаются.
    Чистая функция — под тесты.
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
    """Мониторы из `xrandr --listmonitors`; [] если утилиты нет или она упала."""
    try:
        text = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    return parse_xrandr_monitors(text)


def overlap_area(a, b):
    """Площадь пересечения прямоугольников a и b (0, если не пересекаются)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0


def stitch_monitor_names(outputs, areas):
    """Сшить имена выходов с рабочими областями: [(имя, область)].

    Совпало количество — имена по порядку: у GDK и xrandr один и тот же порядок
    мониторов. Иначе — по максимальному пересечению площадей (GDK может отдать
    одну область на несколько выходов или наоборот), а области без пересечения
    получают имя `monitor-<i>`. Без имён (нет xrandr) — только `monitor-<i>`, то
    есть прежнее поведение. Чистая функция — под тесты.
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
    """Рабочая область монитора, содержащего точку; иначе ближайшая по центру.

    Точка может не попасть ни в одну область (окно под панелью, в щели между
    мониторами) — тогда берётся область с ближайшим центром, а не монитор 0.
    `monitors` — непустой список прямоугольников (x, y, w, h).
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


# ------------------------------------------------------------- запись (X11)


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
        ("pad", ctypes.c_long * 15),  # чтобы структура была не меньше sizeof(XEvent)
    ]


CLIENT_MESSAGE = 33
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # Notify | Redirect
X_FLAGS = 1 | 2 | 4 | 8  # X, Y, Width, Height
GRAVITY_NORTH_WEST = 1
SOURCE_PAGER = 2
# `WM_CHANGE_STATE` со значением IconicState (ICCCM) — просьба WM свернуть окно.
ICONIC_STATE = 3


class X11:
    """Соединение с X-сервером и команды оконному менеджеру через `XSendEvent`.

    `move_resize` (`_NET_MOVERESIZE_WINDOW`), `set_state` (`_NET_WM_STATE`),
    `iconify` (`WM_CHANGE_STATE`), `activate` (`_NET_ACTIVE_WINDOW`),
    `set_current_desktop` (`_NET_CURRENT_DESKTOP`), `atom`, `send`, `close`.

    Ошибка открытия соединения — `WindowBackendError`; чужое соединение не
    закрывается (им владеет вызывающий).
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
        """Закрыть X-соединение (парно `XOpenDisplay`). Идемпотентно."""
        if getattr(self, "dpy", None):
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    def atom(self, name):
        """Атом по имени."""
        return self.x.XInternAtom(self.dpy, name.encode(), 0)

    def send(self, win, msg_type, values):
        """Отправить клиентское сообщение окну (`XSendEvent`)."""
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
        """`_NET_WM_STATE`: action: 0 = снять, 1 = добавить, 2 = переключить.

        `second` — второе свойство состояния (например
        `_NET_WM_STATE_MAXIMIZED_HORZ`); пустое — без него.
        """
        self.send(
            win,
            "_NET_WM_STATE",
            [action, self.atom(first), self.atom(second) if second else 0, SOURCE_PAGER, 0],
        )

    def move_resize(self, win, x, y, w, h):
        """`_NET_MOVERESIZE_WINDOW`: переставить и изменить размер окна."""
        flags = (SOURCE_PAGER << 12) | (X_FLAGS << 8) | GRAVITY_NORTH_WEST
        self.send(win, "_NET_MOVERESIZE_WINDOW", [flags, x, y, w, h])

    def iconify(self, win):
        """Свернуть окно: `WM_CHANGE_STATE` со значением `IconicState`."""
        self.send(win, "WM_CHANGE_STATE", [ICONIC_STATE, 0, 0, 0, 0])

    def activate(self, win):
        """Поднять и активировать окно: `_NET_ACTIVE_WINDOW` от имени pager'а.

        `data.l[0] = 2` (source indication, pager) и нулевой timestamp: muffin
        принимает активацию от pager'а, а устаревший ненулевой timestamp может
        отклонить (focus-stealing prevention) — и окно осталось бы свёрнутым.

        Важно: для окна **не на активном рабочем столе** muffin это сообщение
        игнорирует (проверено дымом) — сначала нужен `set_current_desktop`.
        """
        self.send(win, "_NET_ACTIVE_WINDOW", [SOURCE_PAGER, 0, 0, 0, 0])

    def set_current_desktop(self, desktop):
        """Переключить активный рабочий стол: `_NET_CURRENT_DESKTOP` в root.

        `ev.window` — сам root, как это делают wmctrl/xdotool: muffin принимает
        такой запрос с нулевым timestamp (проверено дымом).
        """
        self.send(self.root, "_NET_CURRENT_DESKTOP", [desktop, 0, 0, 0, 0])

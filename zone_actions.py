#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_actions — чистое ядро действий над окном и зоной для GridSnap.

Действие — это то, что пользователь повесил на клавишу: «свернуть остальные»,
«показать окно зоны», «зеркалить», «терминал в зоне» — и вообще любая его
команда. Запись живёт в `zones.conf` секцией ``[action:<имя>]`` (см.
``zone_config.load_actions``), а запускается командой ``zone-tiler.py action
<имя>`` из сочетания, которое ставит ``setup-hotkeys.py``.

Контракт один на все действия: **действие получает окно и зону** — окно, зону,
её пиксельную рамку, монитор, стол, класс и заголовок окна, путь к конфигу.
Раздаётся двумя каналами (:class:`ActionContext`): переменными окружения
``GRIDSNAP_*`` и подстановками ``{zone}``/``{window}``/``{x}``… в аргументах
команды. Оба канала описаны в :meth:`ActionContext.env` и
:meth:`ActionContext.values` — это и есть публичный API, на который опираются
документация и чужие скрипты.

Модуль чистый: ни X, ни GTK, ни сокета — только арифметика рамок, разбор
команды и подстановки. Поэтому он проверяется обычными юнит-тестами без
X-сервера, как ``zone_tabs``/``zone_reflow``/``zone_picker_nav``. Всё, что
трогает окна (свернуть, поднять, запустить процесс), живёт в ``zone-tiler.py``.
"""

import math
import re
import shlex
from dataclasses import dataclass

import zone_tabs

# Встроенные действия записываются в `[action:<имя>] run` как `builtin:<имя>`:
# так одна и та же запись может быть и своей командой, и готовым действием
# проекта, а ядро не путает одно с другим.
BUILTIN_PREFIX = "builtin:"

# Имена встроенных действий. `minimize-others` — тот же режим, что `focus`
# (окно в зону, остальные окна стола и монитора свёрнуты, повторное нажатие
# возвращает); `show-window` — показать окно зоны; `mirror` — обмен с
# зеркальной зоной; `terminal` — терминал в зоне.
MINIMIZE_OTHERS = "minimize-others"
SHOW_WINDOW = "show-window"
MIRROR = "mirror"
TERMINAL = "terminal"
BUILTINS = (MINIMIZE_OTHERS, SHOW_WINDOW, MIRROR, TERMINAL)

# Терминал `builtin:terminal` по умолчанию: альтернативы Debian, за которыми
# стоит терминал рабочего стола. Переопределяется полем `command` записи.
DEFAULT_TERMINAL = "x-terminal-emulator"

# Ось зеркала: `horizontal` — левое↔правое (умолчание), `vertical` — верх↔низ.
MIRROR_AXES = ("horizontal", "vertical")
DEFAULT_MIRROR_AXIS = "horizontal"

# Префикс переменных окружения и имена подстановок — один список имён на оба
# канала, чтобы они не разъехались.
ENV_PREFIX = "GRIDSNAP_"
PLACEHOLDERS = (
    "window",
    "zone",
    "x",
    "y",
    "w",
    "h",
    "monitor",
    "workspace",
    "class",
    "title",
    "conf",
)

# Подстановка в аргументах команды: `{имя}`. Заменяются только известные имена
# (PLACEHOLDERS); неизвестное `{что-то}` — ошибка конфига, а не тихая пустота:
# опечатка в чужом скрипте иначе выглядела бы как «пустой аргумент».
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


class ActionError(Exception):
    """Отказ действия: неизвестное встроенное имя, битая команда, нет зеркала.

    Разбор конфига заворачивает её в код 3 (`zone_config.load_actions`), а
    отказ по состоянию окна (нет зоны, нечего показывать) раскладчик сообщает
    сам — здесь только то, что не зависит от X.
    """


@dataclass(frozen=True)
class ActionContext:
    """Что действие получает про окно и зону.

    Поля пусты (`None`/`""`), когда неизвестны: окно вне зон — нет `zone` и
    `rect`; действие без зоны — нет `monitor`; окна нет вовсе (пустая зона) —
    нет `window`, `wm_class` и `wm_title`.

    `rect` — пиксельная рамка зоны `(x, y, w, h)` на её мониторе: то, что нужно
    команде, чтобы поставить окно или запустить терминал в зоне, не считая
    доли самой.
    """

    window: int | None = None
    zone: str | None = None
    rect: tuple | None = None
    monitor: str | None = None
    workspace: int | None = None
    wm_class: str = ""
    wm_title: str = ""
    conf: str = ""

    def values(self):
        """``{имя подстановки: значение}`` — для `{zone}`, `{window}` и прочих.

        Неизвестное значение — пустая строка, а не «None»: подстановка попадает
        в чужую команду, и там должно быть видно «ничего», а не выдуманное слово.
        Окно отдаётся в hex (`0x1a00007`) — в том же виде, в каком его печатают
        раскладчик и демон.
        """
        x, y, w, h = self.rect if self.rect else ("", "", "", "")
        return {
            "window": "" if self.window is None else hex(self.window),
            "zone": self.zone or "",
            "x": str(x),
            "y": str(y),
            "w": str(w),
            "h": str(h),
            "monitor": self.monitor or "",
            "workspace": "" if self.workspace is None else str(self.workspace),
            "class": self.wm_class or "",
            "title": self.wm_title or "",
            "conf": self.conf or "",
        }

    def env(self):
        """Окружение для внешней команды: ``GRIDSNAP_*`` поверх текущего.

        Имена — `GRIDSNAP_WINDOW`, `GRIDSNAP_ZONE`, `GRIDSNAP_X`, `GRIDSNAP_Y`,
        `GRIDSNAP_W`, `GRIDSNAP_H`, `GRIDSNAP_MONITOR`, `GRIDSNAP_WORKSPACE`,
        `GRIDSNAP_CLASS`, `GRIDSNAP_TITLE`, `GRIDSNAP_CONF`; значения те же, что
        у :meth:`values` (пустая строка — «неизвестно»).
        """
        return {ENV_PREFIX + name.upper(): value for name, value in self.values().items()}


def is_builtin(run):
    """Начинается ли ``run`` с ``builtin:`` — встроенное действие или команда.

    Имя при этом не проверяется: за него отвечает :func:`builtin_name`, чтобы у
    «это встроенное» и «такого встроенного нет» были разные сообщения.
    """
    return str(run or "").strip().lower().startswith(BUILTIN_PREFIX)


def builtin_name(run):
    """Имя встроенного действия из ``run`` или ``None`` — если это команда.

    ``"builtin:mirror"`` → ``"mirror"``; ``"notify-send …"`` → ``None``.
    Неизвестное имя (``builtin:что-то``) — :class:`ActionError` со списком
    известных: опечатка не должна молча оставить человека без действия.
    """
    text = str(run or "").strip()
    if not is_builtin(text):
        return None
    name = text[len(BUILTIN_PREFIX):].strip().lower()
    if name not in BUILTINS:
        raise ActionError(
            f"неизвестное встроенное действие 'builtin:{name}': можно "
            + ", ".join(f"builtin:{builtin}" for builtin in BUILTINS)
        )
    return name


def expand_command(command, context, splitter=shlex.split):
    """Команда из конфига → ``argv`` с подставленным контекстом.

    Разбор — `shlex.split` (оболочки нет: ни ``|``, ни ``>``, ни ``$VAR``; кому
    нужна оболочка, пишет ``sh -c "…"`` сам), затем подстановки в каждом
    аргументе. Пустая команда и неизвестная подстановка ``{что-то}`` —
    :class:`ActionError`; отсутствующие значения дают пустую строку.
    """
    text = str(command or "").strip()
    if not text:
        raise ActionError("у действия пустая команда в run")
    try:
        tokens = splitter(text)
    except ValueError as exc:
        raise ActionError(f"не разобрать команду действия: {exc}") from None
    if not tokens:
        raise ActionError("у действия пустая команда в run")
    values = context.values()
    return [_expand_token(token, values) for token in tokens]


def _expand_token(token, values):
    """Подставить ``{имя}`` в один аргумент; неизвестное имя — `ActionError`."""

    def replace(match):
        name = match.group(1)
        if name not in values:
            raise ActionError(
                f"в команде действия неизвестная подстановка '{{{name}}}': можно "
                + ", ".join("{" + known + "}" for known in PLACEHOLDERS)
            )
        return values[name]

    return _PLACEHOLDER_RE.sub(replace, token)


def mirror_fractions(rect, axis=DEFAULT_MIRROR_AXIS):
    """Зеркальная рамка в долях: относительно центра рабочей области по оси.

    ``horizontal`` (умолчание) — левое↔правое: ``(1 - x - w, y, w, h)``;
    ``vertical`` — верх↔низ: ``(x, 1 - y - h, w, h)``. Неизвестная ось —
    :class:`ActionError`. Рамка нужна, чтобы найти зону-партнёра по геометрии
    (:func:`find_mirror`), а не по имени: имена зон человек придумывает сам.
    """
    axis = _mirror_axis(axis)
    x, y, w, h = (float(v) for v in rect)
    if axis == "vertical":
        return (x, 1.0 - y - h, w, h)
    return (1.0 - x - w, y, w, h)


def _mirror_axis(axis):
    """Ось зеркала из конфига: `horizontal`/`vertical`, иначе `ActionError`."""
    name = str(axis or DEFAULT_MIRROR_AXIS).strip().lower()
    if name not in MIRROR_AXES:
        raise ActionError(
            f"неизвестная ось зеркала '{name}': можно " + ", ".join(MIRROR_AXES)
        )
    return name


def find_mirror(zones, subject, axis=DEFAULT_MIRROR_AXIS):
    """Имя зеркальной зоны для ``subject`` или ``None`` — если её нет.

    ``zones`` — ``{имя: (x, y, w, h)}`` в долях рабочей области одного монитора,
    в порядке сетки. Зеркальная рамка считается :func:`mirror_fractions`, а
    зоной-партнёром становится та, у которой с ней **наибольшая площадь
    пересечения**; ничьи решаются по близости центров, затем по порядку зон.
    Сама `subject` кандидатом не является: зеркало зоны во весь экран — она же,
    и обмен с собой ничего не делает. Нет пересечения ни с одной зоной — ``None``:
    «в этой сетке зеркальной зоны нет» — честный отказ, а не догадка.
    Неизвестное имя `subject` — :class:`ActionError`.
    """
    if subject not in zones:
        raise ActionError(f"зоны '{subject}' нет в сетке — зеркалить нечего")
    target = mirror_fractions(zones[subject], axis)
    tx, ty = _center(target)
    best = None
    for order, (name, rect) in enumerate(zones.items()):
        if name == subject:
            continue
        area = _intersection_area(rect, target)
        if area <= 0:
            continue
        cx, cy = _center(rect)
        # Меньше — лучше: сначала площадь (с минусом), затем расстояние, затем
        # порядок сетки: два равных кандидата должны разрешаться одинаково
        # в каждом запуске, а не как ляжет словарь.
        rank = (-area, math.hypot(cx - tx, cy - ty), order)
        if best is None or rank < best[0]:
            best = (rank, name)
    return None if best is None else best[1]


def _center(rect):
    """Центр рамки в долях: ``(x + w/2, y + h/2)``."""
    x, y, w, h = (float(v) for v in rect)
    return (x + w / 2.0, y + h / 2.0)


def _intersection_area(left, right):
    """Площадь пересечения двух рамок в долях; не пересекаются — ноль."""
    lx, ly, lw, lh = (float(v) for v in left)
    rx, ry, rw, rh = (float(v) for v in right)
    width = min(lx + lw, rx + rw) - max(lx, rx)
    height = min(ly + lh, ry + rh) - max(ly, ry)
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def zone_windows(frames, zone_rect, tab_rect, tolerance=zone_tabs.FRAME_TOLERANCE):
    """Окна, стоящие в зоне, — в порядке переданного словаря рамок.

    Окно считается стоящим в зоне, если его рамка совпала с рамкой зоны целиком
    (одно окно в зоне) **или** с рамкой вкладки (несколько окон зоны стоят на
    общей рамке). Сравнение — по каждой стороне с допуском ``tolerance``: рамку
    ставит оконный менеджер, точного равенства float недостаточно (тот же смысл
    и та же величина, что у `zone_tabs.FRAME_TOLERANCE`). Рамка не прочитана
    (``None``) — окно пропускается.
    """
    found = []
    for win, frame in frames.items():
        if frame is None:
            continue
        if _same_rect(frame, zone_rect, tolerance) or _same_rect(frame, tab_rect, tolerance):
            found.append(win)
    return found


def _same_rect(left, right, tolerance):
    """Совпали ли две рамки с точностью ``tolerance`` по каждой стороне.

    Копия правила `zone_tabs._same_rect` (оно приватное): здесь оно нужно и для
    «окно в зоне», и для «зона окна», а тянуть приватную функцию из соседнего
    модуля хуже, чем продублировать две строки.
    """
    if left is None or right is None:
        return False
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))


def zone_of_frame(frame, zones_px, height=None, tolerance=zone_tabs.FRAME_TOLERANCE):
    """Имя зоны, в которой стоит окно с рамкой ``frame``, или ``None``.

    ``zones_px`` — ``{имя: (x, y, w, h)}`` в пикселях на одном мониторе, в
    порядке сетки; ``height`` — высота полосы вкладок (`[tabs] height`), чтобы
    принять за «в зоне» и окно на общей рамке вкладки. Первое совпадение по
    порядку сетки — зоны не перекрываются, и двух ответов быть не должно.
    """
    if frame is None:
        return None
    strip = zone_tabs.STRIP_HEIGHT if height is None else int(height)
    for name, rect in zones_px.items():
        tab = zone_tabs.stack_rect(tuple(float(v) for v in rect), 2, strip)
        if _same_rect(frame, rect, tolerance) or _same_rect(frame, tab, tolerance):
            return name
    return None

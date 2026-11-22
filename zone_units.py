#!/usr/bin/env python3
"""zone_units — единственное место перевода логических пикселей GTK в физические X.

Зачем два пространства: GTK (и `Gdk.Monitor.get_workarea()`) считает в
**логических** пикселях — при `GDK_SCALE=2` монитор 1920x1080 виден как 960x540, —
а сырой X11, которым двигает окна раскладчик, — в **физических**. Доли зоны
безразмерны, поэтому расходятся не доли, а то, от чего их считают: 0.8358 от 960
дают 802, а от 1920 — 1605.

Правило: арифметика зон — в физических пикселях (это то, что уходит в X), а
логические нужны там, где координаты читает или задаёт GTK — окно оверлея пикера,
окно HUD, полоса вкладок. Обе стороны считает этот модуль, поэтому `round` в
переводе один на всю программу.

Дробный масштаб: `GDK_SCALE` у GTK3 целочисленный, а дробный масштаб Cinnamon на
X11 делается растяжением выхода, где `_NET_WORKAREA` и GDK уже в одних и тех же
(масштабированных) пикселях и переводить нечего. Поэтому отдельного правила
округления для 1.5 тут нет: края переводятся от общих линий — `round` от левого и
от правого края, ширина — их разность, — и при любом масштабе соседние зоны
остаются без щелей, тем же правилом, что и `Grid._px`.

Модуль чистый: ни GTK, ни X, ни подпроцессов — только арифметика над кортежами
`(x, y, w, h)`; масштаб ему передаёт вызывающий.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def scale_value(scale):
    """Масштаб как целое не меньше 1; `None`, 0 и мусор — 1.

    Масштаба нет (обычный экран, GDK не ответил) — это ровно 1, а не ошибка:
    тогда перевод не меняет ничего, и вызывающему не нужна особая ветка.
    """
    try:
        value = int(scale)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def physical_rect(rect, scale):
    """Логическая рамка `(x, y, w, h)` → физическая.

    Края считаются от одних и тех же линий: `x1 = round(x*s)`,
    `x2 = round((x+w)*s)`, ширина — их разность. Так соседние рамки, сомкнутые в
    логических пикселях, остаются сомкнутыми и в физических.
    """
    x, y, w, h = rect
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y), int(w), int(h))
    x1, x2 = round(x * s), round((x + w) * s)
    y1, y2 = round(y * s), round((y + h) * s)
    return (x1, y1, x2 - x1, y2 - y1)


def logical_rect(rect, scale):
    """Физическая рамка `(x, y, w, h)` → логическая; обратна :func:`physical_rect`."""
    x, y, w, h = rect
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y), int(w), int(h))
    x1, x2 = round(x / s), round((x + w) / s)
    y1, y2 = round(y / s), round((y + h) / s)
    return (x1, y1, x2 - x1, y2 - y1)


def physical_point(point, scale):
    """Логическая точка `(x, y)` → физическая."""
    x, y = point
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y))
    return (round(x * s), round(y * s))


def logical_point(point, scale):
    """Физическая точка `(x, y)` → логическая."""
    x, y = point
    s = scale_value(scale)
    if s == 1:
        return (int(x), int(y))
    return (round(x / s), round(y / s))


def scale_at(scales, index):
    """Масштаб монитора `index` из списка `scales`; нет его — 1.

    Список масштабов короче списка областей — обычное дело, когда GDK недоступен
    (резерв `_NET_WORKAREA`): тогда масштаба нет и перевод ничего не меняет.
    """
    try:
        if 0 <= index < len(scales):
            return scale_value(scales[index])
    except TypeError:
        pass
    return 1


def scale_for_point(areas, scales, point):
    """Масштаб монитора, в чью область попала физическая точка.

    Точка мимо всех областей (стык мониторов, окно утащили за край) — масштаб
    ближайшего по центру, как выбирает монитор `zone-tiler.monitor_for_point`;
    областей нет — 1.
    """
    if not areas:
        return 1
    x, y = point
    best, best_d = 0, None
    for i, (ax, ay, aw, ah) in enumerate(areas):
        if ax <= x < ax + aw and ay <= y < ay + ah:
            return scale_at(scales, i)
        dx = (ax + aw / 2.0) - x
        dy = (ay + ah / 2.0) - y
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = i, d
    return scale_at(scales, best)

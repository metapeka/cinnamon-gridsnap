#!/usr/bin/env python3
"""zone_picker_nav — чистая навигация выбора зоны для оверлея ``zone-picker.py``.

Здесь живёт вся арифметика визуального выбора зоны: начальное выделение,
перемещение выделения стрелками по геометрии зон, зона под курсором и границы
списка. Модуль намеренно не импортирует ни GTK, ни cairo, ни X: его можно и
нужно проверять обычными юнит-тестами без X-сервера (инвариант проекта).

Зона — что угодно с атрибутами ``x, y, w, h`` (как ``zone_config.Zone``) или
кортеж ``(x, y, w, h)``. Координаты — доли рабочей области (0..1), как в
``zones.conf``. Выделение — индекс в списке зон; ``None`` — пустой список.
"""

DIRECTIONS = ("left", "right", "up", "down")
EPS = 1e-9


def rect(zone):
    """Рамка зоны как ``(x, y, w, h)``: и из объекта с атрибутами, и из кортежа."""
    if hasattr(zone, "x"):
        return float(zone.x), float(zone.y), float(zone.w), float(zone.h)
    x, y, w, h = zone
    return float(x), float(y), float(w), float(h)


def center(zone):
    """Центр зоны в долях рабочей области — по нему сравниваются направления."""
    x, y, w, h = rect(zone)
    return (x + w / 2.0, y + h / 2.0)


def first_index(zones):
    """Начальное выделение: ``0`` для непустого списка, иначе ``None``."""
    return 0 if zones else None


def clamp_index(zones, index):
    """Индекс внутри ``[0, len)``; ``None``/мусор/выход за край — к ближайшему.

    Для пустого списка всегда ``None``: тогда выделять нечего.
    """
    if not zones:
        return None
    if index is None:
        return 0
    try:
        i = int(index)
    except (TypeError, ValueError):
        return 0
    return min(max(0, i), len(zones) - 1)


def _overlap(a, b, axis):
    """Длина перекрытия двух прямоугольников по оси, перпендикулярной движению.

    Нужна, чтобы «вправо» из зоны с полосой справа выбирало соседа в той же
    строке, а не случайную зону в другой строке с близким центром.
    """
    ax, ay, aw, ah = rect(a)
    bx, by, bw, bh = rect(b)
    if axis == 0:  # движение по X — считаем перекрытие по Y
        lo, hi = max(ay, by), min(ay + ah, by + bh)
    else:  # движение по Y — перекрытие по X
        lo, hi = max(ax, bx), min(ax + aw, bx + bw)
    return max(0.0, hi - lo)


def move_selection(zones, index, direction):
    """Индекс соседней зоны в направлении; если соседа нет — тот же индекс.

    Кандидатом считается только зона, центр которой лежит в нужную сторону от
    центра текущей. Среди кандидатов побеждает сперва тот, что перекрывается с
    текущей по перпендикулярной оси, затем ближайший по перпендикуляру, затем
    ближайший по оси движения. Так «вправо» из широкой зоны ведёт в полосу
    справа (а не в зону снизу-справа), а из центра сетки — в соседнюю клетку.
    Неизвестное направление ничего не меняет.
    """
    if not zones:
        return None
    index = clamp_index(zones, index)
    if direction not in DIRECTIONS:
        return index
    sign = 1.0 if direction in ("right", "down") else -1.0
    axis = 0 if direction in ("left", "right") else 1
    cur = center(zones[index])
    best = None
    best_key = None
    for i, z in enumerate(zones):
        if i == index:
            continue
        c = center(z)
        along = sign * (c[axis] - cur[axis])
        if along <= EPS:  # центр не в ту сторону — не сосед
            continue
        perp = abs(c[1 - axis] - cur[1 - axis])
        has_overlap = _overlap(z, zones[index], axis) > EPS
        key = (0 if has_overlap else 1, perp, along, i)
        if best_key is None or key < best_key:
            best_key = key
            best = i
    return index if best is None else best


def zone_at(zones, fx, fy):
    """Индекс зоны под точкой ``(fx, fy)`` в долях рабочей области.

    ``None`` — если точка не попала ни в одну зону (например, в «щель» между
    свободными прямоугольниками). Зоны могут перекрываться: под курсором
    считается та, что нарисована позже (последняя в списке) — как в редакторе.
    """
    for i in range(len(zones) - 1, -1, -1):
        x, y, w, h = rect(zones[i])
        if x - EPS <= fx < x + w + EPS and y - EPS <= fy < y + h + EPS:
            return i
    return None

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_reflow — чистая геометрия reflow для GridSnap (без X11 и без GTK).

Когда окно, поставленное GridSnap, закрывается и его зона освобождается, занятая
соседняя зона расширяется на освободившуюся площадь — если объединение двух зон
снова прямоугольник. Работает в долях рабочей области (0..1), как `zones.conf`.

Публичная функция одна: `reflow(zones, freed, occupied)`.
"""

# Допуск сравнения долей. Доли приходят из конфига и арифметики 0..1, поэтому
# точного равенства float недостаточно.
EPS = 1e-9


def _close(a, b):
    """Равны ли доли `a` и `b` в пределах EPS."""
    return abs(a - b) <= EPS


def _union(a, b):
    """Bounding box двух rect'ов (x, y, w, h)."""
    x = min(a[0], b[0])
    y = min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    return (x, y, x2 - x, y2 - y)


def _shared_edge(freed, cand):
    """Длина общего ребра `freed` и `cand`, если они соседи с прямоугольным
    объединением; иначе None.

    Вертикальные соседи (один над другим) — совпадают x и w, y-диапазоны
    стыкуются; общее ребро горизонтально, его длина равна w.
    Горизонтальные соседи (бок о бок) — совпадают y и h, x-диапазоны стыкуются;
    общее ребро вертикально, его длина равна h.
    """
    fx, fy, fw, fh = freed
    cx, cy, cw, ch = cand

    if _close(fx, cx) and _close(fw, cw):
        if _close(fy + fh, cy) or _close(cy + ch, fy):
            return fw

    if _close(fy, cy) and _close(fh, ch):
        if _close(fx + fw, cx) or _close(cx + cw, fx):
            return fh

    return None


def reflow(zones, freed, occupied):
    """Вернуть {zone: (x, y, w, h)} — новые rect'ы после освобождения `freed`.

    zones:    {имя: (x, y, w, h)} в долях 0..1
    freed:    имя освобождённой зоны
    occupied: множество имён занятых зон

    Кандидат — занятая зона, делящая с `freed` ребро, объединение с которой —
    прямоугольник. Выигрывает кандидат с самым длинным общим ребром; при равной
    длине — лексикографически меньшее имя. v1 — без каскада: возвращается ровно
    один расширенный rect (или {}).
    """
    if freed not in zones or freed in occupied:
        return {}

    src = zones[freed]
    best_name = None
    best_edge = None

    for name in occupied:
        if name == freed or name not in zones:
            continue
        edge = _shared_edge(src, zones[name])
        if edge is None:
            continue
        if (
            best_name is None
            or edge > best_edge + EPS
            or (abs(edge - best_edge) <= EPS and name < best_name)
        ):
            best_name, best_edge = name, edge

    if best_name is None:
        return {}
    return {best_name: _union(src, zones[best_name])}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_tabstrip_nav — чистая раскладка полосы вкладок для ``zone-tabstrip.py``.

Здесь живёт всё, что полоса вкладок делает с числами и строками: разбор
построчного снимка от демона, рамки вкладок по ширине полосы, подпись под
ширину вкладки и вкладка под точкой клика. Модуль намеренно не импортирует ни
GTK, ни cairo, ни X: его можно и нужно проверять обычными юнит-тестами без
X-сервера (инвариант проекта, как у ``zone_picker_nav``/``zone_tabs``).

Что здесь есть:

* :func:`parse_snapshot` — строка снимка ``{"strips": [...]}`` от демона →
  список полос; ``None`` — строка не снимок (её пропускают молча), ``[]`` —
  снимок «рисовать нечего», и это разные вещи;
* :func:`tab_frames` — рамки вкладок по ширине полосы: поровну, последняя
  вкладка забирает остаток деления, поэтому вкладки не вылезают за зону;
* :func:`fit_title` — подпись, обрезанная под ширину вкладки (``…`` вместо
  хвоста). Скролла и второй строки в полосе нет: лишние подписи обрезаются;
* :func:`layout` — по записи снимка то, что рисуется: окно, подпись, рамка,
  активность;
* :func:`tab_at` — индекс вкладки под точкой клика (клик уходит в сокет демона).

Рамки — пиксельные ``(x, y, w, h)`` в координатах экрана, как в
``zone_tabs.strip_record``: полоса рисуется поверх рабочего стола.
"""

import json
import math

# Зазор между вкладками и отступ подписи от краёв вкладки, px. Полоса узкая,
# поэтому зазор в один пиксель: он отделяет вкладки, не съедая ширину подписей.
TAB_GAP = 1
TAB_PADDING = 8

# Подпись обрезается «на глаз», без Pango и cairo: ширина одного символа
# оценивается средним числом для шрифта полосы. Точный расчёт был бы заботой
# рисующего, а обрезать надо до него, чтобы не заказывать подпись шире вкладки.
CHAR_WIDTH = 7

# Многоточие вместо обрезанного хвоста подписи.
ELLIPSIS = "…"

EPS = 1e-9


def _rect_values(value):
    """``(x, y, w, h)`` из чего угодно похожего на рамку; ``None`` — мусор."""
    try:
        x, y, w, h = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) for v in (x, y, w, h)):
        return None
    return x, y, w, h


def record_rect(record):
    """Рамка полосы из записи снимка; ``None`` — рисовать по ней нечего.

    Нулевая или отрицательная ширина — тоже ``None``: окно нулевого размера не
    покажет ни одной подписи, а рисовать его «на всякий случай» значит плодить
    невидимые окна поверх рабочего стола.
    """
    if not isinstance(record, dict):
        return None
    rect = _rect_values(record.get("rect"))
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect


def _tabs_of(record):
    tabs = record.get("tabs") if isinstance(record, dict) else None
    return [tab for tab in tabs if isinstance(tab, dict)] if isinstance(tabs, list) else []


def parse_snapshot(line):
    """Снимок из строки stdin → список полос; ``None`` — это не снимок.

    Снимок полный, а не дельта, поэтому ``[]`` (пустой список) значит «полос
    нет, окна убрать», а ``None`` — «строку не разобрали, состояние не менять»:
    демон может прислать мусор или недописанную строку, и полоса не должна из-за
    этого исчезать с экрана. Записи без вкладок и с битой рамкой отсеиваются:
    рисовать по ним нечего.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(line, str) or not line.strip():
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    strips = payload.get("strips")
    if not isinstance(strips, list):
        return None
    return [
        record
        for record in strips
        if record_rect(record) is not None and _tabs_of(record)
    ]


def tab_frames(rect, count, gap=TAB_GAP):
    """Рамки ``count`` вкладок в полосе `rect` — слева направо, без выхода за зону.

    Ширина полосы делится поровну; последняя вкладка забирает остаток деления,
    поэтому сумма рамок и зазоров ровно равна ширине полосы. Полоса уже, чем
    зазоры между вкладками, — зазоры отбрасываются: вкладки делят её целиком,
    но всё равно остаются в её пределах.
    """
    count = int(count)
    if count <= 0:
        return []
    values = _rect_values(rect)
    if values is None:
        return []
    x, y, w, h = values
    if w <= 0 or h <= 0:
        return []
    gap = max(0.0, float(gap))
    avail = w - gap * (count - 1)
    if avail <= 0:
        gap, avail = 0.0, w
    step = avail / count
    frames, left = [], x
    for index in range(count):
        right = x + w if index == count - 1 else left + step
        frames.append((left, y, max(0.0, right - left), h))
        left = right + gap
    return frames


def label_width(frame, padding=TAB_PADDING):
    """Сколько пикселей остаётся подписи внутри вкладки `frame` (может быть 0)."""
    return max(0.0, float(frame[2]) - 2 * max(0.0, float(padding)))


def fit_title(title, width, char_width=CHAR_WIDTH):
    """Подпись, влезающая в `width` пикселей: хвост заменяется на ``…``.

    Ширина символа оценивается средним (`char_width`), а не измеряется: точная
    раскладка текста — забота рисующего, а решение «сколько символов оставить»
    принимается здесь, до него. Не влезает даже многоточие — подписи нет.
    """
    title = "" if title is None else str(title)
    width = float(width)
    if width <= 0 or char_width <= 0 or not title:
        return ""
    max_chars = int(width // char_width)
    if max_chars <= 0:
        return ""
    if len(title) <= max_chars:
        return title
    if max_chars == 1:
        return ELLIPSIS
    return title[: max_chars - 1] + ELLIPSIS


def layout(record):
    """Что рисовать по записи снимка: ``[{window, title, label, active, rect}]``.

    ``[]`` — рисовать нечего (полоса без вкладок или её рамка не разобралась).
    Подписи уже обрезаны под ширину своей вкладки, поэтому рисующему не надо
    считать ни ширину шрифта, ни зазоры.
    """
    rect = record_rect(record)
    if rect is None:
        return []
    tabs = _tabs_of(record)
    frames = tab_frames(rect, len(tabs))
    rows = []
    for tab, frame in zip(tabs, frames):
        title = tab.get("title")
        rows.append(
            {
                "window": tab.get("window"),
                "title": "" if title is None else str(title),
                "label": fit_title(title, label_width(frame)),
                "active": bool(tab.get("active")),
                "rect": frame,
            }
        )
    return rows


def tab_at(frames, point):
    """Индекс вкладки под точкой ``point = (x, y)``; ``None`` — мимо вкладок.

    Точка — в тех же координатах, что и рамки (экранные пиксели). Соседние
    вкладки делят границу, поэтому левый край вкладки принадлежит ей, а правый
    уже нет — как у ``zone_picker_nav.zone_at``: клик на границе не может
    попасть в две вкладки сразу.
    """
    if not frames or point is None:
        return None
    try:
        px, py = (float(v) for v in point)
    except (TypeError, ValueError):
        return None
    for index, (x, y, w, h) in enumerate(frames):
        if x - EPS <= px < x + w - EPS and y - EPS <= py < y + h - EPS:
            return index
    return None


def tab_under(record, point):
    """Окно под точкой клика; ``None`` — точка мимо вкладок или окна нет.

    Одна функция на клик: по записи снимка считает и рамки, и попадание, —
    чтобы ``zone-tabstrip.py`` не собирал их сам по частям и не разошёлся с
    тестами.
    """
    rows = layout(record)
    index = tab_at([row["rect"] for row in rows], point)
    return None if index is None else rows[index]["window"]

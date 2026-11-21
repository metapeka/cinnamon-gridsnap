#!/usr/bin/env python3
"""zone_geom — чистая геометрия редактора зон (без GTK и без X).

Вынесена из `zone-editor.py`, чтобы расчёты перемещения, изменения размера,
прилипания и границ проверялись юнит-тестами без X-сервера. Координаты — доли
рабочей области (0..1), как в `zones.conf`.

Модуль не импортирует ни GTK, ни `zone_config`: он работает с чем угодно, у чего
есть атрибуты `x, y, w, h`, и с кортежами `(x, y, w, h)`.

Здесь же лежат чистые правила наборов зон на монитор: какой набор показать для
монитора (`set_for_monitor`), что писать в статусе (`set_origin_text`) и какие
секции `[zones:<выход>]` записать (`save_plan`). Они не про пиксели, но живут в
этом модуле по той же причине: `zone-editor.py` тянет GTK, а эти решения нужны
юнит-тестам без X и без GTK.
"""

MIN_SIZE = 0.05          # минимальный размер зоны (доля экрана)
SNAP_PX = 9              # прилипание, пикселей
MARGIN = 26              # поля холста редактора вокруг «экрана», пикселей
EDGE_PX = 7              # полоса захвата края/угла, пикселей
SLACK_PX = 4             # допуск попадания в зону мимо её границы, пикселей
GUIDES = [0.0, 0.25, 1 / 3, 0.5, 2 / 3, 0.75, 1.0]


def _xywh(rect):
    """Рамка как (x, y, w, h): и из объекта с атрибутами, и из кортежа."""
    if hasattr(rect, "x"):
        return rect.x, rect.y, rect.w, rect.h
    return tuple(rect)


def screen_rect(canvas_w, canvas_h, workarea, margin=MARGIN):
    """Прямоугольник «экрана» на холсте: (sx, sy, sw, sh) с полями и центровкой."""
    _, _, mw, mh = workarea
    avail_w = max(50, canvas_w - 2 * margin)
    avail_h = max(50, canvas_h - 2 * margin)
    scale = min(avail_w / mw, avail_h / mh)
    w, h = mw * scale, mh * scale
    return ((canvas_w - w) / 2, (canvas_h - h) / 2, w, h)


def to_px(screen, rect):
    """Доли -> пиксели холста."""
    sx, sy, sw, sh = screen
    x, y, w, h = _xywh(rect)
    return (sx + x * sw, sy + y * sh, w * sw, h * sh)


def to_frac(screen, px, py):
    """Пиксели холста -> доли."""
    sx, sy, sw, sh = screen
    return ((px - sx) / sw, (py - sy) / sh)


def snap_tol(screen, axis, snap_px=SNAP_PX):
    """Допуск прилипания в долях по оси (из пикселей)."""
    _, _, sw, sh = screen
    return snap_px / (sw if axis == "x" else sh)


def snap_frac(frac, axis, tol, zones, skip=None):
    """Подтянуть долю к ближайшей направляющей/краю зоны в пределах `tol`.

    `skip` — индекс перетаскиваемой зоны, её края в приманки не берутся.
    """
    cand = list(GUIDES)
    for i, z in enumerate(zones):
        if skip is not None and i == skip:
            continue
        x, y, w, h = _xywh(z)
        if axis == "x":
            cand += [x, x + w]
        else:
            cand += [y, y + h]
    best = None
    for c in cand:
        d = abs(c - frac)
        if d <= tol and (best is None or d < abs(best - frac)):
            best = c
    return frac if best is None else best


def snap_move(pos, size, axis, tol, zones, skip=None):
    """Двигаемое ребро: сначала левый/верхний край, иначе правый/нижний."""
    left = snap_frac(pos, axis, tol, zones, skip)
    if abs(left - pos) > 1e-9:
        return min(max(0.0, left), 1.0 - size)
    right = snap_frac(pos + size, axis, tol, zones, skip)
    if abs(right - (pos + size)) > 1e-9:
        return min(max(0.0, right - size), 1.0 - size)
    return pos


def drag_rect(start, mode, edges, start_f, cur_f, zones, tol_x, tol_y,
              skip=None, min_size=MIN_SIZE):
    """Новая рамка (x, y, w, h) при перетаскивании мышью.

    start — рамка на момент нажатия (кортеж или объект), mode — "move" или
    "resize", edges — множество сторон ("l", "r", "t", "b"),
    start_f/cur_f — доли курсора в начале и сейчас.
    """
    x, y, w, h = _xywh(start)
    dx, dy = cur_f[0] - start_f[0], cur_f[1] - start_f[1]

    if mode == "move":
        nx = min(max(0.0, x + dx), 1.0 - w)
        ny = min(max(0.0, y + dy), 1.0 - h)
        return (
            snap_move(nx, w, "x", tol_x, zones, skip),
            snap_move(ny, h, "y", tol_y, zones, skip),
            w,
            h,
        )

    x1, y1, x2, y2 = x, y, x + w, y + h
    if "l" in edges:
        x1 = min(snap_frac(x1 + dx, "x", tol_x, zones, skip), x2 - min_size)
    if "r" in edges:
        x2 = max(snap_frac(x2 + dx, "x", tol_x, zones, skip), x1 + min_size)
    if "t" in edges:
        y1 = min(snap_frac(y1 + dy, "y", tol_y, zones, skip), y2 - min_size)
    if "b" in edges:
        y2 = max(snap_frac(y2 + dy, "y", tol_y, zones, skip), y1 + min_size)
    x1, y1 = max(0.0, x1), max(0.0, y1)
    x2, y2 = min(1.0, x2), min(1.0, y2)
    return (x1, y1, x2 - x1, y2 - y1)


def nudge(rect, dx, dy, resize=False, min_size=MIN_SIZE):
    """Сдвиг/изменение размера с клавиатуры; те же границы, что у мыши."""
    x, y, w, h = _xywh(rect)
    if resize:
        w = min(1 - x, max(min_size, w + dx))
        h = min(1 - y, max(min_size, h + dy))
        return (x, y, w, h)
    x = min(max(0.0, x + dx), 1.0 - w)
    y = min(max(0.0, y + dy), 1.0 - h)
    return (x, y, w, h)


def scroll_resize(rect, delta, min_size=MIN_SIZE):
    """Ширина зоны от колеса мыши: границы [min_size, 1 - x]."""
    x, y, w, h = _xywh(rect)
    return (x, y, min(1 - x, max(min_size, w + delta)), h)


def hit_test(px, py, screen, zones, edge_px=EDGE_PX, slack_px=SLACK_PX):
    """Что под курсором: (индекс, "move"/"resize", стороны) или (None, "", set()).

    Зоны проверяются сверху вниз (последняя нарисованная — первая), мимо границы
    допускается `slack_px`, захват края/угла — `edge_px`.
    """
    for i in reversed(range(len(zones))):
        x, y, w, h = to_px(screen, zones[i])
        if x - slack_px <= px <= x + w + slack_px and y - slack_px <= py <= y + h + slack_px:
            edges = set()
            if abs(px - x) <= edge_px:
                edges.add("l")
            if abs(px - (x + w)) <= edge_px:
                edges.add("r")
            if abs(py - y) <= edge_px:
                edges.add("t")
            if abs(py - (y + h)) <= edge_px:
                edges.add("b")
            if x <= px <= x + w and y <= py <= y + h:
                return i, ("resize" if edges else "move"), edges
            return i, "move", set()
    return None, "", set()


# ------------------------------------------------- наборы зон на монитор
#
# Набор по умолчанию живёт в `[zones]`, набор монитора — в `[zones:<выход>]`.
# Монитор без своей секции наследует общий набор, поэтому секция нужна только
# там, где набор реально отличается (иначе файл обрастает копиями `[zones]`).

SIG_DIGITS = 4           # точность сравнения геометрии — как у Zone.fmt()


def _zone_fields(zone):
    """`(имя, x, y, w, h, привязка)` зоны или кортежа — без требований к типу."""
    if hasattr(zone, "name"):
        return (
            str(zone.name),
            float(zone.x),
            float(zone.y),
            float(zone.w),
            float(zone.h),
            str(getattr(zone, "binding", "") or ""),
        )
    values = tuple(zone)
    if len(values) < 4:
        raise ValueError(f"zone_geom: в наборе не зона, а {zone!r}")
    name = str(values[0]) if len(values) > 4 else ""
    x, y, w, h = values[-4], values[-3], values[-2], values[-1]
    return name, float(x), float(y), float(w), float(h), ""


def zone_set_signature(zones):
    """Отпечаток набора для сравнения: имена и геометрия в порядке файла.

    Привязки в отпечаток не входят: `[keys]` общая на файл и живёт по имени
    зоны, так что набор с теми же зонами и другими клавишами — тот же набор.
    """
    out = []
    for zone in zones:
        name, x, y, w, h, _binding = _zone_fields(zone)
        out.append((name, round(x, SIG_DIGITS), round(y, SIG_DIGITS),
                    round(w, SIG_DIGITS), round(h, SIG_DIGITS)))
    return tuple(out)


def set_for_monitor(monitor, monitor_sets, default_zones):
    """Набор, который показать для монитора: ``(зоны, своя ли секция)``.

    Есть набор монитора — его копия и ``True`` (свой); нет — копия общего
    ``[zones]`` и ``False`` (наследует). Копии обязательны: правка холста одного
    монитора не должна молча менять набор другого.
    """
    own = (monitor_sets or {}).get(monitor)
    if own:
        return [zone.copy() for zone in own], True
    return [zone.copy() for zone in default_zones], False


def set_origin_text(monitor, own, shared=False, offline=False):
    """Что показано на холсте — одной строкой для статуса редактора.

    ``shared`` — это общий набор `[zones]`, ``own`` — своя секция монитора,
    иначе набор унаследован. Отключённый выход помечается отдельно.
    """
    if shared:
        text = "общий набор [zones] — его наследуют остальные мониторы"
    elif own:
        text = f"свой набор [zones:{monitor}]"
    else:
        text = "наследует [zones]"
    return f"{text} · нет на экране" if offline else text


def _binding_of(zone):
    """Привязка зоны (``[keys]``) как строка; у кортежа её нет."""
    return str(getattr(zone, "binding", "") or "").strip()


def _without_seen_bindings(zones, seen):
    """Копии зон, у которых имя уже встречалось в `[keys]`: привязка гасится.

    `[keys]` одна на файл, поэтому две секции с зоной одного имени не должны
    писать клавишу дважды. Сами наборы не меняются — пишутся копии.
    """
    out = []
    for zone in zones:
        copy = zone.copy()
        if copy.name in seen:
            copy.binding = ""
        elif _binding_of(copy):
            seen.add(copy.name)
        out.append(copy)
    return out


def save_plan(default_zones, monitor_sets, edited=(), onscreen=None, existing=None):
    """План записи: ``({монитор: зоны}, [монитор])`` — что писать и что удалять.

    Секция ``[zones:<монитор>]`` пишется, если набор монитора отличается от
    общего ``[zones]`` или монитор правили явно (``edited``); секции, где набор
    совпал с общим и правок не было, удаляются — но только у мониторов на
    экране (``onscreen``) и только если секция уже есть в файле (``existing``).
    Отключённый выход не удаляется никогда: его секцию либо пишем как есть
    (если правили), либо не трогаем. Пустой набор монитора не пишется.

    Зоны возвращаются копиями: у зон, чьё имя уже есть в общем наборе, гасится
    привязка — `[keys]` общая, и дублей в ней быть не должно.
    """
    default_sig = zone_set_signature(default_zones)
    onscreen = None if onscreen is None else set(onscreen)
    existing = set(existing or ())
    seen = {zone.name for zone in default_zones if _binding_of(zone)}
    write, drop = {}, []
    for monitor, zones in (monitor_sets or {}).items():
        if not zones:
            continue
        if monitor in edited or zone_set_signature(zones) != default_sig:
            write[monitor] = _without_seen_bindings(zones, seen)
        elif (onscreen is None or monitor in onscreen) and monitor in existing:
            drop.append(monitor)
    return write, drop


def apply_save_plan(path, default_zones, plan, extra=None):
    """Записать `zones.conf` по плану :func:`save_plan`; возвращает путь.

    ``zone_config`` импортируется здесь, а не наверху модуля: геометрия не
    должна тянуть конфиг-слой и `zone-tiler.py` только ради импорта. Файл
    переписывается одним проходом через :func:`zone_config.save_zone_sets`:
    наборы мониторов читаются из файла один раз, из них убираются секции из
    ``drop``, к ним добавляются записываемые из ``write``. Чужие секции самого
    файла (`[rules]`, `[daemon]`, `[zones:*]` отключённых выходов) при этом
    сохраняются — их берёт из файла сам `save_zone_sets`.

    Повторов в `[keys]` не будет: их отсекает сам писатель, поэтому никакой
    текстовой чистки после записи не нужно.
    """
    import zone_config as zc  # noqa: PLC0415 — ленивый импорт, см. docstring

    write, drop = plan
    monitors = {
        monitor: zones
        for monitor, zones in zc.load_zone_sets(path).items()
        if monitor is not None
    }
    for monitor in drop:
        monitors.pop(monitor, None)
    for monitor, zones in write.items():
        if zones:
            monitors[monitor] = list(zones)
        else:
            monitors.pop(monitor, None)
    return zc.save_zone_sets(path, list(default_zones), monitors, extra)

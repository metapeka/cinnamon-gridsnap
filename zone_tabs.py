#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_tabs — чистое ядро вкладок внутри зоны для GridSnap.

Вкладки — это когда в одной зоне стоит несколько окон: одно видно целиком,
остальные лежат на той же рамке и ждут переключения. Здесь живёт вся арифметика
этого и оба протокола, которыми обмениваются демон, полоса и раскладчик; X, GTK
и сокета тут нет, поэтому модуль проверяется обычными юнит-тестами без X-сервера
(инвариант проекта, как у ``zone_geom``/``zone_reflow``/``zone_picker_nav``).

Что здесь есть:

* геометрия — :func:`strip_rect` (где полоса вкладок), :func:`tab_rect` (рамка
  окна-вкладки: зона минус полоса) и :func:`stack_rect` (то же правило по числу вкладок:
  одна — вся зона). Узкая зона полосы не получает: `strip_rect` вернёт ``None``, а
  `tab_rect` — рамку зоны целиком;
* выбор окон — :func:`tabs_of_zone` (какие окна стоят вкладками этой зоны, снизу
  вверх) и :func:`next_tab` (следующее по кругу);
* протокол «демон → полоса» — :func:`strip_record`/:func:`strips_payload`:
  построчный JSON в stdin процесса ``zone-tabstrip.py``;
* протокол «полоса/клавиша → демон» — :func:`tabs_switch_command` (клик по
  вкладке) и :func:`tabs_next_command` (``zone-tiler.py tabs-next`` по клавише):
  обычные запросы сокета демона, как у ``zone_ipc``.

Рамки — пиксельные ``(x, y, w, h)`` в координатах экрана: полоса рисуется поверх
рабочего стола, а не в долях, поэтому доли в пиксели переводит вызывающий
(``Grid.rect``), как и везде в проекте.
"""

import json

# Высота полосы вкладок и минимум, который обязан остаться окну под ней.
# Величины с умолчаниями, а не догмы: высота приходит из ``[tabs] height``.
STRIP_HEIGHT = 22
MIN_WINDOW_H = 44

# Допуск сравнения рамок при поиске вкладок зоны, px. Тот же смысл и та же
# величина, что у ``PLACED_TOLERANCE`` в демоне: рамку ставит оконный менеджер,
# точного равенства float недостаточно.
FRAME_TOLERANCE = 8

# Имена команд сокета демона, которые приходят не от ``gridsnap-ctl``.
TABS_NEXT = "tabs-next"
TABS_SWITCH = "tabs-switch"

EPS = 1e-9


def strip_rect(zone_rect, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """Рамка полосы вкладок вверху зоны — или ``None``, если полоса не влезает.

    Полоса отнимает высоту у окна, поэтому в узкой зоне (окну не остаётся
    `min_window_h`) она не рисуется совсем. Вкладки при этом работают: окна всё
    равно собираются на общей рамке и переключаются клавишей, просто на экране
    нет подсказки, что их несколько.
    """
    x, y, w, h = (float(v) for v in zone_rect)
    if h - float(height) < float(min_window_h):
        return None
    return (x, y, w, float(height))


def tab_rect(zone_rect, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """Рамка окна-вкладки: зона минус полоса; без полосы — сама зона.

    Считается через :func:`strip_rect`, поэтому полоса и окно не могут
    разойтись: либо есть полоса и окно сдвинуто вниз на её высоту, либо полосы
    нет и окно занимает зону целиком.
    """
    x, y, w, h = (float(v) for v in zone_rect)
    if strip_rect(zone_rect, height, min_window_h) is None:
        return (x, y, w, h)
    return (x, y + float(height), w, h - float(height))


def stack_rect(zone_rect, count, height=STRIP_HEIGHT, min_window_h=MIN_WINDOW_H):
    """Рамка окон зоны по числу её вкладок.

    Одна вкладка — зона целиком: полосы нет, и окно занимает всю зону, как до волны вкладок. Две и
    больше — рамка вкладки (:func:`tab_rect`), потому что сверху появилась полоса. Правило одно на
    демон и раскладчик (`apply`/`arrange`/`stack`), иначе они разойдутся на одном и том же числе окон.
    """
    if count < 2:
        return tuple(float(v) for v in zone_rect)
    return tab_rect(zone_rect, height, min_window_h)


def _same_rect(left, right, tolerance):
    """Совпали ли две рамки с точностью `tolerance` по каждой стороне."""
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))


def tabs_of_zone(frames, tab_frame, tolerance=FRAME_TOLERANCE):
    """Окна зоны вкладками — в порядке снизу вверх.

    ``frames`` — ``{окно: (x, y, w, h)}`` в порядке ``_NET_CLIENT_LIST`` (снизу
    вверх), ``tab_frame`` — рамка, на которой стоят вкладки (:func:`tab_rect`).

    Файла состояния у вкладок нет: состав выводится из того, что видно на
    экране, — вкладкой считается окно, чья рамка совпала с общей. Поэтому
    перезапуск демона ничего не теряет, а чистить мёртвые XID не нужно. Рамку
    не прочитали (окно исчезло) — ``None`` в словаре, окно пропускается.
    """
    return [
        win
        for win, frame in frames.items()
        if frame is not None and _same_rect(frame, tab_frame, tolerance)
    ]


def next_tab(ids, current):
    """Следующее окно кольца после `current`; ``None`` — если переключать нечего.

    Порядок тот же, что у :func:`tabs_of_zone` (снизу вверх): «следующее»
    поднимает окно, лежащее выше текущего, а с верхнего уходит на нижнее. Если
    `current` в списке нет (окно ушло, фокус вне зоны), начинаем с нижнего.
    Одиночное окно и пустой список переключать некуда.
    """
    ids = list(ids)
    if len(ids) < 2:
        return None
    if current in ids:
        return ids[(ids.index(current) + 1) % len(ids)]
    return ids[0]


def strip_record(zone, zone_rect, tabs, active=None, height=STRIP_HEIGHT,
                 min_window_h=MIN_WINDOW_H):
    """Одна запись снимка для полосы — или ``None``, если полосе тут не место.

    ``tabs`` — ``[(окно, заголовок), ...]`` снизу вверх, ``active`` — окно
    видимой (верхней) вкладки. Полоса не рисуется в двух случаях: вкладка всего
    одна (полоса — признак стопки, у одиночного окна её нет) и полоса не влезает
    в зону (:func:`strip_rect`).
    """
    tabs = list(tabs)
    if len(tabs) < 2:
        return None
    rect = strip_rect(zone_rect, height, min_window_h)
    if rect is None:
        return None
    return {
        "zone": zone,
        "rect": [int(round(v)) for v in rect],
        "tabs": [
            {"window": win, "title": title, "active": win == active}
            for win, title in tabs
        ],
    }


def strips_payload(records):
    """Строка снимка для полосы: один JSON в строке, с завершающим ``\\n``.

    Снимок полный, а не дельта: зон на экране мало, зато полосе не надо помнить
    прошлое состояние, а демону — считать разницу. ``ensure_ascii=False`` —
    заголовки окон русские. Записи-``None`` (нечего рисовать) отсеиваются.
    """
    return json.dumps(
        {"strips": [record for record in records if record is not None]},
        ensure_ascii=False,
    ) + "\n"


def tabs_switch_command(zone, window):
    """Запрос демону от полосы: показать окно `window` в зоне `zone`.

    Клик по вкладке идёт в тот же сокет демона, что и команды ``gridsnap-ctl``,
    а не в отдельный канал: полосе для этого довольно ``zone_ipc``.
    """
    return {"cmd": TABS_SWITCH, "zone": zone, "window": int(window)}


def tabs_next_command():
    """Запрос демону от клавиши: поднять следующее окно стопки.

    Зону демон выбирает сам — по активному окну; окно вне зон переключать некуда,
    и это не ошибка.
    """
    return {"cmd": TABS_NEXT}

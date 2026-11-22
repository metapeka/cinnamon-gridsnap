#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_config.py — чистая работа с zones.conf без GTK.

Здесь живёт всё, что редактор зон (``zone-editor.py``) делает с конфигом:
модель зоны, загрузка раскладки через ``zone-tiler.py``, сохранение в
каноническом виде и перенос «чужих» секций (``[rules]``, ``[daemon]``, …)
дословно. Наборы зон на монитор живут в секциях ``[zones]`` (по умолчанию) и
``[zones:<имя выхода>]`` (например ``[zones:HDMI-1]``); модуль знает оба вида
и не теряет соседние наборы при сохранении одного.

Модуль намеренно не импортирует gi/GTK: его можно и нужно тестировать
обычными юнит-тестами в CI, где ``python3-gi`` не установлен.
"""

import configparser
import importlib.util
import json
import os
import re

import zone_actions
import zone_tabs

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")

# Секции, которыми управляет сам редактор: их нельзя переносить как «чужие».
RESERVED_SECTIONS = frozenset({"grid", "zones", "keys", "move"})

# Набор зон монитора: ``[zones:<имя выхода>]``. Имя выхода (eDP-1, HDMI-1) —
# не имя зоны, поэтому в кавычки/дефисы оно не приводится.
MONITOR_SECTION_PREFIX = "zones:"

# Перемещение окна в соседнюю зону: ``[move]``, ключ — направление, значение —
# сочетание Cinnamon. Секция необязательная: нет её — действуют значения по
# умолчанию. Ключи не из направлений — ошибка конфига: молча проигнорированная
# опечатка оставила бы человека без клавиши, а он бы этого не заметил.
MOVE_SECTION = "move"
MOVE_DIRECTIONS = ("left", "right", "up", "down")
# ``Win``+стрелки — сочетания, которые Cinnamon (muffin) занимает своим
# ``push-tile-*``; поэтому их и берём по умолчанию, но человек может поменять.
DEFAULT_MOVE_KEYS = {
    "left": "<Super>Left",
    "right": "<Super>Right",
    "up": "<Super>Up",
    "down": "<Super>Down",
}

# Режим «только эта зона»: ``[focus]``, ``key`` — сочетание, ``zone`` — зона, куда
# кладётся окно (необязательна). Секция необязательная: нет её — режим не
# настроен. Ключи не из ``key``/``zone`` — ошибка конфига, как и пустая ``zone``:
# молча проигнорированная опечатка оставила бы человека без клавиши.
#
# В :data:`RESERVED_SECTIONS` секция не входит намеренно: редактор её не правит,
# а переносит дословно как чужую — так она и переживает сохранение раскладки.
FOCUS_SECTION = "focus"

# Вкладки в зоне: ``[tabs]``, ``key`` — сочетание «следующее окно зоны», ``height``
# — высота полосы вкладок в пикселях (необязательна, умолчание —
# :data:`zone_tabs.STRIP_HEIGHT`). Секция необязательная. Как и ``[focus]``, в
# :data:`RESERVED_SECTIONS` она не входит: редактор её не правит, а переносит
# дословно как чужую. Неизвестный ключ — ошибка конфига (опечатка оставила бы
# человека без клавиши), мусор в ``height`` — тоже: полоса нулевой или
# отрицательной высоты не рисуется, и вкладки молча стали бы невидимыми.
TABS_SECTION = "tabs"
TABS_FIELDS = ("key", "height")

# Действия: одна секция — один экземпляр действия, ``[action:<имя>]`` (как
# ``[zones:<выход>]`` — один набор на экран). В :data:`RESERVED_SECTIONS` секция
# не входит: редактор её не правит, а переносит дословно, как
# ``[focus]``/``[tabs]``. Имя действия — из ``[a-z0-9_-]``: оно уже приведено
# ``ConfigParser`` к нижнему регистру и попадает в командную строку клавиши
# (``zone-tiler.py action <имя>``), поэтому пробелы и кавычки в нём недопустимы.
ACTION_SECTION_PREFIX = "action:"
ACTION_FIELDS = ("run", "zone", "key", "command", "axis")
ACTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Рабочие столы: ``[workspaces]``, ключ — номер стола как в интерфейсе Cinnamon
# (с 1), значение — имя профиля из ``templates/<имя>.conf``.
WORKSPACES_SECTION = "workspaces"

# Экраны: ``[monitors]``, ключ — имя выхода (``HDMI-1``, ``eDP-1``), значение — имя
# профиля. Секция машинная: в профиль не входит, ``profile use`` её не трогает
# (переносится дословно, как ``[grid]``). Порядок строк значим: профиль первого
# экрана решает спор при слиянии (``profile use-monitors``).
MONITORS_SECTION = "monitors"

# Подпись «стол без профиля» в диалоге «Профили столов». С настоящим именем
# профиля не совпадёт: имена санируются, пробел в них превращается в дефис.
WORKSPACE_SHARED = "общий набор"

# Подпись «экран без привязки» в диалоге «Профили экранов» — та же строка, что
# у столов: пустое значение убирает привязку.
MONITOR_SHARED = WORKSPACE_SHARED

# Как в диалоге помечен выход из `[monitors]`, которого сейчас нет на машине.
MONITOR_OFFLINE_NOTE = "нет на экране"

# Сколько столов показать в диалоге, если ``_NET_NUMBER_OF_DESKTOPS`` не прочитать.
WORKSPACE_DESKTOPS_DEFAULT = 4

# Порядок «чужих» секций при сохранении: [rules] и [ignore] до [keys], [daemon] после.
# ``[move]`` пишется сама: она «своя» (см. :data:`RESERVED_SECTIONS`), но
# редактор её не правит и обязан сохранить — тело берётся из прежнего файла
# (или из ``extra``, если его передали явно).
BEFORE_KEYS = ("rules", "ignore")
AFTER_KEYS = ("daemon",)


def load_zonetiler(path=ZONEBIN):
    """Загрузить ``zone-tiler.py`` как модуль (в нём парсер конфига)."""
    spec = importlib.util.spec_from_file_location("zone_tiler", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"zone_config: не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_zonetiler()


# ------------------------------------------------------------------ данные


class Zone:
    def __init__(self, name, x, y, w, h, binding=""):
        self.name = name
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.binding = binding

    def copy(self):
        return Zone(self.name, self.x, self.y, self.w, self.h, self.binding)

    def as_tuple(self):
        return (self.x, self.y, self.w, self.h)

    def fmt(self):
        return ", ".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in self.as_tuple())


class _SpecParser:
    """Разбор спецификаций зон правилами ``zone-tiler.Grid`` без X и GTK.

    У ``Grid`` берутся только чистые методы: ``_parse_zone`` (формы ``x,y,w,h``
    и ``c0:c1, r0:r1`` с проверками), ``zone_spec`` и ``fractions``, — поэтому
    правила разбора и сообщения об ошибках не расходятся с раскладчиком. Своё
    состояние (``cols``/``rows`` и разобранные ``zones``) держит сам, конфига не
    читает.
    """

    _parse_zone = zt.Grid._parse_zone
    fractions = zt.Grid.fractions

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.zones = {}

    def add(self, name, spec):
        """Разобрать зону пары ``имя = спецификация``; возвращает имя как в файле."""
        key = name.strip()
        self.zones[key] = self._parse_zone(key, spec)
        return key

    def fractions_of(self, name):
        """``(x, y, w, h)`` в долях рабочей области для разобранной зоны."""
        return self.fractions(name)

    def zone_spec(self, zone, monitor_name=None):
        """Описание зоны из уже собранного набора.

        У ``Grid`` этот метод ищет сначала в наборе монитора, потом в общем
        ``[zones]``. Здесь набор собран из нужной секции заранее, так что
        искать больше негде; ``monitor_name`` принимается ради совместимости
        сигнатуры (``Grid.fractions`` вызывает ``self.zone_spec``).
        """
        spec = self.zones.get(zone)
        if spec is None:
            zt.fail(f"zone-tiler: зоны '{zone}' нет в наборе", zt.EXIT_CONFIG)
        return spec


def load_workspace_map(path):
    """``{номер_стола: имя_профиля}`` из секции ``[workspaces]``; нет секции — ``{}``.

    Ключи — **с 1**, как номера столов в интерфейсе Cinnamon («Рабочий стол 1»),
    а в EWMH ``_NET_CURRENT_DESKTOP`` они с нуля; переводит коды в имена
    ``zone-tiler.grid_for_workspace()``. Значение — имя профиля
    (``templates/<имя>.conf``), существование файла проверяется в момент
    использования: профиль может появиться позже, и это не повод ломать чтение
    всего конфига.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(WORKSPACES_SECTION):
        return {}
    mapping = {}
    for key, value in cp.items(WORKSPACES_SECTION):
        number = str(key).strip()
        if not number.isdigit() or int(number) < 1:
            zt.fail(
                f"zone_config: в [{WORKSPACES_SECTION}] номер стола должен быть "
                f"целым с 1, а не '{number}'",
                zt.EXIT_CONFIG,
            )
        profile = sanitize_profile_name(value)
        if not profile:
            zt.fail(
                f"zone_config: в [{WORKSPACES_SECTION}] у стола {number} пустое "
                "имя профиля",
                zt.EXIT_CONFIG,
            )
        mapping[int(number)] = profile
    return mapping


def load_move_keys(path):
    """``{направление: сочетание}`` из секции ``[move]``; секции нет — умолчания.

    Ключи — только направления :data:`MOVE_DIRECTIONS` (``left``/``right``/
    ``up``/``down``), значения — сочетания в синтаксисе Cinnamon
    (``<Super>Left``). Нет файла или секции — :data:`DEFAULT_MOVE_KEYS`
    целиком, так что файл без ``[move]`` работает как раньше.

    Неизвестный ключ или пустое сочетание — ошибка конфига (код 3): это
    опечатка, из-за которой человек остался бы без клавиши и не понял почему.
    Недостающие в секции направления добираются умолчаниями — писать все
    четыре ради одной замены не нужно.
    """
    keys = dict(DEFAULT_MOVE_KEYS)
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(MOVE_SECTION):
        return keys
    for name, value in cp.items(MOVE_SECTION):
        # ConfigParser приводит ключи к нижнему регистру, так что ``Left`` в
        # файле тоже сойдёт за направление.
        direction = str(name).strip().lower()
        if direction not in MOVE_DIRECTIONS:
            zt.fail(
                f"zone_config: в [{MOVE_SECTION}] неизвестное направление "
                f"'{direction}': можно {', '.join(MOVE_DIRECTIONS)}",
                zt.EXIT_CONFIG,
            )
        combo = str(value).strip()
        if not combo:
            zt.fail(
                f"zone_config: в [{MOVE_SECTION}] у направления '{direction}' "
                "пустое сочетание",
                zt.EXIT_CONFIG,
            )
        keys[direction] = combo
    return keys


def load_focus_keys(path):
    """Секция ``[focus]``: ``{"key": сочетание, "zone": имя зоны | None}``.

    ``None`` — режим не настроен: нет файла, нет секции или пустое ``key``.
    ``zone`` необязательна: без неё сочетание только сворачивает остальные окна
    («только это окно»), с ней — ещё и кладёт окно в зону. Неизвестный ключ в
    секции и пустое значение ``zone`` — ошибка конфига (код 3): это опечатка, из-за
    которой человек остался бы без клавиши и не понял почему.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(FOCUS_SECTION):
        return None
    key, zone = None, None
    for name, value in cp.items(FOCUS_SECTION):
        # ConfigParser приводит ключи к нижнему регистру, так что ``Key`` в
        # файле тоже сойдёт.
        field = str(name).strip().lower()
        if field not in ("key", "zone"):
            zt.fail(
                f"zone_config: в [{FOCUS_SECTION}] неизвестный ключ '{field}': "
                "можно key, zone",
                zt.EXIT_CONFIG,
            )
        text = str(value).strip()
        if field == "key":
            key = text or None
            continue
        if not text:
            zt.fail(
                f"zone_config: в [{FOCUS_SECTION}] пустое значение zone",
                zt.EXIT_CONFIG,
            )
        zone = text
    if not key:
        # Секция без сочетания — это не режим: без клавиши он недоступен.
        return None
    return {"key": key, "zone": zone}


def load_tabs_config(path):
    """Секция ``[tabs]``: ``{"key": сочетание | None, "height": высота полосы}``.

    ``height`` — высота полосы вкладок в пикселях, целое больше нуля; нет
    секции или нет ключа — :data:`zone_tabs.STRIP_HEIGHT` (умолчание полосы, а
    не второе число в конфиге). ``key`` — сочетание Cinnamon для «следующего
    окна зоны»:
    ``None`` — клавиши нет, остальные поля при этом читаются. Мусор в
    ``height`` (не число, ноль, отрицательное) и неизвестный ключ в секции —
    ошибка конфига (код 3): полосы нулевой высоты не видно, и человек не понял
    бы, почему вкладки перестали показываться.
    """
    config = {"key": None, "height": int(zone_tabs.STRIP_HEIGHT)}
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(TABS_SECTION):
        return config
    for name, value in cp.items(TABS_SECTION):
        # ConfigParser приводит ключи к нижнему регистру, так что ``Key`` в
        # файле тоже сойдёт.
        field = str(name).strip().lower()
        if field not in TABS_FIELDS:
            zt.fail(
                f"zone_config: в [{TABS_SECTION}] неизвестный ключ '{field}': "
                f"можно {', '.join(TABS_FIELDS)}",
                zt.EXIT_CONFIG,
            )
        text = str(value).strip()
        if field == "key":
            config["key"] = text or None
            continue
        config["height"] = _tabs_height(text)
    return config


def _tabs_height(text):
    """Высота полосы вкладок из ``[tabs] height``: целое больше нуля, иначе код 3."""
    try:
        height = int(str(text).strip(), 10)
    except (TypeError, ValueError):
        height = 0
    if height <= 0:
        zt.fail(
            f"zone_config: в [{TABS_SECTION}] height — целое число пикселей больше "
            f"нуля, а не '{text}'",
            zt.EXIT_CONFIG,
        )
    return height


def load_actions(path):
    """Действия из секций ``[action:<имя>]``: ``{имя: запись}``.

    Запись — словарь с полями :data:`ACTION_FIELDS`:

    * ``run`` — обязательный: ``builtin:<имя>`` из ``zone_actions.BUILTINS`` либо
      внешняя команда (её разбирает ``zone_actions.expand_command``);
    * ``zone`` — необязательное имя зоны-предмета; без неё предметом становится
      активное окно и зона, где оно стоит;
    * ``key`` — необязательное сочетание Cinnamon для ``setup-hotkeys.py``;
    * ``command`` — только у ``builtin:terminal``: чем запускать терминал
      (умолчание — ``zone_actions.DEFAULT_TERMINAL``);
    * ``axis`` — только у ``builtin:mirror``: ось из ``zone_actions.MIRROR_AXES``
      (умолчание — ``zone_actions.DEFAULT_MIRROR_AXIS``).

    Отсутствующие необязательные поля — ``None``; нет файла или нет ни одной
    секции — пустой словарь (действий нет, и это не ошибка).

    Ошибка конфига (код 3): пустой ``run``/``zone``/``key``/``command``,
    неизвестное имя встроенного действия, неизвестное поле секции, ``command``
    или ``axis`` не у своего действия, ось не из :data:`zone_actions.MIRROR_AXES`,
    имя секции не из ``[a-z0-9_-]``. Всё это — опечатки, из-за которых человек
    остался бы без действия или без клавиши и не понял почему.

    Имя секции и имя действия сравниваются без учёта регистра (как ключи
    ``ConfigParser``): ``[action:Main]`` и ``[action:main]`` — не два действия с
    одним именем, а опечатка, поэтому это тоже код 3. Порядок результата — как
    секции в файле.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None:
        return {}
    actions, sections = {}, {}
    for section in cp.sections():
        name = _action_section_name(section)
        if name is None:
            continue
        if name in sections:
            zt.fail(
                f"zone_config: действие '{name}' задано дважды: "
                f"[{sections[name]}] и [{section}] — имена действий сравниваются "
                "без учёта регистра",
                zt.EXIT_CONFIG,
            )
        actions[name] = _action_entry(section, cp.items(section))
        sections[name] = section
    return actions


def _action_section_name(section):
    """Имя действия из имени секции ``[action:<имя>]``; не наша секция — ``None``.

    Имя приводится к нижнему регистру: оно попадает в командную строку клавиши
    (``zone-tiler.py action <имя>``), поэтому пробелы, точки и кавычки в нём
    недопустимы (:data:`ACTION_NAME_RE`), а пустое ``[action:]`` — опечатка, а
    не действие без имени.
    """
    name = str(section)
    if name[: len(ACTION_SECTION_PREFIX)].lower() != ACTION_SECTION_PREFIX:
        return None
    short = name[len(ACTION_SECTION_PREFIX):].strip().lower()
    if not short:
        zt.fail(
            f"zone_config: пустое имя действия в секции [{name}]: пишите "
            "[action:<имя>], например [action:term-main]",
            zt.EXIT_CONFIG,
        )
    if not ACTION_NAME_RE.match(short):
        zt.fail(
            f"zone_config: имя действия '{short}' из секции [{name}]: можно "
            "строчные латинские буквы, цифры, дефис и подчёркивание — имя "
            "попадает в командную строку клавиши",
            zt.EXIT_CONFIG,
        )
    return short


def _action_entry(section, items):
    """Запись действия из пар секции; всё не по контракту — код 3.

    Поля читаются все сразу, а проверяются после: ``command``/``axis`` зависят от
    того, какое встроенное действие стоит в ``run``, а порядок строк в секции
    произвольный. ``run`` хранится как в файле (внешняя команда
    регистрозависима), ``axis`` — приведённой к нижнему регистру, как её
    разбирает ``zone_actions.mirror_fractions``.
    """
    where = f"zone_config: в [{section}]"
    raw = {}
    for field, value in items:
        # ConfigParser приводит ключи к нижнему регистру, так что ``Run`` в файле
        # тоже сойдёт за поле.
        field = str(field).strip().lower()
        if field not in ACTION_FIELDS:
            zt.fail(
                f"{where} неизвестное поле '{field}': можно "
                + ", ".join(ACTION_FIELDS),
                zt.EXIT_CONFIG,
            )
        raw[field] = str(value).strip()

    run = raw.get("run", "")
    if not run:
        zt.fail(
            f"{where} пустое обязательное поле run: нужно builtin:<имя> или "
            "внешняя команда",
            zt.EXIT_CONFIG,
        )
    try:
        builtin = zone_actions.builtin_name(run)
    except zone_actions.ActionError as exc:
        # Имя встроенного действия проверяет ядро: список известных и сообщение
        # об опечатке должны быть одни и те же в конфиге и в раскладчике.
        zt.fail(f"{where} {exc}", zt.EXIT_CONFIG)

    entry = {"run": run, "zone": None, "key": None, "command": None, "axis": None}
    for field in ("zone", "key"):
        if field not in raw:
            continue
        if not raw[field]:
            zt.fail(f"{where} пустое значение {field}", zt.EXIT_CONFIG)
        entry[field] = raw[field]

    if "command" in raw:
        if builtin != zone_actions.TERMINAL:
            zt.fail(
                f"{where} поле command бывает только у builtin:"
                f"{zone_actions.TERMINAL}, а run = '{run}'",
                zt.EXIT_CONFIG,
            )
        if not raw["command"]:
            zt.fail(
                f"{where} пустое значение command: чем запускать терминал",
                zt.EXIT_CONFIG,
            )
        entry["command"] = raw["command"]

    if "axis" in raw:
        if builtin != zone_actions.MIRROR:
            zt.fail(
                f"{where} поле axis бывает только у builtin:"
                f"{zone_actions.MIRROR}, а run = '{run}'",
                zt.EXIT_CONFIG,
            )
        axis = raw["axis"].lower()
        if axis not in zone_actions.MIRROR_AXES:
            zt.fail(
                f"{where} неизвестная ось зеркала axis = '{raw['axis']}': можно "
                + ", ".join(zone_actions.MIRROR_AXES),
                zt.EXIT_CONFIG,
            )
        entry["axis"] = axis
    return entry


def _workspace_number(value):
    """Номер стола из ключа карты: целое с 1, иначе ``ValueError``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"номер стола должен быть целым: {value!r}") from None
    if number < 1:
        raise ValueError(f"номер стола должен быть с 1, а не {number}")
    return number


def _workspace_body(mapping):
    """Тело секции ``[workspaces]``: ``номер = имя`` по возрастанию номера.

    Имя профиля санируется тем же правилом, что при чтении
    (:func:`sanitize_profile_name`), поэтому запись и чтение понимают друг
    друга. Номер не целый или меньше 1, а пустое после санирования имя —
    ``ValueError``: писать заведомо нечитаемый конфиг незачем.
    """
    items = {}
    for number, name in (mapping or {}).items():
        items[_workspace_number(number)] = name
    lines = []
    for number in sorted(items):
        profile = sanitize_profile_name(items[number])
        if not profile:
            raise ValueError(
                f"пустое имя профиля у стола {number}: {items[number]!r}"
            )
        lines.append(f"{number} = {profile}")
    return lines


def _section_line_spans(lines, section):
    """``(начало, конец)`` всех секций ``[section]``: заголовок и тело."""
    spans, start = [], None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if start is not None:
            spans.append((start, index))
            start = None
        if match.group(1).strip().lower() == section:
            start = index
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def _workspace_spans(lines):
    """``(начало, конец)`` всех секций ``[workspaces]``."""
    return _section_line_spans(lines, WORKSPACES_SECTION)


def _replace_section(lines, section, block):
    """Заменить все секции ``[section]`` одним блоком ``block`` (заголовок + тело).

    Блок встаёт на место первой секции, дубли (если их наплодили руками) уходят:
    ``ConfigParser`` на повторе секции падает. Пустой ``block`` убирает секцию
    совсем; нет её вообще — блок дописывается в конец. Пустые строки подчищаются.
    """
    spans = _section_line_spans(lines, section)
    if spans:
        insert_at = spans[0][0]
        drop = set()
        for start, end in spans:
            drop.update(range(start, end))
        result = []
        for index, line in enumerate(lines):
            if index == insert_at:
                result.extend(block)
            if index not in drop:
                result.append(line)
    else:
        result = list(lines)
        if block:
            while result and not result[-1].strip():
                result.pop()
            if result:
                result.append("")
            result.extend(block)
    return _tidy_blank(result)


def _tidy_blank(lines):
    """Пустые строки: без ведущих, без хвостовых и без пар подряд."""
    out = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def save_workspace_map(path, mapping):
    """Записать секцию ``[workspaces]``: номер стола (с 1) → имя профиля.

    Правится только эта секция, и текстом: ``[grid]``, наборы зон ``[zones]``/
    ``[zones:<выход>]``, ``[keys]``, ``[rules]``, ``[daemon]`` и прочие строки
    файла остаются дословно, как после ручной правки. Пустой ``mapping``
    убирает секцию совсем, номера пишутся по возрастанию. Имена профилей
    санируются, поэтому ``{"2": "my cinema"}`` станет ``2 = my-cinema``.
    Файла нет — он будет создан с одной этой секцией. Кривой номер (не целый
    или меньше 1) и пустое имя профиля — ``ValueError``. Возвращает путь.
    """
    body = _workspace_body(mapping)
    block = [f"[{WORKSPACES_SECTION}]"] + body if body else []
    lines = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    lines = _replace_section(lines, WORKSPACES_SECTION, block)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n" if lines else "")
    return path


def _monitor_body(mapping):
    """Тело секции ``[monitors]``: ``выход = имя профиля`` в заданном порядке.

    Порядок строк значим: профиль **первого** экрана решает спор при слиянии
    (``profile use-monitors``), поэтому сортировки нет — как передали, так и
    запишем. Имя выхода пишется как есть (обычно так его печатает
    ``xrandr --listmonitors``), но сравнивать его надо без учёта регистра: в файле
    может оказаться ``hdmi-1``, а ``ConfigParser`` вообще опускает регистр ключей.
    Пустое имя выхода или пустое (после санирования) имя профиля — ``ValueError``:
    писать заведомо нечитаемый конфиг незачем.
    """
    lines = []
    for output, name in (mapping or {}).items():
        output = str(output).strip()
        if not output:
            raise ValueError(f"пустое имя выхода: {output!r}")
        profile = sanitize_profile_name(name)
        if not profile:
            raise ValueError(f"пустое имя профиля у выхода {output}: {name!r}")
        lines.append(f"{output} = {profile}")
    return lines


def load_monitor_map(path):
    """``{имя выхода: имя профиля}`` из секции ``[monitors]``; нет секции — ``{}``.

    Ключ — имя выхода как его печатает ``xrandr --listmonitors`` (``HDMI-1``,
    ``eDP-1``), значение — имя профиля (``templates/<имя>.conf``). Сравнивать имена
    выходов надо **без учёта регистра**: ``ConfigParser`` опускает регистр ключей,
    поэтому ``HDMI-1`` вернётся как ``hdmi-1`` — ровно так же терпимо сравнивает
    наборы зон ``Grid.zones_for`` (см. :func:`resolve_profile`).

    Порядок строк значим (профиль первого экрана решает спор при слиянии), поэтому
    словарь в порядке файла, а не отсортированный. Существование файла профиля здесь
    не проверяется: профиль могли переименовать, и это повод откатиться на следующий
    уровень с предупреждением, а не ломать чтение конфига (как у ``[workspaces]``).
    Пустое имя профиля — ошибка конфига (код 3): строка, которая ничего не значит.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(MONITORS_SECTION):
        return {}
    mapping = {}
    for key, value in cp.items(MONITORS_SECTION):
        output = str(key).strip()
        if not output:
            zt.fail(
                f"zone_config: в [{MONITORS_SECTION}] пустое имя выхода",
                zt.EXIT_CONFIG,
            )
        profile = sanitize_profile_name(value)
        if not profile:
            zt.fail(
                f"zone_config: в [{MONITORS_SECTION}] у выхода {output} пустое имя "
                "профиля",
                zt.EXIT_CONFIG,
            )
        mapping[output] = profile
    return mapping


def save_monitor_map(path, mapping):
    """Записать секцию ``[monitors]``: имя выхода → имя профиля.

    Правится только эта секция и текстом: ``[grid]``, наборы зон ``[zones]``/
    ``[zones:<выход>]``, ``[workspaces]``, ``[keys]``, ``[rules]``, ``[daemon]`` и
    прочие строки файла остаются дословно, как после ручной правки. Пустой
    ``mapping`` убирает секцию совсем; порядок строк сохраняется как передали —
    профиль первого экрана решает спор при слиянии. Имена профилей санируются,
    поэтому ``{"HDMI-2": "my cinema"}`` станет ``HDMI-2 = my-cinema``.
    Файла нет — он будет создан с одной этой секцией. Пустое имя выхода и пустое
    имя профиля — ``ValueError``. Возвращает путь.
    """
    body = _monitor_body(mapping)
    block = [f"[{MONITORS_SECTION}]"] + body if body else []
    lines = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    lines = _replace_section(lines, MONITORS_SECTION, block)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n" if lines else "")
    return path


def resolve_profile(monitor_map, workspace_map=None, output=None, workspace_code=None):
    """Имя профиля для окна: привязка экрана важнее привязки стола.

    Порядок разбора (решение владельца, 22.11.2026):

    1. выход окна есть в ``[monitors]`` — профиль экрана;
    2. иначе номер стола окна есть в ``[workspaces]`` — профиль стола;
    3. иначе ``None`` — значит активный ``zones.conf``.

    ``workspace_code`` — номер стола из EWMH (``_NET_CURRENT_DESKTOP``, с нуля), а
    в ``[workspaces]`` номера записаны с 1, как в интерфейсе Cinnamon: перевод
    делает эта функция, чтобы его не повторял каждый вызывающий.

    Имя выхода сравнивается без учёта регистра — как наборы ``[zones:<выход>]``; в
    словаре из :func:`load_monitor_map` регистр вообще опущен парсером.

    Существование файла профиля не проверяется: профиль могли переименовать, и
    тогда нужен откат на следующий уровень с предупреждением (это делает
    вызывающий — :func:`read_profile_sets` бросит ``FileNotFoundError``).
    """
    if output:
        name = str(output).strip()
        for key, profile in (monitor_map or {}).items():
            if str(key).strip().casefold() == name.casefold():
                return profile
    if workspace_code is not None:
        try:
            desk = int(workspace_code) + 1
        except (TypeError, ValueError):
            desk = None
        if desk is not None:
            # `load_workspace_map` отдаёт ключи-числа, но словарь могли собрать и
            # руками (диалог редактора, тесты) — смотрим оба вида.
            for key in (desk, str(desk)):
                profile = (workspace_map or {}).get(key)
                if profile:
                    return profile
    return None


# Строки диалога «Профили экранов» (редактор) — рядом с профилями столов:
# чистая функция без GTK и X, поэтому её проверяют обычные тесты.
def _monitor_row(output, profile, names, on_screen=True):
    """Одна строка диалога «Профили экранов»: варианты, предупреждение и признак."""
    options = [MONITOR_SHARED] + names
    notes = []
    if profile is not None and profile not in names:
        options.append(profile)
        notes.append(f"профиля «{profile}» нет на диске: привязка пока не действует")
    if not on_screen:
        notes.append(f"выхода {output} сейчас {MONITOR_OFFLINE_NOTE}: привязка не "
                     "действует, пока экран не подключён")
    return (output, profile, options, "; ".join(notes), on_screen)


def monitor_dialog_rows(mapping, profiles=None, outputs=None):
    """Строки диалога «Профили экранов» — чистая функция без GTK и без X.

    ``mapping`` — карта из :func:`zone_config.load_monitor_map`, ``profiles`` —
    имена профилей на диске (:func:`zone_config.list_profiles`), ``outputs`` — имена
    выходов в порядке ``xrandr --listmonitors``. Порядок значим: профиль первого
    экрана решает спор при слиянии (``profile use-monitors``), и сохранение пишет
    секцию ровно в порядке строк этого списка.

    Строка — ``(выход, профиль, варианты, предупреждение, на_экране)``:

    * ``выход`` — имя как его печатает xrandr; для выхода из ``[monitors]``, которого
      сейчас нет на машине (отключён, переименован), — как записано в файле: строку
      всё равно показываем, иначе её нельзя ни поправить, ни убрать;
    * ``профиль`` — имя профиля экрана, ``None`` — общий набор (привязки нет);
    * ``варианты`` — выпадающий список: :data:`MONITOR_SHARED`, затем имена профилей
      с диска, и обязательно имя из сопоставления — профиль мог исчезнуть, но запись
      показываем и не выбрасываем;
    * ``предупреждение`` — пусто или текст про профиль, которого нет на диске, и про
      выход, которого сейчас нет на экране;
    * ``на_экране`` — ``False`` у строки выхода, которого сейчас нет.

    Имена выходов сравниваются **без учёта регистра**: ``ConfigParser`` опускает
    регистр ключей, поэтому из файла приходит ``hdmi-1``, а xrandr говорит
    ``HDMI-1``. Строки идут в порядке xrandr, а записи про отсутствующие выходы —
    следом, в порядке файла.
    """
    names = sorted({sanitize_profile_name(n) for n in (profiles or [])} - {""})
    file_order = []
    by_key = {}
    for output, name in (mapping or {}).items():
        output = str(output).strip()
        if not output:
            continue
        key = output.casefold()
        if key in by_key:
            continue
        # Имя профиля из файла уже санировано, но словарь могли собрать и руками.
        file_order.append(output)
        by_key[key] = (output, sanitize_profile_name(name) or None)
    rows = []
    seen = set()
    for output in outputs or ():
        name = str(output or "").strip()
        if not name:
            continue
        key = name.casefold()
        seen.add(key)
        profile = by_key[key][1] if key in by_key else None
        rows.append(_monitor_row(name, profile, names, on_screen=True))
    for output in file_order:
        key = output.casefold()
        if key in seen:
            continue
        rows.append(_monitor_row(output, by_key[key][1], names, on_screen=False))
    return rows


def workspace_dialog_rows(mapping, profiles=None, desktops=None):
    """Строки диалога «Профили столов»: по одной на стол.

    ``mapping`` — карта из :func:`load_workspace_map`, ``profiles`` — имена
    профилей на диске (:func:`list_profiles`), ``desktops`` — число столов из
    ``_NET_NUMBER_OF_DESKTOPS`` (``None``/0 — не прочитали, берём
    :data:`WORKSPACE_DESKTOPS_DEFAULT`).

    Строка — ``(номер, профиль, варианты, предупреждение)``:

    * ``номер`` — стол с 1, как в интерфейсе Cinnamon;
    * ``профиль`` — имя профиля стола, ``None`` — общий набор зон;
    * ``варианты`` — выпадающий список: :data:`WORKSPACE_SHARED`, затем имена
      профилей с диска, и обязательно имя из сопоставления — профиль мог
      исчезнуть, но запись показываем и не выбрасываем;
    * ``предупреждение`` — пусто или текст про профиль, которого нет на диске:
      до появления файла такой стол раскладывается общим набором.

    Столы идут по порядку; сопоставления за пределами ``desktops`` тоже
    попадают в список — иначе сохранение потеряло бы их.
    """
    try:
        total = int(desktops)
    except (TypeError, ValueError):
        total = WORKSPACE_DESKTOPS_DEFAULT
    if total < 1:
        total = WORKSPACE_DESKTOPS_DEFAULT
    names = sorted({sanitize_profile_name(n) for n in (profiles or [])} - {""})
    mapped = {}
    for number, name in (mapping or {}).items():
        try:
            key = int(number)
        except (TypeError, ValueError):
            continue
        if key >= 1:
            mapped[key] = name
    rows = []
    for number in sorted(set(range(1, total + 1)) | set(mapped)):
        profile = sanitize_profile_name(mapped.get(number)) or None
        options = [WORKSPACE_SHARED] + names
        missing = profile is not None and profile not in names
        if missing:
            options.append(profile)
        warning = ""
        if missing:
            warning = (f"профиля «{profile}» нет на диске: стол {number} пока "
                       "раскладывается общим набором")
        rows.append((number, profile, options, warning))
    return rows


def _clean_monitor(monitor):
    """Имя выхода из аргумента; ``None``/пусто — набор по умолчанию."""
    name = str(monitor or "").strip()
    return name or None


def _zone_section_name(monitor):
    """Имя секции набора: ``zones`` или ``zones:<монитор>``."""
    name = _clean_monitor(monitor)
    return "zones" if name is None else MONITOR_SECTION_PREFIX + name


def _monitor_of(section):
    """Монитор из имени секции ``zones:<выход>``; ``None`` — секция не набор монитора.

    Пустое имя (``[zones:]``) — ошибка конфига: сообщение подсказывает форму
    записи. Регистр имени выхода сохраняется: это имя выхода X11, а не зоны.
    """
    name = str(section)
    if name[: len(MONITOR_SECTION_PREFIX)].lower() != MONITOR_SECTION_PREFIX:
        return None
    monitor = name[len(MONITOR_SECTION_PREFIX):].strip()
    if not monitor:
        zt.fail(
            f"zone_config: пустое имя монитора в секции [{name}]: "
            "пишите [zones:<имя выхода>], например [zones:HDMI-1]",
            zt.EXIT_CONFIG,
        )
    return monitor


def _read_conf(path, missing_ok=False):
    """``ConfigParser`` того же вида, что у ``Grid``; нет файла — ``None`` или SystemExit."""
    cp = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
        delimiters=("=",),
    )
    if not cp.read(path, encoding="utf-8"):
        if missing_ok:
            return None
        zt.fail(f"zone_config: не найден конфиг {path}", zt.EXIT_CONFIG)
    return cp


def _zone_bindings(cp):
    """Привязки зон из ``[keys]`` (пустой словарь, если секции нет)."""
    if not cp.has_section("keys"):
        return {}
    return {k.strip(): v.strip() for k, v in cp.items("keys")}


def _with_bindings(zones, keys):
    """Проставить зонам привязки из ``[keys]`` по имени зоны."""
    for zone in zones:
        zone.binding = keys.get(zone.name, "")
    return zones


def _zone_set(cp, section):
    """Зоны секции ``section`` в порядке файла; ``None`` — секции нет."""
    if not cp.has_section(section):
        return None
    parser = _SpecParser(
        zt.Grid._numlist(cp.get("grid", "cols", fallback="1.0")),
        zt.Grid._numlist(cp.get("grid", "rows", fallback="1.0")),
    )
    for name, spec in cp.items(section):
        parser.add(name, spec)
    if not parser.zones:
        zt.fail(f"zone_config: в секции [{section}] нет ни одной зоны", zt.EXIT_CONFIG)
    return [Zone(name, *parser.fractions_of(name)) for name in parser.zones]


def _zone_sets(path):
    """Все наборы файла: ``(набор по умолчанию или None, {монитор: [Zone]})``.

    Порядок мониторов — как секции в файле; привязки ``[keys]`` проставлены
    всем наборам. Нет файла — ``(None, {})``.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None:
        return None, {}
    keys = _zone_bindings(cp)
    default = _zone_set(cp, "zones")
    if default is not None:
        _with_bindings(default, keys)
    monitors = {}
    for section in cp.sections():
        monitor = _monitor_of(section)
        if monitor is not None:
            monitors[monitor] = _with_bindings(_zone_set(cp, section), keys)
    return default, monitors


def load_zone_sets(path):
    """Все наборы зон файла: ``{монитор: [Zone]}``, ключ ``None`` — по умолчанию.

    Ключ ``None`` есть только если в файле есть секция ``[zones]``; порядок —
    ``[zones]``, затем секции мониторов в порядке файла. Нет файла — ``{}``.
    """
    default, monitors = _zone_sets(path)
    sets = {}
    if default is not None:
        sets[None] = default
    sets.update(monitors)
    return sets


def list_zone_sets(path):
    """Какие наборы зон есть в файле: ``{монитор: [имена зон]}``.

    Ключ ``None`` — набор по умолчанию из ``[zones]`` (есть только если секция
    в файле есть), остальные ключи — имена выходов из ``[zones:<выход>]``
    в порядке файла. Нет файла — ``{}``.
    """
    return {monitor: [z.name for z in zones] for monitor, zones in load_zone_sets(path).items()}


def _default_zones(path):
    """Зоны ``[zones]`` с привязками ``[keys]`` — прежний путь через ``Grid``."""
    grid = zt.Grid(path)
    keys = _zone_bindings(grid.conf)
    zones = []
    for name in grid.zones:
        x, y, w, h = grid.fractions(name)
        zones.append(Zone(name, x, y, w, h, keys.get(name, "")))
    return zones


def load_zones(path, monitor=None):
    """Зоны набора с привязками из ``[keys]``. Райзит SystemExit на битом конфиге.

    Без ``monitor`` (или с пустым) — набор по умолчанию из ``[zones]``, как
    раньше. С ``monitor="HDMI-1"`` — набор из ``[zones:HDMI-1]``; если такой
    секции нет, откат на набор по умолчанию (неизвестный монитор не ошибка).
    """
    name = _clean_monitor(monitor)
    if name is not None:
        cp = _read_conf(path)
        zones = _zone_set(cp, _zone_section_name(name))
        if zones is not None:
            return _with_bindings(zones, _zone_bindings(cp))
    return _default_zones(path)


# ------------------------------------------------- перенос «чужих» секций


_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:[#;].*)?$")


def _trim_blank(lines):
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def load_extra_sections(path, sections=None):
    """Секции конфига, которые редактор не редактирует, но обязан сохранить.

    Возвращает ``{имя_секции: [строки_тела, ...]}`` только для секций, реально
    присутствующих в файле, — порядок и содержимое строк сохраняются дословно
    (включая регистр ключей и комментарии). ``sections`` ограничивает набор
    имён (в нижнем регистре); по умолчанию берутся все, кроме
    :data:`RESERVED_SECTIONS`. Несуществующий файл даёт пустой словарь.
    """
    wanted = None if sections is None else {s.lower() for s in sections}
    result = {}
    if not path or not os.path.exists(path):
        return result
    current = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            match = _SECTION_RE.match(line)
            if match:
                current = match.group(1).strip().lower()
                # Наборы мониторов (`[zones:*]`) ведёт сам редактор: как «чужие»
                # их не переносим, иначе имя выхода потеряет регистр и задвоится.
                is_zone_set = _monitor_of(current) is not None
                if (
                    not is_zone_set
                    and current not in RESERVED_SECTIONS
                    and (wanted is None or current in wanted)
                ):
                    result.setdefault(current, [])
                else:
                    current = None
                continue
            if current is not None and current in result:
                result[current].append(line)
    for body in result.values():
        _trim_blank(body)
    return result


def load_config(path):
    """``(зоны, чужие секции)`` одним чтением — то, что нужно редактору."""
    return load_zones(path), load_extra_sections(path)


def _raw_section(path, name):
    """Тело секции ``name`` из файла дословно; ``None`` — секции нет.

    Нужно для секций, которых нет в ``extra``, но которые обязаны пережить
    сохранение зон. ``[move]`` — как раз такая: она «своя»
    (:data:`RESERVED_SECTIONS`), поэтому :func:`load_extra_sections` её не
    отдаёт, а терять чужие сочетания при сохранении раскладки нельзя.
    Пустая секция — ``[]`` (заголовок будет записан, тело — нет).
    """
    if not path or not os.path.exists(path):
        return None
    wanted = str(name).strip().lower()
    body, current = None, False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            match = _SECTION_RE.match(line)
            if match:
                current = match.group(1).strip().lower() == wanted
                if current and body is None:
                    body = []
                continue
            if current:
                body.append(line)
    if body is None:
        return None
    _trim_blank(body)
    return body


def _section_lines(body):
    """Тело секции как строки: готовые строки пишутся как есть, пары ``(k, v)`` — как ``k = v``."""
    out = []
    for item in body:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(f"{item[0]} = {item[1]}")
        else:
            out.append(str(item))
    return out


def _zone_lines(zones):
    """Строки тела секции набора зон: ``имя = x, y, w, h``."""
    return [f"{z.name} = {z.fmt()}" for z in zones]


def _key_lines(zones):
    """Строки ``[keys]`` без повторов: одна клавиша на имя зоны.

    ``[keys]`` одна на файл, а зона одного имени может стоять и в общем наборе,
    и в наборах мониторов. Повтор имени в секции — это не только лишняя строка:
    ``ConfigParser`` падает на повторной опции, и файл перестаёт читаться.
    Поэтому привязка пишется только при первом появлении имени: наборы идут в
    порядке записи (общий первым), и его клавиша главная. Пустые привязки не
    пишутся вовсе.
    """
    out, seen = [], set()
    for zone in zones:
        binding = zone.binding.strip()
        if not binding or zone.name in seen:
            continue
        seen.add(zone.name)
        out.append(f"{zone.name} = {binding}")
    return out


def _write_config(path, default_zones, monitor_sets=None, extra=None):
    """Записать ``zones.conf``: ``[grid]``, наборы зон, ``[keys]`` и чужие секции.

    ``default_zones=None`` — секции ``[zones]`` в файле не будет (набор по
    умолчанию не задан). Привязки ``[keys]`` собираются со всех записанных
    наборов, поэтому горячие клавиши соседних мониторов не теряются.

    ``[move]`` пишется перед ``[daemon]``: тело берётся из ``extra``, а если
    его там нет — дословно из прежнего файла (:func:`_raw_section`). Так
    сохранение зон не сбрасывает переопределённые человеком сочетания.
    """
    extra = {str(k).strip().lower(): list(v) for k, v in (extra or {}).items()}
    lines = [
        # Заголовок файла — не текст интерфейса, а содержимое zones.conf.
        "# Сетка окон для zone-tiler. Файл создан визуальным редактором zone-editor.py.",  # i18n: не переводится
        "# Зона = x,y,w,h в долях рабочей области (0..1), без панели задач.",  # i18n: не переводится
        "# Горячие клавиши — в секции [keys].",  # i18n: не переводится
        "# Набор монитора — секция вида zones:<имя выхода>, например zones:HDMI-1.",  # i18n: не переводится
        "",
        "[grid]",
        "cols = 1.0",
        "rows = 1.0",
    ]
    written_zones = []
    if default_zones is not None:
        lines += ["", "[zones]"]
        lines += _zone_lines(default_zones)
        written_zones += list(default_zones)
    for monitor, zones in (monitor_sets or {}).items():
        if not zones:
            continue
        lines += ["", f"[{_zone_section_name(monitor)}]"]
        lines += _zone_lines(zones)
        written_zones += list(zones)

    def append_section(name):
        body = extra.get(name)
        if body is None:
            return
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(_section_lines(body))

    for name in BEFORE_KEYS:
        append_section(name)

    key_lines = _key_lines(written_zones)
    if key_lines:
        lines += ["", "[keys]"]
        lines.extend(key_lines)

    # ``[move]`` — своя секция (её читает zone_config), но редактор её не правит:
    # сохраняем как было, чтобы переопределённые сочетания не терялись.
    move_body = extra.pop(MOVE_SECTION, None)
    if move_body is None:
        move_body = _raw_section(path, MOVE_SECTION)
    if move_body is not None:
        lines += ["", f"[{MOVE_SECTION}]"]
        lines.extend(_section_lines(move_body))

    written = set(BEFORE_KEYS) | set(AFTER_KEYS)
    for name in AFTER_KEYS:
        append_section(name)
        written.add(name)
    for name in extra:
        if name in written or name in RESERVED_SECTIONS or _monitor_of(name) is not None:
            continue
        append_section(name)

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def save_zone_sets(path, default_zones, monitor_sets=None, extra=None):
    """Записать все наборы зон за один проход файла.

    ``default_zones`` — набор по умолчанию: список зон, ``None`` — секции
    ``[zones]`` в файле не будет. ``monitor_sets`` — ``{имя выхода: [Zone]}``;
    пустые наборы и пустые имена выходов отбрасываются, имя выхода берётся как
    есть (регистр значим). ``extra`` — словарь вида
    :func:`load_extra_sections` (``{секция: [строки]}``): секции, которых в нём
    нет, в файл не попадают.

    Файл переписывается целиком, поэтому читать его не нужно: битый ``[keys]``
    с повтором имени не мешает записи (он и появлялся от таких повторов).
    Исключение — ``[move]``: её тело читается из прежнего файла (или берётся
    из ``extra``), чтобы сохранение зон не сбросило настройку клавиш.
    ``[keys]`` собирается со всех записанных наборов без повторов — по клавише
    на имя зоны, главная из набора по умолчанию (он пишется первым). Порядок:
    ``[grid]``, ``[zones]``, ``[zones:*]``, ``[rules]``, ``[ignore]``, ``[keys]``, ``[move]``,
    ``[daemon]``, остальные — после. Возвращает путь.
    """
    sets = {}
    for monitor, zones in (monitor_sets or {}).items():
        name = _clean_monitor(monitor)
        if name is not None and zones:
            sets[name] = list(zones)
    _write_config(path, default_zones, sets, extra)
    return path


def save_zones(path, zones, extra=None, monitor=None):
    """Записать набор зон: ``[zones]`` без ``monitor``, ``[zones:<монитор>]`` с ним.

    ``extra`` — словарь вида :func:`load_extra_sections` (``{секция: [строки]}``);
    секции, которых в нём нет, в файл не попадают. Остальные наборы мониторов
    берутся из текущего файла, поэтому сохранение одного набора не теряет
    соседние (и их привязки в ``[keys]``). Пустой набор монитора удаляет его
    секцию: набор без зон — невалидный конфиг. Это удобная обёртка над
    :func:`save_zone_sets` для одного набора; когда записываются все наборы
    сразу (редактор), дешевле позвать её.
    """
    target = _clean_monitor(monitor)
    default, monitors = _zone_sets(path)
    if target is None:
        default = list(zones)
    elif zones:
        monitors[target] = list(zones)
    else:
        monitors.pop(target, None)
    return save_zone_sets(path, default, monitors, extra)


# ------------------------------------------------- проверка правил [rules]

# Инлайн-комментарий: ` # ...` или ` ; ...`, как у ConfigParser (нужен пробел перед знаком).
_INLINE_COMMENT_RE = re.compile(r"\s[#;].*$")


def _rule_pairs(body):
    """Пары ``(selector, zone)`` из тела секции ``[rules]`` в порядке появления."""
    pairs = []
    for item in body or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            selector, zone = str(item[0]).strip(), str(item[1]).strip()
        else:
            line = str(item).strip()
            if not line or line[0] in "#;":
                continue
            line = _INLINE_COMMENT_RE.sub("", line)
            if "=" not in line:
                continue
            selector, zone = (part.strip() for part in line.split("=", 1))
        if selector:
            pairs.append((selector, zone))
    return pairs


def missing_rule_targets(extra_sections, zone_names):
    """Правила ``[rules]``, ссылающиеся на отсутствующие зоны.

    Возвращает список пар ``(selector, отсутствующая_зона)`` в порядке, в
    котором правила записаны (первое совпадение важнее в ``match_rules``).
    Пустой список, если секции ``[rules]`` нет или все её цели на месте.
    Повторяет семантику ``zone-tiler.Grid``: тело секции — строки вида
    ``selector = zone``, инлайн-комментарии после пробела отбрасываются,
    имя зоны сверяется точно. Модуль остаётся чистым — без GTK.
    """
    body = None
    for key, value in (extra_sections or {}).items():
        if str(key).strip().lower() == "rules":
            body = value
            break
    if not body:
        return []
    names = {getattr(name, "name", name) for name in zone_names}
    return [(sel, zone) for sel, zone in _rule_pairs(body) if zone not in names]


# ------------------------------------------------------- имена зон (чистые)


def sanitize_name(text):
    """Имя зоны из поля ввода: пробелы и посторонние символы — в дефисы."""
    return re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "-", (text or "").strip())  # i18n: не переводится


def unique_name(zones, base="zone"):
    """Свободное имя вида ``zone1``, ``zone2``, … среди уже занятых."""
    names = {z.name for z in zones}
    i = 1
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"


# --------------------------------------------------------------- профили

PROFILE_EXT = ".conf"

# Переносимый формат профиля (JSON). Ключ версии обязателен: чужая версия —
# не повод угадывать, что имел в виду автор файла.
PROFILE_JSON_KEY = "gridsnap_profile"
PROFILE_JSON_VERSION = 1

# Доли в JSON округляются до 4 знаков — столько же, сколько пишет ``Zone.fmt``
# в ``.conf``; больше знаков файл профиля всё равно не сохранит.
PROFILE_JSON_DIGITS = 4


def profiles_dir(conf_path=None):
    """Каталог профилей.

    Рядом с активным ``zones.conf`` (``<каталог>/templates``) — тогда ``--conf``
    уводит профили за собой; без аргумента — XDG-каталог
    ``$XDG_CONFIG_HOME/gridsnap/templates`` (по умолчанию
    ``~/.config/gridsnap/templates``).
    """
    if conf_path:
        return os.path.join(os.path.dirname(os.path.abspath(conf_path)), "templates")
    return os.path.join(zt.config_home(), "gridsnap", "templates")


def sanitize_profile_name(text):
    """Имя профиля тем же правилом, что имя зоны; пустое и «.», «..» отбрасываются."""
    name = sanitize_name(text)
    if not name or name in (".", ".."):
        return ""
    return name


def profile_path(name, conf_path=None):
    """Путь к ``<имя>.conf`` в каталоге профилей; плохое имя — ``ValueError``.

    Имя санируется, поэтому ``.conf`` не может выйти за каталог шаблонов: у
    результата тот же каталог, что у самого каталога профилей.
    """
    safe = sanitize_profile_name(name)
    if not safe:
        raise ValueError(f"плохое имя профиля: {name!r}")
    directory = profiles_dir(conf_path)
    path = os.path.join(directory, safe + PROFILE_EXT)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(directory):
        raise ValueError(f"плохое имя профиля: {name!r}")
    return path


def list_profiles(conf_path=None):
    """Имена сохранённых профилей (без ``.conf``) по алфавиту; нет каталога — пусто."""
    directory = profiles_dir(conf_path)
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        if entry.endswith(PROFILE_EXT) and os.path.isfile(os.path.join(directory, entry)):
            names.append(entry[: -len(PROFILE_EXT)])
    return sorted(names)


def _profile_missing(name):
    return FileNotFoundError(f"нет профиля «{name}»")


def profile_text(name, conf_path=None):
    """Содержимое файла профиля дословно (для ``profile show``)."""
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise _profile_missing(sanitize_profile_name(name)) from exc


def read_profile_sets(name, conf_path=None):
    """``(зоны [zones], строки [rules], {монитор: [Zone]}, строки [ignore])``.

    Зоны несут привязки из ``[keys]``; набор по умолчанию — всегда список
    (пустой, если секции ``[zones]`` в профиле нет), наборы мониторов — в
    порядке секций файла. ``[ignore]`` идёт вместе с ``[rules]``: это одно
    семейство правил окон, и расхождение «правило уехало, а исключение осталось»
    удивляло бы.
    """
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    default, monitors = _zone_sets(path)
    extra = load_extra_sections(path, sections=["rules", "ignore"])
    return (default or []), extra.get("rules") or [], monitors, extra.get("ignore") or []


def read_profile(name, conf_path=None):
    """``(зоны, строки [rules], строки [ignore])`` профиля; зоны с привязками.

    Это только набор по умолчанию (``[zones]``) — ради совместимости с
    редактором; все наборы отдаёт :func:`read_profile_sets`.
    """
    zones, rules, _monitors, ignore = read_profile_sets(name, conf_path)
    return zones, rules, ignore


def save_profile(name, zones, rules=None, conf_path=None, monitor_sets=None, ignore=None):
    """Записать профиль: ``[zones]``, ``[zones:*]``, ``[rules]``, ``[ignore]``, ``[keys]``.

    ``[grid]``/``[daemon]`` и прочие чужие секции в профиль не тащим — он и
    есть только раскладка. ``monitor_sets`` — ``{имя выхода: [Zone]}``;
    ключи ``None``/пустые игнорируются (набор по умолчанию — ``zones``).
    ``ignore`` — строки секции ``[ignore]`` (селекторы исключённых окон).
    Возвращает путь.
    """
    sets = {}
    for monitor, monitor_zones in (monitor_sets or {}).items():
        clean = _clean_monitor(monitor)
        if clean is not None and monitor_zones:
            sets[clean] = list(monitor_zones)
    if not zones and not sets:
        raise ValueError("профиль без зон сохранять нечего")
    path = profile_path(name, conf_path)
    safe = sanitize_profile_name(name)
    lines = [
        f"# Профиль раскладки GridSnap: {safe}.",  # i18n: не переводится
        "# Секции [zones], [zones:<монитор>], [rules], [ignore], [keys]; "  # i18n: не переводится
        "активация — zone-tiler.py profile use <имя>.",
    ]
    written = []
    if zones:
        lines += ["", "[zones]"]
        lines += _zone_lines(zones)
        written += list(zones)
    for monitor, monitor_zones in sets.items():
        lines += ["", f"[{_zone_section_name(monitor)}]"]
        lines += _zone_lines(monitor_zones)
        written += list(monitor_zones)
    if rules:
        lines += ["", "[rules]"]
        lines.extend(_section_lines(rules))
    if ignore:
        lines += ["", "[ignore]"]
        lines.extend(_section_lines(ignore))
    key_lines = _key_lines(written)
    if key_lines:
        lines += ["", "[keys]"]
        lines.extend(key_lines)
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise OSError(f"не удалось записать профиль «{safe}»: {exc}") from exc
    return path


def remove_profile(name, conf_path=None):
    """Удалить файл профиля; нет файла — ``FileNotFoundError``. Возвращает путь."""
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    try:
        os.remove(path)
    except OSError as exc:
        raise _profile_missing(sanitize_profile_name(name)) from exc
    return path


class ProfileJsonError(ValueError):
    """Ошибка JSON-профиля: формат, версия или значения (в CLI — код 3)."""


# --------------------------------------------- перенос профиля в JSON
def _json_number(value):
    """Доля в JSON: целое остаётся целым (0, 1), остальное — float."""
    number = round(float(value), PROFILE_JSON_DIGITS)
    return int(number) if number == int(number) else number


def _json_zone(zones):
    """Набор зон как ``{имя: [x, y, w, h]}`` в порядке зон набора."""
    return {z.name: [_json_number(v) for v in z.as_tuple()] for z in zones or []}


def _zone_keys(zones, monitor_sets=None):
    """``{зона: сочетание}`` из привязок наборов.

    Привязки лежат в ``[keys]`` и проставляются зонам при чтении. Одна зона
    может стоять в нескольких наборах, а ``[keys]`` пишется по первому
    появлению имени (:func:`_key_lines`), поэтому и здесь главное — первое.
    """
    keys = {}
    chunks = [zones or []] + list((monitor_sets or {}).values())
    for chunk in chunks:
        for zone in chunk:
            binding = (zone.binding or "").strip()
            if binding and zone.name not in keys:
                keys[zone.name] = binding
    return keys


def profile_to_dict(name, conf_path=None):
    """Профиль → dict переносимого формата (``gridsnap_profile`` версии 1).

    ``zones`` — набор по умолчанию (пустой словарь, если секции ``[zones]``
    нет), ``monitors`` — наборы ``[zones:<выход>]`` (ключа нет, если их нет),
    ``rules`` — строки ``[rules]`` как в файле, ``ignore`` — строки ``[ignore]``,
    ``keys`` — ``{зона: сочетание}`` из ``[keys]``. Доли округлены до
    :data:`PROFILE_JSON_DIGITS`.
    Профиля нет — ``FileNotFoundError``; битый ``.conf`` — ``SystemExit``.
    """
    zones, rules, monitor_sets, ignore = read_profile_sets(name, conf_path)
    data = {
        PROFILE_JSON_KEY: PROFILE_JSON_VERSION,
        "name": sanitize_profile_name(name),
        "zones": _json_zone(zones),
    }
    if monitor_sets:
        data["monitors"] = {m: _json_zone(zs) for m, zs in monitor_sets.items()}
    data["rules"] = list(rules or [])
    data["ignore"] = list(ignore or [])
    data["keys"] = _zone_keys(zones, monitor_sets)
    return data


def dumps_profile(data):
    """Стабильный текст JSON-профиля: ключи по алфавиту, отступ 2, UTF-8 как есть.

    Стабильность нужна для git: один и тот же профиль должен давать один и тот
    же файл, иначе диффы гуляют от порядка обхода словаря.
    """
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_profile_json(data, path):
    """Записать JSON-профиль в файл (каталог создаётся); возвращает путь.

    Кривой путь или отказ файловой системы — ``ProfileJsonError`` с подсказкой.
    """
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dumps_profile(data))
    except OSError as exc:
        raise ProfileJsonError(f"не удалось записать JSON-профиль {path}: {exc}") from exc
    return path


def read_profile_json(path):
    """Прочитать dict из файла JSON-профиля; кривой JSON — ``ProfileJsonError``."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise ProfileJsonError(f"не удалось прочитать JSON-профиль {path}: {exc}") from exc
    except ValueError as exc:
        raise ProfileJsonError(f"{path} — не JSON: {exc}") from exc


def profile_zone_total(data):
    """Сколько зон в dict профиля: набор по умолчанию плюс наборы мониторов."""
    total = len(data.get("zones") or {})
    for body in (data.get("monitors") or {}).values():
        total += len(body or {})
    return total


def _dict_zone_rect(name, spec):
    """``[x, y, w, h]`` из JSON с проверками; ошибка — ``ProfileJsonError``."""
    if isinstance(spec, (str, bytes, dict)):
        raise ProfileJsonError(f"зона '{name}': ожидался список [x, y, w, h], а не {spec!r}")
    try:
        values = list(spec)
    except TypeError:
        raise ProfileJsonError(
            f"зона '{name}': ожидался список [x, y, w, h], а не {spec!r}"
        ) from None
    if len(values) != 4:
        raise ProfileJsonError(f"зона '{name}': ожидалось 4 доли [x, y, w, h], а не {spec!r}")
    numbers = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProfileJsonError(f"зона '{name}': доли должны быть числами, а не {item!r}")
        numbers.append(float(item))
    x, y, w, h = numbers
    if not (
        0 <= x < 1
        and 0 <= y < 1
        and w > 0
        and h > 0
        and x + w <= 1.0001
        and y + h <= 1.0001
    ):
        raise ProfileJsonError(
            f"зона '{name}': доли вне 0..1 или w/h ≤ 0: {spec!r}"
        )
    return x, y, w, h


def _dict_zones(body, where):
    """``{имя: [x, y, w, h]}`` из JSON → ``[Zone]`` с проверками."""
    if body is None:
        return []
    if not isinstance(body, dict):
        raise ProfileJsonError(
            f"в JSON-профиле {where} должен быть объектом {{зона: [x, y, w, h]}}"
        )
    zones = []
    for raw_name, spec in body.items():
        name = str(raw_name).strip()
        if not name:
            raise ProfileJsonError(f"в JSON-профиле {where}: пустое имя зоны")
        zones.append(Zone(name, *_dict_zone_rect(name, spec)))
    return zones


def profile_from_dict(data, name=None, conf_path=None):
    """dict ``gridsnap_profile`` → файл профиля ``templates/<имя>.conf``.

    Имя берётся из аргумента ``name``, иначе из ``data["name"]``, и санируется
    (:func:`sanitize_profile_name`); пустое — ``ValueError``. Ошибки формата —
    :class:`ProfileJsonError` (в CLI это код 3): не та или отсутствующая
    версия, нет ни одной зоны, зона вне 0..1 или с неположительными ``w``/``h``,
    правило на несуществующую зону, кривые типы. Клавиши к зонам, которых в
    профиле нет, отбрасываются молча — ``[keys]`` пишется по зонам набора.
    Порядок зон внутри набора — как в JSON-объекте (после файла на диске — по
    алфавиту, так работает ``sort_keys``): имена, доли и привязки значат, а
    порядок в ``.conf`` — только вид.
    Возвращает путь записанного профиля.
    """
    if not isinstance(data, dict):
        raise ProfileJsonError("в JSON-профиле ожидался объект {...}")
    version = data.get(PROFILE_JSON_KEY)
    if isinstance(version, bool) or version != PROFILE_JSON_VERSION:
        raise ProfileJsonError(
            f"в JSON-профиле {PROFILE_JSON_KEY} должно быть "
            f"{PROFILE_JSON_VERSION}, а не {version!r}"
        )

    safe = sanitize_profile_name(name if name is not None else data.get("name"))
    if not safe:
        raise ValueError(
            "плохое имя профиля: укажите --name, в JSON имя "
            f"{data.get('name')!r} не годится"
        )

    zones = _dict_zones(data.get("zones"), "zones")
    raw_monitors = data.get("monitors")
    if raw_monitors is None:
        raw_monitors = {}
    if not isinstance(raw_monitors, dict):
        raise ProfileJsonError(
            "в JSON-профиле monitors должен быть объектом {выход: {зона: [x, y, w, h]}}"
        )
    monitor_sets = {}
    for raw_monitor, body in raw_monitors.items():
        monitor = _clean_monitor(raw_monitor)
        if monitor is None:
            raise ProfileJsonError("в JSON-профиле пустое имя монитора в monitors")
        parsed = _dict_zones(body, f"monitors.{monitor}")
        if parsed:
            monitor_sets[monitor] = parsed

    names = {z.name for z in zones}
    for chunk in monitor_sets.values():
        names |= {z.name for z in chunk}
    if not names:
        raise ProfileJsonError("в JSON-профиле нет ни одной зоны")

    rules = data.get("rules")
    if rules is None:
        rules = []
    if isinstance(rules, str) or not isinstance(rules, (list, tuple)):
        raise ProfileJsonError("в JSON-профиле rules должен быть списком строк")
    rules = [str(line) for line in rules]
    for line in rules:
        text = line.strip()
        if not text or text[0] in "#;":
            continue
        if "=" not in _INLINE_COMMENT_RE.sub("", text):
            raise ProfileJsonError(f"правило без «селектор = зона»: {line!r}")
    for selector, target in _rule_pairs(rules):
        if target not in names:
            raise ProfileJsonError(
                f"правило «{selector} = {target}» ссылается на неизвестную зону '{target}'"
            )

    ignore = data.get("ignore")
    if ignore is None:
        ignore = []
    if isinstance(ignore, str) or not isinstance(ignore, (list, tuple)):
        raise ProfileJsonError("в JSON-профиле ignore должен быть списком строк")
    ignore = [str(line) for line in ignore]
    for line in ignore:
        text = line.strip()
        if not text or text[0] in "#;":
            continue
        if "=" not in _INLINE_COMMENT_RE.sub("", text):
            raise ProfileJsonError(f"исключение без «селектор = заметка»: {line!r}")

    keys = data.get("keys")
    if keys is None:
        keys = {}
    if not isinstance(keys, dict):
        raise ProfileJsonError("в JSON-профиле keys должен быть объектом {зона: сочетание}")
    bindings = {str(k).strip(): str(v).strip() for k, v in keys.items()}
    for chunk in [zones] + list(monitor_sets.values()):
        for zone in chunk:
            zone.binding = bindings.get(zone.name, "")

    return save_profile(
        safe, zones, rules, conf_path, monitor_sets=monitor_sets, ignore=ignore
    )


def apply_profile(name, conf_path=None):
    """Активировать профиль в ``zones.conf``.

    Наборы зон ``[zones]``/``[zones:*]``, ``[rules]``, ``[ignore]`` и ``[keys]``
    берутся из профиля (``[keys]`` — из привязок зон) и заменяют наборы активного
    конфига целиком: наборов мониторов, которых в профиле нет, в конфиге не
    останется. ``[grid]``, ``[daemon]`` и прочие секции активного конфига
    сохраняются. Возвращает зоны набора по умолчанию (пустой список, если его в
    профиле нет).
    """
    zones, rules, monitor_sets, ignore = read_profile_sets(name, conf_path)
    extra = load_extra_sections(conf_path)
    if rules:
        extra["rules"] = rules
    else:
        extra.pop("rules", None)
    if ignore:
        extra["ignore"] = ignore
    else:
        extra.pop("ignore", None)
    _write_config(conf_path, zones or None, monitor_sets, extra)
    return zones

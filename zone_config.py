#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_config.py — чистая работа с zones.conf без GTK.

Здесь живёт всё, что редактор зон (``zone-editor.py``) делает с конфигом:
модель зоны, загрузка раскладки через ``zone-tiler.py``, сохранение в
каноническом виде и перенос «чужих» секций (``[rules]``, ``[daemon]``, …)
дословно.

Модуль намеренно не импортирует gi/GTK: его можно и нужно тестировать
обычными юнит-тестами в CI, где ``python3-gi`` не установлен.
"""

import importlib.util
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")

# Секции, которыми управляет сам редактор: их нельзя переносить как «чужие».
RESERVED_SECTIONS = frozenset({"grid", "zones", "keys"})

# Порядок «чужих» секций при сохранении: [rules] до [keys], [daemon] после.
BEFORE_KEYS = ("rules",)
AFTER_KEYS = ("daemon",)


def load_zonetiler(path=ZONEBIN):
    """Загрузить ``zone-tiler.py`` как модуль (в нём парсер конфига)."""
    spec = importlib.util.spec_from_file_location("zone_tiler", path)
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


def load_zones(path):
    """Зоны из ``[zones]`` с привязками из ``[keys]``. Райзит SystemExit на битом конфиге."""
    grid = zt.Grid(path)
    keys = {}
    if grid.conf.has_section("keys"):
        keys = {k.strip(): v.strip() for k, v in grid.conf.items("keys")}
    zones = []
    for name in grid.zones:
        x, y, w, h = grid.fractions(name)
        zones.append(Zone(name, x, y, w, h, keys.get(name, "")))
    return zones


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
                if current not in RESERVED_SECTIONS and (wanted is None or current in wanted):
                    result.setdefault(current, [])
                continue
            if current is not None and current in result:
                result[current].append(line)
    for body in result.values():
        _trim_blank(body)
    return result


def load_config(path):
    """``(зоны, чужие секции)`` одним чтением — то, что нужно редактору."""
    return load_zones(path), load_extra_sections(path)


def _section_lines(body):
    """Тело секции как строки: готовые строки пишутся как есть, пары ``(k, v)`` — как ``k = v``."""
    out = []
    for item in body:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(f"{item[0]} = {item[1]}")
        else:
            out.append(str(item))
    return out


def save_zones(path, zones, extra=None):
    """Записать ``zones.conf``: канонические ``[grid]``/``[zones]``/``[keys]`` плюс чужие секции.

    ``extra`` — словарь вида :func:`load_extra_sections` (``{секция: [строки]}``);
    секции, которых в нём нет, в файл не попадают. Порядок: ``[grid]``,
    ``[zones]``, ``[rules]``, ``[keys]``, ``[daemon]``, остальные — после.
    """
    extra = {str(k).strip().lower(): list(v) for k, v in (extra or {}).items()}
    lines = [
        "# Сетка окон для zone-tiler. Файл создан визуальным редактором zone-editor.py.",
        "# Зона = x,y,w,h в долях рабочей области (0..1), без панели задач.",
        "# Горячие клавиши — в секции [keys].",
        "",
        "[grid]",
        "cols = 1.0",
        "rows = 1.0",
        "",
        "[zones]",
    ]
    for z in zones:
        lines.append(f"{z.name} = {z.fmt()}")

    def append_section(name):
        body = extra.get(name)
        if body is None:
            return
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(_section_lines(body))

    for name in BEFORE_KEYS:
        append_section(name)

    bindings = [z for z in zones if z.binding.strip()]
    if bindings:
        lines += ["", "[keys]"]
        for z in bindings:
            lines.append(f"{z.name} = {z.binding.strip()}")

    written = set(BEFORE_KEYS) | set(AFTER_KEYS)
    for name in AFTER_KEYS:
        append_section(name)
        written.add(name)
    for name in extra:
        if name in written or name in RESERVED_SECTIONS:
            continue
        append_section(name)

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ------------------------------------------------------- имена зон (чистые)


def sanitize_name(text):
    """Имя зоны из поля ввода: пробелы и посторонние символы — в дефисы."""
    return re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "-", (text or "").strip())


def unique_name(zones, base="zone"):
    """Свободное имя вида ``zone1``, ``zone2``, … среди уже занятых."""
    names = {z.name for z in zones}
    i = 1
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"

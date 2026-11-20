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
    return re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "-", (text or "").strip())


def unique_name(zones, base="zone"):
    """Свободное имя вида ``zone1``, ``zone2``, … среди уже занятых."""
    names = {z.name for z in zones}
    i = 1
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"


# --------------------------------------------------------------- профили

PROFILE_EXT = ".conf"


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


def read_profile(name, conf_path=None):
    """``(зоны, строки [rules])`` профиля; зоны несут привязки из ``[keys]``."""
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    zones = load_zones(path)
    rules = load_extra_sections(path, sections=["rules"]).get("rules") or []
    return zones, rules


def save_profile(name, zones, rules=None, conf_path=None):
    """Записать профиль: ``[zones]`` + ``[rules]`` + ``[keys]``, без ``[grid]``/``[daemon]``.

    Чужие секции в профиль не тащим — он и есть только раскладка. Возвращает путь.
    """
    if not zones:
        raise ValueError("профиль без зон сохранять нечего")
    path = profile_path(name, conf_path)
    safe = sanitize_profile_name(name)
    lines = [
        f"# Профиль раскладки GridSnap: {safe}.",
        "# Секции [zones], [rules], [keys]; активация — zone-tiler.py profile use <имя>.",
        "",
        "[zones]",
    ]
    for z in zones:
        lines.append(f"{z.name} = {z.fmt()}")
    if rules:
        lines += ["", "[rules]"]
        lines.extend(_section_lines(rules))
    bindings = [z for z in zones if z.binding.strip()]
    if bindings:
        lines += ["", "[keys]"]
        for z in bindings:
            lines.append(f"{z.name} = {z.binding.strip()}")
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


def apply_profile(name, conf_path=None):
    """Активировать профиль в ``zones.conf``.

    Секции ``[zones]``/``[rules]``/``[keys]`` берутся из профиля (``[keys]`` — из
    привязок зон), ``[grid]``, ``[daemon]`` и прочие секции активного конфига
    сохраняются как есть. Возвращает зоны профиля.
    """
    zones, rules = read_profile(name, conf_path)
    extra = load_extra_sections(conf_path)
    if rules:
        extra["rules"] = rules
    else:
        extra.pop("rules", None)
    save_zones(conf_path, zones, extra)
    return zones

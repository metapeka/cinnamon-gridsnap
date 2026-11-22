---
id: zone_config
type: module-design
status: active
title: "Модуль: слой конфигурации zone_config"
parent: gridsnap-architecture
---

_Описано задним числом (долг спек закрыт в волне перевода man-страниц); владельцем не сверено._

## Ответственность

`zone_config.py` — весь разбор и запись `zones.conf` и профилей раскладок. Чистый модуль: ни `gi`, ни
GTK, ни X, поэтому конфиг-слой проверяется обычными юнит-тестами в CI, где `python3-gi` не установлен.
Сюда смотрит всё, что читает или пишет конфиг: редактор (`zone-editor.py`), пикер, демон, раскладчик
(через `load_zonetiler`), `setup-hotkeys.py`.

Модуль держит две вещи, которые легко разъезжаются, если их продублировать в вызывающих:

1. **Канонический вид `zones.conf`** — редактор сохраняет файл целиком, значит обязан не потерять
   секции, которыми не управляет.
2. **Наборы зон** — за монитор (`[zones:<имя выхода>]`) и профиль за стол/экран (`[workspaces]`,
   `[monitors]`), при том что сам набор зон — один и тот же тип.

## Модель

- `Zone(x, y, w, h)` — прямоугольник в долях рабочей области; рядом `_SpecParser`, разбирающий запись
  зоны из `[zones]` (свободный прямоугольник `x, y, w, h` или диапазон ячеек `c0:c1, r0:r1`).
- Зоны ищутся командой `zone-tiler.py list` (через `load_zonetiler`: `importlib` загружает модуль по
  пути `ZONEBIN`), поэтому раскладка не дублируется между редактором и раскладчиком.
- Имя зоны санируется `sanitize_name`/`unique_name` — так же, как имена профилей
  (`sanitize_profile_name`): путь не может выйти за каталог профилей.

## Возможности

**Зоны и файл конфига.**

- `load_zones(path, monitor)` / `save_zones(...)`, `load_zone_sets` / `save_zone_sets`,
  `list_zone_sets` — чтение и запись набора зон по умолчанию и наборов `[zones:<выход>]`, без потери
  соседних наборов.
- `load_extra_sections` — «чужие» секции (`[rules]`, `[ignore]`, `[daemon]`, `[action:*]`, …)
  поднимаются дословно и кладутся обратно; `RESERVED_SECTIONS` (`grid`, `zones`, `keys`, `move`) ими не
  считаются. `missing_rule_targets` находит правила на несуществующую зону.
- `load_config` / `_write_config` — атомарная запись в каноническом виде.

**Клавиши и секции-надстройки.**

- `load_move_keys` / `MOVE_DIRECTIONS` / `DEFAULT_MOVE_KEYS` — `[move]`, направления `left/right/up/down`;
  ключ не из направлений — ошибка конфига, а не тихо забытая клавиша.
- `load_focus_keys` — `[focus]` (режим «только эта зона»), `load_tabs_config` / `TABS_FIELDS` — `[tabs]`
  (сочетание и высота полосы), `load_actions` / `_action_entry` — `[action:<имя>]` со встроенными и
  своими командами (`ACTION_NAME_RE`, поля `run/zone/key/command/axis`).
- `load_zonetiler` даёт остальным процессам разобранные зоны и рамки раскладчика.

**Профили раскладок** (`~/.config/gridsnap/templates/*.conf`):

- `profiles_dir`, `profile_path`, `list_profiles`, `profile_text`, `read_profile`/`read_profile_sets`,
  `save_profile` (зоны + `[rules]` + `[ignore]` + привязки `[keys]`), `remove_profile`,
  `apply_profile` (замена секций в активном `zones.conf`).
- Экспорт/импорт JSON: `profile_to_dict`, `dumps_profile`, `write_profile_json`, `read_profile_json`,
  `profile_from_dict`, `profile_zone_total`, `ProfileJsonError`. Формат версионируется
  (`PROFILE_JSON_KEY`, `PROFILE_JSON_VERSION`, округление долей `PROFILE_JSON_DIGITS`).

**Профиль за стол и за экран.**

- `load_workspace_map` / `save_workspace_map` — секция `[workspaces]` (стол → профиль),
  `load_monitor_map` / `save_monitor_map` — `[monitors]` (выход → профиль).
- `resolve_profile(monitor_map, workspace_map, output, workspace_code)` — единственное место, где
  решается, чей профиль применить: **экран важнее стола**.
- `monitor_dialog_rows` / `workspace_dialog_rows` — строки для меню редактора («нет на экране»,
  «общий набор»); `WORKSPACE_SHARED`/`MONITOR_OFFLINE_NOTE` считаются на импорте, поэтому тесты
  выставляют язык (`tests/i18n_env.py`) до импорта.

## Границы

- Ни GTK, ни X, ни `gi` — инвариант проекта; всё, что требует дисплея, живёт в вызывающих модулях.
- Раскладку зон модуль не считает: геометрию и пиксельные рамки даёт `zone-tiler.py`/`zone_geom.py`.
- Права и конфигурация демона (`[daemon]`) — не его: секция переносится дословно.
- Формат `zones.conf` — INI UTF-8, разделитель только `=`, комментарии `#` и `;`.

## Проверки

- `tests/test_zone_config.py` — модель, чтение/запись, чужие секции, санитайз имён;
- `tests/test_profiles.py`, `tests/test_profile_json.py` — файлы профилей и JSON-обмен;
- `tests/test_workspaces.py`, `tests/test_monitor_profiles.py` — `[workspaces]`/`[monitors]` и
  `resolve_profile`;
- `tests/test_move.py`, `tests/test_focus_config.py`, `tests/test_tabs_config.py`,
  `tests/test_actions_config.py` — секции-надстройки;
- `tests/test_editor_sets.py` — наборы зон по мониторам на уровне редактора.

## Известные ограничения

- Модуль большой (≈1900 строк) и держит вместе файл, профили и диалоговые строки; разделение — отдельная
  работа, а не побочный эффект волны.

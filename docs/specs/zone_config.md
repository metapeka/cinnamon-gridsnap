---
id: zone_config
type: module-design
status: active
title: "Module: the configuration layer zone_config"
parent: gridsnap-architecture
---

_Described after the fact (the spec debt was closed in the man-page translation wave); not reviewed
by the owner._

## Responsibility

`zone_config.py` is all the parsing and writing of `zones.conf` and of the layout profiles. A pure
module: no `gi`, no GTK, no X, so the configuration layer is checked by ordinary unit tests in CI,
where `python3-gi` is not installed. Everything that reads or writes the config looks here: the
editor (`zone-editor.py`), the picker, the daemon, the tiler (through `load_zonetiler`) and
`setup-hotkeys.py`.

The module holds two things that drift apart easily when duplicated in the callers:

1. **The canonical form of `zones.conf`** — the editor saves the whole file, so it must not lose the
   sections it does not manage.
2. **Zone sets** — per monitor (`[zones:<output name>]`) and a profile per workspace or screen
   (`[workspaces]`, `[monitors]`), while a zone set is one and the same type.

## Model

- `Zone(x, y, w, h)` — a rectangle in fractions of the work area; next to it `_SpecParser`, which
  parses a zone entry from `[zones]` (a free rectangle `x, y, w, h` or a cell range `c0:c1, r0:r1`).
- Zones are fetched by the `zone-tiler.py list` command (through `load_zonetiler`: `importlib` loads
  the module by `ZONEBIN`), so the layout is not duplicated between the editor and the tiler.
- A zone name is sanitised by `sanitize_name`/`unique_name` — the same way as profile names
  (`sanitize_profile_name`). The name's own characters: Latin letters, digits, `_`, `.`, `-` and **all
  of Cyrillic in the range U+0400–U+04FF** (that is why "ё" and the Ukrainian letters survive);
  everything else, spaces included, is squeezed into one hyphen. A profile path therefore cannot
  leave the profiles directory.

## Capabilities

**Zones and the config file.**

- `load_zones(path, monitor)` / `save_zones(...)`, `load_zone_sets` / `save_zone_sets`,
  `list_zone_sets` — reading and writing the default zone set and the `[zones:<output>]` sets without
  losing the neighbouring sets.
- `load_extra_sections` — "foreign" sections (`[rules]`, `[ignore]`, `[daemon]`, `[action:*]`, …) are
  lifted verbatim and put back; `RESERVED_SECTIONS` (`grid`, `zones`, `keys`, `move`) do not count as
  foreign. `missing_rule_targets` finds rules that point at a non-existent zone. A repeated section
  (`[action:x]` twice) and two sections differing only in case (`[action:Main]` and `[action:main]`)
  are a config error (code 3): `ConfigParser` treats such sections as different, and merging them into
  one block would write a file that no longer reads.
- `load_config` / `_write_config` — an atomic write in the canonical form.

A parsing error of `ConfigParser` itself (a repeated section or key) is caught by `_read_conf` and
reported with the same code 3 and the file name: without it a person would see a traceback instead of
a line about a typo.

**Keys and extension sections.**

- `load_move_keys` / `MOVE_DIRECTIONS` / `DEFAULT_MOVE_KEYS` — `[move]`, directions
  `left/right/up/down`; a key that is not one of the directions is a config error, not a silently
  forgotten binding.
- `load_focus_keys` — `[focus]` (the "only this zone" mode); `load_tabs_config` / `TABS_FIELDS` —
  `[tabs]` (the binding and the strip height); `load_overlay_key` / `OVERLAY_SECTION` — `[overlay]`
  (the desktop grid binding: `<Super>g` without the section, an empty `key` turns it off);
  `load_actions` / `_action_entry` — `[action:<name>]` with built-in and custom commands
  (`ACTION_NAME_RE`, the fields `run/zone/key/command/axis`).
- `load_zonetiler` gives the other processes the parsed zones and the tiler's frames.

**Layout profiles** (`~/.config/gridsnap/templates/*.conf`):

- `profiles_dir`, `profile_path`, `list_profiles`, `profile_text`, `read_profile`/`read_profile_sets`,
  `save_profile` (zones + `[rules]` + `[ignore]` + `[keys]` bindings), `remove_profile`,
  `apply_profile` (replacing sections in the active `zones.conf`).
- JSON export/import: `profile_to_dict`, `dumps_profile`, `write_profile_json`, `read_profile_json`,
  `profile_from_dict`, `profile_zone_total`, `ProfileJsonError`. The format is versioned
  (`PROFILE_JSON_KEY`, `PROFILE_JSON_VERSION`, fraction rounding `PROFILE_JSON_DIGITS`).

**A profile per workspace and per screen.**

- `load_workspace_map` / `save_workspace_map` — the `[workspaces]` section (workspace → profile);
  `load_monitor_map` / `save_monitor_map` — `[monitors]` (output → profile).
- **The gallery of ready-made layouts:** `gallery_dir()` (the directory in the order
  `$GRIDSNAP_GALLERY` → `gallery/` next to the code → `<prefix>/share/gridsnap/gallery`),
  `gallery_ids()`/`gallery_layouts()` (the cards: id, title, description, zones, `order`),
  `load_gallery_layout(id)` (the whole layout), `install_gallery_layout(id, name, force, conf)` (a copy
  into a profile). The `[gallery]` section exists only in a gallery file and is not carried into a
  profile; errors are `GalleryError`. The cards are data, not literals: `gallery_sources()` returns
  them verbatim, `gallery_layouts()`/`load_gallery_layout()` translate the text through the catalog
  (`make pot` collects the strings through `tools/gallery-pot.py`), and with equal `order` the sort is
  by the source title, so the menu does not jump when the language changes.
- `resolve_profile(monitor_map, workspace_map, output, workspace_code)` — the single place that
  decides whose profile applies: **the screen wins over the workspace**.
- `monitor_dialog_rows` / `workspace_dialog_rows` — the rows of the editor's menus ("not on screen",
  "the shared set"); `WORKSPACE_SHARED`/`MONITOR_OFFLINE_NOTE` are computed at import, so tests set the
  language (`tests/i18n_env.py`) before importing.

## Boundaries

- No GTK, no X, no `gi` — a project invariant; everything that needs a display lives in the calling
  modules.
- The module does not compute the layout: geometry and pixel frames come from
  `zone-tiler.py`/`zone_geom.py`.
- The daemon's rights and configuration (`[daemon]`) are not its business: the section is carried
  verbatim.
- The `zones.conf` format is INI UTF-8, only `=` as the separator, `#` and `;` as comments.

## Checks

- `tests/test_zone_config.py` — the model, reading/writing, foreign sections, name sanitising;
- `tests/test_profiles.py`, `tests/test_profile_json.py` — profile files and the JSON exchange;
- `tests/test_workspaces.py`, `tests/test_monitor_profiles.py` — `[workspaces]`/`[monitors]` and
  `resolve_profile`;
- `tests/test_move.py`, `tests/test_focus_config.py`, `tests/test_tabs_config.py`,
  `tests/test_actions_config.py`, `tests/test_overlay_config.py` — the extension sections;
- `tests/test_editor_sets.py` — zone sets per monitor at the editor level.

## Known limitations

- The module is large (≈2300 lines) and keeps the file, the profiles and the dialog rows together;
  splitting it is a separate piece of work, not a side effect of a wave.

---
id: gridsnap-hotkeys
type: module-design
status: active
title: "Module: hotkeys setup-hotkeys"
parent: gridsnap-architecture
depends-on: [zone-tiler]
---

_Draft from `importing-a-codebase`: reconstructed from the code, not reviewed by the owner._

## Responsibility

`setup-hotkeys.py` carries the shortcuts from `zones.conf` into Cinnamon's custom keybindings
through `gsettings`: the `[keys]` section (zone = binding), the optional `[move]` (direction =
binding), the optional `[focus]` — one key for the "only this zone" mode, the optional `[tabs]` —
the "next window of the zone" key, the optional `[overlay]` — the "show the desktop grid" key, and
the `[action:<name>]` sections with a non-empty `key`.

## Contract

- the source of the "zone → binding" and "direction → binding" pairs is the tiler's CLI:
  `zone-tiler.py keys` (and `keys --move`); the `[focus]` section the module reads itself — through
  `zone_config.load_focus_keys()` (`{"key": binding, "zone": zone name | None}` or `None`), because
  it has no pair list and no output command of its own; `[tabs]` — through
  `zone_config.load_tabs_config()` (`{"key": binding | None, "height": strip height}`), of which the
  key needs only `key`; `[overlay]` — through `zone_config.load_overlay_key()` (a binding, or `None`
  when the key is switched off); actions — through `zone_config.load_actions()`, of which the key
  needs only a non-empty `key`;
- **the sections are read from the real file**, not from `--conf`: without the flag the path comes
  from the tiler (`zone-tiler.default_conf`: `~/.config/gridsnap/zones.conf`, and if there is none, the
  file next to the code). Otherwise `--list` without `--conf` would see an empty `None` and silently
  lose the `[focus]`/`[tabs]`/`[overlay]`/action bindings;
- the path to `zones.conf` can be given with `--conf PATH`: the flag goes to `zone-tiler.py keys`, and
  the binding's command is installed as `… apply <zone> --quiet --conf PATH`;
- the name of a binding is `zone-tiler: <zone>`; for a move — `zone-tiler: move <direction>`; for the
  mode — `zone-tiler: focus`; for tabs — `zone-tiler: tabs-next`; for the grid —
  `zone-tiler: desktop grid`; for an action — `zone-tiler: action <name>`;
- the binding's command is `python3 <dir>/zone-tiler.py apply <zone> --quiet`; for the mode —
  `… focus [<zone>] --quiet` (the zone is substituted only when `[focus]` names it; without it the
  mode only minimises extra windows and does not move the target); for tabs — `… tabs-next --quiet`
  (which zone's stack to switch is decided by the daemon from the active window); for the grid —
  `python3 <dir>/zone-picker.py --toggle --quiet`; for an action — `… action <name> --quiet`;
- the list schema is `org.cinnamon.desktop.keybindings` (`custom-list`); the bindings themselves are
  `org.cinnamon.desktop.keybindings.custom-keybinding:<base path>` with the base path
  `/org/cinnamon/desktop/keybindings/custom-keybindings/`;
- the module owns only the bindings whose name carries the `zone-tiler: ` prefix: before installing it
  resets exactly those and keeps every foreign id;
- the reset goes **key by key** (`gsettings reset <schema:/path/> name|binding|command`), not
  `reset-recursively`: on a live Cinnamon 6.4.10 a recursive reset of the relocatable schema does not
  clear a registered entry (measured: after it `name` stayed as it was, while a single `reset`
  removed it);
- removal (`--remove`) goes in the order **list → keys → list again**: Cinnamon (`keybindings.js`)
  rewrites `custom-list` from its own memory on every change, and the order "reset, then list" loses
  the race (on a live Cinnamon four bindings survived two `--remove` runs). The list without our ids,
  written first, resets that memory. After removal the command checks whether our ids came back,
  makes one more attempt and, if that fails, names the leftovers and returns code 1 — there is no
  quiet "removed" on a failure;
- new ids are taken as `customN`, skipping the ones already used.

Commands: without flags — install/update; `--list` — only show the pairs; `--remove` — remove the
`zone-tiler` bindings (code `1` and the names of the left ids if something could not be removed);
`--conf PATH` — work with another config and put it into the binding commands.

## Boundaries

- Depends on a working `gsettings` and the Cinnamon schemas; it does not work outside Cinnamon.
- It does not parse `[keys]`/`[move]`: the bindings come from the tiler, so it has to ship with the
  same version; `[focus]`, `[tabs]`, `[overlay]` and `[action:<name>]` are sections it reads itself
  (through `zone_config`).
- It touches no foreign bindings and performs no layout.

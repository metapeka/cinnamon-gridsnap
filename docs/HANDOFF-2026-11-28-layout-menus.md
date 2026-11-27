# Wave: the layout menus show the layout

The owner's report was visual: after a workspace scheme is made, the editor's menus say nothing
about it — Templates and Profiles were two columns of names, the “…” menu was one flat list, and
`Clear all…` sat directly under the harmless rows. This wave turns the menus into pictures and
separates the one irreversible row.

| Where | Change |
|---|---|
| `zone_menu.py` | new pure module: `thumb_rects(zones, w, h)` maps the fractions onto a small box (inset frame, a hair between neighbours) and `compact_ranges([1,2,3,5])` gives `1–3, 5` |
| `zone_config.py` | `profiles_overview(conf)` — one pass over the saved profiles: default-set zones for the thumbnail, the number of monitor sets, total zones, `[rules]` **pairs**, `[workspaces]`/`[monitors]` bindings, a `broken` flag for an unreadable file |
| `zone-editor.py` | Templates is a 2×3 grid of tiles drawn from `templates()` itself (“blank slate” is a dashed frame); Profiles rows carry a thumbnail, bold name, a totals line and a bindings line; the loaded draft has a ✓ and an accent tint; **Monitor's own set** is the way back from a draft; the “…” menu is grouped (Theme, Placement, Bindings) with `Clear all…` behind a gap, in the danger colour and with a warning icon; the gallery dialog draws each card's shape |
| `zone_theme.py` | the sheet gained `.layout-tile`, `.layout-row`, `.layout-row.current`, `.layout-current-mark`, `.menu-row`, and `css_rgba()` for the accent tint (GTK 3 `rgba()` takes 0–255 components) |
| `Makefile`, `install.sh`, `debian/install`, `PKGBUILD` | `zone_menu.py` in every installation path |
| `docs/specs/zone-editor.md`, `docs/specs/profiles.md`, `docs/architecture.md` | the menu contract, the `profiles_overview` row, the module and its bullet in the graph |
| `po/ru.po`, `po/zh_CN.po` | eleven strings (`Zones: {}`, `Monitor sets: {}`, `Rules: {}`, `Workspaces: {}`, `Monitors: {}`, `Monitor's own set`, `own set not read: {}`, `monitor's own set: {} zones`, `Placement`, `Bindings`, `{} — the file cannot be read`) plus the retuned “More:” tooltip; the fuzzy matches `msgmerge` made from neighbouring strings were rewritten by hand |
| `tests/test_zone_menu.py`, `tests/test_zone_config.py` | 18 new tests: the thumbnail geometry (box, gap, tiny zones, empty set) and the rows of the menu (counts, rule pairs, bindings, order, a broken file kept) |

## Decisions

1. **A thumbnail is computed, never drawn by hand.** `templates()` already returns the zones that
   `apply_template` uses, and a profile's file is the source for its row; the picture is
   `thumb_rects` over that geometry. A hand-drawn icon would be a second truth, and the first
   edited template would leave it lying.
2. **The editor does not read profile files.** Eight files, each with rules and bindings, are a data
   question: `profiles_overview` answers it in `zone_config`, and the rows are built from one list.
3. **The current draft is marked until the first edit.** `self.profile_source` is set by
   `load_profile` after `set_dirty(True)` (which clears it) and dropped by any later edit or monitor
   switch: a changed draft is nobody's profile, and a ✓ on it would be a lie.
4. **Monitor's own set is the first row and the only way back.** Loading a profile replaces the
   canvas, and before this wave the only exit was `Clear all…`; the row reloads the file's zones,
   rules and exclusions for the chip's monitor and drops that monitor's unsaved edits.
5. **Menu rows are flat buttons, not `ModelButton`s.** A `ModelButton` centres its label and cannot
   carry a two-line box. `button.menu-row` + `button.layout-row` make the popover left-aligned; the
   danger row needs `button.menu-row.destructive-action` because the sheet's later `.menu-row` rule
   would otherwise erase the danger colour (the first screenshot showed a black-on-dark row).
6. **The dangerous row is quiet, not a slab.** Transparent background, danger-coloured text and a
   warning icon, at the end behind its own gap; a full-width red block was the first attempt and
   competed with every other row.
7. **`[monitors]` keeps the config's spelling.** `ConfigParser` lowercases option names, so the
   overview returns `hdmi-1`; the editor restores the real case from `combo_entries` by `casefold`,
   and a profile bound to a monitor that is not connected keeps the lowercase form it has in the
   file instead of a guessed one.

## Verification

```
$ make check
Ran 1536 tests in 18.528s

OK (skipped=9)

$ make lint
All checks passed!

$ make check-i18n
msgfmt ru
msgfmt zh_CN
собраны каталоги в locale/
каталоги в порядке

$ make smoke
  ✔ x=0 y=0 1605x1080 — совпало с зоной  [main]
  ✔ x=1605 y=540 315x540 — совпало с зоной  [top-far]
  ✔ x=1605 y=0 315x540 — совпало с зоной  [top-right]
Дымовой тест пройден.
```

The GTK surface was checked on a private Xvfb (`:95`, a temp config with two profiles, a
`[workspaces]` and a `[monitors]` binding) by opening each popover and clicking through it:

- Templates: six tiles in two columns, thumbnails legible, “blank slate” dashed.
- Profiles: own set tinted with ✓; after choosing `w1-4` the mark moved to its row and the status
  said `profile "w1-4": 3 zones (don't forget to save)`; **Monitor's own set** put the four zones
  of `zones.conf` back and said `monitor's own set: 4 zones`.
- `…`: three groups, `Clear all…` behind the gap in the danger colour with the warning icon.
- Gallery: eight cards, each with its shape beside the title and description.

A chosen layout now closes its menu (`_from_popover`), which the first pass did not do — the
popover stayed over the canvas it had just changed.

## Next

- Push the wave; CI has not run on `8339094` yet.
- The Chinese catalog deserves a native proofread, now with eleven more rows.
- Wayland and the Debian archive are untouched by this wave.

# HANDOFF — current state of GridSnap

Entry point for a fresh session. Read this first, then `AGENTS.md`. The per-wave history lives in
`docs/HANDOFF-<date>-<slug>.md`; the newest is `docs/HANDOFF-2026-11-28-desktop-grid.md` — the zone
grid over the live desktop, `--toggle`, `[overlay]` and the `Super+g` key — before it
`docs/HANDOFF-2026-11-28-ui-surfaces.md` (the picker and the HUD neutral with hue dots, rounded tabs,
the quiet gallery selection), then
`docs/HANDOFF-2026-11-28-layout-menus.md` (the layout menus as pictures, the grouped “…” menu and
the way back from a draft), and
`docs/HANDOFF-2026-11-27-editor-rework.md` (the editor's neutral chrome plus the translated gallery
cards, #53 and #54), then `docs/HANDOFF-2026-11-26-demo-scene.md`,
`docs/HANDOFF-2026-11-26-screenshots.md`, `docs/HANDOFF-2026-11-26-docs.md`,
`docs/HANDOFF-2026-11-26-gtk-controls.md`, `docs/HANDOFF-2026-11-26-man-theme.md`,
`docs/HANDOFF-2026-11-26-theme-refresh.md` and `docs/HANDOFF-2026-11-26-theme-wallpaper.md` — read
them only when you need the “why”.

## Goal and scope

GridSnap gives Cinnamon/X11 user-drawn window zones — arbitrary rectangles, one key or a picker per
zone, windows placed through EWMH on top of muffin. Scope is the product only: a compositor or WM of
its own, a Windows port and browser automation are out of scope (`SPEC.md`, `docs/product.md`).

## State (snapshot)

| What | Value |
|---|---|
| Code | `main` = `73fcf7c5` (Put the zone grid on the live desktop — the wave record is `docs/HANDOFF-2026-11-28-desktop-grid.md`), then this record; before them `45d9454` (Record the UI surfaces wave), `79be393e` (Re-render the stills and the demo for the new surfaces) and `58b5768` (Give the window surfaces one accent — `docs/HANDOFF-2026-11-28-ui-surfaces.md`); before them `f2cc24f` (Record the layout menus in the handoff), `8339094` (Show the layout menus as pictures, not names), `38aa2de` (the tutorial draft and the deferred scope) and `34551a2` (the editor wave record); before them `60c1087` Merge #54: the gallery cards translated through the catalogs, work `a81889c`, and `4dc7b16` Merge #53: the editor's neutral chrome and one header, work `119791f`; before the editor waves `fafac1d` — the HANDOFF section rename for the OAW quality gate; the theme waves: palette `8b61c61`, wallpaper `ea2dc1e`, refresh `32b6aa5`, man page `60f288d`, GTK controls `575d601`, docs `7f8201d`, screenshots `3d61d20`, demo scene `65ad946`, release `47f8003`; the AUR checksum pin `a3de4c2`) — the three waves since `f2cc24f` are local; `origin/main` = `f2cc24f` |
| Version | `0.3.0`, tag `v0.3.0` + GitHub Release with `gridsnap-0.3.0.tar.gz`; AUR `sha256sums` is the tag archive (`031c2e3…`), which is not the attached tarball: the tag archive is what `source` names |
| Repo | `metapeka/cinnamon-gridsnap` (`gh` is authed as `metapeka`) |
| Language | Everything in the repo is **English**; talk to the owner in Russian |
| Tests | `1556` unit tests, `OK` in ~18 s without X (9 skipped once `make man` has run, 15 in a fresh checkout) |
| Smoke | `14` scenarios under `tests/smoke*.sh`, each on its own Xvfb display (`smoke-grid.sh` also runs `picom`) |
| CI | Green on `main` at `f2cc24f` (run `46084884631`, 114 s), 5 jobs; the UI surfaces wave (`58b5768`, `79be393e`, `45d9454`) and the desktop grid wave (`73fcf7c5`) are local, so CI has not run on them; `pages.yml` fires only on `site/**` changes and last deployed from `4dc7b16` (run `45990574013`), so the live site still shows the previous stills until the push |
| Open PRs | None |
| Live install | This checkout **is** the installed copy. The owner's desktop layout lives in `~/.config/gridsnap/zones.conf` (profiles in `~/.config/gridsnap/templates/`), which `zone-tiler.default_conf` finds first; the tracked root `zones.conf` is the shipped starting layout |

### The tree

Working tree clean apart from this record. The zone grid can now live on the desktop itself:
`zone-picker.py --grid` draws neutral frames and plates over whatever is open, marks the zone under
the pointer, and `--toggle` (bound to `<Super>g` from `[overlay]`, or the editor's **… → Placement →
Show the zones on the desktop**) shows and hides it per desktop. Before it the window surfaces took
the editor's language: the picker and the HUD paint zones neutral with the hue as a 7 px identity dot,
the tab strip rounds its tabs on top, the gallery selects with a tint instead of an accent slab, and
derived hues alternate lightness so neighbours stay apart. Before them the layout menus — thumbnails
in Templates and Profiles, the grouped “…” menu, the “Monitor's own set” way back — and the editor
rework. Wave records: `docs/HANDOFF-2026-11-28-desktop-grid.md`,
`docs/HANDOFF-2026-11-28-ui-surfaces.md` and `docs/HANDOFF-2026-11-28-layout-menus.md`.

Live side, outside git: `~/.config/gridsnap/theme.conf` is a marked neutral file (`bg #1c1c1e`,
`panel #232325`, accent `#3793be` from the wallpaper); the daemon runs `0.3.0` on the merged code —
pid `1064788`, registered as the `gridsnap-daemon` service, reading `~/.config/gridsnap/zones.conf`
(`gridsnap-ctl status` / `reload` confirmed).

## Checks (gates)

| Gate | Command | What it proves |
|---|---|---|
| Units | `make test` (= `env -u DISPLAY python3 -m unittest discover -s tests`) | all pure logic; must stay `OK` |
| Compile | `make check` | `compileall` over modules + tools + tests, then units |
| One smoke | `make smoke` | a window lands in each zone pixel-for-pixel on a private Xvfb + muffin |
| All smokes | `for s in tests/smoke*.sh; do env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh "$s"; echo "$s rc=$?"; done` | daemon, tabs, focus, monitor profiles, actions, hotcorners, events… |
| Lint | `make lint` (`ruff check .`, rules `ruff.toml`) | undefined, shadowed or redefined names; ~1 s |
| Version | `make check-version` | `VERSION` agrees across six files, PKGBUILD, man page, changelog |
| Catalogs | `make check-i18n` | `msgfmt --check`, templates vs code, no `ru.po` source catalog |
| Man pages | `make check-man-i18n` | po4a build, `--…` keys vs `--help`, widths under `zh_CN`/`ru_RU` |

Editor tooling: `basedpyright` and `ruff` are installed via `uv tool install`; the LSP setup is
navigation-only (`pyrightconfig.json` with `typeCheckingMode: off`) because most modules are untyped
ctypes/GTK code loaded through `importlib`. Type-checking warnings that remain are expected noise;
the real gates are `make lint`, `make test` and the smokes.

Rules: `env -u DISPLAY` always for units — with a live display some tests reach into the owner's session.
Smoke scripts raise their own `:89`–`:99`, so they are safe on a live desktop, but never point one at `:0`.

## Files (map)

| Module | Lines | Role |
|---|---|---|
| `zone-tiler.py` | 4077 | the core: config → zone model → EWMH placement; `list/apply/arrange/stack/undo/focus/profile/tabs-next/action` |
| `zone-daemon.py` | 3300 | optional long-lived daemon: `[rules]` auto-placement, reflow, tabs, IPC server, event subscriptions, the wallpaper poll and the controls-theme follow |
| `zone_config.py` | 2327 | pure `zones.conf` model: load/save canonically, per-monitor and per-workspace sets, profiles, `profiles_overview` for the Profiles menu, the `[focus]`/`[tabs]`/`[overlay]`/`[action:*]` sections; no GTK |
| `zone-editor.py` | 2048 | GTK zone editor: header (monitor chip, Templates tiles, Profiles rows with thumbnails, Save, grouped “…” menu), zone list + inspector sidebar, canvas, gallery with shapes, the “Monitor's own set” row, hotkey registration, **Theme** menu (wallpaper palette, controls theme, reload), the **Show the zones on the desktop** row |
| `zone-hotcorners.py` | 1186 | drag watcher over XInput2 raw events → HUD → place on drop |
| `zone-picker.py` | 1418 | overlay zone picker / `--hud` passive grid / `--grid` desktop grid with `--toggle`: neutral zone surfaces, the hue as an identity dot, one accent for the zone under the pointer |
| `window_backend.py` | 740 | the single seam to the window system (X11/EWMH today; Wayland later) |
| `zone_ipc.py` | 587 | JSON-line IPC framing, command dispatch, UNIX socket |
| `zone-tabstrip.py` | 553 | tab strip window for a zone (daemon spawns it); tabs rounded on top, the accent stripe clipped to the path |
| `setup-hotkeys.py` | 389 | `gsettings` registration of Cinnamon custom keybindings: zone keys, moves, focus, tabs, the desktop grid toggle, actions |
| `zone_actions.py` | 348 | `[action:<name>]`: built-ins and external commands with `GRIDSNAP_*` |
| `zone_i18n.py` | 333 | gettext wrapper: source `en`, catalogs `ru`, `zh_CN` |
| `zone_theme.py` | 982 | one palette for the picker, the HUD, the tab strip and the editor: optional `theme.conf` next to `zones.conf`, five presets, a neutral chrome with one accent (a wallpaper chooses only the accent), zone-hue derivation as identity dots with alternating lightness, the editor's GTK sheet (tiles, rows, the current mark, the gallery's quiet selection), wallpaper sampling (GdkPixbuf), the generated GTK controls theme and its refresh (daemon poll and editor actions) |
| `zone_geom.py` | 333 | fractions ↔ pixels, frame extents, snapping helpers |
| `gridsnap-ctl` | 289 | CLI client for the daemon (also `watch`, `status`, `reload`) |
| `zone_menu.py`, `zone_tabs.py`, `zone_tabstrip_nav.py`, `zone_picker_nav.py`, `zone_units.py`, `zone_reflow.py`, `make-icon.py` | 66–225 each | pure helpers: menu thumbnails and compact number ranges, tab layout, keyboard nav, unit parsing, reflow, icons |

Behaviour contracts live in `docs/specs/` (19 files, one per module feature). Architecture overview:
`docs/architecture.md`. Product scope and non-goals: `SPEC.md`. Plan: `docs/ROADMAP.md`.

## Next (open work)

| Item | State |
|---|---|
| Push the local waves | Local `main` is ahead of `origin/main` (`f2cc24f`): `58b5768` (the surfaces), `79be393e` (re-rendered stills and demo), `45d9454` (their record), `73fcf7c5` (the desktop grid, its record included) and this record. CI has not run on them; the push also redeploys the site (`site/assets/` changed in the surfaces wave). |
| Wayland backend | Open, but the cheap part is done: the three compatibility fixes landed in PR #46 (`5c8d79c` — `current_workspace_or_first`/`x_alive` for the missing `_NET_CURRENT_DESKTOP`, `prefer_x11_gdk`, and `window_iconic` with callers that switch the desktop instead of trusting `Iconic`), held by `tests/test_wayland_compat.py`. What remains: the Cinnamon extension for Wayland-native windows (owner decision, not started), a live-session check of the three fixes, and the per-output fractional scale model. Cinnamon 6.8 / Mint 23 (~Christmas 2026) makes the Wayland session official. Measurements: `docs/RESEARCH-wayland-recon.md`. |
| Debian archive | `.deb` builds, license declared, lintian clean; missing ITP bug, sponsor, review. |
| Docs in Russian | `SPEC.md`, `docs/**` (except `zone-i18n`/`man-i18n`), `README.ru.md`, site, `Makefile`, `install.sh`, `PKGBUILD`, `debian/**`, `tools/**`, `tests/smoke*.sh` were written before the language inversion. Translate them **as they are touched**, not in one sweep. Done so far: `docs/architecture.md` (English, with a Mermaid module graph), `docs/specs/theme.md`, `tools/shoot-screens.sh`, `tools/record-demo.sh`; the site and both translated READMEs carry §3.4 (theme). The rest is still Russian. |
| Owner actions after the last wave | Add the applet to the panel; after a Cinnamon restart re-run `python3 setup-hotkeys.py --remove` once (see `docs/HANDOFF-2026-11-22-events.md`). |
| Design track (43PR palette) | **Five code waves + a docs wave merged into `main`** (`8b61c61` palette, `ea2dc1e` wallpaper, `32b6aa5` refresh, `60f288d` man page, `575d601` GTK controls, `7f8201d` docs), all pushed since. Optional `theme.conf`, five presets, zone-hue derivation, editor GTK sheet; **Theme** menu in the editor: palette from the wallpaper (GdkPixbuf sampling, no new dependency) and a generated GTK controls theme. A generated `theme.conf` follows `picture-uri` through the daemon (5 s poll, marker-only, `--once` excluded); a generated controls theme (`~/.themes/Gridsnap`) follows the palette from the same poll and from the editor's actions. `man gridsnap` documents both with `ru`/`zh_CN`; `docs/architecture.md` is English with a Mermaid module graph, `docs/specs/theme.md` has two Mermaid flows, README ru/zh carry §3.4, the site carries the feature and the spec link, ROADMAP item 5 is closed. Contracts `docs/specs/theme.md`; records `…-design-theme.md`, `…-theme-wallpaper.md`, `…-theme-refresh.md`, `…-man-theme.md`, `…-gtk-controls.md`, `…-docs.md`, `…-screenshots.md`, `…-demo-scene.md`. The README and site images are no longer hand-made: `tools/shoot-screens.sh` re-renders `site/assets/editor.png` and `picker.png` on its own Xvfb `:91`, `tools/record-demo.sh` re-records `site/assets/demo.gif` on its own Xvfb `:90` with muffin and the daemon; both build the palette with `tools/scene-palette.py`, held by `tests/test_screenshots.py` (7 tests) and `tests/test_demo_gif.py` (14 tests). Remaining out of scope: GTK 4/libadwaita, the Cinnamon shell theme, metacity borders (Muffin never reads them), Quickshell/QML. |
| Tutorial | Personal Russian draft `.thinkrail/context/TUTORIAL-live.ru.md` (git-ignored) plus an HTML build in `.thinkrail/context/tutorial/` — `sh build.sh` renders a self-contained `index.html` (pandoc, embedded CSS and PNGs); two real stills so far (editor, picker), shot on Xvfb with `GRIDSNAP_LANG=ru`. **Deferred, recorded in the draft's “Что не сделано” section:** the public prose (only the skeleton `docs/tutorial.md` exists), six missing sessions (install, own grid in the editor, monitors, workspace profiles, IPC/`gridsnap-ctl`, troubleshooting), 21 of the 23 screenshots and a scene script (`tools/shoot-tutorial.sh` does not exist), the live-test journal rows, video (real capture is feasible via Xvfb + ffmpeg + xdotool — the `tools/record-demo.sh` pattern; `stageshow` is a faux React UI, not a recording of the product), the delivery decision (`docs/` vs `site/…`, languages) and the `tests/test_tutorial.py` gate. |

## Decisions (sharp edges)

| Trap | What to do |
|---|---|
| The editor's colour is identity, not decoration | The chrome is zinc-neutral (`bg #1c1c1e`, `panel #232325`, `card #2c2c2e`, `border #545456`) with one accent; a zone hue appears only as an identity dot (the zone list, and beside each name in the picker and the HUD), while the canvas, the picker and the HUD paint zones neutral and mark the selection with the accent. A wallpaper palette keeps the neutral chrome and takes only the accent — the violet cast on every surface was the rejected look (`docs/specs/theme.md`). Text on an accent uses `contrast_text` threshold `0.18` (where white and black ratios meet, so the default blue keeps white at 4.58:1); a `.dim-label` inside a selected row must take the selection colour, or the key hint turns grey-on-blue. |
| A zone hue fills nothing | The picker and the HUD paint a `bg` 0.55 scrim then an `fg` 0.05 surface with an `fg` 0.22 hairline, and the hue is a 7 px dot; four hue fills on one grid were four competing colours and took the accent's meaning away (`docs/HANDOFF-2026-11-28-ui-surfaces.md`). The surface pass comes before the labels and the selection between them: the accent wash over the chosen zone must not tint its own text. The scrim is not decoration — the HUD's backdrop is a quarter opaque, and a bare 5 % fill disappears over a bright wallpaper. |
| Derived hues alternate lightness | `derive_zones` steps 0.56/0.66 at the same saturation: hues 73° and 118° at one lightness read as the same green at 7 px, and a dot that names nothing is worse than no dot. A user's own `zones` list is taken as written — the editor's dot is the only place a bad pair shows. |
| The gallery's tall cards are the one quiet selection | `row:selected` stays a full accent fill for the 24 px zone list; the 60 px gallery cards take `.gallery-list row:selected` — an `accent` 0.16 tint with a 0.50 hairline and `fg` text, because a slab turned the layout shape orange and flattened the description. Its `.dim-label` must be reset to `fg_muted`, since the global `*:selected .dim-label` rule would otherwise follow the accent. The primary action is `button.suggested-action`; **Add to profiles…** carries the ellipsis because it opens a name prompt. |
| Tab tops are rounded, the stripe is clipped | `_tab_path` rounds the top corners (`TAB_RADIUS` 5 px, divided by the strip scale) and squares the bottom edge on the panel; the active tab's `accent` stripe is drawn inside that path, not as a rectangle, or the corners show square orange bites. |
| The desktop grid is a picker mode, not a binary of its own | `--grid`/`--toggle` live in `zone-picker.py`: a separate `gridsnap-grid` would need its own entry in four packaging lists, the man page, the CI and the docs for one `os.kill`. The toggle trusts a pid only when `/proc/<pid>/cmdline` names `zone-picker` and `--grid`, so a stale file or a recycled pid cannot kill an unrelated process. |
| One pid file per desktop | `$XDG_RUNTIME_DIR/gridsnap/grid-<desktop>.pid`: a toggle on another desktop must neither kill nor hide the grid of this one. The grid window is not sticky; switching the desktop hides it, and the toggle there shows that desktop's grid. |
| The grid refuses without a compositor | Without one the window has no alpha visual, the transparent clear turns opaque and a persistent window blacks out the desktop. `--grid` fails with code 1 before creating a window and names the compositor; Cinnamon always composites. `tests/smoke-grid.sh` starts `picom` and checks the refusal first, on the bare Xvfb screen. |
| The grid's frame carries its own contrast | A light hairline vanishes on a bright window and a dark one on a dark terminal: each frame is a `bg` 0.45 under-stroke with an `fg` 0.55 line on top, and the labels sit on a `bg` 0.66 plate. Screenshot over the cream wallpaper showed the bare hairline disappearing. The surface respects the theme tokens; nothing is hard-coded white. |
| `[overlay]` is the grid key, `[grid]` is not | `[grid]` is the editor's reserved section (`cols`/`rows`); the shortcut lives in the optional `[overlay]` (default `<Super>g`, an empty `key` turns it off), carried verbatim by the editor and registered as `zone-tiler: desktop grid`. gTile (README §2) also takes `Super+G`: the READMEs say to change `[overlay] key` when both are installed. |
| Gallery card text is data, not a literal | `gallery/*.conf` carries the English source (`title`, `description`); `gallery_layouts()` and `load_gallery_layout()` translate it through the catalog, and the menu sorts by the **source** title. `make pot` gets the cards from `tools/gallery-pot.py` and merges them with `msgcat`, so editing a card changes the `msgid`: run `make pot`, `msgmerge` both catalogs, translate the new entry. `make check-i18n` catches a stale template, `tests/test_i18n_catalogs.py` a missing `msgstr`, and `profile gallery` runs under `en`/`ru` there. The empty title never reaches gettext — the empty `msgid` is the catalog header. |
| The root `zones.conf` is the **shipped** layout, not the live one | The owner's desktop layout lives in `~/.config/gridsnap/zones.conf` (found first by `zone-tiler.default_conf`); the tracked file is only the starting layout for a new installation, and it ships through `Makefile`, `install.sh`, `PKGBUILD` and `debian/install`. Twice the live layout reached the tracked file by accident; the last time (`2c6a247`, dropped before push) it carried `[workspaces] 1 = ws1`, and `ws1.conf` lives in `templates/`, which is gitignored and shipped nowhere. `tests/test_zone_config.py::ShippedConfigTest` now refuses any `[workspaces]`/`[monitors]` binding in the shipped file. |
| Every smoke writes its own config | `smoke-arrange.sh` was the last scenario reading the root `zones.conf`; a `[workspaces]` entry in the live layout made the tiler resolve a profile the scenario's expectations did not read, and the smoke failed on a clean tree. All 13 scenarios now write a config into their own temp directory (`smoke.sh` still reads the root file by design — it is the only scenario that checks the shipped default layout, and it passes `--conf`-less on purpose). |
| `theme.conf` follows the resolved `zones.conf` | Resolved here to `~/.config/gridsnap/zones.conf`, so `~/.config/gridsnap/theme.conf` is the owner's live palette — never commit one. The editor's wallpaper action writes exactly that file (with a `# Generated by gridsnap …` first line); tests and smokes keep their themes in temp directories. The daemon rewrites a file only while that marker is its first line and only when `picture-uri` resolves to a different image; deleting the file or removing the line returns the palette to the user. |
| Wiki | The repository has **no GitHub wiki** (`has_wiki: false`, no `*.wiki.git`), and there is no wiki checkout on the machine. The living project notes are outside git, in `~/.pi/agent/memory/MEMORY.md` (+ `daily/`), indexed by `qmd`; the 2026-11-26 entry records the theme track and the Muffin border finding. Do not create a wiki in the repository without the owner's ask. |
| `~/.themes/Gridsnap` is the user's theme directory | The daemon rewrites it only when the files carry the generated marker (or the first version's `Name=Gridsnap` + `Comment=Gridsnap controls (…)` pair). Tests pass their own directory to `refresh_gtk_theme` and leave `Daemon.refresh_controls` unset — a plain `Daemon` must never write into the owner's home. Running GTK apps keep the old sheet until restart. |
| Window borders are not themable | `libmuffin` 6.4 never opens `metacity-1/`; its `meta_theme_new` is an allocator and a red metacity theme changes nothing. Do not build metacity themes — the decorations of server-side windows stay as Muffin draws them. The reachable surfaces are the GTK controls theme and the Cinnamon shell theme. |
| Real Cinnamon keybindings | `setup-hotkeys.py` talks to `gsettings`; tests must use a **keyfile backend sandbox** (`GSETTINGS_BACKEND=keyfile`, `XDG_CONFIG_HOME` inside a temp dir). One earlier session wrote probe keys into the owner's live session — restore by hand. |
| Display | Units with `env -u DISPLAY`; smoke with `dbus-run-session` and `env -u DISPLAY`; never place windows on `:0`. |
| Type checking is off on purpose | `pyrightconfig.json` keeps the server for navigation only; do not flip `typeCheckingMode` to `basic`/`strict` — `importlib`-loaded siblings and ctypes give hundreds of false errors |
| Rename blind spot | modules loaded via `importlib` (`zt.fail`, `tiler.*`, `zone_config_module()`) are invisible to `lsp rename`: it renamed `fail` in 67 places inside `zone-tiler.py` and missed 28 `zt.fail` in `zone_config.py`. Preview the edit count, then `grep` for `zt\.`/`tiler\.`/`zd\.` uses before applying; `make lint` (F821) catches a missed name at once |
| Muffin reparents clients | `SubstructureNotify` on root yields frame windows without `WM_CLASS`. The daemon listens for `_NET_CLIENT_LIST` changes and reconciles through `xprop`. |
| Translations and man pages are generated | After changing a source string: `make pot`; after changing a man page: `make man-pot`, then refresh `po/man/ru.po` and `zh_CN.po`. CI fails on a stale template. |
| Version is duplicated | `VERSION` in `zone-tiler.py`, `zone-daemon.py`, `zone-hotcorners.py`, `zone-tabstrip.py`, `gridsnap-ctl`, PKGBUILD, `man/gridsnap.1`, `debian/changelog` — `make check-version`. |
| Orca worker base branch | `worker-start --worktree new-child` branches from the project default (`main`), not from the coordinator's branch. Fix the base explicitly or cherry-pick the contract commit. |
| The README and site images are generated | `site/assets/editor.png` and `picker.png` come from `tools/shoot-screens.sh` (own Xvfb `:91`), `site/assets/demo.gif` from `tools/record-demo.sh` (own Xvfb `:90`, muffin, the daemon, five scene steps). Both build the palette with `tools/scene-palette.py`, so the stills and the recording share one look. Do not hand-edit or hand-save them: re-run the script, then `env -u DISPLAY python3 -m unittest tests.test_screenshots tests.test_demo_gif`. The scene pins the display size, so a shot from another display fails the size assertion instead of silently becoming a different picture. |
| A scene must pin the wallpaper setting | The daemon refreshes a marked `theme.conf` whose recorded source differs from the live `picture-uri`, so a scene that only writes the file gets the palette of the **owner's** wallpaper on the first 5 s poll. `tools/scene-palette.py --pin-wallpaper` points the setting at the drawn image; the callers do it inside a `GSETTINGS_BACKEND=keyfile` sandbox with a temporary `HOME`, so neither the setting nor `~/.themes/Gridsnap` is touched. |
| A background job started in `$(…)` is not the job `$!` names | `W1PID=$(open_window …)` runs the function in a subshell; the `xmessage` dies with it and `dash` reports a PID the process never had. `record-demo.sh` hands the PID out through `OPEN_PID` instead, and `tests/test_demo_gif.py` fails if a command substitution comes back. |
| The layout menus are pictures of the data, not icons | `zone_menu.thumb_rects` draws from the same zone fractions `templates()` and the profile files carry; a hand-drawn icon would be a second truth and the first edited template would leave it stale. The rows come from `zone_config.profiles_overview` — the editor never opens a profile file itself. |
| The loaded draft is marked until the first edit | `self.profile_source` is set in `load_profile` after `set_dirty(True)` (which clears it) and dropped by the next edit or monitor switch: a changed draft is nobody's profile. `Monitor's own set` is the only way back and drops that monitor's unsaved edits. |
| A menu row is a flat button with an explicit class | `ModelButton` centres its label and cannot hold a two-line box, so `.menu-row`/`.layout-row` are plain buttons. The danger row needs `button.menu-row.destructive-action` **after** `.menu-row` in the sheet, otherwise the later transparent rule wipes the danger colour — the first screenshot showed a black-on-dark row. |
| `[monitors]` names come back lowercase | `ConfigParser` lowercases option names, so `profiles_overview` returns `hdmi-1`; the editor restores the real spelling from `combo_entries` by `casefold`, and a profile bound to an absent output keeps the file's lowercase form instead of a guessed one. |
| Watchers | Every spawn gets `tools/worker-watch.sh`; every PR/run gets `tools/ci-watch.sh`. Waiting without one is a process error. |

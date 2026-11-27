# HANDOFF — current state of GridSnap

Entry point for a fresh session. Read this first, then `AGENTS.md`. The per-wave history lives in
`docs/HANDOFF-<date>-<slug>.md`; the newest are `docs/HANDOFF-2026-11-26-demo-scene.md`,
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
| Code | `main` = `fafac1d` (renamed the HANDOFF sections for the OAW quality gate; before it `76f88e7` — `docs/product.md`, `docs/platforms.md`, `docs/tutorial.md`; the theme waves: palette `8b61c61`, wallpaper `ea2dc1e`, refresh `32b6aa5`, man page `60f288d`, GTK controls `575d601`, docs `7f8201d`, screenshots `3d61d20`, demo scene `65ad946`, release `47f8003`; the AUR checksum pin `a3de4c2`) — pushed, CI green |
| Version | `0.3.0`, tag `v0.3.0` + GitHub Release with `gridsnap-0.3.0.tar.gz`; AUR `sha256sums` is the tag archive (`031c2e3…`), which is not the attached tarball: the tag archive is what `source` names |
| Repo | `metapeka/cinnamon-gridsnap` (`gh` is authed as `metapeka`) |
| Language | Everything in the repo is **English**; talk to the owner in Russian |
| Tests | `1518` unit tests, `OK` in ~18 s without X (9 skipped once `make man` has run, 15 in a fresh checkout) |
| Smoke | `13` scenarios under `tests/smoke*.sh`, each on its own Xvfb display |
| CI | Green on `main` (`fafac1d`, run `45981678719`), 5 jobs; `pages.yml` fires only on `site/**` changes and last deployed from `f674af0` (run `45800268713`), so the site lives at `metapeka.github.io/cinnamon-gridsnap` from that commit |
| Open PRs | None |
| Live install | This checkout **is** the installed copy. The owner's desktop layout lives in `~/.config/gridsnap/zones.conf` (profiles in `~/.config/gridsnap/templates/`), which `zone-tiler.default_conf` finds first; the tracked root `zones.conf` is the shipped starting layout |

### Uncommitted work in the tree (15 files, 2026-11-27)

The **neutral-palette + header/sidebar rework** of the editor: the owner rejected the
wallpaper-tinted chrome and the twelve-control toolbar. Gates are green (`make check` — 1518 tests,
`OK`, 9 skipped; `make lint`; `make check-i18n`; `tests.test_screenshots test_demo_gif
test_docs_languages test_site_pages` — 30 `OK`), the stills and the demo were re-rendered by the
scripts. **Not committed, no PR, nothing pushed.** These 15 files are the rework; `HANDOFF.md`
itself is the sixteenth modified file — this very handoff — and `.omp-handoff-request.json` is a
local, git-excluded request (`.git/info/exclude`), not part of the product.

- `zone-editor.py` (1743 lines) — header (title, monitor chip, Templates, Profiles, filled Save
  that is insensitive while nothing changed, and a “…” menu: gallery, theme group, place on another
  monitor, workspace/monitor profiles, hotkeys, place the active window, Clear all… with a
  confirmation); one sidebar (zone list with an identity dot, name and key, plus the inspector and a
  one-line geometry `N % × M % · X × Y px`); one-line status (the workspace note moved to the chip's
  tooltip); neutral canvas (`fg` 4 % fill, `border` outline, selection `accent` 10 % + 1.8 px);
  zone labels are name + key only; the empty-inspector hint is `set_no_show_all`-guarded so
  `show_all()` cannot resurrect it.
- `zone_theme.py` (909 lines) — neutral `DEFAULT_COLORS` (measured: fg 15.3:1, muted 6.6:1, accent
  3.7:1, white on accent 4.58:1), system `DEFAULT_ZONES` as identity hues, wallpaper palettes keep
  the neutral chrome and take only the accent, the GTK sheet grew header/status/row/suggested/
  destructive/entry-focus/popover rules, `contrast_text` threshold `0.18`, `.dim-label` inside a
  selected row follows the selection colour.
- `tests/test_theme.py` — contracts updated: neutral defaults, neutral wallpaper chrome, the
  samples-to-theme accent rule.
- `docs/specs/theme.md` — tokens, zone-hue derivation 0.56/0.58, the editor's landing table, the
  sheet contract, the wallpaper bridge, the contrast threshold.
- `docs/specs/zone-editor.md` — translated to English and rewritten for the new UI.
- `po/gridsnap.pot`, `po/ru.po`, `po/zh_CN.po`, `po/gridsnap@metapeka/gridsnap@metapeka.pot` — nine
  new strings plus `Layout` translated into `ru`/`zh_CN`, fuzzy matches cleared.
- `README.md`, `README.ru.md`, `README.zh.md` — §3.2 re-mapped to the new controls (chip, “…” menu,
  sidebar inspector, no more “−”/“▶” buttons).
- `site/assets/editor.png`, `site/assets/picker.png`, `site/assets/demo.gif` — re-rendered by
  `tools/shoot-screens.sh` and `tools/record-demo.sh` (neutral chrome, the scene's orange accent).

Live side, outside git: `~/.config/gridsnap/theme.conf` was regenerated as a marked neutral file
(`bg #1c1c1e`, `panel #232325`, accent `#3793be` from the wallpaper); the daemon was restarted on
the new code — pid `1064788`, registered as the `gridsnap-daemon` service, `0.3.0`, reading
`~/.config/gridsnap/zones.conf` (`gridsnap-ctl status` / `reload` confirmed).

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
| `zone_config.py` | 2202 | pure `zones.conf` model: load/save canonically, per-monitor and per-workspace sets, profiles; no GTK |
| `zone-editor.py` | 1743 | GTK zone editor: header (monitor chip, Templates, Profiles, Save, “…” menu), zone list + inspector sidebar, canvas, gallery, profile menus, hotkey registration, **Theme** menu (wallpaper palette, controls theme, reload) |
| `zone-hotcorners.py` | 1186 | drag watcher over XInput2 raw events → HUD → place on drop |
| `zone-picker.py` | 1003 | overlay zone picker / `--hud` passive grid |
| `window_backend.py` | 740 | the single seam to the window system (X11/EWMH today; Wayland later) |
| `zone_ipc.py` | 587 | JSON-line IPC framing, command dispatch, UNIX socket |
| `zone-tabstrip.py` | 532 | tab strip window for a zone (daemon spawns it) |
| `setup-hotkeys.py` | 376 | `gsettings` registration of Cinnamon custom keybindings |
| `zone_actions.py` | 348 | `[action:<name>]`: built-ins and external commands with `GRIDSNAP_*` |
| `zone_i18n.py` | 333 | gettext wrapper: source `en`, catalogs `ru`, `zh_CN` |
| `zone_theme.py` | 909 | one palette for the picker, the HUD, the tab strip and the editor: optional `theme.conf` next to `zones.conf`, five presets, a neutral chrome with one accent (a wallpaper chooses only the accent), zone-hue derivation as identity dots, the editor's GTK sheet, wallpaper sampling (GdkPixbuf), the generated GTK controls theme and its refresh (daemon poll and editor actions) |
| `zone_geom.py` | 333 | fractions ↔ pixels, frame extents, snapping helpers |
| `gridsnap-ctl` | 289 | CLI client for the daemon (also `watch`, `status`, `reload`) |
| `zone_tabs.py`, `zone_tabstrip_nav.py`, `zone_picker_nav.py`, `zone_units.py`, `zone_reflow.py`, `make-icon.py` | 88–225 each | pure helpers: tab layout, keyboard nav, unit parsing, reflow, icons |

Behaviour contracts live in `docs/specs/` (19 files, one per module feature). Architecture overview:
`docs/architecture.md`. Product scope and non-goals: `SPEC.md`. Plan: `docs/ROADMAP.md`.

## Next (open work)

| Item | State |
|---|---|
| Editor UI rework (uncommitted) | **Done, gates green, sitting in the tree** (15 files, list above in State). Next: commit it and open a PR (watch CI with `tools/ci-watch.sh`); then the last known UI gap — gallery data (`gallery/*.conf` `title`/`description`) is still Russian inside the English dialog, route it through `po` or ship English source values; if the palette changes again, re-run `tools/shoot-screens.sh` and `tools/record-demo.sh`, then `tests.test_screenshots tests.test_demo_gif`. |
| Wayland backend | Open. Cinnamon 6.8 / Mint 23 (~Christmas 2026) makes the Wayland session official. Reconstruction in `docs/HANDOFF-2026-11-22-window-seam.md`: placing windows through muffin still works under nested `muffin --wayland`, but the seam is not finished. |
| Debian archive | `.deb` builds, license declared, lintian clean; missing ITP bug, sponsor, review. |
| Docs in Russian | `SPEC.md`, `docs/**` (except `zone-i18n`/`man-i18n`), `README.ru.md`, site, `Makefile`, `install.sh`, `PKGBUILD`, `debian/**`, `tools/**`, `tests/smoke*.sh` were written before the language inversion. Translate them **as they are touched**, not in one sweep. Done so far: `docs/architecture.md` (English, with a Mermaid module graph), `docs/specs/theme.md`, `tools/shoot-screens.sh`, `tools/record-demo.sh`; the site and both translated READMEs carry §3.4 (theme). The rest is still Russian. |
| Owner actions after the last wave | Add the applet to the panel; after a Cinnamon restart re-run `python3 setup-hotkeys.py --remove` once (see `docs/HANDOFF-2026-11-22-events.md`). |
| Design track (43PR palette) | **Five code waves + a docs wave merged into `main`** (`8b61c61` palette, `ea2dc1e` wallpaper, `32b6aa5` refresh, `60f288d` man page, `575d601` GTK controls, `7f8201d` docs), not pushed. Optional `theme.conf`, five presets, zone-hue derivation, editor GTK sheet; **Theme** menu in the editor: palette from the wallpaper (GdkPixbuf sampling, no new dependency) and a generated GTK controls theme. A generated `theme.conf` follows `picture-uri` through the daemon (5 s poll, marker-only, `--once` excluded); a generated controls theme (`~/.themes/Gridsnap`) follows the palette from the same poll and from the editor's actions. `man gridsnap` documents both with `ru`/`zh_CN`; `docs/architecture.md` is English with a Mermaid module graph, `docs/specs/theme.md` has two Mermaid flows, README ru/zh carry §3.4, the site carries the feature and the spec link, ROADMAP item 5 is closed. Contracts `docs/specs/theme.md`; records `…-design-theme.md`, `…-theme-wallpaper.md`, `…-theme-refresh.md`, `…-man-theme.md`, `…-gtk-controls.md`, `…-docs.md`, `…-screenshots.md`, `…-demo-scene.md`. The README and site images are no longer hand-made: `tools/shoot-screens.sh` re-renders `site/assets/editor.png` and `picker.png` on its own Xvfb `:91`, `tools/record-demo.sh` re-records `site/assets/demo.gif` on its own Xvfb `:90` with muffin and the daemon; both build the palette with `tools/scene-palette.py`, held by `tests/test_screenshots.py` (7 tests) and `tests/test_demo_gif.py` (14 tests). Remaining out of scope: GTK 4/libadwaita, the Cinnamon shell theme, metacity borders (Muffin never reads them), Quickshell/QML. |

## Decisions (sharp edges)

| Trap | What to do |
|---|---|
| The editor's colour is identity, not decoration | The chrome is zinc-neutral (`bg #1c1c1e`, `panel #232325`, `card #2c2c2e`, `border #545456`) with one accent; a zone hue appears only as the list dot and in the picker/HUD, while the canvas paints zones `fg` 4 % and the selection `accent` 10 % + 1.8 px. A wallpaper palette keeps the neutral chrome and takes only the accent — the violet cast on every surface was the rejected look (`docs/specs/theme.md`). Text on an accent uses `contrast_text` threshold `0.18` (where white and black ratios meet, so the default blue keeps white at 4.58:1); a `.dim-label` inside a selected row must take the selection colour, or the key hint turns grey-on-blue. |
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
| Watchers | Every spawn gets `tools/worker-watch.sh`; every PR/run gets `tools/ci-watch.sh`. Waiting without one is a process error. |

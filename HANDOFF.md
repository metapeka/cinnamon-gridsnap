# HANDOFF — current state of GridSnap

Entry point for a fresh session. Read this first, then `AGENTS.md`. The per-wave history lives in
`docs/HANDOFF-<date>-<slug>.md`; the newest are `docs/HANDOFF-2026-11-26-screenshots.md`,
`docs/HANDOFF-2026-11-26-docs.md`, `docs/HANDOFF-2026-11-26-gtk-controls.md`,
`docs/HANDOFF-2026-11-26-man-theme.md`, `docs/HANDOFF-2026-11-26-theme-refresh.md` and
`docs/HANDOFF-2026-11-26-theme-wallpaper.md` — read
them only when you need the “why”.

## Snapshot

| What | Value |
|---|---|
| Code | `main` = `7f8201d` (theme waves: palette `8b61c61`, wallpaper `ea2dc1e`, refresh `32b6aa5`, man page `60f288d`, GTK controls `575d601`, docs `7f8201d`) plus the screenshots wave and their handoff records; ahead of `origin/main`, **not pushed** |
| Version | `0.2.0`, tag `v0.2.0` |
| Repo | `metapeka/cinnamon-gridsnap` (`gh` is authed as `metapeka`) |
| Language | Everything in the repo is **English**; talk to the owner in Russian |
| Tests | `1513` unit tests, `OK` in ~27 s without X (9 skipped once `make man` has run, 15 in a fresh checkout) |
| Smoke | `13` scenarios under `tests/smoke*.sh`, each on its own Xvfb display |
| CI | Green on `main` (run `45097865174`), 5 jobs |
| Open PRs | None |
| Live install | This checkout **is** the installed copy: `zones.conf` at the root is the owner's desktop layout |

## Gates

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

## Map

| Module | Lines | Role |
|---|---|---|
| `zone-tiler.py` | 4077 | the core: config → zone model → EWMH placement; `list/apply/arrange/stack/undo/focus/profile/tabs-next/action` |
| `zone-daemon.py` | 3300 | optional long-lived daemon: `[rules]` auto-placement, reflow, tabs, IPC server, event subscriptions, the wallpaper poll and the controls-theme follow |
| `zone_config.py` | 2202 | pure `zones.conf` model: load/save canonically, per-monitor and per-workspace sets, profiles; no GTK |
| `zone-editor.py` | 1657 | GTK zone editor, gallery, profile menu, hotkey registration button, **Theme** menu (wallpaper palette, controls theme, reload) |
| `zone-hotcorners.py` | 1186 | drag watcher over XInput2 raw events → HUD → place on drop |
| `zone-picker.py` | 1003 | overlay zone picker / `--hud` passive grid |
| `window_backend.py` | 740 | the single seam to the window system (X11/EWMH today; Wayland later) |
| `zone_ipc.py` | 587 | JSON-line IPC framing, command dispatch, UNIX socket |
| `zone-tabstrip.py` | 532 | tab strip window for a zone (daemon spawns it) |
| `setup-hotkeys.py` | 376 | `gsettings` registration of Cinnamon custom keybindings |
| `zone_actions.py` | 348 | `[action:<name>]`: built-ins and external commands with `GRIDSNAP_*` |
| `zone_i18n.py` | 333 | gettext wrapper: source `en`, catalogs `ru`, `zh_CN` |
| `zone_theme.py` | 872 | one palette for the picker, the HUD, the tab strip and the editor: optional `theme.conf` next to `zones.conf`, five presets, zone-hue derivation, the editor's GTK sheet, wallpaper sampling (GdkPixbuf), the generated GTK controls theme and its refresh (daemon poll and editor actions) |
| `zone_geom.py` | 333 | fractions ↔ pixels, frame extents, snapping helpers |
| `gridsnap-ctl` | 289 | CLI client for the daemon (also `watch`, `status`, `reload`) |
| `zone_tabs.py`, `zone_tabstrip_nav.py`, `zone_picker_nav.py`, `zone_units.py`, `zone_reflow.py`, `make-icon.py` | 88–225 each | pure helpers: tab layout, keyboard nav, unit parsing, reflow, icons |

Behaviour contracts live in `docs/specs/` (18 files, one per module feature). Architecture overview:
`docs/architecture.md`. Product scope and non-goals: `SPEC.md`. Plan: `docs/ROADMAP.md`.

## Open work

| Item | State |
|---|---|
| Wayland backend | Open. Cinnamon 6.8 / Mint 23 (~Christmas 2026) makes the Wayland session official. Reconstruction in `docs/HANDOFF-2026-11-22-window-seam.md`: placing windows through muffin still works under nested `muffin --wayland`, but the seam is not finished. |
| Debian archive | `.deb` builds, license declared, lintian clean; missing ITP bug, sponsor, review. |
| Docs in Russian | `SPEC.md`, `docs/**` (except `zone-i18n`/`man-i18n`), `README.ru.md`, site, `Makefile`, `install.sh`, `PKGBUILD`, `debian/**`, `tools/**`, `tests/smoke*.sh` were written before the language inversion. Translate them **as they are touched**, not in one sweep. Done so far: `docs/architecture.md` (English, with a Mermaid module graph), `docs/specs/theme.md`; the site and both translated READMEs carry §3.4 (theme). The rest is still Russian. |
| Owner actions after the last wave | Add the applet to the panel; after a Cinnamon restart re-run `python3 setup-hotkeys.py --remove` once (see `docs/HANDOFF-2026-11-22-events.md`). |
| Design track (43PR palette) | **Five code waves + a docs wave merged into `main`** (`8b61c61` palette, `ea2dc1e` wallpaper, `32b6aa5` refresh, `60f288d` man page, `575d601` GTK controls, `7f8201d` docs), not pushed. Optional `theme.conf`, five presets, zone-hue derivation, editor GTK sheet; **Theme** menu in the editor: palette from the wallpaper (GdkPixbuf sampling, no new dependency) and a generated GTK controls theme. A generated `theme.conf` follows `picture-uri` through the daemon (5 s poll, marker-only, `--once` excluded); a generated controls theme (`~/.themes/Gridsnap`) follows the palette from the same poll and from the editor's actions. `man gridsnap` documents both with `ru`/`zh_CN`; `docs/architecture.md` is English with a Mermaid module graph, `docs/specs/theme.md` has two Mermaid flows, README ru/zh carry §3.4, the site carries the feature and the spec link, ROADMAP item 5 is closed. Contracts `docs/specs/theme.md`; records `…-design-theme.md`, `…-theme-wallpaper.md`, `…-theme-refresh.md`, `…-man-theme.md`, `…-gtk-controls.md`, `…-docs.md`, `…-screenshots.md`. The README and site images are no longer hand-made: `tools/shoot-screens.sh` re-renders `site/assets/editor.png` and `picker.png` on its own Xvfb `:91` from a wallpaper-sampled palette, `tests/test_screenshots.py` holds the pair (7 tests). Remaining out of scope: GTK 4/libadwaita, the Cinnamon shell theme, metacity borders (Muffin never reads them), Quickshell/QML. |

## Sharp edges

| Trap | What to do |
|---|---|
| The root `zones.conf` is the live desktop layout | Never “fix” it as test data and never commit it without the owner's ask. Every test and smoke uses its own `--conf` in a temp dir. `smoke-dpi.sh`, `smoke-picker.sh` and `smoke-hotcorners.sh` used to read the root file; a `[workspaces]` entry in the live layout then made the tiler resolve a profile the scenario's own expectations did not read, and all three failed on a clean tree. They now write their own config. |
| `theme.conf` follows the resolved `zones.conf` | In this checkout the resolved config is the root `zones.conf`, so a `theme.conf` at the root would be the owner's live palette — never commit one without asking. The editor's wallpaper action writes exactly that file (with a `# Generated by gridsnap …` first line); tests and smokes keep their themes in temp directories. The daemon rewrites a file only while that marker is its first line and only when `picture-uri` resolves to a different image; deleting the file or removing the line returns the palette to the user. |
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
| The README and site images are generated | `site/assets/editor.png` and `picker.png` come from `tools/shoot-screens.sh` (own Xvfb `:91`, wallpaper-sampled palette, English scene). Do not hand-edit or hand-save them: re-run the script, then `env -u DISPLAY python3 -m unittest tests.test_screenshots`. The scene pins the display size, so a shot from another display fails the size assertion instead of silently becoming a different picture. |
| Watchers | Every spawn gets `tools/worker-watch.sh`; every PR/run gets `tools/ci-watch.sh`. Waiting without one is a process error. |

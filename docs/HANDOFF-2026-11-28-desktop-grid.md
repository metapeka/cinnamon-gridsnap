# Wave: the grid on the live desktop

The owner asked for the zone grid on the real screen — not only on the editor canvas and not only
while the picker is open — to see where the windows are and how much of the screen a zone takes. The
pieces existed but not the mode: the HUD is the same grid, but the hot corners own it and it lives
only for the length of a drag; the picker covers the screen with a 90 % backdrop; the editor draws
the grid in its own window. What was missing was a passive grid that stays on the desktop, keeps the
windows visible, and can be switched on and off.

| Where | Change |
|---|---|
| `zone-picker.py` | `--grid` — a passive grid over the live desktop: one `Hud` window per monitor with `style="grid"`, a new `draw_grid` (neutral frame `fg` 0.55 over a `bg` 0.45 under-stroke, a rounded `bg` 0.66 plate behind the name/size/key, a 7 px hue dot, the zone under the pointer in `accent` 0.08 + 2.2 px), the pointer polled every `GRID_TICK_MS` (66 ms) and the same physical point sent to every window; `--grid --shot` writes a transparent PNG; `--toggle` starts or stops the grid of the current desktop through a per-desktop pid file (`$XDG_RUNTIME_DIR/gridsnap/grid-<desktop>.pid`), trusting a pid only when its command line names `zone-picker` and `--grid`; without a compositor `--grid` refuses with code 1 before creating a window |
| `zone_config.py` | `OVERLAY_SECTION` (`[overlay]`), `DEFAULT_OVERLAY_KEY` (`<Super>g`), `load_overlay_key` — the default without the section, an empty `key` turns the shortcut off, an unknown key is a config error (code 3) |
| `setup-hotkeys.py` | the plan gains `("desktop grid", <key>, "python3 …/zone-picker.py --toggle --quiet")`, installed under the common `zone-tiler: ` prefix so `--remove` and a reinstall own it |
| `zone-editor.py` | **… → Placement → Show the zones on the desktop** runs the same toggle with the editor's config (absolute path); with unsaved edits the status says the grid shows the saved layout |
| `tests/test_overlay_config.py` | 19 tests: the `[overlay]` section (default, own key, empty, typo, not reserved), the per-desktop pid file, a stale and a foreign pid, the detached command, a failed start, and the mode combinations (`--grid` with a window/point, `--toggle` with a shot) |
| `tests/smoke-grid.sh` | a 14th scenario: its own Xvfb `:96`, `picom` as the compositor, a white window as the desktop; the refusal without a compositor, transparency (the window shows through), the frame line, the pointer highlight (tint moves, the other zones do not), the transparent still, and `--toggle` on/off with window, pid file and process |
| `.github/workflows/ci.yml` | `picom` in the smoke job's packages and a step for the new scenario |
| `docs/specs/zone-picker.md` | rewritten in English (the inversion rule for a touched document) and extended with "Desktop grid (`--grid`) and its toggle" |
| `docs/specs/hotkeys.md`, `docs/specs/zone_config.md` | both translated into English and extended: the grid pair in the plan, `[overlay]`/`load_overlay_key`, the new test file |
| `docs/specs/zone-editor.md` | the new **…** menu row and the saved-layout status |
| `man/gridsnap.1` + `po/man/{ru,zh_CN}.po` | `--grid`/`--toggle` in the synopsis and options, `[overlay]` in CONFIGURATION, the grid in HOTKEYS, an example; translated |
| `README.md`, `README.ru.md`, `README.zh.md` | §8 gains "the same grid on the live desktop: `Super+g`" (what it shows, the three commands, `[overlay]`, the editor row, the gTile collision on `Super+G`) |
| `po/{ru,zh_CN}.po` | 14 new strings for the picker, the config loader and the editor |

## Decisions

1. **A new mode, not the HUD.** The HUD's job is the drag: a quarter backdrop over the whole screen
   and a point stream from the watcher. The desktop grid must keep the windows readable and works with
   nobody feeding it, so it polls the pointer itself and paints no screen-wide wash.
2. **The frame carries its own contrast.** A light hairline disappears on a bright window and a dark
   one on a dark terminal, so every frame is a `bg` 0.45 under-stroke with an `fg` 0.55 line on top;
   the labels sit on a `bg` 0.66 plate, because text over arbitrary windows cannot rely on the
   window's colour.
3. **A compositor is a requirement, not a nicety.** Without one the grid window has no alpha visual,
   the transparent clear turns opaque, and a persistent window that blacks out the desktop is worse
   than no grid: `--grid` refuses with code 1 and names the compositor. Cinnamon always composites.
4. **One pid file per desktop.** The grid of the desktop is what is shown; a toggle elsewhere must
   neither kill nor hide it. A pid is trusted only when `/proc/<pid>/cmdline` names `zone-picker` and
   `--grid`, so a stale file or a recycled pid cannot kill an unrelated process.
5. **The toggle lives in the picker, not in a new binary.** The grid is the picker's fourth mode; a
   separate `gridsnap-grid` script would need its own entry in four packaging lists, the man page and
   the CI, for one `os.kill`.
6. **`[overlay]`, not `[grid]`.** `[grid]` is the editor's reserved section (`cols`/`rows`); a new
   optional section follows `[focus]`/`[tabs]` and is carried verbatim by the editor.
7. **The default key is `<Super>g`, with the collision named.** The picker's own `Super+Z` is set by
   hand; the grid ships with a key so it works out of the box. gTile (README §2) also takes
   `Super+G` — the README says to change `[overlay] key` when both are installed.

## Verification

```
$ make check
Ran 1556 tests in 18.170s

OK (skipped=9)

$ make lint
All checks passed!

$ make check-i18n
msgfmt ru
msgfmt zh_CN
собраны каталоги в locale/
каталоги в порядке

$ make check-man-i18n
Ran 14 tests in 0.922s

OK
man-страницы в порядке
```

Every smoke scenario, one by one (`env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh <s>`):

```
tests/smoke-actions.sh rc=0          tests/smoke-monitor-profiles.sh rc=0
tests/smoke-arrange.sh rc=0          tests/smoke-monitors.sh rc=0
tests/smoke-dpi.sh rc=0              tests/smoke-picker-workspace.sh rc=0
tests/smoke-events.sh rc=0           tests/smoke-picker.sh rc=0
tests/smoke-focus.sh rc=0            tests/smoke-tabs.sh rc=0
tests/smoke-grid.sh rc=0             tests/smoke.sh rc=0
tests/smoke-hotcorners.sh rc=0       tests/smoke-ignore.sh rc=0
```

## What was looked at

On a private Xvfb (`:93`) with `picom --backend xrender` and the scene wallpaper: the grid with the
pointer in `top-right` — frames and plates readable over a dark and over a bright wallpaper, the
accent mark on the zone under the pointer and nowhere else, the wallpaper showing through between the
frames; the pixel samples that the smoke automates (`(300,500)` equals the window colour until the
pointer enters `main`, then turns blue on the default palette; `(2,500)` is the frame line). The
screenshot of the bright case is the one that made the under-stroke necessary: a bare `fg` 0.55
hairline vanished on the cream background.

## Leftovers

- **A desktop switch hides the grid.** The window is not sticky: switching away leaves it on its own
  desktop, and the toggle on the new desktop shows that desktop's grid. Following a switch would mean
  re-resolving the workspace profile (and the zone set) while the window is up; recorded, not done.
- **The grid reads the saved config once.** Editing zones needs a toggle; the editor's row says so
  when the canvas is dirty.
- **Wayland is not measured.** Under Cinnamon's Wayland session the grid should behave like the HUD
  (Xwayland, EWMH), but only X11 was run; the compositor check asks GDK, which answers honestly on
  both. [INFERENCE]
- **The overlay over fullscreen windows** (`keep_above` + `DOCK`) is not tested: the grid is meant
  for arranging windows, not for living above a video.

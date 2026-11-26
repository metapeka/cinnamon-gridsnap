# Wave: the design track — one palette for every surface (43PR reference)

Branch: `design/43pr-theme`, worktree `../gridsnap-design`, based on the frozen `main` (`5031f29`,
plus the doc commit `053c55f`). The reference is [43PR/dotfiles](https://github.com/43PR/dotfiles)
(MIT) — Hyprland + Quickshell; only its palettes and token names are borrowed.

## Why this wave exists

The owner wanted the look of that setup on Cinnamon. The honest answer is that the shell cannot be
ported: the bar, the side panel, the power menu and the OSDs are Quickshell widgets that need a
Wayland compositor with `wlr-layer-shell` and Hyprland's IPC. Cinnamon runs Muffin on X11 with its
own panel and notification daemon, so those files have no host. What does transfer is the palette
language: semantic tokens, a monochrome chrome with one accent, a family for the captions, a
corner radius. GridSnap already drew four surfaces of its own with hard-coded colours, and this
wave gives them a shared, optional theme.

## Freeze

`main` at `5031f29` (2026-11-26, 114 commits after `v0.2.0`) is the base. Gates on it: `make check`
— 1431 tests `OK` (15 skipped), `make lint` — clean. The live install is the root checkout and stays
there; this wave lives in a separate worktree so the owner's desktop keeps running the frozen code.
`zones.conf` in the root checkout was dirty with the owner's layout and was not committed.

## What shipped

| File | Change |
|---|---|
| `zone_theme.py` (new, 443 lines) | theme model, INI loading, validation, presets, zone derivation, the GTK sheet |
| `zone-picker.py` | overlay and HUD paint from `THEME` (`bg`, `accent`, `zones`, `radius`, `font`) |
| `zone-tabstrip.py` | strip, tabs, captions and accent stripe paint from `THEME` |
| `zone-editor.py` | canvas, labels, swatches and the GTK chrome paint from `THEME`; `apply_theme` / `apply_theme_tree` |
| `zone-hotcorners.py` | no change — its HUD is `zone-picker.py --hud --conf`, so it inherits the theme |
| `tests/test_theme.py` (new, 233 lines) | defaults, loading, presets, every fallback, derivation, the sheet |
| `Makefile`, `install.sh`, `debian/install`, `PKGBUILD` | `zone_theme.py` in every distribution list |
| `README.md` §3.4 | `theme.conf` for users: tokens, presets, what changes and what does not |
| `docs/specs/theme.md` | the contract |
| `po/gridsnap.pot` | the new `zone-theme:` warnings |

## Decisions, including two deviations from the first sketch

1. **INI `theme.conf`, not TOML `theme.toml`.** `tomllib` is Python 3.11; `ruff.toml` targets
   `py310` and Cinnamon ships on distributions with 3.10 (Mint 21). An import error here would take
   the whole picker down, not just the theme, and the project promises "Python 3 + GTK" without a
   floor above that. The format is the same family as `zones.conf`.
2. **Presets are a dict in the module, not files under `themes/`.** A directory would need a lookup
   path, four packaging lists and a gallery-like test; a dict is one entry to add. The values are
   transcribed verbatim from 43PR's `themes/*.toml`, so a third-party palette can still be copied
   into `theme.conf` by hand (the token names match, unknown keys are ignored).
3. **The default palette reproduces the old constants exactly** for the picker, the HUD and the
   editor, so a frozen installation is not quietly restyled; `tests/test_theme.py` pins the three
   values that decide a surface (backdrop, accent, first zone hue). The tab strip is the one
   exception: it now shares `bg_panel`/`bg_card` with the editor and moves one step darker —
   `#21242b` → `#1c1f24` for the strip, `#333842` → `#2b2e36` for a tab; the active tab and the
   captions stay as they were.
4. **Zone hues are derived from the accent when the theme names none.** 43PR's palettes are
   monochrome; eight grey zones would make the grid unreadable, so a low-saturation accent starts
   the hue ring at 210°.
5. **The GTK sheet is scoped to the editor window.** It is not a GTK theme: the panel, the menu,
   the window decorations and the notification daemon belong to Cinnamon, which has no blur and no
   per-window rounding to match the reference.
6. **Not ported, on purpose:** the QML shell (above), a Cinnamon/GTK theme, `matugen` and 43PR's
   template fan-out to other programs. Those theme the desktop, not GridSnap; `matugen` can later
   write `theme.conf` without changing this contract.

## Two GTK facts worth remembering

* A provider added with `Gtk.StyleContext.add_provider(widget_context, ...)` styles **that widget
  only** — it does not cascade to children. The real window therefore takes the sheet on its
  screen; the `--shot` path, a `Gtk.OffscreenWindow` that is not on any screen, gets it on a walk of
  the tree. Without that, `--shot` shows unthemed chrome and proves nothing.
* Adwaita's rules win on specificity: type selectors such as `toolbar`/`list`/`entry` were not
  enough for the in-app sheet in one place and had to clear `background-image` — the theme paints
  gradients there, and a background colour alone stays invisible under them. The classes that
  mattered were `.toolbar`, `list`, `row:selected`, `button.flat`/`button.image-button`.

## Verification (raw results)

```
$ make check
Ran 1456 tests in 16.755s
OK (skipped=15)

$ make lint
All checks passed!

$ make pot && make check-i18n
обновлены po/gridsnap.pot и po/gridsnap@metapeka/gridsnap@metapeka.pot
каталоги в порядке

$ env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh tests/smoke-picker.sh
tests/smoke-picker.sh rc=0
$ ... tests/smoke-tabs.sh      -> rc=0
$ ... tests/smoke-hotcorners.sh -> rc=0
```

Visual proof without a session (Xvfb, a three-zone config, `theme.conf` with `preset = 43pr`):

* `zone-picker.py --conf … --shot` — default mean RGB `(61, 63, 60)`, 1010 colours; 43pr mean
  `(51, 50, 62)`, 733 colours — the monochrome chrome and derived zone hues are visibly different.
* `zone-editor.py --conf … --shot` — 43pr: black window, `#050505` toolbar, `#0d0d0d` entries and
  tab list, white selected row; the default shot keeps the old canvas and chrome.
* `zone-tabstrip.py` fed one snapshot — default: active tab `(82, 92, 112)` = `#525c70`, inactive
  `(43, 46, 54)` = `#2b2e36`; 43pr: active `(36, 40, 50)` = `#242832`, inactive `(13, 13, 13)` =
  `#0d0d0d`, with the white accent stripe on the active tab.

The editor shot also printed `Gdk-CRITICAL … gdk_window_move_resize_internal` — pre-existing noise of
the offscreen path, not introduced here.

## Next

1. Review the branch and try it on the owner's layout:
   `python3 zone-editor.py --conf zones.conf` with a `theme.conf` next to it.
2. Merge after review (`main` stays the frozen base until then; one PR per wave).
3. Later waves: `matugen` → `theme.conf` written from the wallpaper; a docs-site page with the
   presets rendered as swatches.

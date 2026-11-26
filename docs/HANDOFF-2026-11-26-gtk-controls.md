# Wave: the generated GTK controls theme follows the palette

Closes the optional item of `docs/HANDOFF-2026-11-26-man-theme.md`: `~/.themes/Gridsnap` was
written once by the editor's **Theme → Window controls…** action and then went stale as soon as
`theme.conf` or the wallpaper changed. Wayland and the Cinnamon shell stay deferred.

## What shipped

| Where | Change |
|---|---|
| `zone_theme.py` | `GTK_THEME_DIR`, `GTK_NAME`, `GTK_COMMENT`, `GTK_MARK`, `GTK_CSS_MARK`; `_gtk_theme_files(theme)` renders both files; `write_gtk_theme` uses it and marks the files; `generated_gtk_theme(directory)` claims a directory (marked, or the unmarked first-version pair); `refresh_gtk_theme(conf_path, directory, theme)` rewrites only when the text differs; GTK 4/libadwaita documented as out of scope |
| `zone-daemon.py` | `Daemon(refresh_controls=…)` (off unless wired); `poll_theme` runs `_refresh_theme_file` then `_refresh_controls_theme`, each with a once-per-message log in `_theme_errors`; a palette rewrite still restarts the tab strip, a controls rewrite does not; `main` wires `zone_theme.refresh_gtk_theme(conf_path)` |
| `zone-editor.py` | `follow_controls_theme()` after **From the wallpaper…** and **Reload theme.conf**; the **Window controls…** action uses `GTK_THEME_DIR` |
| `tests/test_theme.py` (64 tests in the file) | markers; owner test (marked, legacy unmarked, a user theme, a name without the comment, absent); refresh on a new palette, no write for the same palette or a user theme, palette from `theme.conf` |
| `tests/test_daemon.py` | palette before controls; controls log; a controls rewrite does not restart the strip; separate once-per-message errors that do not hide each other |
| `docs/specs/theme.md` | “A generated theme follows the palette”, the GTK 3 restart caveat, GTK 4 out of scope, verification list |
| `README.md`, `docs/specs/daemon.md` | the auto-follow paragraph; the loop bullet |
| `man/gridsnap.1` | the controls-theme paragraph gained the marker/refresh clause; `~/.themes/Gridsnap` added to FILES |
| `po/*.pot`, `po/ru.po`, `po/zh_CN.po`, `po/man/*` | 3 new strings, one changed man entry, three `man` entries (changed paragraph, new FILES path and its text) |

## Decisions

1. **The marker is the only claim.** Both files start with a generated line; the first version's
   unmarked `Name=Gridsnap` + `Comment=Gridsnap controls (…)` pair is adopted so an upgrade needs no
   extra click. Anything else at that path belongs to the user and is never rewritten — the same
   rule as for `theme.conf`.
2. **The palette, not the wallpaper, is the source.** The editor's action always wrote the *current*
   palette, and `refresh_gtk_theme` does the same: it takes the passed theme, else `theme.conf`
   next to the config, else the defaults. So a hand-written palette also propagates, and deleting
   `theme.conf` returns the controls theme to the built-in colours instead of freezing a stale one.
3. **Refreshed every tick, written only on a difference.** The daemon compares the rendered text
   with the files, so an idle poll does not churn mtimes; the unit test pins the mtimes when the
   palette is unchanged.
4. **Order: palette, then controls.** The controls theme reads `theme.conf`; writing it first makes
   one tick carry one look (the e2e log shows the order).
5. **The strip restarts only for `theme.conf`.** GTK applications take a changed controls sheet on
   restart; nothing in GridSnap's own processes but the strip depends on it, and a strip restart
   for a controls-only change would move windows for no reason.
6. **Explicit wiring for the user's home.** `Daemon.refresh_controls` is `None` unless `main` passes
   it: a plain `Daemon` (tests, utility runs) must never rewrite `~/.themes/Gridsnap`. The default
   for `refresh_theme` stays because it only ever touches the config directory.
7. **GTK 3 only, and not live.** GTK 3 does not watch its theme files; running applications keep the
   old sheet until restart or a theme re-selection. GTK 4/libadwaita ships its own style and ignores
   a theme's `gtk-4.0` sheet for its widgets, so an overlay there would be a different product.

## Verification (raw results)

```
$ make check
Ran 1506 tests in 18.6s
OK (skipped=9)
$ make lint
All checks passed!
$ make check-i18n
каталоги в порядке
$ make check-man-i18n
Ran 14 tests in 0.937s
OK
man-страницы в порядке
```

`skipped=9` (was 15) because `make check-man-i18n` now leaves `man/build/` in place and the
built-page checks run instead of skipping. `make man-pot` rerun is byte-stable.

End to end on a private `Xvfb :94` and a temp `HOME`, with a generated `theme.conf` naming a
nonexistent old image and a generated controls theme carrying the old palette:

```
the daemon listens on X11 (root 0x46f, reflow=off)
_NET_CLIENT_LIST is unavailable - skipping reconciliation
theme.conf rebuilt from the wallpaper: /tmp/…/theme.conf
controls theme rebuilt from the palette: /tmp/…/.themes/Gridsnap
```

The CSS then reads `/* Generated by gridsnap from "wallpaper-1280x1024"; … */` — the sampled
palette, not the old one — and the next tick (t ≈ 10 s, inside the same 12 s run) logged nothing:
no rewrite without a change.

## Next

1. GTK 4/libadwaita and the Cinnamon shell theme remain out of scope (different products; no
   libadwaita recolouring short of its own `@define-color` API).
2. The Chinese interface and man page are still machine-translated; a native proofread is open.

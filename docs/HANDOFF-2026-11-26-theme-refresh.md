# Wave: a generated `theme.conf` follows the wallpaper

Continues `docs/HANDOFF-2026-11-26-theme-wallpaper.md`. The editor could sample the wallpaper and
write a marked `theme.conf`; nothing refreshed it when the picture changed. This wave closes that
gap in the daemon and fixes the descriptor shape the live Cinnamon setting actually uses.

## What shipped

| Where | Change |
|---|---|
| `zone_theme.py` (791 lines) | `generated_source(path)` parses the marker (``None`` — not ours, `""` — generated without a source); `refresh_from_wallpaper(conf_path, path, wallpaper)` rewrites only a generated file whose recorded image differs from the resolved one; `GENERATED_SUFFIX` is one constant for the header and the parser; `_background_image` reads Debian desktop-base's `<file><size>path</size></file>` as well as GNOME's `<file>path</file>` |
| `zone-daemon.py` (3255 lines) | `THEME_POLL_SECONDS` (5 s); `Daemon(refresh_theme=…, theme_interval=…)`; `poll_theme(now=None)` with the throttle, the log-once error and the strip restart; `serve()` waits at most `theme_interval` when it is not `--once` and calls the poll every pass; `main` passes `theme_interval=None` for `--once` |
| `tests/test_theme.py` (54 tests in the file) | the nested-size descriptor; `generated_source` on generated/source-less/hand-written/missing files; the refresh on a new image, the same image, a `file://` URI of it, a hand-written file, no wallpaper, an unreadable image |
| `tests/test_daemon.py` | off without an interval, throttled by it, the strip restart after a rewrite, one log line per distinct failure, `serve()` waking up for the poll and never polling in `--once` |
| `docs/specs/theme.md` | “Following the wallpaper” (the rule, the poll, `--once`, the strip), the resolver now documents both descriptor shapes, the out-of-scope item removed, the verification list |
| `docs/specs/daemon.md`, `README.md` | the loop’s bounded wait and the poll; the README paragraph about the running daemon |
| `po/*.pot`, `po/ru.po`, `po/zh_CN.po` | 2 new strings |

No new flag, no new binary, no man-page entry: `--help` is unchanged, so the man pages and their
translations stay untouched.

## Decisions

1. **Poll, not a `GSettings` signal.** The daemon's loop is `select()` over the X connection, the
   IPC socket and the subscriber descriptors; it has no GLib main loop, and `Gio.Settings` change
   signals are only dispatched by one. A 5 s bounded `select` costs one `readline` of `theme.conf`;
   the setting is touched only after the first line proves the file generated.
2. **The marker is the ownership test.** A hand-written `theme.conf` is never rewritten, whatever
   the wallpaper is; a generated file the user edited keeps its edits because the recorded image
   still matches. Deleting the file stays the undo. `--once` is a probe and does not write.
3. **`os.path.abspath` on both sides of the comparison.** The editor writes the resolved path, so a
   `file://` setting from the same image compares equal and costs no sampling.
4. **The error is logged once per distinct message.** A verbose daemon would otherwise repeat
   “cannot read the image” every five seconds while the picture is broken.
5. **The strip restarts only if it was running.** On a rewrite the palette must reach the one
   long-lived surface; a strip that never started has nothing to repaint, and rebuilding an empty
   snapshot would read `_NET_ACTIVE_WINDOW` for nothing.
6. **The resolver learned the real descriptor.** `picture-uri` on the live Cinnamon session points
   at `desktop-base`'s `gnome-background.xml`, whose paths are the text of `<size>` children — the
   old `<file>`-only parser returned `None`, so the editor's action would have said “no wallpaper”.
   Its SVG loads through the GdkPixbuf SVG loader (verified on this machine).

## Verification (raw results)

```
$ make check
Ran 1491 tests in ~17 s
OK (skipped=15)
$ make lint
All checks passed!
$ make check-i18n
каталоги в порядке
```

End to end with the live setting, in a temp config directory (`env -u DISPLAY`):

```
setting resolves to: /usr/share/desktop-base/homeworld-theme/wallpaper/contents/images/1280x1024.svg
first refresh: /tmp/tmpb035ttju/theme.conf
marker now: /usr/share/desktop-base/homeworld-theme/wallpaper/contents/images/1280x1024.svg
palette colors: True
second refresh: None mtime unchanged: True
hand-written refresh: None
hand-written kept: True
```

The daemon loop on a private `Xvfb :92`, `zone-daemon.py --conf <temp> --no-socket --verbose`,
a generated `theme.conf` naming a nonexistent old image:

```
the daemon listens on X11 (root 0x46f, reflow=off)
_NET_CLIENT_LIST is unavailable - skipping reconciliation
theme.conf rebuilt from the wallpaper: /tmp/tmp.VBXNWYwPGE/theme.conf
```

`dbus-run-session -- sh tests/run-smoke.sh tests/smoke-tabs.sh` — passed (tabs, focus, strip
removal).

## Next

1. Man page: `theme.conf` is still documented in `README.md` and `docs/specs/theme.md` only; a
   `CONFIGURATION` subsection means new po4a entries and two translations — the next man wave.
2. Optional: the daemon could rewrite the generated GTK controls theme (`~/.themes/Gridsnap`) too;
   it is not switched by the daemon today, and the user's choice in Cinnamon Settings must stay
   theirs.

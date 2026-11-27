# Platforms: Cinnamon X11, Cinnamon Wayland, Hyprland

GridSnap moves **other applications' windows**. That single requirement decides everything below:
it needs a window layer that lets a normal client list, move, resize and focus windows.

| Column | How it is known |
|---|---|
| Cinnamon X11 | The shipped target; all gates pass (`make check`, 13 smokes) |
| Cinnamon Wayland (muffin) | **Measured** in a nested `muffin --wayland` session — `docs/RESEARCH-wayland-recon.md:44-120` (22.11.2026) |
| Hyprland | **Not measured in this project**; estimated from its documented IPC (`hyprctl`, event socket). Cells marked `[INFERENCE]` |

Legend: ✅ works without changing the model · ⚠️ same model, but a new backend or patches are needed ·
❌ unreachable.

## Why this is not a switch

- The product talks EWMH: `_NET_CLIENT_LIST`, `_NET_MOVERESIZE_WINDOW`, `_NET_CURRENT_DESKTOP`,
  `_NET_WORKAREA`, `_NET_FRAME_EXTENTS`, `_NET_WM_STATE_MAXIMIZED_*`, `_NET_ACTIVE_WINDOW`.
- On Wayland a normal client cannot move another client's window: placement belongs to the
  compositor, and there is no cross-compositor protocol for it. Compositors that offer control do it
  through their **own** IPC (`niri msg`, `hyprctl`, `swaymsg`); muffin offers none.
- So a “Wayland backend” is a **separate layer per compositor** (or a compositor extension), not a
  flag. Hence the plan: one seam first (`window_backend.py` — every X11/EWMH call in one place), then
  measurement, then compatibility.

Measured under nested muffin-Wayland: `_NET_SUPPORTED` is complete, so for **Xwayland** windows the
current path works — `apply`/`--verify`, maximize and restore, `_NET_WM_DESKTOP`, active window,
client list, frame extents, work area, monitors via xrandr, a DOCK-type overlay above clients, and
`XGrabKeyboard`. About 90 % of the code runs unchanged; three fixes were needed and shipped in 0.2.0
(`5c8d79c`).

## Window placement

| Function (0.3.0) | Cinnamon X11 | Cinnamon Wayland | Hyprland |
|---|---|---|---|
| Zone editor: drag/resize, snapping, templates, names and keys, Profiles menu, Theme menu (`zone-editor.py`) | ✅ | ✅ GTK, independent of the compositor | ✅ |
| `list` / `apply <zone>` with `--dry-run`/`--verify` | ✅ | ✅ Xwayland windows (`_NET_MOVERESIZE_WINDOW` measured) | ⚠️ `dispatch movewindowpixel` / `resizewindowpixel` |
| Pixel-exact frame plus `_NET_FRAME_EXTENTS` compensation | ✅ | ✅ for Xwayland (measured) | ⚠️ otherwise: the compositor draws the frame, there are no frame extents; geometry comes from `hyprctl clients -j` `[INFERENCE]` |
| Profiles: `save/use/rm/export/import` JSON, `templates/` | ✅ | ✅ | ✅ own file |
| Monitor sets `[zones:<output>]`, `[monitors]`, `profile use-monitors` | ✅ | ✅ xrandr still answers under the Wayland session (measured) | ⚠️ `hyprctl monitors -j` |
| Per-workspace sets `[workspaces]` | ✅ | ✅ native Cinnamon workspaces | ⚠️ workspaces are dynamic; the mapping needs its own rules `[INFERENCE]` |
| `undo` (20 operations), `arrange`, `stack` | ✅ | ✅ Xwayland | ⚠️ |

## Automation and the daemon

| Function (0.3.0) | Cinnamon X11 | Cinnamon Wayland | Hyprland |
|---|---|---|---|
| `[rules]` auto-placement by `WM_CLASS` / `class|title` (glob + `re:`) | ✅ | ✅ Xwayland: `_NET_CLIENT_LIST` is readable (measured) | ⚠️ the `openwindow` event on the Hyprland socket `[INFERENCE]` |
| `reflow = neighbors` when a window closes | ✅ | ✅ | ⚠️ |
| `[ignore]` plus “pulled out of a zone — stays out” | ✅ | ✅ | ⚠️ Hyprland moves windows itself; a tracker of our own is needed `[INFERENCE]` |
| Tabs in a zone, `zone-tabstrip.py`, `tabs-next` | ✅ | ✅ Xwayland (the `Iconic` fix shipped) | ⚠️ either Hyprland groups or our strip through layer-shell `[INFERENCE]` |
| Focus mode “this zone only” (IPC `focus`) | ✅ | ✅ | ⚠️ a special workspace (`movetoworkspacesilent`) `[INFERENCE]` |
| `[action:<name>]`, built-ins, `GRIDSNAP_*` | ✅ | ✅ | ⚠️ class and geometry from `hyprctl clients -j` `[INFERENCE]` |
| Daemon + IPC: `status/apply/focus/reload/quit/watch`, `subscribe` | ✅ | ✅ our own socket; the logic is compositor-independent | ✅ |

## Input

| Function (0.3.0) | Cinnamon X11 | Cinnamon Wayland | Hyprland |
|---|---|---|---|
| Hotkeys via `setup-hotkeys.py` (`gsettings`, `zone-tiler: <zone>`) | ✅ | ✅ Cinnamon registers its own keybindings `[INFERENCE]` | ❌ no `gsettings` → ⚠️ a generator for `bind` lines in `hyprland.conf` |
| Direction keys `move left/right/up/down` | ✅ | ✅ | ⚠️ our neighbour math plus `dispatch` `[INFERENCE]` |
| Hot corners/edges: XInput2 raw events, HUD, place on drop | ✅ | ⚠️ only over Xwayland windows; over native ones nobody exposes the global cursor | ⚠️/❌ no motion event stream, only polling `hyprctl cursorpos` `[INFERENCE]` |
| Picker `Super+Z`, `--hud`, start zone under the cursor, X keyboard grab | ✅ | ⚠️ over Xwayland everything works (DOCK above clients, `XGrabKeyboard` measured); over native windows there is no start zone | ⚠️ `wlr-layer-shell` exists, `cursorpos` exists; the grab goes through the layer surface `[INFERENCE]` |

## Theme, shell, packaging

| Function (0.3.0) | Cinnamon X11 | Cinnamon Wayland | Hyprland |
|---|---|---|---|
| `theme.conf`: palette, five presets, zone hues | ✅ | ✅ | ✅ |
| Palette from the wallpaper (GdkPixbuf, `gsettings picture-uri`) | ✅ | ✅ same schema | ⚠️ no such schema → hyprpaper or the config file `[INFERENCE]` |
| Generated GTK controls theme `~/.themes/Gridsnap`, palette polling | ✅ | ✅ | ✅ |
| Cinnamon applet in the panel | ✅ | ✅ the shell runs | ❌ no Cinnamon |
| `en`/`ru`/`zh_CN` interface, man pages via po4a | ✅ | ✅ | ✅ |
| Packaging: `install.sh`, `.deb`, AUR, tarball | ✅ | ✅ plus the `xwayland` dependency | ⚠️ another distribution/desktop — out of scope |
| HiDPI: fraction of the **physical** work area, `GDK_SCALE` | ✅ | ⚠️ scale is per-output and fractional — the model needs revisiting | ⚠️ same |

## Never on Wayland, whatever the patches

For **Wayland-native** windows the X path cannot work: muffin has neither
`wlr-foreign-toplevel-management` nor `wlr-layer-shell`, and there is no public window IPC
(`org.cinnamon.Muffin.DisplayConfig` is monitors only; `IdleMonitor` is idleness only).

| Surface | Why it is lost |
|---|---|
| Frame compensation (`_NET_FRAME_EXTENTS`) | The client or the compositor draws the frame |
| Hot corners/HUD and the picker's start zone under the cursor | Nobody exposes the global cursor position |
| Hotkeys through `gsettings` | Replaced by the compositor's config file |
| “Fraction of the physical work area” | Scale becomes per-output and fractional |

The only official way to move **all** windows, native ones included, is a **Cinnamon extension**
(JS inside the compositor: `Meta.Window.move_resize_frame()`, `get_window_actors()`). An overlay
needs no layer-shell there: an Xwayland DOCK window already draws above clients (measured). This is
an owner decision and is not started. Without the `xwayland` package a muffin Wayland session has no
X layer at all, and this whole path stops working (Linux Mint ships xwayland).

## Hyprland: a richer API, a different desktop

Hyprland is **not** the audience (nobody runs the Cinnamon desktop on it) and not a session of
Cinnamon. It is worth knowing because its API is the opposite of muffin's:

- `hyprctl -j clients|monitors|workspaces|activewindow` — windows, outputs, workspaces as JSON.
- `hyprctl dispatch movewindowpixel|resizewindowpixel` — move and resize a window by address.
- An event socket (`socket2`): window creation, focus and workspace changes — the equivalent of the
  EWMH subscription the daemon builds by hand.
- `wlr-layer-shell` for overlays, `hyprctl cursorpos` for the pointer.
- Hotkeys are `bind` lines in `hyprland.conf`, editable at runtime with `hyprctl keyword`.

A port would therefore be **a new backend behind `window_backend.py`**, not a compositor extension:
zones stay fractions, windows must be floating (Hyprland's tiling would otherwise overrule the
layout), there are no frame extents, and the fractional-scale model needs rework. Nothing of this is
planned work; the rails are `docs/ROADMAP.md` (Wayland item) and `docs/architecture.md`
(“Window layer”).

## Sources

- `docs/RESEARCH-wayland-recon.md` — the nested stand, the measurements, the verdict, what stayed
  unmeasured (live hardware, the panel's strut, multi-monitor fractional scale).
- `docs/HANDOFF-2026-11-22-window-seam.md` — the wave that produced the seam and the recon.
- `docs/ROADMAP.md` — the Wayland item in its current wording; the old wording (“a layer over
  `niri msg`/`hyprctl`/`swaymsg`”) is history.
- Code: `window_backend.py` (the seam), `tests/test_wayland_compat.py`, `docs/architecture.md`
  (“Out of scope”).

Keep this page honest: the Hyprland column is an estimate, the Cinnamon Wayland column is a
measurement, and both must be re-checked on real hardware before they become promises.

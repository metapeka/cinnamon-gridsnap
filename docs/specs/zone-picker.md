---
id: zone-picker
type: module-design
status: active
title: "Module: the visual zone picker zone-picker"
parent: gridsnap-architecture
depends-on: [zone-tiler, zone_config, zone_picker_nav, zone-units]
---

_Described together with the implementation (wave 2); not reviewed by the owner._

## Responsibility

`zone-picker.py` is a GTK-3 overlay for visual zone selection (like gTile): a hotkey shows the
monitor's work area with the zones, arrows or the mouse choose a zone, and `Enter` places the active
window into it. It moves no window itself: placement goes through `zone-tiler.apply_zone`/`Grid.rect`,
so the layout is not duplicated.

## Capabilities

- an undecorated overlay over the monitor's work area (`workarea` from `zt.monitors()`); the monitor
  comes from the active window, otherwise from the pointer, and can be set with `--monitor`;
- zone arithmetic is in **physical** X pixels (`Grid.rect`, `apply_zone`), while the GTK window is
  logical: `move`/`set_default_size` get `zone_units.logical_rect`, or at scale 2 the overlay grows
  twice and covers the neighbouring screen. The pointer point from GDK is logical too: it is
  converted to physical before the monitor and the initial selection are chosen
  (`zone_units.physical_point`);
- zones come from `zones.conf` (`zone_config.load_zones`) and are drawn in fractions of the work area
  with the palette colour and a label of name, size and key;
- the zone under the cursor and the current selection are highlighted with the accent;
- **arrows** move the selection by the zone geometry, **`Enter`** places the active window, **`Esc`**
  cancels; a left click places, a right click cancels;
- the overlay grabs the keyboard itself (`Gdk.Seat.grab`): muffin keeps a `DOCK` window above, but the
  WM does not give it focus, so without the grab the arrows would go to another window;
- placement goes through `apply_zone`, so the window frame matches the zone pixel for pixel, and
  `Enter` records the operation in the layout history (`undo` works), like an ordinary
  `zone-tiler.py apply`;
- an excluded window (`[ignore]` or a utility `_NET_WM_WINDOW_TYPE`) is not moved by `Enter`: the
  overlay writes to stderr and exits with code 1 — the same `window_excluded` check the daemon and
  `arrange` use;
- `--dry-run` computes and prints the target frame without moving the window; `--conf` takes another
  config;
- `--shot FILE.png` draws the overlay into a PNG (cairo `ImageSurface`) and exits, without interaction.

## HUD mode (`--hud`) for the hot corners

The same overlay, but **passive** — `zone-hotcorners` drives it: during a drag muffin holds the
keyboard, so the HUD takes no `Gdk.Seat.grab`, asks for no focus (`set_accept_focus(False)`,
`set_focus_on_map(False)`) and is transparent for input.

- `--hud --at X,Y` — one frame and exit (for tests and `--shot`); the point is in **physical** X
  coordinates as `zone-hotcorners` sends it (`XQueryPointer`), so it is compared with the monitor's
  physical area (`Hud.set_point` → `zone_at_point`); the HUD window itself is logical
  (`zone_units.logical_rect`);
- `--hud --follow` — the window lives, reads `X Y` lines from stdin and redraws the highlight
  without a restart (a restart per frame would flicker); it exits on EOF or `SIGTERM`;
- the whole grid is drawn and the zone under the point is highlighted
  (`zone_picker_nav.zone_at`); if `[workspaces]` binds a desktop to a profile, the profile is named
  in the corner;
- input transparency: an empty input shape (`Gdk.Window.input_shape_combine_region`) **and** the
  `set_pass_through` flag — measured that the flag alone is not enough: GDK sets it while the X
  server keeps delivering events to the window.

## Desktop grid (`--grid`) and its toggle

`--grid` leaves the grid on the live desktop: the frames and the label plates are drawn over whatever
windows are open, and the zone under the pointer is the only accented one. Nothing covers the desktop
wholesale — a screen-wide wash would hide the windows the grid is read against.

- the geometry follows the editor canvas: a neutral frame (`fg` 0.55 over a `bg` 0.45 under-stroke, so
  the line reads over a bright window), a rounded plate (`bg` 0.66) behind the name, the size and the
  key, a 7 px hue dot as identity; the selected zone gets an `accent` 0.08 fill and a 2.2 px `accent`
  frame;
- one window per monitor (`Hud` with `style="grid"`), each monitor's grid resolved by the same
  `screen_grid` (so a `[zones:<output>]` set counts); the pointer is polled every `GRID_TICK_MS`
  (~15 Hz) and every window gets the same physical point, so the mark follows the pointer across
  screens;
- the window takes no input (the HUD's empty input shape and `set_pass_through`), uses a `DOCK` type
  hint and `keep_above`, and takes no focus: the desktop stays usable under it;
- **a compositor is required**: without one the window has no alpha visual, the transparent clear
  turns opaque and the grid would cover the desktop — `--grid` refuses with code 1 before creating a
  window;
- `--grid --shot FILE.png` writes the grid as a transparent PNG: a still for checks and pictures;
- `--toggle` starts the grid of the current desktop detached, or stops the one already shown. The pid
  file is `$XDG_RUNTIME_DIR/gridsnap/grid-<desktop>.pid`, one per desktop, so a toggle on one desktop
  neither kills nor hides the grid of another. A pid is trusted only when its command line names
  `zone-picker` and `--grid`: a stale file or a recycled pid never costs an unrelated process its
  life;
- the grid reads the saved config once, when it starts: after editing zones, toggle it again — the
  editor's **Show the zones on the desktop** row is the same command.

## Navigation (pure, `zone_picker_nav.py`)

The selection is an index into the zone list. All arithmetic lives outside GTK and is covered by
`tests/test_picker_nav.py`:

- `first_index` / `clamp_index` — the initial selection and index normalisation (an empty list gives
  `None`);
- `move_selection(zones, index, direction)` — the neighbour in a direction; the candidate must have
  its centre on that side, and the winner is chosen by `(no_perpendicular_overlap, perpendicular,
  axial_distance, index)`. That is why "right" from a wide `main` leads into the right column and not
  into `bottom`; at the edge of the list the index does not move;
- `zone_at(zones, fx, fy)` — the zone under a point in fractions; `None` in a gap between free
  rectangles, and for overlapping zones the last one drawn wins (as in the editor).

## Boundaries

- X11/EWMH and GTK 3 only; no new runtime dependencies (`python3-cairo` is already required by the
  project).
- Zone geometry is not duplicated: zones through `zone_config`, pixel frames through `Grid.rect`,
  placement through `apply_zone`.
- Pure logic stays without GTK and X (a project invariant); GTK and cairo live only in
  `zone-picker.py`.
- The picker's own hotkey (`Super+Z` in the README) is the user's business: `setup-hotkeys.py`
  registers the zone keys from `[keys]` and the desktop grid toggle from `[overlay]`.

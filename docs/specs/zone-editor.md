---
id: zone-editor
type: module-design
status: active
title: "Module: the visual editor zone-editor"
parent: gridsnap-architecture
depends-on: [zone-tiler]
---

_Draft from `importing-a-codebase`: restored from the code, not yet reviewed by the owner._

## Responsibility

`zone-editor.py` is the GTK 3 grid editor: it shows the work area at scale, lets the user draw
zones with the mouse, name them and bind keys, saves `zones.conf`, and launches the tiler or the
hotkey registration.

## Capabilities

- The monitor's work area is drawn at scale, zones in natural fractions; the canvas and the "px"
  line are computed in **physical** X pixels (`workarea` from `zt.monitor_outputs()`), while GTK
  draws in logical ones — fractions are dimensionless, so the picture and the numbers stay right
  above scale 1 (see `zone-units`).
- **One zone set per monitor**, selected by the chip in the header (`eDP-1`, `HDMI-1`): switching
  it loads that monitor's set — its own `[zones:<output>]` or a copy of the shared `[zones]`. The
  chip's tooltip says what is shown ("own `[zones:HDMI-1]` set" / "inherits `[zones]`") and, when
  the active workspace has a profile, which one. A monitor that is not connected is listed as
  "not on screen" and is never deleted.
- The header keeps the common path: the monitor chip, **Templates**, **Profiles**, a filled
  **Save** (insensitive while nothing is dirty) and a **…** menu. Save is also `Ctrl+S`.
- The **…** menu holds everything else: **From the gallery…**; the **Theme** group (**From the
  wallpaper…**, **Window controls…**, **Reload theme.conf**); **Place the window on another
  monitor…** (monitor → zone, through `apply --monitor <output>`, after saving unsaved edits);
  **Workspace profiles…**; **Monitor profiles…**; **Apply hotkeys in Cinnamon**; **Place the active
  window into the selected zone**; and **Clear all…**, which asks for confirmation first.
- Dragging a zone and resizing it by an edge or corner; the cursor names the side.
- Snapping within `SNAP_PX = 9` px to the screen edges, to neighbouring zones and to the lines
  `0, 1/4, 1/3, 1/2, 2/3, 3/4, 1`.
- The smallest zone is `MIN_SIZE = 0.05`; zones stay inside 0..1.
- All pure geometry — scale, fractions ↔ pixels, snapping, drag/resize, bounds and hit testing —
  lives in `zone_geom.py` without GTK or X; `Canvas` only delegates to it.
- Double click on empty space adds a zone; `Delete`/`Backspace` removes it; the arrows move it,
  `Ctrl`+arrows resize it, the wheel changes its width.
- Templates: 2 columns, 1+2, 2×2, 3 columns, main + stack, blank slate (with ready
  `Super+Alt+N`).
- **The zone list** in the sidebar is the selection surface: an identity dot (`zones[i]`), the
  name and its key. The canvas stays neutral — zones are `fg` at 0.04 with a `border` outline, the
  selected one is `accent` (0.10 fill, 1.8 px outline) — so the colour identifies a zone in the
  list and in the picker, not on every pixel of the grid.
- **The inspector** under the list appears while a zone is selected: Name, Keys and one muted
  geometry line — `49 % × 100 % · 935 × 1040 px`. With nothing selected it is replaced by a hint.
- The **Workspace profiles…** dialog: one row per Cinnamon workspace (`_NET_NUMBER_OF_DESKTOPS`,
  4 without X), each a profile or "shared set"; only `[workspaces]` is written
  (`zone_config.save_workspace_map`) and re-read into the foreign sections so the next `Ctrl+S`
  does not roll it back; a profile missing on disk is shown as a warning, the mapping is not
  deleted.
- The **Profiles** menu: the saved profiles, "Save the current layout as a profile…" and deletion;
  choosing a profile loads its zones, rules and keys into the canvas as a draft (saved with the
  usual `Ctrl+S`).
- A zone name is sanitized (`[^0-9A-Za-zА-Яа-я_.-]+ → '-'`) and must stay unique.
- Saving (`Ctrl+S`, the button or the menu) writes the canonical `zones.conf` with a header
  comment; foreign sections (`[rules]`, `[daemon]`, …) are carried over **verbatim**, with their
  comments and key case (`[rules]` before `[keys]`, `[daemon]` and the rest after).
- If a `[rules]` rule points at a missing zone, the file is **not saved** and the reason appears
  in the status bar.
- **Apply hotkeys in Cinnamon** saves the file and runs `setup-hotkeys.py`; **Place the active
  window into the selected zone** saves and runs `zone-tiler.py apply <zone> --verify`.
- The status bar is one line: the last event, or whose set is on the canvas. The long workspace
  note lives in the monitor chip's tooltip instead of the status prose.
- `--shot FILE.png` — an offscreen snapshot of the interface without showing the window.

## Bounds

- UI only: the editor does not lay out windows or register keys itself — it delegates to
  `zone-tiler.py` / `setup-hotkeys.py` and to the imported tiler module (`Grid`, `monitors`,
  `fractions`).
- It works on one active `zones.conf`; profiles go through `zone_config` (directory, read/write):
  the editor loads the chosen profile into the canvas as a draft but does not store profiles
  itself. There is no "profile → monitor" binding here: a profile holds the sets of all monitors,
  but the editor does not lay different profiles on different screens.
- What to write on save is decided by the pure function `zone_geom.save_plan`: a
  `[zones:<output>]` section is written when the monitor's set differs from the shared one or was
  edited explicitly; a section whose set matches the shared one with no edits is removed — but
  only for a monitor that is on screen. A disconnected output is never removed. Zones that repeat
  a name lose their key binding: `[keys]` is one per file.
- The `[keys]` section is one per file, so `zone_config.save_zone_sets` writes all sets in one pass
  and does not duplicate names in `[keys]` (a repeated name broke reading: `ConfigParser` raises on
  a repeated option).
- Geometry is not duplicated: the maths lives in the pure `zone_geom.py`, covered by unit tests
  without X (`tests/test_zone_geom.py`).
- Requires GTK 3 and a display; `--shot` exists for an offscreen check.

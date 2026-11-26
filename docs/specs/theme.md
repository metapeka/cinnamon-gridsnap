# `theme.conf` — one palette for every surface

Status: implemented on `design/43pr-theme`. The module is `zone_theme.py`; the consumers are
`zone-picker.py` (overlay and HUD), `zone-hotcorners.py` (through the HUD child), `zone-tabstrip.py`
and `zone-editor.py`.

GridSnap draws four surfaces of its own: the interactive picker overlay, the passive HUD the hot
corners drive, the tab strip above a zone, and the editor. They used to carry their colours as
float constants, each module with its own set. One optional file now feeds all of them, so a
selection, a tab and a zone look like the same product.

## File

```
<directory of the zone config>/theme.conf
```

The zone config is `--conf` (default `~/.config/gridsnap/zones.conf`), so with a custom config the
theme follows it. There is no `--theme` flag: the theme is a property of the configuration, like the
profiles, and a second path on the command line would let the two drift apart.

The file is optional and its absence changes almost nothing: the defaults below reproduce the
colours the surfaces drew before the theme existed — the picker, the HUD and the editor are exact.
The tab strip moves one step darker, because it now shares the panel and card shades with the editor:
its strip goes `#21242b` → `#1c1f24` (`bg_panel`) and a tab `#333842` → `#2b2e36` (`bg_card`); the
active tab (`#525c70`) and the captions are unchanged. The format is INI, the same family as
`zones.conf`, and not TOML on purpose: `tomllib` arrived in Python 3.11, Cinnamon ships on
distributions with 3.10 (Mint 21), and an import error here would take the whole picker down, not
just the theme.

The `[theme]` section holds the switches (`name`, `preset`, `radius`, `font`, `zones`); the colours
live in `[colors]`, and a colour in `[theme]` is accepted too. Keys in `[DEFAULT]` are accepted as
well: configparser has no way to write a key before the first section, and a file without a palette
section is a legitimate thing to write. Unknown keys are ignored without a word — a palette file from
another project may be dropped in as is and keep its extra keys.

```ini
[theme]
preset = 43pr
radius = 10
font = JetBrains Mono

[colors]
bg       = #000000
bg_panel = #050505
bg_card  = #0d0d0d
bg_hover = #242832
fg       = #ffffff
fg_muted = #8b93a7
accent   = #ffffff
border   = #151515
danger   = #ff003c
ok       = #00ff9c
zones    = #5c9eeb, #6bc794, #eba85c, #cc80d9, #6bccd9, #eb8585, #b8c266, #9e9eeb
```

## Tokens

| Token | Meaning | Default |
|---|---|---|
| `bg` | behind everything: the overlay backdrop, the GTK window | `#12141a` |
| `bg_panel` | a panel surface: the tab strip, the editor canvas, the toolbar | `#1c1f24` |
| `bg_card` | a raised surface: a tab, the editor "screen", a button, an entry | `#2b2e36` |
| `bg_hover` | hovered or active: the active tab, a hovered button | `#525c70` |
| `fg` | main text and the work-area frame | `#ffffff` |
| `fg_muted` | captions, `.dim-label`, inactive tab titles | `#c7ccd6` |
| `accent` | the selected zone, the active tab stripe, a checked button | `#ffb554` |
| `border` | frames and separators of the GTK parts | `#525c70` |
| `danger` | reserved for destructive actions | `#eb8585` |
| `ok` | reserved for confirmations | `#6bc794` |
| `zones` | up to 16 zone hues, comma-separated; derived from `accent` when absent | the eight hues below |
| `radius` | zone corner radius in px (picker and editor) | `10` |
| `font` | Pango family for labels and tab titles | `Sans` |

The default zone hues are `#5c9eeb`, `#6bc794`, `#eba85c`, `#cc80d9`, `#6bccd9`, `#eb8585`,
`#b8c266`, `#9e9eeb` — the picker palette that predates the theme. Every default is the exact
colour the old constants produced (`(0.36, 0.62, 0.92)` is `#5c9eeb`), so an installation without
`theme.conf` does not change its look.

## Presets

| Name | Source |
|---|---|
| `43pr` | 43PR/dotfiles `default` — monochrome chrome, white accent |
| `nord` | 43PR/dotfiles `nord` |
| `tokyo-night` | 43PR/dotfiles `tokyo-night` |
| `catppuccin-mocha` | 43PR/dotfiles `catppuccin-mocha` |
| `graphite` | 43PR/dotfiles `graphite` |

The values are transcribed from [43PR/dotfiles](https://github.com/43PR/dotfiles) (MIT), including
its token names — that is the part of its design GridSnap borrows. What is **not** borrowed: the
QML shell. The bar, the side panel, the power menu and the OSDs are Quickshell widgets that need a
Wayland compositor with `wlr-layer-shell` and Hyprland's IPC; Cinnamon runs Muffin on X11 and its
own panel, so those files have no host here. A preset therefore changes colours, radius and font,
not the shape of the desktop.

`preset` is a base: `[colors]` overrides whatever it names. An unknown preset prints a warning and
keeps the default palette.

## Deriving the zone hues

Without `zones` the palette is generated from `accent`, so a theme needs one colour to make a whole
grid:

* a saturated accent starts the hue ring at its own hue;
* an accent with HSL saturation below 0.15 (the monochrome presets: `43pr` has a white accent,
  `graphite` a grey one) is replaced by hue 210°, because eight identical greys would make the grid
  unreadable;
* the ring is eight steps of 45°, at fixed lightness 0.62 and saturation 0.45 — a theme changes the
  hues and the chrome, not the contrast of a caption on a zone.

## Validation: a broken theme never breaks the desktop

Every problem keeps a usable value and prints one `zone-theme:` line on stderr. Nothing raises:

| Problem | Result |
|---|---|
| the file is missing | the defaults, silently |
| the file is unreadable or is not INI | the defaults, one warning |
| a colour is not `#RRGGBB` | that token keeps the base colour (the preset's, else the default) |
| an unknown `preset` | the default palette |
| `zones` holds a malformed entry | the entry is dropped, the rest survive |
| more than 16 zone colours | the extra ones are ignored |
| `radius` is not an integer or outside 0..64 | the default radius |
| `font` contains `,;={}[]` or is empty | the default family |

A grid that disappears because of a typo in a colour is worse than a grid in the default colours,
so the tools that draw never exit because of the theme.

## Consumers

| Surface | Tokens |
|---|---|
| picker overlay and HUD backdrop | `bg` (0.90 opaque in the overlay, `HUD_ALPHA` 0.25 in the HUD) |
| picker zone fill and outline | `zones[i]` (fill 0.20, outline 0.75) |
| picker selection | `accent` (fill 0.16, 3 px outline), corner radius `radius` |
| picker caption and hint | `accent` 0.92 for the caption, `fg` 0.72 for the hint |
| picker labels | `font` through `Theme.font_desc` |
| tab strip background / tab / active tab | `bg_panel` / `bg_card` / `bg_hover` |
| tab strip captions / accent stripe | `fg_muted` and `fg` / `accent` |
| tab strip titles | `font` at size 8 |
| editor canvas / "screen" | `bg_panel` / `bg_card` |
| editor zones and selection | `zones[i]` (fill 0.30, outline 0.85) / `accent` |
| editor guides, frame, legend | `fg` at 0.05 / 0.35 / 0.45 |
| editor labels | `font` at 11 bold and 8; legend at 9 |
| editor GTK chrome | the sheet below |

`zone-hotcorners.py` needs no change: it runs `zone-picker.py --hud --conf ...`, so the HUD picks
the theme up from the same path as the overlay.

## The editor's GTK sheet

`Theme.gtk_css()` generates the sheet the editor applies at
`GTK_STYLE_PROVIDER_PRIORITY_APPLICATION`:

* `window, window.background, .background` — `bg` / `fg`;
* `.toolbar, toolbar, .primary-toolbar, headerbar, .titlebar, .menubar, menubar, notebook,
  notebook > header` — `bg_panel`, no background image;
* `list, row, .view, treeview, scrolledwindow, viewport, viewport.frame` — `bg_panel`, no background
  image;
* `button` — `bg_card` with a `border` frame; `button.flat, button.image-button` — transparent
  (toolbar icons keep the toolbar surface); `:hover` — `bg_hover`; `:checked, :active` — `accent`
  with black or white text, whichever reads on it (relative luminance, threshold 0.35);
* `entry, spinbutton` and `textview, textview text` — `bg_card` / `fg` with no background image;
* `row:selected, list row:selected, .view:selected, *:selected` — `accent` with the contrasting text;
* `tooltip` — `bg_card` / `fg` / `border`;
* `frame, frame > border, separator, menubar > menuitem` — `border`;
* `.dim-label` — `fg_muted`.

Every selector clears `background-image` where the theme paints a gradient: a background colour
alone is invisible under one. The classes are the ones the running widgets actually carry
(`.toolbar`, `list`, `row:selected`); a type selector on its own loses to the theme's class rules.

Where the provider goes is part of the contract: a provider attached to a widget's style context
styles **that widget only**, so the real window takes the sheet on its screen (all descendants
follow), while `--shot` — a `Gtk.OffscreenWindow` that is not on any screen — gets the sheet on a
walk of the tree (`apply_theme_tree`). Without that the snapshot would show unthemed chrome and
would prove nothing.

This is not a GTK theme: it styles the editor window, not every application. A full GTK or Cinnamon
theme is a different product, and the shell parts — the panel, the menu, the notification daemon,
the window decorations — belong to Cinnamon's own theme system, which has no blur and no per-window
rounding to match the reference setup.

## Out of scope

* **Cinnamon shell theme** (`cinnamon.css`, GTK3/GTK4 theme). It would repaint the whole desktop
  and would still miss the blur and vibrancy the reference design gets from Hyprland.
* **Quickshell/QML widgets** — no host on Muffin/X11 (see above).
* **`matugen`** (a palette sampled from the wallpaper) and the template fan-out of 43PR's
  `theme.py` to other programs' configs: they theme the desktop, not GridSnap. A later wave can
  write `theme.conf` from `matugen`'s output without touching this contract.

## Verification

* `tests/test_theme.py` — defaults equal the old constants, loading and presets, every fallback
  above, derivation, the GTK sheet.
* `python3 zone-picker.py --conf <conf> --shot /tmp/picker.png` and
  `python3 zone-editor.py --conf <conf> --shot /tmp/editor.png` render the surfaces without a
  session; a `theme.conf` next to `<conf>` changes the PNG.
* `make test`, `make check-i18n`, `tests/smoke-picker*.sh`, `tests/smoke-tabs.sh` — the drawing
  paths still run.

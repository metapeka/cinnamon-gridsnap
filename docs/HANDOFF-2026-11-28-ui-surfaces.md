# Wave: the window surfaces leave the many-hue field

The owner asked to take the remaining surfaces — the picker, the HUD, the tab strip, the gallery
dialog — to the same Apple-leaning language the editor got in #53/#54. The audit found one thing
running through them: the picker and the HUD filled every zone with its own hue, so a four-zone grid
was four competing colours, the wallpaper-derived palette turned zones 2 and 3 into the same green,
and the accent's "this is the zone the keys act on" was one more colour among them. The editor
canvas had already been through this correction; the surfaces behind it had not.

| Where | Change |
|---|---|
| `zone-picker.py` | `draw_zones` paints a neutral zone (`bg` 0.55 scrim + `fg` 0.05 surface, `fg` 0.22 hairline) and puts the zone's hue in a 7 px dot beside the name; surfaces, selection and labels are three passes, so the selection wash no longer tints the text it marks; the labels and the hint use `fg`/`fg_muted` from the theme instead of hard-coded white; selection softened to `accent` 0.10 + 2.5 px |
| `zone-tabstrip.py` | tabs are rounded on top only (`TAB_RADIUS` 5 px, `_tab_path`); the active tab's accent stripe is clipped to that path instead of a square rectangle |
| `zone_theme.py` | `.gallery-list row:selected` — an `accent` 0.16 tint with a 0.50 hairline and `fg` text, its `.dim-label` back to `fg_muted`; derived zone hues alternate lightness 0.56/0.66 (`DERIVED_LIGHTNESS_ALT`) so neighbours stay apart in the dot |
| `zone-editor.py` | the gallery dialog: a `.gallery-list` class on the list, a wrapped muted intro (`max_width_chars` 64, which also narrows the dialog), the primary action as `button.suggested-action`, and **Add to profiles…** (with the ellipsis, because it opens a name prompt) |
| `docs/specs/theme.md` | the consumers table: the picker's neutral surface, the identity dot, the softened selection, the rounded tabs, the gallery's quiet selection and primary action; the derivation section names the alternating lightness; the sheet list documents the gallery exception |
| `docs/specs/zone-editor.md` | the gallery dialog: a tint instead of a filled slab, the accent on the primary action |
| `po/ru.po`, `po/zh_CN.po` | `Add to profiles…` (“Добавить в профили…”, “添加到方案…”); the obsolete `Add to profiles` entries dropped |
| `site/assets/` | `picker.png`, `editor.png` (dots) and `demo.gif` re-rendered by the scripts, not by hand |

## Decisions

1. **The picker paints zones like the editor canvas, and the hue becomes a dot.** The theme track
   already rejected a many-hue field once; the picker kept it, and with a wallpaper accent the
   derived ring gave two greens side by side. One accent per view means the selection is the only
   accent, and `zones[i]` is identity — the same 10 px dot the zone list uses.
2. **A scrim under the surface, not a stronger hue.** The HUD's backdrop is a quarter opaque, so a
   neutral fill alone would disappear on a bright wallpaper. Each zone gets `bg` 0.55 first: the
   grid reads over the desktop while the wallpaper still shows through.
3. **Three passes: surfaces, selection, labels.** The selection was drawn after the labels, so the
   accent wash tinted the name and the percentages of the selected zone. Drawing the selection
   between the surfaces and the labels keeps both the wash and the text clean.
4. **Hue alone does not identify at 7 px.** Hues 73° and 118° at one lightness read as the same
   green; alternating lightness 0.56/0.66 costs nothing in contrast (both steps stay above the
   3:1 floor on the dark chrome) and makes neighbours tell apart.
5. **The gallery is the one quiet selection.** A 60 px card with a layout picture and a description
   cannot carry a full accent slab — the picture turned orange and the text lost its hierarchy. The
   zone list keeps its accent row (#54): a 24 px row is a tag, a card is a surface.
6. **Text comes from the tokens, not from white.** The picker labels were `(1, 1, 1, …)`; a light
   `fg` in a preset would have been the only surface not following its own palette.

## Verification

```
$ make check
Ran 1536 tests in 17.537s

OK (skipped=9)

$ make lint
All checks passed!

$ make check-i18n
msgfmt zh_CN
собраны каталоги в locale/
каталоги в порядке

$ env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh <scenario>
tests/smoke-picker.sh        rc=0   Дымовой тест выбора зоны пройден.
tests/smoke-picker-workspace.sh rc=0 Дымовой тест сетки стола в пикере пройден.
tests/smoke-tabs.sh          rc=0   Дымовой тест вкладок в зоне пройден.
tests/smoke-hotcorners.sh    rc=0   Дымовой тест hotcorners пройден.

$ env -u DISPLAY python3 -m unittest tests.test_screenshots tests.test_demo_gif
Ran 21 tests in 0.127s

OK
```

The surfaces were looked at, not inferred. On a private Xvfb `:93` with the scene palette
(`accent #c7763b`): the picker and the HUD rendered by `--shot`; the HUD additionally composited
over a bright wallpaper with cairo (the same `bg` 0.25 backdrop the compositor would produce, which
a screenshot without a compositor cannot show); the tab strip fed a three-tab snapshot at the real
`STRIP_HEIGHT` 22; the gallery dialog opened through the editor's **…** menu with `xdotool`.

- Picker: four zones as neutral surfaces, hue dots, one accent outline on `main`, the key hint in
  `fg`.
- Picker with no `theme.conf`: the eight default hues give blue/green/purple/orange dots — still one
  accent on the surface.
- HUD over a bright wallpaper: the grid stays visible, the name reads; the size line is the weakest
  text on the surface (a recorded leftover, see below).
- Tab strip: rounded tops, the stripe follows the curve; `Terminal` reads as the active tab.
- Gallery: the chosen card is a tint; its layout shape keeps the neutral fills; **Add to profiles…**
  carries the accent; the intro wraps and the dialog narrows with it.

## Leftovers

- **The HUD's size line over a bright wallpaper.** `fg_muted` at 95 % on a mid-grey scrim is about
  2:1. It was no better before (white 62 % on a hue wash) and the owner's palette is dark, so it is
  recorded rather than fixed: the honest fix is a text scrim behind the label, not another colour
  tweak.
- **Two hues can still sit close.** Alternating lightness separates neighbours, but a theme that
  names its own `zones` list can put two similar hues side by side; that is the user's list, and the
  editor's dot is the only place it shows.
- The gallery/scroller still has no visible scrollbar until it is hovered — GTK overlay scrolling
  under the editor's sheet; eight cards scroll with the wheel.

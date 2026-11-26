# Wave: the screenshots are rendered by a script

Closes the first item of `## Next` in `docs/HANDOFF-2026-11-26-docs.md`: the READMEs and the site
showed a hand-made `site/assets/editor.png` whose palette predates the theme track, and no surface
of the picker at all. A hand-made picture drifts silently — the palette moves, the surface moves,
the file keeps showing last month's product — so the images are now produced by a script, and a
unit test holds the pair.

## What shipped

| Where | Change |
|---|---|
| `tools/shoot-screens.sh` | new: its own pinned Xvfb `:91` (1280×800), a temporary `zones.conf` and `theme.conf`, then `zone-editor.py --shot` and `zone-picker.py --shot`; writes `site/assets/editor.png` (1140×720) and `site/assets/picker.png` (1280×800). A PNG encoder of thirty lines (`zlib` + `struct`) draws the wallpaper — a slate gradient with a warm glint — so the scene needs no ImageMagick and the palette is **derived from an image the script can describe in a sentence** (`zone_theme.from_wallpaper` → `write_theme` with the generated marker, the same path the editor's **Theme → From the wallpaper…** action takes) |
| `site/assets/editor.png` | re-rendered: the palette is the sampled one (`bg #090c11`, `card #171e2b`, `accent #c7763b`), the scene is English (`GRIDSNAP_LANG=en`), the zone set is two columns with the right one split |
| `site/assets/picker.png` | new: the picker overlay of the same palette and zone set, with the hint line and the selected zone |
| `README.md`, `README.ru.md`, `README.zh.md` | the picker image between the editor and the GIF, with a translated alt text |
| `site/index.html`, `index.ru.html`, `index.zh.html` | the same image in the screenshots section, with a translated alt text |
| `tests/test_screenshots.py` | 7 tests: both files are PNGs by signature and IHDR size; the editor is ≥1000×600; the picker is exactly the 1280×800 work area the script pins; all three READMEs and all three pages reference both; the script is executable and names both files; the script still samples a wallpaper and pins the scene language |
| `docs/specs/theme.md` | the verification list gained the script and the test |

## Decisions

1. **The wallpaper is drawn in Python, not by ImageMagick.** `record-demo.sh` uses `convert` for
   its background, but that is optional cosmetics there. Here the palette *is* the point of the
   picture, so the source image has to be reproducible on any machine with `python3` alone; the
   encoder is `zlib.compress` plus five 4-byte fields.
2. **A worked palette, not the default one.** The images exist to show where the colours come
   from. The default palette (`#12141a`, `#ffb554`) looks like a product without the feature;
   the sampled one is warm slate with an orange accent — visibly "not the defaults".
3. **Two stills, not three.** The HUD draws the same grid as the overlay; the two `--shot` files
   differ in exactly 1280 pixels, and their bounding box is the 286×15 hint line
   (`arrows · select · Enter · place · Esc · cancel`) — that is the only thing a HUD image would
   say. The GIF already carries the moving scene.
4. **The scene stays English.** All three READMEs and all three pages show the same two files, and
   English is the source language and the site's entry page; a Russian editor shot on the English
   README would be the worse trade.
5. **The test pins sizes, not pixels.** Comparing hashes would fail on every GTK point release and
   on every palette tweak, while a crop to 300×200 or a PNG that is actually a renamed JPEG are
   the regressions worth catching.

## Verification

```
$ sh tools/shoot-screens.sh
screenshots: display :91, work area 1280×800
  palette: bg #090c11, card #171e2b, accent #c7763b, 8 zone hues
rendering the editor…
rendering the picker…
  /home/db1/thinkrail/gridsnape/site/assets/editor.png (51878 bytes)
  /home/db1/thinkrail/gridsnape/site/assets/picker.png (16069 bytes)
screenshots written: editor.png, picker.png
$ file site/assets/editor.png site/assets/picker.png
site/assets/editor.png: PNG image data, 1140 x 720, 8-bit/color RGBA, non-interlaced
site/assets/picker.png: PNG image data, 1280 x 800, 8-bit/color RGB, non-interlaced
$ env -u DISPLAY python3 -m unittest tests.test_screenshots -v
Ran 7 tests … OK
```

The second run of the script produced byte-identical files (the palette is deterministic, the
Xvfb size is pinned, the scene has no clock).

## Next

- A native proofread of the Chinese interface, man page and alt texts is still open.
- The rest of `docs/**` is still Russian; translate it file by file as it is touched.

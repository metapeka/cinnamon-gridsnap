# Wave: the demo GIF is recorded from a themed, sandboxed scene

`site/assets/demo.gif` was recorded on 22 November — before the theme track. The still images
(`editor.png`, `picker.png`) had already been re-rendered from a wallpaper palette, but the GIF
still showed the default colours and an editor without the **From the gallery…** and **Theme**
buttons: the README and the site led with a picture of a product two waves older than `main`.

Re-recording it turned up three real defects in `tools/record-demo.sh`, all of them latent because
the script had not been run since the language inversion.

## What shipped

| Where | Change |
|---|---|
| `tools/scene-palette.py` | new: the scene wallpaper (a PNG encoder over `zlib` + `struct`) and the `theme.conf` sampled from it, extracted from `shoot-screens.sh` so both recordings share one palette. Prints `bg`/`card`/`accent` on one line. `--pin-wallpaper` points the desktop background setting at the drawn image |
| `tools/record-demo.sh` | three fixes (below), the scene palette instead of a flat `convert` gradient, an English script (the repository rule), `identify` demoted to optional |
| `tools/shoot-screens.sh` | English, and the inline wallpaper encoder replaced by `tools/scene-palette.py`; the re-rendered `editor.png`/`picker.png` are byte-identical to the previous ones |
| `tests/test_demo_gif.py` | rewritten in English; the file's shape as before, plus the script's contract: the shared palette, the pin, the sandbox, `OPEN_PID` instead of a command substitution, the repository root on `sys.path`; a new `ScenePaletteTest` runs the helper into a temporary directory and checks the PNG and the marker |
| `tests/test_screenshots.py` | the "still samples a wallpaper" assertion now names the shared helper |
| `docs/specs/theme.md` | the two recording scripts and the shared helper in the verification list |
| `site/assets/demo.gif` | re-recorded: 960×600, 332 frames, 1414 KiB, from the wallpaper palette |

## The three defects

1. **A window opened in a command substitution does not survive it.**
   `W1PID=$(open_window "Mail — inbox" …)` runs the whole function in a subshell; the `xmessage`
   started there is killed with it. `dash` returns a plausible PID, `ps -o pid -p "$W1PID"` says
   "no such process", `xdotool search --name Mail` finds nothing, and the scene dies on "the window
   did not open" with an empty log. Measured: with the substitution, `AFTER1S: 0` `xmessage`
   processes; with `open_window …; W1PID=$OPEN_PID`, the window lives and step 2 passes. The
   function now hands the PID out through `OPEN_PID`.

2. **The frame check could not import its own modules.**
   `frame_ok` loads `zone-tiler.py` with `importlib` and no `sys.path` entry. Outside the repository
   root that raises `ModuleNotFoundError: No module named 'zone_i18n'` (the tiler imports its
   siblings by bare name), so every `expect_frame` failed for a reason that had nothing to do with
   the frames. The root is now on `sys.path`; the helper takes it as `$ROOT`.

3. **The daemon replaced the scene palette with the owner's wallpaper palette.**
   A marked `theme.conf` is refreshed by the daemon whenever its recorded source differs from the
   live `picture-uri`. The scene wrote its own `theme.conf` but left the setting alone, so on the
   first 5-second poll the palette became the one sampled from
   `/usr/share/images/desktop-base/desktop-background` — the recording would have differed between
   machines, and the editor's light Adwaita sheet in the GIF was the visible symptom. The scene now
   pins `picture-uri` to its own wallpaper inside a `GSETTINGS_BACKEND=keyfile` sandbox, and owns a
   temporary `HOME` so `~/.themes/Gridsnap` is never written.

## Decisions

1. **One palette helper, not two encoders.** `shoot-screens.sh` had the wallpaper drawn inline; the
   demonstration needed the same picture. A shared script keeps the two scenes on one palette and
   gives the test something to hold that is not a copy of the code.
2. **Pin the wallpaper rather than switch the daemon's poll off.** The poll is part of what the
   product does; a scene that disables it would not show the product. Pinning the setting keeps the
   poll live and makes its outcome deterministic.
3. **The GIF is not re-shot for the HUD.** The HUD draws the same grid as the picker overlay; the
   five steps already carry the editor, placement, the picker, tabs and the focus mode, and the
   file stays inside 2.5 MB at 960 px.
4. **The scene wallpaper is `feh`'s background when `feh` is installed.** Without it muffin's
   default background stays — cosmetics, not the palette: the surfaces read `theme.conf` either
   way.

## Verification

```
$ sh tools/record-demo.sh --display :90 /tmp/demo-new2.gif
demo: display :90, work area 1280×800, 15 frames/s
  palette: bg #090c11, card #171e2b, accent #c7763b, 8 zone hues
recording the scene…
  step 2: a window into the main zone
  step 3: the picker
  step 4: tabs
  step 5: only this zone
assembling the GIF…
GIF written: /tmp/demo-new2.gif (1414 KiB, 960x600, 332 frames)

$ sh tools/shoot-screens.sh
screenshots: display :91, work area 1280×800
  palette: bg #090c11, card #171e2b, accent #c7763b, 8 zone hues
rendering the editor…
rendering the picker…
screenshots written: editor.png, picker.png
# md5 of both PNGs unchanged from before the refactor

$ env -u DISPLAY python3 -m unittest discover -s tests
Ran 1520 tests in 20.634s — OK (skipped=9)

$ make lint
All checks passed!

$ make check-i18n
каталоги в порядке
```

The frames were read back out of the new GIF: the editor shows the sampled palette, the zone list
and the **From the gallery…**/**Theme** buttons; the picker shows the accent-lit grid with the hint
line; the tab strip shows a tab in `bg_card` with the accent stripe; the focus step shows the other
window iconified.

## Next

- A native proofread of the Chinese interface, man page and alt texts is still open.
- The rest of `docs/**` is still Russian; translate it file by file as it is touched.
- `docs/HANDOFF-2026-11-26-screenshots.md` still describes the wallpaper encoder as part of
  `tools/shoot-screens.sh`; the wave history is not rewritten, so read this file for the current
  layout.

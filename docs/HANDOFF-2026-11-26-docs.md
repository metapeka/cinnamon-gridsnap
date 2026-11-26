# Wave: documentation catches up with the theme track

No code. The theme waves (palette, wallpaper, refresh, man page, GTK controls) had landed in `main`
while the surrounding documents still described a product without colours: `docs/architecture.md` did
not know `zone_theme.py`, the Russian and Chinese READMEs lacked the `theme.conf` section that the
English one had, the site did not mention the feature, and the roadmap had no entry for the track.

## What shipped

| Where | Change |
|---|---|
| `docs/architecture.md` | translated to English (the file was Russian and is updated as it is touched); a **module graph** in Mermaid; `zone_theme.py` and the daemon's refresh loop in the module list; decisions for the theme, the wallpaper sampling (and why not `matugen`), the marked generated files; invariants (`zone_theme` imports without GTK, the daemon writes only generated files); out-of-scope section with the Muffin/Metacity and Quickshell findings |
| `docs/specs/theme.md` | status moved to `main`; two Mermaid diagrams — how a palette reaches a surface (presets/`theme.conf`/wallpaper → `Theme` → surfaces, sheet, controls theme, daemon poll) and how the file is resolved (absent → defaults; preset → base; `[colors]` → overrides; lenient validation; derive zones) |
| `README.ru.md`, `README.zh.md` | §3.4 “Colours: `theme.conf`”, mirroring the English section: tokens, presets, the **Theme** menu with both actions, the daemon refresh, and the border limit. Same `<!-- section: … -->` keys, so the language test stays green |
| `docs/ROADMAP.md` | item 5: the theme track is done, with the two things not attempted and the contract link |
| `site/index.html`, `site/index.ru.html`, `site/index.zh.html` | one feature bullet (“Colours from the wallpaper”, with the border caveat) and a link to `docs/specs/theme.md` in the documentation list |
| memory wiki (outside the repository) | a dated GridSnap entry in `~/.pi/agent/memory/MEMORY.md`: the palette file, the wallpaper bridge, the border finding and where the docs now live |

## Decisions

1. **Mermaid in docs, plain HTML on the site.** GitHub renders Mermaid in Markdown; the site is
   three hand-written HTML files with no build step and no external scripts, so a diagram there would
   mean a CDN dependency. The site gets words and a link instead.
2. **The architecture doc is translated, not versioned twice.** The language rule of the repository is
   “translate as you touch”, and this wave touches most of the file anyway. The structure and all
   decisions are kept; the graph is new.
3. **The roadmap entry is English inside a Russian document.** New text in this repository is English
   (`AGENTS.md`); the older Russian prose stays until the file is translated as a whole.
4. **`theme.conf` stays out of the site’s “install” block.** It is optional — the site’s promise is
   still “draw zones, press a key”, and the palette is a footnote, not a step.

## Verification

```
$ env -u DISPLAY python3 -m unittest tests.test_docs_languages tests.test_site_pages
Ran 9 tests … OK
$ make lint
All checks passed!
$ make check
Ran 1506 tests … OK (skipped=9)
```

`test_docs_languages` compares the section keys of the three READMEs and the images they link;
`test_site_pages` compares the `id`s of the three pages and checks every link and anchor.

## Next

- ~~Screenshots of the themed surfaces for the site (`site/assets/`) — the editor shot with a
  wallpaper palette would show the feature better than a bullet.~~ Done in
  `docs/HANDOFF-2026-11-26-screenshots.md`: `tools/shoot-screens.sh` renders the editor and the
  picker from a sampled wallpaper palette, and `tests/test_screenshots.py` holds the pair.
- The rest of `docs/**` is still Russian; translate it file by file as it is touched.

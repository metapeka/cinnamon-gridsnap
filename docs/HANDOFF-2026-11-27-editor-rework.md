# Wave: a neutral editor chrome, and gallery cards that speak the interface language

Two pull requests went into `main` on 27 November, both from the editor rework the owner asked for
after rejecting the wallpaper-tinted chrome and the twelve-control toolbar:

| Merge | Commit | What |
|---|---|---|
| #53 | `4dc7b16` (work `119791f`) | the editor's header + sidebar rework and the neutral palette |
| #54 | `60c1087` (work `a81889c`) | the gallery cards translated through the catalogs |

The work sat uncommitted in the tree for most of a session; the split into two PRs is deliberate —
the UI rework is the owner's decision made visible, the gallery translation is the last known gap
inside it.

## What shipped — #53, the chrome

| Where | Change |
|---|---|
| `zone-editor.py` | header (title, monitor chip with the workspace note in its tooltip, Templates, Profiles, a filled Save that is insensitive while nothing changed, and the “…” menu: gallery, Theme group, place on another monitor, workspace/monitor profiles, hotkeys, place the active window, Clear all… with a confirmation); one sidebar (zone list with an identity dot, name and key, the inspector, a one-line `N % × M % · X × Y px`); one line of status; the empty-inspector hint behind `set_no_show_all` so `show_all()` cannot resurrect it |
| `zone_theme.py` | neutral `DEFAULT_COLORS` (measured fg 15.3:1, muted 6.6:1, accent 3.7:1, white on accent 4.58:1); a wallpaper palette keeps the neutral chrome and takes only the accent; zone hues are identity only (list dot, picker, HUD); canvas zones `fg` 4 % with a `border` outline, the selection `accent` 10 % + 1.8 px; `contrast_text` threshold `0.18`; `.dim-label` inside a selected row follows the selection colour |
| `docs/specs/theme.md`, `docs/specs/zone-editor.md` | tokens, the hue derivation (0.56/0.58), the editor's landing table, the sheet contract, the wallpaper bridge; the editor spec translated and rewritten |
| `README.md`, `README.ru.md`, `README.zh.md` | §3.2 re-mapped to the chip, the “…” menu and the sidebar inspector |
| `po/*` | nine new strings plus `Layout` in `ru`/`zh_CN`, fuzzy matches cleared |
| `site/assets/editor.png`, `picker.png`, `demo.gif` | re-rendered by `tools/shoot-screens.sh` and `tools/record-demo.sh` from the neutral palette |

## What shipped — #54, the gallery cards

The cards in `gallery/*.conf` were Russian data inside an English dialog. They cannot be `_()`
literals: `GRIDSNAP_GALLERY` may point at a catalogue of one's own, and a card is a line of a
`.conf`. The chain that keeps them translated is:

| Piece | Role |
|---|---|
| `gallery/*.conf` | the eight cards in English: comments, `title`, `description` |
| `zone_config.gallery_sources()` | the cards worded exactly as the files do, untranslated |
| `zone_config.gallery_layouts()` / `load_gallery_layout()` | every card through `_()`; an empty title never reaches gettext (the empty `msgid` is the catalog header); the menu sorts by the **source** title, so it does not reshuffle with the language |
| `tools/gallery-pot.py` | writes the cards as `.po` entries `#: gallery/<id>.conf` |
| `Makefile` `pot` | `xgettext` for the code, `tools/gallery-pot.py` for the cards, `msgcat --use-first --no-wrap` merges the two |
| `po/ru.po`, `po/zh_CN.po` | sixteen entries — the former Russian card text for `ru`, fresh translations for `zh_CN` |

`profile gallery` joined the commands `tests/test_i18n_catalogs.py` runs under `en` and `ru`, so a
Russian card creeping back into the English output fails a test.

## Decisions

1. **The chrome is neutral by contract, not by taste.** The wallpaper palette keeps `bg`, `panel`,
   `card` and `border` and takes only the accent; a zone hue appears only as an identity dot. The
   violet cast on every surface was the rejected look.
2. **One header and one sidebar, not a toolbar.** The twelve controls were the density the owner
   rejected; everything off the common path went into the “…” menu, where a menu item is a sentence
   rather than an icon to decode.
3. **Gallery text stays in the data files.** Moving it to a Python map would have made `xgettext`
   happy and split the card across two sources of truth. Reading the data at `make pot` time keeps
   one source and costs one helper script.
4. **Sort by the source title.** A translated title would have made the gallery order depend on the
   interface language for cards that share an `order`.
5. **The empty title never reaches gettext.** In a catalog the empty `msgid` is the header, so
   `_(title)` with an empty string returns the whole header block — the fallback to the layout id
   happens after translation.

## Verification

```
$ make check
Ran 1518 tests in 18.220s

OK (skipped=9)

$ make lint
All checks passed!

$ make check-i18n
msgfmt ru
msgfmt zh_CN
собраны каталоги в locale/
каталоги в порядке

$ GRIDSNAP_LANG=en python3 zone-tiler.py profile gallery | head -1
two-columns	Two columns	zones: 2	Two equal full-height columns: the main document on the left, the second one on the right.

$ GRIDSNAP_LANG=ru python3 zone-tiler.py profile gallery | head -1
two-columns	Две колонки	зон: 2	Две равные колонки на всю высоту: слева основной документ, справа второй.

$ GRIDSNAP_LANG=zh_CN python3 zone-tiler.py profile gallery | head -2
two-columns	两栏	区域：2	两个等高的整列：左侧主文档，右侧第二个文档。
main-stack	主窗口 + 堆叠	区域：4	左侧一个大窗口，右侧三个堆叠窗口：一边处理文档，一边查看参考资料。
```

CI green on both pull requests: `45990192465` (#53) and `45990745378` (#54), five jobs each.

The editor itself was not re-opened after the merge — the GTK surface is the same code that
rendered the stills in #53; the gallery dialog calls the same `gallery_layouts()` the CLI check
above exercises.

## Next

- The rest of `docs/**` and the site are still Russian; translate file by file as they are touched.
- A native proofread of the Chinese catalog (now including the gallery cards) is still open.
- The Wayland backend and the Debian archive are unchanged by this wave.

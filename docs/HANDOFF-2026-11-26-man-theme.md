# Wave: the man page documents `theme.conf`

Closes the last item of `docs/HANDOFF-2026-11-26-theme-refresh.md`: `theme.conf` was documented in
`README.md` and `docs/specs/theme.md` only, while `man gridsnap` — the page a user actually reads —
knew nothing about the palette.

## What shipped

| Where | Change |
|---|---|
| `man/gridsnap.1` | `.SS theme.conf` under CONFIGURATION: what the file feeds, `[theme]`/`[colors]`, the colour tokens, the five presets, `radius`/`font`, the wallpaper action and the daemon refresh, the “no window decorations” limit; a `FILES` entry next to `zones.conf` |
| `po/man/gridsnap.1.pot` | rebuilt by `make man-pot`, 228 entries |
| `po/man/ru.po`, `po/man/zh_CN.po` | 8 new entries translated (the `.SS`, five paragraphs, the `FILES` path and its description); the `FILES` path entry was fuzzy against `templates/*.conf` — the fuzzy mark is gone |
| `docs/specs/theme.md` | one sentence pointing at the man page |

## Decisions

1. **No new page.** The package ships one page and seven `.so` aliases (see `docs/specs/man-i18n.md`);
   `theme.conf` is a configuration file, so it belongs in CONFIGURATION, not in a page of its own.
   A separate page would add two translations and an eighth alias for no extra reach.
2. **The path entry keeps an identical msgstr.** `I<~/.config/gridsnap/theme.conf>` translates to
   itself: the filename is not translatable, but po4a makes it a message, and leaving `msgstr` empty
   fails the completeness test.
3. **Two old Russian entries were reflowed.** `msgmerge` (run by `make man-pot`) wrapped two long
   `focus` translations; the text is unchanged, the diff is line breaks only. `make man-pot` is
   idempotent afterwards (same diff hash).
4. **No new limit checked.** The page documents no new `--flag`, so the key-vs-`--help` comparison in
   `tests/test_man_i18n.py` is untouched; the new `.SS` is extra, and the test only requires every
   section `zone_config` knows to exist.

## Verification (raw results)

```
$ make check-man-i18n
po4a --no-update --keep 0 "man/po4a.conf"
cp "man/gridsnap.1" "man/build/en/man1/gridsnap.1"
собраны страницы в man/build/
Ran 14 tests in 1.390s
OK
man-страницы в порядке
```

* `make man-pot` twice — the second run leaves `po/man` byte-identical (same diff hash).
* Built pages rendered through `MANPATH=man/build/<lang>` with `LC_ALL=ru_RU.utf8` and
  `LC_ALL=zh_CN.utf8`: the new section reads back in both languages, the Chinese page keeps the
  `\:` break points placed by `tools/man-cjk.py` and fits the column; `man --warnings` is clean
  (the check target runs it).

## Next

1. Optional: the daemon could rewrite the generated GTK controls theme (`~/.themes/Gridsnap`) with a
   new wallpaper palette; the switch in Cinnamon Settings stays the user's action.
2. The Chinese page remains machine-translated; a native proofread is still open (as for the UI).

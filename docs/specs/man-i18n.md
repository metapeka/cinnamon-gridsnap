---
id: man-i18n
type: submodule-design
status: active
title: "Submodule: localisation of man pages"
parent: gridsnap-architecture
depends-on: [zone-i18n]
---

_Described together with the implementation (the man translation wave); rewritten for the language
inversion (wave 8). Not yet reviewed by the owner._

## Responsibility

Localisation of the GridSnap man pages and the layout of their installation. Man pages are **not a
gettext domain**: `.mo` does not fit here, because man-db picks a page by the locale from the
`/usr/share/man/<language>/man1/` directory rather than from a translation catalog. So the page has
three languages (as many as the interface), and `po4a` builds them.

Files:

| What | Where |
|---|---|
| Source (English, edited by hand) | `man/gridsnap.1` |
| Seven aliases | `man/gridsnap-tiler.1`, `…-daemon`, `…-editor`, `…-picker`, `…-hotcorners`, `…-ctl`, `setup-hotkeys.1` |
| po4a configuration | `man/po4a.conf` |
| Template | `po/man/gridsnap.1.pot` |
| Catalogs | `po/man/ru.po`, `po/man/zh_CN.po` (there is no `en.po` catalog) |
| Built pages | `man/build/<language>/man1/*.1` (in `.gitignore`) |

All translation catalogs live under `po/` — next to the gettext domains, so that what is translatable
sits in one place. Catalogs (the translation source) are committed, built pages are not — exactly as
`.po` versus `.mo`.

## Why po4a, and why the English page is not rebuilt

`xgettext` does not parse roff markup, so the man translation needs its own tool. `po4a` is used
(0.73 in Debian): it is the only one that makes `.po` straight out of roff and builds the page back,
and it is also the Debian standard for man pages.

But `po4a-translate` **reflows roff**: it rebuilds paragraphs to the column width, replaces `.B x` with
`\fBx\fP`, adds a space to `.TP `, escapes hyphens. Measured on this page: an empty catalog gives 44935
bytes against 44400 for the source and hundreds of differing lines. Hence the decision:

- `ru` and `zh_CN` **are built** by po4a — these are translations, so reflowing is normal for them;
- `en` **is copied** from `man/gridsnap.1` as it stands, because it is the source language.

Before the inversion (wave 8) the source language was Russian and the Russian page was the copy. The
inversion moved the source to English: the English text was the `po/man/en.po` translation (itself
po4a-formatted), and it became `man/gridsnap.1`; the former Russian source became the `ru` catalog. The
cost is accepted and written down: the Russian page is now reflowed by po4a and is no longer “the
hand-edited source” — its text is unchanged, its roff layout is po4a's, exactly as the English page's
used to be.

## Default language and aliases

The language rule is the same as for the interface (`zone-i18n`): English by default, `zh*` — Chinese,
`ru*` — Russian. So the **English** page goes into `/usr/share/man/man1/` (that is what `LANG=C` and any
locale without a translation get), while the Russian and Chinese ones go into `/usr/share/man/ru/man1/`
and `/usr/share/man/zh_CN/man1/`.

The seven aliases are not translated: they are one `.so man1/gridsnap.1` line each. Man-db resolves it
**within a language** (verified on man-db 2.13: under `--locale=ru` the Russian page opens from
`ru/man1/`), so the files are copied into all three catalogs unchanged.

## Break points for languages without spaces

groff does not break a line between hieroglyphs: CJK has no spaces, and without a space troff has no
break point. po4a floods a paragraph, so the Chinese page came out in lines of 190–254 columns, and
`man --warnings` poured out “cannot break line”. The cure is escaping with `\:` — a zero-width break
point: after it the page fits into 79 columns and there are no break warnings.

`tools/man-cjk.py` places them in the **built** page (the languages are in `MAN_CJK` in the `Makefile`)
rather than in the translation catalog: the translator writes ordinary prose, the build does the markup.
The rule is one: the point goes between two neighbouring characters of which **at least one** is CJK, if
there is no space between them. One side is enough — hieroglyphs and full-width punctuation sit tight
against Latin, so `GRIDSNAP_WINDOW、GRIDSNAP_ZONE` is one unbreakable piece, and it was exactly on such
lists (not on prose) that the page overflowed the column. A line junction also gets the point: troff
glues the lines of a paragraph. Roff control lines, comments, empty lines and foreign escape sequences
are left alone.

One warning does remain — “cannot adjust line”: troff cannot stretch a line without spaces, and it stays
short. The user does not see it (ordinary `man` prints no warnings), so the check threshold tolerates it,
while “cannot break line” is not tolerated.

## How to check the page

Only as a user would: a temporary `MANPATH` plus a locale. `man path/zh_CN/man1/…` gives a **different**
result from `man gridsnap` under `LC_ALL=zh_CN.UTF-8` — that is where a false alarm was caught before.
The locale matters for groff too: under `zh_CN.UTF-8` a hieroglyph counts as two columns, under `C` as
one.

The path is taken as `<tmp>/usr/share/man` rather than a short `<tmp>`: man-db enables the groff language
macros only for that layout, and without break points the page gives 254 columns under a short path
against 193 under a real one. A check that does not account for this is green on a broken page — that is
how it was: a gate with a short path let through a variant without the identifier-list fix.

## Building and installing

- `make man-pot` — rebuild the template from `man/gridsnap.1`; po4a pulls the new `msgid`s into the
  catalogs as well (`msgmerge`).
- `make man` — build `man/build/<language>/man1/`: `ru`/`zh_CN` through `po4a --no-update --keep 0`,
  `en` as a copy of the source, the aliases as copies into all three languages. `--keep 0` is there so
  that a page is written even with an incomplete catalog: completeness is checked by a separate target
  rather than by a silently missing file.
- `install.sh`, `make deb`, the stock `debian/` and `PKGBUILD` install the three languages into their
  catalogs; the release `tar.gz` packs `man/build` into itself — `install.sh` from the archive installs
  what is already built and does not need `po4a`. `install.sh` from a checkout without a build falls back
  to the English source in `man1/` and says so.
- `po4a` is a build dependency (`Build-Depends` in `debian/control`, `makedepends` in `PKGBUILD`, a
  separate install step in CI).

## Checks

Completeness and agreement with the code are held by a test and a target, not by discipline:

- `tests/test_man_i18n.py` (no po4a, no GTK):
  - every `msgid` of the template is translated in `ru.po` and `zh_CN.po`, with no extra entries and no
    `fuzzy` — read by their own `zone_i18n.parse_po`;
  - **the page has not fallen behind the code**: the sets of long `--…` keys in `man/gridsnap.1` and in
    the `--help` of the seven commands match; every `zones.conf` section that `zone_config` knows has its
    own `.SS`; the commands and subcommands in the EXAMPLES section are real; the `profile` actions in
    the page are exactly `PROFILE_ACTIONS` from `zone-tiler.py` (without this comparison a new `profile`
    subcommand silently drops out of the man page: that is how the gallery dropped out);
  - `man --warnings` does not complain about the source; if `make man` has already run, the roff macro
    skeleton of the translated pages matches the source, the English page is byte-for-byte equal to the
    source, and the aliases contain `.so man1/gridsnap.1`;
  - the built pages are checked through a temporary `MANPATH` and a locale: under `C` the English page
    opens, under `ru_RU.UTF-8` the Russian one, under `zh_CN.UTF-8` the Chinese one; each fits the column
    (a hieroglyph counts as two) and leaves no “cannot break line”;
- `make check-man-i18n` — the same plus building the pages and `cmp` of the English page with the source.
  CI runs it in the job “Translation catalogs (gettext and po4a)”; that job also installs `locales` and
  generates `zh_CN.UTF-8` and `ru_RU.UTF-8` (without them the language checks are skipped). There too
  `make man-pot` must not change `po/man/` — otherwise an edit of the source forgot to rebuild the
  template and the translation silently fell behind.

## Invariants

- There is no `en` catalog: the English page is the source as it stands.
- A translated page does not change the macro skeleton of the source: not a single `.TP`/`.PP`/`.SH`/`.RS`
  is lost or added.
- The page is installed by language; the English one is what a locale without a translation gets.
- `po4a` is needed only at build time: a package installed from the archive does not require it.

## Known limitations

- The Chinese page is machine-translated and has not been proofread by a native speaker (as is the
  Chinese interface).
- `debian/control` (the package description) is still Russian: to be decided in the docs wave.
- There is still one page for the whole package: `gridsnap.1` describes all seven commands, and the
  seven pages are its `.so` aliases.
- The Russian page is now reflowed by po4a (see “Why po4a”): the text is the former hand-edited source,
  the roff layout is generated.

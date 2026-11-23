---
id: zone-i18n
type: submodule-design
status: active
title: "Submodule: interface localisation zone_i18n"
parent: gridsnap-architecture
references: [man-i18n]
---

_Described together with the implementation (wave 5); rewritten for the language inversion (wave 8).
Not yet reviewed by the owner._

## Responsibility

`zone_i18n.py` owns the whole choice of the GridSnap interface language and the loading of translation
catalogs. A pure module: no X, no GTK, no `gi`. It is imported by the editor, the picker, the tab strip,
the hot corners watcher, the tiler, the daemon, the socket client, the hotkey installer and the config
layer, so the language rule lives in exactly one place and cannot drift between processes.

Public surface: `_` / `translate`, `language`, `set_language`, `normalize_language`, `resolve_language`,
`catalog`, `parse_po`, `unescape_po`, `module_dir`, `localedirs`, `po_dir`, `mo_path`, `po_path`,
`reset_cache` and the constants `DOMAIN`, `APPLET_DOMAIN`, `SOURCE_LANGUAGE`, `DEFAULT_LANGUAGE`,
`LANGUAGES`, `LANGUAGE_ALIASES`.

## The source language is English

The strings in the code are English: that is the source language and the repository rule. There is
**no `en.po` catalog** — the English interface *is* the `msgid` as it stands. A missing catalog is not an
error: any untranslated string comes back as it is, in English.

Catalog layout:

| What | Where |
|---|---|
| Template for the Python tools | `po/gridsnap.pot` (from `xgettext`) |
| Catalogs for the Python tools | `po/ru.po`, `po/zh_CN.po` |
| Template and catalogs of the applet | `po/gridsnap@metapeka/gridsnap@metapeka.pot`, `ru.po`, `zh_CN.po` |
| Built `.mo` files | `locale/<language>/LC_MESSAGES/{gridsnap,gridsnap@metapeka}.mo` (in `.gitignore`) |

Before the inversion (wave 8) the source language was Russian: the Russian text lived in the `msgid`
and the English one in `po/en.po`. The inversion moved the English text into the code and the Russian
one into `po/ru.po`; the Russian strings are the same text as before, now a translation.

## Choosing the language

The order is the first non-empty value:

1. `GRIDSNAP_LANG` — an explicit override (tests, “I want Russian right now”);
2. `LC_ALL`; 3. `LC_MESSAGES`; 4. `LANG`.

The value is parsed by the prefix before `_`/`-` and without the encoding modifier
(`ru_RU.UTF-8@euro` → `ru`). `ru*` → the `ru` catalog, `zh*` (both `zh_CN` and `zh_TW`) → `zh_CN`,
**everything else** — `en`, `de`, `fr`, `C`, `POSIX`, junk — → English, which is also the source
language and the default. `DEFAULT_LANGUAGE` and `SOURCE_LANGUAGE` are both `en`, and
`LANGUAGE_ALIASES` maps `zh` to `zh_CN`.

`LANGUAGE` is deliberately **not** read: the rule must be one and explicit, and `GRIDSNAP_LANG`
overrides everything. The price of that decision: the applet differs, because it goes the standard xlet
way and knows nothing about `GRIDSNAP_LANG` (see “The Cinnamon applet”).

`set_language(code)` overrides the choice in the process (`None`/`""`/`"auto"` clears the override);
`language()` returns `ru`, `zh_CN` or `en`.

## Catalog lookup and running from a checkout

Lookup order:

1. the built `.mo` in the catalogs: `$GRIDSNAP_LOCALEDIR`, `<module dir>/locale`,
   `<module dir>/../../share/locale` (for `/usr/lib/gridsnap` that is `/usr/share/locale`, for
   `~/.local/lib/gridsnap` — `~/.local/share/locale`);
2. if there is no `.mo` — `po/<language>.po` next to the module (a small parser of its own for
   `msgid`/`msgstr`: multi-line strings, escapes, the header, `#, fuzzy`, no plural forms).

That way the owner works straight from a checkout without building anything, while an installed package
uses the standard `.mo`. A catalog is read lazily and cached per language.

## Wrapping strings

One rule for the whole code base: **every message is one literal inside `_()`**, with substitutions as
`{}` fields and `.format(...)`. An f-string or a concatenation of pieces does not go inside `_()`:
`xgettext` would not see it, and a translation inside a piece would break the word order. Wrapped are
also `--help`/`description`/`epilog` of argparse and the daemon journal.

Not translated (marked with `# i18n: do not translate` if the string contains anything non-ASCII):
docstrings, comments, regular expressions, config keys and the headers the code writes into
`zones.conf` and profiles. That is file content, not interface text.

A name trap: in a function where `_` is already taken by a throwaway variable (`for _ in ...`), `_`
becomes local and `_(...)` fails with `UnboundLocalError` — rename the throwaway variable.

## The Cinnamon applet — a domain of its own

The applet is not Python but an xlet: it gets its translation through the standard
`Gettext.bindtextdomain(uuid, dir)`, so it has **its own domain name** `gridsnap@metapeka` and its own
`.mo`. The applet looks for the catalog in `~/.local/share/locale` — where `install.sh` puts it with
the default `~/.local` prefix. The applet language is the locale of the Cinnamon session;
`GRIDSNAP_LANG` does not affect it.

The applet strings are extracted with `xgettext --language=JavaScript`; the strings are wrapped in `_()`
just as in Python. The first line of `zone-tiler.py list` (“monitor N work area: x,y WxH”) is translated
too, so the applet parses **numbers** in it (`N: x,y WxH`) rather than words — otherwise the applet menu
would break in Russian and Chinese.

## Building and installing

- `make pot` — rebuild both `.pot` files from the code (`xgettext --keyword=_`); `make mo` — build the
  four `.mo` files through `msgfmt --check`; `make check-i18n` — both of those plus comparing the
  templates with the code by `msgid` set and checking that there is no `en` catalog. `check-i18n` runs
  in CI.
- `.mo` files are installed into `<prefix>/share/locale/<language>/LC_MESSAGES/`: `install.sh` (and
  removed by its `--uninstall`), `make deb`, the stock `debian/` and `PKGBUILD`. The release `tar.gz`
  packs `locale/` into itself — otherwise `install.sh` from the archive would have nothing to install.
- The Russian translation is the former source language, so it reads like a native text; the Chinese one
  is machine-made and has not been proofread by a native speaker (a note in the README and on the site).
  English is the language of the source, not a “second translation”.

## Checks

Completeness and wrapping are held by tests, not by discipline:

- `tests/test_zone_i18n.py` — the module contract: locale parsing, the choice order, `GRIDSNAP_LANG`
  beating the locale, `.mo` beating `.po`, a missing catalog not being an error, `.po` parsing;
- `tests/test_i18n_catalogs.py` — every `msgid` of the template has a non-empty `msgstr` in `ru` and
  `zh_CN` (and the other way round: no extra entries) for both domains, plus under `GRIDSNAP_LANG=en`
  the commands print without a single Cyrillic letter and under `ru` with Cyrillic;
- `tests/test_i18n_wrapped.py` — not a single user-visible string outside `_()`; **all** application
  modules are checked (including `zone_geom`, `zone_ipc` and `make-icon`), not only the ones that had
  strings when the wrapping was introduced: that is how a forgotten string would otherwise stay
  untranslated;
- `tests/i18n_env.py` sets `GRIDSNAP_LANG=en` for the tests, and the smoke scenarios do the same
  themselves: the tests check behaviour against the English messages and do not depend on the machine
  locale or CI.

## Invariants

- No X, no GTK, no config: the module imports without a display (a project invariant).
- A missing catalog, a broken `.mo` and a missing entry are not errors: the source text is returned.
- The language comes from the process environment; an override applies to the whole process.
- The English interface needs no catalog file at all.

## Known limitations

- Man pages are translated by a separate pipeline (po4a, `docs/specs/man-i18n.md`), not by this module:
  `.mo` does not fit there, man-db picks a page by language directory.
- `debian/control` (the package description) is still Russian: to be decided in the docs wave.
- `GRIDSNAP_LANG` does not affect the applet — it follows the locale of the Cinnamon session.
- The Chinese translation is machine-made and has not been proofread by a native speaker.

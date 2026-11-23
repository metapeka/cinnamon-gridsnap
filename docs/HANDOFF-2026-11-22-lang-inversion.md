# Handoff: wave “language inversion — English is the source, Russian is a catalog” (22.11.2026)

The previous handoff is `HANDOFF-2026-11-22-profile-nits.md` (the `--json` help, the extra positional
argument, the “ё” check). This one is the state at the end of the next session; nothing from the
previous session carries over into a new chat.

**This file is in English on purpose**: the repository language became English in this wave (see
`AGENTS.md`). The rest of `docs/` is still Russian and is translated as it is touched.

## Where things are

| What | Where |
|---|---|
| Project (the live code) | `/home/db1/thinkrail/gridsnape`, branch `main` (after `git pull` — `4a3cef1` plus the commit with this handoff) |
| Repository on GitHub | `metapeka/cinnamon-gridsnap` |
| Merged PR of this wave | **#51 “Invert the language: English is the source, Russian is a catalog”**, merge commit `4a3cef1` |
| Wave branch | `lang-inversion` — merged, checked out in this workspace (`workspace-4`), to be deleted with the workspace |
| Version | unchanged — `0.2.0` |
| Rules for agents | `AGENTS.md` at the root — **now in English**, start there |
| Checks | unit tests — **1431 OK** (9 skipped); `tests/smoke*.sh` — **13**, all green; CI — 5 jobs, green (PR run `45089816075`) |
| Watchers | `tools/ci-watch.sh <run-id>` (take the id from `gh run list`), `tools/worker-watch.sh <dispatch-id>` |
| Worker tasks of this wave | `.thinkrail/context/worker-comments-a.md`, `-b.md` (not committed) |

## What was done (PR #51, nine commits)

**1. The language model (`zone_i18n.py`).** `SOURCE_LANGUAGE = DEFAULT_LANGUAGE = "en"`,
`LANGUAGES = ("ru", "zh_CN")`, `LANGUAGE_ALIASES = {"zh": "zh_CN"}`. `normalize_language` resolves `ru*`
to the Russian catalog, `zh*` to Chinese and **everything else** (`en`, `C`, junk) to the source strings.
A missing catalog, a broken `.mo` or a missing entry are still not errors: the source text comes back —
now in English.

**2. The catalogs.** `po/ru.po` (and the applet's `po/gridsnap@metapeka/ru.po`) is built by swapping the
old `en.po`: the English text became the `msgid`, the Russian one the `msgstr`. `po/zh_CN.po` and the
applet's Chinese catalog were rebuilt with English `msgid`s, keeping the Chinese text. `po/en.po` and the
applet's `en.po` are deleted. Language lists moved with them: `Makefile` (`LANGS := ru zh_CN`, and
`check-i18n` now asserts that **no `en` catalog** exists), `install.sh`, `debian/install`, `PKGBUILD`,
and the package assertions in `.github/workflows/ci.yml`.

**3. The code strings.** 644 wrapped literals in all modules were translated by a one-off script driven by
the old `en.po` map (every literal was in the map; there was no non-literal `_()` argument). No Cyrillic
literal is left inside `_()`. The applet (`applet.js`) was translated by the same rule.

**4. The man pages.** `man/gridsnap.1` is now the English page (the former `po/man/en.po` translation,
with a rewritten comment header), the Russian text moved into `po/man/ru.po`, the Chinese catalog was
rebuilt against the English `msgid`s, and `po/man/en.po` is gone. `po4a.conf` and `make man` build
`ru`/`zh_CN`, while the English page is copied from the source; `check-man-i18n` `cmp`s **that** page with
the source. `tests/test_man_i18n.py` follows the new model.

**5. The tests.** Expected substrings in 20 files are English, fake messages and daemon replies in the
fixtures too, and `tests/i18n_env.py` pins `GRIDSNAP_LANG=en`. `test_i18n_catalogs` checks the `ru`/`zh_CN`
catalogs and that `en` prints no Cyrillic while `ru` does.

**6. The rules and the docs that had to follow.** `AGENTS.md` rewritten in English with the language rule
and the list of known Russian debt; `docs/specs/zone-i18n.md` and `docs/specs/man-i18n.md` rewritten in
English for the new model; the three READMEs have their language facts corrected (their own language is
unchanged).

**7. Comments and docstrings — seven modules of seventeen.** Two Orca workers translated
`zone-tabstrip.py` (worker A) and `zone_geom.py`, `zone_picker_nav.py`, `zone_reflow.py`,
`zone_tabstrip_nav.py`, `zone_units.py`, `make-icon.py` (worker B). Both workers then **stalled** (see
the traps); their work was committed, merged and verified (`compileall`, no Cyrillic left in those files).
The rest is the next wave.

**8. `tools/worker-watch.sh` defect fixed.** Its progress line contains “45m ago” and changes by itself
every minute, so `cur != prev` was always true and the stall branch never fired: two stuck workers sat
idle for 45 minutes while the watcher kept printing updates. Staleness now comes from each worker's
`lastActivity` age; verified with a shim (stale worker → exit 2 at once, settled worker → exit 0).

**9. CI fix.** The package-build job still grepped the built `.deb` for
`usr/share/locale/en/LC_MESSAGES/gridsnap.mo` — the catalog is `ru` now, so the job failed. Fixed, and the
stale comment above `check-i18n` corrected.

## Decisions made by the owner

- The whole source in English: code, comments, docstrings, man source, tests (not just user-visible
  strings).
- User-visible strings read through while moving them (the old English was machine-made). Done: 593
  strings reviewed, no broken ones; three merged pairs are listed below.
- Commit messages and PRs in English from this wave on; conversation with the owner stays Russian.
- Documents and scripts that are still Russian stay as known debt, translated as they are touched.

## Traps of this wave

- **`msgmerge` without `--no-wrap` reflows the whole catalog**: one changed string gave +927 lines in
  `po/en.po`. For the gettext catalogs use `msgmerge --update --no-wrap --backup=none
  --no-fuzzy-matching`; for the po4a catalogs keep po4a's own wrapping.
- **po4a writes a long `msgid` as `msgid ""` plus continuation lines.** A parser that treats any empty
  `msgid` as the file header silently loses half the entries (it did: 110 of 220). Only the *first* such
  entry is the header.
- **po4a inserts a double space** where the source has a line break (two paragraphs in the man page). The
  English→Russian/Chinese map then misses those two entries (they fell back to English); fixed by matching
  against the old catalog by longest common prefix.
- **Three Russian strings shared one English translation**: “Экран”/“Монитор” → `Monitor`, “окна нет”/“нет
  окна” → `no window`, “нет”/“ни одного” → `none`. They are one message each now; the Russian variant was
  chosen explicitly (the editor's monitor frame, the action plan line, the picker captions) and the
  Chinese only differed for `no window` (`无窗口`).
- **`re.sub` expands `\n` in the replacement template** — use a lambda when the replacement is a literal
  with escapes (this bit the header rewrite of the catalogs).
- **`git show <path>` without the `HEAD:` prefix returned nothing here**; pass the full `HEAD:path` spec.
- **Mechanical replacements inside string literals break quoting**: after bulk-editing the tests, four
  files had nested double quotes and did not parse. Check with `ast.parse` after such a pass.
- **Stale build artifacts break tests**, as in earlier waves: `locale/*.mo` (rebuild with `make
  check-i18n`) and `man/build` (rebuild with `make check-man-i18n`) must be fresh before the unit tests.
- **The CI workflow holds language assumptions too.** The package job asserted `locale/en/...`; when the
  language lists move, grep `.github/workflows/ci.yml` as well.
- **A worker task must be small.** A 1600-line translation task in one go (six modules) stalled a worker:
  the `pi` processes were alive with 0 % CPU for 45 minutes, and Orca never woke the coordinator. For the
  next wave give **one file per task** and the recipe below.

### Recipe for translating the remaining comments (used successfully in this wave)

1. List the lines with Cyrillic and classify them: comments, docstrings, or data literals that must stay
   (the worker's `scan.py` did this through `ast` + `tokenize`).
2. Translate the comment/docstring lines **as a list of `(line number, new text)`**, without rewriting the
   whole file.
3. Patch by line number (the worker's `patchlib.py` also restored the backtick markup ``` `` ``` / `` ` ``).
4. Verify: `python3 -m compileall -q <files>`, `grep -c '[А-Яа-яёЁ]' <files>` → 0, and
   `git diff --stat` shows only the worker's files.

## How to check that everything is alive

```bash
cd /home/db1/thinkrail/gridsnape            # after git pull
env -u DISPLAY python3 -m unittest discover -s tests      # 1431 OK (9 skipped)
make check-version && make check-i18n && make check-man-i18n
python3 -m compileall -q .

# smoke scenarios, one at a time (each has its own display :89-:99)
for s in tests/smoke*.sh; do env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh "$s"; echo "$s rc=$?"; done

# the interface languages, by hand
GRIDSNAP_LANG=en python3 zone-tiler.py --help | head        # English (the source)
GRIDSNAP_LANG=ru python3 zone-tiler.py --help | head        # Russian (the catalog)
GRIDSNAP_LANG=zh_CN python3 zone-tiler.py --help | head     # Chinese
```

## What is left (deliberate debt, also listed in `AGENTS.md`)

- **Comments and docstrings** in the remaining modules: `zone-tiler.py`, `zone-daemon.py`,
  `zone_config.py`, `zone-picker.py`, `zone-hotcorners.py`, `zone-editor.py`, `window_backend.py`,
  `zone_ipc.py`, `zone_actions.py`, `setup-hotkeys.py`, `zone-tabs.py`, `gridsnap-ctl`, `zone_i18n.py`;
  and the comments in `tests/*.py` (≈1800 lines). One file per worker task.
- **Scripts**: `Makefile`, `install.sh`, `PKGBUILD`, `debian/**`, `tools/**`, `tests/smoke*.sh`
  (their messages are Russian; they keep working because they ask for the Russian catalog — switching them
  to `GRIDSNAP_LANG=en` and English expectations is part of the same follow-up).
- **Documents**: `SPEC.md`, `docs/**` (except the two language specs), `README.ru.md`, the site,
  `debian/control` (the package metadata is already English — the note in `zone-i18n`/`man-i18n` about it
  being Russian is stale and should be dropped when those specs are next touched).
- **The queue from `docs/ROADMAP.md` is unchanged**: accepting the three Wayland fixes on a stand, the
  Debian archive (external steps), the Cinnamon extension for Wayland-native windows, the applet in the
  panel, and re-running `setup-hotkeys.py --remove` after a Cinnamon restart.

## On the owner

- `git pull` in `/home/db1/thinkrail/gridsnape` when convenient: the live checkout is still on the older
  commit, and its `zones.conf` and `templates/` are untouched by this wave.
- Add the GridSnap applet to the panel (no Cinnamon restart needed), and after a Cinnamon restart run
  `python3 setup-hotkeys.py --remove` once (the case “Cinnamon knew the shortcuts since login”).

## Branches and workspaces

- The wave branch `lang-inversion` is merged; the remote branch is deleted.
- The worker workspaces `lang-comments-a` and `lang-comments-b` and their branches are removed
  (`worker-stop` + `orca-ide worktree rm --delete-branch`). The coordinator terminal
  `term_aa3ff725-b0c2-4656-864c-b803811500bf` (“COORD gridsnape: ё+man”) is still there and can be closed.

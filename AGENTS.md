# AGENTS.md — how work is done in this repository

Every agent reads this file automatically (pi/ThinkRail pick up `AGENTS.md`/`AGENTS.override.md`/
`CLAUDE.md` as project context). Only process rules live here; the product itself is described in
`SPEC.md`, `docs/architecture.md`, `docs/specs/`, and the plan in `docs/ROADMAP.md`.

Start a fresh session with `HANDOFF.md` at the root: snapshot, module map, gates, open work and the
traps. Per-wave history is in `docs/HANDOFF-<date>-<slug>.md`; do not rewrite it, add a new file.

## Language

**Everything in the repository is in English**: code, comments, docstrings, messages, tests, specs, man
pages, commit messages, pull requests, handoffs. Talk with the owner in Russian; anything written into
the repository is English.

An English source string is the rule for the code: user-visible messages are English literals inside
`_()`, and Russian and Chinese are catalogs (`po/ru.po`, `po/zh_CN.po`) — see `docs/specs/zone-i18n.md`.
A commit message is prose in the imperative (“Add man pages for the package commands”), without prefixes
like `feat:`.

Documents and scripts written before the language inversion are still Russian: `SPEC.md`, `docs/**`
(except `docs/specs/zone-i18n.md` and `docs/specs/man-i18n.md`, which the inversion rewrote),
`README.ru.md`, the site, `debian/control`, and the build, packaging and test scripts (`Makefile`,
`install.sh`, `PKGBUILD`, `debian/**`, `tools/**`, `tests/smoke*.sh`). They are translated as they are
touched, not in one sweep.

## What we do ourselves and what goes to a worker

Delegated (an Orca worker in its own worktree, its own branch):

- implementation in known files once the contract is fixed;
- routine work: packaging lists (`Makefile`, `install.sh`, `PKGBUILD`, `debian/install`), man pages,
  tests for described behaviour, reconnaissance with a written report.

Not delegated — kept in one context:

- the seams between parallel branches (two workers each pass their own tests and break the shared one);
- decisions and the “why”: specs, priorities, the config format;
- integration, review and checking other people's results;
- changes under ~15 minutes — the overhead is worth more than the work.

## Delegation: three mandatory rules

1. **The contract goes into the code first.** Before spawning a pair of workers the interface (signatures
   plus docstrings, or a failing test) is committed to `main`. A prose description in the two task
   messages is not enough: a mismatch at the seam is only found by running the tests after the merge.
2. **One owner per file.** At most two workers in parallel, and their files do not overlap. List in the
   worker's task what it may edit and what it may not.
3. **The coordinator integrates.** Worker branches are merged into a throwaway branch, the tests are run
   there, and only then is a pull request opened. One PR per wave, not per worker.

A worker's task must always say: why the task exists, which APIs already exist, what may and may not be
edited, which commands to run, and “if something is missing, do not fix it — write it in the report”.

## Verification

- A worker's report carries **raw command output**, not “verified”. The coordinator re-runs the gates
  anyway: `env -u DISPLAY python3 -m unittest discover -s tests` and one smoke scenario.
- Wording like “should work” counts as no verification at all.

### Quick loop

| Step | Command | Time |
|---|---|---|
| Units (always, `env -u DISPLAY` matters) | `make test` | ~25 s |
| Units + `compileall` (first CI job) | `make check` | ~30 s |
| Everything local, one entry point | `make verify` (delegates to `check`) | ~30 s |
| One pixel-for-pixel smoke on private Xvfb | `make smoke` | ~5–40 s |
| Every smoke, one by one | `for s in tests/smoke*.sh; do env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh "$s"; echo "$s rc=$?"; done` | minutes |
| Lint (undefined/shadowed names, `pyflakes` subset) | `make lint` (needs `uv tool install ruff`) | ~1 s |
| Version, catalogs, man pages | `make check-version check-i18n check-man-i18n` | ~10 s each |

Type checking is deliberately off (`pyrightconfig.json`: `typeCheckingMode: off`).
basedpyright is installed for navigation only — definition, references, hover, rename —
because the modules are untyped ctypes/GTK code loaded through `importlib`, and a type
pass reports mostly its own blind spots. Lint rules live in `ruff.toml` and carry no taste.

`make test` and the smokes never touch the live session: units run without `DISPLAY`, smokes raise their
own `:89`–`:99`. The root `zones.conf` is the owner's live layout — tests must pass their own `--conf`
in a temp directory, and `setup-hotkeys.py` tests must stay on a `keyfile` gsettings sandbox.

## Watchers (so we never wait in silence)

- `tools/ci-watch.sh --pr <N>` or `tools/ci-watch.sh <run-id>` — CI: watches job steps and exits with
  code 0 (green), 1 (failed), 2 (stalled: no progress longer than `--stall`, or `--deadline` passed).
  Right after `gh pr create` the `--pr` form sometimes finds no run yet — take the `databaseId` from
  `gh run list` instead.
- `tools/worker-watch.sh <dispatch-id>` (or `--all` — every worker that has not finished) — Orca workers:
  the same over `worker-show`; without it a stuck worker simply waits, because Orca wakes the coordinator
  only on a report, a failure or a lost terminal.

**Every spawn goes together with a watcher.** Orca cannot notice a stuck worker: silence is not a reason
for it to wake the coordinator (and rightly so — otherwise it would wake on every thinking pause). So
right after `worker-start` the watcher is put into a background command, and waiting ends on its signal
rather than on hope: code 0 — everyone reported, 1 — someone failed, 2 — silence longer than `--stall`.
Waiting for a worker without such a watcher is a process error, not an optimisation.

## Order of work on a change

1. Brainstorm: product questions to the owner, then the technical forks — and only then code.
2. A temporary task-spec in `.thinkrail/context/TASK-<slug>.md`; it is not committed.
3. Work (see the delegation rules), then `docs/specs/**` and the README catch up with the code — in the
   same pull request.
4. The PR body has three sections — `## Summary`, `## Changes`, `## Testing` (commands and their result,
   not “tests pass”). CI must be green; watch it with the watcher, not with your eyes.
5. After the merge — delete the branch and the Orca workspace. Merged work is not archived: the commits
   are already in `main`.

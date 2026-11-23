#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Man pages: po4a catalog completeness, the cross-check against the CLI, and what the user sees.

Man pages are not a gettext domain but localized pages: po4a makes a `.po` out of
roff, and the built pages are installed into `/usr/share/man/<language>/man1/`. This test
holds what would otherwise rot quietly:

* every `msgid` of the `po/man/gridsnap.1.pot` template is translated in `en.po` and
  `zh_CN.po`, there are no extra entries and no `fuzzy` ones — it is read through our own
  `zone_i18n.parse_po`, so the test needs neither `gettext` nor `po4a`;
* **the page has not fallen behind the code**: the sets of long `--…` keys in
  `man/gridsnap.1` and in the `--help` of the seven commands are the same; every `zones.conf` section
  that `zone_config` knows has its own `.SS`; the commands and subcommands of the
  EXAMPLES section are real; the `profile` actions in the page are exactly `PROFILE_ACTIONS` from
  `zone-tiler.py` (that is how the gallery used to drop out of the page unnoticed);
* **the built pages** (if `make man` has already run) behave as they do for
  the user: they are put into a temporary `MANPATH`, and `man gridsnap` under the right
  locale shows the right language, fits into the column and leaves no lines
  that troff could not break.

About `man path/zh_CN/man1/gridsnap.1`: that is not a valid way to check — man-db behaves
differently when it is given a file from a locale directory, and the measurement lies (we have already
caught a false alarm that way). So a temporary `MANPATH` and a locale, as in real life, and the path
is taken as `<tmp>/usr/share/man` rather than a short `<tmp>`: man-db enables
the groff language macros only for such a layout. The difference is measurable and decides
everything: without hyphenation points the page gives 254 columns under the short path and 193 under
the real one, while with points it is 79 in both.

The `zh_CN.UTF-8` locale is needed for the page to fit the column: without it groff
counts a CJK character as one column instead of two. If the locale is absent from the system, the language
checks are skipped (`make check-man-i18n` in CI installs them beforehand);
the width and the "cannot break line" checks run all the same.

Usage:
    python3 tests/test_man_i18n.py
    python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest

try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # run as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402
import zone_i18n as i18n  # noqa: E402

MAN = os.path.join(ROOT, "man")
SOURCE = os.path.join(MAN, "gridsnap.1")
BUILD = os.path.join(MAN, "build")
POT = os.path.join(ROOT, "po", "man", "gridsnap.1.pot")
LANGUAGES = ("ru", "zh_CN")
# The page language in man1/ is English (`MAN_DEFAULT` in the Makefile); it is also the
# source language, so there is no `en` catalog and the English page is a copy of the source.
BUILT_LANGS = ("en", "ru", "zh_CN")
# Language -> locale under which man-db must pick that very page.
LOCALES = {"en": "C", "ru": "ru_RU.UTF-8", "zh_CN": "zh_CN.UTF-8"}
ALIASES = (
    "gridsnap-tiler.1",
    "gridsnap-daemon.1",
    "gridsnap-editor.1",
    "gridsnap-picker.1",
    "gridsnap-hotcorners.1",
    "gridsnap-ctl.1",
    "setup-hotkeys.1",
)

# Launcher command -> file in the repository. `--help` works for all of them without a display,
# but three of them need `python3-gi` to import.
COMMANDS = (
    ("gridsnap-tiler", "zone-tiler.py"),
    ("gridsnap-daemon", "zone-daemon.py"),
    ("gridsnap-editor", "zone-editor.py"),
    ("gridsnap-picker", "zone-picker.py"),
    ("gridsnap-hotcorners", "zone-hotcorners.py"),
    ("gridsnap-ctl", "gridsnap-ctl"),
    ("setup-hotkeys", "setup-hotkeys.py"),
)
FILE_OF = dict(COMMANDS)

OPTION_RE = re.compile(r"--([a-z][a-z-]*)")
# A `profile` action in the page: `.B list`, `.B from\-gallery \fIID\fP` and the like.
PROFILE_ACTION_RE = re.compile(r"^\.B ([a-z][a-z0-9\\-]*)")
SECTION_RE = re.compile(r'^\.SS "?\[([^\]]+)\]"?', re.M)
# The page skeleton: structural macros, not font macros. po4a turns `.B x` into
# `\fBx\fP`, so font macros take no part in the comparison.
STRUCT_RE = re.compile(r"^\.(SH|SS|TP|PP|IP|RS|RE|nf|fi|TH|EX|EE)\b")
BUILD_NOTICE_RE = re.compile(r'^\.\\"')
# Page width: man-db typesets in 80 columns, the text is 78 minus the indent.
MAX_WIDTH = 90
# The only warning a CJK page may leave: troff cannot
# stretch a line without spaces, so it stays short. The user
# does not see it (a plain `man` prints no warnings), but an unbreakable
# line is a thing they do see: it runs past the terminal width.
TOLERATED_WARNING = "cannot adjust line"
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
CJK_RE = re.compile(r"[\u2e80-\u9fff\u3040-\u30ff\uff00-\uffef]")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def po_entries(path):
    """Catalog entries without the header; skips `fuzzy`, `msgctxt` and `msgid_plural`."""
    return {key: value for key, value in i18n.parse_po(read(path)).items() if key}


def source_body():
    """The source page without the `.\\"` comments (they carry `--warnings`)."""
    return "\n".join(
        line for line in read(SOURCE).splitlines()
        if not line.startswith('.\\"')
    )


def option_names(text):
    """Long `--…` keys; `\\-` in roff is an escaped hyphen."""
    return set(OPTION_RE.findall(text.replace("\\-", "-")))


def help_text(binary):
    """The `--help` output of a command, or `None` if it does not start without GTK."""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env["GRIDSNAP_LANG"] = "en"
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, binary), "--help"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def built_help():
    """The `--help` of every command that started; the skipped ones — as a separate list."""
    texts, skipped = {}, []
    for name, binary in COMMANDS:
        text = help_text(binary)
        if text is None:
            skipped.append(name)
        else:
            texts[name] = text
    return texts, skipped


def skeleton(text):
    """The sequence of structural macros of the page (without arguments)."""
    found = []
    for line in text.splitlines():
        if BUILD_NOTICE_RE.match(line):
            continue
        match = STRUCT_RE.match(line)
        if match:
            found.append(match.group(1))
    return found


def example_lines():
    """The lines of the `.nf` … `.fi` block from the EXAMPLES section."""
    lines = read(SOURCE).splitlines()
    try:
        start = lines.index(".nf")
        end = lines.index(".fi", start)
    except ValueError:  # pragma: no cover - the page is broken
        return []
    return [
        line.strip().replace("\\-", "-")
        for line in lines[start + 1:end]
        if line.strip()
    ]


def display_width(line):
    """The line width in columns: a CJK character takes two."""
    return sum(
        2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line
    )


def locale_available(name):
    """Whether the locale is present in the system (`locale -a` knows it under its own spelling)."""
    proc = subprocess.run(["locale", "-a"], stdout=subprocess.PIPE, text=True)
    normalize = lambda text: text.lower().replace("-", "").replace("_", "").replace(".", "")
    wanted = normalize(name)
    return any(normalize(line) == wanted for line in proc.stdout.splitlines())


class ManCatalogsTest(unittest.TestCase):
    """The po4a catalogs are complete and contain no garbage."""

    def test_pot_matches_source(self):
        self.assertTrue(os.path.exists(POT), "нет po/man/gridsnap.1.pot: make man-pot")
        entries = po_entries(POT)
        self.assertGreater(len(entries), 100, "в шаблоне подозрительно мало записей")

    def test_catalogs_cover_pot(self):
        pot = po_entries(POT)
        for lang in LANGUAGES:
            path = os.path.join(ROOT, "po", "man", f"{lang}.po")
            self.assertTrue(os.path.exists(path), f"нет {path}")
            catalog = po_entries(path)
            missing = sorted(set(pot) - set(catalog))
            empty = sorted(k for k, v in catalog.items() if not v.strip())
            extra = sorted(set(catalog) - set(pot))
            self.assertEqual(missing, [], f"{lang}.po: нет перевода этих строк")
            self.assertEqual(empty, [], f"{lang}.po: пустой msgstr у этих строк")
            self.assertEqual(extra, [], f"{lang}.po: лишние строки, их нет в шаблоне")

    def test_no_fuzzy(self):
        for lang in LANGUAGES:
            path = os.path.join(ROOT, "po", "man", f"{lang}.po")
            fuzzy = [
                n for n, line in enumerate(read(path).splitlines(), 1)
                if line.startswith("#,") and "fuzzy" in line
            ]
            self.assertEqual(fuzzy, [], f"{lang}.po: fuzzy на строках {fuzzy}")


class ManMatchesCliTest(unittest.TestCase):
    """The page has not fallen behind the code — neither in keys, nor in sections, nor in examples."""

    @classmethod
    def setUpClass(cls):
        cls.help_texts, cls.skipped = built_help()

    def test_options_match_cli_both_ways(self):
        """The keys of the page and the keys of `--help` are one and the same set.

        The comparison is two-way, so the GTK commands need `python3-gi`: without
        it (the unit CI job) both sides of the set are incomplete and the test is skipped,
        while a full run is done by `make check-man-i18n`.
        """
        if self.skipped:
            self.skipTest(f"без GTK не запускаются: {', '.join(self.skipped)}")
        man = option_names(source_body())
        real = set()
        for text in self.help_texts.values():
            real |= option_names(text)
        self.assertGreater(len(real), 20, "ни одна команда не ответила на --help")
        self.assertEqual(sorted(man - real), [],
                         "в man-странице есть ключи, которых нет у команд")
        self.assertEqual(sorted(real - man), [],
                         "у команд есть ключи, не описанные в man-странице")

    def test_config_sections_are_documented(self):
        known = set(zc.RESERVED_SECTIONS)
        known |= {
            zc.MOVE_SECTION, zc.FOCUS_SECTION, zc.TABS_SECTION,
            zc.WORKSPACES_SECTION, zc.MONITORS_SECTION,
            zc.ACTION_SECTION_PREFIX + "<name>",
        }
        known |= set(zc.BEFORE_KEYS) | set(zc.AFTER_KEYS)
        documented = set(SECTION_RE.findall(read(SOURCE)))
        self.assertEqual(sorted(known - documented), [],
                         "секции zones.conf нет в man-странице")

    def test_example_commands_are_real(self):
        actions = set(zc.load_zonetiler().PROFILE_ACTIONS)
        for line in example_lines():
            tokens = line.split()
            binary = FILE_OF.get(tokens[0])
            self.assertIsNotNone(binary, f"пример на неизвестную команду: {line!r}")
            text = self.help_texts.get(tokens[0])
            if text is None:
                continue  # the command does not start without GTK - skipping it
            # The subcommand is the second word; for `profile` it is the third, and its set
            # is defined not by argparse but by PROFILE_ACTIONS in zone-tiler.py.
            if len(tokens) > 1 and tokens[1] == "profile":
                if len(tokens) <= 2:
                    continue
                self.assertIn(tokens[2], actions,
                              f"нет действия profile {tokens[2]!r} (пример {line!r})")
                continue
            if len(tokens) <= 1 or tokens[1].startswith("-"):
                continue
            self.assertIn(tokens[1], text,
                          f"в --help {tokens[0]} нет подкоманды {tokens[1]!r} "
                          f"(пример {line!r})")

    def test_profile_actions_match_cli(self):
        """The `profile` actions in the man page and in the code are one and the same set.

        The list of `profile` actions is defined not by argparse but by `PROFILE_ACTIONS` in
        `zone-tiler.py`; without this cross-check a new subcommand quietly fails to get
        into the page (that is how the gallery dropped out). The page entries are `.TP` blocks with
        font macros (`\fBlist\fP`), so the name is taken up to the first `\fP`.
        """
        lines = source_body().splitlines()
        try:
            start = next(
                n for n, line in enumerate(lines) if line.startswith("\\fBprofile ")
            )
        except StopIteration:
            self.fail("в man-странице нет записи про profile")
        end = next(
            (n for n in range(start + 1, len(lines)) if lines[n].startswith(".SS ")),
            len(lines),
        )
        documented = set()
        for n in range(start + 1, end - 1):
            if lines[n].strip() != ".TP" or not lines[n + 1].strip().startswith("\\fB"):
                continue
            name = lines[n + 1].strip()[len("\\fB"):].split("\\fP", 1)[0]
            documented.add(name.replace("\\-", "-").strip())
        real = set(zc.load_zonetiler().PROFILE_ACTIONS)
        self.assertEqual(sorted(documented), sorted(real),
                         "действия profile в man-странице разошлись с PROFILE_ACTIONS")

    def test_source_renders_without_warnings(self):
        if shutil.which("man") is None:
            self.skipTest("нет man-db")
        proc = subprocess.run(
            ["man", "--warnings", "-E", "UTF-8", "-l", SOURCE],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.strip(), "", proc.stderr)


class BuiltPagesTest(unittest.TestCase):
    """The pages built by `make man`, as the user sees them.

    Without `make man` (the unit CI job, which has no po4a) the tests are skipped: the completeness
    of the catalogs they still check above, and the build is run by `make check-man-i18n`.
    """

    manpath = None
    root = None

    @classmethod
    def setUpClass(cls):
        cls.built = os.path.isdir(BUILD)
        if cls.built:
            # The path is exactly `<tmp>/usr/share/man`: under a short `<tmp>` man-db does not
            # enable the groff language macros and the page is formatted differently
            # than for the user (verified: without hyphenation points 254 columns
            # against 193 under the real path).
            cls.root = tempfile.mkdtemp(prefix="gridsnap-man-")
            cls.manpath = os.path.join(cls.root, "usr", "share", "man")
            for lang in BUILT_LANGS:
                sub = "" if lang == "en" else os.path.join(lang, "")
                target = os.path.join(cls.manpath, sub, "man1")
                os.makedirs(target, exist_ok=True)
                for name in os.listdir(os.path.join(BUILD, lang, "man1")):
                    shutil.copy(
                        os.path.join(BUILD, lang, "man1", name),
                        os.path.join(target, name),
                    )

    @classmethod
    def tearDownClass(cls):
        if cls.root:
            shutil.rmtree(cls.root, ignore_errors=True)

    def page(self, lang, name="gridsnap.1"):
        return os.path.join(BUILD, lang, "man1", name)

    def render_man(self, locale, *args):
        env = dict(os.environ)
        env["LC_ALL"] = locale
        env["MANPATH"] = self.manpath
        for key in ("LANG", "LANGUAGE", "LC_MESSAGES"):
            env.pop(key, None)
        return subprocess.run(
            ["man", *args], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60,
        )

    def test_all_languages_and_aliases_exist(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        for lang in BUILT_LANGS:
            self.assertTrue(os.path.exists(self.page(lang)), lang)
            for alias in ALIASES:
                path = self.page(lang, alias)
                self.assertTrue(os.path.exists(path), path)
                self.assertEqual(read(path).strip(), ".so man1/gridsnap.1", path)

    def test_structure_survives_translation(self):
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        expected = skeleton(read(SOURCE))
        self.assertGreater(len(expected), 50, "в источнике мало структурных макросов")
        for lang in BUILT_LANGS:
            self.assertEqual(skeleton(read(self.page(lang))), expected,
                             f"скелет {lang} разошёлся с источником")

    def test_english_page_is_the_source(self):
        """The English page is a copy of `man/gridsnap.1`, not a po4a build.

        Before the language inversion the copy of the source was the Russian page.
        """
        if not self.built:
            self.skipTest("нет man/build: сначала make man")
        with open(SOURCE, "rb") as src, open(self.page("en"), "rb") as en:
            self.assertEqual(en.read(), src.read(),
                             "английская страница должна быть копией man/gridsnap.1")

    def test_locale_chooses_the_page(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        expected = {
            "C": lambda text: not CYRILLIC_RE.search(text) and not CJK_RE.search(text),
            "ru_RU.UTF-8": lambda text: bool(CYRILLIC_RE.search(text)),
            "zh_CN.UTF-8": lambda text: bool(CJK_RE.search(text)),
        }
        for locale, ok in expected.items():
            if locale != "C" and not locale_available(locale):
                continue  # the locale was not generated - nothing to check
            proc = self.render_man(locale, "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            self.assertTrue(ok(proc.stdout), f"{locale}: man gridsnap показал не ту страницу")

    def test_pages_fit_the_column(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        for locale in LOCALES.values():
            if locale != "C" and not locale_available(locale):
                continue
            proc = self.render_man(locale, "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            width = max((display_width(line) for line in proc.stdout.splitlines()),
                        default=0)
            self.assertLessEqual(
                width, MAX_WIDTH,
                f"{locale}: строка шириной {width} колонок — troff не разорвал её")

    def test_no_unbreakable_lines(self):
        if not self.built or shutil.which("man") is None:
            self.skipTest("нет man/build или man-db")
        for locale in LOCALES.values():
            if locale != "C" and not locale_available(locale):
                continue
            proc = self.render_man(locale, "--warnings", "-E", "UTF-8", "gridsnap")
            self.assertEqual(proc.returncode, 0, f"{locale}: {proc.stderr}")
            bad = [
                line for line in proc.stderr.splitlines()
                if "warning" in line and "cannot break line" in line
            ]
            self.assertEqual(bad, [], f"{locale}: неразрывные строки: {bad[:3]}")


if __name__ == "__main__":
    unittest.main()

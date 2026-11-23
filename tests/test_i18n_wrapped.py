#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The interface strings are wrapped in `_()` — and a test checks that, not hope.

The rule of the localization wave: every user-facing string is one literal
inside `_()` (substitutions go through `.format(...)`), not an f-string and not a concatenation
of fragments: otherwise the string is not in the catalog, and translation breaks the word order.

The test parses the sources with `ast` and complains about Cyrillic in string
literals that are not an argument of `_()`/`translate`. The following do not count
as violations:

* docstrings (that is code documentation, not user-facing text);
* strings on a line marked ``# i18n: do not translate`` — regular expressions, config
  keys and headings that the code writes into files (they are not interface text).

Running:
    python3 tests/test_i18n_wrapped.py
    python3 -m unittest discover -s tests -v
"""

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The files with user-facing strings (the applet is JavaScript and has its own test).
# The list is all modules of the application, not only those that had strings when the
# wrapping was done: otherwise the next string added to a "quiet" module would stay
# Russian only in Russian (which is exactly what happened with `zone_geom.set_origin_text`,
# which shows in the editor status and in the demo).
SOURCES = (
    "window_backend.py",
    "zone-editor.py",
    "zone-picker.py",
    "zone-tabstrip.py",
    "zone-hotcorners.py",
    "zone-tiler.py",
    "zone-daemon.py",
    "zone_config.py",
    "zone_actions.py",
    "zone_geom.py",
    "zone_ipc.py",
    "zone_reflow.py",
    "zone_tabs.py",
    "zone_picker_nav.py",
    "zone_tabstrip_nav.py",
    "setup-hotkeys.py",
    "make-icon.py",
    "gridsnap-ctl",
)

# The mark "this string is not user-facing text" (a regex, a key, a file heading).
# The mark "the string is not user-facing text". The old Russian form lives on
# until the end of the inversion: the workers translating module comments remove it.
SKIP_MARK = ("i18n: do not translate", "i18n: не переводится")

CYRILLIC = re.compile(r"[\u0400-\u04ff]")

# The names that wrap a string: `_` from `zone_i18n` and the full `translate`.
WRAPPERS = ("_", "translate")


def is_cyrillic(text):
    """Whether the string contains Cyrillic (user-facing text, not an identifier)."""
    return bool(CYRILLIC.search(text))


def docstring_ids(tree):
    """The `id()`s of the string constants that are docstrings: module, class, function."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


def wrapper_arg_ids(tree):
    """The `id()`s of the constants standing as a direct argument of `_()`/`translate()`."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in WRAPPERS:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(id(arg))
    return found


def marked_lines(source):
    """Line numbers with the "do not translate" mark (1-based)."""
    return {n for n, line in enumerate(source.splitlines(), start=1)
            if any(mark in line for mark in SKIP_MARK)}


def unwrapped(source, path):
    """The list of violations: `(line, text, why)` — in line order."""
    tree = ast.parse(source)
    skip_doc = docstring_ids(tree)
    skip_arg = wrapper_arg_ids(tree)
    skip_mark = marked_lines(source)

    # The parent is needed to tell a plain string from a part of an f-string.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not is_cyrillic(node.value):
            continue
        if id(node) in skip_doc or id(node) in skip_arg:
            continue
        if any(line in skip_mark for line in range(node.lineno, node.end_lineno + 1)):
            continue
        if isinstance(parents.get(node), ast.JoinedStr):
            found.append((node.lineno, node.value, "f-строка: шаблон надо собрать литералом"))
        else:
            found.append((node.lineno, node.value, "строка не обёрнута в _()"))
    return sorted(found)


class TestStringsWrapped(unittest.TestCase):
    def test_every_user_string_is_wrapped(self):
        problems = []
        for name in SOURCES:
            path = os.path.join(ROOT, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, text, why in unwrapped(source, path):
                problems.append("%s:%d: %s — %r" % (name, line, why, text))
        self.assertEqual(
            problems, [],
            "\nОбёрнуты не все пользовательские строки:\n  " + "\n  ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()

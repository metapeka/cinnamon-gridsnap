#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The source language of the interface for the tests.

The strings in the code are English (the source language), and the `ru` and `zh_CN`
catalogs are layered on top; the language comes from the machine locale, and `LANG=ru_RU.UTF-8` means Russian.
The tests check behaviour rather than translation and compare against the English messages
straight from the code, so they need the source language — the same on any machine.

It is included with one line at the top of a module:

    import i18n_env  # noqa: F401  (the tests run in the source language)

The variable is not overwritten if `GRIDSNAP_LANG` is already set outside: that is how
`GRIDSNAP_LANG=ru python3 -m unittest discover -s tests` works, and so do the language
tests in `test_zone_i18n.py`.
"""

import os

os.environ.setdefault("GRIDSNAP_LANG", "en")

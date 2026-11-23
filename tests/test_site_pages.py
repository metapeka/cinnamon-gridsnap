#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the site pages (`site/`): three languages, the same anchors, live links.

The site is three independent HTML files (no build and no generator), a shared
`site/style.css` and images in `site/assets/`. Separate files per language drift
apart easily, so this is checked:

* `site/index.html` (English — it is also the Pages entry point), `site/index.zh.html`
  (simplified Chinese) and `site/index.ru.html` (Russian);
* the set of `id` is the same on all three pages and equals the expected one: a section
  that is on one page and forgotten on another is a translation that drifted apart;
* every page links to the two other versions;
* all local links and images exist, and anchor links lead to real `id`s;
* the pages have the right `lang` (en, zh-Hans, ru).

Running: `python3 -m unittest discover -s tests` or `python3 tests/test_site_pages.py`.
"""

import html.parser
import os
import unittest
from urllib.parse import urlparse


try:
    import i18n_env  # noqa: F401  (the tests run in the source language)
except ImportError:  # a run of the form `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SITE = os.path.join(ROOT, "site")

PAGES = ("index.html", "index.zh.html", "index.ru.html")
LANGS = {"index.html": "en", "index.zh.html": "zh-Hans", "index.ru.html": "ru"}
# The sections of the page: header, features, screenshots, install, documentation.
IDS = ("lang", "top", "features", "screenshots", "install", "docs")
EXTERNAL = ("http://", "https://", "mailto:", "//")


class PageParser(html.parser.HTMLParser):
    """Collects the `id`s, links and images of a page — without external dependencies."""

    def __init__(self):
        super().__init__()
        self.ids = []
        self.hrefs = []
        self.srcs = []
        self.lang = None
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "html" and "lang" in attrs:
            self.lang = attrs["lang"]
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag in ("img", "script") and attrs.get("src"):
            self.srcs.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet" and attrs.get("href"):
            self.stylesheets.append(attrs["href"])


def parse(name):
    parser = PageParser()
    with open(os.path.join(SITE, name), encoding="utf-8") as fh:
        parser.feed(fh.read())
    return parser


class SitePagesTest(unittest.TestCase):
    def test_pages_and_stylesheet_exist(self):
        for name in PAGES:
            self.assertTrue(os.path.exists(os.path.join(SITE, name)), f"нет site/{name}")
        self.assertTrue(os.path.exists(os.path.join(SITE, "style.css")), "нет site/style.css")

    def test_same_ids_in_every_language(self):
        found = {name: parse(name).ids for name in PAGES}
        for name, ids in found.items():
            self.assertEqual(
                list(ids), list(IDS), f"в site/{name} другой набор разделов: {ids}"
            )

    def test_every_page_links_the_other_two(self):
        for name in PAGES:
            hrefs = parse(name).hrefs
            for other in PAGES:
                if other == name:
                    continue
                self.assertIn(other, hrefs, f"site/{name} не ссылается на {other}")

    def test_lang_and_stylesheet(self):
        for name in PAGES:
            page = parse(name)
            self.assertEqual(page.lang, LANGS[name], f"site/{name}: неверный lang")
            self.assertIn("style.css", page.stylesheets, f"site/{name}: нет стилей")

    def test_local_links_and_images_exist(self):
        for name in PAGES:
            page = parse(name)
            for ref in page.hrefs + page.srcs:
                if ref.startswith(EXTERNAL):
                    continue
                path = urlparse(ref).path
                if not path:  # an anchor link: checked below
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(SITE, path)),
                    f"site/{name} ссылается на несуществующий {ref}",
                )
            for ref in page.hrefs:
                if ref.startswith(EXTERNAL) or not ref.startswith("#"):
                    continue
                self.assertIn(
                    ref[1:], page.ids, f"site/{name}: якорь {ref} ни на что не ведёт"
                )


if __name__ == "__main__":
    unittest.main()

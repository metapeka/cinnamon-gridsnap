#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты страниц сайта (`site/`): три языка, одни якоря, живые ссылки.

Сайт — три самостоятельных HTML-файла (без сборки и генератора), общий
`site/style.css` и картинки в `site/assets/`. Отдельные файлы на каждый язык
легко расходятся, поэтому проверяется:

* `site/index.html` (английский — он же вход Pages), `site/index.zh.html`
  (упрощённый китайский) и `site/index.ru.html` (русский);
* набор `id` одинаков во всех трёх страницах и равен ожидаемому: раздел,
  который есть на одной странице и забыт на другой, — это разошедшийся перевод;
* каждая страница ссылается на две другие версии;
* все локальные ссылки и картинки существуют, а ссылки-якоря ведут на реальные `id`;
* у страниц правильные `lang` (en, zh-Hans, ru).

Запуск: `python3 -m unittest discover -s tests` или `python3 tests/test_site_pages.py`.
"""

import html.parser
import os
import unittest
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SITE = os.path.join(ROOT, "site")

PAGES = ("index.html", "index.zh.html", "index.ru.html")
LANGS = {"index.html": "en", "index.zh.html": "zh-Hans", "index.ru.html": "ru"}
# Разделы страницы: заголовок, возможности, снимки, установка, документация.
IDS = ("lang", "top", "features", "screenshots", "install", "docs")
EXTERNAL = ("http://", "https://", "mailto:", "//")


class PageParser(html.parser.HTMLParser):
    """Собирает `id`, ссылки и картинки страницы — без внешних зависимостей."""

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
                if not path:  # ссылка-якорь: проверяется ниже
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

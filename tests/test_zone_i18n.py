#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты выбора языка и каталогов (`zone_i18n`) — без X, без GTK, без сборки.

Покрыто: разбор значения локали, порядок выбора языка (`GRIDSNAP_LANG` важнее
локали, локаль `ru*` — исходные строки, всё прочее — английский), переопределение
`set_language`, поиск каталога (`.mo` важнее `.po`, нет каталога — не ошибка),
разбор `.po` (многострочные строки, эскейпы, `#, fuzzy`, заголовок) и сам
перевод. Ни `msgfmt`, ни `xgettext` тестам не нужны: `.mo` собирается тут же
вручную — формат у него простой, а проверить приоритет `.mo` над `.po` нужно.

Запуск:
    python3 tests/test_zone_i18n.py
    python3 -m unittest discover -s tests -v
"""

import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_i18n as i18n  # noqa: E402

# Переменные, из которых выбирается язык. Каждый тест начинается с чистого
# окружения: иначе результат зависел бы от локали машины и от порядка тестов.
LANG_VARS = ("GRIDSNAP_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


def build_mo(entries):
    """Собрать `.mo` (формат GNU gettext) из `{msgid: msgstr}` — для теста.

    Формат: заголовок из 7 слов, две таблицы `(длина, смещение)` и склеенные
    строки с нулевым байтом в конце. Этого достаточно `GNUTranslations`.
    """
    header = {
        "": "Content-Type: text/plain; charset=UTF-8\nLanguage: en\n",
        "GridSnap": "GridSnap",
    }
    all_entries = dict(header)
    all_entries.update(entries)
    ordered = sorted(all_entries.items())

    ids = b""
    strs = b""
    id_table = []
    str_table = []
    base = 28 + 16 * len(ordered)
    for key, _value in ordered:
        # Длина в таблице — без завершающего нуля (так пишет GNU msgfmt); сам
        # нуль занимает место в потоке строк. Нулевая длина у пустого msgid —
        # это заголовок каталога, `GNUTranslations` опознаёт его именно так.
        raw = key.encode("utf-8")
        id_table.append((len(raw), base + len(ids)))
        ids += raw + b"\0"
    str_base = base + len(ids)
    for _key, value in ordered:
        raw = value.encode("utf-8")
        str_table.append((len(raw), str_base + len(strs)))
        strs += raw + b"\0"

    out = struct.pack("<7I", 0x950412DE, 0, len(ordered), 28, 28 + 8 * len(ordered), 0, 0)
    for length, offset in id_table:
        out += struct.pack("<2I", length, offset)
    for length, offset in str_table:
        out += struct.pack("<2I", length, offset)
    # GNU msgfmt оставляет в конце лишний нулевой байт; `GNUTranslations`
    # требует, чтобы строка кончалась *до* конца файла, поэтому повторяем.
    return out + ids + strs + b"\0"


class LanguageCase(unittest.TestCase):
    """Общая обвязка: чистое окружение и сброшенные кэши/переопределение."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in LANG_VARS:
            os.environ.pop(name, None)
        i18n.set_language(None)
        i18n.reset_cache()

    def env(self, **values):
        """Задать переменные языка (`None` — убрать переменную)."""
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class TestNormalize(LanguageCase):
    def test_russian_prefixes(self):
        for value in ("ru", "ru_RU", "ru_RU.UTF-8", "ru-RU", "ru_RU.UTF-8@euro"):
            self.assertEqual(i18n.normalize_language(value), "ru", value)

    def test_chinese_prefixes(self):
        for value in ("zh", "zh_CN", "zh_CN.UTF-8", "zh-Hans", "zh_TW", "ZH_cn"):
            self.assertEqual(i18n.normalize_language(value), "zh_CN", value)

    def test_everything_else_is_english(self):
        for value in ("en", "en_US.UTF-8", "de_DE.UTF-8", "fr", "C", "POSIX", "c.utf-8", "xx"):
            self.assertEqual(i18n.normalize_language(value), "en", value)

    def test_empty_is_none(self):
        for value in (None, "", "   ", "."):
            self.assertIsNone(i18n.normalize_language(value), repr(value))


class TestLanguage(LanguageCase):
    def test_default_is_english_without_environment(self):
        self.assertEqual(i18n.language(), "en")

    def test_locale_chain_order(self):
        self.env(LANG="zh_CN.UTF-8", LC_MESSAGES="ru_RU.UTF-8", LC_ALL="en_US.UTF-8")
        self.assertEqual(i18n.language(), "en")
        self.env(LC_ALL=None)
        self.assertEqual(i18n.language(), "ru")
        self.env(LC_MESSAGES=None)
        self.assertEqual(i18n.language(), "zh_CN")

    def test_override_wins(self):
        self.env(LANG="ru_RU.UTF-8", GRIDSNAP_LANG="en")
        self.assertEqual(i18n.language(), "en")

    def test_empty_override_falls_through(self):
        self.env(LANG="zh_CN.UTF-8", GRIDSNAP_LANG="")
        self.assertEqual(i18n.language(), "zh_CN")

    def test_set_language(self):
        self.env(LANG="zh_CN.UTF-8")
        self.assertEqual(i18n.set_language("ru"), "ru")
        self.assertEqual(i18n.language(), "ru")
        self.assertEqual(i18n.set_language(None), "zh_CN")
        self.assertEqual(i18n.set_language("auto"), "zh_CN")

    def test_set_language_unknown_is_english(self):
        self.env(LANG="ru_RU.UTF-8")
        self.assertEqual(i18n.set_language("de_DE.UTF-8"), "en")


class TestParsePo(LanguageCase):
    def test_simple_and_header(self):
        table = i18n.parse_po(
            '# комментарий\n'
            'msgid ""\n'
            'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
            '\n'
            'msgid "зона пуста"\n'
            'msgstr "zone is empty"\n'
        )
        self.assertEqual(table["зона пуста"], "zone is empty")
        self.assertNotIn("", table)

    def test_header_is_not_an_entry(self):
        table = i18n.parse_po(
            'msgid ""\n'
            'msgstr ""\n'
            '"Content-Type: text/plain; charset=UTF-8\\n"\n'
            '"Language: en\\n"\n'
        )
        self.assertEqual(table, {})

    def test_multiline_and_escapes(self):
        table = i18n.parse_po(
            'msgid "зона "\n'
            '"пуста"\n'
            'msgstr ""\n'
            '"zone\\n"\n'
            '"\\tis \\"empty\\""\n'
        )
        self.assertEqual(table["зона пуста"], 'zone\n\tis "empty"')

    def test_empty_msgstr_is_kept(self):
        table = i18n.parse_po('msgid "зона"\nmsgstr ""\n')
        self.assertEqual(table, {"зона": ""})

    def test_fuzzy_is_skipped(self):
        table = i18n.parse_po(
            '#, fuzzy\n'
            'msgid "черновик"\n'
            'msgstr "draft"\n'
            '\n'
            'msgid "готовое"\n'
            'msgstr "ready"\n'
        )
        self.assertEqual(table, {"готовое": "ready"})

    def test_plural_and_context_are_skipped(self):
        table = i18n.parse_po(
            'msgctxt "menu"\n'
            'msgid "зона"\n'
            'msgstr "zone"\n'
            '\n'
            'msgid "окно"\n'
            'msgid_plural "окна"\n'
            'msgstr[0] "window"\n'
            'msgstr[1] "windows"\n'
            '\n'
            'msgid "экран"\n'
            'msgstr "screen"\n'
        )
        self.assertEqual(table, {"экран": "screen"})

    def test_unescape(self):
        self.assertEqual(i18n.unescape_po(r"a\nb\tc\"d\\e"), 'a\nb\tc"d\\e')
        self.assertEqual(i18n.unescape_po(r"неизвестное \q"), r"неизвестное \q")


class CatalogCase(LanguageCase):
    """Временный каталог вместо настоящих `po/` и `locale/`."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        env = mock.patch.dict(os.environ, {"GRIDSNAP_LOCALEDIR": self.root})
        env.start()
        self.addCleanup(env.stop)
        # Своя сборка `locale/` в чекауте нашлась бы раньше временного каталога:
        # здесь ищем каталоги только там, где их положил тест.
        dirs = mock.patch.object(i18n, "localedirs", return_value=[self.root])
        dirs.start()
        self.addCleanup(dirs.stop)
        pdir = mock.patch.object(i18n, "po_dir", return_value=self.root)
        pdir.start()
        self.addCleanup(pdir.stop)
        i18n.reset_cache()

    def write_po(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_mo(self, lang, entries, domain="gridsnap"):
        path = os.path.join(self.root, lang, "LC_MESSAGES", domain + ".mo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(build_mo(entries))
        return path


class TestCatalog(CatalogCase):
    def test_missing_catalog_is_not_an_error(self):
        self.assertEqual(i18n.catalog("en"), {})
        self.assertEqual(i18n.translate("зона пуста"), "зона пуста")

    def test_mo_is_used(self):
        self.write_mo("en", {"зона пуста": "zone is empty"})
        i18n.reset_cache()
        i18n.set_language("en")
        self.assertEqual(i18n.translate("зона пуста"), "zone is empty")
        self.assertEqual(i18n._("зона пуста"), "zone is empty")

    def test_mo_wins_over_po(self):
        self.write_mo("en", {"зона": "from mo"})
        self.write_po("en.po", 'msgid "зона"\nmsgstr "from po"\n')
        i18n.reset_cache()
        self.assertEqual(i18n.catalog("en")["зона"], "from mo")

    def test_po_is_used_when_no_mo(self):
        self.write_po("zh_CN.po", 'msgid "зона"\nmsgstr "区域"\n')
        i18n.reset_cache()
        i18n.set_language("zh_CN")
        self.assertEqual(i18n.translate("зона"), "区域")

    def test_missing_entry_returns_source(self):
        self.write_mo("en", {"зона": "zone"})
        i18n.reset_cache()
        i18n.set_language("en")
        self.assertEqual(i18n.translate("окно"), "окно")

    def test_broken_mo_falls_back_to_po(self):
        path = os.path.join(self.root, "en", "LC_MESSAGES", "gridsnap.mo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"not a mo file at all")
        self.write_po("en.po", 'msgid "зона"\nmsgstr "from po"\n')
        i18n.reset_cache()
        self.assertEqual(i18n.catalog("en")["зона"], "from po")

    def test_russian_uses_source_ignoring_catalog(self):
        self.write_mo("ru", {"зона": "не должно пригодиться"})
        self.write_po("ru.po", 'msgid "зона"\nmsgstr "не должно пригодиться"\n')
        i18n.reset_cache()
        i18n.set_language("ru")
        self.assertEqual(i18n.translate("зона"), "зона")


class TestPaths(LanguageCase):
    def test_localedir_priority_env_first(self):
        with mock.patch.dict(os.environ, {"GRIDSNAP_LOCALEDIR": "/tmp/локали"}):
            self.assertEqual(i18n.localedirs()[0], "/tmp/локали")
        self.assertEqual(i18n.localedirs()[0], os.path.join(ROOT, "locale"))

    def test_install_prefix_localedir(self):
        # <префикс>/lib/gridsnap -> <префикс>/share/locale
        expected = os.path.normpath(os.path.join(ROOT, "..", "..", "share", "locale"))
        self.assertIn(expected, i18n.localedirs())

    def test_module_dir_and_po_dir(self):
        self.assertEqual(i18n.module_dir(), ROOT)
        self.assertEqual(i18n.po_dir(), os.path.join(ROOT, "po"))

    def test_mo_path_and_po_path_none(self):
        with mock.patch.object(i18n, "localedirs", return_value=["/tmp/пусто-локаль"]):
            with mock.patch.object(i18n, "po_dir", return_value="/tmp/пусто-po"):
                self.assertIsNone(i18n.mo_path("en"))
                self.assertIsNone(i18n.po_path("en"))

    def test_applet_domain_points_to_its_own_subdirectory(self):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root, True)
        with mock.patch.object(i18n, "po_dir", return_value=root):
            os.makedirs(os.path.join(root, i18n.APPLET_DOMAIN))
            with open(os.path.join(root, i18n.APPLET_DOMAIN, "en.po"), "w", encoding="utf-8") as fh:
                fh.write('msgid "Зоны"\nmsgstr "Zones"\n')
            self.assertEqual(i18n.po_path("en", i18n.APPLET_DOMAIN),
                             os.path.join(root, i18n.APPLET_DOMAIN, "en.po"))
            self.assertIsNone(i18n.po_path("en"))

if __name__ == "__main__":
    unittest.main()

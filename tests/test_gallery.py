#!/usr/bin/env python3
"""Галерея готовых раскладок: контракт (падающий тест до реализации).

Волна «галерея раскладок» отдана воркеру, а этот файл — зафиксированный заранее
интерфейс: чистые функции живут в `zone_config.py`, данные — в каталоге `gallery/`
рядом с кодом (и ставятся пакетом). Тест намеренно **падает** до реализации и
проверяет ровно то, о чём договорились с владельцем:

* раскладка из галереи — это набор зон **плюс** клавиши **плюс** правила для
  приложений (готовый «сценарий», а не только геометрия);
* выбор копирует раскладку в **профиль пользователя** — дальше её правят как свою;
* галерея видна и в редакторе, и в CLI (CLI-часть проверяется как функции
  `zone_config`, потому что редактор и командная строка зовут их же);
* файлы галереи — обычные `.conf` в формате профиля, с секцией `[gallery]`
  (человеческое название и описание — их показывают и меню, и `profile gallery`).

Тела не подсказываются: важно, чтобы воркер сам выбрал разумную реализацию,
а тест держал обещания интерфейса.
"""

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402

GALLERY = os.path.join(ROOT, "gallery")

# Сколько раскладок должно быть в первой поставке: меньше — «галерея» из одного
# пункта, больше — каша в меню. Владелец просил готовые сценарии, а не каталог.
MIN_LAYOUTS = 6


class TestGalleryData(unittest.TestCase):
    def test_gallery_directory_is_shipped_with_code(self):
        self.assertTrue(
            os.path.isdir(GALLERY),
            "нет каталога gallery/ рядом с кодом — раскладкам неоткуда взяться",
        )

    def test_every_layout_is_a_profile_with_title_and_description(self):
        files = sorted(f for f in os.listdir(GALLERY) if f.endswith(".conf"))
        self.assertGreaterEqual(
            len(files), MIN_LAYOUTS, f"в галерее {len(files)} раскладок, мало"
        )
        for name in files:
            with open(os.path.join(GALLERY, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("[gallery]", text, f"{name}: нет секции [gallery]")
            self.assertIn("[zones]", text, f"{name}: нет зон")
            self.assertIn("title", text, f"{name}: нет человеческого названия")
            self.assertIn("description", text, f"{name}: нет описания")

    def test_layouts_bring_keys_and_rules_not_only_geometry(self):
        # Владелец выбрал вариант «наборы + клавиши + правила»: хотя бы одна
        # раскладка обязана приносить правила для приложений.
        texts = []
        for name in sorted(os.listdir(GALLERY)):
            if name.endswith(".conf"):
                with open(os.path.join(GALLERY, name), encoding="utf-8") as fh:
                    texts.append((name, fh.read()))
        self.assertTrue(any("[rules]" in t for _n, t in texts), "ни одной [rules]")
        self.assertTrue(any("[keys]" in t for _n, t in texts), "ни одной [keys]")


class TestGalleryApi(unittest.TestCase):
    """Интерфейс галереи в `zone_config` — им пользуются и CLI, и редактор."""

    def test_gallery_layouts_lists_all_with_titles(self):
        layouts = zc.gallery_layouts()
        self.assertGreaterEqual(len(layouts), MIN_LAYOUTS)
        for item in layouts:
            self.assertTrue(item["id"], "у раскладки нет id")
            self.assertTrue(item["title"].strip(), f"{item['id']}: пустое название")
            self.assertTrue(
                item["description"].strip(), f"{item['id']}: пустое описание"
            )
            self.assertTrue(item["zones"], f"{item['id']}: нет зон")

    def test_load_gallery_layout_gives_zones_keys_and_rules(self):
        gid = zc.gallery_layouts()[0]["id"]
        layout = zc.load_gallery_layout(gid)
        self.assertTrue(layout.get("zones"), f"{gid}: нечего раскладывать")
        # keys и rules могут быть пустыми у конкретной раскладки, но ключи в
        # словаре обязаны быть — по ним редактор и CLI решают, что переносить.
        self.assertIn("keys", layout)
        self.assertIn("rules", layout)

    def test_load_unknown_layout_is_a_clear_error(self):
        with self.assertRaises(zc.GalleryError):
            zc.load_gallery_layout("нет-такой-раскладки")

    def test_install_copies_into_user_profile(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write("[zones]\nmain = 0, 0, 1, 1\n")
            gid = zc.gallery_layouts()[0]["id"]
            path = zc.install_gallery_layout(gid, conf_path=conf)
            self.assertTrue(os.path.exists(path), "профиль не создан")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("[zones]", text, "в профиль не перенеслись зоны")
            # Повторный выбор того же имени без force — ошибка, а не тихая
            # перезапись чужого профиля.
            with self.assertRaises(zc.GalleryError):
                zc.install_gallery_layout(gid, conf_path=conf)

    def test_install_can_write_under_a_chosen_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "zones.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write("[zones]\nmain = 0, 0, 1, 1\n")
            gid = zc.gallery_layouts()[0]["id"]
            path = zc.install_gallery_layout(gid, name="моя-раскладка", conf_path=conf)
            self.assertEqual(os.path.basename(path), "моя-раскладка.conf")
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()

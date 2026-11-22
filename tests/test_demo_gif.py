#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тест демонстрации: `site/assets/demo.gif` и скрипт её записи.

Демонстрацию снимает `tools/record-demo.sh` (свой Xvfb, muffin, окна и демон
зон), а ссылаются на неё README на трёх языках и все три страницы сайта.
Поэтому проверяется не «картинка вообще», а именно запись сцены:

* файл существует и начинается с `GIF87a`/`GIF89a` — не PNG и не заглушка;
* ширина не меньше 640 px: в маленьком окне README сцена должна читаться;
* кадров больше десяти — значит, это движение (редактор, вкладки, пикер,
  режим «только эта зона»), а не один статичный снимок;
* размер в пределах 2.5 МБ — иначе README и сайт грузятся зря;
* `tools/record-demo.sh` исполняемый и пишет по умолчанию в `site/assets/demo.gif`:
  демонстрацию можно перезаписать одной командой, а не «где-то был скрипт».

Кадры считаются по блокам-дескрипторам изображения `0x2C` (а не по числу
байтов): у GIF кадр — это дескриптор, за которым идут LZW-подблоки, и только
разбор блоков отличает 20 кадров от одного с мусором в данных.

X, GTK и ffmpeg тесту не нужны — только байты файлов.

Запуск: `python3 -m unittest tests.test_demo_gif -v` или
`python3 -m unittest discover -s tests`.
"""

import os
import unittest

import i18n_env  # noqa: F401  (тесты идут на исходном языке)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GIF = os.path.join(ROOT, "site", "assets", "demo.gif")
SCRIPT = os.path.join(ROOT, "tools", "record-demo.sh")

SIGNATURES = (b"GIF87a", b"GIF89a")
MIN_WIDTH = 640
MIN_FRAMES = 11
MAX_BYTES = int(2.5 * 1024 * 1024)


def skip_subblocks(data, offset):
    """Пропустить цепочку подблоков GIF (размер + данные, конец — нулевой блок)."""
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
    raise ValueError("подблоки GIF оборвались на середине")


def gif_info(data):
    """`(подпись, ширина, высота, число кадров)` из байтов GIF.

    Разбирает только заголовок, логический экран, глобальную палитру,
    расширения и дескрипторы изображений: этого хватает, чтобы отличить
    настоящую анимацию от заглушки, и не нужно распаковывать LZW.
    """
    if len(data) < 13:
        raise ValueError("файл короче заголовка GIF")
    signature = data[:6]
    if signature not in SIGNATURES:
        raise ValueError(f"не GIF: подпись {signature!r}")
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    packed = data[10]
    offset = 13
    if packed & 0x80:  # глобальная палитра
        offset += 3 * (2 ** ((packed & 0x07) + 1))

    frames = 0
    while offset < len(data):
        block = data[offset]
        if block == 0x3B:  # трейлер
            break
        if block == 0x21:  # расширение: метка, затем подблоки
            offset = skip_subblocks(data, offset + 2)
        elif block == 0x2C:  # дескриптор изображения — это кадр
            frames += 1
            if offset + 10 > len(data):
                raise ValueError("дескриптор кадра оборван")
            local = data[offset + 9]
            offset += 10
            if local & 0x80:  # локальная палитра кадра
                offset += 3 * (2 ** ((local & 0x07) + 1))
            offset += 1  # минимальный размер LZW-кода
            offset = skip_subblocks(data, offset)
        elif block == 0x00:  # заполнитель между блоками
            offset += 1
        else:
            raise ValueError(f"неизвестный блок GIF 0x{block:02x} на смещении {offset}")
    return signature, width, height, frames


class DemoGifTest(unittest.TestCase):
    """Запись демонстрации на месте и пригодна для README и сайта."""

    @classmethod
    def setUpClass(cls):
        # Файла может не быть — тогда падает test_file_exists с внятным
        # сообщением, а не setUpClass трейсбеком на открытии.
        cls.data = None
        if os.path.isfile(GIF):
            with open(GIF, "rb") as handle:
                cls.data = handle.read()
        cls.info = gif_info(cls.data) if cls.data else None

    def parsed(self):
        """`(подпись, ширина, высота, кадров)` — с проверкой, что файл есть."""
        self.assertIsNotNone(self.info, f"нет файла демонстрации: {GIF}")
        return self.info

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(GIF), f"нет файла демонстрации: {GIF}")
        self.assertGreater(os.path.getsize(GIF), 0, "файл демонстрации пуст")

    def test_is_gif(self):
        signature = self.parsed()[0]
        self.assertIn(signature, SIGNATURES, f"подпись {signature!r} — не GIF")

    def test_width_at_least_640(self):
        _, width, height, _ = self.parsed()
        self.assertGreaterEqual(
            width, MIN_WIDTH,
            f"ширина {width} px: в маленьком окне сцена не читается",
        )
        self.assertGreater(height, 0, "высота GIF нулевая")

    def test_more_than_ten_frames(self):
        frames = self.parsed()[3]
        self.assertGreater(
            frames, MIN_FRAMES,
            f"кадров {frames}: это статичный снимок, а не запись сцены",
        )

    def test_size_within_2_5_mb(self):
        size = len(self.data or b"")
        self.assertLessEqual(
            size, MAX_BYTES,
            f"{size / 1024 / 1024:.2f} МБ больше 2.5 МБ — перезапишите сцену короче",
        )

    def test_record_script_is_executable(self):
        self.assertTrue(os.path.isfile(SCRIPT), f"нет скрипта записи: {SCRIPT}")
        self.assertTrue(
            os.access(SCRIPT, os.X_OK),
            "tools/record-demo.sh не исполняемый: chmod +x tools/record-demo.sh",
        )

    def test_record_script_writes_site_demo_gif(self):
        with open(SCRIPT, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(
            "site/assets/demo.gif", text,
            "tools/record-demo.sh не упоминает site/assets/demo.gif: "
            "путь результата разошёлся с README и сайтом",
        )


if __name__ == "__main__":
    unittest.main()

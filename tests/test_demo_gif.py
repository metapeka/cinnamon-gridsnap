#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test of the demo: `site/assets/demo.gif` and the script that records it.

The demo is recorded by `tools/record-demo.sh` (its own Xvfb, muffin, windows and the zone
daemon), and it is referenced by the README in three languages and by all three pages of the site.
So what is checked is not "a picture in general" but specifically the recording of the scene:

* the file exists and starts with `GIF87a`/`GIF89a` — not a PNG and not a stub;
* the width is at least 640 px: in the small README window the scene must be readable;
* there are more than ten frames — so this is motion (the editor, tabs, the picker,
  the "only this zone" mode), not a single static snapshot;
* the size is within 2.5 MB — otherwise the README and the site load for nothing;
* `tools/record-demo.sh` is executable and writes to `site/assets/demo.gif` by default:
  the demo can be re-recorded with one command, rather than "there was a script somewhere".

Frames are counted by image descriptor blocks `0x2C` (not by the number of
bytes): in a GIF a frame is a descriptor followed by LZW sub-blocks, and only
parsing the blocks tells 20 frames from one with garbage in the data.

The test needs no X, GTK or ffmpeg — only the bytes of the files.

Running: `python3 -m unittest tests.test_demo_gif -v` or
`python3 -m unittest discover -s tests`.
"""

import os
import unittest


try:
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GIF = os.path.join(ROOT, "site", "assets", "demo.gif")
SCRIPT = os.path.join(ROOT, "tools", "record-demo.sh")

SIGNATURES = (b"GIF87a", b"GIF89a")
MIN_WIDTH = 640
MIN_FRAMES = 11
MAX_BYTES = int(2.5 * 1024 * 1024)


def skip_subblocks(data, offset):
    """Skip a chain of GIF sub-blocks (size + data, the end is a zero block)."""
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
    raise ValueError("подблоки GIF оборвались на середине")


def gif_info(data):
    """`(signature, width, height, frame count)` from the bytes of a GIF.

    Parses only the header, the logical screen, the global palette,
    extensions and image descriptors: that is enough to tell
    a real animation from a stub, and there is no need to unpack LZW.
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
    if packed & 0x80:  # global palette
        offset += 3 * (2 ** ((packed & 0x07) + 1))

    frames = 0
    while offset < len(data):
        block = data[offset]
        if block == 0x3B:  # trailer
            break
        if block == 0x21:  # extension: a label, then sub-blocks
            offset = skip_subblocks(data, offset + 2)
        elif block == 0x2C:  # an image descriptor is a frame
            frames += 1
            if offset + 10 > len(data):
                raise ValueError("дескриптор кадра оборван")
            local = data[offset + 9]
            offset += 10
            if local & 0x80:  # the frame's local palette
                offset += 3 * (2 ** ((local & 0x07) + 1))
            offset += 1  # the minimum LZW code size
            offset = skip_subblocks(data, offset)
        elif block == 0x00:  # filler between blocks
            offset += 1
        else:
            raise ValueError(f"неизвестный блок GIF 0x{block:02x} на смещении {offset}")
    return signature, width, height, frames


class DemoGifTest(unittest.TestCase):
    """The demo recording is in place and fit for the README and the site."""

    @classmethod
    def setUpClass(cls):
        # The file may be missing — then test_file_exists fails with a clear
        # message, not setUpClass with a traceback on open.
        cls.data = None
        if os.path.isfile(GIF):
            with open(GIF, "rb") as handle:
                cls.data = handle.read()
        cls.info = gif_info(cls.data) if cls.data else None

    def parsed(self):
        """`(signature, width, height, frames)` — with a check that the file exists."""
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

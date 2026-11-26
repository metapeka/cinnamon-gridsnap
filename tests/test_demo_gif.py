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
  the demo can be re-recorded with one command, rather than "there was a script somewhere";
* the palette comes from `tools/scene-palette.py` and the wallpaper setting is pinned inside the
  sandbox: a demo that samples the owner's own wallpaper would differ between machines, and a
  demo that writes the live `picture-uri` would change the owner's desktop;
* the windows are opened through the function's own `OPEN_PID`, never through `$(open_window …)`:
  the whole function then runs in a subshell, the background job does not survive it, and the
  scene dies on "the window did not open" — the bug this test was written after.

Frames are counted by image descriptor blocks `0x2C` (not by the number of bytes): in a GIF a
frame is a descriptor followed by LZW sub-blocks, and only parsing the blocks tells 20 frames from
one with garbage in the data.

The test needs no X, GTK or ffmpeg — only the bytes of the files.

Running: `python3 -m unittest tests.test_demo_gif -v` or
`python3 -m unittest discover -s tests`.
"""

import os
import re
import unittest


try:
    import i18n_env  # noqa: F401  (tests run in the source language)
except ImportError:  # launched as `python3 -m unittest tests.<module>`
    from tests import i18n_env  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GIF = os.path.join(ROOT, "site", "assets", "demo.gif")
SCRIPT = os.path.join(ROOT, "tools", "record-demo.sh")
PALETTE = os.path.join(ROOT, "tools", "scene-palette.py")

SIGNATURES = (b"GIF87a", b"GIF89a")
MIN_WIDTH = 640
MIN_FRAMES = 11
MAX_BYTES = int(2.5 * 1024 * 1024)


def skip_subblocks(data, offset):
    """Skip a chain of GIF sub-blocks (size + data, a zero block ends it)."""
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
    raise ValueError("the GIF sub-blocks stop mid-chain")


def gif_info(data):
    """`(signature, width, height, frame count)` from the bytes of a GIF.

    Parses only the header, the logical screen, the global palette, extensions and image
    descriptors: that is enough to tell a real animation from a stub, and there is no need to
    unpack LZW.
    """
    if len(data) < 13:
        raise ValueError("the file is shorter than a GIF header")
    signature = data[:6]
    if signature not in SIGNATURES:
        raise ValueError(f"not a GIF: signature {signature!r}")
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
                raise ValueError("a frame descriptor is truncated")
            local = data[offset + 9]
            offset += 10
            if local & 0x80:  # the frame's local palette
                offset += 3 * (2 ** ((local & 0x07) + 1))
            offset += 1  # the minimum LZW code size
            offset = skip_subblocks(data, offset)
        elif block == 0x00:  # filler between blocks
            offset += 1
        else:
            raise ValueError(f"unknown GIF block 0x{block:02x} at offset {offset}")
    return signature, width, height, frames


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class DemoGifTest(unittest.TestCase):
    """The demo recording is in place and fit for the README and the site."""

    @classmethod
    def setUpClass(cls):
        # The file may be missing — then test_file_exists fails with a clear message, not
        # setUpClass with a traceback on open.
        cls.data = None
        if os.path.isfile(GIF):
            with open(GIF, "rb") as handle:
                cls.data = handle.read()
        cls.info = gif_info(cls.data) if cls.data else None

    def parsed(self):
        """`(signature, width, height, frames)` — with a check that the file exists."""
        self.assertIsNotNone(self.info, f"no demo file: {GIF}")
        return self.info

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(GIF), f"no demo file: {GIF}")
        self.assertGreater(os.path.getsize(GIF), 0, "the demo file is empty")

    def test_is_gif(self):
        signature = self.parsed()[0]
        self.assertIn(signature, SIGNATURES, f"signature {signature!r} is not a GIF")

    def test_width_at_least_640(self):
        _, width, height, _ = self.parsed()
        self.assertGreaterEqual(
            width, MIN_WIDTH,
            f"width {width} px: the scene does not read in a small window",
        )
        self.assertGreater(height, 0, "the GIF height is zero")

    def test_more_than_ten_frames(self):
        frames = self.parsed()[3]
        self.assertGreater(
            frames, MIN_FRAMES,
            f"{frames} frames: that is a still snapshot, not a recorded scene",
        )

    def test_size_within_2_5_mb(self):
        size = len(self.data or b"")
        self.assertLessEqual(
            size, MAX_BYTES,
            f"{size / 1024 / 1024:.2f} MB is over 2.5 MB — record a shorter scene",
        )

    def test_record_script_is_executable(self):
        self.assertTrue(os.path.isfile(SCRIPT), f"no recording script: {SCRIPT}")
        self.assertTrue(
            os.access(SCRIPT, os.X_OK),
            "tools/record-demo.sh is not executable: chmod +x tools/record-demo.sh",
        )

    def test_record_script_writes_site_demo_gif(self):
        self.assertIn(
            "site/assets/demo.gif", read(SCRIPT),
            "tools/record-demo.sh never mentions site/assets/demo.gif: "
            "the result path drifted from the README and the site",
        )

    def test_scene_palette_is_shared_and_pinned(self):
        # The recording must show the palette of a scene wallpaper, not the defaults and not the
        # wallpaper of the machine that recorded it.
        text = read(SCRIPT)
        self.assertIn(
            "scene-palette.py", text,
            "tools/record-demo.sh does not use tools/scene-palette.py: the palette is built twice",
        )
        self.assertIn(
            "--pin-wallpaper", text,
            "tools/record-demo.sh does not pin the wallpaper setting: the daemon would replace "
            "the sampled palette with the one of the owner's own wallpaper",
        )

    def test_scene_runs_in_a_sandbox(self):
        # `~/.themes/Gridsnap` is the user's theme directory and the wallpaper setting is the
        # user's setting: the scene must own a temporary home and a keyfile gsettings backend.
        text = read(SCRIPT)
        for needle in ("GSETTINGS_BACKEND=keyfile", 'HOME="$WORK/home"'):
            self.assertIn(needle, text, f"tools/record-demo.sh does not sandbox via {needle}")

    def test_windows_are_not_opened_in_a_subshell(self):
        # `$(open_window …)` runs the function in a subshell; the `xmessage` started there dies
        # with it, and the scene stops on "the window did not open" while `ps` shows nothing.
        code = "\n".join(
            line for line in read(SCRIPT).splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotRegex(
            code, r"\$\(\s*open_window",
            "tools/record-demo.sh opens a window in a command substitution: "
            "the background job does not survive it — use OPEN_PID",
        )
        self.assertIn(
            "OPEN_PID=$!", code,
            "tools/record-demo.sh no longer hands the window PID out through OPEN_PID",
        )

    def test_frame_check_loads_the_repository_root(self):
        # `zone-tiler.py` imports `zone_i18n` and `zone_tabs` by bare name: the frame check has to
        # put the repository root on the path, or the scene stops with ModuleNotFoundError.
        self.assertIn(
            "sys.path.insert(0, root)", read(SCRIPT),
            "the frame check does not put the repository root on sys.path",
        )


class ScenePaletteTest(unittest.TestCase):
    """`tools/scene-palette.py` produces the wallpaper and the palette the scenes read."""

    def test_script_is_executable(self):
        self.assertTrue(os.path.isfile(PALETTE), f"no helper: {PALETTE}")
        self.assertTrue(
            os.access(PALETTE, os.X_OK), "tools/scene-palette.py is not executable: chmod +x it"
        )

    def test_draws_a_wallpaper_and_writes_the_theme(self):
        import tempfile

        with tempfile.TemporaryDirectory() as work:
            code = (
                "import runpy, sys; sys.argv = ['scene-palette.py', '--out', %r,"
                " '--width', '64', '--height', '48'];\n"
                "runpy.run_path(%r, run_name='__main__')" % (work, PALETTE)
            )
            import subprocess

            result = subprocess.run(
                [os.sys.executable, "-c", code], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            image = os.path.join(work, "wallpaper.png")
            theme = os.path.join(work, "theme.conf")
            self.assertTrue(os.path.isfile(image), "no wallpaper.png")
            self.assertTrue(os.path.isfile(theme), "no theme.conf")
            with open(image, "rb") as handle:
                self.assertTrue(handle.read(8).startswith(b"\x89PNG"), "not a PNG")
            text = read(theme)
            self.assertTrue(
                text.startswith("# Generated by gridsnap"),
                "the scene theme.conf does not carry the generated marker: the daemon would "
                "treat it as the user's own file",
            )
            self.assertIn("[colors]", text, "the scene theme.conf has no palette")
            self.assertRegex(
                result.stdout, r"palette: bg #[0-9a-f]{6}, card #[0-9a-f]{6}",
                "the helper does not report the palette it drew",
            )

    def test_default_palette_is_rejected(self):
        # The scenes exist to show where the colours come from: a helper that produced the default
        # palette would give a demo nobody can tell from a product without the feature.
        self.assertIn(
            "from_wallpaper", read(PALETTE),
            "tools/scene-palette.py does not sample the wallpaper it draws",
        )


if __name__ == "__main__":
    unittest.main()

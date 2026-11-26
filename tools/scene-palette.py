#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Draw the scene wallpaper and the palette it yields, for the recording scripts.

`tools/shoot-screens.sh` (the still images) and `tools/record-demo.sh` (the GIF) both need the
same thing before they start a surface: a wallpaper the repository can describe in one sentence,
and the `theme.conf` that `zone_theme.from_wallpaper` derives from it. The palette is deliberately
not the default one — the pictures exist to show where the colours come from.

The wallpaper is drawn here rather than taken from the disk or made with ImageMagick: a PNG
encoder is a few lines of `zlib` + `struct`, so the picture is byte-identical on every machine,
and the palette sampled from it is reproducible.

    tools/scene-palette.py --out DIR [--width 1280] [--height 800]

Writes ``DIR/wallpaper.png`` and ``DIR/theme.conf`` (with the generated marker, so the daemon
would keep it in step with the wallpaper instead of treating it as the user's own), and prints
one line: the background, card and accent colours plus the number of zone hues.

``--pin-wallpaper`` also points the desktop background setting at the drawn image. A scene needs
that: the daemon rewrites a marked ``theme.conf`` whose recorded source differs from the live
``picture-uri``, so on the owner's machine the sampled palette would be replaced by the palette of
the owner's own wallpaper on the first poll and the picture would silently differ between
machines. The caller does this inside a `gsettings` sandbox (``GSETTINGS_BACKEND=keyfile`` with
``XDG_CONFIG_HOME`` in the temporary directory), so the owner's setting is never touched.

Return code: 0 — both files written; 2 — the arguments are wrong or the image cannot be sampled.
"""

import argparse
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import zone_theme  # noqa: E402  (after sys.path: the module lives in the repository root)

# A slate gradient lit by a warm glint on the right: dark enough for a window manager's desktop
# to disappear behind windows, warm enough that the sampled accent is unmistakable.
DARK = (0x0B, 0x10, 0x20)
LIGHT = (0x1B, 0x2B, 0x45)
GLINT = (0xE8, 0x86, 0x3C)
GLINT_AT = (1010.0, 210.0)
GLINT_RADIUS = 300.0


def png_bytes(width, height):
    """A truecolour PNG of the scene wallpaper, built row by row."""
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")  # filter type 0: no prediction
        for x in range(width):
            t = (x / width) * 0.55 + (y / height) * 0.45
            pixel = [DARK[i] + (LIGHT[i] - DARK[i]) * t for i in range(3)]
            dx = (x - GLINT_AT[0]) / GLINT_RADIUS
            dy = (y - GLINT_AT[1]) / GLINT_RADIUS
            distance = dx * dx + dy * dy
            if distance < 1.0:
                k = (1.0 - distance) ** 2
                pixel = [pixel[i] + (GLINT[i] - pixel[i]) * k for i in range(3)]
            row += bytes(min(255, max(0, int(value))) for value in pixel)
        rows.append(bytes(row))

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def pin_wallpaper(image):
    """Point the desktop background setting at ``image``; returns the schema or ``None``.

    Called only inside a `gsettings` sandbox — see the module docstring. A machine without the
    Cinnamon (or GNOME) background schema is not an error: the daemon then finds no wallpaper to
    sample and leaves the file alone, which is the behaviour the scene wants anyway.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except (ImportError, ValueError):
        return None
    source = Gio.SettingsSchemaSource.get_default()
    if source is None:
        return None
    for schema in zone_theme.WALLPAPER_SCHEMAS:
        if source.lookup(schema, True) is None:
            continue
        Gio.Settings.new(schema).set_string(zone_theme.WALLPAPER_KEY, "file://" + image)
        return schema
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="draw the scene wallpaper and its palette")
    ap.add_argument("--out", required=True, help="directory for wallpaper.png and theme.conf")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument(
        "--pin-wallpaper",
        action="store_true",
        help="point the background setting at the drawn image (sandbox only)",
    )
    args = ap.parse_args(argv)

    if args.width <= 0 or args.height <= 0:
        print("scene-palette: --width and --height must be positive", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    image = os.path.join(args.out, "wallpaper.png")
    try:
        with open(image, "wb") as handle:
            handle.write(png_bytes(args.width, args.height))
    except OSError as exc:
        print(f"scene-palette: cannot write {image}: {exc}", file=sys.stderr)
        return 2

    try:
        theme = zone_theme.from_wallpaper(image)
    except (ValueError, OSError) as exc:
        print(f"scene-palette: cannot sample {image}: {exc}", file=sys.stderr)
        return 2
    zone_theme.write_theme(theme, os.path.join(args.out, "theme.conf"), source=image)
    if args.pin_wallpaper and pin_wallpaper(image) is None:
        print(
            "scene-palette: no background schema — the daemon has nothing to sample",
            file=sys.stderr,
        )
    print(
        "palette: bg %s, card %s, accent %s, %d zone hues"
        % (
            theme.colors["bg"],
            theme.colors["bg_card"],
            theme.colors["accent"],
            len(theme.zones),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

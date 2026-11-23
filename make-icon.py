#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make-icon.py — draws the Gridsnap icon (SVG + PNG of every size).

Idea: a dark screen tile split into three zones; the bottom-right zone is outlined
in amber — "a window lands here". PNGs are drawn through cairo (librsvg is not needed).
"""

import os
import sys

import cairo

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zone_i18n import _  # noqa: E402  (UI strings and their translation)

ICONS = os.path.join(HERE, "icons")
SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

BG_TOP = (0.16, 0.18, 0.23)
BG_BOT = (0.09, 0.10, 0.13)
INSET = 0.115
GAP = 0.040
ACCENT = (1.0, 0.70, 0.33)
# (x, y, w, h, top, bottom)
PANE = [
    (0.00, 0.00, 0.46, 1.00, (0.36, 0.65, 0.96), (0.24, 0.48, 0.80)),
    (0.46, 0.00, 0.54, 0.48, (0.46, 0.82, 0.63), (0.32, 0.64, 0.47)),
    (0.46, 0.48, 0.54, 0.52, (0.79, 0.55, 0.90), (0.60, 0.38, 0.71)),
]


def rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


def grad(cr, x, y, w, h, top, bot):
    g = cairo.LinearGradient(x, y, x, y + h)
    g.add_color_stop_rgb(0, *top)
    g.add_color_stop_rgb(1, *bot)
    return g


def draw(cr, size):
    # background
    rounded(cr, 0.6, 0.6, size - 1.2, size - 1.2, size * 0.22)
    cr.set_source(grad(cr, 0, 0, size, size, BG_TOP, BG_BOT))
    cr.fill_preserve()
    cr.set_line_width(max(1.0, size * 0.012))
    cr.set_source_rgba(1, 1, 1, 0.13)
    cr.stroke()

    area = size * (1 - 2 * INSET)
    base = size * INSET
    gap = size * GAP
    r = size * 0.055

    for i, (fx, fy, fw, fh, top, bot) in enumerate(PANE):
        x = base + fx * area + gap / 2
        y = base + fy * area + gap / 2
        w = fw * area - gap
        h = fh * area - gap
        rounded(cr, x, y, w, h, r)
        cr.set_source(grad(cr, x, y, w, h, top, bot))
        cr.fill()
        if i == 2:  # active zone — amber outline
            cr.set_line_width(max(1.2, size * 0.030))
            cr.set_source_rgba(*ACCENT, 1)
            rounded(cr, x, y, w, h, r)
            cr.stroke()
        # highlight on top — volume
        cr.set_line_width(max(0.7, size * 0.008))
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.move_to(x + r, y + max(0.7, size * 0.008))
        cr.line_to(x + w - r, y + max(0.7, size * 0.008))
        cr.stroke()


SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2a2e3b"/><stop offset="1" stop-color="#171a21"/>
    </linearGradient>
    <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5ca6f5"/><stop offset="1" stop-color="#3d7acc"/>
    </linearGradient>
    <linearGradient id="g2" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#75d1a1"/><stop offset="1" stop-color="#52a378"/>
    </linearGradient>
    <linearGradient id="g3" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#c98ce6"/><stop offset="1" stop-color="#9961b5"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="254" height="254" rx="56" ry="56" fill="url(#bg)"
        stroke="#ffffff" stroke-opacity="0.13" stroke-width="3"/>
  <rect x="29" y="29" width="94" height="198" rx="14" fill="url(#g1)"/>
  <rect x="127" y="29" width="100" height="93" rx="14" fill="url(#g2)"/>
  <rect x="127" y="126" width="100" height="101" rx="14" fill="url(#g3)"
        stroke="#ffb455" stroke-width="8"/>
</svg>
"""


def make_png(path, size):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    draw(cr, size)
    surface.write_to_png(path)


def main():
    os.makedirs(ICONS, exist_ok=True)
    for size in SIZES:
        make_png(os.path.join(ICONS, f"gridsnap-{size}.png"), size)
    with open(os.path.join(ICONS, "gridsnap.svg"), "w", encoding="utf-8") as fh:
        fh.write(SVG)
    print(_("Gridsnap icons: {sizes}").format(sizes=", ".join(str(s) for s in SIZES)))


if __name__ == "__main__":
    sys.exit(main())

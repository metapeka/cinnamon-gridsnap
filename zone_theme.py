#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_theme — one palette for every GridSnap surface: the picker, the HUD, the tab strip, the editor.

A theme is optional. Without it every surface keeps the colours it has always drawn, so an old
installation looks exactly the same. A file next to ``zones.conf`` changes them together:

    ~/.config/gridsnap/zones.conf
    ~/.config/gridsnap/theme.conf

    # theme.conf
    [theme]
    preset = 43pr              # a base palette from PRESETS; optional
    radius = 10                # corner radius of a zone, px (picker and editor)
    font = JetBrains Mono      # any family Pango knows; the default is Sans

    [colors]
    bg       = #12141a         # behind everything: the overlay backdrop
    bg_panel = #1c1f24         # a panel: the tab strip, the editor canvas
    bg_card  = #2b2e36         # a raised surface: a tab, the editor "screen"
    bg_hover = #525c70         # hovered or active: the active tab
    fg       = #ffffff
    fg_muted = #c7ccd6
    accent   = #ffb554         # the selected zone, the active tab stripe
    border   = #525c70         # frames and separators of the GTK parts
    danger   = #eb8585         # reserved for destructive actions
    ok       = #6bc794         # reserved for confirmations
    zones    = #5c9eeb, #6bc794, #ebaa5c, #cc80d9, #6bccd9, #eb8585, #b8c266, #9e9eeb

The format is INI, the same family as ``zones.conf``, and not TOML on purpose: ``tomllib``
arrived in Python 3.11, while Cinnamon ships on distributions that still carry 3.10 (Mint 21).
GridSnap promises "Python 3 + GTK" and no version floor above that, and a missing module here
would take the whole picker down — not just the theme.

A broken theme never breaks the desktop: an unreadable file, an unknown preset or a malformed
colour all keep the default and print one warning on stderr. The tools that draw stay up: a grid
that disappears because of a typo in a colour is worse than a grid in the default colours.

``PRESETS`` are palettes transcribed from 43PR/dotfiles (MIT), a Hyprland + Quickshell setup;
the token names are its names too, so its look is reachable without its QML. What does not come
from there is the shell itself: the bar and the panels are Quickshell and need a Wayland compositor.
"""

import colorsys
import configparser
import os
import re
import sys

from zone_i18n import _

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# The colour tokens, in the order the documentation lists them. Every surface takes its colours
# from here, so a theme cooperates with itself: a selection looks the same in all three windows.
COLOR_TOKENS = (
    "bg",
    "bg_panel",
    "bg_card",
    "bg_hover",
    "fg",
    "fg_muted",
    "accent",
    "border",
    "danger",
    "ok",
)

# The default palette reproduces what the surfaces drew before the theme existed: #12141a is the
# overlay backdrop, #1c1f24 the editor canvas, #2b2e36 the editor "screen", #525c70 the active tab.
# The tab strip now shares bg_panel/bg_card with the editor, so its strip and tab are one step
# darker than they were (#21242b and #333842) — the only visible difference of an absent theme.
DEFAULT_COLORS = {
    "bg": "#12141a",
    "bg_panel": "#1c1f24",
    "bg_card": "#2b2e36",
    "bg_hover": "#525c70",
    "fg": "#ffffff",
    "fg_muted": "#c7ccd6",
    "accent": "#ffb554",
    "border": "#525c70",
    "danger": "#eb8585",
    "ok": "#6bc794",
}

# The picker palette: eight hues that stay apart from each other on a dark backdrop.
DEFAULT_ZONES = (
    "#5c9eeb",
    "#6bc794",
    "#eba85c",
    "#cc80d9",
    "#6bccd9",
    "#eb8585",
    "#b8c266",
    "#9e9eeb",
)

DEFAULT_RADIUS = 10
DEFAULT_FONT = "Sans"

# A theme is a look, not a canvas: more zone colours than a sane grid has, or a radius that
# turns a zone into a circle, are typos, and the default is kept.
MAX_ZONES = 16
MAX_RADIUS = 64

# Autolightness of a derived zone: mid tones that read on every dark theme, and that keep the
# caption (white, 0.94 alpha) legible on the fill (the same hue at 0.20 alpha).
DERIVED_LIGHTNESS = 0.62
DERIVED_SATURATION = 0.45
# The hue a monochrome accent is replaced with: eight identical greys would make the grid
# unreadable, and a monochrome accent is exactly what the 43PR presets carry.
MONOCHROME_HUE = 210 / 360.0

# Palettes from 43PR/dotfiles (MIT), values as published in `.config/43pr/themes/*.toml`.
PRESETS = {
    "43pr": {
        "bg": "#000000",
        "bg_panel": "#050505",
        "bg_card": "#0d0d0d",
        "bg_hover": "#242832",
        "fg": "#ffffff",
        "fg_muted": "#8b93a7",
        "accent": "#ffffff",
        "border": "#151515",
        "danger": "#ff003c",
        "ok": "#00ff9c",
    },
    "nord": {
        "bg": "#2e3440",
        "bg_panel": "#292e39",
        "bg_card": "#3b4252",
        "bg_hover": "#4c566a",
        "fg": "#eceff4",
        "fg_muted": "#aeb9cc",
        "accent": "#88c0d0",
        "border": "#434c5e",
        "danger": "#bf616a",
        "ok": "#a3be8c",
    },
    "tokyo-night": {
        "bg": "#16161e",
        "bg_panel": "#1a1b26",
        "bg_card": "#1f2335",
        "bg_hover": "#292e42",
        "fg": "#c0caf5",
        "fg_muted": "#7982a9",
        "accent": "#7aa2f7",
        "border": "#292e42",
        "danger": "#f7768e",
        "ok": "#9ece6a",
    },
    "catppuccin-mocha": {
        "bg": "#1e1e2e",
        "bg_panel": "#181825",
        "bg_card": "#313244",
        "bg_hover": "#585b70",
        "fg": "#cdd6f4",
        "fg_muted": "#7f849c",
        "accent": "#89b4fa",
        "border": "#313244",
        "danger": "#f38ba8",
        "ok": "#a6e3a1",
    },
    "graphite": {
        "bg": "#1c1c1e",
        "bg_panel": "#242426",
        "bg_card": "#2c2c2e",
        "bg_hover": "#48484a",
        "fg": "#e5e5e7",
        "fg_muted": "#8e8e93",
        "accent": "#8e8e93",
        "border": "#3a3a3c",
        "danger": "#CD7774",
        "ok": "#91B49C",
    },
}


def _warn(message):
    """One line on stderr; the surfaces keep drawing."""
    print(_("zone-theme: {}").format(message), file=sys.stderr)


def rgb(value):
    """``#RRGGBB`` → ``(r, g, b)`` in 0..1, the scale cairo paints in."""
    return tuple(int(value[index : index + 2], 16) / 255.0 for index in (1, 3, 5))


def hex_of(colour):
    """``(r, g, b)`` in 0..1 → ``#rrggbb``; the inverse of :func:`rgb`."""
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, round(channel * 255))) for channel in colour
    )


def luminance(value):
    """Relative luminance of ``#RRGGBB`` (WCAG 2.x): what decides the text colour on an accent."""
    channels = []
    for channel in rgb(value):
        channels.append(
            channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_text(value):
    """Black or white — whichever reads on top of ``value``."""
    return "#000000" if luminance(value) > 0.35 else "#ffffff"


def derive_zones(accent, count=len(DEFAULT_ZONES)):
    """Eight zone colours from the accent when the theme names none.

    The hue of a saturated accent starts the ring; a monochrome accent would give eight
    identical greys, so the ring starts at blue instead. Lightness and saturation are fixed:
    a theme changes the hues and the chrome, not the readability of the grid.
    """
    red, green, blue = rgb(accent)
    hue, _lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    if saturation < 0.15:
        hue = MONOCHROME_HUE
    return tuple(
        hex_of(colorsys.hls_to_rgb((hue + step / count) % 1.0, DERIVED_LIGHTNESS, DERIVED_SATURATION))
        for step in range(count)
    )


class Theme:
    """A resolved palette: hex colours, zone colours, the corner radius and the font family."""

    __slots__ = ("name", "colors", "zones", "radius", "font", "path")

    def __init__(
        self,
        colors=None,
        zones=None,
        radius=DEFAULT_RADIUS,
        font=DEFAULT_FONT,
        name="default",
        path=None,
    ):
        self.colors = dict(DEFAULT_COLORS if colors is None else colors)
        self.zones = tuple(DEFAULT_ZONES if zones is None else zones)
        self.radius = radius
        self.font = font
        self.name = name
        self.path = path

    def rgb(self, token):
        return rgb(self.colors[token])

    def rgba(self, token, alpha=1.0):
        return (*self.rgb(token), alpha)

    def zone_rgb(self, index):
        return rgb(self.zones[index % len(self.zones)])

    def zone_rgba(self, index, alpha=1.0):
        return (*self.zone_rgb(index), alpha)

    def font_desc(self, size, bold=False):
        """Pango font description: ``"JetBrains Mono Bold 11"``, or ``"Sans 11"``."""
        return f"{self.font} Bold {size}" if bold else f"{self.font} {size}"

    def gtk_css(self):
        """The GTK 3 sheet of the editor, generated from the same tokens.

        Only what the editor actually shows is styled — window, toolbar, buttons, entries, lists
        and the muted hints. A full GTK theme is a different product; this keeps the editor in
        the palette without pretending to theme every application.

        Selectors carry the classes the theme itself uses (`.toolbar`, `list`, `row:selected`),
        and every surface clears `background-image`: Adwaita paints gradients there, and a
        background colour alone would not be visible under them.
        """
        colors = self.colors
        on_accent = contrast_text(colors["accent"])
        rules = (
            (
                "window, window.background, .background",
                f"background-color: {colors['bg']}; color: {colors['fg']};",
            ),
            (
                ".toolbar, toolbar, .primary-toolbar, headerbar, .titlebar, .menubar, menubar,"
                " notebook, notebook > header",
                "background-color: %s; background-image: none; color: %s;"
                % (colors["bg_panel"], colors["fg"]),
            ),
            (
                "list, row, .view, treeview, scrolledwindow, viewport, viewport.frame",
                "background-color: %s; background-image: none; color: %s;"
                % (colors["bg_panel"], colors["fg"]),
            ),
            (
                "button",
                "background-image: none; background-color: %s; border-color: %s; color: %s;"
                % (colors["bg_card"], colors["border"], colors["fg"]),
            ),
            (
                "button.flat, button.image-button",
                "background-color: transparent; border-color: transparent;",
            ),
            ("button:hover", f"background-color: {colors['bg_hover']};"),
            (
                "button:checked, button:active",
                "background-color: %s; color: %s; background-image: none;"
                % (colors["accent"], on_accent),
            ),
            (
                "entry, spinbutton",
                "background-image: none; background-color: %s; color: %s; border-color: %s;"
                % (colors["bg_card"], colors["fg"], colors["border"]),
            ),
            (
                "textview, textview text",
                f"background-color: {colors['bg_card']}; color: {colors['fg']};",
            ),
            (
                "row:selected, list row:selected, .view:selected, *:selected",
                f"background-color: {colors['accent']}; color: {on_accent};",
            ),
            (
                "tooltip",
                "background-color: %s; color: %s; border-color: %s;"
                % (colors["bg_card"], colors["fg"], colors["border"]),
            ),
            ("frame, frame > border, separator, menubar > menuitem", f"border-color: {colors['border']};"),
            (".dim-label", f"color: {colors['fg_muted']};"),
        )
        return "\n".join(f"{selector} {{ {body} }}" for selector, body in rules) + "\n"


def default_theme():
    """The palette of an installation without ``theme.conf``: exactly the old colours."""
    return Theme(DEFAULT_COLORS, DEFAULT_ZONES)


def theme_path(conf_path):
    """``theme.conf`` next to the zone config; ``None`` when there is no config path."""
    if not conf_path:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(conf_path)), "theme.conf")


def load(conf_path=None, path=None):
    """Read the theme next to ``zones.conf`` (or at ``path``); never raises for a bad file."""
    path = theme_path(conf_path) if path is None else path
    if not path or not os.path.exists(path):
        return default_theme()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        _warn(_("cannot read {}: {}").format(path, exc))
        return default_theme()
    return build(parser, path=path)


def build(parser, path=None):
    """A :class:`Theme` from a parsed file. Every key is optional; a bad one keeps the base."""
    # `[theme]` holds the switches, `[colors]` the palette. Keys in `[DEFAULT]` are accepted too:
    # configparser has no other way to write a key before the first section, and a two-line file
    # without a palette section is a legitimate thing to write.
    root = dict(parser.defaults())
    if parser.has_section("theme"):
        root.update(dict(parser["theme"]))
    colors = dict(DEFAULT_COLORS)
    preset = str(root.get("preset", "")).strip().lower()
    if preset:
        chosen = PRESETS.get(preset)
        if chosen is None:
            _warn(_("unknown preset \"{}\", the default palette is used").format(preset))
        else:
            colors.update(chosen)
    section = dict(parser["colors"]) if parser.has_section("colors") else {}
    for token in COLOR_TOKENS:
        for source in (root, section):
            if token in source:
                colors[token] = _color(source[token], token, colors[token])
                break
    zones = _zones(section.get("zones", root.get("zones", "")))
    if not zones:
        zones = derive_zones(colors["accent"])
    name = str(root.get("name") or preset or "default").strip() or "default"
    return Theme(
        colors,
        zones,
        radius=_int(root.get("radius"), DEFAULT_RADIUS, 0, MAX_RADIUS, "radius"),
        font=_font(root.get("font")),
        name=name,
        path=path,
    )


def _color(value, token, fallback):
    text = str(value).strip()
    if HEX.match(text):
        return text.lower()
    _warn(_("{}: \"{}\" is not #RRGGBB, the base colour is kept").format(token, text))
    return fallback


def _zones(value):
    """``"#a, #b"`` → ``("#a", "#b")``; malformed entries are dropped, the rest survives."""
    if not value:
        return ()
    parts = [part.strip() for part in re.split(r"[,\s]+", str(value)) if part.strip()]
    zones = []
    for part in parts[:MAX_ZONES]:
        if HEX.match(part):
            zones.append(part.lower())
        else:
            _warn(_("zone colour \"{}\" is not #RRGGBB, skipped").format(part))
    if len(parts) > MAX_ZONES:
        _warn(_("more than {} zone colours, the extra ones are ignored").format(MAX_ZONES))
    return tuple(zones)


def _int(value, default, low, high, token):
    if value is None:
        return default
    text = str(value).strip()
    try:
        number = int(text)
    except ValueError:
        _warn(_("{}: \"{}\" is not an integer, the default is kept").format(token, text))
        return default
    if not low <= number <= high:
        _warn(
            _("{}: {} is outside {}..{}, the default is kept").format(token, number, low, high)
        )
        return default
    return number


def _font(value):
    if value is None:
        return DEFAULT_FONT
    text = str(value).strip()
    if not text or any(char in text for char in ",;={}[]\n"):
        _warn(_("font: \"{}\" is not a family name, the default is kept").format(text))
        return DEFAULT_FONT
    return text

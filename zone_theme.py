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

The palette can come from the wallpaper: :func:`wallpaper_image` resolves the picture behind the
Cinnamon desktop (a file, or a GNOME background descriptor such as ``gnome-background.xml``),
:func:`from_wallpaper` samples it with GdkPixbuf, and :func:`write_theme` puts the result into
``theme.conf``. :func:`write_gtk_theme` renders the same palette as a GTK 3 controls theme, so the
headerbars of GTK applications match the surfaces GridSnap draws.

``PRESETS`` are palettes transcribed from 43PR/dotfiles (MIT), a Hyprland + Quickshell setup;
the token names are its names too, so its look is reachable without its QML. What does not come
from there is the shell itself: the bar and the panels are Quickshell and need a Wayland compositor.
"""

import colorsys
import configparser
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ElementTree

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

# A neutral chrome and one accent: surfaces are grey steps, so colour on screen belongs to the
# accent and to the zone dots. Measured on `bg`: fg 15.3:1, muted 6.6:1, accent 3.7:1 (white on
# the accent 4.6:1 — AA for the 11 px labels), danger 5.0:1, ok 8.4:1.
# The old palette reproduced the pre-theme surfaces (#12141a canvas, #1c1f24 panel, violet-cast
# borders); a wallpaper tint on every surface read as a broken palette and is gone.
DEFAULT_COLORS = {
    "bg": "#1c1c1e",
    "bg_panel": "#232325",
    "bg_card": "#2c2c2e",
    "bg_hover": "#3a3a3c",
    "fg": "#f2f2f7",
    "fg_muted": "#a1a1a6",
    "accent": "#0b74e0",
    "border": "#545456",
    "danger": "#ff453a",
    "ok": "#30d158",
}

# The picker palette: eight hues that stay apart from each other on a dark backdrop and read as
# identity dots in the zone list. They are system hues, not a pastel ring: a fill at 0.20 alpha
# keeps them distinguishable without painting the grid in candy colours.
DEFAULT_ZONES = (
    "#0a84ff",
    "#30d158",
    "#5e5ce6",
    "#ff9f0a",
    "#ff375f",
    "#bf5af2",
    "#ff453a",
    "#64d2ff",
)

DEFAULT_RADIUS = 10
DEFAULT_FONT = "Sans"

# A theme is a look, not a canvas: more zone colours than a sane grid has, or a radius that
# turns a zone into a circle, are typos, and the default is kept.
MAX_ZONES = 16
MAX_RADIUS = 64

# Autolightness of a derived zone: mid tones that read on every dark theme, and that keep the
# caption (white, 0.94 alpha) legible on the fill (the same hue at 0.20 alpha). With the neutral
# chrome the ring is a little darker and more saturated than the old pastel one: the hues are
# identity dots and picker fills, not the colour of the whole canvas.
DERIVED_LIGHTNESS = 0.56
DERIVED_SATURATION = 0.58
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


def css_rgba(value, alpha=1.0):
    """``#RRGGBB`` → ``rgba(55, 147, 190, 0.12)``: an alpha tint the GTK 3 sheet can paint.

    GTK 3 has no `color-mix`, and its ``rgba()`` takes 0–255 components while the tokens
    are hex — the conversion lives next to :func:`rgb`, not inside the sheet string.
    """
    red, green, blue = rgb(value)
    return "rgba({}, {}, {}, {})".format(
        round(red * 255), round(green * 255), round(blue * 255), alpha
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
    """Black or white — whichever reads better on top of ``value``.

    The crossover is the luminance where the two ratios meet (``(L + 0.05) ** 2 == 0.0525``,
    ``L ≈ 0.179``); the threshold is 0.18 so the default blue ``#0b74e0`` (4.58:1 either way)
    keeps white. The old fixed 0.35 picked white on orange and light blue, where white reads at
    ~3.4:1 — under AA for the 11–13 px labels a selection row carries.
    """
    return "#000000" if luminance(value) > 0.18 else "#ffffff"


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
        on_danger = contrast_text(colors["danger"])
        rules = (
            (
                "window, window.background, .background",
                f"background-color: {colors['bg']}; color: {colors['fg']};",
            ),
            # Chrome and sidebars are one material step above the window; content sits under it.
            (
                ".editor-header, .status-bar, .toolbar, toolbar, .primary-toolbar, headerbar,"
                " .titlebar, .menubar, menubar, notebook, notebook > header",
                "background-color: %s; background-image: none; color: %s;"
                % (colors["bg_panel"], colors["fg"]),
            ),
            (
                ".editor-header",
                "border-bottom: 1px solid %s; padding: 8px 10px;" % colors["border"],
            ),
            (
                ".status-bar",
                "border-top: 1px solid %s; padding: 6px 12px;" % colors["border"],
            ),
            (
                "list, row, .view, treeview, scrolledwindow, viewport, viewport.frame",
                "background-color: %s; background-image: none; color: %s;"
                % (colors["bg_panel"], colors["fg"]),
            ),
            ("list row", "padding: 6px 8px; border-radius: 6px;"),
            ("list row:hover", f"background-color: {colors['bg_hover']};"),
            (
                "button",
                "background-image: none; background-color: %s;"
                " border: 1px solid %s; border-radius: 7px; padding: 4px 10px;"
                " min-height: 24px; color: %s;"
                % (colors["bg_card"], colors["border"], colors["fg"]),
            ),
            (
                "button.flat, button.image-button",
                "background-color: transparent; border-color: transparent;",
            ),
            ("button:hover", f"background-color: {colors['bg_hover']};"),
            ("button:active", f"background-color: {colors['bg_hover']};"),
            (
                "button:checked",
                "background-color: %s; color: %s; background-image: none;"
                % (colors["accent"], on_accent),
            ),
            (
                "button.suggested-action, .suggested-action",
                "background-color: %s; color: %s; border-color: %s;"
                % (colors["accent"], on_accent, colors["accent"]),
            ),
            (
                "button.destructive-action, .destructive-action",
                "background-color: %s; color: %s; border-color: %s;"
                % (colors["danger"], on_danger, colors["danger"]),
            ),
            (
                "entry, spinbutton, combobox button",
                "background-image: none; background-color: %s; color: %s;"
                " border: 1px solid %s; border-radius: 6px; padding: 4px 8px;"
                % (colors["bg_card"], colors["fg"], colors["border"]),
            ),
            ("entry:focus, spinbutton:focus", f"border-color: {colors['accent']};"),
            (
                "textview, textview text",
                f"background-color: {colors['bg_card']}; color: {colors['fg']};",
            ),
            (
                "row:selected, list row:selected, .view:selected, *:selected",
                f"background-color: {colors['accent']}; color: {on_accent};",
            ),
            (
                "row:selected .dim-label, list row:selected .dim-label,"
                " .view:selected .dim-label, *:selected .dim-label",
                f"color: {on_accent}; opacity: 0.85;",
            ),
            (
                "button.layout-tile, button.layout-row, button.menu-row",
                "background-color: transparent; border-color: transparent; padding: 6px;",
            ),
            (
                "button.layout-tile:hover, button.layout-row:hover, button.menu-row:hover",
                f"background-color: {colors['bg_hover']};",
            ),
            (
                "button.layout-row.current",
                "background-color: %s; border-color: transparent;"
                % css_rgba(colors["accent"], 0.12),
            ),
            (
                "button.layout-row.current:hover",
                "background-color: %s;"
                % css_rgba(colors["accent"], 0.18),
            ),
            (".layout-current-mark", f"color: {colors['accent']}; font-weight: bold;"),
            (
                "button.menu-row.destructive-action",
                "background-color: transparent; color: %s; border-color: transparent;"
                % colors["danger"],
            ),
            (
                "button.menu-row.destructive-action:hover",
                "background-color: %s;" % css_rgba(colors["danger"], 0.14),
            ),
            (
                "popover, popover.background, menu, .menu",
                "background-color: %s; border: 1px solid %s;"
                " border-radius: 10px; padding: 4px;"
                % (colors["bg_panel"], colors["border"]),
            ),
            ("menuitem, modelbutton", "padding: 6px 10px; border-radius: 6px;"),
            ("menuitem:hover, modelbutton:hover", f"background-color: {colors['bg_hover']};"),
            (
                "tooltip",
                "background-color: %s; color: %s; border: 1px solid %s;"
                " border-radius: 6px;"
                % (colors["bg_card"], colors["fg"], colors["border"]),
            ),
            (
                "frame, frame > border, separator, menubar > menuitem",
                f"border-color: {colors['border']};",
            ),
            ("separator", f"background-color: {colors['border']}; min-height: 1px;"),
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


# ---------------------------------------------------------------- the wallpaper

# The first line of a generated `theme.conf`: the user sees what overwrote the file, and the daemon
# refreshes only files that carry the marker. `generated_source` parses it, `refresh_from_wallpaper`
# acts on it.
GENERATED_MARK = "# Generated by gridsnap"
GENERATED_SUFFIX = "; delete this file to return to the default palette."

# The wallpaper setting of Cinnamon, then the GNOME one — the second covers a session where the
# Cinnamon schema is absent.
WALLPAPER_SCHEMAS = ("org.cinnamon.desktop.background", "org.gnome.desktop.background")
WALLPAPER_KEY = "picture-uri"

# Sampling: a 64×64 thumbnail is enough for a palette, and its size does not depend on the image.
SAMPLE_SIZE = 64
# Four bits per channel: 4096 buckets for 4096 pixels, so the histogram is a clustering already.
SAMPLE_BITS = 4
MAX_SAMPLES = 12


def wallpaper_image(value=None):
    """The image behind the desktop as a filesystem path, or ``None``.

    ``value`` accepts what the setting holds (``file:///...``), a plain path, or ``None`` to read
    the setting itself. A GNOME background descriptor (``*.xml`` with ``<file>`` entries) resolves
    to the first entry that exists: Cinnamon accepts one, and it points at the real image.
    """
    if value is None:
        value = _wallpaper_setting()
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("file:"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    path = os.path.expanduser(text)
    if not os.path.isfile(path):
        return None
    if path.lower().endswith(".xml"):
        return _background_image(path)
    return path


def _wallpaper_setting():
    """The Cinnamon wallpaper setting; ``None`` when there is no schema or no Gio."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except (ImportError, ValueError):
        return None
    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return None
        for schema in WALLPAPER_SCHEMAS:
            if source.lookup(schema, True) is None:
                continue
            value = Gio.Settings.new(schema).get_string(WALLPAPER_KEY)
            if value:
                return value
    except Exception:  # a broken GSettings backend must not kill the editor
        return None
    return None


def _background_image(descriptor):
    """The first existing image of a GNOME background descriptor.

    Two shapes are read: GNOME's own ``<file>/path.png</file>`` and Debian's
    ``<file><size width="1920" height="1080">/path.svg</size>…</file>``, where the paths sit on
    the ``<size>`` children and the ``<file>`` element itself holds only whitespace (the live
    Cinnamon setting on this desktop points at the second kind).
    """
    try:
        tree = ElementTree.parse(descriptor)
    except (OSError, ElementTree.ParseError):
        return None
    for node in tree.iter():
        if node.tag not in ("file", "size"):
            continue
        text = (node.text or "").strip()
        if text and os.path.isfile(text):
            return text
    return None


def _pixels(path, size=SAMPLE_SIZE):
    """The RGB bytes of a ``size``×``size`` thumbnail, as ``(r, g, b)`` in 0..255."""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError) as exc:
        raise ValueError(_("GdkPixbuf is not available: {}").format(exc)) from exc
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, False)
    except Exception as exc:  # GdkPixbuf raises a plain GLib.Error
        raise ValueError(_("cannot read the image: {}").format(exc)) from exc
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    alpha = pixbuf.get_has_alpha()
    data = pixbuf.get_pixels()
    for row in range(height):
        base = row * stride
        for column in range(width):
            index = base + column * channels
            if alpha and data[index + 3] < 128:
                continue
            yield data[index], data[index + 1], data[index + 2]


def samples_of(path, buckets=MAX_SAMPLES):
    """``[((r, g, b), weight), ...]`` for an image: the most common colours, by area.

    Binning at four bits per channel turns the thumbnail into at most 4096 buckets; the weighted
    average of each bucket is its colour, the count is its weight. Deterministic, and good enough
    for a palette: the exact shades do not survive on screen anyway.
    """
    shift = 8 - SAMPLE_BITS
    bins = {}
    for red, green, blue in _pixels(path):
        key = (red >> shift, green >> shift, blue >> shift)
        entry = bins.get(key)
        if entry is None:
            bins[key] = [1, red, green, blue]
        else:
            entry[0] += 1
            entry[1] += red
            entry[2] += green
            entry[3] += blue
    samples = [
        ((red / count / 255, green / count / 255, blue / count / 255), count)
        for count, red, green, blue in bins.values()
    ]
    samples.sort(key=lambda item: item[1], reverse=True)
    return samples[:buckets]


def from_wallpaper(path, buckets=MAX_SAMPLES):
    """A :class:`Theme` from the colours of an image; raises ``ValueError`` when it cannot be read."""
    theme = theme_from_samples(samples_of(path, buckets))
    stem = os.path.splitext(os.path.basename(path))[0]
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")
    theme.name = "wallpaper-" + (clean or "image")
    return theme


def theme_from_samples(samples, name="wallpaper"):
    """A :class:`Theme` from ``((r, g, b), weight)`` samples in 0..1.

    The wallpaper chooses the **accent** and, through it, the zone hues; the chrome stays the
    neutral ramp of :data:`DEFAULT_COLORS`. A sampled photo has no scheme of its own, and tinting
    every surface with its dominant colour is what made the generated palettes read as broken
    (a violet cast on borders, muddy fills). ``danger`` and ``ok`` stay the default too: they are
    signals, not decoration, and an image has no opinion about them.
    """
    total = sum(weight for _colour, weight in samples if weight > 0)
    if not samples or total <= 0:
        raise ValueError(_("the wallpaper has no readable colours"))
    accent = _accent_colour(samples, total)
    accent_hue, accent_lightness, accent_saturation = colorsys.rgb_to_hls(*accent)
    colours = dict(DEFAULT_COLORS)
    colours["accent"] = _hls(
        accent_hue,
        min(0.66, max(0.48, accent_lightness)),
        min(0.80, max(0.45, accent_saturation)),
    )
    return Theme(colours, derive_zones(colours["accent"]), name=name)


def _accent_colour(samples, total):
    """The most vivid usable cluster; a monochrome image falls back to its lightest colour."""
    best = None
    for colour, weight in samples:
        hue, lightness, saturation = colorsys.rgb_to_hls(*colour)
        if not 0.18 <= lightness <= 0.85:
            continue
        score = saturation * (weight / total) ** 0.3
        if best is None or score > best[0]:
            best = (score, hue, lightness, saturation)
    if best is not None:
        _score, hue, lightness, saturation = best
        if saturation >= 0.10:
            return colorsys.hls_to_rgb(hue, lightness, saturation)
    lightest = max(samples, key=lambda item: sum(item[0]))[0]
    hue, lightness, _saturation = colorsys.rgb_to_hls(*lightest)
    return colorsys.hls_to_rgb(hue, max(0.55, lightness), 0.04)


def _hls(hue, lightness, saturation):
    """``(hue, lightness, saturation)`` in 0..1 → ``#rrggbb``."""
    return hex_of(
        colorsys.hls_to_rgb(
            hue % 1.0,
            max(0.0, min(1.0, lightness)),
            max(0.0, min(1.0, saturation)),
        )
    )


def write_theme(theme, path, source=None):
    """Write ``theme.conf`` for ``theme``; returns the path.

    The file is marked as generated: the daemon refreshes only files that carry the marker (see
    :func:`refresh_from_wallpaper`), and a user who deletes it returns to the default palette.
    Written atomically, so a picker reading the file during the write never sees half of it.
    """
    header = GENERATED_MARK + (f" from {source}" if source else "")
    lines = [
        header + GENERATED_SUFFIX,
        "[theme]",
        f"name = {theme.name}",
        f"radius = {theme.radius}",
        f"font = {theme.font}",
        "",
        "[colors]",
    ]
    lines.extend(f"{token} = {theme.colors[token]}" for token in COLOR_TOKENS)
    lines.append("zones = " + ", ".join(theme.zones))
    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)
    return path


def generated_source(path):
    """The image a generated ``theme.conf`` was written from; ``None`` when the file is not ours.

    ``None`` covers a missing file and a hand-written palette: neither is ever rewritten by the
    daemon. An empty string means the file carries the marker without a source (``write_theme``
    was called without one) — still generated, so still refreshed.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    if not first.startswith(GENERATED_MARK):
        return None
    prefix = GENERATED_MARK + " from "
    if not first.startswith(prefix):
        return ""
    rest = first[len(prefix) :]
    cut = rest.find(GENERATED_SUFFIX)
    return (rest if cut < 0 else rest[:cut]).strip()


def refresh_from_wallpaper(conf_path=None, path=None, wallpaper=None):
    """Rebuild a generated ``theme.conf`` when the wallpaper behind it changed.

    Only a file whose first line carries :data:`GENERATED_MARK` is touched: a hand-written palette
    belongs to the user, whatever the wallpaper is. ``wallpaper`` is what the setting holds (a
    ``file://`` URI, a path, a GNOME descriptor) or ``None`` to read the setting itself.

    Returns the written path, or ``None`` when nothing was written: no theme file, a hand-written
    one, no wallpaper to sample, the same image as the marker names, or an unreadable image
    (:func:`from_wallpaper` raises ``ValueError`` and the file keeps its palette).
    """
    path = theme_path(conf_path) if path is None else path
    if not path:
        return None
    source = generated_source(path)
    if source is None:
        return None
    image = wallpaper_image(wallpaper)
    if not image:
        return None
    if source and os.path.abspath(source) == os.path.abspath(image):
        return None
    return write_theme(from_wallpaper(image), path, source=image)


# ------------------------------------------------------- window controls (GTK 3 theme)

# The window manager does not read theme files on Cinnamon: `libmuffin` 6.4 never opens
# `metacity-1/` (its `meta_theme_new` is a plain allocation, and a deliberately red metacity theme
# changes nothing on a nested Muffin), so the borders of server-side decorated windows cannot be
# themed. What can be themed are the client-side decorations of GTK applications — headerbars,
# buttons, entries — which is what this theme is, and the Cinnamon shell itself, which is a
# different product.
GTK_BASE = "resource:///org/gtk/libgtk/theme/Adwaita/gtk-contained-dark.css"

# Where Cinnamon's Theme → Controls looks for a user theme. The files are marked as generated so
# that the daemon never rewrites a theme the user wrote: deleting the directory (or the marker
# lines) takes the files back.
GTK_THEME_DIR = "~/.themes/Gridsnap"
GTK_NAME = "Gridsnap"
GTK_COMMENT = "Gridsnap controls"
GTK_MARK = "# Generated by gridsnap"
GTK_CSS_MARK = "/* Generated by gridsnap"


def gtk_theme_css(theme):
    """The ``gtk.css`` of a controls theme: Adwaita dark as the base, the palette on top."""
    return (
        f'@import url("{GTK_BASE}");\n'
        + theme.gtk_css()
        + f"decoration, window.csd {{ border-color: {theme.colors['border']}; }}\n"
    )


def _gtk_theme_files(theme):
    """The two files of a controls theme as ``(index.theme, gtk-3.0/gtk.css)`` text."""
    index = (
        f"{GTK_MARK}; delete this directory to remove the theme.\n"
        "[Desktop Entry]\n"
        "Type=X-GNOME-Metatheme\n"
        f"Name={GTK_NAME}\n"
        f"Comment={GTK_COMMENT} ({theme.name})\n"
        "Encoding=UTF-8\n"
    )
    css = (
        f'{GTK_CSS_MARK} from "{theme.name}"; the daemon refreshes this file while this '
        "line stays. */\n" + gtk_theme_css(theme)
    )
    return index, css


def write_gtk_theme(directory, theme):
    """Write a GTK 3 controls theme (``index.theme`` + ``gtk-3.0/gtk.css``); returns the paths."""
    directory = os.path.expanduser(directory)
    css_dir = os.path.join(directory, "gtk-3.0")
    os.makedirs(css_dir, exist_ok=True)
    index_path = os.path.join(directory, "index.theme")
    css_path = os.path.join(css_dir, "gtk.css")
    index, css = _gtk_theme_files(theme)
    with open(index_path, "w", encoding="utf-8") as handle:
        handle.write(index)
    with open(css_path, "w", encoding="utf-8") as handle:
        handle.write(css)
    return [index_path, css_path]


def _controls_dir(directory=None):
    return os.path.expanduser(directory or GTK_THEME_DIR)


def generated_gtk_theme(directory=None):
    """Whether the controls theme at ``directory`` is GridSnap's to rewrite.

    A marked ``index.theme`` is ours. So is the unmarked file the first version wrote
    (``Name=Gridsnap`` plus a ``Gridsnap controls (…)`` comment): adopting it means an
    installation does not have to run the editor action once more after the upgrade. Anything
    else — a theme the user wrote or copied — is not touched.
    """
    path = os.path.join(_controls_dir(directory), "index.theme")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle]
    except OSError:
        return False
    if lines and lines[0].startswith(GTK_MARK):
        return True
    return f"Name={GTK_NAME}" in lines and any(
        line.startswith(f"Comment={GTK_COMMENT} (") for line in lines
    )


def _same_content(path, text):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read() == text
    except OSError:
        return False


def refresh_gtk_theme(conf_path=None, directory=None, theme=None):
    """Bring a generated controls theme in step with the current palette.

    Only a directory :func:`generated_gtk_theme` claims is touched. The palette is ``theme`` when
    given, otherwise the one the surfaces read (``theme.conf`` next to ``conf_path``, else the
    defaults). Returns the directory when the files were rewritten, ``None`` when they were
    already current or the theme is not ours — so an idle poll does not churn mtimes.

    GTK 3 does not watch its theme for changes: applications that are already running keep the
    old sheet until they restart (or the theme is re-selected in Cinnamon Settings). GTK 4 and
    libadwaita are out of scope: libadwaita ships its own style and does not read a theme's
    ``gtk-4.0`` sheet for its widgets.
    """
    directory = _controls_dir(directory)
    if not generated_gtk_theme(directory):
        return None
    if theme is None:
        theme = load(conf_path)
    index_text, css_text = _gtk_theme_files(theme)
    index_path = os.path.join(directory, "index.theme")
    css_path = os.path.join(directory, "gtk-3.0", "gtk.css")
    if _same_content(index_path, index_text) and _same_content(css_path, css_text):
        return None
    write_gtk_theme(directory, theme)
    return directory

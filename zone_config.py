#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zone_config.py — pure work with zones.conf, without GTK.

Everything the zone editor (``zone-editor.py``) does with the config lives here:
the zone model, loading the layout through ``zone-tiler.py``, saving in
canonical form and carrying "foreign" sections (``[rules]``, ``[daemon]``, …)
verbatim. Per-monitor zone sets live in the ``[zones]`` (default) and
``[zones:<output name>]`` sections (for example ``[zones:HDMI-1]``); the module
knows both kinds and does not lose the neighbouring sets when one is saved.

The module deliberately does not import gi/GTK: it can and should be tested
by ordinary unit tests in CI, where ``python3-gi`` is not installed.
"""

import configparser
import importlib.util
import json
import os
import re

import zone_actions
import zone_tabs

from zone_i18n import _  # UI strings; without a catalog — the source text

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")

# Sections the editor manages itself: they must not be carried as “foreign”.
RESERVED_SECTIONS = frozenset({"grid", "zones", "keys", "move"})

# A monitor zone set: ``[zones:<output name>]``. The output name (eDP-1, HDMI-1)
# is not a zone name, so it is not mangled into quotes/dashes.
MONITOR_SECTION_PREFIX = "zones:"

# Moving a window into the neighbouring zone: ``[move]``, the key is a direction,
# the value a Cinnamon binding. The section is optional: without it the
# defaults apply. Keys that are not directions are a config error: a silently
# ignored typo would leave a person without a key, and they would not notice.
MOVE_SECTION = "move"
MOVE_DIRECTIONS = ("left", "right", "up", "down")
# ``Win``+arrows are the bindings Cinnamon (muffin) takes for its own
# ``push-tile-*``; that is why we take them as the default, but a person may change them.
DEFAULT_MOVE_KEYS = {
    "left": "<Super>Left",
    "right": "<Super>Right",
    "up": "<Super>Up",
    "down": "<Super>Down",
}

# The “this zone only” mode: ``[focus]``, ``key`` is the binding, ``zone`` the zone the
# window is dropped into (optional). The section is optional: without it the
# mode is not configured. Keys other than ``key``/``zone`` are a config error,
# as is an empty ``zone``: a silently ignored typo would leave a person without a key.
#
# The section is deliberately absent from :data:`RESERVED_SECTIONS`: the editor
# does not edit it but carries it verbatim as foreign — that is how it survives a save.
FOCUS_SECTION = "focus"

# Tabs in a zone: ``[tabs]``, ``key`` is the “next window of the zone” binding,
# ``height`` is the height of the tabs strip in pixels (optional, the default is
# :data:`zone_tabs.STRIP_HEIGHT`). The section is optional. Like ``[focus]``, it
# is not in :data:`RESERVED_SECTIONS`: the editor does not edit it but carries it
# verbatim as foreign. An unknown key is a config error (a typo would leave a
# person without a key), and so is garbage in ``height``: a strip of zero or
# negative height is not drawn, and the tabs would silently become invisible.
TABS_SECTION = "tabs"
TABS_FIELDS = ("key", "height")

# Actions: one section is one action instance, ``[action:<name>]`` (like
# ``[zones:<output>]`` is one set per screen). The section is not in
# :data:`RESERVED_SECTIONS`: the editor does not edit it but carries it verbatim,
# like ``[focus]``/``[tabs]``. The action name comes from ``[a-z0-9_-]``: it has
# already been lowercased by ``ConfigParser`` and ends up in the command line of
# a key (``zone-tiler.py action <name>``), so spaces and quotes are not allowed in it.
ACTION_SECTION_PREFIX = "action:"
ACTION_FIELDS = ("run", "zone", "key", "command", "axis")
ACTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Workspaces: ``[workspaces]``, the key is the workspace number as in the Cinnamon
# UI (starting at 1), the value is a profile name from ``templates/<name>.conf``.
WORKSPACES_SECTION = "workspaces"

# Screens: ``[monitors]``, the key is an output name (``HDMI-1``, ``eDP-1``), the
# value a profile name. The section is machine-owned: it is not part of a profile
# and ``profile use`` leaves it alone (carried verbatim, like ``[grid]``). Line
# order matters: the first screen's profile breaks the tie (``profile use-monitors``).
MONITORS_SECTION = "monitors"

# The “workspace without a profile” label in the “Workspace profiles” dialog. It
# cannot match a real profile name: names are sanitized, a space becomes a dash.
WORKSPACE_SHARED = _("default set")

# The “screen without a binding” label in the “Screen profiles” dialog — the same
# string as for workspaces: an empty value removes the binding.
MONITOR_SHARED = WORKSPACE_SHARED

# How the dialog marks an output from `[monitors]` that is not on the machine now.
MONITOR_OFFLINE_NOTE = _("not on screen")

# How many workspaces to show in the dialog if ``_NET_NUMBER_OF_DESKTOPS`` is unreadable.
WORKSPACE_DESKTOPS_DEFAULT = 4

# Order of the "foreign" sections when saving: [rules] and [ignore] before [keys],
# [daemon] after. ``[move]`` writes itself: it is ours (see
# :data:`RESERVED_SECTIONS`), but the editor does not edit it and has to preserve
# it — the body comes from the previous file (or from ``extra``, if passed explicitly).
BEFORE_KEYS = ("rules", "ignore")
AFTER_KEYS = ("daemon",)


def load_zonetiler(path=ZONEBIN):
    """Load ``zone-tiler.py`` as a module (it holds the config parser)."""
    spec = importlib.util.spec_from_file_location("zone_tiler", path)
    if spec is None or spec.loader is None:
        raise ImportError(_("zone_config: failed to load {path}").format(path=path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load_zonetiler()


# -------------------------------------------------------------------------- data


class Zone:
    def __init__(self, name, x, y, w, h, binding=""):
        self.name = name
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.binding = binding

    def copy(self):
        return Zone(self.name, self.x, self.y, self.w, self.h, self.binding)

    def as_tuple(self):
        return (self.x, self.y, self.w, self.h)

    def fmt(self):
        return ", ".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in self.as_tuple())


class _SpecParser:
    """Parsing zone specs with the rules of ``zone-tiler.Grid``, without X and GTK.

    Only the pure methods are taken from ``Grid``: ``_parse_zone`` (the ``x,y,w,h``
    and ``c0:c1, r0:r1`` forms with their checks), ``zone_spec`` and ``fractions`` —
    so the parsing rules and the error messages do not drift away from the tiler.
    It keeps its own state (``cols``/``rows`` and the parsed ``zones``) and does not
    read the config.
    """

    _parse_zone = zt.Grid._parse_zone
    fractions = zt.Grid.fractions

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.zones = {}

    def add(self, name, spec):
        """Parse the zone of a ``name = spec`` pair; returns the name as spelled in the file."""
        key = name.strip()
        self.zones[key] = self._parse_zone(key, spec)
        return key

    def fractions_of(self, name):
        """``(x, y, w, h)`` in work-area fractions for a parsed zone."""
        return self.fractions(name)

    def zone_spec(self, zone, monitor_name=None):
        """The description of a zone from an already collected set.

        In ``Grid`` this method looks first in the monitor set, then in the common
        ``[zones]``. Here the set is already collected from the right section, so there
        is nowhere left to look; ``monitor_name`` is accepted for signature
        compatibility (``Grid.fractions`` calls ``self.zone_spec``).
        """
        spec = self.zones.get(zone)
        if spec is None:
            zt.fail(
                _("zone-tiler: no zone '{zone}' in the set").format(zone=zone),
                zt.EXIT_CONFIG,
            )
        return spec


def load_workspace_map(path):
    """``{workspace_number: profile_name}`` from the ``[workspaces]`` section; no section — ``{}``.

    Keys are **1-based**, like the workspace numbers in the Cinnamon UI ("Workspace 1"),
    while in EWMH ``_NET_CURRENT_DESKTOP`` they are zero-based; the codes are translated
    into names by ``zone-tiler.grid_for_workspace()``. The value is a profile name
    (``templates/<name>.conf``); the existence of the file is checked at the moment
    of use: a profile may appear later, and that is no reason to break reading
    the whole config.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(WORKSPACES_SECTION):
        return {}
    mapping = {}
    for key, value in cp.items(WORKSPACES_SECTION):
        number = str(key).strip()
        if not number.isdigit() or int(number) < 1:
            zt.fail(
                _(
                    "zone_config: in [{section}] workspace number must be an integer from 1, not '{number}'"
                ).format(section=WORKSPACES_SECTION, number=number),
                zt.EXIT_CONFIG,
            )
        profile = sanitize_profile_name(value)
        if not profile:
            zt.fail(
                _(
                    "zone_config: in [{section}] workspace {number} has an empty profile name"
                ).format(section=WORKSPACES_SECTION, number=number),
                zt.EXIT_CONFIG,
            )
        mapping[int(number)] = profile
    return mapping


def load_move_keys(path):
    """``{direction: shortcut}`` from the ``[move]`` section; no section — the defaults.

    The keys are only the directions :data:`MOVE_DIRECTIONS` (``left``/``right``/
    ``up``/``down``), the values are shortcuts in Cinnamon syntax
    (``<Super>Left``). No file or no section — :data:`DEFAULT_MOVE_KEYS`
    as a whole, so a file without ``[move]`` works as before.

    An unknown key or an empty shortcut is a config error (code 3): it is a
    typo that would have left a person without a key and with no idea why.
    Directions missing from the section are filled in from the defaults — there is
    no need to write all four to replace one.
    """
    keys = dict(DEFAULT_MOVE_KEYS)
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(MOVE_SECTION):
        return keys
    for name, value in cp.items(MOVE_SECTION):
        # ConfigParser lowercases the keys, so ``Left`` in the
        # file counts as a direction too.
        direction = str(name).strip().lower()
        if direction not in MOVE_DIRECTIONS:
            zt.fail(
                _(
                    "zone_config: in [{section}] unknown direction '{direction}': available {directions}"
                ).format(
                    section=MOVE_SECTION,
                    direction=direction,
                    directions=", ".join(MOVE_DIRECTIONS),
                ),
                zt.EXIT_CONFIG,
            )
        combo = str(value).strip()
        if not combo:
            zt.fail(
                _(
                    "zone_config: in [{section}] direction '{direction}' has an empty shortcut"
                ).format(section=MOVE_SECTION, direction=direction),
                zt.EXIT_CONFIG,
            )
        keys[direction] = combo
    return keys


def load_focus_keys(path):
    """The ``[focus]`` section: ``{"key": shortcut, "zone": zone name | None}``.

    ``None`` — the mode is not configured: no file, no section or an empty ``key``.
    ``zone`` is optional: without it the shortcut only minimises the other windows
    ("this window only"), with it the window is also dropped into the zone. An unknown
    key in the section and an empty ``zone`` value are a config error (code 3): a typo
    that would have left a person without a key and with no idea why.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(FOCUS_SECTION):
        return None
    key, zone = None, None
    for name, value in cp.items(FOCUS_SECTION):
        # ConfigParser lowercases the keys, so ``Key`` in the
        # file counts too.
        field = str(name).strip().lower()
        if field not in ("key", "zone"):
            zt.fail(
                _(
                    "zone_config: in [{section}] unknown key '{field}': available key, zone"
                ).format(section=FOCUS_SECTION, field=field),
                zt.EXIT_CONFIG,
            )
        text = str(value).strip()
        if field == "key":
            key = text or None
            continue
        if not text:
            zt.fail(
                _("zone_config: in [{section}] empty zone value").format(
                    section=FOCUS_SECTION
                ),
                zt.EXIT_CONFIG,
            )
        zone = text
    if not key:
        # A section without a shortcut is not a mode: without a key it is unreachable.
        return None
    return {"key": key, "zone": zone}


def load_tabs_config(path):
    """The ``[tabs]`` section: ``{"key": shortcut | None, "height": strip height}``.

    ``height`` is the height of the tabs strip in pixels, an integer greater than zero;
    no section or no key — :data:`zone_tabs.STRIP_HEIGHT` (the strip default, and not
    a second number in the config). ``key`` is the Cinnamon shortcut for "the next window
    of the zone":
    ``None`` — no key, while the other fields are still read. Garbage in
    ``height`` (not a number, zero, negative) and an unknown key in the section are
    a config error (code 3): a strip of zero height is not visible, and a person would
    not have understood why the tabs stopped showing.
    """
    config = {"key": None, "height": int(zone_tabs.STRIP_HEIGHT)}
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(TABS_SECTION):
        return config
    for name, value in cp.items(TABS_SECTION):
        # ConfigParser lowercases the keys, so ``Key`` in the
        # file counts too.
        field = str(name).strip().lower()
        if field not in TABS_FIELDS:
            zt.fail(
                _(
                    "zone_config: in [{section}] unknown key '{field}': available {fields}"
                ).format(
                    section=TABS_SECTION,
                    field=field,
                    fields=", ".join(TABS_FIELDS),
                ),
                zt.EXIT_CONFIG,
            )
        text = str(value).strip()
        if field == "key":
            config["key"] = text or None
            continue
        config["height"] = _tabs_height(text)
    return config


def _tabs_height(text):
    """The tabs strip height from ``[tabs] height``: an integer above zero, else code 3."""
    try:
        height = int(str(text).strip(), 10)
    except (TypeError, ValueError):
        height = 0
    if height <= 0:
        zt.fail(
            _(
                "zone_config: in [{section}] height must be an integer number of pixels greater than zero, not '{text}'"
            ).format(section=TABS_SECTION, text=text),
            zt.EXIT_CONFIG,
        )
    return height


def load_actions(path):
    """The actions from the ``[action:<name>]`` sections: ``{name: record}``.

    A record is a dictionary with the fields of :data:`ACTION_FIELDS`:

    * ``run`` — mandatory: ``builtin:<name>`` from ``zone_actions.BUILTINS`` or an
      external command (parsed by ``zone_actions.expand_command``);
    * ``zone`` — an optional name of the subject zone; without it the subject is
      the active window and the zone it sits in;
    * ``key`` — an optional Cinnamon shortcut for ``setup-hotkeys.py``;
    * ``command`` — only for ``builtin:terminal``: what to launch the terminal with
      (the default is ``zone_actions.DEFAULT_TERMINAL``);
    * ``axis`` — only for ``builtin:mirror``: an axis from ``zone_actions.MIRROR_AXES``
      (the default is ``zone_actions.DEFAULT_MIRROR_AXIS``).

    Missing optional fields are ``None``; no file or not a single section — an
    empty dictionary (there are no actions, and that is not an error).

    Config error (code 3): an empty ``run``/``zone``/``key``/``command``, an unknown
    builtin action name, an unknown section field, ``command`` or ``axis`` on an
    action that does not take it, an axis not from :data:`zone_actions.MIRROR_AXES`,
    a section name not from ``[a-z0-9_-]``. All of these are typos that would have
    left a person without an action or without a key and with no idea why.

    The section name and the action name are compared case-insensitively (like
    ``ConfigParser`` keys): ``[action:Main]`` and ``[action:main]`` are not two actions
    with one name but a typo, so that is code 3 as well. The order of the result is
    the order of the sections in the file.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None:
        return {}
    actions, sections = {}, {}
    for section in cp.sections():
        name = _action_section_name(section)
        if name is None:
            continue
        if name in sections:
            zt.fail(
                _(
                    "zone_config: action '{name}' is defined twice: [{first}] and [{second}] - action names are compared case-insensitively"
                ).format(name=name, first=sections[name], second=section),
                zt.EXIT_CONFIG,
            )
        actions[name] = _action_entry(section, cp.items(section))
        sections[name] = section
    return actions


def _action_section_name(section):
    """The action name from a ``[action:<name>]`` section name; not our section — ``None``.

    The name is lowercased: it ends up in the command line of a key
    (``zone-tiler.py action <name>``), so spaces, dots and quotes in it are
    not allowed (:data:`ACTION_NAME_RE`), and an empty ``[action:]`` is a typo,
    not an action without a name.
    """
    name = str(section)
    if name[: len(ACTION_SECTION_PREFIX)].lower() != ACTION_SECTION_PREFIX:
        return None
    short = name[len(ACTION_SECTION_PREFIX):].strip().lower()
    if not short:
        zt.fail(
            _(
                "zone_config: empty action name in section [{section}]: write [action:<name>], for example [action:term-main]"
            ).format(section=name),
            zt.EXIT_CONFIG,
        )
    if not ACTION_NAME_RE.match(short):
        zt.fail(
            _(
                "zone_config: action name '{name}' in section [{section}]: only lowercase Latin letters, digits, dash and underscore are allowed - the name ends up in the key's command line"
            ).format(name=short, section=name),
            zt.EXIT_CONFIG,
        )
    return short


def _action_entry(section, items):
    """The action record from the pairs of a section; anything off contract is code 3.

    All the fields are read at once and checked afterwards: ``command``/``axis`` depend
    on which builtin action stands in ``run``, while the line order in the section is
    arbitrary. ``run`` is stored as in the file (an external command is
    case-sensitive), ``axis`` lowercased, the way
    ``zone_actions.mirror_fractions`` parses it.
    """
    raw = {}
    for field, value in items:
        # ConfigParser lowercases the keys, so ``Run`` in the file
        # counts as a field too.
        field = str(field).strip().lower()
        if field not in ACTION_FIELDS:
            zt.fail(
                _(
                    "zone_config: in [{section}] unknown field '{field}': available {fields}"
                ).format(
                    section=section,
                    field=field,
                    fields=", ".join(ACTION_FIELDS),
                ),
                zt.EXIT_CONFIG,
            )
        raw[field] = str(value).strip()

    run = raw.get("run", "")
    if not run:
        zt.fail(
            _(
                "zone_config: in [{section}] required field run is empty: builtin:<name> or an external command is required"
            ).format(section=section),
            zt.EXIT_CONFIG,
        )
    try:
        builtin = zone_actions.builtin_name(run)
    except zone_actions.ActionError as exc:
        # The core checks the builtin action name: the list of known ones and the message
        # about a typo must be the same in the config and in the tiler.
        zt.fail(
            _("zone_config: in [{section}] {error}").format(section=section, error=exc),
            zt.EXIT_CONFIG,
        )

    entry = {"run": run, "zone": None, "key": None, "command": None, "axis": None}
    for field in ("zone", "key"):
        if field not in raw:
            continue
        if not raw[field]:
            zt.fail(
                _("zone_config: in [{section}] empty value of {field}").format(
                    section=section, field=field
                ),
                zt.EXIT_CONFIG,
            )
        entry[field] = raw[field]

    if "command" in raw:
        if builtin != zone_actions.TERMINAL:
            zt.fail(
                _(
                    "zone_config: in [{section}] the command field exists only for builtin:{builtin}, but run = '{run}'"
                ).format(
                    section=section,
                    builtin=zone_actions.TERMINAL,
                    run=run,
                ),
                zt.EXIT_CONFIG,
            )
        if not raw["command"]:
            zt.fail(
                _(
                    "zone_config: in [{section}] empty command value: what to launch the terminal with"
                ).format(section=section),
                zt.EXIT_CONFIG,
            )
        entry["command"] = raw["command"]

    if "axis" in raw:
        if builtin != zone_actions.MIRROR:
            zt.fail(
                _(
                    "zone_config: in [{section}] the axis field exists only for builtin:{builtin}, but run = '{run}'"
                ).format(section=section, builtin=zone_actions.MIRROR, run=run),
                zt.EXIT_CONFIG,
            )
        axis = raw["axis"].lower()
        if axis not in zone_actions.MIRROR_AXES:
            zt.fail(
                _(
                    "zone_config: in [{section}] unknown mirror axis axis = '{axis}': available {axes}"
                ).format(
                    section=section,
                    axis=raw["axis"],
                    axes=", ".join(zone_actions.MIRROR_AXES),
                ),
                zt.EXIT_CONFIG,
            )
        entry["axis"] = axis
    return entry


def _workspace_number(value):
    """The workspace number from a map key: an integer from 1, else ``ValueError``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            _("workspace number must be an integer: {value!r}").format(value=value)
        ) from None
    if number < 1:
        raise ValueError(
            _("workspace number must be from 1, not {number}").format(number=number)
        )
    return number


def _workspace_body(mapping):
    """The body of the ``[workspaces]`` section: ``number = name``, ascending by number.

    The profile name is sanitized by the same rule as when reading
    (:func:`sanitize_profile_name`), so writing and reading understand each
    other. A number that is not an integer or is below 1, and a name that is empty
    after sanitizing — ``ValueError``: there is no point in writing a knowingly unreadable one.
    """
    items = {}
    for number, name in (mapping or {}).items():
        items[_workspace_number(number)] = name
    lines = []
    for number in sorted(items):
        profile = sanitize_profile_name(items[number])
        if not profile:
            raise ValueError(
                _("empty profile name for workspace {number}: {name!r}").format(
                    number=number, name=items[number]
                )
            )
        lines.append(f"{number} = {profile}")
    return lines


def _section_line_spans(lines, section):
    """``(start, end)`` of every ``[section]`` section: the header and the body."""
    spans, start = [], None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if start is not None:
            spans.append((start, index))
            start = None
        if match.group(1).strip().lower() == section:
            start = index
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def _workspace_spans(lines):
    """``(start, end)`` of every ``[workspaces]`` section."""
    return _section_line_spans(lines, WORKSPACES_SECTION)


def _replace_section(lines, section, block):
    """Replace all ``[section]`` sections with one ``block`` (header + body).

    The block takes the place of the first section, and duplicates (if they were
    bred by hand) go away: ``ConfigParser`` fails on a repeated section. An empty
    ``block`` removes the section; no section at all — appended at the end. Blank lines are tidied up.
    """
    spans = _section_line_spans(lines, section)
    if spans:
        insert_at = spans[0][0]
        drop = set()
        for start, end in spans:
            drop.update(range(start, end))
        result = []
        for index, line in enumerate(lines):
            if index == insert_at:
                result.extend(block)
            if index not in drop:
                result.append(line)
    else:
        result = list(lines)
        if block:
            while result and not result[-1].strip():
                result.pop()
            if result:
                result.append("")
            result.extend(block)
    return _tidy_blank(result)


def _tidy_blank(lines):
    """Blank lines: no leading, no trailing, and no pairs in a row."""
    out = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def save_workspace_map(path, mapping):
    """Write the ``[workspaces]`` section: workspace number (from 1) → profile name.

    Only this section is edited, and as text: ``[grid]``, the zone sets ``[zones]``/
    ``[zones:<output>]``, ``[keys]``, ``[rules]``, ``[daemon]`` and the other lines
    of the file stay verbatim, as after a manual edit. An empty ``mapping`` removes
    the section altogether, the numbers are written ascending. Profile names are
    sanitized, so ``{"2": "my cinema"}`` becomes ``2 = my-cinema``.
    No file — it will be created with this one section. A bad number (not an integer
    or below 1) and an empty profile name are ``ValueError``. Returns the path.
    """
    body = _workspace_body(mapping)
    block = [f"[{WORKSPACES_SECTION}]"] + body if body else []
    lines = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    lines = _replace_section(lines, WORKSPACES_SECTION, block)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n" if lines else "")
    return path


def _monitor_body(mapping):
    """The body of the ``[monitors]`` section: ``output = profile name`` in the order given.

    Line order matters: the profile of the **first** screen breaks the tie when merging
    (``profile use-monitors``), so there is no sorting — we write it the way it was
    passed. The output name is written as is (usually that is how
    ``xrandr --listmonitors`` prints it), but it must be compared case-insensitively:
    the file may hold ``hdmi-1``, and ``ConfigParser`` lowercases keys anyway.
    An empty output name or an empty (after sanitizing) profile name is ``ValueError``:
    there is no point in writing a knowingly unreadable config.
    """
    lines = []
    for output, name in (mapping or {}).items():
        output = str(output).strip()
        if not output:
            raise ValueError(_("empty output name: {output!r}").format(output=output))
        profile = sanitize_profile_name(name)
        if not profile:
            raise ValueError(
                _("empty profile name for output {output}: {name!r}").format(
                    output=output, name=name
                )
            )
        lines.append(f"{output} = {profile}")
    return lines


def load_monitor_map(path):
    """``{output name: profile name}`` from the ``[monitors]`` section; no section — ``{}``.

    The key is the output name as ``xrandr --listmonitors`` prints it (``HDMI-1``,
    ``eDP-1``), the value is a profile name (``templates/<name>.conf``). Output names
    must be compared **case-insensitively**: ``ConfigParser`` lowercases keys, so
    ``HDMI-1`` comes back as ``hdmi-1`` — exactly the way the zone sets of
    ``Grid.zones_for`` are compared tolerantly (see :func:`resolve_profile`).

    Line order matters (the profile of the first screen breaks the tie when merging),
    so the dictionary is in file order, not sorted. The existence of the profile file
    is not checked here: a profile could have been renamed, and that is a reason to
    fall back to the next level with a warning, not to break reading the config (as
    with ``[workspaces]``). An empty profile name is a config error (code 3): a line that means nothing.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None or not cp.has_section(MONITORS_SECTION):
        return {}
    mapping = {}
    for key, value in cp.items(MONITORS_SECTION):
        output = str(key).strip()
        if not output:
            zt.fail(
                _("zone_config: in [{section}] empty output name").format(
                    section=MONITORS_SECTION
                ),
                zt.EXIT_CONFIG,
            )
        profile = sanitize_profile_name(value)
        if not profile:
            zt.fail(
                _(
                    "zone_config: in [{section}] output {output} has an empty profile name"
                ).format(section=MONITORS_SECTION, output=output),
                zt.EXIT_CONFIG,
            )
        mapping[output] = profile
    return mapping


def save_monitor_map(path, mapping):
    """Write the ``[monitors]`` section: output name → profile name.

    Only this section is edited, and as text: ``[grid]``, the zone sets ``[zones]``/
    ``[zones:<output>]``, ``[workspaces]``, ``[keys]``, ``[rules]``, ``[daemon]`` and
    the other lines of the file stay verbatim, as after a manual edit. An empty
    ``mapping`` removes the section altogether; the line order is kept as passed —
    the profile of the first screen breaks the tie when merging. Profile names are
    sanitized, so ``{"HDMI-2": "my cinema"}`` becomes ``HDMI-2 = my-cinema``.
    No file — it will be created with this one section. An empty output name and an
    empty profile name are ``ValueError``. Returns the path.
    """
    body = _monitor_body(mapping)
    block = [f"[{MONITORS_SECTION}]"] + body if body else []
    lines = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    lines = _replace_section(lines, MONITORS_SECTION, block)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n" if lines else "")
    return path


def resolve_profile(monitor_map, workspace_map=None, output=None, workspace_code=None):
    """The profile name for a window: a screen binding beats a workspace binding.

    Resolution order (the owner's decision, 22.11.2026):

    1. the output of the window is in ``[monitors]`` — the screen profile;
    2. otherwise the workspace number of the window is in ``[workspaces]`` — its profile;
    3. otherwise ``None`` — which means the active ``zones.conf``.

    ``workspace_code`` is the workspace number from EWMH (``_NET_CURRENT_DESKTOP``,
    zero-based), while in ``[workspaces]`` the numbers are written from 1, as in the
    Cinnamon UI: this function does the translation, so that no caller has to repeat it.

    The output name is compared case-insensitively — like the ``[zones:<output>]``
    sets; in the dictionary from :func:`load_monitor_map` the parser lowercases it anyway.

    The existence of the profile file is not checked: a profile could have been
    renamed, and then a fallback to the next level with a warning is needed (the
    caller does that — :func:`read_profile_sets` will raise ``FileNotFoundError``).
    """
    if output:
        name = str(output).strip()
        for key, profile in (monitor_map or {}).items():
            if str(key).strip().casefold() == name.casefold():
                return profile
    if workspace_code is not None:
        try:
            desk = int(workspace_code) + 1
        except (TypeError, ValueError):
            desk = None
        if desk is not None:
            # `load_workspace_map` returns numbers as keys, but the dictionary may have been
            # assembled by hand (the editor dialog, tests) — we look at both kinds.
            for key in (desk, str(desk)):
                profile = (workspace_map or {}).get(key)
                if profile:
                    return profile
    return None


# The rows of the "Screen profiles" dialog (the editor) — next to the workspace
# profiles: a pure function without GTK and X, so ordinary tests check it.
def _monitor_row(output, profile, names, on_screen=True):
    """One row of the "Screen profiles" dialog: the options, the warning and the flag."""
    options = [MONITOR_SHARED] + names
    notes = []
    if profile is not None and profile not in names:
        options.append(profile)
        notes.append(
            _("profile \"{profile}\" is not on disk: the binding does not apply yet").format(
                profile=profile
            )
        )
    if not on_screen:
        notes.append(
            _(
                "output {output} is currently {note}: the binding does not apply until the monitor is connected"
            ).format(output=output, note=MONITOR_OFFLINE_NOTE)
        )
    return (output, profile, options, "; ".join(notes), on_screen)


def monitor_dialog_rows(mapping, profiles=None, outputs=None):
    """The rows of the "Screen profiles" dialog — a pure function without GTK and X.

    ``mapping`` is the map from :func:`zone_config.load_monitor_map`, ``profiles`` the
    profile names on disk (:func:`zone_config.list_profiles`), ``outputs`` the output
    names in ``xrandr --listmonitors`` order. Order matters: the profile of the first
    screen breaks the tie when merging (``profile use-monitors``), and saving writes
    the section exactly in the line order of this list.

    A row is ``(output, profile, options, warning, on_screen)``:

    * ``output`` — the name as xrandr prints it; for an output from ``[monitors]`` that
        is not on the machine right now (disconnected, renamed) — as recorded in the file:
        we show the row anyway, otherwise it can be neither fixed nor removed;
    * ``profile`` — the screen profile name, ``None`` — the common set (no binding);
    * ``options`` — the drop-down list: :data:`MONITOR_SHARED`, then the profile names
        from disk, and always the name from the mapping — a profile may have vanished, but
        we show the record and do not throw it away;
    * ``warning`` — empty, or text about a profile that is not on disk and about an
        output that is not on screen right now;
    * ``on_screen`` — ``False`` for a row of an output that is not there right now.

    Output names are compared **case-insensitively**: ``ConfigParser`` lowercases
    the keys, so the file gives ``hdmi-1`` while xrandr says ``HDMI-1``. The rows go
    in xrandr order, and the records about outputs that are not on the screen now
    follow them, in file order.
    """
    names = sorted({sanitize_profile_name(n) for n in (profiles or [])} - {""})
    file_order = []
    by_key = {}
    for output, name in (mapping or {}).items():
        output = str(output).strip()
        if not output:
            continue
        key = output.casefold()
        if key in by_key:
            continue
        # The profile name from the file is already sanitized, but the dictionary could have been built by hand.
        file_order.append(output)
        by_key[key] = (output, sanitize_profile_name(name) or None)
    rows = []
    seen = set()
    for output in outputs or ():
        name = str(output or "").strip()
        if not name:
            continue
        key = name.casefold()
        seen.add(key)
        profile = by_key[key][1] if key in by_key else None
        rows.append(_monitor_row(name, profile, names, on_screen=True))
    for output in file_order:
        key = output.casefold()
        if key in seen:
            continue
        rows.append(_monitor_row(output, by_key[key][1], names, on_screen=False))
    return rows


def workspace_dialog_rows(mapping, profiles=None, desktops=None):
    """The rows of the "Workspace profiles" dialog: one per workspace.

    ``mapping`` is the map from :func:`load_workspace_map`, ``profiles`` the profile
    names on disk (:func:`list_profiles`), ``desktops`` the number of workspaces from
    ``_NET_NUMBER_OF_DESKTOPS`` (``None``/0 — could not be read, take
    :data:`WORKSPACE_DESKTOPS_DEFAULT`).

    A row is ``(number, profile, options, warning)``:

    * ``number`` — the workspace from 1, as in the Cinnamon UI;
    * ``profile`` — the workspace profile name, ``None`` — the common zone set;
    * ``options`` — the drop-down list: :data:`WORKSPACE_SHARED`, then the profile
        names from disk, and always the name from the mapping — a profile may have
        vanished, but we show the record and do not throw it away;
    * ``warning`` — empty, or text about a profile that is not on disk: until the
        file appears, such a workspace is laid out with the common set.

    The workspaces go in order; mappings beyond ``desktops`` end up in the list too —
    otherwise saving would lose them.
    """
    try:
        total = int(desktops)
    except (TypeError, ValueError):
        total = WORKSPACE_DESKTOPS_DEFAULT
    if total < 1:
        total = WORKSPACE_DESKTOPS_DEFAULT
    names = sorted({sanitize_profile_name(n) for n in (profiles or [])} - {""})
    mapped = {}
    for number, name in (mapping or {}).items():
        try:
            key = int(number)
        except (TypeError, ValueError):
            continue
        if key >= 1:
            mapped[key] = name
    rows = []
    for number in sorted(set(range(1, total + 1)) | set(mapped)):
        profile = sanitize_profile_name(mapped.get(number)) or None
        options = [WORKSPACE_SHARED] + names
        missing = profile is not None and profile not in names
        if missing:
            options.append(profile)
        warning = ""
        if missing:
            warning = _(
                "profile \"{profile}\" is not on disk: workspace {number} is laid out by the default set for now"
            ).format(profile=profile, number=number)
        rows.append((number, profile, options, warning))
    return rows


def _clean_monitor(monitor):
    """The output name from an argument; ``None``/empty — the default set."""
    name = str(monitor or "").strip()
    return name or None


def _zone_section_name(monitor):
    """The name of a set section: ``zones`` or ``zones:<monitor>``."""
    name = _clean_monitor(monitor)
    return "zones" if name is None else MONITOR_SECTION_PREFIX + name


def _monitor_of(section):
    """The monitor from a ``zones:<output>`` section name; ``None`` — not a monitor set.

    An empty name (``[zones:]``) is a config error: the message hints at the way it
    should be written. The case of the output name is kept: it is an X11 output name, not a zone.
    """
    name = str(section)
    if name[: len(MONITOR_SECTION_PREFIX)].lower() != MONITOR_SECTION_PREFIX:
        return None
    monitor = name[len(MONITOR_SECTION_PREFIX):].strip()
    if not monitor:
        zt.fail(
            _(
                "zone_config: empty monitor name in section [{section}]: write [zones:<output name>], for example [zones:HDMI-1]"
            ).format(section=name),
            zt.EXIT_CONFIG,
        )
    return monitor


def _read_conf(path, missing_ok=False):
    """A ``ConfigParser`` of the same kind as ``Grid`` has; no file — ``None`` or SystemExit."""
    cp = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
        delimiters=("=",),
    )
    try:
        read = cp.read(path, encoding="utf-8")
    except configparser.Error as exc:
        # A repeated section or key in it: `ConfigParser` dies with a traceback, while a
        # person needs code 3 and the file name — as with any other typo.
        zt.fail(
            _("zone_config: cannot parse config {path}: {error}").format(
                path=path, error=exc),
            zt.EXIT_CONFIG,
        )
    if not read:
        if missing_ok:
            return None
        zt.fail(
            _("zone_config: config not found: {path}").format(path=path),
            zt.EXIT_CONFIG,
        )
    return cp


def _zone_bindings(cp):
    """Zone bindings from ``[keys]`` (an empty dictionary if there is no section)."""
    if not cp.has_section("keys"):
        return {}
    return {k.strip(): v.strip() for k, v in cp.items("keys")}


def _with_bindings(zones, keys):
    """Give the zones the bindings from ``[keys]`` by zone name."""
    for zone in zones:
        zone.binding = keys.get(zone.name, "")
    return zones


def _zone_set(cp, section):
    """The zones of the ``section`` section in file order; ``None`` — no section."""
    if not cp.has_section(section):
        return None
    parser = _SpecParser(
        zt.Grid._numlist(cp.get("grid", "cols", fallback="1.0")),
        zt.Grid._numlist(cp.get("grid", "rows", fallback="1.0")),
    )
    for name, spec in cp.items(section):
        parser.add(name, spec)
    if not parser.zones:
        zt.fail(
            _("zone_config: section [{section}] has no zones").format(
                section=section
            ),
            zt.EXIT_CONFIG,
        )
    return [Zone(name, *parser.fractions_of(name)) for name in parser.zones]


def _zone_sets(path):
    """All the sets of the file: ``(default set or None, {monitor: [Zone]})``.

    The monitor order is the order of the sections in the file; the ``[keys]``
    bindings are set on every set. No file — ``(None, {})``.
    """
    cp = _read_conf(path, missing_ok=True)
    if cp is None:
        return None, {}
    keys = _zone_bindings(cp)
    default = _zone_set(cp, "zones")
    if default is not None:
        _with_bindings(default, keys)
    monitors = {}
    for section in cp.sections():
        monitor = _monitor_of(section)
        if monitor is not None:
            monitors[monitor] = _with_bindings(_zone_set(cp, section), keys)
    return default, monitors


def load_zone_sets(path):
    """All the zone sets of the file: ``{monitor: [Zone]}``, the key ``None`` — default.

    The key ``None`` is there only if the file has a ``[zones]`` section; the order is
    ``[zones]``, then the monitor sections in file order. No file — ``{}``.
    """
    default, monitors = _zone_sets(path)
    sets = {}
    if default is not None:
        sets[None] = default
    sets.update(monitors)
    return sets


def list_zone_sets(path):
    """Which zone sets the file has: ``{monitor: [zone names]}``.

    The key ``None`` is the default set from ``[zones]`` (present only if the section
    is in the file), the other keys are output names from ``[zones:<output>]`` in file
    order. No file — ``{}``.
    """
    return {monitor: [z.name for z in zones] for monitor, zones in load_zone_sets(path).items()}


def _default_zones(path):
    """The ``[zones]`` zones with the ``[keys]`` bindings — the old path through ``Grid``."""
    grid = zt.Grid(path)
    keys = _zone_bindings(grid.conf)
    zones = []
    for name in grid.zones:
        x, y, w, h = grid.fractions(name)
        zones.append(Zone(name, x, y, w, h, keys.get(name, "")))
    return zones


def load_zones(path, monitor=None):
    """The zones of a set with the ``[keys]`` bindings. Raises SystemExit on a broken config.

    Without ``monitor`` (or with an empty one) — the default set from ``[zones]``, as
    before. With ``monitor="HDMI-1"`` — the set from ``[zones:HDMI-1]``; if there is no
    such section, a fallback to the default set (an unknown monitor is not an error).
    """
    name = _clean_monitor(monitor)
    if name is not None:
        cp = _read_conf(path)
        zones = _zone_set(cp, _zone_section_name(name))
        if zones is not None:
            return _with_bindings(zones, _zone_bindings(cp))
    return _default_zones(path)


# ---------------------------------------------------------------- carrying "foreign" sections


_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:[#;].*)?$")


def _trim_blank(lines):
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def load_extra_sections(path, sections=None):
    """Config sections the editor does not edit but has to preserve.

    Returns ``{section_name: [body lines, ...]}`` only for the sections really
    present in the file — the order and the content of the lines are kept verbatim
    (including the case of the keys and the comments). ``sections`` limits the set
    of names (lowercase); by default all of them are taken, except
    :data:`RESERVED_SECTIONS`. Two sections whose names match case-insensitively
    (``[action:Main]`` and ``[action:main]``) or repeat each other are code 3:
    gluing them into one block means writing an unreadable file. A nonexistent
    file gives an empty dictionary.
    """
    wanted = None if sections is None else {s.lower() for s in sections}
    result = {}
    headers = {}
    if not path or not os.path.exists(path):
        return result
    current = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            match = _SECTION_RE.match(line)
            if match:
                raw_name = match.group(1).strip()
                current = raw_name.lower()
                # The editor manages the monitor sets (`[zones:*]`) itself: we do not carry
                # them as "foreign", otherwise the output name would lose its case and be doubled.
                is_zone_set = _monitor_of(current) is not None
                if (
                    not is_zone_set
                    and current not in RESERVED_SECTIONS
                    and (wanted is None or current in wanted)
                ):
                    # Two sections with one name in lowercase are either a verbatim repeat or
                    # `[action:Main]` and `[action:main]`. Gluing them into one block is impossible:
                    # ConfigParser considers them different, and the glue that was written down
                    # (`DuplicateOptionError`) stops being readable. Hence code 3, not a
                    # mangled file.
                    if current in headers:
                        first = headers[current]
                        if first == raw_name:
                            zt.fail(
                                _(
                                    "zone_config: section [{name}] is defined twice: keep only one"
                                ).format(name=raw_name),
                                zt.EXIT_CONFIG,
                            )
                        zt.fail(
                            _(
                                "zone_config: sections [{first}] and [{second}] differ only in case: a merged section would no longer be readable — keep only one"
                            ).format(first=first, second=raw_name),
                            zt.EXIT_CONFIG,
                        )
                    headers[current] = raw_name
                    result[current] = []
                else:
                    current = None
                continue
            if current is not None and current in result:
                result[current].append(line)
    for body in result.values():
        _trim_blank(body)
    return result


def load_config(path):
    """``(zones, foreign sections)`` in one read — exactly what the editor needs."""
    return load_zones(path), load_extra_sections(path)


def _raw_section(path, name):
    """The body of the ``name`` section from the file, verbatim; ``None`` — no section.

    Needed for sections that are not in ``extra`` but must survive saving the zones.
    ``[move]`` is exactly such a section: it is ours
    (:data:`RESERVED_SECTIONS`), so :func:`load_extra_sections` does not return it,
    while losing someone else's bindings when saving the layout is not allowed.
    An empty section is ``[]`` (the header will be written, the body will not).
    """
    if not path or not os.path.exists(path):
        return None
    wanted = str(name).strip().lower()
    body, current = None, False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            match = _SECTION_RE.match(line)
            if match:
                current = match.group(1).strip().lower() == wanted
                if current and body is None:
                    body = []
                continue
            if current:
                body.append(line)
    if body is None:
        return None
    _trim_blank(body)
    return body


def _section_lines(body):
    """The section body as lines: ready lines are written as is, ``(k, v)`` pairs as ``k = v``."""
    out = []
    for item in body:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(f"{item[0]} = {item[1]}")
        else:
            out.append(str(item))
    return out


def _zone_lines(zones):
    """The body lines of a zone set section: ``name = x, y, w, h``."""
    return [f"{z.name} = {z.fmt()}" for z in zones]


def _key_lines(zones):
    """The ``[keys]`` lines without repeats: one key per zone name.

    ``[keys]`` is one per file, while a zone of the same name may stand both in the
    common set and in the monitor sets. A repeated name in the section is not only a
    redundant line: ``ConfigParser`` fails on a repeated option, and the file stops
    being readable. That is why a binding is written only at the first appearance of
    the name: the sets go in write order (the common one first), and its key wins.
    Empty bindings are not written at all.
    """
    out, seen = [], set()
    for zone in zones:
        binding = zone.binding.strip()
        if not binding or zone.name in seen:
            continue
        seen.add(zone.name)
        out.append(f"{zone.name} = {binding}")
    return out


def _write_config(path, default_zones, monitor_sets=None, extra=None):
    """Write ``zones.conf``: ``[grid]``, the zone sets, ``[keys]`` and foreign sections.

    ``default_zones=None`` — there will be no ``[zones]`` section in the file (the
    default set is not given). The ``[keys]`` bindings are collected from all the
    written sets, so the hotkeys of the neighbouring monitors are not lost.

    ``[move]`` is written before ``[daemon]``: the body comes from ``extra``, and if
    it is not there — verbatim from the previous file (:func:`_raw_section`). That way
    saving the zones does not reset the bindings a person has overridden.
    """
    extra = {str(k).strip().lower(): list(v) for k, v in (extra or {}).items()}
    lines = [
        # The file header is not UI text but the content of zones.conf.
        "# Window grid for zone-tiler. The file is created by the zone-editor.py visual editor.",  # i18n: do not translate
        "# A zone = x,y,w,h as work-area fractions (0..1), without the taskbar.",  # i18n: do not translate
        "# Hotkeys live in the [keys] section.",  # i18n: do not translate
        "# A monitor set is a section like zones:<output>, for example zones:HDMI-1.",  # i18n: do not translate
        "",
        "[grid]",
        "cols = 1.0",
        "rows = 1.0",
    ]
    written_zones = []
    if default_zones is not None:
        lines += ["", "[zones]"]
        lines += _zone_lines(default_zones)
        written_zones += list(default_zones)
    for monitor, zones in (monitor_sets or {}).items():
        if not zones:
            continue
        lines += ["", f"[{_zone_section_name(monitor)}]"]
        lines += _zone_lines(zones)
        written_zones += list(zones)

    def append_section(name):
        body = extra.get(name)
        if body is None:
            return
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(_section_lines(body))

    for name in BEFORE_KEYS:
        append_section(name)

    key_lines = _key_lines(written_zones)
    if key_lines:
        lines += ["", "[keys]"]
        lines.extend(key_lines)

    # ``[move]`` is our section (zone_config reads it), but the editor does not edit it:
    # we save it as it was, so that overridden bindings are not lost.
    move_body = extra.pop(MOVE_SECTION, None)
    if move_body is None:
        move_body = _raw_section(path, MOVE_SECTION)
    if move_body is not None:
        lines += ["", f"[{MOVE_SECTION}]"]
        lines.extend(_section_lines(move_body))

    written = set(BEFORE_KEYS) | set(AFTER_KEYS)
    for name in AFTER_KEYS:
        append_section(name)
        written.add(name)
    for name in extra:
        if name in written or name in RESERVED_SECTIONS or _monitor_of(name) is not None:
            continue
        append_section(name)

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def save_zone_sets(path, default_zones, monitor_sets=None, extra=None):
    """Write all the zone sets in one pass over the file.

    ``default_zones`` is the default set: a list of zones, ``None`` — there will be
    no ``[zones]`` section in the file. ``monitor_sets`` is ``{output name: [Zone]}``;
    empty sets and empty output names are dropped, the output name is taken as is
    (the case matters). ``extra`` is a dictionary of the
    :func:`load_extra_sections` kind (``{section: [lines]}``): sections that are not
    in it do not make it into the file.

    The file is rewritten as a whole, so there is no need to read it: a broken ``[keys]``
    with a repeated name does not get in the way of writing (it appeared from such repeats).
    The exception is ``[move]``: its body is read from the previous file (or taken from
    ``extra``), so that saving the zones does not reset the key settings. ``[keys]`` is
    collected from all the written sets without repeats — one key per zone name, the one
    from the default set winning (it is written first). The order is:
    ``[grid]``, ``[zones]``, ``[zones:*]``, ``[rules]``, ``[ignore]``, ``[keys]``, ``[move]``,
    ``[daemon]``, the rest after it. Returns the path.
    """
    sets = {}
    for monitor, zones in (monitor_sets or {}).items():
        name = _clean_monitor(monitor)
        if name is not None and zones:
            sets[name] = list(zones)
    _write_config(path, default_zones, sets, extra)
    return path


def save_zones(path, zones, extra=None, monitor=None):
    """Write a zone set: ``[zones]`` without ``monitor``, ``[zones:<monitor>]`` with it.

    ``extra`` is a dictionary of the :func:`load_extra_sections` kind
    (``{section: [lines]}``); sections that are not in it do not make it into the
    file. The other monitor sets are taken from the current file, so saving one set
    does not lose the neighbouring ones (and their bindings in ``[keys]``). An empty
    monitor set removes its section: a set without zones is an invalid config. This is
    a convenient wrapper over :func:`save_zone_sets` for a single set; when all the
    sets are written at once (the editor), calling that one is cheaper.
    """
    target = _clean_monitor(monitor)
    default, monitors = _zone_sets(path)
    if target is None:
        default = list(zones)
    elif zones:
        monitors[target] = list(zones)
    else:
        monitors.pop(target, None)
    return save_zone_sets(path, default, monitors, extra)


# ------------------------------------------------------------ checking the [rules] rules

# An inline comment: ` # ...` or ` ; ...`, as in ConfigParser (a space before the sign is required).
_INLINE_COMMENT_RE = re.compile(r"\s[#;].*$")


def _rule_pairs(body):
    """The ``(selector, zone)`` pairs from the ``[rules]`` section body, in order of appearance."""
    pairs = []
    for item in body or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            selector, zone = str(item[0]).strip(), str(item[1]).strip()
        else:
            line = str(item).strip()
            if not line or line[0] in "#;":
                continue
            line = _INLINE_COMMENT_RE.sub("", line)
            if "=" not in line:
                continue
            selector, zone = (part.strip() for part in line.split("=", 1))
        if selector:
            pairs.append((selector, zone))
    return pairs


def missing_rule_targets(extra_sections, zone_names):
    """The ``[rules]`` rules that point at missing zones.

    Returns a list of ``(selector, missing_zone)`` pairs in the order the rules are
    written in (the first match matters more to ``match_rules``). An empty list if
    there is no ``[rules]`` section or all of its targets are in place. It repeats the
    semantics of ``zone-tiler.Grid``: the section body is lines of the form
    ``selector = zone``, inline comments after a space are dropped, the zone name is
    matched exactly. The module stays pure — without GTK.
    """
    body = None
    for key, value in (extra_sections or {}).items():
        if str(key).strip().lower() == "rules":
            body = value
            break
    if not body:
        return []
    names = {getattr(name, "name", name) for name in zone_names}
    return [(sel, zone) for sel, zone in _rule_pairs(body) if zone not in names]


# ---------------------------------------------------------------------- zone names (pure)


def sanitize_name(text):
    """A zone name from an input field: spaces and stray characters become dashes.

    The allowed characters are Latin letters, all of Cyrillic (the range
    U+0400–U+04FF, which covers the letter “yo” (U+0451) and the Ukrainian ones),
    digits, “_”, “.” and “-”; everything else, spaces included, is squeezed into a dash.
    """
    return re.sub(r"[^0-9A-Za-z\u0400-\u04FF_.-]+", "-", (text or "").strip())  # i18n: do not translate


def unique_name(zones, base="zone"):
    """A free name of the ``zone1``, ``zone2``, … kind among the ones already taken."""
    names = {z.name for z in zones}
    i = 1
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"


# --------------------------------------------------------------------- profiles

PROFILE_EXT = ".conf"

# The portable profile format (JSON). The version key is mandatory: a foreign
# version is no reason to guess what the author of the file meant.
PROFILE_JSON_KEY = "gridsnap_profile"
PROFILE_JSON_VERSION = 1

# Fractions in JSON are rounded to 4 digits — as many as ``Zone.fmt`` writes
# to ``.conf``; the profile file will not keep more digits anyway.
PROFILE_JSON_DIGITS = 4


def profiles_dir(conf_path=None):
    """The profile directory.

    Next to the active ``zones.conf`` (``<directory>/templates``) — then ``--conf``
    takes the profiles along; without an argument — the XDG directory
    ``$XDG_CONFIG_HOME/gridsnap/templates`` (by default
    ``~/.config/gridsnap/templates``).
    """
    if conf_path:
        return os.path.join(os.path.dirname(os.path.abspath(conf_path)), "templates")
    return os.path.join(zt.config_home(), "gridsnap", "templates")


def sanitize_profile_name(text):
    """A profile name by the same rule as a zone name; empty, “.” and “..” are dropped."""
    name = sanitize_name(text)
    if not name or name in (".", ".."):
        return ""
    return name


def profile_path(name, conf_path=None):
    """The path to ``<name>.conf`` in the profile directory; a bad name — ``ValueError``.

    The name is sanitized, so ``.conf`` cannot escape the templates directory: the
    result has the same directory as the profile directory itself.
    """
    safe = sanitize_profile_name(name)
    if not safe:
        raise ValueError(
            _("bad profile name: {name!r}").format(name=name)
        )
    directory = profiles_dir(conf_path)
    path = os.path.join(directory, safe + PROFILE_EXT)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(directory):
        raise ValueError(
            _("bad profile name: {name!r}").format(name=name)
        )
    return path


def list_profiles(conf_path=None):
    """The names of the saved profiles (without ``.conf``) alphabetically; no directory — empty."""
    directory = profiles_dir(conf_path)
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        if entry.endswith(PROFILE_EXT) and os.path.isfile(os.path.join(directory, entry)):
            names.append(entry[: -len(PROFILE_EXT)])
    return sorted(names)


def _profile_missing(name):
    return FileNotFoundError(_("no profile \"{name}\"").format(name=name))


def profile_text(name, conf_path=None):
    """The content of a profile file verbatim (for ``profile show``)."""
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise _profile_missing(sanitize_profile_name(name)) from exc


def read_profile_sets(name, conf_path=None):
    """``(zones [zones], lines [rules], {monitor: [Zone]}, lines [ignore])``.

    The zones carry the ``[keys]`` bindings; the default set is always a list (empty
    if the profile has no ``[zones]`` section), the monitor sets are in the order of
    the sections in the file. ``[ignore]`` goes together with ``[rules]``: it is one
    family of window rules, and a mismatch like “the rule went away but the exclusion
    stayed” would be surprising.
    """
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    default, monitors = _zone_sets(path)
    extra = load_extra_sections(path, sections=["rules", "ignore"])
    return (default or []), extra.get("rules") or [], monitors, extra.get("ignore") or []


def read_profile(name, conf_path=None):
    """``(zones, lines [rules], lines [ignore])`` of a profile; zones with bindings.

    This is only the default set (``[zones]``) — for compatibility with the editor;
    all the sets are given by :func:`read_profile_sets`.
    """
    zones, rules, _monitors, ignore = read_profile_sets(name, conf_path)
    return zones, rules, ignore


def save_profile(name, zones, rules=None, conf_path=None, monitor_sets=None, ignore=None):
    """Write a profile: ``[zones]``, ``[zones:*]``, ``[rules]``, ``[ignore]``, ``[keys]``.

    ``[grid]``/``[daemon]`` and other foreign sections are not dragged into a profile
    — it is only a layout. ``monitor_sets`` is ``{output name: [Zone]}``; ``None``/
    empty keys are ignored (the default set is ``zones``). ``ignore`` holds the lines
    of the ``[ignore]`` section (selectors of the excluded windows).
    Returns the path.
    """
    sets = {}
    for monitor, monitor_zones in (monitor_sets or {}).items():
        clean = _clean_monitor(monitor)
        if clean is not None and monitor_zones:
            sets[clean] = list(monitor_zones)
    if not zones and not sets:
        raise ValueError(_("a profile without zones has nothing to save"))
    path = profile_path(name, conf_path)
    safe = sanitize_profile_name(name)
    lines = [
        f"# GridSnap layout profile: {safe}.",  # i18n: do not translate
        "# Sections [zones], [zones:<monitor>], [rules], [ignore], [keys]; "  # i18n: do not translate
        "activation - zone-tiler.py profile use <name>.",
    ]
    written = []
    if zones:
        lines += ["", "[zones]"]
        lines += _zone_lines(zones)
        written += list(zones)
    for monitor, monitor_zones in sets.items():
        lines += ["", f"[{_zone_section_name(monitor)}]"]
        lines += _zone_lines(monitor_zones)
        written += list(monitor_zones)
    if rules:
        lines += ["", "[rules]"]
        lines.extend(_section_lines(rules))
    if ignore:
        lines += ["", "[ignore]"]
        lines.extend(_section_lines(ignore))
    key_lines = _key_lines(written)
    if key_lines:
        lines += ["", "[keys]"]
        lines.extend(key_lines)
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise OSError(
            _("failed to write profile \"{name}\": {error}").format(
                name=safe, error=exc
            )
        ) from exc
    return path


def remove_profile(name, conf_path=None):
    """Delete a profile file; no file — ``FileNotFoundError``. Returns the path."""
    path = profile_path(name, conf_path)
    if not os.path.exists(path):
        raise _profile_missing(sanitize_profile_name(name))
    try:
        os.remove(path)
    except OSError as exc:
        raise _profile_missing(sanitize_profile_name(name)) from exc
    return path


# -------------------------------------------------------------------- gallery

# The section with a human-readable title and a description of the layout. It exists
# only in gallery files: it is not carried into a user profile — a profile is a
# layout, while `[gallery]` is a catalog card. The name is the same as the catalog
# in the distribution (`gallery/`): one word for “where it lives” and “what is written in it”.
GALLERY_SECTION = "gallery"

# An explicit override of the gallery directory (own layouts, tests): like
# `GRIDSNAP_LANG` for the UI strings — one way to say “take it from here”.
GALLERY_ENV = "GRIDSNAP_GALLERY"

# The menu order of a layout without `order` in `[gallery]`: such layouts come after
# the numbered ones and are sorted by title.
GALLERY_ORDER_DEFAULT = 100


class GalleryError(ValueError):
    """A gallery error: no layout, a broken `[gallery]`, the profile is already there.

    It inherits from ``ValueError``, like :class:`ProfileJsonError`: both the editor
    and the CLI catch input errors with one type and do not know a special word for the gallery.
    """


def _gallery_candidates():
    """Where to look for the gallery directory, in order of priority."""
    env = (os.environ.get(GALLERY_ENV) or "").strip()
    if env:
        yield env
    # A checkout: gallery/ next to the code.
    yield os.path.join(HERE, GALLERY_SECTION)
    # An installation: the code in <prefix>/lib/gridsnap, the data in <prefix>/share/gridsnap.
    yield os.path.join(
        os.path.dirname(os.path.dirname(HERE)), "share", "gridsnap", GALLERY_SECTION
    )


def gallery_dir():
    """The directory of ready-made layouts — the first candidate that exists.

    The order: ``$GRIDSNAP_GALLERY``, then ``gallery/`` next to the code (a checkout),
    then ``<prefix>/share/gridsnap/gallery`` (an installation: ``install.sh`` puts the
    data there, `.deb`/AUR — into ``/usr/share/gridsnap/gallery``). If nothing is
    found, the path next to the code is returned: it shows in the error message what
    is missing.
    """
    for path in _gallery_candidates():
        if os.path.isdir(path):
            return path
    return os.path.join(HERE, GALLERY_SECTION)


def gallery_ids():
    """The ids of the gallery layouts (file names without ``.conf``) alphabetically."""
    directory = gallery_dir()
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        entry[: -len(PROFILE_EXT)]
        for entry in entries
        if entry.endswith(PROFILE_EXT)
        and os.path.isfile(os.path.join(directory, entry))
    )


def gallery_layout_path(gid):
    """The path to a layout file; an unknown or sloppy id — ``GalleryError``.

    The id is a file name without the extension, not a path: it is checked by the same
    rule as a profile name, so it cannot walk out of the gallery directory.
    """
    name = str(gid or "").strip()
    safe = sanitize_profile_name(name)
    if not safe or safe != name:
        raise GalleryError(_("no layout \"{id}\" in the gallery").format(id=name))
    path = os.path.join(gallery_dir(), safe + PROFILE_EXT)
    if not os.path.isfile(path):
        raise GalleryError(_("no layout \"{id}\" in the gallery").format(id=name))
    return path


def _gallery_conf(path):
    """The ``ConfigParser`` of a gallery file; broken or unreadable — ``GalleryError``."""
    cp = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
        delimiters=("=",),
    )
    try:
        read = cp.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeError) as exc:
        raise GalleryError(
            _("cannot parse gallery layout {path}: {error}").format(
                path=path, error=exc
            )
        ) from exc
    except OSError as exc:
        raise GalleryError(
            _("cannot read gallery layout {path}: {error}").format(
                path=path, error=exc
            )
        ) from exc
    if not read:
        raise GalleryError(
            _("cannot read gallery layout {path}").format(path=path)
        )
    return cp


def _gallery_meta(cp, layout_id):
    """``(title, description, order)`` from the ``[gallery]`` section.

    No title — the id is taken: a layout without a card still has to be visible in
    the menu, otherwise a file put there by hand would silently disappear.
    """
    title = ""
    description = ""
    order = GALLERY_ORDER_DEFAULT
    if cp.has_section(GALLERY_SECTION):
        title = cp.get(GALLERY_SECTION, "title", fallback="").strip()
        description = cp.get(GALLERY_SECTION, "description", fallback="").strip()
        raw = cp.get(GALLERY_SECTION, "order", fallback="").strip()
        if raw:
            try:
                order = int(raw)
            except ValueError:
                raise GalleryError(
                    _(
                        "layout \"{id}\": order in [gallery] must be a number, not {value!r}"
                    ).format(id=layout_id, value=raw)
                ) from None
    return (title or layout_id), description, order


def _gallery_zone_names(cp):
    """The zone names of ``[zones]`` without parsing the geometry — for the menu list."""
    if not cp.has_section("zones"):
        return []
    return [str(name).strip() for name in cp.options("zones")]


def gallery_layouts():
    """The ready-made layouts for the menu: ``[{"id", "title", "description", "zones", "order"}]``.

    ``zones`` are the zone names in file order: that is enough both for the menu and
    for the CLI to show “zones: N”; the geometry is given by
    :func:`load_gallery_layout`. The order is by ``order`` from ``[gallery]``, then by
    title: the classics above the scenarios, and an equal ``order`` ignores the directory order.
    """
    directory = gallery_dir()
    items = []
    for gid in gallery_ids():
        cp = _gallery_conf(os.path.join(directory, gid + PROFILE_EXT))
        title, description, order = _gallery_meta(cp, gid)
        items.append(
            {
                "id": gid,
                "title": title,
                "description": description,
                "zones": _gallery_zone_names(cp),
                "order": order,
            }
        )
    items.sort(key=lambda item: (item["order"], item["title"].casefold(), item["id"]))
    return items


def load_gallery_layout(gid):
    """A gallery layout as a whole — a ready-made “scenario”, not just the geometry.

    The keys of the dictionary: ``id``, ``title``, ``description``, ``order``, ``zones``
    (a list of :class:`Zone` with the ``[keys]`` bindings; empty if the layout is
    given only by monitor sets), ``monitors`` (``{output: [Zone]}``), ``keys``
    (``{zone: shortcut}``), ``rules`` and ``ignore`` (section lines verbatim, in file
    order).

    The errors of the layout itself are :class:`GalleryError` (no file, a broken
    ``[gallery]``, no zones), while a bad zone is ``SystemExit``, as with a profile:
    the message with the zone name is printed by the tiler's parser.
    """
    path = gallery_layout_path(gid)
    layout_id = os.path.basename(path)[: -len(PROFILE_EXT)]
    cp = _gallery_conf(path)
    title, description, order = _gallery_meta(cp, layout_id)
    default, monitors = _zone_sets(path)
    if not default and not monitors:
        raise GalleryError(
            _("layout \"{id}\" has no [zones] section").format(id=layout_id)
        )
    extra = load_extra_sections(path, sections=["rules", "ignore"])
    keys = {}
    for chunk in [default or []] + list(monitors.values()):
        for zone in chunk:
            binding = (zone.binding or "").strip()
            if binding and zone.name not in keys:
                keys[zone.name] = binding
    return {
        "id": layout_id,
        "title": title,
        "description": description,
        "order": order,
        "zones": default or [],
        "monitors": monitors,
        "keys": keys,
        "rules": extra.get("rules") or [],
        "ignore": extra.get("ignore") or [],
    }


def install_gallery_layout(gid, name=None, force=False, conf_path=None):
    """Copy a gallery layout into the user profile; return the path.

    The profile is the same one the editor creates and ``profile use`` reads:
    ``templates/<name>.conf`` next to ``zones.conf``. Everything the layout carries is
    transferred: the zones, the ``[zones:<output>]`` sets, ``[rules]``, ``[ignore]`` and
    ``[keys]``. The ``[gallery]`` section stays in the catalog: it has nothing to do in
    a profile, otherwise the copied layout would keep counting as part of the gallery.

    The name by default is the layout id. A profile with such a name is
    :class:`GalleryError` until ``force`` is passed: silently overwriting someone
    else's layout would be a loss, not a “choice from the gallery”.
    """
    layout = load_gallery_layout(gid)
    safe = sanitize_profile_name(name if name is not None else layout["id"])
    if not safe:
        raise GalleryError(
            _("bad profile name for layout \"{id}\": {name!r}").format(
                id=layout["id"], name=name
            )
        )
    path = profile_path(safe, conf_path)
    if os.path.exists(path) and not force:
        raise GalleryError(
            _(
                "profile \"{name}\" already exists: choose another name or overwrite it"
            ).format(name=safe)
        )
    return save_profile(
        safe,
        layout["zones"],
        layout["rules"],
        conf_path,
        monitor_sets=layout["monitors"],
        ignore=layout["ignore"],
    )


class ProfileJsonError(ValueError):
    """A JSON profile error: format, version or values (in the CLI — code 3)."""


# ------------------------------------------------------ carrying a profile into JSON
def _json_number(value):
    """A fraction in JSON: an integer stays an integer (0, 1), the rest is a float."""
    number = round(float(value), PROFILE_JSON_DIGITS)
    return int(number) if number == int(number) else number


def _json_zone(zones):
    """A zone set as ``{name: [x, y, w, h]}`` in the order of the zones of the set."""
    return {z.name: [_json_number(v) for v in z.as_tuple()] for z in zones or []}


def _zone_keys(zones, monitor_sets=None):
    """``{zone: shortcut}`` from the bindings of the sets.

    The bindings live in ``[keys]`` and are set on the zones when reading. One zone may
    stand in several sets, while ``[keys]`` is written at the first appearance of the
    name (:func:`_key_lines`), so here too the first one is what matters.
    """
    keys = {}
    chunks = [zones or []] + list((monitor_sets or {}).values())
    for chunk in chunks:
        for zone in chunk:
            binding = (zone.binding or "").strip()
            if binding and zone.name not in keys:
                keys[zone.name] = binding
    return keys


def profile_to_dict(name, conf_path=None):
    """A profile → dict of the portable format (``gridsnap_profile`` version 1).

    ``zones`` is the default set (an empty dictionary if there is no ``[zones]``
    section), ``monitors`` the ``[zones:<output>]`` sets (no key if there are none),
    ``rules`` the ``[rules]`` lines as in the file, ``ignore`` the ``[ignore]`` lines,
    ``keys`` — ``{zone: shortcut}`` from ``[keys]``. The fractions are rounded to
    :data:`PROFILE_JSON_DIGITS`.
    No profile — ``FileNotFoundError``; a broken ``.conf`` — ``SystemExit``.
    """
    zones, rules, monitor_sets, ignore = read_profile_sets(name, conf_path)
    data = {
        PROFILE_JSON_KEY: PROFILE_JSON_VERSION,
        "name": sanitize_profile_name(name),
        "zones": _json_zone(zones),
    }
    if monitor_sets:
        data["monitors"] = {m: _json_zone(zs) for m, zs in monitor_sets.items()}
    data["rules"] = list(rules or [])
    data["ignore"] = list(ignore or [])
    data["keys"] = _zone_keys(zones, monitor_sets)
    return data


def dumps_profile(data):
    """Stable text of a JSON profile: keys alphabetically, indent 2, UTF-8 as is.

    The stability is needed for git: one and the same profile must give one and the
    same file, otherwise the diffs wander with the dictionary traversal order.
    """
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_profile_json(data, path):
    """Write a JSON profile to a file (the directory is created); returns the path.

    A bad path or a file system refusal — ``ProfileJsonError`` with a hint.
    """
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dumps_profile(data))
    except OSError as exc:
        raise ProfileJsonError(
            _("failed to write JSON profile {path}: {error}").format(
                path=path, error=exc
            )
        ) from exc
    return path


def read_profile_json(path):
    """Read a dict from a JSON profile file; bad JSON — ``ProfileJsonError``."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise ProfileJsonError(
            _("failed to read JSON profile {path}: {error}").format(
                path=path, error=exc
            )
        ) from exc
    except ValueError as exc:
        raise ProfileJsonError(
            _("{path} is not JSON: {error}").format(path=path, error=exc)
        ) from exc


def profile_zone_total(data):
    """How many zones a profile dict holds: the default set plus the monitor sets."""
    total = len(data.get("zones") or {})
    for body in (data.get("monitors") or {}).values():
        total += len(body or {})
    return total


def _dict_zone_rect(name, spec):
    """``[x, y, w, h]`` from JSON with checks; an error — ``ProfileJsonError``."""
    if isinstance(spec, (str, bytes, dict)):
        raise ProfileJsonError(
            _("zone '{zone}': expected a list [x, y, w, h], not {spec!r}").format(
                zone=name, spec=spec
            )
        )
    try:
        values = list(spec)
    except TypeError:
        raise ProfileJsonError(
            _("zone '{zone}': expected a list [x, y, w, h], not {spec!r}").format(
                zone=name, spec=spec
            )
        ) from None
    if len(values) != 4:
        raise ProfileJsonError(
            _("zone '{zone}': expected 4 fractions [x, y, w, h], not {spec!r}").format(
                zone=name, spec=spec
            )
        )
    numbers = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProfileJsonError(
                _("zone '{zone}': fractions must be numbers, not {item!r}").format(
                    zone=name, item=item
                )
            )
        numbers.append(float(item))
    x, y, w, h = numbers
    if not (
        0 <= x < 1
        and 0 <= y < 1
        and w > 0
        and h > 0
        and x + w <= 1.0001
        and y + h <= 1.0001
    ):
        raise ProfileJsonError(
            _("zone '{zone}': fractions outside 0..1 or w/h ≤ 0: {spec!r}").format(
                zone=name, spec=spec
            )
        )
    return x, y, w, h


def _dict_zones(body, where):
    """``{name: [x, y, w, h]}`` from JSON → ``[Zone]`` with checks."""
    if body is None:
        return []
    if not isinstance(body, dict):
        raise ProfileJsonError(
            _(
                "in JSON profile {where} must be an object {{zone: [x, y, w, h]}}"
            ).format(where=where)
        )
    zones = []
    for raw_name, spec in body.items():
        name = str(raw_name).strip()
        if not name:
            raise ProfileJsonError(
                _("in JSON profile {where}: empty zone name").format(where=where)
            )
        zones.append(Zone(name, *_dict_zone_rect(name, spec)))
    return zones


def profile_from_dict(data, name=None, conf_path=None):
    """A ``gridsnap_profile`` dict → a profile file ``templates/<name>.conf``.

    The name comes from the ``name`` argument, otherwise from ``data["name"]``, and is
    sanitized (:func:`sanitize_profile_name`); empty — ``ValueError``. Format errors
    are :class:`ProfileJsonError` (code 3 in the CLI): the wrong or a missing version,
    not a single zone, a zone outside 0..1 or with non-positive ``w``/``h``, a rule
    pointing at a nonexistent zone, wrong types. Keys for zones the profile does not
    have are dropped silently — ``[keys]`` is written from the zones of the set. The
    order of the zones inside a set is the one of the JSON object (after a file on
    disk — alphabetical, that is how ``sort_keys`` works): the names, the fractions
    and the bindings matter, while the order in ``.conf`` is only looks.
    Returns the path of the written profile.
    """
    if not isinstance(data, dict):
        raise ProfileJsonError(_("JSON profile expected an object {...}"))
    version = data.get(PROFILE_JSON_KEY)
    if isinstance(version, bool) or version != PROFILE_JSON_VERSION:
        raise ProfileJsonError(
            _(
                "in JSON profile {key} must be {version}, not {value!r}"
            ).format(
                key=PROFILE_JSON_KEY,
                version=PROFILE_JSON_VERSION,
                value=version,
            )
        )

    safe = sanitize_profile_name(name if name is not None else data.get("name"))
    if not safe:
        raise ValueError(
            _(
                "bad profile name: specify --name, the name {name!r} in JSON will not do"
            ).format(name=data.get("name"))
        )

    zones = _dict_zones(data.get("zones"), "zones")
    raw_monitors = data.get("monitors")
    if raw_monitors is None:
        raw_monitors = {}
    if not isinstance(raw_monitors, dict):
        raise ProfileJsonError(
            _(
                "in JSON profile monitors must be an object {output: {zone: [x, y, w, h]}}"
            )
        )
    monitor_sets = {}
    for raw_monitor, body in raw_monitors.items():
        monitor = _clean_monitor(raw_monitor)
        if monitor is None:
            raise ProfileJsonError(
                _("in JSON profile empty monitor name in monitors")
            )
        parsed = _dict_zones(body, f"monitors.{monitor}")
        if parsed:
            monitor_sets[monitor] = parsed

    names = {z.name for z in zones}
    for chunk in monitor_sets.values():
        names |= {z.name for z in chunk}
    if not names:
        raise ProfileJsonError(_("JSON profile has no zones"))

    rules = data.get("rules")
    if rules is None:
        rules = []
    if isinstance(rules, str) or not isinstance(rules, (list, tuple)):
        raise ProfileJsonError(_("in JSON profile rules must be a list of strings"))
    rules = [str(line) for line in rules]
    for line in rules:
        text = line.strip()
        if not text or text[0] in "#;":
            continue
        if "=" not in _INLINE_COMMENT_RE.sub("", text):
            raise ProfileJsonError(
                _("rule without \"selector = zone\": {line!r}").format(line=line)
            )
    for selector, target in _rule_pairs(rules):
        if target not in names:
            raise ProfileJsonError(
                _(
                    "rule \"{selector} = {zone}\" refers to unknown zone '{zone}'"
                ).format(selector=selector, zone=target)
            )

    ignore = data.get("ignore")
    if ignore is None:
        ignore = []
    if isinstance(ignore, str) or not isinstance(ignore, (list, tuple)):
        raise ProfileJsonError(_("in JSON profile ignore must be a list of strings"))
    ignore = [str(line) for line in ignore]
    for line in ignore:
        text = line.strip()
        if not text or text[0] in "#;":
            continue
        if "=" not in _INLINE_COMMENT_RE.sub("", text):
            raise ProfileJsonError(
                _("exclusion without \"selector = note\": {line!r}").format(
                    line=line
                )
            )

    keys = data.get("keys")
    if keys is None:
        keys = {}
    if not isinstance(keys, dict):
        raise ProfileJsonError(
            _("in JSON profile keys must be an object {zone: shortcut}")
        )
    bindings = {str(k).strip(): str(v).strip() for k, v in keys.items()}
    for chunk in [zones] + list(monitor_sets.values()):
        for zone in chunk:
            zone.binding = bindings.get(zone.name, "")

    return save_profile(
        safe, zones, rules, conf_path, monitor_sets=monitor_sets, ignore=ignore
    )


def apply_profile(name, conf_path=None):
    """Activate a profile in ``zones.conf``.

    The zone sets ``[zones]``/``[zones:*]``, ``[rules]``, ``[ignore]`` and ``[keys]``
    are taken from the profile (``[keys]`` — from the zone bindings) and replace the
    sets of the active config as a whole: monitor sets that the profile does not have
    will not remain in the config. ``[grid]``, ``[daemon]`` and the other sections of
    the active config are preserved. Returns the zones of the default set (an empty
    list if the profile has none).
    """
    zones, rules, monitor_sets, ignore = read_profile_sets(name, conf_path)
    extra = load_extra_sections(conf_path)
    if rules:
        extra["rules"] = rules
    else:
        extra.pop("rules", None)
    if ignore:
        extra["ignore"] = ignore
    else:
        extra.pop("ignore", None)
    _write_config(conf_path, zones or None, monitor_sets, extra)
    return zones

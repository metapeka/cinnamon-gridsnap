#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup-hotkeys.py — registers the zone hotkeys from zones.conf in Cinnamon.

Reads the [keys] section (zone = binding), the optional [move] (direction =
binding), [focus] (the binding of the "this zone only" mode), [tabs] (the binding
of "the next window of the zone"), [overlay] (the binding that shows the zone grid
over the live desktop) and [action:<name>] (an action with a non-empty
`key`) and creates Cinnamon custom keybindings of the form:
    zone-tiler: main          ->  python3 .../zone-tiler.py apply main --quiet
    zone-tiler: move left     ->  python3 .../zone-tiler.py move left --quiet
    zone-tiler: focus         ->  python3 .../zone-tiler.py focus main --quiet
    zone-tiler: tabs-next     ->  python3 .../zone-tiler.py tabs-next --quiet
    zone-tiler: desktop grid ->  python3 .../zone-picker.py --toggle --quiet
    zone-tiler: action mirror ->  python3 .../zone-tiler.py action mirror --quiet

    python3 setup-hotkeys.py            # install/update
    python3 setup-hotkeys.py --list     # show what will be installed
    python3 setup-hotkeys.py --remove   # remove all the zone-tiler shortcuts

The default move bindings are the same `Win`+arrows that muffin
takes for its native tiling (`push-tile-left/right/up/down`). A binding of one's own
is registered as it is, the Cinnamon settings are not changed: if both handlers
listen to the same key, the one that fired last wins — so for
moving between zones it is better to pick free bindings in `[move]`.

The path to zones.conf can be given with the ``--conf`` flag: it is passed both to
``zone-tiler.py keys`` and into the installed shortcut command — into the command
**as an absolute** path, because Cinnamon does not launch it from the launch directory and
a relative path would give a silently dead key.
"""

import argparse
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_config  # noqa: E402  (looked up next to itself, as it is installed)
from zone_i18n import _  # noqa: E402  (interface strings and their translation)

# The return code when the command could not do what it was asked to.
EXIT_FAIL = 1

ZONEBIN = os.path.join(HERE, "zone-tiler.py")
PICKERBIN = os.path.join(HERE, "zone-picker.py")
LIST_SCHEMA = "org.cinnamon.desktop.keybindings"
CB_SCHEMA = "org.cinnamon.desktop.keybindings.custom-keybinding"
BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
NAME_PREFIX = "zone-tiler: "
# The keys of one shortcut entry: reset one by one (see `reset_binding`).
CB_KEYS = ("name", "binding", "command")
# muffin's native tiling: these bindings live in Cinnamon separately from ours.
WM_SCHEMA = "org.cinnamon.desktop.keybindings.wm"
PUSH_TILE_PREFIX = "push-tile-"


def gs(*args, get=False):
    cmd = ["gsettings"] + (["get"] if get else ["set"]) + list(args)
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"setup-hotkeys: gsettings {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def gs_reset(path, key):
    """Reset one key of an entry to its default value.

    A separate function rather than `gsettings reset-recursively`: on a live Cinnamon
    6.4.10 the recursive reset of a relocatable schema (`schema:/path/`) does not
    reset a registered entry — measured: after it `name` stayed as it was, while a
    single `reset` of the same key removed it.
    """
    r = subprocess.run(
        ["gsettings", "reset", path, key], text=True, capture_output=True
    )
    if r.returncode:
        sys.exit(f"setup-hotkeys: gsettings reset {key}: {r.stderr.strip()}")


def gvariant_str(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def custom_ids():
    raw = gs(LIST_SCHEMA, "custom-list", get=True).replace("@as ", "").strip()
    try:
        return list(ast.literal_eval(raw) or [])
    except Exception:
        return []


def cb_name(cid):
    try:
        return gs(f"{CB_SCHEMA}:{BASE}{cid}/", "name", get=True).strip().strip("'")
    except SystemExit:
        return ""


def zone_pairs(conf=None, *flags):
    """`label<TAB>binding` pairs from `zone-tiler.py keys [flags]`.

    `flags` — extra flags of `keys`, e.g. `--move`: then the labels are the move
    directions rather than the zone names.
    """
    cmd = [sys.executable, ZONEBIN, "keys", *flags]
    if conf:
        cmd += ["--conf", conf]
    out = subprocess.run(cmd, text=True, capture_output=True)
    if out.returncode:
        sys.exit(f"setup-hotkeys: {out.stderr.strip()}")
    pairs = []
    for line in out.stdout.splitlines():
        if "\t" in line:
            zone, binding = line.split("\t", 1)
            pairs.append((zone.strip(), binding.strip()))
    return pairs


def with_conf(command, conf=None):
    """The shortcut command with the same `--conf` as the script itself.

    The path is made absolute: Cinnamon does not launch a shortcut from the
    launch directory, and a relative `--conf zones.conf` would turn into "config
    not found" (code 3) — that is, into a silently dead key (verified live).
    """
    if not conf:
        return command
    return command + f" --conf {os.path.abspath(conf)}"


def conf_path(conf=None):
    """The path to the config for **reading** the sections: without `--conf` — as in zone-tiler.

    The shortcut command does not get `--conf` without the flag (`with_conf`), while reading
    the sections still has to come from the real file: `~/.config/gridsnap/zones.conf`,
    and if there is none — the one next to the code (`zone-tiler.default_conf`). Otherwise
    `setup-hotkeys.py --list` without `--conf` would read "nothing" and silently lose
    the `[focus]`/`[tabs]`/action bindings.
    """
    return conf or zone_config.zt.default_conf()


def planned(conf=None):
    """What to install: `(name, binding, command)` in the order of zones, directions, mode.

    The name goes after the `zone-tiler: ` prefix: by it `drop_owned` tells its own
    shortcuts from foreign ones and does not touch foreign ones on reinstall. `focus` —
    the name of the `[focus]` section, `tabs-next` — the command of the `[tabs]` section, and
    ``action <name>`` — an action from `[action:<name>]` with a non-empty `key` (its command is
    `zone-tiler.py action <name>`): without these sections the plan is exactly the same as before.
    """
    items = []
    for zone, binding in zone_pairs(conf):
        command = with_conf(f"{sys.executable} {ZONEBIN} apply {zone} --quiet", conf)
        items.append((zone, binding, command))
    for direction, binding in zone_pairs(conf, "--move"):
        command = with_conf(f"{sys.executable} {ZONEBIN} move {direction} --quiet", conf)
        items.append((f"move {direction}", binding, command))
    path = conf_path(conf)
    focus = zone_config.load_focus_keys(path)
    if focus:
        command = f"{sys.executable} {ZONEBIN} focus"
        if focus.get("zone"):
            command += f" {focus['zone']}"
        items.append(("focus", focus["key"], with_conf(command + " --quiet", conf)))
    tabs = zone_config.load_tabs_config(path)
    if tabs.get("key"):
        # The tabs key — the same path as `[focus]`: a separate process of the
        # tiler that only passes `tabs-next` to the daemon. The daemon itself
        # switches tabs, so the command has no zone and no arguments.
        items.append((
            "tabs-next",
            tabs["key"],
            with_conf(f"{sys.executable} {ZONEBIN} tabs-next --quiet", conf),
        ))
    overlay_key = zone_config.load_overlay_key(path)
    if overlay_key:
        # The desktop grid: the picker draws it, and the same command hides it again.
        # The name keeps the `zone-tiler: ` prefix of every GridSnap shortcut, so `--remove`
        # and a reinstall own it together with the rest.
        items.append((
            "desktop grid",
            overlay_key,
            with_conf(f"{sys.executable} {PICKERBIN} --toggle --quiet", conf),
        ))
    for name, record in zone_config.load_actions(path).items():
        if not record.get("key"):
            continue
        # An action on a key — the same command a person would run by hand;
        # the shortcut name "action <name>" is removed by `--remove` along with the rest.
        items.append((
            f"action {name}",
            record["key"],
            with_conf(f"{sys.executable} {ZONEBIN} action {name} --quiet", conf),
        ))
    return items


def push_tile_bindings():
    """`{direction: [bindings]}` of muffin's native tiling (`push-tile-*`).

    It is read only for the sake of a warning: the `move` and `push-tile` bindings
    are the same by default (`Win`+arrows), and when two handlers claim
    one key, it fires now one, now the other. The script does not change the Cinnamon
    settings — it only reports the collision.
    """
    out = {}
    for direction in ("left", "right", "up", "down"):
        r = subprocess.run(
            ["gsettings", "get", WM_SCHEMA, PUSH_TILE_PREFIX + direction],
            text=True,
            capture_output=True,
        )
        if r.returncode:
            continue
        try:
            values = ast.literal_eval(r.stdout.strip()) or []
        except (ValueError, SyntaxError):
            continue
        if values:
            out[direction] = [str(v) for v in values]
    return out


def conflicts_with_push_tile(items, native=None):
    """`[(direction, binding)]` — our `move` bindings on a native tiling key.

    `items` — the result of `planned`; `native` — :func:`push_tile_bindings` (can be
    substituted in tests). The comparison is exact, by the binding string: `Cinnamon`
    understands both sides the same way.
    """
    native = push_tile_bindings() if native is None else native
    out = []
    for name, binding, _command in items:
        if not name.startswith("move "):
            continue
        direction = name[len("move "):].strip()
        if binding and binding in native.get(direction, []):
            out.append((direction, binding))
    return out


def warn_conflicts(items):
    """Warn on stderr about a collision with `push-tile-*`; changes nothing."""
    for direction, binding in conflicts_with_push_tile(items):
        print(
            _(
                "setup-hotkeys: warning: {binding} for move {direction} matches muffin's native {prefix}{direction} - one of the two handlers will fire on this key. I am not disabling the native shortcut: set your own in [move] or turn off push-tile in Cinnamon settings"
            ).format(
                binding=binding,
                direction=direction,
                prefix=PUSH_TILE_PREFIX,
            ),
            file=sys.stderr,
        )


def split_owned(ids):
    """Split the ids of the shortcuts into our own and foreign ones: `(ours, foreign)`."""
    owned, kept = [], []
    for cid in ids:
        (owned if cb_name(cid).startswith(NAME_PREFIX) else kept).append(cid)
    return owned, kept


def reset_binding(cid):
    """Extinguish a shortcut entry: reset each of its keys separately."""
    path = f"{CB_SCHEMA}:{BASE}{cid}/"
    for key in CB_KEYS:
        gs_reset(path, key)


def drop_owned(ids):
    """Removes our shortcuts: resets their keys and returns the remaining ids."""
    owned, kept = split_owned(ids)
    for cid in owned:
        reset_binding(cid)
    return kept


def remove_owned():
    """Remove all our shortcuts; return the ids that could not be removed.

    The order is deliberate: **first** the list without our ids, then the reset of the entries,
    then the list once more. Cinnamon (`keybindings.js`) rewrites
    `custom-list` from its own memory on every change, so the order "reset,
    then list" loses the race: on a live Cinnamon 6.4.10 `--remove` twice
    reported success, while four shortcuts remained both in the list and in the entries.
    The list written first resets that memory, and after that the keys
    are reset without resistance; the repeated write of the list is for the case when
    the rewrite did happen between the steps.
    """
    owned, kept = split_owned(custom_ids())
    for _attempt in range(2):
        if not owned:
            return []
        set_custom_list(kept)
        for cid in owned:
            reset_binding(cid)
        set_custom_list(kept)
        owned = [cid for cid in custom_ids() if cb_name(cid).startswith(NAME_PREFIX)]
        if not owned:
            return []
    return owned


def next_ids(kept, count):
    used = set(kept)
    ids, n = [], 0
    while len(ids) < count:
        cid = f"custom{n}"
        n += 1
        if cid not in used:
            ids.append(cid)
    return ids


def set_custom_list(ids):
    """Write Cinnamon's `custom-list` (the order of the ids does not matter to us)."""
    gs(LIST_SCHEMA, "custom-list", "[" + ", ".join(gvariant_str(i) for i in ids) + "]")


def main():
    ap = argparse.ArgumentParser(description=_("Zone hotkeys for Cinnamon"))
    ap.add_argument("--list", action="store_true", help=_("only show the plan"))
    ap.add_argument(
        "--remove", action="store_true", help=_("remove zone-tiler shortcuts")
    )
    ap.add_argument(
        "--conf",
        help=_("path to zones.conf (the same as zone-tiler.py uses by default)"),
    )
    args = ap.parse_args()

    if args.remove:
        left = remove_owned()
        if left:
            print(
                _(
                    "setup-hotkeys: these shortcuts could not be removed: {ids} — remove them by hand in Cinnamon's keyboard settings"
                ).format(ids=", ".join(left)),
                file=sys.stderr,
            )
            return EXIT_FAIL
        print(_("setup-hotkeys: zone-tiler shortcuts removed"))
        return 0

    pairs = planned(args.conf)
    kept = drop_owned(custom_ids())

    if args.list:
        for name, binding, _command in pairs:
            print(f"{binding:<24} -> {name}")
        warn_conflicts(pairs)
        return

    ids = next_ids(kept, len(pairs))
    for cid, (name, binding, command) in zip(ids, pairs):
        path = f"{CB_SCHEMA}:{BASE}{cid}/"
        gs(path, "name", gvariant_str(NAME_PREFIX + name))
        gs(path, "binding", "[" + gvariant_str(binding) + "]")
        gs(path, "command", gvariant_str(command))

    # The list is written in two steps: Cinnamon (`keybindings.js`) rereads the entries
    # only on the `changed::custom-list` signal. When the ids are reused — and that is
    # the usual case — the new list matches the old one, there is no signal, and Cinnamon
    # stays with the old commands: an edit of the path or of the config does not reach the key
    # (verified live: the press called the previous path and failed with code 3).
    # So first the list without our ids, then with them: the second signal registers
    # the already new commands.
    set_custom_list(kept)
    set_custom_list(kept + ids)

    print(
        _("setup-hotkeys: shortcuts installed: {count}").format(count=len(pairs))
    )
    for name, binding, _command in pairs:
        print(f"  {binding:<22} {name}")
    warn_conflicts(pairs)


if __name__ == "__main__":
    sys.exit(main())

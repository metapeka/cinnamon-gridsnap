#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup-hotkeys.py — регистрирует в Cinnamon горячие клавиши для зон из zones.conf.

Читает секцию [keys] (зона = сочетание) и создаёт пользовательские
сочетания Cinnamon вида:
    zone-tiler: main  ->  python3 .../zone-tiler.py apply main --quiet

  python3 setup-hotkeys.py            # установить/обновить
  python3 setup-hotkeys.py --list     # показать, что будет установлено
  python3 setup-hotkeys.py --remove   # удалить все сочетания zone-tiler

Путь к zones.conf можно задать флагом ``--conf``: он передаётся и в
``zone-tiler.py keys``, и в устанавливаемую команду сочетания.
"""

import argparse
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ZONEBIN = os.path.join(HERE, "zone-tiler.py")
LIST_SCHEMA = "org.cinnamon.desktop.keybindings"
CB_SCHEMA = "org.cinnamon.desktop.keybindings.custom-keybinding"
BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
NAME_PREFIX = "zone-tiler: "


def gs(*args, get=False):
    cmd = ["gsettings"] + (["get"] if get else ["set"]) + list(args)
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"setup-hotkeys: gsettings {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


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


def zone_pairs(conf=None):
    cmd = [sys.executable, ZONEBIN, "keys"]
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


def drop_owned(ids):
    """Убирает наши сочетания: сбрасывает их ключи и возвращает оставшиеся id."""
    kept = []
    for cid in ids:
        if cb_name(cid).startswith(NAME_PREFIX):
            subprocess.run(
                ["gsettings", "reset-recursively", f"{CB_SCHEMA}:{BASE}{cid}/"],
                capture_output=True,
            )
        else:
            kept.append(cid)
    return kept


def next_ids(kept, count):
    used = set(kept)
    ids, n = [], 0
    while len(ids) < count:
        cid = f"custom{n}"
        n += 1
        if cid not in used:
            ids.append(cid)
    return ids


def main():
    ap = argparse.ArgumentParser(description="Горячие клавиши зон для Cinnamon")
    ap.add_argument("--list", action="store_true", help="только показать план")
    ap.add_argument("--remove", action="store_true", help="удалить сочетания zone-tiler")
    ap.add_argument("--conf", help="путь к zones.conf (по умолчанию — как у zone-tiler.py)")
    args = ap.parse_args()

    pairs = [] if args.remove else zone_pairs(args.conf)
    kept = drop_owned(custom_ids())

    if args.list:
        for zone, binding in pairs:
            print(f"{binding:<24} -> {zone}")
        return

    ids = next_ids(kept, len(pairs))
    for cid, (zone, binding) in zip(ids, pairs):
        path = f"{CB_SCHEMA}:{BASE}{cid}/"
        command = f"{sys.executable} {ZONEBIN} apply {zone} --quiet"
        if args.conf:
            command += f" --conf {args.conf}"
        gs(path, "name", gvariant_str(NAME_PREFIX + zone))
        gs(path, "binding", "[" + gvariant_str(binding) + "]")
        gs(path, "command", gvariant_str(command))

    new_list = kept + ids
    gs(LIST_SCHEMA, "custom-list", "[" + ", ".join(gvariant_str(i) for i in new_list) + "]")

    if args.remove:
        print("setup-hotkeys: сочетания zone-tiler удалены")
    else:
        print(f"setup-hotkeys: установлено сочетаний: {len(pairs)}")
        for zone, binding in pairs:
            print(f"  {binding:<22} {zone}")


if __name__ == "__main__":
    main()

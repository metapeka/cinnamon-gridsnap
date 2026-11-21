#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup-hotkeys.py — регистрирует в Cinnamon горячие клавиши для зон из zones.conf.

Читает секцию [keys] (зона = сочетание), необязательные [move] (направление =
сочетание) и [focus] (сочетание режима «только эта зона») и создаёт
пользовательские сочетания Cinnamon вида:
    zone-tiler: main        ->  python3 .../zone-tiler.py apply main --quiet
    zone-tiler: move left   ->  python3 .../zone-tiler.py move left --quiet
    zone-tiler: focus       ->  python3 .../zone-tiler.py focus main --quiet

  python3 setup-hotkeys.py            # установить/обновить
  python3 setup-hotkeys.py --list     # показать, что будет установлено
  python3 setup-hotkeys.py --remove   # удалить все сочетания zone-tiler

Сочетания перемещения по умолчанию — те же `Win`+стрелки, которые muffin
занимает родным тайлингом (`push-tile-left/right/up/down`). Своё сочетание
регистрируется как есть, настройки Cinnamon не меняются: если оба обработчика
слушают одну клавишу, выигрывает тот, кто сработал последним, — поэтому для
перемещения по зонам лучше выбрать в `[move]` свободные сочетания.

Путь к zones.conf можно задать флагом ``--conf``: он передаётся и в
``zone-tiler.py keys``, и в устанавливаемую команду сочетания.
"""

import argparse
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_config  # noqa: E402  (ищем рядом с собой, как устанавливается)

ZONEBIN = os.path.join(HERE, "zone-tiler.py")
LIST_SCHEMA = "org.cinnamon.desktop.keybindings"
CB_SCHEMA = "org.cinnamon.desktop.keybindings.custom-keybinding"
BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
NAME_PREFIX = "zone-tiler: "
# Родной тайлинг muffin: эти сочетания живут в Cinnamon отдельно от наших.
WM_SCHEMA = "org.cinnamon.desktop.keybindings.wm"
PUSH_TILE_PREFIX = "push-tile-"


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


def zone_pairs(conf=None, *flags):
    """Пары `подпись<TAB>сочетание` от `zone-tiler.py keys [флаги]`.

    `flags` — дополнительные флаги `keys`, например `--move`: тогда подписи —
    направления перемещения, а не имена зон.
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
    """Команда сочетания с тем же `--conf`, что у самого скрипта."""
    return command + (f" --conf {conf}" if conf else "")


def planned(conf=None):
    """Что установить: `(имя, сочетание, команда)` по порядку зон, направлений, режима.

    Имя идёт в префикс `zone-tiler: `: по нему `drop_owned` отличает свои
    сочетания от чужих и не трогает чужие при переустановке. `focus` —
    имя секции `[focus]`: без неё в плане ровно то же, что раньше.
    """
    items = []
    for zone, binding in zone_pairs(conf):
        command = with_conf(f"{sys.executable} {ZONEBIN} apply {zone} --quiet", conf)
        items.append((zone, binding, command))
    for direction, binding in zone_pairs(conf, "--move"):
        command = with_conf(f"{sys.executable} {ZONEBIN} move {direction} --quiet", conf)
        items.append((f"move {direction}", binding, command))
    focus = zone_config.load_focus_keys(conf)
    if focus:
        command = f"{sys.executable} {ZONEBIN} focus"
        if focus.get("zone"):
            command += f" {focus['zone']}"
        items.append(("focus", focus["key"], with_conf(command + " --quiet", conf)))
    return items


def push_tile_bindings():
    """`{направление: [сочетания]}` родного тайлинга muffin (`push-tile-*`).

    Читается только на предмет предупреждения: сочетания `move` и `push-tile`
    по умолчанию совпадают (`Win`+стрелки), а когда на одну клавишу претендуют
    два обработчика, срабатывает то один, то другой. Настройки Cinnamon скрипт
    не меняет — только сообщает о совпадении.
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
    """`[(направление, сочетание)]` — наши `move` на клавише родного тайлинга.

    `items` — результат `planned`; `native` — :func:`push_tile_bindings` (можно
    подменить в тестах). Сравнение точное, по строке сочетания: `Cinnamon`
    понимает обе стороны одинаково.
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
    """Предупредить в stderr о совпадении с `push-tile-*`; ничего не меняет."""
    for direction, binding in conflicts_with_push_tile(items):
        print(
            f"setup-hotkeys: внимание: {binding} для move {direction} совпадает с родным "
            f"{PUSH_TILE_PREFIX}{direction} muffin — на этой клавише сработает один из "
            "двух обработчиков. Родное сочетание не выключаю: задайте своё в [move] "
            "или отключите push-tile в настройках Cinnamon",
            file=sys.stderr,
        )


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

    pairs = [] if args.remove else planned(args.conf)
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

    new_list = kept + ids
    gs(LIST_SCHEMA, "custom-list", "[" + ", ".join(gvariant_str(i) for i in new_list) + "]")

    if args.remove:
        print("setup-hotkeys: сочетания zone-tiler удалены")
    else:
        print(f"setup-hotkeys: установлено сочетаний: {len(pairs)}")
        for name, binding, _command in pairs:
            print(f"  {binding:<22} {name}")
        warn_conflicts(pairs)


if __name__ == "__main__":
    main()

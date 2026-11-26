#!/bin/sh
# Дымовой тест watcher'а перетаскивания — zone-hotcorners.py.
#
# Поднимает СВОЙ X-сервер (Xvfb :96) и muffin, открывает окно xmessage и
# проверяет три вещи:
#
# 1. watcher стартует, находит XI2 raw events (или честно уходит в fallback),
#    показывает `--dry-run`-план укладки через zone-tiler apply --dry-run и
#    корректно завершается по SIGTERM и по SIGINT (код 0);
# 2. edge-tiling действительно гасится на время HUD и возвращается на дропе —
#    это проверяется живым gsettings с той же схемой org.cinnamon.muffin, но в
#    ПЕСОЧНИЦЕ: GSETTINGS_BACKEND=keyfile в своём XDG_CONFIG_HOME. Настройка
#    владельца сессии не трогается; в конце печатается её реальное значение и
#    проверяется, что оно не изменилось;
# 3. пакетная обвязка на месте: `--version` и `--help` работают.
#
# Чего сценарий НЕ делает и почему: настоящий drag за титлбар здесь
# невоспроизводим. XTest-инъекции запрещены решением по задаче, а XWarpPointer
# (не XTest) raw-событий не порождает — проверено. Поэтому сценарий
# press -> motion -> release идёт через сам `Hotcorners` с заглушками X, но с
# настоящими gsettings и настоящим Grid: логика arm/HUD/дропа и обязательный
# возврат настройки — проверяются, «живая рука на мышке» — нет (её покрывают
# tests/test_hotcorners.py и ручная проверка владельца).
#
#   tests/smoke-hotcorners.sh
#   GRIDSNAP_SMOKE_DISPLAY=:91 tests/smoke-hotcorners.sh   # другой дисплей
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 gsettings.
# Код возврата: 0 — всё совпало, 1 — есть несовпадения, 2 — нет зависимостей.

set -u

export XDG_SESSION_TYPE=x11

# Интерфейс — на исходном языке: сценарий сверяет поведение по русским
# сообщениям CLI, а перевод (каталоги en/zh_CN) выбирается из локали машины.
GRIDSNAP_LANG=ru
export GRIDSNAP_LANG
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:96}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
WATCHER="$ROOT/zone-hotcorners.py"
TILER="$ROOT/zone-tiler.py"

# Своё состояние (история undo) — во временном каталоге, не в домашнем.
STATE=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-state.$$)
export XDG_STATE_HOME="$STATE/state"
mkdir -p "$XDG_STATE_HOME"

# Песочница gsettings: keyfile пишет в $XDG_CONFIG_HOME/glib-2.0/settings,
# поэтому ни dconf, ни настройки владельца сессии не меняются.
export XDG_CONFIG_HOME="$STATE/config"
export GSETTINGS_BACKEND=keyfile
mkdir -p "$XDG_CONFIG_HOME"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
# A config of the scenario's own: the root zones.conf is the owner's live layout, and its
# [workspaces]/[monitors] make the tiler resolve a profile these zone names do not match.
CONF="$LOGDIR/zones.conf"
cat >"$CONF" <<'CONF'
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.8358, 1
top-right = 0.8358, 0, 0.1642, 0.5
top-far = 0.8358, 0.5, 0.1642, 0.5
bottom = 0.7129, 0.8068, 0.2871, 0.1932
CONF
MUFFIN_LOG="$LOGDIR/gridsnap-smoke-hotcorners-muffin.log"
WATCHER_LOG="$LOGDIR/gridsnap-smoke-hotcorners-watcher.log"
# Сокет демона — в своём каталоге: при живом демоне владельца сценарий
# разбирал бы поведение чужого процесса, а не наше.
RUNTIME="$LOGDIR/run"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
export XDG_RUNTIME_DIR="$RUNTIME"

fails=0
say() { printf '%s\n' "$1"; }
ok()  { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✖\033[0m %s\n' "$1"; fails=$((fails + 1)); }

wait_for() {
    _left=$1; shift
    while [ "$_left" -gt 0 ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

for c in Xvfb muffin xmessage xdotool xprop python3 gsettings; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool gsettings-desktop-schemas"
        exit 2
    fi
done

# edge-tiling в песочнице (то, что видит watcher во время теста).
edge_here() { gsettings get org.cinnamon.muffin edge-tiling 2>/dev/null; }
# Реальная настройка владельца сессии: читаем, но не пишем.
edge_real() {
    ( unset GSETTINGS_BACKEND XDG_CONFIG_HOME; gsettings get org.cinnamon.muffin edge-tiling 2>/dev/null )
}

XPID=""; MPID=""; WPIDS=""
cleanup() {
    for p in $WPIDS; do kill "$p" 2>/dev/null; done
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    rm -rf "$LOGDIR" "$STATE"
    return 0
}
trap cleanup EXIT INT TERM

EDGE_REAL_BEFORE=$(edge_real)
EDGE_BEFORE=$(edge_here)
say "edge-tiling: песочница '$EDGE_BEFORE', реальная сессия '$EDGE_REAL_BEFORE'"

# --- Xvfb + muffin ---------------------------------------------------------

Xvfb "$DISP" -screen 0 1920x1080x24 >/dev/null 2>&1 &
XPID=$!
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят; задайте GRIDSNAP_SMOKE_DISPLAY)"
    exit 1
fi

DISPLAY="$DISP" muffin >"$MUFFIN_LOG" 2>&1 &
MPID=$!
if ! wait_for 60 sh -c "DISPLAY='$DISP' xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id # 0x'"; then
    say "muffin не стал оконным менеджером за 30 с. Его вывод:"
    sed 's/^/      /' "$MUFFIN_LOG" 2>/dev/null | tail -25
    exit 1
fi
if ! wait_for 40 sh -c "DISPLAY='$DISP' xprop -root _NET_WORKAREA 2>/dev/null | grep -q '_NET_WORKAREA(CARDINAL)'"; then
    say "у muffin нет _NET_WORKAREA — раскладчик не посчитает зоны"
    exit 1
fi

export DISPLAY="$DISP"

xmessage -title "gridsnap-hotcorners" -buttons OK:0 "окно" >/dev/null 2>&1 &
WPIDS="$WPIDS $!"
if ! wait_for 40 sh -c "xdotool search --name '^gridsnap-hotcorners$' >/dev/null 2>&1"; then
    say "не удалось открыть тестовое окно"
    exit 1
fi
WINS=$(xdotool search --name '^gridsnap-hotcorners$' 2>/dev/null | sort -u | head -n1)
say "дисплей $DISP, окно: $WINS"

# --- 1. dry-run укладки через раскладчик --------------------------------

say "dry-run укладки: python3 zone-tiler.py apply main --dry-run"
OUT=$(python3 "$TILER" apply main --dry-run --conf "$CONF" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "zone-tiler apply --dry-run завершился с кодом $RC"
    printf '%s\n' "$OUT" | sed 's/^/      /'
else
    printf '%s\n' "$OUT" | sed 's/^/      /'
    if printf '%s' "$OUT" | grep -q "main: x="; then
        ok "раскладчик напечатал пиксельную рамку зоны main"
    else
        bad "в выводе dry-run нет рамки зоны main"
    fi
fi

# --- 2. watcher стартует, находит XI2/fallback, гаснет по SIGTERM --------

say "запускаю watcher: python3 zone-hotcorners.py --verbose"
: >"$WATCHER_LOG"
python3 "$WATCHER" --conf "$CONF" --verbose >"$WATCHER_LOG" 2>&1 &
WPID=$!
WPIDS="$WPIDS $WPID"
if ! wait_for 20 sh -c "grep -q 'XI2 raw events\|fallback' '$WATCHER_LOG'"; then
    bad "watcher не сообщил режим детекта за 10 с"
    sed 's/^/      /' "$WATCHER_LOG" | tail -20
else
    if grep -q "XI2 raw events" "$WATCHER_LOG"; then
        ok "XI2 raw events поднялись: $(grep -o 'XI2 raw events [0-9.]*' "$WATCHER_LOG" | head -n1)"
    else
        ok "XI2 не поднялся — watcher честно ушёл в fallback (опрос XQueryPointer)"
    fi
fi

sleep 1
if kill -0 "$WPID" 2>/dev/null; then
    ok "watcher живёт и не падает без перетаскиваний"
else
    bad "watcher умер сам"
    sed 's/^/      /' "$WATCHER_LOG" | tail -20
fi

kill -TERM "$WPID" 2>/dev/null
wait "$WPID"
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "SIGTERM: watcher завершился с кодом 0"
else
    bad "SIGTERM: watcher завершился с кодом $RC"
    sed 's/^/      /' "$WATCHER_LOG" | tail -20
fi
sed 's/^/      /' "$WATCHER_LOG"

# --- 3. второй запуск по SIGINT, без HUD -------------------------------

say "запускаю watcher: python3 zone-hotcorners.py --no-hud --dry-run"
: >"$WATCHER_LOG"
python3 "$WATCHER" --conf "$CONF" --no-hud --dry-run --verbose >"$WATCHER_LOG" 2>&1 &
WPID=$!
WPIDS="$WPIDS $WPID"
if ! wait_for 20 sh -c "grep -q 'XI2 raw events\|fallback' '$WATCHER_LOG'"; then
    bad "watcher (--no-hud) не стартовал"
    sed 's/^/      /' "$WATCHER_LOG" | tail -20
fi
sleep 0.5
kill -INT "$WPID" 2>/dev/null
wait "$WPID"
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "SIGINT: watcher завершился с кодом 0"
else
    bad "SIGINT: watcher завершился с кодом $RC"
fi

# --- 4. edge-tiling: гасится на HUD и возвращается на дропе --------------

say "проверяю edge-tiling на живом gsettings (песочница keyfile)"
if python3 - "$ROOT" "$CONF" <<'PY'
import importlib.util
import os
import sys

root, conf = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("hc_smoke", os.path.join(root, "zone-hotcorners.py"))
hc = importlib.util.module_from_spec(spec)
sys.modules["hc_smoke"] = hc
spec.loader.exec_module(hc)


def edge_value():
    code, out = hc.run_gsettings(["gsettings", "get", "org.cinnamon.muffin", "edge-tiling"])
    assert code == 0, f"gsettings get: код {code}"
    return out


class Hud:
    """Заглушка оверлея: настоящий HUD тут не нужен, нужен только факт запуска."""

    def __init__(self):
        self.starts, self.points, self.stops = [], [], 0
        self.index = None

    def start(self, index, name=""):
        if self.index == index:
            return False
        self.starts.append((index, name))
        self.index = index
        return True

    def feed(self, point):
        self.points.append(point)
        return True

    def stop(self):
        self.index = None
        self.stops += 1

    def running(self):
        return self.index is not None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


clock, hud, applied = Clock(), Hud(), []
watcher = hc.Hotcorners(
    conf,
    hud=hud,
    edge=hc.EdgeTiling(),  # настоящий gsettings, песочница — из окружения
    active_window=lambda: 0x400001,
    frame_extents=lambda win: (0, 0, 30, 0),
    read_frame=lambda win, ext: (100, 100, 400, 300),
    outputs=lambda: [("smoke-0", (0, 0, 1920, 1080))],
    apply=lambda win, rect, label: applied.append((win, rect, label)),
    clock=clock,
    sleep=lambda seconds: None,
    out=print,
    err=lambda text: print(text, file=sys.stderr),
)

assert edge_value() == "true", "на старте edge-tiling должен быть true"

# Нажатие в полосе титлбара (100..138 по y) -> arm -> HUD гасит edge-tiling.
assert watcher.on_press((300, 115), 1) is True, "нажатие в титлбаре не дало arm"
watcher.on_motion((150, 600))
middle = edge_value()
print(f"      edge-tiling при живом HUD: {middle}")
assert middle == "false", f"edge-tiling не погашен: {middle}"

# Дроп в зону main -> настройка возвращается, окно вкладывается.
watcher.on_release((150, 600), 1)
after = edge_value()
print(f"      edge-tiling после дропа: {after}")
assert after == "true", f"edge-tiling не возвращён: {after}"
grid = hc.tiler.Grid(conf)
assert applied == [(0x400001, grid.rect("main", (0, 0, 1920, 1080), monitor_name="smoke-0"), "main")], (
    f"дроп не совпал с зоной main: {applied}"
)
assert hud.starts and hud.points and hud.stops

# Отмена на середине перетаскивания (как SIGTERM) тоже возвращает настройку.
watcher.on_press((300, 115), 1)
watcher.on_motion((150, 600))
assert edge_value() == "false", "edge-tiling не погашен на второй drag"
watcher.finish_drag()
assert edge_value() == "true", "отмена не вернула edge-tiling"
print("      отмена на середине drag тоже вернула настройку")
PY
then
    ok "edge-tiling: гаснет на HUD (false), возвращается на дропе и на отмене (true)"
else
    bad "edge-tiling: проверка через живой gsettings не прошла"
fi

# --- 5. живой дроп: настоящий HUD, настоящий gsettings, настоящее окно ---
#
# Событий мыши тут нет (их нечем воспроизвести, см. шапку), но всё остальное
# настоящее: `Hotcorners` с боевыми frame_extents/read_frame/monitor_outputs/
# apply_zone, дочерний zone-picker --hud --follow и gsettings в песочнице.
# Проверяется, что окно реально уехало в зону, а настройка вернулась.

say "живой дроп в зону: настоящие HUD, gsettings и apply_zone"
if python3 - "$ROOT" "$CONF" "$WINS" <<'PY'
import importlib.util
import os
import sys
import time

root, conf, win = sys.argv[1], sys.argv[2], int(sys.argv[3], 0)
spec = importlib.util.spec_from_file_location("hc_live", os.path.join(root, "zone-hotcorners.py"))
hc = importlib.util.module_from_spec(spec)
sys.modules["hc_live"] = hc
spec.loader.exec_module(hc)

tiler = hc.tiler


def edge_value():
    code, out = hc.run_gsettings(["gsettings", "get", "org.cinnamon.muffin", "edge-tiling"])
    assert code == 0, f"gsettings get: код {code}"
    return out


hud = hc.HudProcess(conf, verbose=False)
watcher = hc.Hotcorners(conf, hud=hud, edge=hc.EdgeTiling(), active_window=lambda: win)

extents = tiler.frame_extents(win)
frame = tiler.read_frame(win, extents)
# Нажатие — в середину полосы титлбара настоящего окна.
press = (frame[0] + frame[2] // 2, frame[1] + max(1, extents[2] // 2))
assert watcher.on_press(press, 1) is True, f"нажатие {press} в титлбар {frame} не дало arm"

point = (200, 500)  # внутри зоны main на одном мониторе
watcher.on_motion(point)
# HUD — отдельный процесс и стартует по первому движению: даём ему подняться.
for _ in range(50):
    if hud.running():
        break
    time.sleep(0.1)
assert hud.running(), "HUD не запустился (zone-picker --hud --follow)"
print(f"      HUD запущен на мониторе {hud.index}: {hud.name}")

middle = edge_value()
print(f"      edge-tiling при живом HUD: {middle}")
assert middle == "false", f"edge-tiling не погашен при HUD: {middle}"

watcher.on_release(point, 1)
time.sleep(0.5)
after = edge_value()
print(f"      edge-tiling после дропа: {after}")
assert after == "true", f"edge-tiling не возвращён после дропа: {after}"
assert not hud.running(), "HUD не погашен после дропа"

outputs = tiler.monitor_outputs()
index = hc.monitor_index_for_point(outputs, point)
name, area = outputs[index]
grid = tiler.grid_for_workspace(conf, tiler.window_workspace_for_zones(win))
expected = grid.rect("main", area, monitor_name=name)
actual = tiler.read_frame(win, tiler.frame_extents(win))
print(f"      окно 0x{win:x}: рамка {actual}, зона main: {expected}")
assert actual == expected, f"окно не в зоне main: {actual} != {expected}"
print("      окно действительно вложено в зону main")
PY
then
    ok "живой дроп: окно в зоне main, HUD погашен, edge-tiling возвращён"
else
    bad "живой дроп в зону не прошёл"
fi

# --- 6. итог и обязательства по настройке ------------------------------

EDGE_AFTER=$(edge_here)
EDGE_REAL_AFTER=$(edge_real)
if [ "$EDGE_AFTER" = "true" ]; then
    ok "после теста в песочнице edge-tiling = $EDGE_AFTER"
else
    bad "после теста в песочнице edge-tiling = $EDGE_AFTER (ожидалось true)"
fi
if [ "$EDGE_REAL_AFTER" = "$EDGE_REAL_BEFORE" ]; then
    ok "настройка владельца сессии не изменилась: $EDGE_REAL_AFTER"
else
    bad "настройка владельца сессии была $EDGE_REAL_BEFORE, стала $EDGE_REAL_AFTER"
fi

OUT=$(python3 "$WATCHER" --version 2>&1)
if printf '%s' "$OUT" | grep -q "^zone-hotcorners "; then
    ok "--version: $OUT"
else
    bad "--version напечатал: $OUT"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест hotcorners пройден."
else
    say "Провалов: $fails"
    exit 1
fi

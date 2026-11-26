#!/bin/sh
# Дымовой тест HiDPI-масштаба (GDK_SCALE=2).
#
# Поднимает СВОЙ X-сервер (Xvfb :97) и muffin, открывает тестовое окно и на
# масштабе 2 проверяет, что все, кто переводит доли зоны в пиксели, дают одну и
# ту же рамку — физическую рамку X:
#
#   1. раскладчик: `apply <зона>` кладёт окно в долю ФИЗИЧЕСКОЙ рабочей области
#      (`_NET_WORKAREA`), а не логической, которую отдаёт GDK;
#   2. пикер: `--shot` рисует оверлей размером с физическую рабочую область;
#   3. HUD: точка в физических координатах подсвечивает ту же зону, в которую
#      кладёт окно раскладчик.
#
# Ожидаемые рамки сценарий считает сам — прямо из долей `zones.conf` и
# `_NET_WORKAREA`, не спрашивая `zone-tiler.py`: иначе тест сверял бы модуль с
# ним же и «совпало с зоной» печаталось бы и при съехавшей сетке.
#
#   tests/smoke-dpi.sh [масштаб]     # по умолчанию 2
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 python3-gi
#   gir1.2-gtk-3.0 python3-cairo
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11

# Интерфейс — на исходном языке: сценарий сверяет поведение по русским
# сообщениям CLI, а перевод (каталоги en/zh_CN) выбирается из локали машины.
GRIDSNAP_LANG=ru
export GRIDSNAP_LANG
unset WAYLAND_DISPLAY 2>/dev/null || true

SCALE=${1:-2}
# Масштаб, отличный от 1, и есть предмет проверки: на нём логические пиксели GDK
# расходятся с физическими X. `GDK_SCALE` целочисленный — GTK3 на X11 умеет
# только это, поэтому дробный масштаб Cinnamon решается растяжением выхода, где
# физическим уже является и то, что видит GDK.
if [ "$SCALE" = "1" ]; then
    echo "smoke-dpi: масштаб 1 — это обычный случай, его проверяет tests/smoke.sh"
    exit 2
fi
export GDK_SCALE="$SCALE"

DISP=":97"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
PICKER="$ROOT/zone-picker.py"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
# A config of the scenario's own: the root zones.conf is the owner's live layout, and its
# [workspaces]/[monitors] make the tiler resolve a profile the expectations below do not read.
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
MUFFIN_LOG="$LOGDIR/gridsnap-dpi-muffin.log"
export XDG_STATE_HOME="$LOGDIR/state"
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

# Ждать до $1 полусекундных попыток, пока команда из $2... вернёт 0.
wait_for() {
    _left=$1; shift
    while [ "$_left" -gt 0 ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

for c in Xvfb muffin xmessage xdotool python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool python3"
        exit 2
    fi
done

XPID=""; MPID=""; WPID=""; HUDPID=""
cleanup() {
    [ -n "$HUDPID" ] && kill "$HUDPID" 2>/dev/null
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    # SMOKE_DPI_KEEP=1 оставляет логи и снимки — разбираться, что не сошлось.
    if [ "${SMOKE_DPI_KEEP:-0}" = "1" ]; then
        say "логи: $LOGDIR"
    else
        rm -rf "$LOGDIR"
    fi
    return 0
}
trap cleanup EXIT INT TERM

Xvfb "$DISP" -screen 0 1920x1080x24 >/dev/null 2>&1 &
XPID=$!
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят — закройте лишний Xvfb)"
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
    say "у muffin нет _NET_WORKAREA — не с чем сравнивать. Вывод muffin:"
    sed 's/^/      /' "$MUFFIN_LOG" 2>/dev/null | tail -25
    exit 1
fi

DISPLAY="$DISP" xmessage -title "smoke-dpi-window" -buttons OK:0 "тестовое окно" >/dev/null 2>&1 &
WPID=$!
export DISPLAY="$DISP"
WIN=""
_left=40
while [ "$_left" -gt 0 ]; do
    WIN=$(xdotool search --name "^smoke-dpi-window$" 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WIN" ]; then
    say "не удалось открыть тестовое окно"
    exit 1
fi
xdotool windowactivate --sync "$WIN" 2>/dev/null

# Ожидаемые рамки и точки: доли из конфига × ФИЗИЧЕСКАЯ рабочая область из EWMH.
# Печатает строки «имя x y w h cx cy зона_под_центром».
expected_zones() {  # $1 = конфиг
    python3 - "$1" <<'PY'
import configparser
import re
import subprocess
import sys

cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
cp.read(sys.argv[1], encoding="utf-8")
txt = subprocess.check_output(["xprop", "-root", "_NET_WORKAREA"], text=True)
wx, wy, ww, wh = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]


def px(zone):
    fx, fy, fw, fh = (float(v) for v in zone)
    # края зоны считаются от одних и тех же линий: соседние зоны стыкуются
    x1, x2 = wx + round(ww * fx), wx + round(ww * (fx + fw))
    y1, y2 = wy + round(wh * fy), wy + round(wh * (fy + fh))
    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))


zones = []
for name, spec in cp.items("zones"):
    zones.append((name.strip(), px([v for v in spec.split(",")])))

for name, (x, y, w, h) in zones:
    cx, cy = x + w // 2, y + h // 2
    # под центром зоны — последняя описанная зона, как и в пикере
    under = ""
    for other, (ox, oy, ow, oh) in zones:
        if ox <= cx < ox + ow and oy <= cy < oy + oh:
            under = other
    print(name, x, y, w, h, cx, cy, under)
PY
}

# Фактическая рамка окна: xwininfo плюс _NET_FRAME_EXTENTS, как их читает раскладчик.
actual_frame() {  # $1 = окно
    python3 - "$1" <<'PY'
import re
import subprocess
import sys

win = int(sys.argv[1], 0)


def nums(text):
    return [int(v) for v in re.findall(r"-?\d+", text.split("=", 1)[1])][:4]


txt = subprocess.check_output(["xprop", "-id", hex(win), "_NET_FRAME_EXTENTS"], text=True)
ext = nums(txt)
if len(ext) != 4 or not all(0 <= n < 200 for n in ext):
    ext = [0, 0, 0, 0]
info = subprocess.check_output(["xwininfo", "-id", hex(win)], text=True)
value = {}
for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height"):
    found = re.search(rf"{key}:\s+(-?\d+)", info)
    value[key] = int(found.group(1))
left, right, top, bottom = ext
print(
    value["Absolute upper-left X"] - left,
    value["Absolute upper-left Y"] - top,
    value["Width"] + left + right,
    value["Height"] + top + bottom,
)
PY
}

EXPECTED="$LOGDIR/expected.txt"
if ! expected_zones "$CONF" >"$EXPECTED"; then
    say "не удалось посчитать ожидаемые рамки"
    exit 1
fi
if [ ! -s "$EXPECTED" ]; then
    say "в $CONF нет зон"
    exit 1
fi

say "дисплей $DISP, масштаб GDK_SCALE=$SCALE, тестовое окно $WIN"
say "рабочая область X: $(xprop -root _NET_WORKAREA | sed 's/.*= //')"
say ""

# ---- 1. раскладчик: окно встаёт в долю физической рабочей области
say "раскладчик — доля физической рабочей области:"
while read -r zone x y w h _cx _cy _under; do
    out=""
    attempt=0
    while :; do
        python3 "$TILER" apply "$zone" --window "$WIN" --conf "$CONF" >/dev/null 2>&1
        out=$(actual_frame "$WIN")
        [ "$out" = "$x $y $w $h" ] && break
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && break
        sleep 0.5
    done
    if [ "$out" = "$x $y $w $h" ]; then
        ok "$zone: $out [$x $y $w $h]"
    else
        bad "$zone: факт $out, ожидалось $x $y $w $h"
    fi
done <"$EXPECTED"
say ""

# ---- 2. пикер: оверлей рисуется размером с физическую рабочую область
say "пикер — размер оверлея:"
wa_w=$(python3 - <<'PY'
import re
import subprocess

txt = subprocess.check_output(["xprop", "-root", "_NET_WORKAREA"], text=True)
nums = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
print(nums[2], nums[3])
PY
)
SHOT="$LOGDIR/shot.png"
if python3 "$PICKER" --shot "$SHOT" --monitor 0 --conf "$CONF" >"$LOGDIR/shot.log" 2>&1; then
    shot_size=$(python3 - "$SHOT" <<'PY'
import struct
import sys

with open(sys.argv[1], "rb") as fh:
    head = fh.read(24)
if head[:8] != b"\x89PNG\r\n\x1a\n":
    print("не PNG")
else:
    print(struct.unpack(">II", head[16:24])[0], struct.unpack(">II", head[16:24])[1])
PY
)
    if [ "$shot_size" = "$wa_w" ]; then
        ok "оверлей $shot_size [$wa_w]"
    else
        bad "оверлей $shot_size, ожидалось $wa_w"
    fi
else
    bad "zone-picker --shot упал"
    sed 's/^/      /' "$LOGDIR/shot.log" | tail -10
fi
say ""

# ---- 3. HUD: физическая точка подсвечивает ту же зону, что и укладка
say "HUD — зона под физической точкой:"
# Точки — центры зон в физических координатах. Подаём их по трубе, как это
# делает zone-hotcorners: GLib-наблюдатель за stdin на обычном файле под
# dbus-run-session не срабатывает, а на трубе — да.
(awk '{print $6, $7}' "$EXPECTED" \
    | python3 "$PICKER" --hud --follow --monitor 0 --conf "$CONF" \
        >"$LOGDIR/hud.log" 2>&1) &
HUDPID=$!
( sleep 15; kill "$HUDPID" 2>/dev/null ) &
_watch=$!
wait "$HUDPID" 2>/dev/null
_hudrc=$?
kill "$_watch" 2>/dev/null
HUDPID=""
if [ "$_hudrc" = "0" ]; then
    python3 - "$LOGDIR/hud.log" "$EXPECTED" <<'PY'
import sys

want = {}
with open(sys.argv[2], encoding="utf-8") as fh:
    for line in fh:
        name, _x, _y, _w, _h, cx, cy, under = line.split()
        want[(int(cx), int(cy))] = under

seen = 0
bad = 0
for line in open(sys.argv[1], encoding="utf-8"):
    # «zone-picker: 1800,200 -> top-right»
    if not line.startswith("zone-picker:") or "->" not in line:
        continue
    head, tail = line.split("->", 1)
    point = head.split()[-1].split(",")
    key = (int(point[0]), int(point[1]))
    expected = want.get(key)
    if expected is None:
        continue
    seen += 1
    got = tail.strip()
    if got != expected:
        print(f"  \033[31m✖\033[0m HUD {key[0]},{key[1]}: {got}, ожидалось {expected}")
        bad += 1
    else:
        print(f"  \033[32m✔\033[0m HUD {key[0]},{key[1]}: {got}")
if seen != len(want):
    print(f"  \033[31m✖\033[0m HUD ответил на {seen} точек из {len(want)}")
    bad += 1
sys.exit(1 if bad else 0)
PY
    if [ $? -ne 0 ]; then fails=$((fails + 1)); fi
else
    bad "HUD не отработал точки (код $_hudrc)"
    sed 's/^/      /' "$LOGDIR/hud.log" | tail -10
fi

wa_geom=$(python3 - <<'PY'
import re
import subprocess

txt = subprocess.check_output(["xprop", "-root", "_NET_WORKAREA"], text=True)
wx, wy, ww, wh = [int(v) for v in re.findall(r"-?\d+", txt.split("=", 1)[1])][:4]
print(wx, wy, ww, wh)
PY
)

# ---- 4. окно оверлея: HUD и пикер накрывают монитор ФИЗИЧЕСКИ
# GTK-координаты логические, поэтому окно, которому задали физические размеры,
# вырастает вдвое и уезжает на соседний экран. Видно это только в X.
say "окно оверлея — физический размер монитора:"
overlay_geometry() {  # $1 = подстрока заголовка
    _win=$(xdotool search --name "$1" 2>/dev/null | head -1)
    [ -z "$_win" ] && return 1
    xwininfo -id "$_win" 2>/dev/null \
        | awk '/Absolute upper-left X/{x=$NF} /Absolute upper-left Y/{y=$NF} /Width:/{w=$NF} /Height:/{h=$NF} END{print x, y, w, h}'
}

# HUD живёт, пока открыт его stdin: держим трубу паузой.
( sleep 25 ) | python3 "$PICKER" --hud --follow --monitor 0 --conf "$CONF" \
    >"$LOGDIR/hud-geom.log" 2>&1 &
HUDPID=$!
_got=""
_left=30
while [ "$_left" -gt 0 ]; do
    _got=$(overlay_geometry "Gridsnap — сетка" 2>/dev/null) && [ -n "$_got" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
kill "$HUDPID" 2>/dev/null
HUDPID=""
if [ "$_got" = "$wa_geom" ]; then
    ok "HUD: $_got [$wa_geom]"
else
    bad "HUD: окно ${_got:-не найдено}, ожидалось $wa_geom"
fi

# Интерактивный оверлей: то же окно, другая ветка кода.
python3 "$PICKER" --window "$WIN" --monitor 0 --conf "$CONF" \
    >"$LOGDIR/picker-geom.log" 2>&1 &
HUDPID=$!
_got=""
_left=30
while [ "$_left" -gt 0 ]; do
    _got=$(overlay_geometry "Gridsnap — выбор зоны" 2>/dev/null) && [ -n "$_got" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
kill "$HUDPID" 2>/dev/null
HUDPID=""
if [ "$_got" = "$wa_geom" ]; then
    ok "пикер: $_got [$wa_geom]"
else
    bad "пикер: окно ${_got:-не найдено}, ожидалось $wa_geom"
fi
say ""

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест масштаба пройден."
else
    say "Провалов: $fails"
    exit 1
fi

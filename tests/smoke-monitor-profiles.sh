#!/bin/sh
# Дымовой тест привязки профиля к экрану (секция `[monitors]`).
#
# Поднимает СВОЙ X-сервер (Xvfb :93), muffin и два виртуальных монитора (общий
# helper tests/two_monitors.py), заводит два профиля и привязку «экран → профиль»,
# и проверяет:
#
#   1. `zone-tiler.py profile use-monitors` собирает активный конфиг: у каждого
#      экрана свой набор зон (это видно по `list --monitor`), а правила обоих
#      профилей слиты в конфиг;
#   2. повторный вызов ничего не меняет (идемпотентность);
#   3. `apply` без `--monitor` работает на экране окна: зона ищется в профиле того
#      экрана, где окно (`--verify` подтверждает рамку), а зона чужого экрана —
#      ошибка конфига (код 3), а не молчаливый промах;
#   4. демон кладёт новое окно по правилу профиля **того экрана, где окно**: у
#      правила в профилях разных экранов разные зоны, и рамка окна должна совпасть
#      с зоной его собственного экрана.
#
# Своё состояние, свой runtime-каталог и свой конфиг — во временном каталоге:
# ни настройки, ни окна владельца сценарий не задевает.
#
#   tests/smoke-monitor-profiles.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 libxrandr2
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:93}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
DAEMON="$ROOT/zone-daemon.py"
CTL="$ROOT/gridsnap-ctl"
HELPER="$ROOT/tests/two_monitors.py"

# Раскладка экрана: два монитора по половине.
SCREEN_W=1920
SCREEN_H=1080
MON_W=960
MON_H=1080
MON2_X=960

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-monitor-profiles.$$)
CONF="$LOGDIR/zones.conf"
TEMPLATES="$LOGDIR/templates"
MONLIST="$LOGDIR/monitors.txt"
HELPER_LOG="$LOGDIR/gridsnap-mp-helper.log"
MUFFIN_LOG="$LOGDIR/gridsnap-mp-muffin.log"
DAEMON_LOG="$LOGDIR/gridsnap-mp-daemon.log"

# Своё состояние и свой сокет — не владельца.
STATE="$LOGDIR/state"
RUNTIME="$LOGDIR/run"
mkdir -p "$STATE" "$RUNTIME" "$TEMPLATES"
chmod 700 "$RUNTIME"
export XDG_STATE_HOME="$STATE"
export XDG_RUNTIME_DIR="$RUNTIME"
unset XDG_CONFIG_HOME 2>/dev/null || true

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

for c in Xvfb muffin xmessage xdotool python3 xprop xwininfo; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool python3"
        exit 2
    fi
done
if ! python3 -c 'import ctypes, ctypes.util, sys; sys.exit(0 if ctypes.util.find_library("Xrandr") else 1)' 2>/dev/null; then
    say "нет libXrandr (нужна для виртуальных мониторов): sudo apt install libxrandr2"
    exit 2
fi

XPID=""; MPID=""; HPID=""; DPID=""; WPIDS=""
cleanup() {
    for p in $WPIDS; do kill "$p" 2>/dev/null; done
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$HPID" ] && kill "$HPID" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

# Фактическая рамка окна с компенсацией _NET_FRAME_EXTENTS — как её читает
# раскладчик, чтобы тест не считал геометрию сам.
frame_of() {  # $1 = окно -> «x y w h»
    python3 - "$TILER" "$1" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_mp", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

# Рамка зоны по данным самого раскладчика: `list --monitor` печатает
# `  <зона>  x=… y=… WxH`, из чего и собирается «x y w h».
zone_frame() {  # $1 = зона, $2 = монитор -> «x y w h»
    python3 "$TILER" list --monitor "$2" --conf "$CONF" 2>/dev/null | awk -v z="$1" '
        $1 == z {
            split($2, a, "="); split($3, b, "="); split($4, c, "x");
            print a[2], b[2], c[1], c[2]
        }'
}

# ---------------------------------------------------------------- X и muffin

Xvfb "$DISP" -screen 0 "${SCREEN_W}x${SCREEN_H}x24" >/dev/null 2>&1 &
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
    say "у muffin нет _NET_WORKAREA — раскладчик не посчитает зоны"
    exit 1
fi

export DISPLAY="$DISP"

# ------------------------------------------------- два виртуальных монитора

python3 "$HELPER" "$MONLIST" "$MON_W" "$MON_H" "MP-LEFT:0" "MP-RIGHT:$MON2_X" \
    >"$HELPER_LOG" 2>&1 &
HPID=$!
if ! wait_for 30 test -s "$MONLIST"; then
    say "не удалось создать два виртуальных монитора. Вывод helper:"
    sed 's/^/      /' "$HELPER_LOG" 2>/dev/null | tail -25
    exit 1
fi

# Мониторы глазами раскладчика: `ИМЯ X Y W H`. Экран выбираем по X, а не по
# порядку строк: порядок рабочих областей GDK сценарию не подчиняется.
VIEW=$(python3 - "$TILER" <<'PY'
import importlib.util
import os
import sys

tiler = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(tiler))
spec = importlib.util.spec_from_file_location("zt_smoke_mp_view", tiler)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, area in module.monitor_outputs():
    print("%s %d %d %d %d" % ((name,) + tuple(area)))
PY
)
if [ -z "$VIEW" ]; then
    say "раскладчик не увидел мониторов"
    exit 1
fi
LEFT_LINE=$(printf '%s\n' "$VIEW" | awk '$2 == 0' | head -1)
RIGHT_LINE=$(printf '%s\n' "$VIEW" | awk '$2 > 0' | head -1)
if [ -z "$LEFT_LINE" ] || [ -z "$RIGHT_LINE" ]; then
    say "ожидались два монитора (левый с X=0 и правый с X>0), а раскладчик видит:"
    printf '%s\n' "$VIEW" | sed 's/^/      /'
    exit 1
fi
LEFT=$(printf '%s' "$LEFT_LINE" | awk '{print $1}')
RIGHT=$(printf '%s' "$RIGHT_LINE" | awk '{print $1}')
say "дисплей $DISP, экраны: $LEFT (слева) и $RIGHT (справа)"

# ------------------------------------------------------------- профили

cat >"$TEMPLATES/studio.conf" <<'EOF'
[zones]
code = 0, 0, 0.5, 1
chat = 0.5, 0, 0.5, 1

[rules]
re:gridsnap-mp-studio$ = code
EOF
cat >"$TEMPLATES/cinema.conf" <<'EOF'
[zones]
film = 0, 0, 0.5, 1

[rules]
re:gridsnap-mp-film$ = film
EOF

cat >"$CONF" <<EOF
# Конфиг дымового теста привязки профиля к экрану.
[grid]
cols = 1.0
rows = 1.0

[zones]
fallback = 0, 0, 1, 1

[monitors]
$LEFT = studio
$RIGHT = cinema
EOF

# --- 1. активация привязки ---------------------------------------------
say ""
say "1. profile use-monitors раскладывает экраны по их профилям"
python3 "$TILER" profile use-monitors --quiet --conf "$CONF" >"$LOGDIR/use.log" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "use-monitors завершился с кодом $RC ($(tail -2 "$LOGDIR/use.log" | tr '\n' ' '))"
else
    ok "use-monitors прошёл"
    LEFT_ZONES=$(python3 "$TILER" list --monitor "$LEFT" --conf "$CONF" 2>/dev/null | awk 'NR>1{print $1}' | sort | tr '\n' ' ')
    RIGHT_ZONES=$(python3 "$TILER" list --monitor "$RIGHT" --conf "$CONF" 2>/dev/null | awk 'NR>1{print $1}' | sort | tr '\n' ' ')
    if [ "$LEFT_ZONES" = "chat code " ]; then
        ok "на экране $LEFT зоны профиля «studio» ($LEFT_ZONES)"
    else
        bad "на экране $LEFT не зоны studio: '$LEFT_ZONES'"
    fi
    if [ "$RIGHT_ZONES" = "film " ]; then
        ok "на экране $RIGHT зоны профиля «cinema» ($RIGHT_ZONES)"
    else
        bad "на экране $RIGHT не зоны cinema: '$RIGHT_ZONES'"
    fi
    RULES=$(grep -c '^re:gridsnap-mp-' "$CONF" 2>/dev/null)
    if [ "$RULES" -ge 2 ]; then
        ok "правила обоих профилей слиты в конфиг ($RULES строк)"
    else
        bad "в конфиге нет слитых правил обоих профилей (найдено $RULES)"
    fi
fi

# --- 2. идемпотентность -----------------------------------------------
say ""
say "2. Повторный вызов ничего не меняет"
cp "$CONF" "$LOGDIR/before.conf"
python3 "$TILER" profile use-monitors --quiet --conf "$CONF" >/dev/null 2>&1
if cmp -s "$CONF" "$LOGDIR/before.conf"; then
    ok "конфиг после второго вызова тот же"
else
    bad "повторный use-monitors переписал конфиг иначе"
    diff "$LOGDIR/before.conf" "$CONF" | head -10 | sed 's/^/      /'
fi

# --- 3. apply без --monitor работает на экране окна --------------------
say ""
say "3. apply без --monitor ищет зону в профиле экрана окна"
DISPLAY="$DISP" xmessage -title "gridsnap-mp-window" -buttons OK:0 "окно теста" >/dev/null 2>&1 &
WPIDS="$WPIDS $!"
WIN=""
_left=40
while [ "$_left" -gt 0 ]; do
    WIN=$(xdotool search --name '^gridsnap-mp-window$' 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WIN" ]; then
    bad "не удалось открыть тестовое окно"
else
    # Окно ставим на правый экран: без `--monitor` команда должна понять это сама.
    xdotool windowmove "$WIN" $((MON2_X + 40)) 40 2>/dev/null
    sleep 1
    OUT=$(python3 "$TILER" apply film --window "$WIN" --verify --conf "$CONF" 2>&1)
    RC=$?
    WANT=$(zone_frame film "$RIGHT")
    HAVE=$(frame_of "$WIN")
    printf '%s\n' "$OUT" | sed 's/^/      /'
    if [ "$RC" -eq 0 ] && [ "$HAVE" = "$WANT" ]; then
        ok "окно на экране $RIGHT встало в зону film его профиля ($HAVE)"
    else
        bad "окно не в зоне film экрана $RIGHT: код $RC, факт '$HAVE', ожидалось '$WANT'"
    fi
    python3 "$TILER" apply code --window "$WIN" --conf "$CONF" >"$LOGDIR/wrong.log" 2>&1
    RC=$?
    if [ "$RC" -eq 3 ]; then
        ok "зона чужого экрана (code) — ошибка конфига, а не молчаливый промах"
    else
        bad "зона чужого экрана дала код $RC вместо 3: $(tail -1 "$LOGDIR/wrong.log")"
    fi
fi

# --- 4. демон: профиль экрана читается заново, а не из снимка ---------
say ""
say "4. Демон читает профиль экрана, а не снимок конфига (после reload)"
python3 "$DAEMON" --conf "$CONF" --verbose >"$DAEMON_LOG" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$RUNTIME/gridsnap/daemon.sock"; then
    bad "демон не поднял сокет за 20 с ($(tail -3 "$DAEMON_LOG" | tr '\n' ' '))"
else
    DISPLAY="$DISP" xmessage -title "gridsnap-mp-where" -buttons OK:0 "где я" >/dev/null 2>&1 &
    WPIDS="$WPIDS $!"
    WIN2=""
    _left=40
    while [ "$_left" -gt 0 ]; do
        WIN2=$(xdotool search --name '^gridsnap-mp-where$' 2>/dev/null | head -1)
        [ -n "$WIN2" ] && break
        sleep 0.5
        _left=$((_left - 1))
    done
    if [ -z "$WIN2" ]; then
        bad "не удалось открыть окно для проверки демона"
    else
        # Окно ставим на правый экран (куда muffin кладёт новые окна, сценарию не
        # подчиняется) — дальше просим демона уложить его через IPC.
        xdotool windowmove "$WIN2" $((MON2_X + 40)) 40 2>/dev/null
        sleep 1
        # Правку профиля делаем ПОСЛЕ активации: в конфиге лежит снимок со старыми
        # зонами. Демон кэширует сетки до `reload` — это его контракт («новый профиль
        # экрана подхватывается без рестарта»), поэтому просим перечитать конфиг и
        # только потом укладываем. Ширина film: половина экрана → четверть.
        sed -i 's|^film = 0, 0, 0.5, 1$|film = 0, 0, 0.25, 1|' "$TEMPLATES/cinema.conf"
        python3 "$CTL" reload >/dev/null 2>&1
        # Ожидаемую рамку считает сам раскладчик по профилю (profile как конфиг).
        WANT=$(python3 "$TILER" apply film --monitor "$RIGHT" --dry-run \
            --conf "$TEMPLATES/cinema.conf" 2>/dev/null \
            | sed -n 's/.*x=\([-0-9]*\) y=\([-0-9]*\) \([0-9]*\)x\([0-9]*\).*/\1 \2 \3 \4/p')
        python3 "$CTL" apply film --window "$WIN2" >"$LOGDIR/daemon-apply.log" 2>&1
        RC=$?
        HAVE=$(frame_of "$WIN2" 2>/dev/null)
        if [ "$RC" -eq 0 ] && [ -n "$WANT" ] && [ "$HAVE" = "$WANT" ]; then
            ok "демон взял обновлённый профиль экрана $RIGHT после reload: $HAVE"
        else
            bad "демон взял снимок, а не профиль экрана: код $RC, факт '$HAVE', ожидалось '$WANT' ($(tail -1 "$LOGDIR/daemon-apply.log"))"
        fi
        python3 "$CTL" apply code --window "$WIN2" >"$LOGDIR/daemon-wrong.log" 2>&1
        RC=$?
        if [ "$RC" -ne 0 ]; then
            ok "зона чужого экрана в IPC тоже отклонена"
        else
            bad "демон принял зону чужого экрана (код 0)"
        fi
    fi
fi

python3 - "$RUNTIME" <<'PY' >/dev/null 2>&1 || true
import glob
import os
import socket
import sys

path = os.path.join(sys.argv[1], "gridsnap", "daemon.sock")
if os.path.exists(path):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(path)
    conn.sendall(b'{"cmd": "quit"}\n')
    conn.close()
PY
[ -n "$DPID" ] && kill "$DPID" 2>/dev/null
DPID=""

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест привязки профиля к экрану пройден."
    exit 0
fi
say "Дымовой тест привязки профиля к экрану: расхождений $fails."
exit 1

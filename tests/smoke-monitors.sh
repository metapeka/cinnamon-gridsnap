#!/bin/sh
# Дымовой тест «зоны своего монитора» на двух мониторах (Cinnamon/X11).
#
# Что проверяет: наборы зон `[zones:<выход>]` и `--monitor` (имя выхода и номер)
# на НАСТОЯЩЕМ X-сервере, у которого два монитора. Юнит-тесты подменяют xrandr и
# GDK, поэтому только здесь видно, что имя второго выхода доходит до раскладчика,
# а окно действительно уезжает во вторую половину экрана.
#
#   tests/smoke-monitors.sh
#
# Почему мониторы задаёт отдельный процесс, а не `xrandr --setmonitor`:
#   * Xvfb на один экран даёт ровно один выход (`screen`), сколько бы `-screen`
#     ни передать: каждый `-screen` — это отдельный X-экран, а не второй выход
#     (в `xrandr --listmonitors` на :N.0 и :N.1 будет по одному монитору с одним
#     и тем же именем `screen`);
#   * `xrandr --setmonitor` ручные мониторы создаёт, но клиент xrandr сразу
#     выходит, а X-сервер удаляет ручные мониторы вместе с клиентом, который их
#     создал. Проверено: пока клиент жив, `xrandr --listmonitors` печатает два
#     монитора, после выхода xrandr — снова один.
# Поэтому виртуальные мониторы создаёт маленький helper ниже (python3 + ctypes,
# XRRSetMonitor из libXrandr) и живёт весь тест: мониторы настоящие, их видит и
# `xrandr`, и GDK, и muffin. Никакой подмены вывода xrandr в тесте нет.
#
# Два нюанса, найденные на практике:
#   * выход Xvfb (`screen`) приписан ВТОРОМУ монитору. Если отдать его первому,
#     GDK показывает только один монитор и раскладчик видит один экран;
#   * muffin (Mutter) при старте пересобирает конфигурацию мониторов (в Xvfb она
#     невалидна: у режима нет частоты), поэтому его поднимают ДО создания
#     мониторов: уже поднятый muffin горячую смену мониторов переживает и ручные
#     мониторы больше не трогает.
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 python3-gi
#   gir1.2-gtk-3.0; x11-xserver-utils (xrandr) — по желанию: с ним мониторы
#   зовутся по имени выхода (GS-LEFT, screen), без него — monitor-0, monitor-1.
# Код возврата: 0 — всё сошлось, 1 — расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

# Свой дисплей: :98 занят остальными дымовыми сценариями, они могут идти следом.
DISP=":97"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"

# Раскладка экрана: два монитора по половине.
SCREEN_W=1920
SCREEN_H=1080
MON_W=960
MON_H=1080
MON2_X=960

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-monitors.$$)
CONF="$LOGDIR/zones.conf"
MONLIST="$LOGDIR/monitors.txt"
HELPER="$ROOT/tests/two_monitors.py"
MUFFIN_LOG="$LOGDIR/gridsnap-monitors-muffin.log"
HELPER_LOG="$LOGDIR/gridsnap-monitors-helper.log"

# Своё состояние (история apply/undo) и своя конфигурация — не владельца.
export XDG_STATE_HOME="$LOGDIR/state"
export XDG_CONFIG_HOME="$LOGDIR/config"
mkdir -p "$LOGDIR/state" "$LOGDIR/config"

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

XPID=""; MPID=""; HPID=""; WPIDS=""
cleanup() {
    for p in $WPIDS; do kill "$p" 2>/dev/null; done
    [ -n "$HPID" ] && kill "$HPID" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- X и muffin

Xvfb "$DISP" -screen 0 "${SCREEN_W}x${SCREEN_H}x24" >/dev/null 2>&1 &
XPID=$!
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят)"
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

# ------------------------------------------------- виртуальные два монитора

# Helper держит соединение: пока он жив, у X-сервера два монитора
# (`GS-LEFT` без выхода и `GS-RIGHT` с выходом Xvfb). Печатает свои мониторы в
# файл, чтобы сценарий не разбирал вывод xrandr регулярками.

python3 "$HELPER" "$MONLIST" "$MON_W" "$MON_H" "GS-LEFT:0" "GS-RIGHT:$MON2_X" \
    >"$HELPER_LOG" 2>&1 &
HPID=$!

if ! wait_for 30 test -s "$MONLIST"; then
    say "не удалось создать два виртуальных монитора. Вывод helper:"
    sed 's/^/      /' "$HELPER_LOG" 2>/dev/null | tail -25
    exit 1
fi

if command -v xrandr >/dev/null 2>&1; then
    say "xrandr --listmonitors:"
    xrandr --listmonitors | sed 's/^/      /'
    XR_COUNT=$(xrandr --listmonitors | grep -c '^[[:space:]]*[0-9][0-9]*:')
    if [ "$XR_COUNT" -eq 2 ]; then
        ok "xrandr видит два монитора"
    else
        bad "xrandr видит $XR_COUNT мониторов вместо двух"
    fi
else
    # Без xrandr (в CI smoke-job он не ставится) раскладчик зовёт мониторы
    # monitor-0/monitor-1 — проверки ниже работают и так: имена берутся у
    # самого раскладчика, а не у xrandr.
    say "xrandr не установлен: имена будут monitor-<номер>, как у раскладчика без xrandr"
fi

# Имена мониторов берём у самого раскладчика: GridSnap называет монитор по
# имени выхода (xrandr печатает его последним полем строки), поэтому у Xvfb,
# где выход один и зовётся `screen`, второй монитор для раскладчика — `screen`.
# Так тест адресует мониторы ровно теми именами, которые видит GridSnap.
VIEW=$(python3 - "$TILER" <<'PY'
import importlib.util, os, sys

tiler = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(tiler))
spec = importlib.util.spec_from_file_location("zone_tiler_smoke_monitors", tiler)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, area in module.monitor_outputs():
    print("%s %d %d %d %d" % ((name,) + tuple(area)))
PY
)
RC=$?
say "мониторы глазами раскладчика (zone-tiler.py:monitor_outputs):"
printf '%s\n' "$VIEW" | sed 's/^/      /'
if [ "$RC" -ne 0 ]; then
    say "не удалось получить мониторы от раскладчика"
    exit 1
fi
MON_COUNT=$(printf '%s\n' "$VIEW" | grep -c .)
if [ "$MON_COUNT" -ne 2 ]; then
    bad "раскладчик видит $MON_COUNT мониторов вместо двух"
    say "Дальше проверять нечего: зоны по мониторам не разделить."
    exit 1
fi
MON1=$(printf '%s\n' "$VIEW" | awk 'NR == 1 {print $1}')
MON2=$(printf '%s\n' "$VIEW" | awk 'NR == 2 {print $1}')
if [ "$(printf '%s\n' "$VIEW" | awk 'NR == 1 {print $2, $3, $4, $5}')" = "0 0 $MON_W $MON_H" ]; then
    ok "первый монитор $MON1: 0,0 ${MON_W}x${MON_H}"
else
    bad "первый монитор $MON1 не там: $(printf '%s\n' "$VIEW" | awk 'NR == 1 {print $2, $3, $4, $5}')"
fi
if [ "$(printf '%s\n' "$VIEW" | awk 'NR == 2 {print $2, $3, $4, $5}')" = "$MON2_X 0 $MON_W $MON_H" ]; then
    ok "второй монитор $MON2: ${MON2_X},0 ${MON_W}x${MON_H}"
else
    bad "второй монитор $MON2 не там: $(printf '%s\n' "$VIEW" | awk 'NR == 2 {print $2, $3, $4, $5}')"
fi

# ------------------------------------------------------------- zones.conf

# Набор [zones] (общий) и [zones:<второй выход>] намеренно разные: зоны общего
# набора — половины монитора, у второго монитора — свои верх/низ, поэтому по
# геометрии окна видно, чей набор применился.
cat >"$CONF" <<EOF
# Временный конфиг дымового теста на двух мониторах.
[grid]
cols = 1.0
rows = 1.0

[zones]
left = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[zones:$MON2]
top = 0, 0, 1, 0.5
bottom = 0, 0.5, 1, 0.5
EOF

# ------------------------------------------------------------------ проверки

# Ожидаемые рамки (x y w h) — считаются здесь, а не берутся из вывода команды,
# чтобы тест проверял раскладчик, а не сам себя.
EXP_TOP="$MON2_X 0 $MON_W $((MON_H / 2))"
EXP_BOTTOM="$MON2_X $((MON_H / 2)) $MON_W $((MON_H / 2))"

say ""
say "list --monitor $MON2 (второй монитор, свой набор зон)"
OUT=$(python3 "$TILER" list --monitor "$MON2" --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -ne 0 ]; then
    bad "list --monitor $MON2 завершился с кодом $RC"
else
    if printf '%s\n' "$OUT" | grep -q "рабочая область монитора 1: ${MON2_X},0 ${MON_W}x${MON_H}"; then
        ok "рабочая область второго монитора: ${MON2_X},0 ${MON_W}x${MON_H}"
    else
        bad "рабочая область второго монитора не та"
    fi
    if [ "$MON2" = "monitor-1" ]; then
        ok "второй монитор назван запасным именем monitor-1 (без xrandr выхода не узнать)"
    elif printf '%s\n' "$OUT" | grep -q "выход $MON2"; then
        ok "list назвал выход $MON2"
    else
        bad "в выводе list нет «выход $MON2»"
    fi
    if printf '%s\n' "$OUT" | grep -q '^  top ' && printf '%s\n' "$OUT" | grep -q '^  bottom '; then
        ok "list показал набор зон второго монитора (top, bottom)"
    else
        bad "list не показал зоны top/bottom второго монитора"
    fi
    if printf '%s\n' "$OUT" | grep -qF "top" && printf '%s\n' "$OUT" | awk '
        $1 == "top" {print $2, $3, $4}' | grep -qx "x=$MON2_X y=0 ${MON_W}x$((MON_H / 2))"; then
        ok "зона top второго монитора: x=$MON2_X y=0 ${MON_W}x$((MON_H / 2))"
    else
        bad "геометрия зоны top второго монитора не та"
    fi
fi

say ""
say "list --monitor $MON1 (первый монитор, общий набор [zones])"
OUT=$(python3 "$TILER" list --monitor "$MON1" --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -ne 0 ]; then
    bad "list --monitor $MON1 завершился с кодом $RC"
elif printf '%s\n' "$OUT" | grep -q '^  left ' && printf '%s\n' "$OUT" | grep -q '^  right '; then
    ok "list показал общий набор зон (left, right) на первом мониторе"
else
    bad "list не показал общий набор зон на первом мониторе"
fi
if printf '%s\n' "$OUT" | grep -q '^  top '; then
    bad "на первом мониторе видна зона top из набора второго монитора"
else
    ok "наборы мониторов не пересекаются: top есть только у $MON2"
fi

say ""
say "list --monitor 1 (номер вместо имени)"
BYNUM=$(python3 "$TILER" list --monitor 1 --conf "$CONF" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "list --monitor 1 завершился с кодом $RC"
    printf '%s\n' "$BYNUM" | sed 's/^/      /'
elif [ "$BYNUM" = "$(python3 "$TILER" list --monitor "$MON2" --conf "$CONF" 2>&1)" ]; then
    ok "list --monitor 1 совпал с list --monitor $MON2"
else
    bad "list --monitor 1 не совпал с list --monitor $MON2"
    printf '%s\n' "$BYNUM" | sed 's/^/      /'
fi

# Рамка окна как «x y w h»: xwininfo даёт клиента, _NET_FRAME_EXTENTS — рамку
# оконного менеджера, поэтому пересчитываем в рамку так же, как это делает
# zone-tiler.py (read_frame).
frame_of() {
    _win=$1
    _info=$(xwininfo -id "$_win" 2>/dev/null)
    _ext=$(xprop -id "$_win" _NET_FRAME_EXTENTS 2>/dev/null)
    printf '%s\n' "$_info" | awk -v ext="$_ext" '
        /Absolute upper-left X:/ {x=$NF}
        /Absolute upper-left Y:/ {y=$NF}
        /^  Width:/  {w=$NF}
        /^  Height:/ {h=$NF}
        END {
            sub(/.*= */, "", ext)
            gsub(/[^0-9,]/, "", ext)
            n = split(ext, e, ",")
            left = (n >= 1 && e[1] != "") ? e[1] : 0
            right = (n >= 2 && e[2] != "") ? e[2] : 0
            top = (n >= 3 && e[3] != "") ? e[3] : 0
            bottom = (n >= 4 && e[4] != "") ? e[4] : 0
            print x - left, y - top, w + left + right, h + top + bottom
        }'
}

say ""
say "открываю окно и раскладываю его в зону второго монитора"
xmessage -title "gridsnap-mon" -buttons OK:0 "окно дымового теста" >/dev/null 2>&1 &
WPIDS="$WPIDS $!"

WIN=""
_left=40
while [ "$_left" -gt 0 ]; do
    WIN=$(xdotool search --name '^gridsnap-mon$' 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WIN" ]; then
    say "не удалось открыть тестовое окно"
    exit 1
fi

OUT=$(python3 "$TILER" apply top --monitor "$MON2" --window "$WIN" --verify --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -ne 0 ]; then
    bad "apply top --monitor $MON2 завершился с кодом $RC"
else
    if printf '%s\n' "$OUT" | grep -q 'совпало с зоной'; then
        ok "apply: рамка окна совпала с зоной top второго монитора"
    else
        bad "apply: рамка окна не совпала с зоной"
    fi
    NOW=$(frame_of "$WIN")
    if [ "$NOW" = "$EXP_TOP" ]; then
        ok "рамка по факту: x=$MON2_X y=0 ${MON_W}x$((MON_H / 2))"
    else
        bad "рамка по факту: $NOW вместо $EXP_TOP"
    fi
fi

say ""
say "то же по номеру монитора: apply bottom --monitor 1"
OUT=$(python3 "$TILER" apply bottom --monitor 1 --window "$WIN" --verify --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -ne 0 ]; then
    bad "apply bottom --monitor 1 завершился с кодом $RC"
else
    if printf '%s\n' "$OUT" | grep -q 'совпало с зоной'; then
        ok "apply: рамка окна совпала с зоной bottom второго монитора"
    else
        bad "apply: рамка окна не совпала с зоной"
    fi
    NOW=$(frame_of "$WIN")
    if [ "$NOW" = "$EXP_BOTTOM" ]; then
        ok "рамка по факту: x=$MON2_X y=$((MON_H / 2)) ${MON_W}x$((MON_H / 2))"
    else
        bad "рамка по факту: $NOW вместо $EXP_BOTTOM"
    fi
fi

say ""
say "зона второго монитора на первом мониторе: apply top --monitor $MON1"
OUT=$(python3 "$TILER" apply top --monitor "$MON1" --window "$WIN" --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -eq 3 ]; then
    ok "apply top на первом мониторе отказал с кодом 3 (зоны нет в его наборе)"
elif [ "$RC" -eq 0 ]; then
    bad "apply top на первом мониторе прошёл, хотя зона есть только у $MON2"
else
    bad "apply top на первом мониторе: код $RC вместо 3"
fi

say ""
say "неизвестное имя монитора"
OUT=$(python3 "$TILER" list --monitor HDMI-9 --conf "$CONF" 2>&1)
RC=$?
printf '%s\n' "$OUT" | sed 's/^/      /'
if [ "$RC" -ne 2 ]; then
    bad "list --monitor HDMI-9: код $RC вместо 2"
elif printf '%s\n' "$OUT" | grep -q "монитора 'HDMI-9' нет"; then
    if printf '%s\n' "$OUT" | grep -q "$MON1" && printf '%s\n' "$OUT" | grep -q "$MON2"; then
        ok "неизвестное имя: код 2 и перечень доступных мониторов"
    else
        bad "в сообщении нет перечня доступных мониторов"
    fi
else
    bad "нет понятного сообщения «монитора 'HDMI-9' нет»"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест двух мониторов пройден."
else
    say "Провалов: $fails"
    exit 1
fi

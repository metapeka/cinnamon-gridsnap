#!/bin/sh
# Дымовой тест действий: команда `action` и встроенные действия на живых окнах.
#
# Поднимает СВОЙ X-сервер (Xvfb :92) и muffin, пишет свой zones.conf во
# временном каталоге и проверяет:
#
#   1. внешняя команда действия получает окно, зону и пиксельную рамку зоны —
#      и переменными GRIDSNAP_*, и подстановками {zone}/{w}/{h} в аргументах;
#   2. builtin:terminal запускает терминал и кладёт его окно в зону (при
#      занятой зоне — на рамку вкладки);
#   3. builtin:show-window на зоне с двумя окнами поднимает соседнее;
#   4. builtin:mirror меняет окна двух зон местами;
#   5. builtin:minimize-others сворачивает чужое окно, повторное нажатие
#      возвращает его.
#
# Своё состояние и свой runtime-каталог — во временном каталоге: `focus.json` и
# сокет владельца не затрагиваются. Демон для действий не нужен:
# `minimize-others` без него работает разово, как `focus`.
#
#   tests/smoke-actions.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool xmessage python3
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:92}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"

# Высота полосы в этом сценарии не умолчание (22), а своё число: иначе тест не
# отличил бы чтение `[tabs] height` от константы в коде.
TABS_H=30

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-actions.$$)
CONF="$LOGDIR/zones.conf"
PROBE="$LOGDIR/probe.out"
MUFFIN_LOG="$LOGDIR/gridsnap-actions-muffin.log"

STATE="$LOGDIR/state"
RUNTIME="$LOGDIR/run"
mkdir -p "$STATE" "$RUNTIME"
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

for c in Xvfb muffin xmessage xdotool python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool xmessage python3"
        exit 2
    fi
done

XPID=""; MPID=""; WA=""; WB=""; WC=""; WD=""; WE=""
cleanup() {
    for _p in "$WA" "$WB" "$WC" "$WD" "$WE" "$MPID" "$XPID"; do
        [ -n "$_p" ] && kill "$_p" 2>/dev/null
    done
    sleep 1
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

# Фактическая рамка окна с компенсацией _NET_FRAME_EXTENTS — как её читает
# раскладчик, чтобы тест не дублировал геометрию сам.
frame_of() {  # $1 = окно -> «x y w h»
    python3 - "$TILER" "$1" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_actions", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

# Ожидаемая рамка: зона или рамка вкладки по числу окон в ней. Считается тем же
# ядром, что у раскладчика: тест не повторяет арифметику полосы сам.
expected_frame() {  # $1 = зона, $2 = число окон -> «x y w h»
    python3 - "$TILER" "$CONF" "$ROOT" "$1" "$2" "$TABS_H" <<'PY'
import importlib.util
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load("zt_smoke_actions", sys.argv[1])
ztabs = load("ztabs_smoke_actions", sys.argv[3] + "/zone_tabs.py")
grid = zt.Grid(sys.argv[2])
rect = grid.rect(sys.argv[4], zt.monitors()[0])
print(*[int(round(v)) for v in ztabs.stack_rect(rect, int(sys.argv[5]), int(sys.argv[6]))])
PY
}

frame_is() {  # молча: рамка окна равна ожидаемой
    [ -n "${1:-}" ] && [ "$(frame_of "$1")" = "$(expected_frame "$2" "$3")" ]
}

frame_report() {  # $1 = окно, $2 = зона, $3 = число окон
    printf 'факт %s, ожидалось %s (%s, окон %s)' \
        "$(frame_of "$1")" "$(expected_frame "$2" "$3")" "$2" "$3"
}

# Действие так, как его зовёт клавиша: отдельным процессом раскладчика.
run_action() {  # $1 = имя действия; остальное — флаги
    _name=$1; shift
    python3 "$TILER" action "$_name" "$@" --conf "$CONF"
}

# Идентификатор окна по заголовку (xmessage ставит его в _NET_WM_NAME).
window_of() {  # $1 = заголовок
    xdotool search --name "^$1\$" 2>/dev/null | head -1
}

# Новое окно xmessage: запустить и дождаться его идентификатора. Результат —
# в `_pid` и `_win`: функция не может вернуть два значения, а вызывать её через
# `$(...)` нельзя — присваивания остались бы в подоболочке.
start_window() {  # $1 = заголовок
    DISPLAY="$DISP" xmessage -title "$1" -buttons OK:0 "$1" >/dev/null 2>&1 &
    _pid=$!
    wait_for 60 sh -c "xdotool search --name '^$1\$' >/dev/null 2>&1"
    _win=$(window_of "$1")
}

# Состояние окна по ICCCM, а не через проверяемый код: Normal|Iconic|?
wm_state() {  # $1 = окно
    xprop -id "$1" WM_STATE 2>/dev/null \
        | sed -n 's/.*window state: *\([A-Za-z]*\).*/\1/p' | head -1
}

active_window() {  # активное окно в десятичном виде (xdotool отдаёт десятичное)
    xdotool getactivewindow 2>/dev/null
}

active_is() { [ -n "${1:-}" ] && [ "$(active_window)" = "$1" ]; }
state_is() { [ "$(wm_state "$1")" = "$2" ]; }

# Прежние окна ушли из `_NET_CLIENT_LIST`: иначе они попадут в чужие зоны.
old_windows_gone() {
    for _t in gridsnap-actions-a gridsnap-actions-b gridsnap-actions-c \
            gridsnap-actions-term; do
        [ -z "$(window_of "$_t")" ] || return 1
    done
    return 0
}

summary() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

cat >"$CONF" <<EOF
# Конфиг дымового теста действий.
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[tabs]
height = $TABS_H

# Внешняя команда: первая строка — из GRIDSNAP_*, вторая — из подстановок.
# Строки сравниваются между собой: так видно, что оба канала раздают одно и то же.
[action:probe]
run = /bin/sh -c 'printf "%s %s %s %s %s %s\n{window} {zone} {x} {y} {w} {h}\n" "\$GRIDSNAP_WINDOW" "\$GRIDSNAP_ZONE" "\$GRIDSNAP_X" "\$GRIDSNAP_Y" "\$GRIDSNAP_W" "\$GRIDSNAP_H" > $PROBE'
zone = main

[action:term]
run = builtin:terminal
command = sh -c "exec xmessage -name gridsnap-actions-term -title gridsnap-actions-term -buttons OK:0 терминал"
zone = main

[action:show]
run = builtin:show-window
zone = right

[action:mir]
run = builtin:mirror
zone = main

[action:min]
run = builtin:minimize-others
zone = main
EOF

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
    say "у muffin нет _NET_WORKAREA — раскладчик не посчитает зоны"
    exit 1
fi

export DISPLAY="$DISP"

say "дисплей $DISP, высота полосы $TABS_H"

# --- 1. внешняя команда действия ---------------------------------------
say ""
say "1. Внешняя команда получает окно, зону и рамку зоны"
start_window "gridsnap-actions-a"
WA=$_pid
WAID=$_win
if [ -z "$WAID" ]; then
    bad "окно gridsnap-actions-a не появилось"
else
    python3 "$TILER" apply main --window "$WAID" --quiet --conf "$CONF" >"$LOGDIR/apply-a.log" 2>&1
    wait_for 60 frame_is "$WAID" main 1
    xdotool windowactivate --sync "$WAID" >/dev/null 2>&1
    wait_for 20 active_is "$WAID"
    run_action probe >"$LOGDIR/action-probe.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && wait_for 20 test -s "$PROBE"; then
        ENV_LINE=$(sed -n 1p "$PROBE")
        SUB_LINE=$(sed -n 2p "$PROBE")
        WANT_LINE="$(printf '0x%x' "$WAID") main $(expected_frame main 1)"
        if [ "$ENV_LINE" = "$WANT_LINE" ] && [ "$SUB_LINE" = "$WANT_LINE" ]; then
            ok "оба канала дали одно: «$ENV_LINE»"
        else
            bad "в файле «$ENV_LINE» / «$SUB_LINE», ожидалось «$WANT_LINE» дважды"
        fi
    else
        bad "действие probe не отработало: код $RC, $(summary "$LOGDIR/action-probe.log")"
    fi
fi

# --- 2. терминал в зоне -------------------------------------------------
say ""
say "2. builtin:terminal ставит окно терминала в зону (при занятой — на рамку вкладки)"
# В зоне main уже стоит окно A: новое окно терминала обязано встать на рамку
# вкладки (зона минус полоса), иначе оно легло бы поверх вкладки.
run_action term >"$LOGDIR/action-term.log" 2>&1
RC=$?
wait_for 40 sh -c "xdotool search --name '^gridsnap-actions-term\$' >/dev/null 2>&1"
WTID=$(window_of gridsnap-actions-term)
if [ "$RC" -eq 0 ] && [ -n "$WTID" ] && wait_for 20 frame_is "$WTID" main 2; then
    ok "окно терминала на рамке вкладки: $(frame_report "$WTID" main 2)"
else
    bad "терминал не встал в зону: код $RC, окно ${WTID:-нет}, $(frame_report "${WTID:-0}" main 2); $(summary "$LOGDIR/action-term.log")"
fi

# --- 3. show-window -----------------------------------------------------
say ""
say "3. builtin:show-window поднимает соседнее окно зоны"
start_window "gridsnap-actions-b"
WB=$_pid
WBID=$_win
start_window "gridsnap-actions-c"
WC=$_pid
WCID=$_win
if [ -z "$WBID" ] || [ -z "$WCID" ]; then
    bad "окна gridsnap-actions-b/-c не появились"
else
    # Оба окна — в правую зону; рамки у них одинаковые, поэтому зона видит оба.
    python3 "$TILER" apply right --window "$WBID" --quiet --conf "$CONF" >"$LOGDIR/apply-b.log" 2>&1
    python3 "$TILER" apply right --window "$WCID" --quiet --conf "$CONF" >"$LOGDIR/apply-c.log" 2>&1
    xdotool windowactivate --sync "$WBID" >/dev/null 2>&1
    wait_for 20 active_is "$WBID"
    run_action show >"$LOGDIR/action-show.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && wait_for 20 active_is "$WCID"; then
        ok "активное окно сменилось: $WBID -> $(active_window)"
    else
        bad "show-window не поднял соседа: код $RC, активно $(active_window), ожидалось $WCID; $(summary "$LOGDIR/action-show.log")"
    fi
fi

# --- 4. mirror ----------------------------------------------------------
say ""
say "4. builtin:mirror меняет окна двух зон местами"
# Прежние окна убираем: зеркало обменивает окна зон, и чужие вкладки в них
# только запутали бы ожидаемую рамку. Терминал запущен самим действием, его pid
# у нас нет — закрываем окно через X.
[ -n "$WTID" ] && xdotool windowkill "$WTID" 2>/dev/null
kill "$WA" "$WB" "$WC" 2>/dev/null
WA=""; WB=""; WC=""
wait_for 40 old_windows_gone
start_window "gridsnap-actions-d"
WD=$_pid
WDID=$_win
start_window "gridsnap-actions-e"
WE=$_pid
WEID=$_win
if [ -z "$WDID" ] || [ -z "$WEID" ]; then
    bad "окна gridsnap-actions-d/-e не появились"
else
    python3 "$TILER" apply main --window "$WDID" --quiet --conf "$CONF" >"$LOGDIR/apply-d.log" 2>&1
    python3 "$TILER" apply right --window "$WEID" --quiet --conf "$CONF" >"$LOGDIR/apply-e.log" 2>&1
    xdotool windowactivate --sync "$WDID" >/dev/null 2>&1
    run_action mir >"$LOGDIR/action-mirror.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && wait_for 20 frame_is "$WDID" right 1 && frame_is "$WEID" main 1; then
        ok "окна обменялись: D в right ($(frame_report "$WDID" right 1)), E в main ($(frame_report "$WEID" main 1))"
    else
        bad "обмена не было: код $RC, D — $(frame_report "${WDID:-0}" right 1), E — $(frame_report "${WEID:-0}" main 1); $(summary "$LOGDIR/action-mirror.log")"
    fi

    # --- 5. minimize-others --------------------------------------------
    say ""
    say "5. builtin:minimize-others сворачивает чужое окно, повторно — возвращает"
    xdotool windowactivate --sync "$WEID" >/dev/null 2>&1
    wait_for 20 active_is "$WEID"
    run_action min >"$LOGDIR/action-min.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && wait_for 20 state_is "$WDID" Iconic; then
        ok "чужое окно свёрнуто: $WDID=$(wm_state "$WDID")"
    else
        bad "чужое окно не свёрнуто: код $RC, $WDID=$(wm_state "$WDID"); $(summary "$LOGDIR/action-min.log")"
    fi
    run_action min >"$LOGDIR/action-min-off.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && wait_for 20 state_is "$WDID" Normal; then
        ok "повторное нажатие вернуло окно: $WDID=$(wm_state "$WDID")"
    else
        bad "повторное нажатие не вернуло окно: код $RC, $WDID=$(wm_state "$WDID"); $(summary "$LOGDIR/action-min-off.log")"
    fi
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест действий пройден."
    exit 0
fi
say "Дымовой тест действий: расхождений $fails."
exit 1

#!/bin/sh
# Дымовой тест режима «только эта зона».
#
# Поднимает СВОЙ X-сервер (Xvfb :98), muffin и четыре окна xmessage — цель
# (`gridsnap-focus-a`), два «остальных» (`-b`, `-c`) и исключённое
# (`gridsnap-focus-ign` под `[ignore]`) — и проверяет:
#
#   1. разовый `zone-tiler.py focus main` без демона: цель в зоне, остальные
#      окна того же стола и монитора свёрнуты, исключённое — нет;
#   2. `focus --off`: свёрнутое режимом развёрнуто, файл состояния убран,
#      цель стоит там же, где её положили;
#   3. toggle: повторный `focus main` выходит из режима сам;
#   4. демон (IPC): `gridsnap-ctl focus` сворачивает остальных, новое окно при
#      активном режиме тоже сворачивается, `gridsnap-ctl focus --off` возвращает всё.
#
# Своё состояние и свой runtime-каталог — во временном каталоге: сокет демона
# не должен совпасть с сокетом демона владельца, а `focus.json` — попасть в его
# домашний каталог. Рабочий стол, окна и настройки владельца не затрагиваются.
#
#   tests/smoke-focus.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 python3-gi
#   gir1.2-gtk-3.0 python3-cairo
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:98}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
DAEMON="$ROOT/zone-daemon.py"
CTL="$ROOT/gridsnap-ctl"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-focus.$$)
CONF="$LOGDIR/zones.conf"
MUFFIN_LOG="$LOGDIR/gridsnap-focus-muffin.log"
DAEMON_LOG="$LOGDIR/gridsnap-focus-daemon.log"

# Состояние режима и сокет — в своём каталоге, а не у владельца.
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
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool python3"
        exit 2
    fi
done

XPID=""; MPID=""; DPID=""
WA=""; WB=""; WC=""; WI=""; WN=""
cleanup() {
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$WA" ] && kill "$WA" 2>/dev/null
    [ -n "$WB" ] && kill "$WB" 2>/dev/null
    [ -n "$WC" ] && kill "$WC" 2>/dev/null
    [ -n "$WI" ] && kill "$WI" 2>/dev/null
    [ -n "$WN" ] && kill "$WN" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
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

spec = importlib.util.spec_from_file_location("zt_smoke_focus", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

# Окно в зоне? Печатает «факт …, ожидалось …» и возвращает 1 при расхождении.
zone_frame_ok() {  # $1 = окно, $2 = зона
    python3 - "$TILER" "$CONF" "$1" "$2" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_focus", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win, zone = int(sys.argv[3], 0), sys.argv[4]
grid = zt.Grid(sys.argv[2])
actual = zt.read_frame(win, zt.frame_extents(win))
expected = grid.rect(zone, zt.monitors()[0])
print(f"факт {actual}, ожидалось {expected} ({zone})")
sys.exit(0 if actual == expected else 1)
PY
}

# Состояние окна по ICCCM, а не через проверяемый код: Normal|Iconic|?
wm_state() {  # $1 = окно
    xprop -id "$1" WM_STATE 2>/dev/null \
        | sed -n 's/.*window state: *\([A-Za-z]*\).*/\1/p' | head -1
}

# Оба окна свёрнуты? Печатает состояния и возвращает 1, если нет.
both_iconic() {  # $1, $2 = окна
    sa=$(wm_state "$1"); sb=$(wm_state "$2")
    printf '    %s=%s %s=%s\n' "$1" "$sa" "$2" "$sb"
    [ "$sa" = "Iconic" ] && [ "$sb" = "Iconic" ]
}

both_normal() {  # $1, $2 = окна
    sa=$(wm_state "$1"); sb=$(wm_state "$2")
    printf '    %s=%s %s=%s\n' "$1" "$sa" "$2" "$sb"
    [ "$sa" = "Normal" ] && [ "$sb" = "Normal" ]
}

summary() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

cat >"$CONF" <<'EOF'
# Конфиг дымового теста режима «только эта зона».
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[ignore]
re:gridsnap-focus-ign$ = тестовое исключённое окно
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

DISPLAY="$DISP" xmessage -title "gridsnap-focus-a" -buttons OK:0 "цель" >/dev/null 2>&1 &
WA=$!
DISPLAY="$DISP" xmessage -title "gridsnap-focus-b" -buttons OK:0 "другое" >/dev/null 2>&1 &
WB=$!
DISPLAY="$DISP" xmessage -title "gridsnap-focus-c" -buttons OK:0 "третье" >/dev/null 2>&1 &
WC=$!
DISPLAY="$DISP" xmessage -title "gridsnap-focus-ign" -buttons OK:0 "исключённое" >/dev/null 2>&1 &
WI=$!

export DISPLAY="$DISP"

WA=""; WB=""; WC=""; WI=""
_left=40
while [ "$_left" -gt 0 ]; do
    WA=$(xdotool search --name '^gridsnap-focus-a$' 2>/dev/null | head -1)
    WB=$(xdotool search --name '^gridsnap-focus-b$' 2>/dev/null | head -1)
    WC=$(xdotool search --name '^gridsnap-focus-c$' 2>/dev/null | head -1)
    WI=$(xdotool search --name '^gridsnap-focus-ign$' 2>/dev/null | head -1)
    [ -n "$WA" ] && [ -n "$WB" ] && [ -n "$WC" ] && [ -n "$WI" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WA" ] || [ -z "$WB" ] || [ -z "$WC" ] || [ -z "$WI" ]; then
    say "не удалось открыть тестовые окна (цель ${WA:-нет}, остальные ${WB:-нет}/${WC:-нет}, исключённое ${WI:-нет})"
    exit 1
fi

say "дисплей $DISP, цель $WA, остальные $WB и $WC, исключённое $WI"

# --- 1. разовый вход без демона ----------------------------------------
say ""
say "1. zone-tiler.py focus main без демона"
python3 "$TILER" focus main --window "$WA" --conf "$CONF" --quiet >"$LOGDIR/focus-1.log" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "focus завершился с кодом $RC ($(summary "$LOGDIR/focus-1.log"))"
else
    if zone_frame_ok "$WA" main >"$LOGDIR/focus-1.frame" 2>&1; then
        ok "цель в зоне main: $(cat "$LOGDIR/focus-1.frame")"
    else
        bad "цель не в зоне main: $(cat "$LOGDIR/focus-1.frame" 2>/dev/null)"
    fi
    if both_iconic "$WB" "$WC" >"$LOGDIR/focus-1.state" 2>&1; then
        ok "остальные окна свёрнуты ($(tail -1 "$LOGDIR/focus-1.state"))"
    else
        bad "остальные окна не свёрнуты: $(tail -1 "$LOGDIR/focus-1.state")"
    fi
    if [ "$(wm_state "$WI")" = "Normal" ]; then
        ok "исключённое окно осталось видимым"
    else
        bad "исключённое окно свернулось: $(wm_state "$WI")"
    fi
    if [ -f "$STATE/gridsnap/focus.json" ]; then
        ok "состояние режима записано в focus.json"
    else
        bad "без демона состояние режима не записано ($STATE/gridsnap)"
    fi
fi

# --- 2. явный выход --off ----------------------------------------------
say ""
say "2. focus --off разворачивает только своё"
python3 "$TILER" focus --off --conf "$CONF" --quiet >"$LOGDIR/focus-2.log" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "focus --off завершился с кодом $RC ($(summary "$LOGDIR/focus-2.log"))"
else
    if both_normal "$WB" "$WC" >"$LOGDIR/focus-2.state" 2>&1; then
        ok "остальные окна развёрнуты ($(tail -1 "$LOGDIR/focus-2.state"))"
    else
        bad "остальные окна не развернулись: $(tail -1 "$LOGDIR/focus-2.state")"
    fi
    if [ ! -f "$STATE/gridsnap/focus.json" ]; then
        ok "состояние режима убрано"
    else
        bad "focus.json остался после выхода — повторный focus выйдет не туда"
    fi
    if zone_frame_ok "$WA" main >"$LOGDIR/focus-2.frame" 2>&1; then
        ok "выход не переставил цель: $(cat "$LOGDIR/focus-2.frame")"
    else
        bad "цель уехала после выхода: $(cat "$LOGDIR/focus-2.frame" 2>/dev/null)"
    fi
fi

# --- 3. toggle ---------------------------------------------------------
say ""
say "3. Повторный focus main — выход из режима"
BEFORE_B=$(frame_of "$WB")
python3 "$TILER" focus main --window "$WA" --conf "$CONF" --quiet >/dev/null 2>&1
# Свёрнутое окно должно сохранять геометрию: иначе память демона об укладках
# считала бы его вышедшим из зоны и запускала reflow на каждом входе в режим.
if [ "$(frame_of "$WB")" = "$BEFORE_B" ]; then
    ok "свёрнутое окно сохранило рамку ($BEFORE_B)"
else
    bad "рамка свёрнутого окна уехала: $BEFORE_B -> $(frame_of "$WB")"
fi
python3 "$TILER" focus main --window "$WA" --conf "$CONF" --quiet >"$LOGDIR/focus-3.log" 2>&1
RC=$?
if [ "$RC" -eq 0 ] && both_normal "$WB" "$WC" >"$LOGDIR/focus-3.state" 2>&1 \
        && [ ! -f "$STATE/gridsnap/focus.json" ]; then
    ok "toggle вышел из режима и развернул окна ($(tail -1 "$LOGDIR/focus-3.state"))"
else
    bad "toggle не сработал: код $RC, $(tail -1 "$LOGDIR/focus-3.state" 2>/dev/null), $(summary "$LOGDIR/focus-3.log")"
fi

# --- 3.1 смена рабочего стола ------------------------------------------
say ""
say "3.1 Режим переживает смену стола, а выход возвращает окна"
# Активацию окна не на активном столе muffin игнорирует (проверено разведкой),
# поэтому режим на своём столе НЕ снимается, а выход сам переводит на нужный стол.
HAS_DESKTOPS=0
NDESK=$(xprop -root _NET_NUMBER_OF_DESKTOPS 2>/dev/null | sed -n 's/.*= *//p')
case "$NDESK" in ''|*[!0-9]*) NDESK=0 ;; esac
if [ "$NDESK" -ge 2 ]; then
    HAS_DESKTOPS=1
fi
if [ "$HAS_DESKTOPS" -eq 1 ]; then
    python3 "$TILER" focus main --window "$WA" --conf "$CONF" --quiet >/dev/null 2>&1
    xdotool set_desktop 1
    sleep 1.5
    DESK=$(xprop -root _NET_CURRENT_DESKTOP 2>/dev/null | sed -n 's/.*= *//p')
    if both_iconic "$WB" "$WC" >/dev/null 2>&1; then
        ok "на другом столе ($DESK) свёрнутые окна остались свёрнутыми, режим жив"
    else
        bad "уход на стол $DESK развернул окна сам (или снял режим)"
    fi
    python3 "$TILER" focus --off --conf "$CONF" --quiet >"$LOGDIR/focus-31.log" 2>&1
    RC=$?
    DESK=$(xprop -root _NET_CURRENT_DESKTOP 2>/dev/null | sed -n 's/.*= *//p')
    if [ "$RC" -eq 0 ] && both_normal "$WB" "$WC" >"$LOGDIR/focus-31.state" 2>&1 \
            && [ "$DESK" = "0" ]; then
        ok "focus --off с другого стола вернул окна и перешёл на стол режима"
    else
        bad "focus --off с другого стола: код $RC, стол $DESK, $(tail -1 "$LOGDIR/focus-31.state" 2>/dev/null) ($(summary "$LOGDIR/focus-31.log"))"
    fi
    # Возвращаем активный стол для следующих секций, даже если проверка упала.
    xdotool set_desktop 0
    sleep 1
else
    ok "смена стола пропущена: в сессии меньше двух рабочих столов"
fi

# --- 4. демон: IPC focus и новые окна ----------------------------------
say ""
say "4. Демон: focus по сокету сворачивает и новое окно"
python3 "$DAEMON" --conf "$CONF" --verbose >"$DAEMON_LOG" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$RUNTIME/gridsnap/daemon.sock"; then
    bad "демон не поднял сокет за 20 с ($(summary "$DAEMON_LOG"))"
else
    python3 "$CTL" focus main --window "$WA" >"$LOGDIR/ctl-focus.log" 2>&1
    RC=$?
    if [ "$RC" -ne 0 ]; then
        bad "gridsnap-ctl focus завершился с кодом $RC ($(summary "$LOGDIR/ctl-focus.log"))"
    elif both_iconic "$WB" "$WC" >"$LOGDIR/ctl-focus.state" 2>&1; then
        ok "фокус-режим демона свернул остальные окна ($(tail -1 "$LOGDIR/ctl-focus.state"))"
        # Новое окно появляется уже при активном режиме — демон должен его свернуть.
        DISPLAY="$DISP" xmessage -title "gridsnap-focus-new" -buttons OK:0 "новое" >/dev/null 2>&1 &
        WN=$!
        _left=40
        while [ "$_left" -gt 0 ]; do
            WNID=$(xdotool search --name '^gridsnap-focus-new$' 2>/dev/null | head -1)
            [ -n "$WNID" ] && [ "$(wm_state "$WNID")" = "Iconic" ] && break
            sleep 0.5
            _left=$((_left - 1))
        done
        if [ -n "${WNID:-}" ] && [ "$(wm_state "$WNID")" = "Iconic" ]; then
            ok "новое окно при активном режиме свёрнуто демоном"
        else
            bad "новое окно не свёрнуто: ${WNID:-нет окна} $(wm_state "${WNID:-0}")"
        fi

        python3 "$CTL" focus --off >"$LOGDIR/ctl-off.log" 2>&1
        RC=$?
        if [ "$RC" -ne 0 ]; then
            bad "gridsnap-ctl focus --off завершился с кодом $RC ($(summary "$LOGDIR/ctl-off.log"))"
        elif both_normal "$WB" "$WC" >"$LOGDIR/ctl-off.state" 2>&1; then
            ok "выход из режима развернул остальные окна ($(tail -1 "$LOGDIR/ctl-off.state"))"
        else
            bad "выход из режима не развернул окна: $(tail -1 "$LOGDIR/ctl-off.state")"
        fi
        # Демон продолжает работать после выхода из режима.
        if python3 "$CTL" status >/dev/null 2>&1; then
            ok "демон жив после выхода из режима"
        else
            bad "демон не отвечает после выхода из режима"
        fi
    else
        bad "focus по сокету не свернул окна: $(tail -1 "$LOGDIR/ctl-focus.state" 2>/dev/null) ($(summary "$LOGDIR/ctl-focus.log"))"
    fi
    python3 "$CTL" quit >/dev/null 2>&1
    wait "$DPID" 2>/dev/null
    DPID=""
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест режима «только эта зона» пройден."
else
    say "Провалов: $fails"
    exit 1
fi

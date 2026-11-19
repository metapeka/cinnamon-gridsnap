#!/bin/sh
# Дымовой тест раскладчика GridSnap.
#
# Поднимает СВОЙ X-сервер (Xvfb :99) и свой оконный менеджер (muffin — тот же, что в Cinnamon),
# открывает тестовое окно и проверяет, что окно встаёт в зону пиксель в пиксель.
# Рабочий стол, текущие окна и настройки не затрагиваются.
#
#   tests/smoke.sh            # проверить все зоны из zones.conf
#   tests/smoke.sh main       # проверить только зону main
#   tests/smoke.sh main bottom
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3
# Код возврата: 0 — всё совпало, 1 — есть несовпадения, 2 — нет зависимостей.
#
# Готовности ждём опросом (X-сервер, WM, рабочая область, окно), а не фиксированными
# паузами: на медленных раннерах muffin не успевает стать WM за `sleep`, и раньше это
# выглядело как «в zones.conf не нашлось ни одной зоны». При сбое печатаем лог muffin.

set -u

DISP=":99"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
MUFFIN_LOG="$LOGDIR/gridsnap-smoke-muffin.log"

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
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool"
        exit 2
    fi
done

XPID=""; MPID=""; WPID=""
cleanup() {
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    # если что-то осталось от предыдущего прогона на этом же дисплее
    sleep 1
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

Xvfb "$DISP" -screen 0 1920x1080x24 >/dev/null 2>&1 &
XPID=$!
# Xvfb готов, когда на дисплее отвечает xprop (до ~20 с)
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят — закройте лишний Xvfb)"
    exit 1
fi

DISPLAY="$DISP" muffin >"$MUFFIN_LOG" 2>&1 &
MPID=$!
# muffin готов, когда объявил себя оконным менеджером (до ~30 с)
if ! wait_for 60 sh -c "DISPLAY='$DISP' xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id # 0x'"; then
    say "muffin не стал оконным менеджером за 30 с. Его вывод:"
    sed 's/^/      /' "$MUFFIN_LOG" 2>/dev/null | tail -25
    exit 1
fi
# рабочая область — то, из чего раскладчик считает пиксельные рамки
if ! wait_for 40 sh -c "DISPLAY='$DISP' xprop -root _NET_WORKAREA 2>/dev/null | grep -q '_NET_WORKAREA(CARDINAL)'"; then
    say "у muffin нет _NET_WORKAREA — раскладчик не сможет посчитать зоны. Вывод muffin:"
    sed 's/^/      /' "$MUFFIN_LOG" 2>/dev/null | tail -25
    exit 1
fi

DISPLAY="$DISP" xmessage -title "gridsnap-smoke" -buttons OK:0 "тестовое окно" >/dev/null 2>&1 &
WPID=$!

export DISPLAY="$DISP"

WIN=""
_left=40
while [ "$_left" -gt 0 ]; do
    WIN=$(xdotool search --name "^gridsnap-smoke$" 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WIN" ]; then
    say "не удалось открыть тестовое окно"
    exit 1
fi
xdotool windowactivate --sync "$WIN" 2>/dev/null

if [ "$#" -gt 0 ]; then
    LIST="$*"
else
    LIST=$(python3 "$TILER" list 2>/dev/null | awk '{print $1}' | grep -E '^[a-z][a-z0-9_-]*$')
fi
if [ -z "$LIST" ]; then
    say "не удалось получить список зон — вывод 'list' (stderr):"
    python3 "$TILER" list 2>&1 | sed 's/^/      /' | tail -15
    exit 1
fi

say "дисплей $DISP, тестовое окно $WIN"
say "проверяю зоны: $(printf '%s ' $LIST)"
for zone in $LIST; do
    out=$(python3 "$TILER" apply "$zone" --verify 2>&1)
    if printf '%s' "$out" | grep -q "совпало с зоной"; then
        ok "$(printf '%s' "$out" | tail -1 | sed 's/^рамка по факту: //')  [$zone]"
    else
        bad "зона $zone — не совпало"
        printf '%s\n' "$out" | sed 's/^/      /'
    fi
done

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест пройден."
else
    say "Провалов: $fails"
    exit 1
fi

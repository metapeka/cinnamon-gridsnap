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

set -u

DISP=":99"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"

fails=0
say() { printf '%s\n' "$1"; }
ok()  { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✖\033[0m %s\n' "$1"; fails=$((fails + 1)); }

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
    return 0
}
trap cleanup EXIT INT TERM

Xvfb "$DISP" -screen 0 1920x1080x24 >/dev/null 2>&1 &
XPID=$!
sleep 3
if ! kill -0 "$XPID" 2>/dev/null; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят — закройте лишний Xvfb)"
    exit 1
fi

DISPLAY="$DISP" muffin >/dev/null 2>&1 &
MPID=$!
sleep 4

DISPLAY="$DISP" xmessage -title "gridsnap-smoke" -buttons OK:0 "тестовое окно" >/dev/null 2>&1 &
WPID=$!
sleep 2

export DISPLAY="$DISP"

WIN=$(xdotool search --name "^gridsnap-smoke$" 2>/dev/null | head -1)
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
    say "в zones.conf не нашлось ни одной зоны"
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

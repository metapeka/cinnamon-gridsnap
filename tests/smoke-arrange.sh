#!/bin/sh
# Дымовой тест «раскладка одной командой + undo» GridSnap.
#
# Поднимает СВОЙ X-сервер (Xvfb :98) и muffin, открывает три окна xmessage,
# раскладывает их `arrange --by order --verify` и возвращает `undo`. Проверяет,
# что окна попали в зоны и что undo вернул исходные рамки. Рабочий стол, окна и
# настройки владельца не затрагиваются.
#
#   tests/smoke-arrange.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3.
# Код возврата: 0 — всё совпало, 1 — есть несовпадения, 2 — нет зависимостей.

set -u

export XDG_SESSION_TYPE=x11

# Интерфейс — на исходном языке: сценарий сверяет поведение по русским
# сообщениям CLI, а перевод (каталоги en/zh_CN) выбирается из локали машины.
GRIDSNAP_LANG=ru
export GRIDSNAP_LANG
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP=":98"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
CONF="$ROOT/zones.conf"

# Своё состояние (история undo) — во временном каталоге, не в домашнем.
STATE=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-state.$$)
export XDG_STATE_HOME="$STATE"
# Не подхватить конфиг владельца из XDG.
unset XDG_CONFIG_HOME 2>/dev/null || true

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
MUFFIN_LOG="$LOGDIR/gridsnap-smoke-arrange-muffin.log"

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
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool"
        exit 2
    fi
done

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

Xvfb "$DISP" -screen 0 1920x1080x24 >/dev/null 2>&1 &
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

# Три окна с разными заголовками, чтобы их можно было найти и различить.
for name in a b c; do
    xmessage -title "gridsnap-$name" -buttons OK:0 "окно $name" >/dev/null 2>&1 &
    WPIDS="$WPIDS $!"
done

WINS=""
_left=40
while [ "$_left" -gt 0 ]; do
    WINS=$(xdotool search --name '^gridsnap-[abc]$' 2>/dev/null | sort -u)
    set -- $WINS
    [ "$#" -eq 3 ] && break
    sleep 0.5
    _left=$((_left - 1))
done
set -- $WINS
if [ "$#" -ne 3 ]; then
    say "не удалось открыть три тестовых окна (нашли: ${*:-нет})"
    exit 1
fi
WINS=$(printf '%s\n' ${WINS} | sort)
say "дисплей $DISP, окна: $(printf '%s ' $WINS)"

# Рамка окна как «x y w h» из xwininfo (абсолютная геометрия клиента).
frame_of() {
    xwininfo -id "$1" 2>/dev/null | awk '
        /Absolute upper-left X:/ {x=$NF}
        /Absolute upper-left Y:/ {y=$NF}
        /^  Width:/  {w=$NF}
        /^  Height:/ {h=$NF}
        END {print x, y, w, h}'
}

BEFORE=""
for win in $WINS; do
    BEFORE="$BEFORE$win $(frame_of "$win")
"
done

say "раскладываю: python3 zone-tiler.py arrange --by order --verify"
OUT=$(python3 "$TILER" arrange --by order --verify --conf "$CONF" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "arrange завершился с кодом $RC"
    printf '%s\n' "$OUT" | sed 's/^/      /'
else
    if printf '%s' "$OUT" | grep -q "не совпало"; then
        bad "arrange: хотя бы одно окно не совпало с зоной"
        printf '%s\n' "$OUT" | sed 's/^/      /'
    else
        ok "все окна разложены по зонам (рамки совпали)"
    fi
fi

# Окна должны были переехать: сверяем с исходными рамками.
MOVED=0
for win in $WINS; do
    now=$(frame_of "$win")
    was=$(printf '%s\n' "$BEFORE" | awk -v w="$win" '$1==w {print $2, $3, $4, $5}')
    [ "$now" != "$was" ] && MOVED=$((MOVED + 1))
done
if [ "$MOVED" -eq 3 ]; then
    ok "все три окна сменили рамку"
else
    bad "сменили рамку только $MOVED из 3 окон"
fi

say "возвращаю: python3 zone-tiler.py undo"
OUT=$(python3 "$TILER" undo --conf "$CONF" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "undo завершился с кодом $RC"
    printf '%s\n' "$OUT" | sed 's/^/      /'
fi

RESTORED=0
for win in $WINS; do
    now=$(frame_of "$win")
    was=$(printf '%s\n' "$BEFORE" | awk -v w="$win" '$1==w {print $2, $3, $4, $5}')
    if [ "$now" = "$was" ]; then
        RESTORED=$((RESTORED + 1))
    else
        bad "окно $win: рамка $now вместо $was"
    fi
done
if [ "$RESTORED" -eq 3 ]; then
    ok "undo вернул все три окна на прежние рамки"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест arrange+undo пройден."
else
    say "Провалов: $fails"
    exit 1
fi

#!/bin/sh
# Дымовой тест оверлея визуального выбора зоны (zone-picker.py).
#
# Поднимает СВОЙ X-сервер (Xvfb :98) и свой оконный менеджер (muffin — тот же,
# что в Cinnamon), открывает тестовое окно, запускает zone-picker.py с явным
# окном и монитором, посылает стрелку и Enter через xdotool и проверяет, что
# окно встало в выбранную зону пиксель в пиксель. Затем проверяет, что Esc
# отменяет выбор и окно остаётся на месте.
#
#   tests/smoke-picker.sh
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

DISP=":98"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
PICKER="$ROOT/zone-picker.py"
CONF="$ROOT/zones.conf"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
MUFFIN_LOG="$LOGDIR/gridsnap-picker-muffin.log"
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

XPID=""; MPID=""; WPID=""; PICKPID=""
cleanup() {
    [ -n "$PICKPID" ] && kill "$PICKPID" 2>/dev/null
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

# Окно точно в зоне? Печатает «факт … ожидалось …» и возвращает 1 при расхождении.
# Обе рамки считает сам zone-tiler: пиксельная зона — Grid.rect, факт — read_frame
# с компенсацией _NET_FRAME_EXTENTS. Так тест не дублирует геометрию.
check_frame() {  # $1 = окно, $2 = зона
    python3 - "$TILER" "$CONF" "$1" "$2" <<'PY'
import importlib.util
import sys

tiler, conf, win, zone = sys.argv[1], sys.argv[2], int(sys.argv[3], 0), sys.argv[4]
spec = importlib.util.spec_from_file_location("zt_picker_smoke", tiler)
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
grid = zt.Grid(conf)
wa = zt.monitors()[0]
extents = zt.frame_extents(win)
actual = zt.read_frame(win, extents)
expected = grid.rect(zone, wa)
print(f"факт {actual}, ожидалось {expected} ({zone})")
sys.exit(0 if actual == expected else 1)
PY
}

# Запустить оверлей выбора зоны в фоне; в $1 — лог. PID кладёт в PICKPID.
start_picker() {
    python3 "$PICKER" --window "$WIN" --monitor 0 --conf "$CONF" >"$1" 2>&1 &
    PICKPID=$!
}

# Дождаться выхода оверлея (или убить его через 12 с). Код возврата — в PICKRC.
finish_picker() {
    ( sleep 12; kill "$PICKPID" 2>/dev/null ) &
    _watch=$!
    wait "$PICKPID" 2>/dev/null
    PICKRC=$?
    kill "$_watch" 2>/dev/null
    PICKPID=""
}

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
    say "у muffin нет _NET_WORKAREA — оверлей не сможет посчитать зоны"
    exit 1
fi

DISPLAY="$DISP" xmessage -title "gridsnap-picker-smoke" -buttons OK:0 "тестовое окно" >/dev/null 2>&1 &
WPID=$!

export DISPLAY="$DISP"

WIN=""
_left=40
while [ "$_left" -gt 0 ]; do
    WIN=$(xdotool search --name "^gridsnap-picker-smoke$" 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$WIN" ]; then
    say "не удалось открыть тестовое окно"
    exit 1
fi

# Курсор в левый верх — начальное выделение попадёт в зону main.
xdotool mousemove 5 5
sleep 0.5

say "дисплей $DISP, тестовое окно $WIN"

# --- стрелки + Enter: main (под курсором) -> вправо -> top-right -------------
start_picker "$LOGDIR/picker-apply.log"
if ! wait_for 40 xdotool search --name "выбор зоны"; then
    bad "оверлей выбора зоны не появился. Его вывод:"
    sed 's/^/      /' "$LOGDIR/picker-apply.log" 2>/dev/null | tail -20
    finish_picker
else
    sleep 1.5               # дать оверлею взять клавиатуру
    xdotool key Right
    sleep 0.6
    xdotool key Return
    finish_picker
    if [ "$PICKRC" -ne 0 ]; then
        bad "оверлей вышел с кодом $PICKRC"
        sed 's/^/      /' "$LOGDIR/picker-apply.log" 2>/dev/null | tail -20
    elif ! grep -q "зона 'top-right'" "$LOGDIR/picker-apply.log"; then
        bad "Enter вложил окно не в top-right:"
        sed 's/^/      /' "$LOGDIR/picker-apply.log" 2>/dev/null | tail -20
    elif check_frame "$WIN" top-right >"$LOGDIR/frame.log" 2>&1; then
        ok "$(cat "$LOGDIR/frame.log")"
    else
        bad "рамка окна не совпала с top-right: $(cat "$LOGDIR/frame.log")"
    fi
fi

# --- Esc: окно остаётся там, где было ---------------------------------------
python3 "$TILER" apply main --window "$WIN" --conf "$CONF" --quiet >/dev/null 2>&1
if ! check_frame "$WIN" main >/dev/null 2>&1; then
    bad "не удалось вернуть окно в main перед проверкой Esc"
else
    start_picker "$LOGDIR/picker-esc.log"
    if ! wait_for 40 xdotool search --name "выбор зоны"; then
        bad "оверлей для проверки Esc не появился"
        finish_picker
    else
        sleep 1.5
        xdotool key Right
        sleep 0.6
        xdotool key Escape
        finish_picker
        if [ "$PICKRC" -ne 0 ]; then
            bad "Esc вышел с кодом $PICKRC"
        elif check_frame "$WIN" main >"$LOGDIR/frame-esc.log" 2>&1; then
            ok "Esc отменил выбор: $(cat "$LOGDIR/frame-esc.log")"
        else
            bad "Esc подвинул окно: $(cat "$LOGDIR/frame-esc.log")"
        fi
    fi
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест выбора зоны пройден."
else
    say "Провалов: $fails"
    exit 1
fi

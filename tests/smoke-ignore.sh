#!/bin/sh
# Дымовой тест исключений окон GridSnap (`[ignore]`).
#
# Поднимает СВОЙ X-сервер (Xvfb :95) и muffin, открывает два окна xmessage —
# обычное (`gridsnap-normal`) и «диалог» (`gridsnap-ignore`) — и проверяет, что
# окно под `[ignore]` не трогает ни один из путей, а обычное двигается:
#
#   1. явный `zone-tiler.py apply` — исключённое окно стоит, обычное едет в зону;
#   2. `zone-tiler.py arrange` — исключённое не раскладывается, обычное — да;
#   3. `zone-daemon.py` при старте — исключённое не укладывается (on_map), обычное — да;
#   4. `zone-picker.py` + Enter — исключённое окно не сдвигается, код возврата 1;
#   5. перезапуск демона — окно, которое пользователь вынес из зоны, не возвращается.
#
# Конфиг — свой, во временном каталоге; рабочий стол, окна и настройки владельца
# не затрагиваются.
#
#   tests/smoke-ignore.sh
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

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:95}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
PICKER="$ROOT/zone-picker.py"
DAEMON="$ROOT/zone-daemon.py"

# Своё состояние — во временном каталоге, не в домашнем.
STATE=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-state.$$)
export XDG_STATE_HOME="$STATE"
# Не подхватить конфиг владельца из XDG.
unset XDG_CONFIG_HOME 2>/dev/null || true

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-ignore.$$)
CONF="$LOGDIR/zones.conf"
MUFFIN_LOG="$LOGDIR/gridsnap-ignore-muffin.log"
# Сокет демона — в своём каталоге: при живом демоне владельца `arrange`
# делегирует ему (это шаг 2 сценария), и он ушёл бы в чужой процесс.
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

for c in Xvfb muffin xmessage xdotool python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb muffin x11-utils xdotool python3"
        exit 2
    fi
done

XPID=""; MPID=""; WIGN=""; WNORM=""; PICKPID=""
cleanup() {
    [ -n "$PICKPID" ] && kill "$PICKPID" 2>/dev/null
    [ -n "$WIGN" ] && kill "$WIGN" 2>/dev/null
    [ -n "$WNORM" ] && kill "$WNORM" 2>/dev/null
    [ -n "$MPID" ] && kill "$MPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 1
    rm -rf "$LOGDIR" "$STATE"
    return 0
}
trap cleanup EXIT INT TERM

# Фактическая рамка окна с компенсацией _NET_FRAME_EXTENTS — как её читает
# раскладчик, чтобы тест не дублировал геометрию сам.
frame_of() {  # $1 = окно -> «x y w h»
    python3 - "$TILER" "$1" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_ignore", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

# Окно в зоне? Печатает «факт … ожидалось …» и возвращает 1 при расхождении.
zone_frame_ok() {  # $1 = окно, $2 = зона
    python3 - "$TILER" "$CONF" "$1" "$2" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_ignore", sys.argv[1])
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

summary() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

cat >"$CONF" <<'EOF'
# Конфиг дымового теста исключений: обычное окно кладём в right, «диалог» исключаем.
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[rules]
re:gridsnap-ignore$ = right
re:gridsnap-normal$ = right

[ignore]
re:gridsnap-ignore$ = тестовое «диалоговое» окно
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

DISPLAY="$DISP" xmessage -title "gridsnap-ignore" -buttons OK:0 "исключённое окно" >/dev/null 2>&1 &
WIGN=$!
DISPLAY="$DISP" xmessage -title "gridsnap-normal" -buttons OK:0 "обычное окно" >/dev/null 2>&1 &
WNORM=$!

export DISPLAY="$DISP"

IGN=""; NRM=""
_left=40
while [ "$_left" -gt 0 ]; do
    IGN=$(xdotool search --name '^gridsnap-ignore$' 2>/dev/null | head -1)
    NRM=$(xdotool search --name '^gridsnap-normal$' 2>/dev/null | head -1)
    [ -n "$IGN" ] && [ -n "$NRM" ] && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -z "$IGN" ] || [ -z "$NRM" ]; then
    say "не удалось открыть тестовые окна (исключённое: ${IGN:-нет}, обычное: ${NRM:-нет})"
    exit 1
fi

say "дисплей $DISP, исключённое окно $IGN, обычное $NRM"

# --- 1. явный apply -----------------------------------------------------
say ""
say "1. Явный apply не двигает исключённое окно"
BEFORE=$(frame_of "$IGN")
python3 "$TILER" apply right --window "$IGN" --conf "$CONF" --quiet >"$LOGDIR/apply-ignore.log" 2>&1
AFTER=$(frame_of "$IGN")
if [ "$BEFORE" = "$AFTER" ]; then
    ok "apply оставил исключённое окно на месте"
else
    bad "apply сдвинул исключённое окно: $BEFORE -> $AFTER ($(summary "$LOGDIR/apply-ignore.log"))"
fi
python3 "$TILER" apply main --window "$NRM" --conf "$CONF" --quiet >/dev/null 2>&1
python3 "$TILER" apply right --window "$NRM" --conf "$CONF" --quiet >/dev/null 2>&1
if zone_frame_ok "$NRM" right >"$LOGDIR/apply-normal.log" 2>&1; then
    ok "apply положил обычное окно в right: $(cat "$LOGDIR/apply-normal.log")"
else
    bad "apply не положил обычное окно в right: $(cat "$LOGDIR/apply-normal.log")"
fi

# --- 2. arrange ---------------------------------------------------------
say ""
say "2. arrange не раскладывает исключённое окно"
python3 "$TILER" apply main --window "$NRM" --conf "$CONF" --quiet >/dev/null 2>&1
BEFORE=$(frame_of "$IGN")
python3 "$TILER" arrange --by rules --conf "$CONF" --quiet >"$LOGDIR/arrange.log" 2>&1
AFTER=$(frame_of "$IGN")
if [ "$BEFORE" = "$AFTER" ] && zone_frame_ok "$NRM" right >"$LOGDIR/arrange-normal.log" 2>&1; then
    ok "arrange пропустил исключённое и разложил обычное: $(cat "$LOGDIR/arrange-normal.log")"
else
    bad "arrange: исключённое $BEFORE -> $AFTER; обычное: $(cat "$LOGDIR/arrange-normal.log" 2>/dev/null) ($(summary "$LOGDIR/arrange.log"))"
fi

# --- 3. демон при старте (on_map) ---------------------------------------
say ""
say "3. Демон не укладывает исключённое окно"
python3 "$TILER" apply main --window "$NRM" --conf "$CONF" --quiet >/dev/null 2>&1
BEFORE=$(frame_of "$IGN")
python3 "$DAEMON" --once --no-socket --conf "$CONF" --verbose >"$LOGDIR/daemon.log" 2>&1
AFTER=$(frame_of "$IGN")
if [ "$BEFORE" = "$AFTER" ] && zone_frame_ok "$NRM" right >"$LOGDIR/daemon-normal.log" 2>&1; then
    ok "демон пропустил исключённое и уложил обычное: $(cat "$LOGDIR/daemon-normal.log")"
else
    bad "демон: исключённое $BEFORE -> $AFTER; обычное: $(cat "$LOGDIR/daemon-normal.log" 2>/dev/null) ($(summary "$LOGDIR/daemon.log"))"
fi

# --- 4. пикер + Enter ---------------------------------------------------
say ""
say "4. Пикер отказывается вкладывать исключённое окно"
python3 "$TILER" apply main --window "$NRM" --conf "$CONF" --quiet >/dev/null 2>&1
BEFORE=$(frame_of "$IGN")
python3 "$PICKER" --window "$IGN" --monitor 0 --conf "$CONF" >"$LOGDIR/picker.log" 2>&1 &
PICKPID=$!
if wait_for 40 xdotool search --name "выбор зоны"; then
    sleep 1.5               # дать оверлею взять клавиатуру
    xdotool key Right
    sleep 0.6
    xdotool key Return
    ( sleep 12; kill "$PICKPID" 2>/dev/null ) &
    _watch=$!
    wait "$PICKPID" 2>/dev/null
    PICKRC=$?
    kill "$_watch" 2>/dev/null
    PICKPID=""
    AFTER=$(frame_of "$IGN")
    if [ "$PICKRC" -eq 1 ] && [ "$BEFORE" = "$AFTER" ]; then
        ok "пикер отказал (код $PICKRC) и не сдвинул исключённое окно"
    else
        bad "пикер: код $PICKRC, исключённое $BEFORE -> $AFTER ($(summary "$LOGDIR/picker.log"))"
    fi
else
    bad "оверлей выбора зоны не появился ($(summary "$LOGDIR/picker.log"))"
    kill "$PICKPID" 2>/dev/null
    PICKPID=""
fi

# --- 5. перезапуск демона и память о вынесенном окне ---------------------
say ""
say "5. Перезапуск демона не возвращает вынесенное окно"
# Окно стоит в main, а в файле состояния демона записано, что он клал его в right.
BEFORE=$(frame_of "$NRM")
python3 "$DAEMON" --once --no-socket --conf "$CONF" --verbose >"$LOGDIR/daemon-restart.log" 2>&1
AFTER=$(frame_of "$NRM")
if [ "$BEFORE" = "$AFTER" ] && zone_frame_ok "$NRM" main >"$LOGDIR/daemon-restart.log.frame" 2>&1; then
    ok "демон не вернул окно в зону, оно осталось в main: $(cat "$LOGDIR/daemon-restart.log.frame")"
else
    bad "демон вернул окно: $BEFORE -> $AFTER ($(summary "$LOGDIR/daemon-restart.log"))"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест исключений окон пройден."
else
    say "Провалов: $fails"
    exit 1
fi

#!/bin/sh
# Дымовой тест вкладок в зоне: несколько окон в одной зоне.
#
# Поднимает СВОЙ X-сервер (Xvfb :94), muffin и три окна xmessage — два
# (`gridsnap-tabs-a`, `gridsnap-tabs-b`) по правилу `[rules]` попадают в одну
# зону `main`, третье (`gridsnap-tabs-c`) — в зону `right` — и проверяет:
#
#   1. одно окно в зоне занимает её целиком: полосы нет, окно по рамке зоны;
#   2. второе окно в той же зоне становится вкладкой и ПЕРЕСТАВЛЯЕТ первое —
#      обе рамки равны «зона минус высота полосы» из `[tabs] height`;
#   3. `tabs-next` по сокету поднимает соседнюю вкладку (кольцо из двух);
#   4. `focus` на зоне со вкладками сворачивает только чужие зоны: вкладки
#      зоны-цели остаются видимыми, выход разворачивает свёрнутое;
#   5. закрытие вкладки возвращает единственной оставшейся всю зону.
#
# Своё состояние и свой runtime-каталог — во временном каталоге: сокет демона
# не должен совпасть с сокетом демона владельца, а файлы состояния — попасть в
# его домашний каталог. Рабочий стол, окна и настройки владельца не затрагиваются.
#
#   tests/smoke-tabs.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 python3-gi
#   gir1.2-gtk-3.0 python3-cairo
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером в CI. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:94}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
DAEMON="$ROOT/zone-daemon.py"
CTL="$ROOT/gridsnap-ctl"

# Высота полосы в этом сценарии не умолчание (22), а своё число: иначе тест не
# отличил бы чтение `[tabs] height` от константы в коде.
TABS_H=30

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-tabs.$$)
CONF="$LOGDIR/zones.conf"
MUFFIN_LOG="$LOGDIR/gridsnap-tabs-muffin.log"
DAEMON_LOG="$LOGDIR/gridsnap-tabs-daemon.log"

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

XPID=""; MPID=""; DPID=""; WA=""; WB=""; WC=""
cleanup() {
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$WA" ] && kill "$WA" 2>/dev/null
    [ -n "$WB" ] && kill "$WB" 2>/dev/null
    [ -n "$WC" ] && kill "$WC" 2>/dev/null
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

spec = importlib.util.spec_from_file_location("zt_smoke_tabs", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

# Ожидаемая рамка: зона в пикселях, сжатая правилом вкладок (одна вкладка — вся
# зона, две и больше — зона минус полоса высотой [tabs] height). Считается тем
# же ядром, что и у демона, — тест не повторяет арифметику сам.
expected_frame() {  # $1 = зона, $2 = число вкладок -> «x y w h»
    python3 - "$TILER" "$CONF" "$ROOT" "$1" "$2" "$TABS_H" <<'PY'
import importlib.util
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load("zt_smoke_tabs", sys.argv[1])
ztabs = load("ztabs_smoke_tabs", sys.argv[3] + "/zone_tabs.py")
grid = zt.Grid(sys.argv[2])
rect = grid.rect(sys.argv[4], zt.monitors()[0])
print(*[int(round(v)) for v in ztabs.stack_rect(rect, int(sys.argv[5]), int(sys.argv[6]))])
PY
}

frame_is() {  # молча: рамка окна равна ожидаемой
    [ "$(frame_of "$1")" = "$(expected_frame "$2" "$3")" ]
}

frame_report() {  # $1 = окно, $2 = зона, $3 = число вкладок
    printf 'факт %s, ожидалось %s (%s, вкладок %s)' \
        "$(frame_of "$1")" "$(expected_frame "$2" "$3")" "$2" "$3"
}

# Свой запрос демону по сокету: `gridsnap-ctl` знает не все команды (вкладки
# шлёт раскладчик и полоса), а протокол — тот же `zone_ipc`.
ipc_call() {  # $1 = JSON запроса -> ответ одной строкой
    python3 - "$ROOT" "$1" <<'PY'
import importlib.util
import json
import socket
import sys

spec = importlib.util.spec_from_file_location("zipc_smoke_tabs", sys.argv[1] + "/zone_ipc.py")
zi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zi)
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.settimeout(5)
conn.connect(zi.default_socket_path())
conn.sendall(zi.encode_message(json.loads(sys.argv[2])))
# Ответ — словарь; печатаем именно JSON, а не repr словаря: дальше его разбирает
# `jget`, и одинарные кавычки Python он бы не понял.
response = zi.read_response(conn)
print(response if isinstance(response, str) else json.dumps(response, ensure_ascii=False))
conn.close()
PY
}

jget() {  # $1 = JSON строкой, $2 = поле -> значение (пусто, если нет)
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(0)
value = data.get(sys.argv[1])
print("" if value is None else value)
' "$2"
}

# Активное окно в десятичном виде (xprop отдаёт hex, xdotool — десятичное).
# Формат строки у xprop — «window id # 0x…», поэтому берём первый hex в строке.
active_window() {
    _hex=$(xprop -root _NET_ACTIVE_WINDOW 2>/dev/null \
        | sed -n 's/.*\(0x[0-9a-fA-F]*\).*/\1/p' | head -1)
    [ -n "$_hex" ] && printf '%d\n' "$_hex"
    return 0
}

wm_state() {  # $1 = окно -> Normal|Iconic|?
    xprop -id "$1" WM_STATE 2>/dev/null \
        | sed -n 's/.*window state: *\([A-Za-z]*\).*/\1/p' | head -1
}

summary() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

# Правило по заголовку: у xmessage один и тот же WM_CLASS на все окна, поэтому
# различать их можно только регуляркой по `class|title`.
cat >"$CONF" <<EOF
# Конфиг дымового теста вкладок в зоне.
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[rules]
re:gridsnap-tabs-a\$ = main
re:gridsnap-tabs-b\$ = main
re:gridsnap-tabs-c\$ = right

[tabs]
height = $TABS_H
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

# Демон — хозяин вкладок, поэтому он поднимается ДО окон: окна должны встать
# в зоны сами, по правилам, как у пользователя.
python3 "$DAEMON" --conf "$CONF" --verbose >"$DAEMON_LOG" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$RUNTIME/gridsnap/daemon.sock"; then
    say "демон не поднял сокет за 20 с ($(summary "$DAEMON_LOG"))"
    exit 1
fi

say "дисплей $DISP, высота полосы $TABS_H"

# --- 1. одно окно: полосы нет -------------------------------------------
say ""
say "1. Первое окно занимает зону целиком"
DISPLAY="$DISP" xmessage -title "gridsnap-tabs-a" -buttons OK:0 "первое" >/dev/null 2>&1 &
WA=$!
_left=60
while [ "$_left" -gt 0 ]; do
    WAID=$(xdotool search --name '^gridsnap-tabs-a$' 2>/dev/null | head -1)
    [ -n "$WAID" ] && frame_is "$WAID" main 1 && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -n "${WAID:-}" ] && frame_is "$WAID" main 1; then
    ok "окно по рамке зоны main: $(frame_report "$WAID" main 1)"
else
    bad "окно не заняло зону целиком: $(frame_report "${WAID:-0}" main 1)"
fi

# --- 2. второе окно в ту же зону: вкладка и перестановка первого --------
say ""
say "2. Второе окно в ту же зону — вкладка"
DISPLAY="$DISP" xmessage -title "gridsnap-tabs-b" -buttons OK:0 "второе" >/dev/null 2>&1 &
WB=$!
_left=60
while [ "$_left" -gt 0 ]; do
    WBID=$(xdotool search --name '^gridsnap-tabs-b$' 2>/dev/null | head -1)
    [ -n "$WBID" ] && frame_is "$WBID" main 2 && frame_is "$WAID" main 2 && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -n "${WBID:-}" ] && frame_is "$WBID" main 2; then
    ok "вторая вкладка на рамке стопки: $(frame_report "$WBID" main 2)"
else
    bad "вторая вкладка не на рамке стопки: $(frame_report "${WBID:-0}" main 2)"
fi
# Смысл вкладок: вторая переставляет первую, а не ложится поверх неё.
if [ -n "${WAID:-}" ] && frame_is "$WAID" main 2; then
    ok "первая вкладка переставлена на ту же рамку: $(frame_report "$WAID" main 2)"
else
    bad "первая вкладка осталась на прежней рамке: $(frame_report "${WAID:-0}" main 2)"
fi

# --- 3. переключение вкладок по сокету ----------------------------------
say ""
say "3. tabs-next поднимает соседнюю вкладку"
# Кто из двух вкладок активен сейчас — не важно: в кольце из двух «следующая»
# всегда другая, и проверка не зависит от того, кого muffin сделал активным.
# Сравниваем с окном, которое демон сам назвал в ответе: окно у xmessage и
# клиент, и его рамка — разные XID, и словари у xdotool и демона расходятся.
BEFORE=$(active_window)
RESP=$(ipc_call '{"cmd": "tabs-next"}')
AFTER=$(active_window)
WANT=$(($(jget "$RESP" window)))
if [ "$(jget "$RESP" switched)" = "True" ] && [ "$BEFORE" != "$AFTER" ] \
        && [ -n "$AFTER" ] && [ "$AFTER" = "$WANT" ] \
        && frame_is "$AFTER" main 2; then
    ok "переключение подняло другую вкладку зоны: $BEFORE -> $AFTER ($RESP)"
else
    bad "переключение не сработало: $BEFORE -> $AFTER, ожидалось $WANT, рамка $(frame_of "${AFTER:-0}"), ответ $RESP"
fi

# --- 4. focus на зоне со вкладками --------------------------------------
say ""
say "4. focus сворачивает чужие зоны, а вкладки зоны-цели — нет"
DISPLAY="$DISP" xmessage -title "gridsnap-tabs-c" -buttons OK:0 "третье" >/dev/null 2>&1 &
WC=$!
_left=60
while [ "$_left" -gt 0 ]; do
    WCID=$(xdotool search --name '^gridsnap-tabs-c$' 2>/dev/null | head -1)
    [ -n "$WCID" ] && frame_is "$WCID" right 1 && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -n "${WCID:-}" ] && frame_is "$WCID" right 1; then
    ok "третье окно встало в чужую зону right: $(frame_report "$WCID" right 1)"
else
    bad "третье окно не встало в right: $(frame_report "${WCID:-0}" right 1)"
fi

python3 "$CTL" focus main --window "$WAID" >"$LOGDIR/ctl-focus.log" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
    bad "gridsnap-ctl focus завершился с кодом $RC ($(summary "$LOGDIR/ctl-focus.log"))"
else
    if [ "$(wm_state "$WCID")" = "Iconic" ]; then
        ok "окно чужой зоны свёрнуто"
    else
        bad "окно чужой зоны не свёрнуто: $(wm_state "$WCID")"
    fi
    SA=$(wm_state "$WAID"); SB=$(wm_state "$WBID")
    printf '    %s=%s %s=%s\n' "$WAID" "$SA" "$WBID" "$SB"
    if [ "$SA" = "Normal" ] && [ "$SB" = "Normal" ]; then
        ok "вкладки зоны-цели остались видимыми"
    else
        bad "режим свернул вкладки зоны-цели: $WAID=$SA $WBID=$SB"
    fi
    python3 "$CTL" focus --off >"$LOGDIR/ctl-off.log" 2>&1
    RC=$?
    if [ "$RC" -eq 0 ] && [ "$(wm_state "$WCID")" = "Normal" ]; then
        ok "выход из режима развернул чужое окно"
    else
        bad "выход из режима не развернул чужое окно: код $RC, $(wm_state "$WCID")"
    fi
fi

# --- 5. закрытие вкладки возвращает зону оставшейся ---------------------
say ""
say "5. Закрытие вкладки возвращает оставшейся всю зону"
kill "$WB" 2>/dev/null
_left=60
while [ "$_left" -gt 0 ]; do
    frame_is "$WAID" main 1 && break
    sleep 0.5
    _left=$((_left - 1))
done
if frame_is "$WAID" main 1; then
    ok "оставшаяся вкладка заняла всю зону: $(frame_report "$WAID" main 1)"
else
    bad "зона не вернулась оставшейся вкладке: $(frame_report "$WAID" main 1)"
fi
WB=""

python3 "$CTL" quit >/dev/null 2>&1
wait "$DPID" 2>/dev/null
DPID=""

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест вкладок в зоне пройден."
    exit 0
fi
say "Дымовой тест вкладок в зоне: расхождений $fails."
exit 1

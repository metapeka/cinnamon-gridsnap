#!/bin/sh
# Дымовой тест подписки на события демона.
#
# Поднимает СВОЙ X-сервер (Xvfb :89), muffin, демон и двух подписчиков —
# `gridsnap-ctl watch` (глазами) и сырое соединение по протоколу — и проверяет,
# что демон рассказывает о том, что делает:
#
#   1. подписка принимается (`subscribe`), а соединение после неё — только на
#      чтение: вторая команда в том же соединении остаётся без ответа;
#   2. `gridsnap-ctl watch` печатает по строке JSON на событие;
#   3. окно, появившееся по правилу, даёт `window-appeared` со своей зоной;
#   4. `arrange` через демон даёт `window-moved` с прежней зоной и новой;
#   5. `focus` даёт `focus-on` (со счётчиком свёрнутых), `focus --off` — `focus-off`;
#   6. закрытие окна даёт `window-closed`;
#   7. обрыв подписчика демон не роняет, а без демона `watch` честно говорит об этом.
#
# Своё состояние и свой runtime-каталог — во временном каталоге: сокет демона не
# должен совпасть с сокетом демона владельца, а файлы состояния — попасть в его
# домашний каталог. Рабочий стол, окна и настройки владельца не затрагиваются.
#
#   tests/smoke-events.sh
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool python3 python3-gi
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

DISP="${GRIDSNAP_SMOKE_DISPLAY:-:89}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TILER="$ROOT/zone-tiler.py"
DAEMON="$ROOT/zone-daemon.py"
CTL="$ROOT/gridsnap-ctl"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp/gridsnap-events.$$)
CONF="$LOGDIR/zones.conf"
MUFFIN_LOG="$LOGDIR/gridsnap-events-muffin.log"
DAEMON_LOG="$LOGDIR/gridsnap-events-daemon.log"
EVENTS="$LOGDIR/events.jsonl"
WATCH_LOG="$LOGDIR/watch.log"

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

XPID=""; MPID=""; DPID=""; WATCHPID=""; WA=""; WB=""
cleanup() {
    [ -n "$WATCHPID" ] && kill "$WATCHPID" 2>/dev/null
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$WA" ] && kill "$WA" 2>/dev/null
    [ -n "$WB" ] && kill "$WB" 2>/dev/null
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

spec = importlib.util.spec_from_file_location("zt_smoke_events", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
win = int(sys.argv[2], 0)
print(*zt.read_frame(win, zt.frame_extents(win)))
PY
}

expected_frame() {  # $1 = зона -> «x y w h»
    python3 - "$TILER" "$CONF" "$1" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("zt_smoke_events", sys.argv[1])
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)
grid = zt.Grid(sys.argv[2])
print(*[int(round(v)) for v in grid.rect(sys.argv[3], zt.monitors()[0])])
PY
}

frame_is() {  # молча: рамка окна равна рамке зоны
    [ "$(frame_of "$1")" = "$(expected_frame "$2")" ]
}

frame_report() {  # $1 = окно, $2 = зона
    printf 'факт %s, ожидалось %s (%s)' "$(frame_of "$1")" "$(expected_frame "$2")" "$2"
}

# Свой запрос демону по сокету: `gridsnap-ctl` знает не все команды (подписка —
# команда протокола), а протокол — тот же `zone_ipc`.
ipc_call() {  # $1 = JSON запроса -> ответ одной строкой
    python3 - "$ROOT" "$1" <<'PY'
import importlib.util
import json
import socket
import sys

spec = importlib.util.spec_from_file_location("zipc_smoke_events", sys.argv[1] + "/zone_ipc.py")
zi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zi)
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.settimeout(5)
conn.connect(zi.default_socket_path())
conn.sendall(zi.encode_message(json.loads(sys.argv[2])))
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

# Найти в потоке событие: первый аргумент — вид, дальше необязательные пары
# «поле значение». Печатает найденную строку; пусто — не нашлось.
find_event() {  # $1 = вид, [$2 = поле $3 = значение]...
    python3 - "$EVENTS" "$@" <<'PY'
import json
import sys

path, kind = sys.argv[1], sys.argv[2]
pairs = sys.argv[3:]
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != kind:
            continue
        if all(str(event.get(pairs[i])) == pairs[i + 1] for i in range(0, len(pairs), 2)):
            print(line)
            sys.exit(0)
sys.exit(1)
PY
}

wait_event() {  # то же, что find_event, но с ожиданием
    _left=60
    while [ "$_left" -gt 0 ]; do
        if find_event "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

event_report() {  # $1 = вид -> что есть в потоке
    _found=$(find_event "$1" 2>/dev/null | tr '\n' ' ')
    if [ -n "$_found" ]; then printf '%s' "$_found"; else printf 'события %s в потоке нет' "$1"; fi
}

summary() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }
hex_of() { printf '0x%x' "$1"; }

# Правило по заголовку: у xmessage один и тот же WM_CLASS на все окна, поэтому
# различать их можно только регуляркой по `class|title`.
cat >"$CONF" <<EOF
# Конфиг дымового теста подписки на события.
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[rules]
re:gridsnap-events-a\$ = right
re:gridsnap-events-b\$ = main
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

python3 "$DAEMON" --conf "$CONF" --verbose >"$DAEMON_LOG" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$RUNTIME/gridsnap/daemon.sock"; then
    say "демон не поднял сокет за 20 с ($(summary "$DAEMON_LOG"))"
    exit 1
fi

say "дисплей $DISP"

# --- 1. подписка принимается, соединение только на чтение ----------------
say ""
say "1. Подписка принимается; после неё соединение только на чтение"
RESP=$(ipc_call '{"cmd": "subscribe", "events": ["window", "focus"]}')
if [ "$(jget "$RESP" ok)" = "True" ] \
        && printf '%s' "$(jget "$RESP" subscribed)" | grep -q window \
        && printf '%s' "$(jget "$RESP" subscribed)" | grep -q focus; then
    ok "демон принял подписку: $RESP"
else
    bad "демон не принял подписку: $RESP"
fi

# Вторая команда в подписанном соединении должна остаться без ответа: демон
# после подписки в это соединение только пишет.
python3 - "$ROOT" <<'PY'
import importlib.util
import json
import socket
import sys

spec = importlib.util.spec_from_file_location("zipc_smoke_events", sys.argv[1] + "/zone_ipc.py")
zi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zi)
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
broken = True
try:
    conn.settimeout(5)
    conn.connect(zi.default_socket_path())
    conn.sendall(zi.encode_message({"cmd": "subscribe"}))
    answer = zi.read_response(conn)
    conn.sendall(zi.encode_message({"cmd": "status"}))
    conn.settimeout(1.5)
    try:
        leftover = conn.recv(4096)
    except socket.timeout:
        leftover = b""
    print("подписка:", json.dumps(answer, ensure_ascii=False))
    print("ответ на status после подписки:", "нет" if not leftover else leftover[:120].decode("utf-8", "replace"))
    # Годится только подписка, принятая демоном: тогда он в это соединение
    # больше не читает и на status не отвечает.
    broken = not answer.get("ok") or bool(leftover)
finally:
    conn.close()
sys.exit(1 if broken else 0)
PY
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "демон принял подписку и молчит на команды после неё"
else
    bad "демон не принял подписку или ответил на команду после неё (код $RC)"
fi

# --- 2. watch печатает поток --------------------------------------------
say ""
say "2. gridsnap-ctl watch печатает события"
: >"$EVENTS"
python3 "$CTL" watch >"$EVENTS" 2>"$WATCH_LOG" &
WATCHPID=$!
if wait_for 20 grep -q 'подписка' "$WATCH_LOG"; then
    ok "watch подписался: $(tail -1 "$WATCH_LOG")"
else
    bad "watch не сказал о подписке: $(summary "$WATCH_LOG")"
fi

# --- 3. окно по правилу -> window-appeared ------------------------------
say ""
say "3. Окно, появившееся по правилу, даёт window-appeared"
DISPLAY="$DISP" xmessage -title "gridsnap-events-a" -buttons OK:0 "первое" >/dev/null 2>&1 &
WA=$!
_left=60
WAID=""
while [ "$_left" -gt 0 ]; do
    WAID=$(xdotool search --name '^gridsnap-events-a$' 2>/dev/null | head -1)
    [ -n "$WAID" ] && frame_is "$WAID" right && break
    sleep 0.5
    _left=$((_left - 1))
done
if [ -n "$WAID" ] && frame_is "$WAID" right; then
    ok "окно встало в зону right: $(frame_report "$WAID" right)"
else
    bad "окно не встало в right: $(frame_report "${WAID:-0}" right)"
fi
WAHEX=$(hex_of "${WAID:-0}")
if wait_event window-appeared window "$WAHEX" zone right; then
    ok "window-appeared с зоной right: $(find_event window-appeared window "$WAHEX" zone right)"
else
    bad "нет window-appeared для $WAHEX с зоной right: $(event_report window-appeared)"
fi

DISPLAY="$DISP" xmessage -title "gridsnap-events-b" -buttons OK:0 "второе" >/dev/null 2>&1 &
WB=$!
_left=60
WBID=""
while [ "$_left" -gt 0 ]; do
    WBID=$(xdotool search --name '^gridsnap-events-b$' 2>/dev/null | head -1)
    [ -n "$WBID" ] && frame_is "$WBID" main && break
    sleep 0.5
    _left=$((_left - 1))
done
WBHEX=$(hex_of "${WBID:-0}")
if [ -n "$WBID" ] && frame_is "$WBID" main; then
    ok "второе окно встало в зону main: $(frame_report "$WBID" main)"
else
    bad "второе окно не встало в main: $(frame_report "${WBID:-0}" main)"
fi
if wait_event window-appeared window "$WBHEX" zone main; then
    ok "window-appeared с зоной main: $(find_event window-appeared window "$WBHEX" zone main)"
else
    bad "нет window-appeared для $WBHEX с зоной main: $(event_report window-appeared)"
fi

# --- 4. arrange через демон -> window-moved -----------------------------
say ""
say "4. arrange через живой демон даёт window-moved"
python3 "$TILER" arrange --by order --conf "$CONF" --quiet >"$LOGDIR/arrange.log" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "arrange вернул 0"
else
    bad "arrange вернул $RC: $(summary "$LOGDIR/arrange.log")"
fi
# `--by order` меняет окна зонами местами: a был в right, b — в main.
if wait_event window-moved window "$WAHEX" zone main previous right; then
    ok "window-moved a: right -> main: $(find_event window-moved window "$WAHEX")"
else
    bad "нет window-moved для a (right -> main): $(event_report window-moved)"
fi
if wait_event window-moved window "$WBHEX" zone right previous main; then
    ok "window-moved b: main -> right: $(find_event window-moved window "$WBHEX")"
else
    bad "нет window-moved для b (main -> right): $(event_report window-moved)"
fi
_left=40
while [ "$_left" -gt 0 ]; do
    frame_is "$WAID" main && break
    sleep 0.5
    _left=$((_left - 1))
done
if frame_is "$WAID" main; then
    ok "окно a действительно переехало в main: $(frame_report "$WAID" main)"
else
    bad "окно a не переехало в main: $(frame_report "$WAID" main)"
fi

# --- 5. focus -> focus-on / focus-off -----------------------------------
say ""
say "5. focus даёт focus-on, focus --off — focus-off"
python3 "$CTL" focus main --window "$WAID" >"$LOGDIR/ctl-focus.log" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "gridsnap-ctl focus вернул 0"
else
    bad "gridsnap-ctl focus вернул $RC: $(summary "$LOGDIR/ctl-focus.log")"
fi
if wait_event focus-on zone main; then
    ok "focus-on: $(event_report focus-on)"
else
    bad "нет focus-on: $(summary "$DAEMON_LOG")"
fi
python3 "$CTL" focus --off >"$LOGDIR/ctl-off.log" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
    ok "gridsnap-ctl focus --off вернул 0"
else
    bad "gridsnap-ctl focus --off вернул $RC: $(summary "$LOGDIR/ctl-off.log")"
fi
if wait_event focus-off restored 1; then
    ok "focus-off с разворотом одного окна: $(event_report focus-off)"
else
    bad "нет focus-off с restored=1: $(event_report focus-off)"
fi

# --- 6. закрытие окна -> window-closed ----------------------------------
say ""
say "6. Закрытие окна даёт window-closed с последней зоной"
kill "$WB" 2>/dev/null
WB=""
if wait_event window-closed window "$WBHEX" zone right; then
    ok "window-closed: $(find_event window-closed window "$WBHEX")"
else
    bad "нет window-closed для $WBHEX: $(event_report window-closed)"
fi

# --- 7. обрыв подписчика демон не роняет --------------------------------
say ""
say "7. Обрыв подписчика демон не роняет"
kill "$WATCHPID" 2>/dev/null
WATCHPID=""
sleep 1
RESP=$(ipc_call '{"cmd": "status"}')
if [ "$(jget "$RESP" ok)" = "True" ]; then
    ok "после смерти подписчика демон отвечает"
else
    bad "демон не ответил после смерти подписчика: $RESP"
fi
DISPLAY="$DISP" xmessage -title "gridsnap-events-a" -buttons OK:0 "третье" >/dev/null 2>&1 &
WC=$!
sleep 2
kill "$WC" 2>/dev/null
RESP=$(ipc_call '{"cmd": "status"}')
if [ "$(jget "$RESP" ok)" = "True" ]; then
    ok "демон пережил ещё одно окно без подписчиков"
else
    bad "демон не пережил окно без подписчиков: $RESP"
fi

python3 "$CTL" quit >/dev/null 2>&1
wait "$DPID" 2>/dev/null
DPID=""

# --- 8. без демона watch честно говорит об этом -------------------------
say ""
say "8. Без демона watch завершается кодом 1"
python3 "$CTL" watch >"$LOGDIR/watch-off.log" 2>&1
RC=$?
if [ "$RC" -eq 1 ]; then
    ok "watch без демона — код 1: $(summary "$LOGDIR/watch-off.log")"
else
    bad "watch без демона дал код $RC вместо 1: $(summary "$LOGDIR/watch-off.log")"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест подписки на события пройден."
    exit 0
fi
say "Дымовой тест подписки на события: расхождений $fails."
exit 1

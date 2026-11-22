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

# Интерфейс — на исходном языке: сценарий сверяет поведение по русским
# сообщениям CLI, а перевод (каталоги en/zh_CN) выбирается из локали машины.
GRIDSNAP_LANG=ru
export GRIDSNAP_LANG
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

XPID=""; MPID=""; DPID=""; WA=""; WB=""; WC=""; WD=""
cleanup() {
    [ -n "$DPID" ] && kill "$DPID" 2>/dev/null
    [ -n "$WA" ] && kill "$WA" 2>/dev/null
    [ -n "$WB" ] && kill "$WB" 2>/dev/null
    [ -n "$WC" ] && kill "$WC" 2>/dev/null
    [ -n "$WD" ] && kill "$WD" 2>/dev/null
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

# Окно полосы вкладок: override-redirect над верхней кромкой зоны. Имени у него
# нет (POPUP/override-redirect), поэтому ищем по геометрии — но не запускаем
# xwininfo на каждое окно Xvfb (их сотни): `xwininfo -root -children` печатает
# размер и место детей сама, и xwininfo нужен только совпавшим по ним.
strip_window() {  # $1 = ширина, $2 = высота, $3 = x, $4 = y -> id или пусто
    for _id in $(xwininfo -root -children 2>/dev/null \
        | grep -F "$1x$2+$3+$4" | awk '{print $1}' | sort -u); do
        if xwininfo -id "$_id" 2>/dev/null | grep -q "Override Redirect State: yes"; then
            printf '%s\n' "$_id"
            return 0
        fi
    done
    return 1
}

strip_report() {  # $1 = ширина, $2 = высота -> что видно про полосу
    _id=$(strip_window "$1" "$2" 0 0)
    if [ -z "$_id" ]; then
        printf 'окна полосы нет'
        return 0
    fi
    xwininfo -id "$_id" 2>/dev/null | sed -n \
        -e 's/^  Width: \(.*\)$/ширина \1/p' \
        -e 's/^  Height: \(.*\)$/высота \1/p' \
        -e 's/^  Override Redirect State: \(.*\)$/override \1/p' | tr '\n' ' '
    printf '\n'
}

strip_is() {  # молча: полоса такого размера над верхней кромкой зоны есть
    [ -n "$(strip_window "$1" "$2" 0 0)" ]
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
re:gridsnap-tabs-d\$ = main
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

# --- 2.1 полоса вкладок -------------------------------------------------
say ""
say "2.1 Полоса вкладок нарисована над зоной"
# Полосу рисует отдельный процесс `zone-tabstrip.py`, которого демон поднимает
# сам, а снимок уходит ему в stdin. Проверяем не процесс, а окно на экране:
# override-redirect над верхней кромкой зоны, высотой из `[tabs] height`.
_left=40
while [ "$_left" -gt 0 ]; do
    strip_is 960 "$TABS_H" && break
    sleep 0.5
    _left=$((_left - 1))
done
if strip_is 960 "$TABS_H"; then
    ok "полоса над зоной main: $(strip_report 960 "$TABS_H")"
else
    bad "полосы над зоной main нет: $(strip_report 960 "$TABS_H"); демон: $(summary "$DAEMON_LOG")"
fi

# --- 3. переключение вкладок клавишей -----------------------------------
say ""
say "3. zone-tiler.py tabs-next поднимает соседнюю вкладку"
# Так её зовёт клавиша `[tabs] key`: путь пользователя, а не только сокет.
BEFORE=$(active_window)
python3 "$TILER" tabs-next --quiet --conf "$CONF" >"$LOGDIR/tabs-next.log" 2>&1
RC=$?
AFTER=$(active_window)
if [ "$RC" -eq 0 ] && [ -n "$AFTER" ] && [ "$BEFORE" != "$AFTER" ] && frame_is "$AFTER" main 2; then
    ok "клавиша переключила вкладку: $BEFORE -> $AFTER"
else
    bad "tabs-next не сработал: код $RC, $BEFORE -> $AFTER, рамка $(frame_of "${AFTER:-0}"), $(summary "$LOGDIR/tabs-next.log")"
fi

# --- 3.1 ответ демона на переключение -----------------------------------
say ""
say "3.1 tabs-next по сокету отвечает, кого поднял"
# Кто из двух вкладок активен — не важно: в кольце из двух «следующая» всегда
# другая. Сравниваем с окном, которое демон сам назвал в ответе: у xmessage
# клиент и его рамка — разные XID, и словари xdotool и демона расходятся.
RESP=$(ipc_call '{"cmd": "tabs-next"}')
AFTER=$(active_window)
WANT=$(($(jget "$RESP" window)))
if [ "$(jget "$RESP" switched)" = "True" ] && [ -n "$AFTER" ] && [ "$AFTER" = "$WANT" ] \
        && frame_is "$AFTER" main 2; then
    ok "демон поднял вкладку и назвал её: $AFTER ($RESP)"
else
    bad "ответ демона не сошёлся: активно $AFTER, ожидалось $WANT, рамка $(frame_of "${AFTER:-0}"), ответ $RESP"
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

    # --- 4.1 новое окно в зоне-цели во время режима ---------------------
    # Режим называется «только эта зона»: окно, которое правило положило в ту же
    # зону уже во время режима, становится вкладкой и остаётся видимым — свернуть
    # его значит спрятать окно той самой зоны, которую пользователь оставил.
    say ""
    say "4.1 Новая вкладка зоны-цели во время режима остаётся видимой"
    DISPLAY="$DISP" xmessage -title "gridsnap-tabs-d" -buttons OK:0 "четвёртое" >/dev/null 2>&1 &
    WD=$!
    _left=60
    while [ "$_left" -gt 0 ]; do
        WDID=$(xdotool search --name '^gridsnap-tabs-d$' 2>/dev/null | head -1)
        [ -n "$WDID" ] && frame_is "$WDID" main 3 && break
        sleep 0.5
        _left=$((_left - 1))
    done
    if [ -n "${WDID:-}" ] && frame_is "$WDID" main 3; then
        ok "новое окно встало третьей вкладкой зоны: $(frame_report "$WDID" main 3)"
    else
        bad "новое окно не встало вкладкой зоны: $(frame_report "${WDID:-0}" main 3)"
    fi
    if [ "$(wm_state "${WDID:-0}")" = "Normal" ]; then
        ok "новая вкладка зоны-цели осталась видимой"
    else
        bad "новая вкладка зоны-цели свёрнута режимом: ${WDID:-0}=$(wm_state "${WDID:-0}")"
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
# Закрываем сначала вкладку, пришедшую во время режима, потом вторую: зона
# возвращается оставшейся, только когда в стопке не осталось никого, кроме неё.
kill "$WD" 2>/dev/null
WD=""
sleep 1
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

# --- 5.1 полосы больше нет ----------------------------------------------
say ""
say "5.1 Полоса убрана: вкладка осталась одна"
_left=40
while [ "$_left" -gt 0 ]; do
    strip_is 960 "$TABS_H" || break
    sleep 0.5
    _left=$((_left - 1))
done
if strip_is 960 "$TABS_H"; then
    bad "полоса осталась над зоной с одной вкладкой: $(strip_report 960 "$TABS_H")"
else
    ok "полосы над зоной нет: окно занимает зону целиком"
fi

# --- 6. каскад удалён ---------------------------------------------------
say ""
say "6. --offset удалён, а не молча проигнорирован"
python3 "$TILER" --offset 20 list --conf "$CONF" >"$LOGDIR/offset.log" 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then
    ok "вызов с --offset — ошибка аргументов (код 2)"
else
    bad "--offset дал код $RC вместо 2: $(summary "$LOGDIR/offset.log")"
fi

python3 "$CTL" quit >/dev/null 2>&1
wait "$DPID" 2>/dev/null
DPID=""

# --- 7. без демона вкладок нет ------------------------------------------
say ""
say "7. Без демона tabs-next говорит, что переключать некому"
python3 "$TILER" tabs-next --quiet --conf "$CONF" >"$LOGDIR/tabs-next-off.log" 2>&1
RC=$?
if [ "$RC" -eq 1 ]; then
    ok "tabs-next без демона — код 1: $(summary "$LOGDIR/tabs-next-off.log")"
else
    bad "tabs-next без демона дал код $RC вместо 1: $(summary "$LOGDIR/tabs-next-off.log")"
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест вкладок в зоне пройден."
    exit 0
fi
say "Дымовой тест вкладок в зоне: расхождений $fails."
exit 1

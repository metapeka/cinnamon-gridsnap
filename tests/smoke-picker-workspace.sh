#!/bin/sh
# Дымовой тест сетки рабочего стола в оверлее выбора зоны (zone-picker.py).
#
# Проверяет, что пикер берёт сетку своего стола, а не общий [zones]: временный
# zones.conf сопоставляет стол 2 профилю «cinema», поэтому `--shot --workspace 1`
# обязан показать зоны профиля, `--workspace 0` — общий набор, а без --workspace
# (тут нет оконного менеджера, значит и активного стола) — снова общий.
# Живой ввод клавиш не нужен: `--shot` печатает набор зон и рисует его в PNG.
#
#   tests/smoke-picker-workspace.sh
#
# Зависимости (Debian): xvfb x11-utils python3 python3-gi gir1.2-gtk-3.0 python3-cairo
# Код возврата: 0 — всё сошлось, 1 — есть расхождения, 2 — нет зависимостей.

set -u

# Как и в smoke-picker.sh: X-сервер поднимаем свой, ровно на один сценарий.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP=":97"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PICKER="$ROOT/zone-picker.py"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
export XDG_STATE_HOME="$LOGDIR/state"

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

for c in Xvfb xprop python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "нет команды $c — установите: sudo apt install xvfb x11-utils python3"
        exit 2
    fi
done

XPID=""
cleanup() {
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 0.5
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

# --- временный конфиг: стол 2 -> профиль cinema ----------------------------
CONFDIR="$LOGDIR/conf"
mkdir -p "$CONFDIR/templates"
CONF="$CONFDIR/zones.conf"

cat >"$CONF" <<'EOF'
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.6, 1
top-right = 0.6, 0, 0.4, 0.5
bottom = 0.6, 0.5, 0.4, 0.5

[keys]
main = <Super><Alt>1

[workspaces]
2 = cinema
EOF

cat >"$CONFDIR/templates/cinema.conf" <<'EOF'
[grid]
cols = 1.0
rows = 1.0

[zones]
video = 0, 0, 1, 0.75
chat = 0, 0.75, 1, 0.25

[keys]
video = <Super><Alt>5
EOF

DEFAULT_SET="зон 3: main, top-right, bottom"
PROFILE_SET="зон 2: video, chat"

# Снимок оверлея: $1 — PNG, дальше — аргументы пикера. Вывод (со строкой про
# сетку) печатается в stdout, код возврата — код пикера.
shot() {
    _png=$1; shift
    DISPLAY="$DISP" python3 "$PICKER" --conf "$CONF" --shot "$_png" "$@" 2>&1
}

Xvfb "$DISP" -screen 0 1280x720x24 >/dev/null 2>&1 &
XPID=$!
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    say "Xvfb не поднялся (возможно, дисплей $DISP занят — закройте лишний Xvfb)"
    exit 1
fi
say "дисплей $DISP, конфиг $CONF (стол 2 = профиль cinema)"

# --- стол 2 (EWMH 1): сетка из профиля -------------------------------------
SHOT1="$LOGDIR/ws1.png"
OUT1=$(shot "$SHOT1" --workspace 1)
RC1=$?
if [ "$RC1" -ne 0 ]; then
    bad "--shot --workspace 1 вышел с кодом $RC1:"
    printf '%s\n' "$OUT1" | sed 's/^/      /'
elif ! printf '%s\n' "$OUT1" | grep -q "профиль «cinema»"; then
    bad "--workspace 1 не взял профиль стола:"
    printf '%s\n' "$OUT1" | sed 's/^/      /'
elif ! printf '%s\n' "$OUT1" | grep -q "$PROFILE_SET"; then
    bad "--workspace 1 показал не набор профиля (ждали «$PROFILE_SET»):"
    printf '%s\n' "$OUT1" | sed 's/^/      /'
elif [ ! -s "$SHOT1" ]; then
    bad "снимок $SHOT1 не записан"
else
    ok "$(printf '%s\n' "$OUT1" | sed 's/^zone-picker: //')"
fi

# --- стол 1 (EWMH 0): профиля нет, общий [zones] ---------------------------
SHOT0="$LOGDIR/ws0.png"
OUT0=$(shot "$SHOT0" --workspace 0)
RC0=$?
if [ "$RC0" -ne 0 ]; then
    bad "--shot --workspace 0 вышел с кодом $RC0:"
    printf '%s\n' "$OUT0" | sed 's/^/      /'
elif ! printf '%s\n' "$OUT0" | grep -q "$DEFAULT_SET"; then
    bad "--workspace 0 показал не общий набор (ждали «$DEFAULT_SET»):"
    printf '%s\n' "$OUT0" | sed 's/^/      /'
elif ! printf '%s\n' "$OUT0" | grep -q "стол 1 · общий набор"; then
    bad "--workspace 0 не подписал сетку как общий набор:"
    printf '%s\n' "$OUT0" | sed 's/^/      /'
else
    ok "$(printf '%s\n' "$OUT0" | sed 's/^zone-picker: //')"
fi

# Разные сетки — разные картинки; одинаковые значили бы, что снимок не зависит
# от стола (или что зоны профиля просто не нарисованы).
if [ -s "$SHOT1" ] && [ -s "$SHOT0" ]; then
    if cmp -s "$SHOT1" "$SHOT0"; then
        bad "снимки столов 1 и 2 совпали — сетка на снимке не зависит от стола"
    else
        ok "снимки столов 1 и 2 различаются (у профиля своя геометрия)"
    fi
fi

# --- без --workspace: активный стол неизвестен (нет WM) -> общий [zones] ---
OUTA=$(shot "$LOGDIR/ws-cur.png")
RCA=$?
if [ "$RCA" -ne 0 ]; then
    bad "--shot без --workspace вышел с кодом $RCA:"
    printf '%s\n' "$OUTA" | sed 's/^/      /'
elif ! printf '%s\n' "$OUTA" | grep -q "$DEFAULT_SET"; then
    bad "без --workspace показан не общий набор (ждали «$DEFAULT_SET»):"
    printf '%s\n' "$OUTA" | sed 's/^/      /'
else
    ok "без --workspace (стол неизвестен): $(printf '%s\n' "$OUTA" | sed 's/^zone-picker: //')"
fi

# --- --workspace conf: всегда сам zones.conf, профилей столов не читаем ----
OUTC=$(shot "$LOGDIR/ws-conf.png" --workspace conf)
RCC=$?
if [ "$RCC" -ne 0 ]; then
    bad "--shot --workspace conf вышел с кодом $RCC:"
    printf '%s\n' "$OUTC" | sed 's/^/      /'
elif ! printf '%s\n' "$OUTC" | grep -q "$DEFAULT_SET"; then
    bad "--workspace conf показал не zones.conf (ждали «$DEFAULT_SET»):"
    printf '%s\n' "$OUTC" | sed 's/^/      /'
else
    ok "--workspace conf: $(printf '%s\n' "$OUTC" | sed 's/^zone-picker: //')"
fi

# --- неверный стол: код 2, как у zone-tiler.py -----------------------------
OUTB=$(shot "$LOGDIR/ws-bad.png" --workspace 7x)
RCB=$?
if [ "$RCB" -eq 2 ]; then
    ok "неверный --workspace даёт код 2"
else
    bad "--workspace 7x дал код $RCB, а не 2:"
    printf '%s\n' "$OUTB" | tail -3 | sed 's/^/      /'
fi

say ""
if [ "$fails" -eq 0 ]; then
    say "Дымовой тест сетки стола в пикере пройден."
else
    say "Провалов: $fails"
    exit 1
fi

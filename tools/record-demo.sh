#!/bin/sh
# tools/record-demo.sh — записать демонстрацию GridSnap в GIF одной командой.
#
# Поднимает СВОЙ X-сервер (Xvfb :90), muffin, демон зон и редактор с временным
# `zones.conf`, открывает окна xmessage и прогоняет сцену:
#
#   1. редактор зон: сетка, список зон и клавиши;
#   2. окно встаёт в зону `main` пиксель в пиксель (вложение через демон);
#   3. пикер (`Super+Z`): оверлей сетки, стрелка вправо, Enter — окно в зоне;
#   4. второе окно в ту же зону — сверху полоса вкладок, вкладка переключается;
#   5. режим «только эта зона»: чужое окно свёрнуто, повторная команда вернула.
#
# Сцена снимается на **английском**: `GRIDSNAP_LANG=en` выставляется самим
# скриптом, а окна-декорации подписаны по-английски — демонстрация идёт в
# README и на сайт, где главная страница английская.
#
# Сцена пишется через `ffmpeg -f x11grab`, затем собирается GIF (палитра
# `palettegen`/`paletteuse`). Ширину GIF скрипт подбирает сам: 960 → 800 → 720
# → 640 px, пока файл не влезет в 2.5 МБ (ниже 640 не опускается: тогда лучше
# сокращать сцену, чем портить читаемость).
#
# Своё состояние, конфиг и сокет демона — во временном каталоге (`mktemp -d`),
# поэтому конфиг, состояние и сокеты владельца не затрагиваются. Временный
# каталог, Xvfb, muffin, окна и демон убираются на выходе — и при ошибке тоже.
#
#   tools/record-demo.sh                 # записать site/assets/demo.gif
#   tools/record-demo.sh /tmp/demo.gif   # другой путь результата
#   tools/record-demo.sh --display :91   # другой дисплей (по умолчанию :90)
#   tools/record-demo.sh --help
#
# Зависимости (Debian): xvfb muffin x11-utils xdotool xmessage ffmpeg
#   imagemagick python3 python3-gi gir1.2-gtk-3.0 python3-cairo
# `feh` не обязателен: если он есть, фон рабочего стола станет тёмным
# (градиент через `convert`), без него остаётся штатный фон muffin.
#
# Код возврата: 0 — GIF записана; 1 — что-то не поднялось или шаг сцены не
# сошёлся; 2 — нет зависимости или неверные аргументы.

set -u

# muffin (Mutter) выбирает бэкенд по logind/$XDG_SESSION_TYPE: без X11 он не
# становится оконным менеджером. См. tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

# Язык сцены — английский: демонстрация идёт в README и на сайт, где главная
# страница английская, а незнакомая локаль и так даёт английский. Переменная
# зафиксирована, чтобы GIF не зависела от локали машины, на которой снимали.
export GRIDSNAP_LANG=en

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$ROOT/site/assets/demo.gif"
DISP=:90

# Рабочая область без панели: 1280×800 даёт GIF 960×600 при ширине 960 px.
W=1280
H=800
FPS=15
LIMIT=2621440   # 2.5 МБ: больше — уже не «маленькая картинка» для README
FONT="-*-fixed-medium-r-normal--20-*-*-*-*-*-iso10646-1"
TABS_H=32

usage() {
    cat <<EOF
Записать демонстрацию GridSnap в GIF.

  tools/record-demo.sh [ОПЦИИ] [ФАЙЛ.gif]

ФАЙЛ.gif по умолчанию — site/assets/demo.gif в корне репозитория.
Каталоги пути создаются сами.

Опции:
  --display :N   X-дисплей для записи (по умолчанию :90; должен быть свободен)
  -h, --help     эта справка

Сцена: редактор зон → окно в зоне → вкладки → пикер → «только эта зона».
Свой Xvfb, muffin, демон и временный zones.conf; состояние и сокеты — во
временном каталоге, настройки владельца не трогаются. Нужны xvfb, muffin,
xdotool, xmessage, ffmpeg, python3 с GTK и cairo.

Код возврата: 0 — GIF записана, 1 — сцена не сошлась, 2 — нет зависимости
или неверные аргументы.
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --display)
            [ $# -ge 2 ] || { printf 'record-demo: --display требует значение\n' >&2; exit 2; }
            DISP=$2; shift ;;
        --display=*) DISP=${1#--display=} ;;
        -*) printf 'record-demo: неизвестная опция %s (см. --help)\n' "$1" >&2; exit 2 ;;
        *) OUT=$1 ;;
    esac
    shift
done

case $DISP in
    :*) ;;
    *) printf 'record-demo: дисплей должен быть вида :90, а не %s\n' "$DISP" >&2; exit 2 ;;
esac

for c in Xvfb muffin xdotool xmessage ffmpeg python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        printf 'record-demo: нет команды %s — установите: sudo apt install xvfb muffin x11-utils xdotool xmessage ffmpeg imagemagick python3 python3-gi gir1.2-gtk-3.0 python3-cairo\n' "$c" >&2
        exit 2
    fi
done

say() { printf '%s\n' "$1"; }
die() { printf 'record-demo: %s\n' "$1" >&2; exit 1; }
log_tail() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

WORK=$(mktemp -d 2>/dev/null || printf '/tmp/gridsnap-demo.%s' "$$")
mkdir -p "$WORK" || { printf 'record-demo: не создать временный каталог\n' >&2; exit 1; }
CONF="$WORK/zones.conf"
XDG_CONFIG_HOME="$WORK/config"
XDG_STATE_HOME="$WORK/state"
XDG_RUNTIME_DIR="$WORK/run"
export XDG_CONFIG_HOME XDG_STATE_HOME XDG_RUNTIME_DIR
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

XPID=""; MPID=""; DPID=""; EPID=""
W1PID=""; W2PID=""; W3PID=""
FFPID=""; PICKPID=""

cleanup() {
    trap - EXIT INT TERM
    for _p in "$PICKPID" "$FFPID" "$W1PID" "$W2PID" "$W3PID" "$EPID" "$DPID" "$MPID" "$XPID"; do
        [ -n "$_p" ] && kill "$_p" 2>/dev/null
    done
    sleep 0.5
    rm -rf "$WORK"
    return 0
}
trap cleanup EXIT INT TERM

wait_for() {
    _left=$1; shift
    while [ "$_left" -gt 0 ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

# Окно по заголовку: XID в stdout.
window_of() {  # $1 = заголовок
    xdotool search --name "$1" 2>/dev/null | head -1
}

wait_window() {  # $1 = заголовок, $2 = попыток по 0.5 с -> XID в stdout
    _left=$2
    while [ "$_left" -gt 0 ]; do
        _id=$(window_of "$1")
        [ -n "$_id" ] && { printf '%s\n' "$_id"; return 0; }
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

# Фактическая и ожидаемая рамка: считает то же ядро, что и раскладчик
# (`read_frame` с компенсацией _NET_FRAME_EXTENTS и `Grid.rect`), поэтому
# скрипт не дублирует геометрию зон сам. Возврат 1 — рамка не та.
frame_ok() {  # $1 = окно, $2 = зона, $3 = число вкладок
    python3 - "$ROOT/zone-tiler.py" "$ROOT/zone_tabs.py" "$CONF" "$1" "$2" "$3" "$TABS_H" <<'PY'
import importlib.util
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load("zt_record_demo", sys.argv[1])
zone_tabs = load("zone_tabs_record_demo", sys.argv[2])
conf, win = sys.argv[3], int(sys.argv[4], 0)
zone, count, height = sys.argv[5], int(sys.argv[6]), int(sys.argv[7])
grid = zt.Grid(conf)
rect = grid.rect(zone, zt.monitors()[0])
expected = tuple(int(round(v)) for v in zone_tabs.stack_rect(rect, count, height))
actual = zt.read_frame(win, zt.frame_extents(win))
print(f"факт {actual}, ожидалось {expected} ({zone}, вкладок {count})")
sys.exit(0 if actual == expected else 1)
PY
}

# Дождаться рамки (демон двигает окна асинхронно) и записать расхождение в лог.
expect_frame() {  # $1 = окно, $2 = зона, $3 = вкладок, $4 = попыток
    _left=${4:-20}
    while [ "$_left" -gt 0 ]; do
        if frame_ok "$1" "$2" "$3" >"$WORK/frame.log" 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    printf '  рамка не сошлась: %s\n' "$(cat "$WORK/frame.log" 2>/dev/null)" >&2
    return 1
}

wm_state() {  # $1 = окно -> Normal|Iconic|?
    xprop -id "$1" WM_STATE 2>/dev/null \
        | sed -n 's/.*window state: *\([A-Za-z]*\).*/\1/p' | head -1
}

# Окно xmessage: заголовок, текст из файла. Заголовок виден в полосе вкладок и
# в правилах `[rules]`, поэтому окна и различаются по нему. UTF-8 подключается
# `*international`, иначе Xaw печатает байты кириллицы октальными кодами.
open_window() {  # $1 = заголовок, $2 = файл с текстом -> PID в stdout
    DISPLAY="$DISP" xmessage -xrm "*international: True" -xrm "*fontSet: $FONT" \
        -bg "#141a22" -fg "#e8e8ee" -borderwidth 0 \
        -title "$1" -buttons "" "$(cat "$2")" >>"$WORK/windows.log" 2>&1 &
    printf '%s\n' "$!"
}

# Временный набор зон: две колонки, правая — из двух половин. Ширины в долях
# рабочей области, поэтому сцена не зависит от разрешения Xvfb.
cat >"$CONF" <<'EOF'
# Временный конфиг демонстрации GridSnap (пишет tools/record-demo.sh).
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.62, 1
top-right = 0.62, 0, 0.38, 0.5
bottom-right = 0.62, 0.5, 0.38, 0.5

[keys]
main = <Super>1
top-right = <Super>2
bottom-right = <Super>3

[rules]
re:Calendar$ = main

[tabs]
height = 32
EOF

# --- зависимости сцены ---------------------------------------------------
# Тексты окон: короткие строки, читаемые в маленьком GIF.
printf 'Inbox\n\n  November report\n  Standup 10:00\n  GridSnap: demo\n' >"$WORK/mail.txt"
printf 'Calendar\n\n  Mon 10:00 standup\n  Wed 15:00 review\n' >"$WORK/cal.txt"
printf 'Files\n\n  demo.gif\n  zones.conf\n  README.md\n' >"$WORK/files.txt"

# Занятость дисплея проверяется подключением, а не файлом блокировки: после
# падения Xvfb сокет и lock-файл могут остаться, и «занят» по файлу было бы
# враньём.
if env DISPLAY="$DISP" xprop -root >/dev/null 2>&1; then
    die "дисплей $DISP занят — закройте лишний Xvfb или укажите другой через --display"
fi

say "демонстрация: дисплей $DISP, рабочая область ${W}×${H}, ${FPS} кадров/с"

Xvfb "$DISP" -screen 0 "${W}x${H}x24" -nolisten tcp >"$WORK/xvfb.log" 2>&1 &
XPID=$!
sleep 0.5
if ! kill -0 "$XPID" 2>/dev/null; then
    die "Xvfb не стартовал на $DISP: $(log_tail "$WORK/xvfb.log") (остался lock-файл /tmp/.X${DISP#:}-lock от прошлого запуска? удалите его, если Xvfb не работает)"
fi
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    die "Xvfb не поднялся на $DISP ($(log_tail "$WORK/xvfb.log"))"
fi

DISPLAY="$DISP" muffin >"$WORK/muffin.log" 2>&1 &
MPID=$!
if ! wait_for 60 sh -c "DISPLAY='$DISP' xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id # 0x'"; then
    die "muffin не стал оконным менеджером за 30 с: $(log_tail "$WORK/muffin.log")"
fi
if ! wait_for 40 sh -c "DISPLAY='$DISP' xprop -root _NET_WORKAREA 2>/dev/null | grep -q '_NET_WORKAREA(CARDINAL)'"; then
    die "у muffin нет _NET_WORKAREA — раскладчик не посчитает зоны"
fi

# Фон — необязательная косметика: без feh остаётся штатный фон muffin.
if command -v feh >/dev/null 2>&1 && command -v convert >/dev/null 2>&1; then
    if convert -size "${W}x${H}" gradient:'#243044-#101820' "$WORK/bg.png" 2>/dev/null; then
        DISPLAY="$DISP" feh --bg-fill "$WORK/bg.png" >/dev/null 2>&1 || true
    fi
fi

export DISPLAY="$DISP"

# Демон — хозяин вкладок и вложения по сокету; он поднимается ДО окон.
python3 "$ROOT/zone-daemon.py" --conf "$CONF" --verbose >"$WORK/daemon.log" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$XDG_RUNTIME_DIR/gridsnap/daemon.sock"; then
    die "демон не поднял сокет за 20 с ($(log_tail "$WORK/daemon.log"))"
fi

# Первое окно — свободное, но открыто оно ДО записи: в кадр оно попадает уже на
# своём месте, когда редактор свернётся. Вложение делает демон (как апплет).
W1PID=$(open_window "Mail — inbox" "$WORK/mail.txt")
W1=$(wait_window "Mail" 40) || die "окно «Mail» не открылось ($(log_tail "$WORK/windows.log"))"
xdotool windowmove "$W1" 130 130

python3 "$ROOT/zone-editor.py" --conf "$CONF" >"$WORK/editor.log" 2>&1 &
EPID=$!
EDITOR=$(wait_window "Gridsnap" 60) || die "редактор зон не открылся ($(log_tail "$WORK/editor.log"))"
xdotool windowmove "$EDITOR" 70 40
xdotool windowsize "$EDITOR" 1120 700
sleep 1

# --- запись --------------------------------------------------------------
RAW="$WORK/demo.mp4"
say "пишу сцену…"
ffmpeg -y -loglevel error -f x11grab -draw_mouse 1 -framerate "$FPS" \
    -video_size "${W}x${H}" -i "${DISP}+0,0" -t 60 \
    -c:v libx264 -preset ultrafast -crf 16 -pix_fmt yuv420p \
    "$RAW" >"$WORK/ffmpeg.log" 2>&1 &
FFPID=$!
sleep 0.8

# 1. Редактор: сетка, список зон, клавиши.
sleep 2.2

# 2. Окно встаёт в зону — через демон, тот же путь, что у апплета.
xdotool windowminimize "$EDITOR"
sleep 1.0
say "  шаг 2: окно в зону main"
sleep 1.2
python3 "$ROOT/gridsnap-ctl" apply main --window "$W1" --quiet >"$WORK/apply.log" 2>&1 \
    || die "не удалось вложить окно в main ($(log_tail "$WORK/apply.log"))"
expect_frame "$W1" main 1 20 || die "окно не встало в зону main: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.5

# 3. Пикер: оверлей, стрелка вправо (main -> top-right), Enter. Пикер идёт
# ДО вкладок: полоса вкладок — окно поверх оверлея и закрыла бы подпись зоны
# `main` у верхней кромки.
say "  шаг 3: пикер"
W3PID=$(open_window "Files" "$WORK/files.txt")
W3=$(wait_window "Files" 40) || die "окно «Files» не открылось ($(log_tail "$WORK/windows.log"))"
sleep 1.0
python3 "$ROOT/zone-picker.py" --window "$W3" --monitor 0 --select main \
    --conf "$CONF" --quiet >"$WORK/picker.log" 2>&1 &
PICKPID=$!
# Окно пикера ищется по WM_CLASS, а не по заголовку: заголовок переводится,
# а класс процесса — нет. Так сцена не зависит от языка интерфейса.
if ! wait_for 40 xdotool search --classname zone-picker.py; then
    die "оверлей пикера не появился ($(log_tail "$WORK/picker.log"))"
fi
sleep 1.5            # оверлею нужно взять клавиатуру
xdotool key Right
sleep 1.2
xdotool key Return
wait "$PICKPID" 2>/dev/null
RC=$?
PICKPID=""
[ "$RC" -eq 0 ] || die "пикер вышел с кодом $RC ($(log_tail "$WORK/picker.log"))"
expect_frame "$W3" top-right 1 20 || die "пикер не вложил окно в top-right: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.5

# 4. Второе окно в ту же зону: правила демона делают из него вкладку.
say "  шаг 4: вкладки"
W2PID=$(open_window "Calendar" "$WORK/cal.txt")
W2=$(wait_window "Calendar" 40) || die "окно «Calendar» не открылось ($(log_tail "$WORK/windows.log"))"
expect_frame "$W2" main 2 40 || die "второе окно не стало вкладкой: $(cat "$WORK/frame.log" 2>/dev/null)"
expect_frame "$W1" main 2 20 || die "первая вкладка не переставлена на общую рамку: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.8
python3 "$ROOT/zone-tiler.py" tabs-next --conf "$CONF" >"$WORK/tabs-next.log" 2>&1 \
    || die "tabs-next не сработал ($(log_tail "$WORK/tabs-next.log"))"
sleep 1.5

# 5. «Только эта зона»: чужое окно свёрнуто, повторная команда — вернула.
say "  шаг 5: только эта зона"
python3 "$ROOT/gridsnap-ctl" focus main --window "$W1" --quiet >"$WORK/focus.log" 2>&1 \
    || die "режим «только эта зона» не включился ($(log_tail "$WORK/focus.log"))"
sleep 1.8
[ "$(wm_state "$W3")" = "Iconic" ] || die "чужое окно не свёрнуто режимом ($(wm_state "$W3"))"
python3 "$ROOT/gridsnap-ctl" focus --off --quiet >"$WORK/focus-off.log" 2>&1 \
    || die "режим не выключился ($(log_tail "$WORK/focus-off.log"))"
sleep 1.8
[ "$(wm_state "$W3")" = "Normal" ] || die "окно не развернулось после режима ($(wm_state "$W3"))"

kill -INT "$FFPID" 2>/dev/null
wait "$FFPID" 2>/dev/null
FFPID=""
[ -s "$RAW" ] || die "ffmpeg не записал видео ($(log_tail "$WORK/ffmpeg.log"))"

# --- GIF -----------------------------------------------------------------
# Палитра по всей сцене (palettegen), затем применение с дизерингом Байера:
# для плоского интерфейса это заметно меньше весит, чем покадровый 256-цветный
# GIF, и не даёт «мусора» на мелком тексте.
say "собираю GIF…"
OUTDIR=$(dirname -- "$OUT")
[ -d "$OUTDIR" ] || mkdir -p "$OUTDIR" || die "не создать каталог $OUTDIR"
CHOSEN=""
for width in 960 800 720 640; do
    CAND="$WORK/demo-$width.gif"
    if ! ffmpeg -y -loglevel error -i "$RAW" -filter_complex \
        "fps=$FPS,scale=${width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
        -loop 0 "$CAND" >"$WORK/gif.log" 2>&1; then
        continue
    fi
    SIZE=$(wc -c <"$CAND" | tr -d ' ')
    if [ "$SIZE" -le "$LIMIT" ]; then
        CHOSEN=$CAND
        break
    fi
    say "  ${width} px — $(($SIZE / 1024)) КиБ, больше 2.5 МБ; пробую уже"
    rm -f "$CAND"
done
[ -n "$CHOSEN" ] || die "GIF не влезла в 2.5 МБ даже при ширине 640 px — сократите сцену"

cp "$CHOSEN" "$OUT" || die "не записать $OUT"
SIZE=$(wc -c <"$OUT" | tr -d ' ')
INFO=""
if command -v identify >/dev/null 2>&1; then
    INFO=$(identify -format '%wx%h, кадров %n\n' "$OUT" 2>/dev/null | head -1)
fi
say "GIF записана: $OUT ($(($SIZE / 1024)) КиБ${INFO:+, $INFO})"
exit 0

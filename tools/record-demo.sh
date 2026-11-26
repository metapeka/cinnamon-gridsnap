#!/bin/sh
# tools/record-demo.sh — record the GridSnap demonstration GIF with one command.
#
# Raises its OWN X server (Xvfb :90), muffin, the zone daemon and the editor over a temporary
# `zones.conf`, opens xmessage windows and runs the scene:
#
#   1. the zone editor: the grid, the zone list and the keys;
#   2. a window lands in the `main` zone pixel for pixel (placement through the daemon);
#   3. the picker (`Super+Z`): the grid overlay, Right, Enter — the window is in a zone;
#   4. a second window in the same zone — a tab strip above it, the tab switches;
#   5. the "only this zone" mode: the other window is iconified, a second command brings it back.
#
# The scene is recorded in **English**: `GRIDSNAP_LANG=en` is set by the script itself and the
# decoration windows are titled in English — the recording goes to the README and the site, whose
# main page is English.
#
# The palette is *not* the default one: `tools/scene-palette.py` draws a wallpaper, samples it with
# `zone_theme.from_wallpaper` and writes `theme.conf` next to the temporary `zones.conf`, so the
# editor, the picker and the tab strip show where their colours come from — the same palette the
# still images in `site/assets/` are rendered with.
#
# The scene is written with `ffmpeg -f x11grab` and assembled into a GIF (`palettegen`/
# `paletteuse`). The script picks the GIF width itself: 960 → 800 → 720 → 640 px until the file
# fits in 2.5 MB (below 640 it stops: shortening the scene beats hurting legibility).
#
# State, config, palette and the daemon socket live in a temporary directory (`mktemp -d`), so the
# owner's config, state and sockets are not touched. The temporary directory, Xvfb, muffin, the
# windows and the daemon are removed on the way out — on failure too.
#
#   tools/record-demo.sh                 # write site/assets/demo.gif
#   tools/record-demo.sh /tmp/demo.gif   # another output path
#   tools/record-demo.sh --display :91   # another display (default :90)
#   tools/record-demo.sh --help
#
# Dependencies (Debian): xvfb muffin x11-utils xdotool xmessage ffmpeg
#   python3 python3-gi gir1.2-gtk-3.0 python3-cairo
# `feh` is optional: with it the desktop background is the sampled wallpaper itself, without it
# muffin's default background stays. `imagemagick` is optional too — only the closing `identify`
# line that reports the GIF's size and frame count uses it.
#
# Return code: 0 — the GIF is written; 1 — something did not come up or a scene step did not
# match; 2 — a dependency is missing or the arguments are wrong.

set -u

# muffin (Mutter) picks its backend from logind/$XDG_SESSION_TYPE: without X11 it does not become
# a window manager. See tests/smoke.sh.
export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

# The scene language is English: the recording goes to the README and the site, whose main page is
# English, and an unknown locale already gives English. Pinned so the GIF does not depend on the
# locale of the machine that recorded it.
export GRIDSNAP_LANG=en

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$ROOT/site/assets/demo.gif"
DISP=:90

# Work area without a panel: 1280×800 gives a 960×600 GIF at a 960 px width.
W=1280
H=800
FPS=15
LIMIT=2621440   # 2.5 MB: any larger is no longer "a small picture" for a README
FONT="-*-fixed-medium-r-normal--20-*-*-*-*-*-iso10646-1"
TABS_H=32

usage() {
    cat <<EOF
Record the GridSnap demonstration GIF.

  tools/record-demo.sh [OPTIONS] [FILE.gif]

FILE.gif defaults to site/assets/demo.gif in the repository root. Directories of the path are
created as needed.

Options:
  --display :N   X display to record (default :90; must be free)
  -h, --help     this help

Scene: the zone editor → a window in a zone → tabs → the picker → "only this zone".
Own Xvfb, muffin, daemon and a temporary zones.conf; state and sockets live in a temporary
directory, the owner's settings are not touched. Needs xvfb, muffin, xdotool, xmessage, ffmpeg,
python3 with GTK and cairo.

Return code: 0 — the GIF is written, 1 — the scene did not match, 2 — a dependency is missing or
the arguments are wrong.
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --display)
            [ $# -ge 2 ] || { printf 'record-demo: --display needs a value\n' >&2; exit 2; }
            DISP=$2; shift ;;
        --display=*) DISP=${1#--display=} ;;
        -*) printf 'record-demo: unknown option %s (see --help)\n' "$1" >&2; exit 2 ;;
        *) OUT=$1 ;;
    esac
    shift
done

case $DISP in
    :*) ;;
    *) printf 'record-demo: the display must look like :90, not %s\n' "$DISP" >&2; exit 2 ;;
esac

for c in Xvfb muffin xdotool xmessage ffmpeg python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        printf 'record-demo: %s is missing — install: sudo apt install xvfb muffin x11-utils xdotool xmessage ffmpeg python3 python3-gi gir1.2-gtk-3.0 python3-cairo\n' "$c" >&2
        exit 2
    fi
done

say() { printf '%s\n' "$1"; }
die() { printf 'record-demo: %s\n' "$1" >&2; exit 1; }
log_tail() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

WORK=$(mktemp -d 2>/dev/null || printf '/tmp/gridsnap-demo.%s' "$$")
mkdir -p "$WORK" || { printf 'record-demo: cannot create a temporary directory\n' >&2; exit 1; }
CONF="$WORK/zones.conf"
XDG_CONFIG_HOME="$WORK/config"
XDG_STATE_HOME="$WORK/state"
XDG_RUNTIME_DIR="$WORK/run"
# The scene owns a whole home: `~/.themes/Gridsnap` is the user's theme directory, and the daemon
# would rewrite it here if the owner had a generated one. A sandboxed `HOME` plus a keyfile
# gsettings backend means the scene can write settings and a controls theme without touching the
# live session (the same rule the tests and the smokes follow).
HOME="$WORK/home"
export HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_RUNTIME_DIR
export GSETTINGS_BACKEND=keyfile
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR" "$HOME"
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

# A window by title: XID on stdout.
window_of() {  # $1 = title
    xdotool search --name "$1" 2>/dev/null | head -1
}

wait_window() {  # $1 = title, $2 = attempts of 0.5 s -> XID on stdout
    _left=$2
    while [ "$_left" -gt 0 ]; do
        _id=$(window_of "$1")
        [ -n "$_id" ] && { printf '%s\n' "$_id"; return 0; }
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

# The actual and the expected frame: the same core the tiler uses (`read_frame` with the
# _NET_FRAME_EXTENTS compensation and `Grid.rect`), so the script does not duplicate the zone
# geometry itself. Return 1 — the frame is not the one.
frame_ok() {  # $1 = window, $2 = zone, $3 = tab count
    python3 - "$ROOT" "$CONF" "$1" "$2" "$3" "$TABS_H" <<'PY'
import importlib.util
import os
import sys

root, conf, win = sys.argv[1], sys.argv[2], int(sys.argv[3], 0)
zone, count, height = sys.argv[4], int(sys.argv[5]), int(sys.argv[6])
# The modules import their siblings by bare name: `zone-tiler.py` pulls `zone_i18n` and
# `zone_tabs` in from the repository root, so the root has to be on the path.
sys.path.insert(0, root)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zt = load("zt_record_demo", os.path.join(root, "zone-tiler.py"))
zone_tabs = load("zone_tabs_record_demo", os.path.join(root, "zone_tabs.py"))
grid = zt.Grid(conf)
rect = grid.rect(zone, zt.monitors()[0])
expected = tuple(int(round(v)) for v in zone_tabs.stack_rect(rect, count, height))
actual = zt.read_frame(win, zt.frame_extents(win))
print(f"actual {actual}, expected {expected} ({zone}, {count} tabs)")
sys.exit(0 if actual == expected else 1)
PY
}

# Wait for the frame (the daemon moves windows asynchronously) and log a mismatch.
expect_frame() {  # $1 = window, $2 = zone, $3 = tab count, $4 = attempts
    _left=${4:-20}
    while [ "$_left" -gt 0 ]; do
        if frame_ok "$1" "$2" "$3" >"$WORK/frame.log" 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    printf '  frame mismatch: %s\n' "$(cat "$WORK/frame.log" 2>/dev/null)" >&2
    return 1
}

wm_state() {  # $1 = window -> Normal|Iconic|?
    xprop -id "$1" WM_STATE 2>/dev/null \
        | sed -n 's/.*window state: *\([A-Za-z]*\).*/\1/p' | head -1
}

# An xmessage window: a title and the text of a file. The title shows in the tab strip and in the
# `[rules]` patterns, which is how the windows differ from each other.
#
# The PID goes into `OPEN_PID` rather than onto stdout on purpose: a `$(open_window …)` would run
# the whole function in a subshell, and the background job started there does not survive it —
# dash reports a PID the process never had, the window is gone a second later, and the scene dies
# on "the window did not open" while `ps` shows nothing.
open_window() {  # $1 = title, $2 = file with the text
    DISPLAY="$DISP" xmessage -xrm "*international: True" -xrm "*fontSet: $FONT" \
        -bg "#141a22" -fg "#e8e8ee" -borderwidth 0 \
        -title "$1" -buttons "" "$(cat "$2")" >>"$WORK/windows.log" 2>&1 &
    OPEN_PID=$!
}

# The temporary zone set: two columns, the right one split in two. The widths are fractions of the
# work area, so the scene does not depend on the resolution of the Xvfb screen.
cat >"$CONF" <<'EOF'
# Temporary GridSnap demo config (written by tools/record-demo.sh).
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

# --- scene preparation ---------------------------------------------------
# Window texts: short lines that stay legible in a small GIF.
printf 'Inbox\n\n  November report\n  Standup 10:00\n  GridSnap: demo\n' >"$WORK/mail.txt"
printf 'Calendar\n\n  Mon 10:00 standup\n  Wed 15:00 review\n' >"$WORK/cal.txt"
printf 'Files\n\n  demo.gif\n  zones.conf\n  README.md\n' >"$WORK/files.txt"

# Busy is checked by connecting, not by the lock file: after a crash Xvfb leaved the socket and
# the lock behind, and "busy" by the file would be a lie.
if env DISPLAY="$DISP" xprop -root >/dev/null 2>&1; then
    die "display $DISP is taken — close the extra Xvfb or pass another one via --display"
fi

say "demo: display $DISP, work area ${W}×${H}, ${FPS} frames/s"

Xvfb "$DISP" -screen 0 "${W}x${H}x24" -nolisten tcp >"$WORK/xvfb.log" 2>&1 &
XPID=$!
sleep 0.5
if ! kill -0 "$XPID" 2>/dev/null; then
    die "Xvfb did not start on $DISP: $(log_tail "$WORK/xvfb.log") (a stale /tmp/.X${DISP#:}-lock from a previous run? remove it if Xvfb is not running)"
fi
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    die "Xvfb did not come up on $DISP ($(log_tail "$WORK/xvfb.log"))"
fi

DISPLAY="$DISP" muffin >"$WORK/muffin.log" 2>&1 &
MPID=$!
if ! wait_for 60 sh -c "DISPLAY='$DISP' xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id # 0x'"; then
    die "muffin did not become the window manager within 30 s: $(log_tail "$WORK/muffin.log")"
fi
if ! wait_for 40 sh -c "DISPLAY='$DISP' xprop -root _NET_WORKAREA 2>/dev/null | grep -q '_NET_WORKAREA(CARDINAL)'"; then
    die "muffin has no _NET_WORKAREA — the tiler cannot compute zones"
fi

# The palette: a wallpaper drawn by the shared helper and the theme.conf sampled from it. Written
# next to the temporary zones.conf, so the editor, the picker and the tab strip read it — the
# owner's own theme.conf is never touched.
#
# `--pin-wallpaper` is what keeps the scene deterministic: the daemon refreshes a marked
# `theme.conf` whose recorded source differs from the live `picture-uri`, so without the pin it
# would replace this palette with the one sampled from the owner's own wallpaper on its first poll
# and the GIF would depend on the machine that recorded it. The setting lives in the keyfile
# sandbox above, so nothing outside the temporary directory changes.
python3 "$ROOT/tools/scene-palette.py" --out "$WORK" --width "$W" --height "$H" --pin-wallpaper \
    >"$WORK/palette.log" 2>&1 || die "cannot build the wallpaper palette ($(log_tail "$WORK/palette.log"))"
say "  $(sed -n '1p' "$WORK/palette.log")"

# The background is optional cosmetics: without feh muffin's default background stays.
if command -v feh >/dev/null 2>&1 && [ -s "$WORK/wallpaper.png" ]; then
    DISPLAY="$DISP" feh --bg-fill "$WORK/wallpaper.png" >/dev/null 2>&1 || true
fi

export DISPLAY="$DISP"

# The daemon owns tabs and placement over the socket; it comes up BEFORE the windows.
python3 "$ROOT/zone-daemon.py" --conf "$CONF" --verbose >"$WORK/daemon.log" 2>&1 &
DPID=$!
if ! wait_for 40 test -S "$XDG_RUNTIME_DIR/gridsnap/daemon.sock"; then
    die "the daemon did not raise its socket within 20 s ($(log_tail "$WORK/daemon.log"))"
fi

# The first window is free-standing, and it is opened BEFORE the recording starts: it walks into
# the frame already in place once the editor is minimised. The placement is done by the daemon
# (the same path the applet takes).
open_window "Mail — inbox" "$WORK/mail.txt"
W1PID=$OPEN_PID
W1=$(wait_window "Mail" 40) || die "the «Mail» window did not open ($(log_tail "$WORK/windows.log"))"
xdotool windowmove "$W1" 130 130

python3 "$ROOT/zone-editor.py" --conf "$CONF" >"$WORK/editor.log" 2>&1 &
EPID=$!
EDITOR=$(wait_window "Gridsnap" 60) || die "the zone editor did not open ($(log_tail "$WORK/editor.log"))"
xdotool windowmove "$EDITOR" 70 40
xdotool windowsize "$EDITOR" 1120 700
sleep 1

# --- recording -----------------------------------------------------------
RAW="$WORK/demo.mp4"
say "recording the scene…"
ffmpeg -y -loglevel error -f x11grab -draw_mouse 1 -framerate "$FPS" \
    -video_size "${W}x${H}" -i "${DISP}+0,0" -t 60 \
    -c:v libx264 -preset ultrafast -crf 16 -pix_fmt yuv420p \
    "$RAW" >"$WORK/ffmpeg.log" 2>&1 &
FFPID=$!
sleep 0.8

# 1. The editor: the grid, the zone list, the keys.
sleep 2.2

# 2. The window lands in a zone — through the daemon, the path the applet takes.
xdotool windowminimize "$EDITOR"
sleep 1.0
say "  step 2: a window into the main zone"
sleep 1.2
python3 "$ROOT/gridsnap-ctl" apply main --window "$W1" --quiet >"$WORK/apply.log" 2>&1 \
    || die "cannot place the window in main ($(log_tail "$WORK/apply.log"))"
expect_frame "$W1" main 1 20 || die "the window did not land in the main zone: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.5

# 3. The picker: the overlay, Right (main -> top-right), Enter. The picker comes BEFORE the tabs:
# the tab strip is a window above the overlay and would cover the `main` zone label at the top edge.
say "  step 3: the picker"
open_window "Files" "$WORK/files.txt"
W3PID=$OPEN_PID
W3=$(wait_window "Files" 40) || die "the «Files» window did not open ($(log_tail "$WORK/windows.log"))"
sleep 1.0
python3 "$ROOT/zone-picker.py" --window "$W3" --monitor 0 --select main \
    --conf "$CONF" --quiet >"$WORK/picker.log" 2>&1 &
PICKPID=$!
# The picker window is found by WM_CLASS, not by title: the title is translated and the class of
# the process is not. That way the scene does not depend on the interface language.
if ! wait_for 40 xdotool search --classname zone-picker.py; then
    die "the picker overlay did not appear ($(log_tail "$WORK/picker.log"))"
fi
sleep 1.5            # the overlay needs a moment to take the keyboard
xdotool key Right
sleep 1.2
xdotool key Return
wait "$PICKPID" 2>/dev/null
RC=$?
PICKPID=""
[ "$RC" -eq 0 ] || die "the picker exited with $RC ($(log_tail "$WORK/picker.log"))"
expect_frame "$W3" top-right 1 20 || die "the picker did not place the window in top-right: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.5

# 4. A second window in the same zone: the daemon's rules turn it into a tab.
say "  step 4: tabs"
open_window "Calendar" "$WORK/cal.txt"
W2PID=$OPEN_PID
W2=$(wait_window "Calendar" 40) || die "the «Calendar» window did not open ($(log_tail "$WORK/windows.log"))"
expect_frame "$W2" main 2 40 || die "the second window did not become a tab: $(cat "$WORK/frame.log" 2>/dev/null)"
expect_frame "$W1" main 2 20 || die "the first tab was not moved to the shared frame: $(cat "$WORK/frame.log" 2>/dev/null)"
sleep 1.8
python3 "$ROOT/zone-tiler.py" tabs-next --conf "$CONF" >"$WORK/tabs-next.log" 2>&1 \
    || die "tabs-next failed ($(log_tail "$WORK/tabs-next.log"))"
sleep 1.5

# 5. "Only this zone": the other window is iconified, a second command brings it back.
say "  step 5: only this zone"
python3 "$ROOT/gridsnap-ctl" focus main --window "$W1" --quiet >"$WORK/focus.log" 2>&1 \
    || die "the «only this zone» mode did not turn on ($(log_tail "$WORK/focus.log"))"
sleep 1.8
[ "$(wm_state "$W3")" = "Iconic" ] || die "the other window was not iconified by the mode ($(wm_state "$W3"))"
python3 "$ROOT/gridsnap-ctl" focus --off --quiet >"$WORK/focus-off.log" 2>&1 \
    || die "the mode did not turn off ($(log_tail "$WORK/focus-off.log"))"
sleep 1.8
[ "$(wm_state "$W3")" = "Normal" ] || die "the window did not come back after the mode ($(wm_state "$W3"))"

kill -INT "$FFPID" 2>/dev/null
wait "$FFPID" 2>/dev/null
FFPID=""
[ -s "$RAW" ] || die "ffmpeg recorded nothing ($(log_tail "$WORK/ffmpeg.log"))"

# --- GIF -----------------------------------------------------------------
# One palette for the whole scene (palettegen), then applied with Bayer dithering: for a flat
# interface that weighs noticeably less than a per-frame 256-colour GIF, and it leaves no speckle
# on small text.
say "assembling the GIF…"
OUTDIR=$(dirname -- "$OUT")
[ -d "$OUTDIR" ] || mkdir -p "$OUTDIR" || die "cannot create the directory $OUTDIR"
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
    say "  ${width} px — $(($SIZE / 1024)) KiB, over 2.5 MB; trying narrower"
    rm -f "$CAND"
done
[ -n "$CHOSEN" ] || die "the GIF does not fit in 2.5 MB even at 640 px — shorten the scene"

cp "$CHOSEN" "$OUT" || die "cannot write $OUT"
SIZE=$(wc -c <"$OUT" | tr -d ' ')
INFO=""
if command -v identify >/dev/null 2>&1; then
    INFO=$(identify -format '%wx%h, %n frames\n' "$OUT" 2>/dev/null | head -1)
fi
say "GIF written: $OUT ($(($SIZE / 1024)) KiB${INFO:+, $INFO})"
exit 0

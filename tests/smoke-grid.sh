#!/bin/sh
# Smoke test for the desktop grid overlay (`zone-picker.py --grid` / `--toggle`).
#
# It raises its own X server (Xvfb :96), a white window that plays the desktop and a
# compositor (picom), then checks what the overlay is for:
#
#   * the grid is drawn OVER the desktop: the window shows through everywhere except the
#     frames and the label plates (a pixel inside a zone equals the window's colour);
#   * the zone under the pointer is the only tinted one, and the tint follows the pointer;
#   * without a compositor the grid is refused instead of covering the screen;
#   * `--toggle` starts the grid, writes one pid file per desktop, and the second call
#     stops it — window, process and pid file gone.
#
# The compositor is not an optional decoration here: without one the grid window has no
# alpha visual, and "transparent" becomes opaque black. That is why the refusal is checked
# first, on the bare Xvfb screen.
#
#   tests/smoke-grid.sh
#
# Dependencies (Debian): xvfb picom x11-utils xdotool python3 python3-gi gir1.2-gtk-3.0
# Return code: 0 — everything matched, 1 — a mismatch, 2 — a dependency is missing.

set -u

export XDG_SESSION_TYPE=x11
# The interface is checked in the source language: the message of the refusal is compared.
GRIDSNAP_LANG=en
export GRIDSNAP_LANG
unset WAYLAND_DISPLAY 2>/dev/null || true

DISP=":96"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PICKER="$ROOT/zone-picker.py"

LOGDIR=$(mktemp -d 2>/dev/null || echo /tmp)
# A config of the scenario's own: three zones that tile the screen, so "the pixel belongs to
# exactly one zone" holds without overlaps (`bottom` in the shipped layout overlaps `main`).
CONF="$LOGDIR/zones.conf"
cat >"$CONF" <<'CONF'
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.6, 1
side = 0.6, 0, 0.4, 0.5
below = 0.6, 0.5, 0.4, 0.5
CONF
export XDG_STATE_HOME="$LOGDIR/state"
export XDG_CONFIG_HOME="$LOGDIR/config"
mkdir -p "$XDG_CONFIG_HOME"
# The pid file lives in the scenario's runtime directory: the owner's live grid and the
# daemon socket in the real one are never touched.
RUNTIME="$LOGDIR/run"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
export XDG_RUNTIME_DIR="$RUNTIME"
PIDFILE="$RUNTIME/gridsnap/grid-0.pid"

fails=0
say() { printf '%s\n' "$1"; }
ok()  { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✖\033[0m %s\n' "$1"; fails=$((fails + 1)); }

# Wait up to $1 half-seconds for the command in the rest of the line to succeed.
wait_for() {
    _left=$1; shift
    while [ "$_left" -gt 0 ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
        _left=$((_left - 1))
    done
    return 1
}

for c in Xvfb picom xdotool xmessage python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        say "no $c: sudo apt install xvfb picom x11-utils xdotool python3 python3-gi gir1.2-gtk-3.0"
        exit 2
    fi
done
if ! python3 -c 'import gi; gi.require_version("Gdk", "3.0")' >/dev/null 2>&1; then
    say "python3 cannot import Gdk: sudo apt install python3-gi gir1.2-gtk-3.0"
    exit 2
fi

# ---- the helpers the scenario needs in a display

cat >"$LOGDIR/composited.py" <<'PY'
import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402

screen = Gdk.DisplayManager.get().get_default_display().get_default_screen()
raise SystemExit(0 if screen.is_composited() else 1)
PY
cat >"$LOGDIR/pixel.py" <<'PY'
import sys

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402

x, y = int(sys.argv[1]), int(sys.argv[2])
pixbuf = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x, y, 1, 1)
if pixbuf is None:
    raise SystemExit(1)
pixels = pixbuf.get_pixels()
print(pixels[0], pixels[1], pixels[2])
PY
composited() { python3 "$LOGDIR/composited.py"; }
pixel() { python3 "$LOGDIR/pixel.py" "$1" "$2"; }
grid_window() { xdotool search --name 'Gridsnap - zones' >/dev/null 2>&1; }

XPID=""; CPID=""; WPID=""; GPID=""
cleanup() {
    trap - EXIT INT TERM
    [ -n "$GPID" ] && kill "$GPID" 2>/dev/null
    [ -n "$CPID" ] && kill "$CPID" 2>/dev/null
    [ -n "$WPID" ] && kill "$WPID" 2>/dev/null
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 0.2
    rm -rf "$LOGDIR"
    return 0
}
trap cleanup EXIT INT TERM

say "grid: display $DISP, config $CONF"
Xvfb "$DISP" -screen 0 1280x800x24 >"$LOGDIR/xvfb.log" 2>&1 &
XPID=$!
if ! wait_for 20 sh -c "DISPLAY=$DISP xdpyinfo >/dev/null 2>&1"; then
    say "Xvfb did not start ($(tail -2 "$LOGDIR/xvfb.log" 2>/dev/null | tr '\n' ' '))"
    exit 1
fi
export DISPLAY="$DISP"

# The "desktop": one white window over the whole sample area, mapped before the grid.
xmessage -geometry 1000x700+0+0 -bg '#ffffff' -fg '#ffffff' ' ' >"$LOGDIR/xmessage.log" 2>&1 &
WPID=$!
wait_for 20 xdotool search --class Xmessage

# ---- without a compositor the grid refuses instead of covering the screen
say "without a compositor:"
out=$(python3 "$PICKER" --grid --conf "$CONF" --workspace 0 2>&1 >/dev/null)
rc=$?
if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q 'compositing'; then
    ok "refused with code 1 and named the compositor"
else
    bad "expected a refusal naming the compositor, got rc=$rc: $out"
fi
if [ -e "$PIDFILE" ]; then bad "a refused grid left a pid file"; else ok "no pid file left behind"; fi

picom --backend xrender --config /dev/null >"$LOGDIR/picom.log" 2>&1 &
CPID=$!
if ! wait_for 20 composited; then
    say "the compositor did not take over the screen ($(tail -2 "$LOGDIR/picom.log" 2>/dev/null | tr '\n' ' '))"
    exit 1
fi
ok "compositor running"

# ---- the grid over the desktop
say "the grid over the desktop:"
python3 "$PICKER" --grid --conf "$CONF" --workspace 0 >"$LOGDIR/grid.log" 2>&1 &
GPID=$!
if ! wait_for 30 grid_window; then
    bad "the grid window did not appear: $(tail -3 "$LOGDIR/grid.log" 2>/dev/null | tr '\n' ' ')"
    say "grid smoke failed."
    exit 1
fi
geometry=$(xdotool search --name 'Gridsnap - zones' getwindowgeometry %@ 2>/dev/null \
    | sed -n 's/.*Geometry: \(.*\)/\1/p' | head -1)
if [ "$geometry" = "1280x800" ]; then
    ok "one window over the whole work area ($geometry)"
else
    bad "the grid window is $geometry, expected 1280x800"
fi

xdotool mousemove 900 200   # the pointer in `side`; `main` stays unselected
sleep 0.8
base=$(pixel 300 500)
if [ "$base" = "255 255 255" ]; then
    ok "the desktop shows through the grid (300,500 = $base)"
else
    bad "the desktop does not show through: (300,500) = $base, expected 255 255 255"
fi
frame=$(pixel 2 500)
if [ "$frame" != "255 255 255" ]; then
    ok "a zone frame is drawn over the window ((2,500) = $frame)"
else
    bad "no frame at the left edge of main"
fi

xdotool mousemove 300 500   # into `main`
sleep 0.8
selected=$(pixel 300 500)
untouched=$(pixel 900 200)
if [ "$selected" != "$base" ]; then
    ok "the zone under the pointer is tinted ((300,500): $base -> $selected)"
else
    bad "the zone under the pointer was not tinted: (300,500) = $selected"
fi
if [ "$untouched" = "$base" ]; then
    ok "the other zones stay clear ((900,200) = $untouched)"
else
    bad "another zone got tinted: (900,200) = $untouched, expected $base"
fi

# ---- a still of the grid: a transparent PNG of the work area
say "the grid snapshot:"
if python3 "$PICKER" --grid --shot "$LOGDIR/grid.png" --conf "$CONF" --workspace 0 \
        >"$LOGDIR/shot.log" 2>&1; then
    size=$(python3 - "$LOGDIR/grid.png" <<'PY'
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

pixbuf = GdkPixbuf.Pixbuf.new_from_file(sys.argv[1])
print(f"{pixbuf.get_width()}x{pixbuf.get_height()}")
PY
)
    if [ "$size" = "1280x800" ]; then
        ok "a transparent still of the whole work area ($size)"
    else
        bad "the grid still is $size, expected 1280x800"
    fi
else
    bad "the grid snapshot failed: $(tail -2 "$LOGDIR/shot.log" 2>/dev/null | tr '\n' ' ')"
fi

kill "$GPID" 2>/dev/null
GPID=""
wait_for 20 sh -c "! xdotool search --name 'Gridsnap - zones' >/dev/null 2>&1"

# ---- the toggle
say "the toggle:"
python3 "$PICKER" --toggle --conf "$CONF" --workspace 0 --quiet
rtc=$?
if [ "$rtc" = 0 ] && wait_for 30 grid_window; then
    ok "the first call shows the grid"
else
    bad "the first toggle did not show the grid (rc=$rtc)"
fi
pid=$(cat "$PIDFILE" 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    ok "the pid file names the running grid ($pid)"
else
    bad "the pid file is missing or names nothing: '$pid'"
fi
python3 "$PICKER" --toggle --conf "$CONF" --workspace 0 --quiet
rtc=$?
if [ "$rtc" = 0 ] && wait_for 20 sh -c "! xdotool search --name 'Gridsnap - zones' >/dev/null 2>&1"; then
    ok "the second call hides it"
else
    bad "the second toggle did not hide the grid (rc=$rtc)"
fi
if [ -e "$PIDFILE" ]; then bad "the pid file survived the second toggle"; else ok "the pid file is gone"; fi
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    bad "the grid process $pid survived the second toggle"
else
    ok "the grid process is gone"
fi

if [ "$fails" -eq 0 ]; then
    say "the desktop grid smoke passed."
    exit 0
fi
say "the desktop grid smoke failed: $fails mismatch(es)."
exit 1

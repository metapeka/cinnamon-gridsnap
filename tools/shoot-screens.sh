#!/bin/sh
# tools/shoot-screens.sh — re-render the screenshots the README and the site show.
#
# The site (three hand-written pages) and the three READMEs carry two still images of the themed
# surfaces: the editor (canvas, zone list, the GTK sheet from the palette) and the picker overlay.
# They are produced here, not by hand, so a change of the palette or of a surface is one command
# away from a current picture.
#
# The palette is deliberately *not* the default one: the images show where the colours come from.
# `tools/scene-palette.py` draws a wallpaper, samples it with `zone_theme.from_wallpaper` and writes
# `theme.conf` with the generated marker — the same path the editor's **Theme → From the
# wallpaper…** action takes.
#
# Both surfaces render offscreen (`--shot`), so no window manager is needed: an own Xvfb display
# with a fixed screen size (`:91`, 1280×800) gives the shots a stable geometry. `GRIDSNAP_LANG=en`
# keeps the picture in the source language, as the English pages are the product's face.
#
# State, config and sockets live in a temporary directory; the owner's config, state and
# `~/.themes/Gridsnap` are never touched.
#
#   tools/shoot-screens.sh              # re-render site/assets/editor.png and picker.png
#   tools/shoot-screens.sh --display :92
#   tools/shoot-screens.sh --help
#
# Dependencies (Debian): xvfb python3 python3-gi gir1.2-gtk-3.0 python3-cairo
#
# Return code: 0 — both images written; 1 — a render failed; 2 — a dependency is missing or the
# arguments are wrong.

set -u

export XDG_SESSION_TYPE=x11
unset WAYLAND_DISPLAY 2>/dev/null || true

# The scene language is English: the images go to the English pages, and an unknown locale
# already gives English. Pinned so the shots do not depend on the machine's locale.
export GRIDSNAP_LANG=en

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
DISP=:91
W=1280
H=800

usage() {
    cat <<EOF
Re-render the screenshots in site/assets/.

  tools/shoot-screens.sh [OPTIONS]

Options:
  --display :N   X display for rendering (default :91; must be free)
  -h, --help     this help

Writes site/assets/editor.png (1140×720) and site/assets/picker.png (${W}×${H}) from a
wallpaper-derived palette: an own Xvfb, a temporary zones.conf and theme.conf, the
editor and the picker rendered offscreen. Nothing of the owner's is read or written.

Return code: 0 — both images written; 1 — a render failed; 2 — a dependency is missing
or the arguments are wrong.
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --display)
            [ $# -ge 2 ] || { printf 'shoot-screens: --display needs a value\n' >&2; exit 2; }
            DISP=$2; shift ;;
        --display=*) DISP=${1#--display=} ;;
        -*) printf 'shoot-screens: unknown option %s (see --help)\n' "$1" >&2; exit 2 ;;
        *) printf 'shoot-screens: unexpected argument %s (see --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

case $DISP in
    :*) ;;
    *) printf 'shoot-screens: the display must look like :91, not %s\n' "$DISP" >&2; exit 2 ;;
esac

for c in Xvfb xprop python3; do
    if ! command -v "$c" >/dev/null 2>&1; then
        printf 'shoot-screens: %s is missing — install: sudo apt install xvfb x11-utils python3 python3-gi gir1.2-gtk-3.0 python3-cairo\n' "$c" >&2
        exit 2
    fi
done

say() { printf '%s\n' "$1"; }
die() { printf 'shoot-screens: %s\n' "$1" >&2; exit 1; }
log_tail() { tail -3 "$1" 2>/dev/null | tr '\n' ' '; }

WORK=$(mktemp -d 2>/dev/null || printf '/tmp/gridsnap-shots.%s' "$$")
mkdir -p "$WORK" || { printf 'shoot-screens: cannot create a temporary directory\n' >&2; exit 1; }
CONF="$WORK/zones.conf"
export XDG_CONFIG_HOME="$WORK/config"
export XDG_STATE_HOME="$WORK/state"
export XDG_RUNTIME_DIR="$WORK/run"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

XPID=""
cleanup() {
    trap - EXIT INT TERM
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    sleep 0.2
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

# The zone set of the scene: two columns, the right one split in two. Fractions of the work area,
# so the picture does not depend on the screen size beyond the one the script pins.
cat >"$CONF" <<'EOF'
# Temporary config of the screenshots (written by tools/shoot-screens.sh).
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.62, 1
side = 0.62, 0, 0.38, 0.5
bottom = 0.62, 0.5, 0.38, 0.5
EOF

# Busy is checked by connecting, not by the lock file: after a crash Xvfb leaves a socket behind
# and "busy" by the file would be a lie.
if env DISPLAY="$DISP" xprop -root >/dev/null 2>&1; then
    die "display $DISP is taken — close the extra Xvfb or pass another one via --display"
fi

say "screenshots: display $DISP, work area ${W}×${H}"

# The wallpaper and the palette it yields, next to the temporary zones.conf so the editor and the
# picker read it.
python3 "$ROOT/tools/scene-palette.py" --out "$WORK" --width "$W" --height "$H" \
    >"$WORK/palette.log" 2>&1 || die "cannot build the wallpaper palette ($(log_tail "$WORK/palette.log"))"
say "  $(sed -n '1p' "$WORK/palette.log")"

Xvfb "$DISP" -screen 0 "${W}x${H}x24" -nolisten tcp >"$WORK/xvfb.log" 2>&1 &
XPID=$!
if ! wait_for 40 env DISPLAY="$DISP" xprop -root; then
    die "Xvfb did not start on $DISP ($(log_tail "$WORK/xvfb.log")) — a stale lock in /tmp/.X${DISP#:}-lock?"
fi

# The editor's snapshot asks for 1140×720 itself (it is an offscreen window of that size);
# the picker draws the work area of the monitor, hence the screen size above.
say "rendering the editor…"
env DISPLAY="$DISP" python3 "$ROOT/zone-editor.py" --conf "$CONF" \
    --shot "$WORK/editor.png" >"$WORK/editor.log" 2>&1 \
    || die "the editor snapshot failed ($(log_tail "$WORK/editor.log"))"

say "rendering the picker…"
env DISPLAY="$DISP" python3 "$ROOT/zone-picker.py" --conf "$CONF" \
    --shot "$WORK/picker.png" >"$WORK/picker.log" 2>&1 \
    || die "the picker snapshot failed ($(log_tail "$WORK/picker.log"))"

OUTDIR="$ROOT/site/assets"
[ -d "$OUTDIR" ] || mkdir -p "$OUTDIR" || die "cannot create $OUTDIR"
for name in editor picker; do
    [ -s "$WORK/$name.png" ] || die "$name.png was not written"
    cp "$WORK/$name.png" "$OUTDIR/$name.png" || die "cannot write $OUTDIR/$name.png"
    say "  $OUTDIR/$name.png ($(wc -c <"$WORK/$name.png" | tr -d ' ') bytes)"
done
say "screenshots written: editor.png, picker.png"
exit 0

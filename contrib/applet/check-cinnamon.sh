#!/bin/bash
# Проверка апплета GridSnap на живом Cinnamon в отдельном Xvfb + приватном dbus.
# Ничего в сессии владельца не трогает: свои XDG_*, свой dbus-run-session.
#
# Запуск:  bash contrib/applet/check-cinnamon.sh
# Требуется: cinnamon, Xvfb (xvfb-run), dbus-run-session, ImageMagick (import),
# python3. Скрипт складывает лог и снимок панели во временный каталог и печатает
# его путь в конце.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

ROOT="$(mktemp -d /tmp/gridsnap-applet-test.XXXXXX)"
DATA="$ROOT/data"
CONF="$ROOT/config"
mkdir -p "$DATA/cinnamon/applets" "$CONF" "$ROOT/cache" "$ROOT/runtime"
chmod 700 "$ROOT/runtime"
cp -r "$HERE/gridsnap@metapeka" "$DATA/cinnamon/applets/"

# Предустановим надпись в панели, чтобы её было видно на снимке.
# Имя файла зависит от версии Cinnamon: одни берут <instance>.json (0.json),
# другие — <uuid>.json, поэтому пишем оба варианта.
SPICE="$CONF/cinnamon/spices/gridsnap@metapeka"
mkdir -p "$SPICE"
for name in 0.json "gridsnap@metapeka.json"; do
    cat > "$SPICE/$name" <<'JSON'
{"use-label": {"value": true}, "label-text": {"value": "GridSnap"}}
JSON
done

export XDG_DATA_HOME="$DATA"
export XDG_CONFIG_HOME="$CONF"
export XDG_CACHE_HOME="$ROOT/cache"
export XDG_CONFIG_DIRS="/etc/xdg"
export XDG_DATA_DIRS="/usr/local/share:/usr/share"
export XDG_RUNTIME_DIR="$ROOT/runtime"
export GSETTINGS_BACKEND=keyfile
export NO_AT_BRIDGE=1

echo "== включённые апплеты =="
gsettings set org.cinnamon enabled-applets "['panel1:left:0:gridsnap@metapeka:0']"
gsettings get org.cinnamon enabled-applets

LOG="$ROOT/cinnamon.log"
cinnamon --replace > "$LOG" 2>&1 &
CPID=$!
echo "cinnamon pid=$CPID"

sleep 25
if ! kill -0 "$CPID" 2>/dev/null; then
    echo "!! Cinnamon завершился сам; лог:"
    tail -40 "$LOG"
    echo "ROOT=$ROOT"
    exit 1
fi

echo "== файл настроек апплета =="
# Файл, который читает Cinnamon этой версии (<uuid>.json), либо старый <instance>.json.
SETTINGS="$CONF/cinnamon/spices/gridsnap@metapeka/gridsnap@metapeka.json"
[ -f "$SETTINGS" ] || SETTINGS="$CONF/cinnamon/spices/gridsnap@metapeka/0.json"
if [ -f "$SETTINGS" ]; then
    echo "есть: $SETTINGS"
    python3 -c "import json;d=json.load(open('$SETTINGS'));print('label =',d['label-text']['value'],'| use-label =',d['use-label']['value'])"
else
    echo "нет: $SETTINGS"
fi

echo "== ошибки, связанные с апплетом, в логе =="
if grep -nE "gridsnap|Failed to evaluate|AppletManager|JS ERROR|Traceback" "$LOG"; then
    true
else
    echo "(совпадений нет)"
fi

echo "== Eval: список апплетов на панели (через org.Cinnamon) =="
CINNAMON_PID=$CPID python3 - <<'PY'
import os, re, subprocess, sys

pid = int(os.environ["CINNAMON_PID"])


def gdbus(*args):
    return subprocess.run(["gdbus"] + list(args), capture_output=True, text=True).stdout


names = re.findall(
    r"'([^']+)'",
    gdbus("call", "--session", "--dest", "org.freedesktop.DBus",
          "--object-path", "/org/freedesktop/DBus",
          "--method", "org.freedesktop.DBus.ListNames"),
)
target = None
for name in names:
    out = gdbus("call", "--session", "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus",
                "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", name)
    m = re.search(r"uint32 (\d+)", out)
    if m and int(m.group(1)) == pid:
        target = name
        break

if not target:
    print("не нашёл D-Bus-соединение Cinnamon")
    sys.exit(1)

code = ("(function(){var r=[];imports.ui.main.panelManager.panels.forEach("
        "function(p){['_leftBox','_centerBox','_rightBox'].forEach(function(b){"
        "p[b].get_children().forEach(function(a){if(a._delegate&&a._delegate._uuid)"
        "r.push(a._delegate._uuid+':'+a._delegate.instance_id);});});});return r;})()")
res = gdbus("call", "--session", "--dest", target, "--object-path", "/org/Cinnamon",
            "--method", "org.Cinnamon.Eval", code)
print("Eval:", res.strip())
PY

echo "== снимок панели =="
sleep 1
if import -window root "$ROOT/panel.png" 2>"$ROOT/import.err"; then
    echo "снимок: $ROOT/panel.png"
else
    echo "import не смог:"
    cat "$ROOT/import.err"
fi

kill "$CPID" 2>/dev/null
sleep 1
kill -9 "$CPID" 2>/dev/null
echo "ROOT=$ROOT"

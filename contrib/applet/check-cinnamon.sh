#!/bin/bash
# Проверка апплета GridSnap на живом Cinnamon в отдельном Xvfb + приватном dbus.
# Ничего в сессии владельца не трогает: свои XDG_*, свой dbus-run-session.
#
# Запуск:  xvfb-run -s "-screen 0 1920x1080x24" bash contrib/applet/check-cinnamon.sh
#          xvfb-run -s "-screen 0 1920x1080x24" bash contrib/applet/check-cinnamon.sh --daemon
#
# Без ключей — апплет загружается, ошибок нет, индикатор живёт опросом (демона нет).
# С `--daemon` скрипт поднимает ещё и настоящий `zone-daemon.py` на том же
# XDG_RUNTIME_DIR, что у Cinnamon (конфиг — свой, во временном каталоге), и
# показывает, что апплет подписался на события: окно, появившееся по правилу,
# меняет надпись в панели без ожидания опроса.
#
# Требуется: cinnamon, xvfb-run (Xvfb), dbus-run-session, ImageMagick (import),
# x11-utils (xprop), xmessage (пакет x11-utils? нет — x11-apps), python3.
# Скрипт складывает лог, журнал вызовов `gridsnap-ctl` и снимок панели во
# временный каталог и печатает его путь в конце.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOTDIR="$(cd "$HERE/../.." && pwd)"

DAEMON=0
for arg in "$@"; do
    case "$arg" in
        --daemon) DAEMON=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $arg (есть только --daemon)" >&2; exit 2 ;;
    esac
done

ROOT="$(mktemp -d /tmp/gridsnap-applet-test.XXXXXX)"
DATA="$ROOT/data"
CONF="$ROOT/config"
GS="$ROOT/gs"
mkdir -p "$DATA/cinnamon/applets" "$CONF" "$ROOT/cache" "$ROOT/runtime" "$ROOT/state" "$GS"
chmod 700 "$ROOT/runtime"
cp -r "$HERE/gridsnap@metapeka" "$DATA/cinnamon/applets/"

# Копия кода GridSnap: апплету указываем её в настройке «каталог GridSnap», а
# `gridsnap-ctl` подменяем обёрткой, которая пишет вызовы в журнал. Так видно,
# что на каждый опрос индикатор больше не зовёт `gridsnap-ctl status`.
tar -C "$ROOTDIR" --exclude=.git -cf - . 2>/dev/null | tar -x -C "$GS" 2>/dev/null
cat > "$GS/gridsnap-ctl" <<PY
#!/usr/bin/env python3
"""Обёртка gridsnap-ctl для проверки: журнал вызовов, потом настоящий клиент."""
import os
import sys

log = os.environ.get("GRIDSNAP_CTL_LOG", "")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")

REAL = os.path.join("$ROOTDIR", "gridsnap-ctl")
os.execv(sys.executable, [sys.executable, REAL] + sys.argv[1:])
PY
chmod +x "$GS/gridsnap-ctl"

# Журнал вызовов `gridsnap-ctl` из апплета (путь — в переменной окружения
# Cinnamon, наследники её видят).
CTL_LOG="$ROOT/ctl.log"
: > "$CTL_LOG"

# Предустановим надпись в панели, чтобы её было видно на снимке, и каталог
# GridSnap — копию кода из этого репозитория.
# Имя файла зависит от версии Cinnamon: одни берут <instance>.json (0.json),
# другие — <uuid>.json, поэтому пишем оба варианта.
SPICE="$CONF/cinnamon/spices/gridsnap@metapeka"
mkdir -p "$SPICE"
for name in 0.json "gridsnap@metapeka.json"; do
    cat > "$SPICE/$name" <<JSON
{"use-label": {"value": true}, "label-text": {"value": "GridSnap"}, "gridsnap-dir": {"value": "$GS"}}
JSON
done

export XDG_DATA_HOME="$DATA"
export XDG_CONFIG_HOME="$CONF"
export XDG_CACHE_HOME="$ROOT/cache"
export XDG_CONFIG_DIRS="/etc/xdg"
export XDG_DATA_DIRS="/usr/local/share:/usr/share"
export XDG_RUNTIME_DIR="$ROOT/runtime"
export XDG_STATE_HOME="$ROOT/state"
export GSETTINGS_BACKEND=keyfile
export GRIDSNAP_CTL_LOG="$CTL_LOG"
export NO_AT_BRIDGE=1

echo "== включённые апплеты =="
gsettings set org.cinnamon enabled-applets "['panel1:left:0:gridsnap@metapeka:0']"
gsettings get org.cinnamon enabled-applets

LOG="$ROOT/cinnamon.log"
cinnamon --replace > "$LOG" 2>&1 &
CPID=$!
echo "cinnamon pid=$CPID"

# Демон — сразу после Cinnamon: апплет подписывается на его сокет при загрузке.
# Конфиг свой (во временном каталоге), состояние — в XDG_STATE_HOME выше.
DPID=""
XPID=""
if [ "$DAEMON" = "1" ]; then
    ZONES="$ROOT/zones.conf"
    cat > "$ZONES" <<'ZONES'
# Конфиг проверки апплета: окно с нашим заголовком уезжает в зону right.
[grid]
cols = 1.0
rows = 1.0

[zones]
main = 0, 0, 0.5, 1
right = 0.5, 0, 0.5, 1

[rules]
re:gridsnap-applet-check$ = right
ZONES
    python3 "$ROOTDIR/zone-daemon.py" --conf "$ZONES" --verbose \
        > "$ROOT/daemon.log" 2>&1 &
    DPID=$!
    echo "daemon pid=$DPID, лог $ROOT/daemon.log"
    left=60
    while [ "$left" -gt 0 ] && [ ! -S "$ROOT/runtime/gridsnap/daemon.sock" ]; do
        sleep 0.5
        left=$((left - 1))
    done
    if [ -S "$ROOT/runtime/gridsnap/daemon.sock" ]; then
        echo "сокет демона: есть ($ROOT/runtime/gridsnap/daemon.sock)"
    else
        echo "!! демон не поднял сокет; его вывод:"
        tail -20 "$ROOT/daemon.log"
    fi
fi

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
    python3 -c "import json;d=json.load(open('$SETTINGS'));print('label =',d['label-text']['value'],'| use-label =',d['use-label']['value'],'| dir =',d['gridsnap-dir']['value'])"
else
    echo "нет: $SETTINGS"
fi

# Выполнить JS внутри Cinnamon и напечатать результат: так видно, что апплет
# на самом деле показывает и знает.
eval_cinnamon() {  # $1 = выражение JS -> строка результата
    CINNAMON_PID=$CPID JS="$1" python3 - <<'PY'
import os, re, subprocess, sys

pid = int(os.environ["CINNAMON_PID"])
code = os.environ["JS"]


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

res = gdbus("call", "--session", "--dest", target, "--object-path", "/org/Cinnamon",
            "--method", "org.Cinnamon.Eval", code)
print("Eval:", res.strip())
PY
}

# Снимок состояния апплета: что в надписи, что в карте «окно -> зона» и жива
# ли подписка. Одним выражением, чтобы не гонять gdbus несколько раз.
# Переводы строк — через String.fromCharCode(10): gdbus разбирает аргумент как
# текст GVariant и съел бы `\n` внутри JS-строки (а живая строка в кавычках —
# уже синтаксическая ошибка).
read -r -d '' APPLETS_JS <<'JS'
(function(){var r=[];imports.ui.main.panelManager.panels.forEach(
function(p){['_leftBox','_centerBox','_rightBox'].forEach(function(b){
p[b].get_children().forEach(function(a){if(a._delegate&&a._delegate._uuid)
r.push(a._delegate._uuid+':'+a._delegate.instance_id);});});});return r;})()
JS
read -r -d '' STATE_JS <<'JS'
(function(){var r=[];imports.ui.main.panelManager.panels.forEach(
function(p){['_leftBox','_centerBox','_rightBox'].forEach(function(b){
p[b].get_children().forEach(function(a){var d=a._delegate;
if(!d||d._uuid!=='gridsnap@metapeka')return;
r.push('надпись = '+JSON.stringify(d._applet_label.get_text())
+' | зона = '+JSON.stringify(d._currentZone)
+' | активное окно = '+JSON.stringify(d._activeWindow)
+' | подписка жива = '+d._watchLive
+' | карта = '+JSON.stringify(Array.from(d._zones.entries())));});});});return r.join(String.fromCharCode(10));})()
JS

echo "== ошибки, связанные с апплетом, в логе =="
if grep -nE "gridsnap|Failed to evaluate|AppletManager|JS ERROR|Traceback" "$LOG"; then
    true
else
    echo "(совпадений нет)"
fi

echo "== Eval: список апплетов на панели (через org.Cinnamon) =="
eval_cinnamon "$APPLETS_JS"

echo "== Eval: что показывает апплет =="
eval_cinnamon "$STATE_JS"

echo "== журнал вызовов gridsnap-ctl из апплета =="
cat "$CTL_LOG"

if [ "$DAEMON" = "1" ]; then
    echo "== процесс(ы) watch, запущенные апплетом =="
    ps -eo pid,ppid,args | grep '[g]ridsnap-ctl watch' || echo "(процесса watch нет)"
fi

# Окно проверки. С демоном оно уезжает по правилу в зону right и даёт событие
# `window-appeared`; без демона — остаётся без зоны, и видно запасной путь.
echo "== окно проверки (xmessage) =="
: > "$CTL_LOG"
xmessage -title gridsnap-applet-check -buttons OK:0 "окно проверки апплета" \
    >/dev/null 2>&1 &
XPID=$!
sleep 4
echo "-- Eval после появления окна --"
eval_cinnamon "$STATE_JS"
echo "-- журнал вызовов gridsnap-ctl за это время (пусто = status не звали) --"
cat "$CTL_LOG"
if [ "$DAEMON" = "1" ]; then
    echo "-- журнал демона (хвост) --"
    tail -8 "$ROOT/daemon.log"
fi
kill "$XPID" 2>/dev/null
XPID=""
sleep 3
echo "-- Eval после закрытия окна --"
eval_cinnamon "$STATE_JS"

echo "== снимок панели =="
sleep 1
if import -window root "$ROOT/panel.png" 2>"$ROOT/import.err"; then
    echo "снимок: $ROOT/panel.png"
else
    echo "import не смог:"
    cat "$ROOT/import.err"
fi

[ -n "$XPID" ] && kill "$XPID" 2>/dev/null
[ -n "$DPID" ] && kill "$DPID" 2>/dev/null
kill "$CPID" 2>/dev/null
sleep 1
kill -9 "$CPID" 2>/dev/null
echo "ROOT=$ROOT"

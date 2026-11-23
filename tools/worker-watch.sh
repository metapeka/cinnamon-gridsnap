#!/bin/sh
# worker-watch.sh — следить за воркерами Orca, не давая себе молча ждать.
#
#   tools/worker-watch.sh [--stall SEC] [--deadline SEC] DISPATCH_ID...
#   tools/worker-watch.sh --all          # все, кто ещё не закончил
#
# Dispatch id печатает `worker-start` в поле dispatchId (ctx_...): Orca не умеет
# перечислить воркеров прогона, поэтому id идут в командной строке. `--all`
# берёт их из `worker-list` — удобно, когда после спавна сразу включаешь надзор.
#
# Смысл: Orca будит координатора, когда воркер отчитался, упал или потерял
# терминал. Но если воркер просто повис (модель «думает», терминал ждёт ответа,
# агент залип), никто не пикнет — и мы ждём молча. Здесь тот же предохранитель,
# что у `ci-watch.sh`: опрос `worker-show` и выход по дедлайну или по отсутствию
# прогресса.
#
# Коды возврата:
#   0 — все воркеры закончили успешно;
#   1 — хотя бы один закончил неудачей (failed/cancelled);
#   2 — не дождались: последняя активность всех воркеров старше --stall
#       (по умолчанию 600 с), вышел --deadline (по умолчанию 1800 с), либо терминал
#       воркера потерян. Возраст берётся из `lastActivity`, а не из строки вывода:
#       в ней есть «Nm ago» и она меняется сама по себе.
#
# Переменные: ORCA_WATCH_STALL, ORCA_WATCH_DEADLINE, ORCA_WATCH_INTERVAL,
#              ORCA_CLI (по умолчанию orca-ide).
set -u

ORCA=${ORCA_CLI:-orca-ide}
STALL=${ORCA_WATCH_STALL:-600}
DEADLINE=${ORCA_WATCH_DEADLINE:-1800}
INTERVAL=${ORCA_WATCH_INTERVAL:-20}
DISPATCHES=""
ALL=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

need_value() {
    [ $# -ge 2 ] || { echo "worker-watch: у ключа $1 нет значения" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stall) need_value "$@"; STALL=$2; shift 2 ;;
        --deadline) need_value "$@"; DEADLINE=$2; shift 2 ;;
        --interval) need_value "$@"; INTERVAL=$2; shift 2 ;;
        --all) ALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "worker-watch: неизвестный ключ $1" >&2; usage >&2; exit 2 ;;
        *) DISPATCHES="$DISPATCHES $1"; shift ;;
    esac
done

if [ "$ALL" -eq 1 ]; then
    DISPATCHES=$("$ORCA" orchestration worker-list --json 2>/dev/null | python3 -c '
import json, sys
terminal = {"succeeded", "failed", "cancelled", "closed"}
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for w in d.get("result", {}).get("workers", []):
    if w.get("state") not in terminal:
        print(w["dispatchId"])
')
    [ -n "$DISPATCHES" ] || { echo "worker-watch: активных воркеров нет — все уже закончили"; exit 0; }
    echo "worker-watch: активные воркеры: $(echo $DISPATCHES)"
fi

if [ -z "$DISPATCHES" ]; then
    echo "worker-watch: укажите dispatch id (ctx_..., его печатает worker-start) или --all" >&2
    exit 2
fi

# Один воркер одной строкой: состояние, живость, активность и её возраст.
worker_line() {
    "$ORCA" orchestration worker-show --dispatch "$1" --json 2>/dev/null | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin).get("result", {})
except ValueError:
    print(sys.argv[1], "(нет ответа Orca)")
    sys.exit(0)
print("%s  %s / %s / %s / %s" % (
    sys.argv[1], r.get("state", "?"), r.get("liveness", "?"),
    r.get("activity", "?"), r.get("lastActivity", "?")))
' "$1"
}

# Возраст последней активности в секундах (по строке вида "13m ago").
age_seconds() {
    "$ORCA" orchestration worker-show --dispatch "$1" --json 2>/dev/null | python3 -c '
import json, re, sys
r = json.load(sys.stdin).get("result", {})
text = str(r.get("lastActivity", ""))
m = re.match(r"(\d+)\s*(s|m|h|d)", text)
mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
print(int(m.group(1)) * mult[m.group(2)] if m else -1)
'
}

terminal() {
    case "$1" in
        succeeded|failed|cancelled|closed) return 0 ;;
        *) return 1 ;;
    esac
}

start=$(date +%s)
last_change=$start
prev=""

while :; do
    now=$(date +%s)
    cur=""
    states=""
    for id in $DISPATCHES; do
        line=$(worker_line "$id")
        state=$(echo "$line" | awk '{print $2}')
        states="$states $state"
        if [ -z "$cur" ]; then cur=$line; else cur="$cur
$line"; fi
    done

    if [ "$cur" != "$prev" ]; then
        [ -z "$prev" ] || echo "--- $(date +%H:%M:%S) ---"
        echo "$cur"
        prev=$cur
        last_change=$now
    fi

    pending=0
    failed=0
    for state in $states; do
        if terminal "$state"; then
            [ "$state" = "succeeded" ] || failed=$((failed + 1))
        else
            pending=$((pending + 1))
        fi
    done

    if [ "$pending" -eq 0 ]; then
        echo "worker-watch: все воркеры закончили за $((now - start))s"
        [ "$failed" -eq 0 ] && exit 0
        echo "worker-watch: неудачных воркеров: $failed"
        exit 1
    fi

    if [ $((now - start)) -ge "$DEADLINE" ]; then
        echo "worker-watch: вышел дедлайн ${DEADLINE}s, воркеры не закончили — считаю зависанием"
        exit 2
    fi

    # Зависание: ни один воркер не двигался дольше --stall. Считаем по возрасту
    # последней активности (`lastActivity`), а не по строке вывода: в ней есть
    # «Nm ago», и она меняется сама по себе каждую минуту — по такой строке
    # прогресс виделся бы вечно (на этом наблюдатель уже проспал 45 минут).
    stalled=1
    for id in $DISPATCHES; do
        age=$(age_seconds "$id")
        if [ "$age" -ge 0 ] && [ "$age" -lt "$STALL" ]; then
            stalled=0
        fi
    done
    if [ "$stalled" -eq 1 ]; then
        echo "worker-watch: ни один воркер не подавал признаков жизни ${STALL}s и дольше"
        echo "worker-watch: смотреть orca orchestration worker-list / worker-show --dispatch <id>"
        exit 2
    fi

    sleep "$INTERVAL"
done

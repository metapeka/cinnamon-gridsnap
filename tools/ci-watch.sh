#!/bin/sh
# ci-watch.sh — следить за прогонами CI, не давая себе висеть.
#
#   tools/ci-watch.sh [--deadline SEC] [--stall SEC] [--repo OWNER/NAME] RUN_ID...
#   tools/ci-watch.sh --pr 14          # взять прогоны открытого PR
#
# Смысл: `gh run watch` честно ждёт, пока GitHub отдаст финал, — а если job повис
# (нет раннера, шаг висит, прогон в очереди), ждать можно до дефолтного лимита в
# 360 минут. Этот скрипт всегда завершается сам и говорит, что именно случилось.
# Следит не за статусом прогона (он часами остаётся `in_progress`), а за шагами
# job'ов: пока inside шагов идёт движение, «зависания» нет.
#
# Коды возврата:
#   0 — все прогоны завершились успешно;
#   1 — хотя бы один прогон завершился неудачей;
#   2 — не дождались: статус не менялся дольше --stall (по умолчанию 300 с) —
#       «завис», либо вышло --deadline (по умолчанию 900 с).
#
# Переменные: CI_WATCH_REPO, CI_WATCH_DEADLINE, CI_WATCH_STALL, CI_WATCH_INTERVAL.
set -u

REPO=${CI_WATCH_REPO:-metapeka/cinnamon-gridsnap}
DEADLINE=${CI_WATCH_DEADLINE:-900}
STALL=${CI_WATCH_STALL:-300}
INTERVAL=${CI_WATCH_INTERVAL:-15}
PR=""
RUNS=""

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

need_value() {
    [ $# -ge 2 ] || { echo "ci-watch: у ключа $1 нет значения" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
    case "$1" in
        --deadline) need_value "$@"; DEADLINE=$2; shift 2 ;;
        --stall) need_value "$@"; STALL=$2; shift 2 ;;
        --interval) need_value "$@"; INTERVAL=$2; shift 2 ;;
        --repo) need_value "$@"; REPO=$2; shift 2 ;;
        --pr) need_value "$@"; PR=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "ci-watch: неизвестный ключ $1" >&2; usage >&2; exit 2 ;;
        *) RUNS="$RUNS $1"; shift ;;
    esac
done

if [ -z "$RUNS" ] && [ -z "$PR" ]; then
    echo "ci-watch: укажите run id или --pr N" >&2
    exit 2
fi

if [ -n "$PR" ]; then
    RUNS=$(gh pr view "$PR" -R "$REPO" --json statusCheckRollup \
        --jq '[.statusCheckRollup[]? | .detailsUrl // "" | capture("runs/(?<id>[0-9]+)").id] | unique | .[]' 2>/dev/null)
    [ -n "$RUNS" ] || { echo "ci-watch: у PR #$PR нет прогонов (или ещё не стартовали)" >&2; exit 2; }
    RUNS=$(echo $RUNS)
    echo "ci-watch: PR #$PR → прогоны: $(echo $RUNS)"
fi

# Один прогон одной строкой: статус прогона + прогресс каждого job'а по шагам.
run_line() {
    gh run view "$1" -R "$REPO" --json status,conclusion,jobs --jq \
        '[.status + "/" + (if (.conclusion // "") == "" then "-" else .conclusion end)] + [.jobs[] | .name + ": " + .status + " " + (([.steps[]? | select(.conclusion != null)] | length) | tostring) + "/" + ((.steps | length) | tostring) + " шагов"] | join("  |  ")' \
        2>/dev/null | sed "s/^/$1  /"
}

start=$(date +%s)
last_change=$start
prev=""

while :; do
    now=$(date +%s)
    cur=""
    for id in $RUNS; do
        line=$(run_line "$id")
        [ -n "$line" ] || line="$id  (прогон ещё не виден)"
        if [ -z "$cur" ]; then
            cur=$line
        else
            cur="$cur
$line"
        fi
    done

    if [ "$cur" != "$prev" ]; then
        [ -z "$prev" ] || echo "--- $(date +%H:%M:%S) ---"
        echo "$cur"
        prev=$cur
        last_change=$now
    fi

    done_all=1
    failed=0
    for id in $RUNS; do
        line=$(printf '%s\n' "$cur" | grep "^$id  " || true)
        case "$line" in
            *"/-"*)                               done_all=0 ;;
            *"/success"*)                         : ;;
            *"/failure"*|*"/cancelled"*|*"/timed_out"*|*"/startup_failure"*) failed=$((failed + 1)) ;;
            *)                                    done_all=0 ;;
        esac
    done

    if [ "$done_all" -eq 1 ]; then
        echo "ci-watch: прогоны завершены за $((now - start))s"
        [ "$failed" -eq 0 ] && exit 0
        echo "ci-watch: неудачных прогонов: $failed"
        exit 1
    fi

    if [ $((now - start)) -ge "$DEADLINE" ]; then
        echo "ci-watch: вышел дедлайн ${DEADLINE}s — прогоны не завершились, считаю зависанием"
        exit 2
    fi

    if [ $((now - last_change)) -ge "$STALL" ]; then
        echo "ci-watch: за $((now - last_change))s ни один шаг не сдвинулся — прогон завис"
        echo "ci-watch: смотреть https://github.com/$REPO/actions"
        exit 2
    fi

    sleep "$INTERVAL"
done

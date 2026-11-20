#!/bin/sh
# run-smoke.sh — запустить дымовой сценарий с одной повторной попыткой.
#
#   dbus-run-session -- sh tests/run-smoke.sh tests/smoke.sh [аргументы...]
#
# Зачем: дымовые сценарии поднимают Xvfb и muffin, и в CI окно иногда не успевает
# получить геометрию от оконного менеджера к моменту проверки — сценарий падает на
# тайминге, а не на коде (локально тот же коммит проходит). Один повтор отделяет
# такую флакость от настоящей поломки: настоящая упадёт обе попытки.
#
# Коды возврата: последний статус сценария (0 — прошёл на любой попытке).
set -u

ATTEMPTS=${SMOKE_ATTEMPTS:-2}

if [ $# -lt 1 ]; then
    echo "run-smoke: укажите сценарий, например: tests/smoke.sh" >&2
    exit 2
fi

script=$1
shift

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
    if [ "$ATTEMPTS" -gt 1 ]; then
        echo "run-smoke: попытка $attempt из $ATTEMPTS — $script"
    fi
    if sh "$script" "$@"; then
        exit 0
    else
        status=$?
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -le "$ATTEMPTS" ]; then
        echo "run-smoke: $script упал (код $status), повторяю" >&2
        echo "::warning title=Дымовой тест нестабилен::попытка $((attempt - 1)) из $ATTEMPTS упала, повторяю"
        sleep 3
    fi
done

echo "run-smoke: $script упал во всех $ATTEMPTS попытках — это не флак" >&2
exit "$status"

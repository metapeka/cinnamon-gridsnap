#!/usr/bin/env bash
# install-daemon.sh — включает автозапуск демона Gridsnap и привязки профилей
# к экранам в сессии Cinnamon (X11).
#
# В Cinnamon `graphical-session.target` для `systemd --user` часто остаётся
# `inactive`, поэтому надёжный способ — XDG-автозапуск
# `~/.config/autostart/*.desktop` (как у системного `ru-keyboard.desktop`).
#
# Ставятся две записи:
#   gridsnap-daemon.desktop   — сам демон (автоукладка по правилам);
#   gridsnap-monitors.desktop — `zone-tiler.py profile use-monitors --quiet`:
#                               при входе в сессию раскладка экранов из
#                               `[monitors]` применяется к активному конфигу.
#
#   ./install-daemon.sh           # включить автозапуск демона и привязки экранов
#   ./install-daemon.sh --list    # показать, что стоит
#   ./install-daemon.sh --remove  # выключить автозапуск
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="${AUTOSTART}/gridsnap-daemon.desktop"
MONITORS_DESKTOP="${AUTOSTART}/gridsnap-monitors.desktop"
NAME="GridSnap daemon"

write_desktop() {  # $1 = путь
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=${NAME}
Name[ru]=Демон GridSnap
Comment=Auto-tile new windows into zones (Cinnamon/X11)
Comment[ru]=Автоукладка новых окон по зонам (Cinnamon/X11)
Exec=python3 ${DIR}/zone-daemon.py
Icon=gridsnap
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
X-GNOME-Autostart-Phase=Applications
Hidden=false
EOF
    chmod +x "$1"
}

write_monitors_desktop() {  # $1 = путь
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=GridSnap monitor profiles
Name[ru]=Привязка профилей GridSnap к экранам
Comment=Apply monitor-bound layout profiles on login (Cinnamon/X11)
Comment[ru]=Применить раскладку экранов из [monitors] при входе в сессию (Cinnamon/X11)
Exec=python3 ${DIR}/zone-tiler.py profile use-monitors --quiet
Icon=gridsnap
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=1
X-GNOME-Autostart-Phase=Applications
Hidden=false
EOF
    chmod +x "$1"
}

list_one() {  # $1 = путь, $2 = что это
    if [[ -f "$1" ]]; then
        echo "автозапуск $2 включён: $1"
        grep -E '^(Type|Exec|X-GNOME-Autostart-enabled)=' "$1" || true
    else
        echo "автозапуск $2 не установлен ($1)"
    fi
}

case "${1:-}" in
    --remove)
        rm -f "$DESKTOP" "$MONITORS_DESKTOP"
        echo "автозапуск GridSnap выключен: удалены $DESKTOP и $MONITORS_DESKTOP"
        exit 0
        ;;
    --list)
        list_one "$DESKTOP" "демона GridSnap"
        list_one "$MONITORS_DESKTOP" "привязки профилей к экранам"
        exit 0
        ;;
    "")
        ;;
    *)
        echo "install-daemon.sh: неизвестный флаг «$1»" >&2
        echo "использование: $0 [--list | --remove]" >&2
        exit 2
        ;;
esac

write_desktop "$DESKTOP"
write_monitors_desktop "$MONITORS_DESKTOP"

# мягкая валидация: если утилита есть — проверить, но не падать из-за неё
desktop-file-validate "$DESKTOP" >/dev/null 2>&1 || true
desktop-file-validate "$MONITORS_DESKTOP" >/dev/null 2>&1 || true

echo "автозапуск демона: $DESKTOP"
echo "Exec:              python3 ${DIR}/zone-daemon.py"
echo "автозапуск привязки экранов: $MONITORS_DESKTOP"
echo "Exec:              python3 ${DIR}/zone-tiler.py profile use-monitors --quiet"
echo "демон сам читает конфиг из XDG (~/.config/gridsnap/zones.conf)"

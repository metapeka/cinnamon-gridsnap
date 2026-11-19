#!/usr/bin/env bash
# install-daemon.sh — включает автозапуск демона Gridsnap в сессии Cinnamon (X11).
#
# В Cinnamon `graphical-session.target` для `systemd --user` часто остаётся
# `inactive`, поэтому надёжный способ — XDG-автозапуск
# `~/.config/autostart/*.desktop` (как у системного `ru-keyboard.desktop`).
#
#   ./install-daemon.sh           # включить автозапуск демона
#   ./install-daemon.sh --list    # показать, что стоит
#   ./install-daemon.sh --remove  # выключить автозапуск
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="${AUTOSTART}/gridsnap-daemon.desktop"
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

case "${1:-}" in
    --remove)
        rm -f "$DESKTOP"
        echo "автозапуск демона GridSnap выключен: удалён $DESKTOP"
        exit 0
        ;;
    --list)
        if [[ -f "$DESKTOP" ]]; then
            echo "автозапуск демона GridSnap включён: $DESKTOP"
            grep -E '^(Type|Exec|X-GNOME-Autostart-enabled)=' "$DESKTOP" || true
        else
            echo "автозапуск демона GridSnap не установлен ($DESKTOP)"
        fi
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

# мягкая валидация: если утилита есть — проверить, но не падать из-за неё
desktop-file-validate "$DESKTOP" >/dev/null 2>&1 || true

echo "автозапуск демона: $DESKTOP"
echo "Exec:              python3 ${DIR}/zone-daemon.py"
echo "демон сам читает конфиг из XDG (~/.config/gridsnap/zones.conf)"

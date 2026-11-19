#!/usr/bin/env bash
# install-launcher.sh — ставит иконку и ярлык Gridsnap в меню Cinnamon.
#
#   ./install-launcher.sh              # ярлык в меню
#   ./install-launcher.sh --desktop    # ещё и ярлык на рабочем столе
#   ./install-launcher.sh --remove     # удалить ярлыки и иконки
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${HOME}/.local/share/applications"
ICONS="${HOME}/.local/share/icons/hicolor"
DESKTOP_DIR="${HOME}/Desktop"
ID="gridsnap"
LEGACY="zone-tiler"
NAME="Gridsnap"
SIZES="16 22 24 32 48 64 128 256 512"

remove_all() {
    rm -f "$APPS/${ID}.desktop" "$APPS/${LEGACY}.desktop"
    rm -f "$DESKTOP_DIR/${ID}.desktop" "$DESKTOP_DIR/${LEGACY}.desktop"
    for size in $SIZES; do
        rm -f "$ICONS/${size}x${size}/apps/${ID}.png" "$ICONS/${size}x${size}/apps/${LEGACY}.png"
    done
    rm -f "$ICONS/scalable/apps/${ID}.svg" "$ICONS/scalable/apps/${LEGACY}.svg"
    gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1 || true
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "--remove" ]]; then
    remove_all
    echo "ярлыки и иконки Gridsnap удалены"
    exit 0
fi

# старые имена (zone-tiler) больше не нужны
rm -f "$APPS/${LEGACY}.desktop" "$DESKTOP_DIR/${LEGACY}.desktop"

python3 "$DIR/make-icon.py" >/dev/null

mkdir -p "$APPS" "$ICONS/scalable/apps"
for size in $SIZES; do
    mkdir -p "$ICONS/${size}x${size}/apps"
    cp "$DIR/icons/${ID}-${size}.png" "$ICONS/${size}x${size}/apps/${ID}.png"
done
cp "$DIR/icons/${ID}.svg" "$ICONS/scalable/apps/${ID}.svg"

write_desktop() {  # $1 = путь
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=${NAME}
GenericName=Window grid layouts
GenericName[ru]=Раскладка окон по зонам
Comment=Draw a zone grid and snap windows into it
Comment[ru]=Нарисуйте сетку зон и вкладывайте в них окна
Exec=python3 ${DIR}/zone-editor.py
Icon=${ID}
Terminal=false
Categories=Settings;
Keywords=grid;snap;tiling;zones;windows;сетка;зоны;окна;мозаика;
StartupNotify=true
StartupWMClass=zone-editor.py
EOF
    chmod +x "$1"
}

mkdir -p "$APPS"
write_desktop "$APPS/${ID}.desktop"

if [[ "${1:-}" == "--desktop" ]]; then
    mkdir -p "$DESKTOP_DIR"
    write_desktop "$DESKTOP_DIR/${ID}.desktop"
    gio set "$DESKTOP_DIR/${ID}.desktop" metadata::trusted true 2>/dev/null || true
fi

gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1 || true
update-desktop-database "$APPS" >/dev/null 2>&1 || true
desktop-file-validate "$APPS/${ID}.desktop" || true

echo "ярлык:  $APPS/${ID}.desktop"
echo "иконка: $ICONS/scalable/apps/${ID}.svg"
echo "в меню: ${NAME} — ищите по слову «grid» или «сетка»"

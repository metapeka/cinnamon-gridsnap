#!/usr/bin/env bash
# install.sh — установка GridSnap из релизного tar.gz в префикс.
#
#   ./install.sh                    # в ~/.local: код, команды, man-страницы, ярлык в меню, иконки
#   ./install.sh --prefix /usr/local
#   ./install.sh --prefix /usr      # системно (нужен root)
#   ./install.sh --no-launcher      # только код, команды и man-страницы, без ярлыка и иконок
#   ./install.sh --uninstall        # убрать установленное (по тем же путям)
#   ./install.sh --help
#
# Это «установка для себя»: файлы кладутся в префикс как есть, без пакетного
# менеджера. Системный пакет (.deb / AUR) ставит то же самое под управлением
# пакетного менеджера — см. README, раздел «Установка».
set -euo pipefail

PROG="$(basename "$0")"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX="${HOME}/.local"
LAUNCHER=1
ACTION="install"

SIZES="16 22 24 32 48 64 128 256 512"
MANPAGES="gridsnap.1 gridsnap-tiler.1 gridsnap-daemon.1 gridsnap-editor.1
           gridsnap-picker.1 gridsnap-hotcorners.1 gridsnap-ctl.1 setup-hotkeys.1"
# Man-страницы — не gettext-домен: man-db выбирает их по каталогу языка, а не
# по каталогу перевода. Собирает их `make man` в man/build/<язык>/man1/;
# английская — язык по умолчанию, поэтому ложится в man1/ без языкового
# каталога (её получает LANG=C и любая локаль без перевода).
MANLANGS="en ru zh_CN"
MANBUILD="$SRC/man/build"

usage() {
    cat <<EOF
$PROG — установка GridSnap в префикс (по умолчанию ~/.local).

Использование:
  ./$PROG [--prefix КАТАЛОГ] [--no-launcher]
  ./$PROG --uninstall [--prefix КАТАЛОГ]

Ключи:
  --prefix КАТАЛОГ  куда ставить (по умолчанию \$HOME/.local)
  --no-launcher     не ставить ярлык в меню и иконки (man-страницы ставятся всё равно)
  --uninstall       удалить файлы, поставленные в этот префикс
  -h, --help        эта справка

Раскладка установки:
  КАТАЛОГ/lib/gridsnap/   код, zones.conf, иконки, install-*.sh
  КАТАЛОГ/share/gridsnap/gallery/
                          готовые раскладки (*.conf: зоны + клавиши + правила)
  КАТАЛОГ/bin/            gridsnap-tiler, gridsnap-daemon, gridsnap-editor, gridsnap-picker,
                          gridsnap-hotcorners, gridsnap-ctl, setup-hotkeys
  КАТАЛОГ/share/man/man1/ man-страницы: английская — язык по умолчанию
  КАТАЛОГ/share/man/ru/man1/, .../zh_CN/man1/
                          страницы ru и zh_CN: man-db выбирает их по локали
                          (маны ставятся всегда: это документация, а не ярлык)
  КАТАЛОГ/share/locale/   каталоги переводов ru и zh_CN (домены gridsnap и gridsnap@metapeka)
  КАТАЛОГ/share/...       ярлык, иконки, документация (если не --no-launcher)

Автозапуск в сессии ставится отдельно и только для текущего пользователя:
  КАТАЛОГ/lib/gridsnap/install-daemon.sh
Он пишет в \$XDG_CONFIG_HOME/autostart (по умолчанию ~/.config/autostart) две
записи: gridsnap-daemon.desktop (демон) и gridsnap-monitors.desktop
(привязка профилей к экранам — `profile use-monitors --quiet`).
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || { echo "$PROG: --prefix требует каталог" >&2; exit 2; }
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"; shift ;;
        --no-launcher)
            LAUNCHER=0; shift ;;
        --launcher)
            LAUNCHER=1; shift ;;
        --uninstall|--remove)
            ACTION="uninstall"; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "$PROG: неизвестный аргумент «$1»" >&2
            usage >&2
            exit 2 ;;
    esac
done

# Приводим префикс к абсолютному пути (как у autoconf --prefix).
mkdir -p "$PREFIX"
PREFIX="$(cd "$PREFIX" && pwd -P)"

LIB="$PREFIX/lib/gridsnap"
BIN="$PREFIX/bin"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor"
GALLERY="$PREFIX/share/gridsnap/gallery"
DOC="$PREFIX/share/doc/gridsnap"
MANDIR="$PREFIX/share/man/man1"

# Каталог man-db для языка сборки: `en` — язык по умолчанию, без языкового
# каталога; остальные — share/man/<язык>/man1/
mandir_for() {  # $1 = en | ru | zh_CN
    case "$1" in
        en) echo "$MANDIR" ;;
        *)  echo "$PREFIX/share/man/$1/man1" ;;
    esac
}

# Каталоги переводов: по домену на язык. Апплет Cinnamon берёт
# `gridsnap@metapeka.mo` из ~/.local/share/locale (префикс по умолчанию).
LOCALEDIR="$PREFIX/share/locale"
LANGUAGES="ru zh_CN"
DOMAINS="gridsnap gridsnap@metapeka"

MODULES="zone-tiler.py zone-daemon.py zone-editor.py zone_config.py zone_geom.py
         zone_reflow.py zone-picker.py zone_picker_nav.py zone-hotcorners.py
         zone_ipc.py zone_actions.py zone_tabs.py zone-tabstrip.py
         zone_tabstrip_nav.py zone_units.py window_backend.py
         setup-hotkeys.py make-icon.py zone_i18n.py"

refresh_caches() {
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1 || true
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$APPS" >/dev/null 2>&1 || true
}

if [ "$ACTION" = "uninstall" ]; then
    rm -f "$BIN/gridsnap-tiler" "$BIN/gridsnap-daemon" \
          "$BIN/gridsnap-editor" "$BIN/gridsnap-picker" \
          "$BIN/gridsnap-hotcorners" "$BIN/gridsnap-ctl" "$BIN/setup-hotkeys"
    rm -f "$APPS/gridsnap.desktop"
    for size in $SIZES; do
        rm -f "$ICONS/${size}x${size}/apps/gridsnap.png"
    done
    rm -f "$ICONS/scalable/apps/gridsnap.svg"
    # Снимаем из всех трёх каталогов: в прошлых версиях страница была одна
    # (русская) и лежала в man1/, и после обновления старый префикс не должен
    # оставлять за собой ни русской страницы, ни пустых языковых каталогов.
    for lang in $MANLANGS; do
        dir="$(mandir_for "$lang")"
        for page in $MANPAGES; do
            rm -f "$dir/$page"
        done
        # снять опустевшие каталоги; чужие страницы (других пакетов) не трогаем
        rmdir "$dir" 2>/dev/null || true
        if [ "$lang" != "en" ]; then
            rmdir "$PREFIX/share/man/$lang" 2>/dev/null || true
        fi
    done
    rmdir "$PREFIX/share/man" 2>/dev/null || true
    for lang in $LANGUAGES; do
        for domain in $DOMAINS; do
            rm -f "$LOCALEDIR/$lang/LC_MESSAGES/$domain.mo"
        done
        # снять опустевшие каталоги; чужие файлы (другие домены) не трогаем
        rmdir "$LOCALEDIR/$lang/LC_MESSAGES" "$LOCALEDIR/$lang" 2>/dev/null || true
    done
    rmdir "$LOCALEDIR" 2>/dev/null || true
    rm -rf "$LIB" "$DOC"
    rm -rf "$GALLERY"
    rmdir "$PREFIX/share/gridsnap" 2>/dev/null || true
    refresh_caches
    echo "GridSnap удалён из $PREFIX"
    exit 0
fi

# --- код и данные ----------------------------------------------------------
mkdir -p "$LIB" "$BIN" "$DOC"
for mod in $MODULES; do
    install -m 0644 "$SRC/$mod" "$LIB/"
done
install -m 0644 "$SRC/zones.conf" "$LIB/zones.conf"
install -m 0755 "$SRC/gridsnap-ctl" "$LIB/gridsnap-ctl"
# Готовые раскладки: zone_config ищет их в <префикс>/share/gridsnap/gallery, а в
# чекауте — рядом с кодом; `gallery/*.conf` из архива кладём туда же, куда .deb/AUR.
if [ -d "$SRC/gallery" ]; then
    mkdir -p "$GALLERY"
    install -m 0644 "$SRC"/gallery/*.conf "$GALLERY/"
fi
install -m 0755 "$SRC/install-launcher.sh" "$SRC/install-daemon.sh" "$LIB/"
rm -rf "$LIB/icons" "$LIB/docs"
cp -a "$SRC/icons" "$LIB/icons"
cp -a "$SRC/docs" "$DOC/docs"
install -m 0644 "$SRC/README.md" "$SRC/SPEC.md" "$SRC/LICENSE" "$DOC/"

# --- man-страницы ----------------------------------------------------------
# Документация, а не часть ярлыка: ставится и при --no-launcher.
# Готовые страницы трёх языков кладёт в архив `make tarball`; po4a при
# установке не нужен.
MAN_DIRS=""
if [ -d "$MANBUILD" ]; then
    for lang in $MANLANGS; do
        dir="$(mandir_for "$lang")"
        mkdir -p "$dir"
        for page in $MANPAGES; do
            install -m 0644 "$MANBUILD/$lang/man1/$page" "$dir/"
        done
        MAN_DIRS="$MAN_DIRS $dir"
    done
else
    # Чекаут без сборки (`make man` не запускали): английских страниц нет.
    # Ставим русский источник и псевдонимы в man1/ — так было до перевода. Но
    # молча класть русскую страницу на место английской нельзя: английская тут
    # язык по умолчанию, поэтому говорим об этом строкой в выводе.
    echo "man-страницы: сборки нет (make man), ставлю русский источник в man1/"
    mkdir -p "$MANDIR"
    for page in $MANPAGES; do
        install -m 0644 "$SRC/man/$page" "$MANDIR/"
    done
    MAN_DIRS="$MANDIR"
fi

# --- каталоги переводов ----------------------------------------------------
# Собраны `make mo` (в tar.gz попадают из каталога locale/). Без них код
# работает на английских строках — исходном языке, поэтому их отсутствие не
# ошибка, а просто отсутствие перевода (например, в чекауте без сборки).
for lang in $LANGUAGES; do
    for domain in $DOMAINS; do
        mo="$SRC/locale/$lang/LC_MESSAGES/$domain.mo"
        [ -f "$mo" ] || continue
        install -Dm0644 "$mo" "$LOCALEDIR/$lang/LC_MESSAGES/$domain.mo"
    done
done

# --- команды-запускалки ----------------------------------------------------
wrapper() {  # $1 = имя команды, $2 = модуль в lib/gridsnap
    cat > "$BIN/$1" <<EOF
#!/bin/sh
# сгенерировано install.sh GridSnap; правьте install.sh, а не этот файл
exec python3 "$LIB/$2" "\$@"
EOF
    chmod 0755 "$BIN/$1"
}

wrapper gridsnap-tiler  zone-tiler.py
wrapper gridsnap-daemon zone-daemon.py
wrapper gridsnap-editor zone-editor.py
wrapper gridsnap-picker zone-picker.py
wrapper gridsnap-hotcorners zone-hotcorners.py
wrapper gridsnap-ctl    gridsnap-ctl
wrapper setup-hotkeys   setup-hotkeys.py

# --- ярлык и иконки --------------------------------------------------------
if [ "$LAUNCHER" -eq 1 ]; then
    for size in $SIZES; do
        install -Dm0644 "$SRC/icons/gridsnap-${size}.png" \
            "$ICONS/${size}x${size}/apps/gridsnap.png"
    done
    install -Dm0644 "$SRC/icons/gridsnap.svg" "$ICONS/scalable/apps/gridsnap.svg"

    mkdir -p "$APPS"
    cat > "$APPS/gridsnap.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Gridsnap
GenericName=Window grid layouts
GenericName[ru]=Раскладка окон по зонам
Comment=Draw a zone grid and snap windows into it
Comment[ru]=Нарисуйте сетку зон и вкладывайте в них окна
Exec=$BIN/gridsnap-editor
Icon=gridsnap
Terminal=false
Categories=Settings;
Keywords=grid;snap;tiling;zones;windows;сетка;зоны;окна;мозаика;
StartupNotify=true
StartupWMClass=zone-editor.py
EOF
    chmod 0644 "$APPS/gridsnap.desktop"
    command -v desktop-file-validate >/dev/null 2>&1 && \
        desktop-file-validate "$APPS/gridsnap.desktop" || true
    refresh_caches
fi

# --- итог ------------------------------------------------------------------
echo "GridSnap установлен в $PREFIX"
echo "  код:      $LIB"
echo "  команды:  $BIN/gridsnap-tiler, $BIN/gridsnap-daemon, $BIN/gridsnap-editor, $BIN/gridsnap-picker, $BIN/gridsnap-hotcorners, $BIN/gridsnap-ctl, $BIN/setup-hotkeys"
echo "  справка:  MANPATH=$PREFIX/share/man man gridsnap (en; ru и zh_CN — по локали)"
echo "  ман:      ${MAN_DIRS# }"
echo "  переводы: $LOCALEDIR/<язык>/LC_MESSAGES (gridsnap, gridsnap@metapeka)"
echo "  галерея:  $GALLERY (готовые раскладки — «Из галереи…» в редакторе)"
echo "  автозапуск: $LIB/install-daemon.sh (демон + привязка профилей к экранам)";
if [ "$LAUNCHER" -eq 1 ]; then
    echo "  ярлык:    $APPS/gridsnap.desktop"
    echo "  иконки:   $ICONS"
fi
echo
echo "Автозапуск в сессии (ставится один раз, для текущего пользователя):"
echo "  $LIB/install-daemon.sh — пишет \$XDG_CONFIG_HOME/autostart/gridsnap-daemon.desktop"
echo "  и gridsnap-monitors.desktop: при входе зовётся zone-tiler.py profile use-monitors --quiet"
echo
echo "Проверка:  $BIN/gridsnap-tiler --version"
echo "Удаление:  ./$PROG --uninstall --prefix $PREFIX"

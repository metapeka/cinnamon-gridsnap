# Maintainer: metapeka <188594035+metapeka@users.noreply.github.com>
#
# Рецепт AUR для GridSnap. Ставит то же, что и .deb: код в /usr/lib/gridsnap,
# команды-запускалки в /usr/bin, ярлык в /usr/share/applications и man-страницы
# трёх языков в /usr/share/man/<язык>/man1 (для Arch — сжатыми gzip).
# Версия обязана совпадать с VERSION в zone-tiler.py (make aur это проверяет).
#
# Сборка:
#   makepkg -si
pkgname=gridsnap
pkgver=0.2.0
pkgrel=1
pkgdesc="window tiling into zones for Cinnamon/X11"
arch=('any')
url="https://github.com/metapeka/cinnamon-gridsnap"
license=('MIT')
depends=('python' 'python-gobject' 'gtk3' 'libx11' 'xorg-xprop' 'xorg-xwininfo')
# gettext — каталоги переводов (msgfmt), po4a — man-страницы (тоже в build()).
makedepends=('gettext' 'po4a')
optdepends=('python-cairo: перерисовка иконок (make-icon.py)')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')
# Контрольная сумма здесь не выдумана: source ссылается на архив тега
# v$pkgver, а такой архив на GitHub существует только после пуша тега. Тег
# v0.2.0 ставится после слияния PR и публикации релиза, поэтому заранее
# посчитать сумму не по чему. После публикации она считается одной командой:
#   curl -L .../archive/refs/tags/v0.2.0.tar.gz | sha256sum
# и вписывается отдельным коммитом. До этого 'SKIP' — осознанная заглушка:
# makepkg соберёт пакет, но скачанный архив не сверит.

# Архив с GitHub (`archive/refs/tags/...`) собранного man/build не содержит:
# каталог в .gitignore, в репозитории лежат только источник и каталоги po4a.
# Поэтому страницы нужно собрать здесь же: `make man` — тот же конвейер, что и
# в .deb/tar.gz (po4a для en и zh_CN, копия источника для ru, псевдонимы во все
# три языка, точки переноса для китайской). Повторять то же руками медленнее и
# легче разойтись с Makefile.
build() {
    make man
}

package() {
    install -d "$pkgdir/usr/lib/gridsnap"
    install -m 0644 \
        zone-tiler.py zone-daemon.py zone-editor.py zone-picker.py \
        zone-hotcorners.py \
        zone_config.py zone_geom.py zone_picker_nav.py zone_reflow.py zone_ipc.py \
        zone_actions.py zone_tabs.py zone-tabstrip.py zone_tabstrip_nav.py \
        zone_i18n.py setup-hotkeys.py make-icon.py \
        zones.conf \
        "$pkgdir/usr/lib/gridsnap/"
    install -m 0755 gridsnap-ctl "$pkgdir/usr/lib/gridsnap/"
    install -m 0755 install-launcher.sh install-daemon.sh "$pkgdir/usr/lib/gridsnap/"
    cp -a icons "$pkgdir/usr/lib/gridsnap/icons"

    install -d "$pkgdir/usr/bin"
    local m c
    for m in "zone-tiler.py:gridsnap-tiler" \
             "zone-daemon.py:gridsnap-daemon" \
             "zone-editor.py:gridsnap-editor" \
             "zone-picker.py:gridsnap-picker" \
             "zone-hotcorners.py:gridsnap-hotcorners" \
             "gridsnap-ctl:gridsnap-ctl" \
             "setup-hotkeys.py:setup-hotkeys"; do
        c="${m#*:}"
        printf '#!/bin/sh\nexec python3 /usr/lib/gridsnap/%s "$@"\n' "${m%%:*}" \
            > "$pkgdir/usr/bin/$c"
        chmod 0755 "$pkgdir/usr/bin/$c"
    done

    install -d "$pkgdir/usr/share/applications"
    install -m 0644 contrib/gridsnap.desktop \
        "$pkgdir/usr/share/applications/gridsnap.desktop"

    install -d "$pkgdir/usr/share/icons/hicolor"
    local size
    for size in 16 22 24 32 48 64 128 256 512; do
        install -Dm0644 "icons/gridsnap-$size.png" \
            "$pkgdir/usr/share/icons/hicolor/${size}x${size}/apps/gridsnap.png"
    done
    install -Dm0644 icons/gridsnap.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/gridsnap.svg"

    install -d "$pkgdir/usr/share/doc/$pkgname"
    install -m 0644 README.md SPEC.md "$pkgdir/usr/share/doc/$pkgname/"
    cp -a docs "$pkgdir/usr/share/doc/$pkgname/docs"
    install -Dm0644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"

    # Каталоги переводов (gettext в makedepends): два домена — Python-инстру-
    # менты и апплет Cinnamon. Язык исходника — русский, каталога ru нет.
    local lang
    for lang in en zh_CN; do
        install -d "$pkgdir/usr/share/locale/$lang/LC_MESSAGES"
        msgfmt --check -o "$pkgdir/usr/share/locale/$lang/LC_MESSAGES/gridsnap.mo" \
            "po/$lang.po"
        msgfmt --check -o "$pkgdir/usr/share/locale/$lang/LC_MESSAGES/gridsnap@metapeka.mo" \
            "po/gridsnap@metapeka/$lang.po"
    done

    # man-страницы: три языка, каждый в свой каталог man-db. Английская —
    # язык по умолчанию (man1/), русская и китайская — man/ru/man1 и
    # man/zh_CN/man1: man-db выбирает их по локали. Псевдонимы (.so man1/gridsnap.1)
    # уже лежат в каталогах сборки и ставятся так же, как основная страница.
    # В Arch страницы принято ставить сжатыми; -n — без имени и времени файла,
    # чтобы сборка была воспроизводимой.
    local lang mdir page
    for lang in en ru zh_CN; do
        case "$lang" in
            en) mdir="man1" ;;
            *)  mdir="$lang/man1" ;;
        esac
        install -d "$pkgdir/usr/share/man/$mdir"
        for page in gridsnap.1 gridsnap-tiler.1 gridsnap-daemon.1 \
                    gridsnap-editor.1 gridsnap-picker.1 gridsnap-hotcorners.1 \
                    gridsnap-ctl.1 setup-hotkeys.1; do
            gzip -9n -c "man/build/$lang/man1/$page" > "$pkgdir/usr/share/man/$mdir/$page.gz"
            chmod 0644 "$pkgdir/usr/share/man/$mdir/$page.gz"
        done
    done
}

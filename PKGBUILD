# Maintainer: metapeka <188594035+metapeka@users.noreply.github.com>
#
# Рецепт AUR для GridSnap. Ставит то же, что и .deb: код в /usr/lib/gridsnap,
# команды-запускалки в /usr/bin и ярлык в /usr/share/applications.
# Собирать не обязательно — это рецепт для Arch; версия обязана совпадать с
# VERSION в zone-tiler.py (make aur это проверяет).
#
# Сборка:
#   makepkg -si
pkgname=gridsnap
pkgver=0.1.0
pkgrel=1
pkgdesc="Раскладка окон по зонам для Cinnamon/X11"
arch=('any')
url="https://github.com/metapeka/cinnamon-gridsnap"
license=('custom')  # лицензия в репозитории не объявлена
depends=('python' 'python-gobject' 'gtk3' 'libx11' 'xorg-xprop' 'xorg-xwininfo')
optdepends=('python-cairo: перерисовка иконок (make-icon.py)')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
    install -d "$pkgdir/usr/lib/gridsnap"
    install -m 0644 \
        zone-tiler.py zone-daemon.py zone-editor.py \
        zone_config.py zone_reflow.py setup-hotkeys.py make-icon.py \
        zones.conf \
        "$pkgdir/usr/lib/gridsnap/"
    install -m 0755 install-launcher.sh install-daemon.sh "$pkgdir/usr/lib/gridsnap/"
    cp -a icons "$pkgdir/usr/lib/gridsnap/icons"

    install -d "$pkgdir/usr/bin"
    local m c
    for m in "zone-tiler.py:gridsnap-tiler" \
             "zone-daemon.py:gridsnap-daemon" \
             "zone-editor.py:gridsnap-editor" \
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
}

# Makefile — сборка релизных артефактов GridSnap.
#
#   make tarball   # dist/gridsnap-<версия>.tar.gz (код + install.sh)
#   make deb       # dist/gridsnap_<версия>_all.deb (через dpkg-deb, без debhelper)
#   make aur       # dist/PKGBUILD для Arch/ AUR (проверка версии + копия)
#   make all       # tarball и deb
#   make version   # только версия из zone-tiler.py
#   make clean
#
# Никаких новых зависимостей: make, tar, dpkg-deb (пакет dpkg), sh.
# Версия берётся из VERSION в zone-tiler.py; zone-daemon.py обязан совпадать.

SHELL  := /bin/sh
PYTHON ?= python3

PKG     := gridsnap
VERSION := $(shell sed -n 's/^VERSION = "\([^"]*\)"/\1/p' zone-tiler.py | head -n1)
DAEMON_VERSION := $(shell sed -n 's/^VERSION = "\([^"]*\)"/\1/p' zone-daemon.py | head -n1)
AUR_VERSION := $(shell sed -n 's/^pkgver=//p' PKGBUILD | head -n1)

DIST  := dist
BUILD := build

DEBROOT := $(BUILD)/deb
TARDIR  := $(BUILD)/$(PKG)-$(VERSION)
DATADIR := /usr/lib/gridsnap
BINDIR  := /usr/bin

MODULES := zone-tiler.py zone-daemon.py zone-editor.py zone_config.py \
           zone_geom.py zone_reflow.py zone-picker.py zone_picker_nav.py setup-hotkeys.py make-icon.py
INSTALL_SH := install.sh install-launcher.sh install-daemon.sh
RELEASE_FILES := $(MODULES) $(INSTALL_SH) zones.conf README.md SPEC.md Makefile PKGBUILD
RELEASE_DIRS := icons docs contrib
ICON_SIZES := 16 22 24 32 48 64 128 256 512
ICON_RECIPES := $(foreach s,$(ICON_SIZES),install -Dm0644 "icons/gridsnap-$(s).png" "$(DEBROOT)/usr/share/icons/hicolor/$(s)x$(s)/apps/gridsnap.png";)

.PHONY: all tarball deb aur version clean check-version

all: tarball deb

# --------------------------------------------------------------- версия

check-version:
	@test -n "$(VERSION)" || { echo "make: не нашёл VERSION в zone-tiler.py" >&2; exit 1; }
	@test "$(VERSION)" = "$(DAEMON_VERSION)" || { \
		echo "make: VERSION расходится: zone-tiler.py=$(VERSION), zone-daemon.py=$(DAEMON_VERSION)" >&2; \
		exit 1; }
	@test -n "$(AUR_VERSION)" || { echo "make: в PKGBUILD нет pkgver" >&2; exit 1; }
	@test "$(AUR_VERSION)" = "$(VERSION)" || { \
		echo "make: PKGBUILD pkgver=$(AUR_VERSION) не совпадает с VERSION=$(VERSION)" >&2; \
		exit 1; }

version:
	@echo "$(VERSION)"

# --------------------------------------------------------------- tar.gz

tarball: check-version
	@rm -rf "$(TARDIR)"
	@mkdir -p "$(TARDIR)" "$(DIST)"
	@cp -a $(RELEASE_FILES) "$(TARDIR)/"
	@cp -a $(RELEASE_DIRS) "$(TARDIR)/"
	@chmod 0755 "$(TARDIR)"/$(INSTALL_SH) "$(TARDIR)"/setup-hotkeys.py "$(TARDIR)"/make-icon.py
	@tar -C "$(BUILD)" -czf "$(DIST)/$(PKG)-$(VERSION).tar.gz" "$(PKG)-$(VERSION)"
	@echo "собран $(DIST)/$(PKG)-$(VERSION).tar.gz:"
	@tar -tzf "$(DIST)/$(PKG)-$(VERSION).tar.gz" | sed 's/^/  /'

# --------------------------------------------------------------- deb

# Обёртка-запускалка в /usr/bin: $1 — модуль, $2 — имя команды.
define deb_wrapper
	printf '%s\n' '#!/bin/sh' 'exec $(PYTHON) $(DATADIR)/$(1) "$$@"' > '$(DEBROOT)/$(BINDIR)/$(2)'
	chmod 0755 '$(DEBROOT)/$(BINDIR)/$(2)'
endef

deb: check-version
	@rm -rf "$(DEBROOT)"
	@mkdir -p "$(DEBROOT)/DEBIAN" "$(DEBROOT)$(DATADIR)" \
	          "$(DEBROOT)$(BINDIR)" "$(DEBROOT)/usr/share/applications" \
	          "$(DEBROOT)/usr/share/doc/$(PKG)"
	@install -m 0644 $(MODULES) "$(DEBROOT)$(DATADIR)/"
	@install -m 0644 zones.conf "$(DEBROOT)$(DATADIR)/zones.conf"
	@install -m 0755 install-launcher.sh install-daemon.sh "$(DEBROOT)$(DATADIR)/"
	@cp -a icons "$(DEBROOT)$(DATADIR)/icons"
	@cp -a docs "$(DEBROOT)/usr/share/doc/$(PKG)/docs"
	@install -m 0644 README.md SPEC.md "$(DEBROOT)/usr/share/doc/$(PKG)/"

	@$(call deb_wrapper,zone-tiler.py,gridsnap-tiler)
	@$(call deb_wrapper,zone-daemon.py,gridsnap-daemon)
	@$(call deb_wrapper,zone-editor.py,gridsnap-editor)
	@$(call deb_wrapper,zone-picker.py,gridsnap-picker)
	@$(call deb_wrapper,setup-hotkeys.py,setup-hotkeys)

	@$(ICON_RECIPES)
	@install -Dm0644 icons/gridsnap.svg \
		"$(DEBROOT)/usr/share/icons/hicolor/scalable/apps/gridsnap.svg"

	@install -Dm0644 contrib/gridsnap.desktop \
		"$(DEBROOT)/usr/share/applications/$(PKG).desktop"

	@echo 'Package: $(PKG)'                                                                       >  "$(DEBROOT)/DEBIAN/control"
	@echo 'Version: $(VERSION)'                                                                   >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Section: utils'                                                                        >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Priority: optional'                                                                    >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Architecture: all'                                                                     >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Maintainer: metapeka <188594035+metapeka@users.noreply.github.com>'                    >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Homepage: https://github.com/metapeka/cinnamon-gridsnap'                               >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Depends: python3, python3-gi, gir1.2-gtk-3.0, libx11-6, x11-utils'                     >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Suggests: python3-cairo'                                                               >> "$(DEBROOT)/DEBIAN/control"
	@echo 'Description: раскладка окон по зонам для Cinnamon/X11'                                 >> "$(DEBROOT)/DEBIAN/control"
	@echo ' GridSnap — визуальный редактор зон рабочей области и раскладчик окон'                  >> "$(DEBROOT)/DEBIAN/control"
	@echo ' для Cinnamon на X11. Активное окно вкладывается в зону горячей клавишей;'             >> "$(DEBROOT)/DEBIAN/control"
	@echo ' необязательный демон (gridsnap-daemon) автоматически укладывает новые окна'           >> "$(DEBROOT)/DEBIAN/control"
	@echo ' по правилам. Работает поверх muffin через EWMH, рамка окна совпадает с зоной.'        >> "$(DEBROOT)/DEBIAN/control"

	@printf '%s\n' '#!/bin/sh' 'set -e' \
		'if [ -x /usr/bin/update-desktop-database ]; then update-desktop-database -q /usr/share/applications || true; fi' \
		'if [ -x /usr/bin/gtk-update-icon-cache ]; then gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true; fi' \
		> "$(DEBROOT)/DEBIAN/postinst"
	@chmod 0755 "$(DEBROOT)/DEBIAN/postinst"
	@printf '%s\n' '#!/bin/sh' 'set -e' \
		'if [ -x /usr/bin/update-desktop-database ]; then update-desktop-database -q /usr/share/applications || true; fi' \
		'if [ -x /usr/bin/gtk-update-icon-cache ]; then gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true; fi' \
		> "$(DEBROOT)/DEBIAN/postrm"
	@chmod 0755 "$(DEBROOT)/DEBIAN/postrm"

	@cd "$(DEBROOT)" && find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum > DEBIAN/md5sums

	@mkdir -p "$(DIST)"
	@dpkg-deb --build --root-owner-group "$(DEBROOT)" "$(DIST)/$(PKG)_$(VERSION)_all.deb" >/dev/null
	@echo "собран $(DIST)/$(PKG)_$(VERSION)_all.deb"

# --------------------------------------------------------------- AUR

aur: check-version
	@mkdir -p "$(DIST)"
	@bash -n PKGBUILD
	@cp PKGBUILD "$(DIST)/PKGBUILD"
	@echo "рецепт AUR: $(DIST)/PKGBUILD (pkgver=$(AUR_VERSION), pkgrel из файла)"

clean:
	@rm -rf "$(BUILD)" "$(DIST)"
	@echo "удалены $(BUILD)/ и $(DIST)/"

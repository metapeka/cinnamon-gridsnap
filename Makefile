# Makefile — сборка релизных артефактов GridSnap.
#
#   make tarball   # dist/gridsnap-<версия>.tar.gz (код + install.sh)
#   make deb       # dist/gridsnap_<версия>_all.deb (через dpkg-deb, без debhelper)
#   make aur       # dist/PKGBUILD для Arch/ AUR (проверка версии + копия)
#   make all       # tarball и deb
#   make version   # только версия из zone-tiler.py
#   make clean
#
# Никаких новых зависимостей: make, tar, dpkg-deb (пакет dpkg), gzip, sh.
# Версия берётся из VERSION в zone-tiler.py; все файлы, где она объявлена, обязаны
# совпадать, как и версия в .TH man/gridsnap.1 и pkgver в PKGBUILD — это проверяет
# `make check-version`.

SHELL  := /bin/sh
PYTHON ?= python3

PKG     := gridsnap
VERSION := $(shell sed -n 's/^VERSION = "\([^"]*\)"/\1/p' zone-tiler.py | head -n1)
AUR_VERSION := $(shell sed -n 's/^pkgver=//p' PKGBUILD | head -n1)
MAN_VERSION := $(shell sed -n 's/^\.TH GRIDSNAP 1 "[^"]*" "gridsnap \([^"]*\)".*/\1/p' man/gridsnap.1 | head -n1)

DIST  := dist
BUILD := build

DEBROOT := $(BUILD)/deb
TARDIR  := $(BUILD)/$(PKG)-$(VERSION)
DATADIR := /usr/lib/gridsnap
BINDIR  := /usr/bin

MODULES := zone-tiler.py zone-daemon.py zone-editor.py zone_config.py \
           zone_geom.py zone_reflow.py zone-picker.py zone_picker_nav.py \
           zone_ipc.py zone_actions.py zone_tabs.py zone-tabstrip.py \
           zone_tabstrip_nav.py zone-hotcorners.py setup-hotkeys.py make-icon.py \
           zone_i18n.py zone_units.py zone_theme.py window_backend.py
CLIENTS := gridsnap-ctl
INSTALL_SH := install.sh install-launcher.sh install-daemon.sh

# Все файлы из поставки, где объявлена версия: `check-version` сверяет их
# поголовно, а не только те, что помнил список. Иначе `VERSION` тихо разъезжается
# по модулям — так чуть не вышло с zone-hotcorners и zone-tabstrip.
# Переменная ленивая (`=`): списки MODULES и CLIENTS определяются выше к этому
# моменту, но так список версионируемых файлов растёт вместе с ними.
VERSIONED = $(shell grep -l '^VERSION = ' $(MODULES) $(CLIENTS) 2>/dev/null | sort | tr '\n' ' ')

# Локализация: каталоги переводов. Язык исходника — английский, поэтому `en.po`
# нет; `ru` и `zh_CN` ложатся поверх английских строк.
LANGS       := ru zh_CN
LOCALEDIR   := locale
POT_FILES   := $(MODULES) $(CLIENTS)
POT_DIR     ?= po
XGETTEXT_FLAGS := --keyword=_ --from-code=UTF-8 --no-wrap --add-location=file --force-po \
                  --package-name=$(PKG) --package-version=$(VERSION)
APPLET_SRC  := contrib/applet/gridsnap@metapeka/applet.js
APPLET_NAME := gridsnap@metapeka
APPLET_DIR  := po/$(APPLET_NAME)

# Man-страницы — не gettext-домен, а локализованные страницы: po4a делает .po из
# roff, а собранные страницы ставятся в /usr/share/man/<язык>/man1/ (man-db сам
# выбирает по локали). Язык-источник — английский: каталога `en.po` нет,
# английская страница ложится копией источника, а русская и китайская собираются
# po4a (`po4a-translate` переливает roff: абзацы, `.B x` → `\fBx\fP`).
MAN_BUILD      := man/build
MAN_SOURCE     := man/gridsnap.1
MAN_ALIASES    := gridsnap-tiler.1 gridsnap-daemon.1 gridsnap-editor.1 \
                  gridsnap-picker.1 gridsnap-hotcorners.1 gridsnap-ctl.1 setup-hotkeys.1
MAN_DEFAULT    := en
MAN_TRANSLATED := ru zh_CN
MAN_LANGS      := $(MAN_DEFAULT) $(MAN_TRANSLATED)
# Языки без пробелов: groff не разрывает строку между иероглифами, поэтому
# собранной странице нужны точки переноса `\:` (см. tools/man-cjk.py).
MAN_CJK        := zh_CN
MAN_PO_DIR     := po/man
MAN_PO4A_CONF  := man/po4a.conf
MAN_POT        := $(MAN_PO_DIR)/gridsnap.1.pot
MAN_PO         := $(foreach l,$(MAN_TRANSLATED),$(MAN_PO_DIR)/$(l).po)
MAN_PAGES      := $(MAN_SOURCE) $(addprefix man/,$(MAN_ALIASES))

RELEASE_FILES := $(MODULES) $(CLIENTS) $(INSTALL_SH) zones.conf README.md SPEC.md LICENSE Makefile PKGBUILD
# `locale/` — сборка (`make mo`), но в tar.gz она обязательна: иначе install.sh
# из архива нечего ставить. Поэтому tarball зависит от mo.
# `gallery/` — готовые раскладки (зон + клавиши + правила): `zone_config` ищет их
# рядом с кодом в чекауте и в `<префикс>/share/gridsnap/gallery` после установки.
RELEASE_DIRS := icons docs contrib man gallery $(LOCALEDIR)
MANPAGES := gridsnap.1 gridsnap-tiler.1 gridsnap-daemon.1 gridsnap-editor.1 \
            gridsnap-picker.1 gridsnap-hotcorners.1 gridsnap-ctl.1 setup-hotkeys.1
ICON_SIZES := 16 22 24 32 48 64 128 256 512
ICON_RECIPES := $(foreach s,$(ICON_SIZES),install -Dm0644 "icons/gridsnap-$(s).png" "$(DEBROOT)/usr/share/icons/hicolor/$(s)x$(s)/apps/gridsnap.png";)
MAN_RECIPES := $(foreach p,$(MANPAGES),\
                 install -Dm0644 "$(MAN_BUILD)/$(MAN_DEFAULT)/man1/$(p)" "$(DEBROOT)/usr/share/man/man1/$(p)";\
                 install -Dm0644 "$(MAN_BUILD)/ru/man1/$(p)" "$(DEBROOT)/usr/share/man/ru/man1/$(p)";\
                 install -Dm0644 "$(MAN_BUILD)/zh_CN/man1/$(p)" "$(DEBROOT)/usr/share/man/zh_CN/man1/$(p)";)
# Каталоги переводов: по домену на язык (`make mo` собирает их из .po).
MO_RECIPES := $(foreach l,$(LANGS),\
                install -Dm0644 "$(LOCALEDIR)/$(l)/LC_MESSAGES/$(PKG).mo" \
                  "$(DEBROOT)/usr/share/locale/$(l)/LC_MESSAGES/$(PKG).mo";\
                install -Dm0644 "$(LOCALEDIR)/$(l)/LC_MESSAGES/$(APPLET_NAME).mo" \
                  "$(DEBROOT)/usr/share/locale/$(l)/LC_MESSAGES/$(APPLET_NAME).mo";)

.PHONY: all tarball deb aur version clean check-version pot mo check-i18n \
        man-pot man check-man-i18n test check verify lint smoke

all: tarball deb

# --------------------------------------------------------------- версия

check-version:
	@test -n "$(VERSION)" || { echo "make: не нашёл VERSION в zone-tiler.py" >&2; exit 1; }
	@test -n "$(VERSIONED)" || { echo "make: не нашёл файлов с VERSION" >&2; exit 1; }
	@for f in $(VERSIONED); do \
		v=$$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "$$f" | head -n1); \
		test "$$v" = "$(VERSION)" || { \
			echo "make: VERSION расходится: $$f=$$v, ожидается $(VERSION)" >&2; \
			exit 1; }; \
	done
	@test -n "$(AUR_VERSION)" || { echo "make: в PKGBUILD нет pkgver" >&2; exit 1; }
	@test "$(AUR_VERSION)" = "$(VERSION)" || { \
		echo "make: PKGBUILD pkgver=$(AUR_VERSION) не совпадает с VERSION=$(VERSION)" >&2; \
		exit 1; }
	@test -n "$(MAN_VERSION)" || { \
		echo "make: в .TH man/gridsnap.1 нет версии вида \"gridsnap $(VERSION)\"" >&2; \
		exit 1; }
	@test "$(MAN_VERSION)" = "$(VERSION)" || { \
		echo "make: man/gridsnap.1: версия $(MAN_VERSION) не совпадает с VERSION=$(VERSION)" >&2; \
		exit 1; }
	@v=$$(sed -n '1s/^[^(]*(*\([^-]*\)-.*/\1/p' debian/changelog); \
		test -n "$$v" || { echo "make: в debian/changelog нет версии" >&2; exit 1; }; \
		test "$$v" = "$(VERSION)" || { \
			echo "make: debian/changelog: версия $$v не совпадает с VERSION=$(VERSION)" >&2; \
			exit 1; }

version:
	@echo "$(VERSION)"

# ---------------------------------------------------------- локализация

# `make pot` — обновить шаблоны из кода. Отдельный домен апплета: xlet'ы берут
# перевод через `Gettext.bindtextdomain(uuid, ...)`, поэтому у него своё имя и
# свой `.mo`. Строки достаёт `xgettext` (`--keyword=_`), а не человек: забытая
# обёртка видна тестом `tests/test_i18n_wrapped.py`.
pot:
	@mkdir -p "$(POT_DIR)" "$(POT_DIR)/$(APPLET_NAME)"
	xgettext --language=Python $(XGETTEXT_FLAGS) \
		--output=$(POT_DIR)/$(PKG).pot $(POT_FILES)
	xgettext --language=JavaScript $(XGETTEXT_FLAGS) \
		--output=$(POT_DIR)/$(APPLET_NAME)/$(APPLET_NAME).pot $(APPLET_SRC)
	@echo "обновлены $(POT_DIR)/$(PKG).pot и $(POT_DIR)/$(APPLET_NAME)/$(APPLET_NAME).pot"

# `make mo` — собрать каталоги в locale/<язык>/LC_MESSAGES/. `--check` ловит
# битые записи и неверные подстановки, `--statistics` показывает полноту.
mo:
	@for lang in $(LANGS); do \
		mkdir -p "$(LOCALEDIR)/$$lang/LC_MESSAGES"; \
		echo "msgfmt $$lang"; \
		msgfmt --check -o "$(LOCALEDIR)/$$lang/LC_MESSAGES/$(PKG).mo" "po/$$lang.po"; \
		msgfmt --check -o "$(LOCALEDIR)/$$lang/LC_MESSAGES/$(APPLET_NAME).mo" \
			"$(APPLET_DIR)/$$lang.po"; \
	done
	@echo "собраны каталоги в $(LOCALEDIR)/"

# `make check-i18n` — то, что проверяет CI: каталоги собираются, шаблоны не
# разошлись с кодом (сверяются наборы `msgid`, а не файлы целиком: дата сборки
# в шапке меняется каждый раз), а каталога `en` нет — английский это msgid как есть.
check-i18n: mo
	@test ! -e "po/en.po" || { echo "make: po/en.po не нужен: английский — исходные строки" >&2; exit 1; }
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
		$(MAKE) --no-print-directory -s pot POT_DIR="$$tmp" >/dev/null; \
		grep '^msgid ' "po/$(PKG).pot" > "$$tmp/a"; \
		grep '^msgid ' "$$tmp/$(PKG).pot" > "$$tmp/b"; \
		diff -u "$$tmp/a" "$$tmp/b" || { \
			echo "make: po/$(PKG).pot разошёлся с кодом — запустите make pot" >&2; exit 1; }; \
		grep '^msgid ' "$(APPLET_DIR)/$(APPLET_NAME).pot" > "$$tmp/c"; \
		grep '^msgid ' "$$tmp/$(APPLET_NAME)/$(APPLET_NAME).pot" > "$$tmp/d"; \
		diff -u "$$tmp/c" "$$tmp/d" || { \
			echo "make: $(APPLET_DIR)/$(APPLET_NAME).pot разошёлся с кодом — запустите make pot" >&2; exit 1; }
	@echo "каталоги в порядке"

# ------------------------------------------------------ локализация man

# `make man-pot` — пересобрать шаблон po4a из исходной страницы. Заодно po4a
# подтягивает новые `msgid` в существующие каталоги (`msgmerge`), поэтому после
# правки `man/gridsnap.1` сначала он, потом перевод.
man-pot:
	@command -v po4a >/dev/null 2>&1 || { \
		echo "make: нужен po4a (Debian: apt install po4a)" >&2; exit 1; }
	po4a --no-translations "$(MAN_PO4A_CONF)"
	@echo "обновлён $(MAN_POT)"

# `make man` — собрать страницы в man/build/<язык>/man1/: ru и zh_CN через
# po4a, en — копией источника, шесть `.so`-псевдонимов — копией во все три
# языка (man-db резолвит `.so man1/gridsnap.1` внутри языка — проверено).
# `--keep 0` — писать страницу и при неполном каталоге: полноту проверяет
# `check-man-i18n`, а не молчаливое отсутствие файла.
man:
	@command -v po4a >/dev/null 2>&1 || { \
		echo "make: нужен po4a (Debian: apt install po4a)" >&2; exit 1; }
	po4a --no-update --keep 0 "$(MAN_PO4A_CONF)"
	@for lang in $(MAN_CJK); do \
		$(PYTHON) tools/man-cjk.py "$(MAN_BUILD)/$$lang/man1/gridsnap.1"; \
	done
	@mkdir -p $(foreach l,$(MAN_LANGS),"$(MAN_BUILD)/$(l)/man1")
	cp "$(MAN_SOURCE)" "$(MAN_BUILD)/$(MAN_DEFAULT)/man1/gridsnap.1"
	@for lang in $(MAN_LANGS); do \
		for page in $(MAN_ALIASES); do \
			cp "man/$$page" "$(MAN_BUILD)/$$lang/man1/$$page"; \
		done; \
	done
	@echo "собраны страницы в $(MAN_BUILD)/"

# `check-man-i18n` — то, что гоняет CI: страницы собираются и проходят
# `tests/test_man_i18n.py` — он же меряет то, что видит пользователь: выбор языка
# по локали, ширину в колонках и «cannot break line»; а английская страница
# байт-в-байт равна источнику.
check-man-i18n: man
	@$(PYTHON) tests/test_man_i18n.py
	@cmp -s "$(MAN_SOURCE)" "$(MAN_BUILD)/$(MAN_DEFAULT)/man1/gridsnap.1" || { \
		echo "make: английская страница разошлась с $(MAN_SOURCE)" >&2; exit 1; }
	@echo "man-страницы в порядке"

# --------------------------------------------------------------- tar.gz

tarball: check-version mo man
	@rm -rf "$(TARDIR)"
	@mkdir -p "$(TARDIR)" "$(DIST)"
	@cp -a $(RELEASE_FILES) "$(TARDIR)/"
	@cp -a $(RELEASE_DIRS) "$(TARDIR)/"
	@chmod 0755 "$(TARDIR)"/$(INSTALL_SH) "$(TARDIR)"/setup-hotkeys.py "$(TARDIR)"/make-icon.py \
		"$(TARDIR)"/$(CLIENTS)
	@tar -C "$(BUILD)" -czf "$(DIST)/$(PKG)-$(VERSION).tar.gz" "$(PKG)-$(VERSION)"
	@echo "собран $(DIST)/$(PKG)-$(VERSION).tar.gz:"
	@tar -tzf "$(DIST)/$(PKG)-$(VERSION).tar.gz" | sed 's/^/  /'

# --------------------------------------------------------------- deb

# Обёртка-запускалка в /usr/bin: $1 — модуль, $2 — имя команды.
define deb_wrapper
	printf '%s\n' '#!/bin/sh' 'exec $(PYTHON) $(DATADIR)/$(1) "$$@"' > '$(DEBROOT)/$(BINDIR)/$(2)'
	chmod 0755 '$(DEBROOT)/$(BINDIR)/$(2)'
endef

# man-страницы: в debian/ их сжимает dh_compress, а здесь debhelper нет, поэтому
# gzip -9n из обязательного пакета gzip (в .deb страницы лежат .gz).
deb: check-version mo man
	@rm -rf "$(DEBROOT)"
	@mkdir -p "$(DEBROOT)/DEBIAN" "$(DEBROOT)$(DATADIR)" \
	          "$(DEBROOT)$(BINDIR)" "$(DEBROOT)/usr/share/applications" \
	          "$(DEBROOT)/usr/share/gridsnap/gallery" \
	          "$(DEBROOT)/usr/share/doc/$(PKG)"
	@install -m 0644 $(MODULES) "$(DEBROOT)$(DATADIR)/"
	@install -m 0644 $(CLIENTS) "$(DEBROOT)$(DATADIR)/"
	@install -m 0644 zones.conf "$(DEBROOT)$(DATADIR)/zones.conf"
	@install -m 0755 install-launcher.sh install-daemon.sh "$(DEBROOT)$(DATADIR)/"
	@cp -a icons "$(DEBROOT)$(DATADIR)/icons"
	@install -m 0644 gallery/*.conf "$(DEBROOT)/usr/share/gridsnap/gallery/"
	@cp -a docs "$(DEBROOT)/usr/share/doc/$(PKG)/docs"
	@install -m 0644 README.md SPEC.md LICENSE "$(DEBROOT)/usr/share/doc/$(PKG)/"

	@$(MAN_RECIPES)
	@find "$(DEBROOT)/usr/share/man" -name '*.1' -exec gzip -9n {} +

	@$(MO_RECIPES)

	@$(call deb_wrapper,zone-tiler.py,gridsnap-tiler)
	@$(call deb_wrapper,zone-daemon.py,gridsnap-daemon)
	@$(call deb_wrapper,zone-editor.py,gridsnap-editor)
	@$(call deb_wrapper,zone-picker.py,gridsnap-picker)
	@$(call deb_wrapper,zone-hotcorners.py,gridsnap-hotcorners)
	@$(call deb_wrapper,gridsnap-ctl,gridsnap-ctl)
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
	# Поля control: описание — дословно то же, что в debian/control (это текст для
	# пользователя, а не про сборку): расхождение ловит tests/test_packaging_metadata.py.
	@echo 'Description: window tiling into zones for Cinnamon/X11'                                >> "$(DEBROOT)/DEBIAN/control"
	@echo ' GridSnap is a visual editor for workspace zones and a window tiler for'               >> "$(DEBROOT)/DEBIAN/control"
	@echo ' Cinnamon on X11. The active window is snapped into a zone with a hotkey;'             >> "$(DEBROOT)/DEBIAN/control"
	@echo ' the optional gridsnap-daemon places new windows automatically according to'           >> "$(DEBROOT)/DEBIAN/control"
	@echo ' the rules. It works on top of muffin through EWMH, and the window frame'              >> "$(DEBROOT)/DEBIAN/control"
	@echo ' matches the zone.'                                                                    >> "$(DEBROOT)/DEBIAN/control"

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

# ------------------------------------------------------------ разработка

# `make test` — только юнит-тесты без X-сервера: тот же гейт, что в CI. Быстрый
# цикл правки: ~25 с на 1430+ тестов. `env -u DISPLAY` обязателен: с живым
# дисплеем часть тестов лезет в настоящую сессию владельца.
test:
	@env -u DISPLAY $(PYTHON) -m unittest discover -s tests

# `make check` — компиляция всех модулей плюс тесты (первый job CI целиком).
check:
	@$(PYTHON) -m compileall -q $(MODULES) $(CLIENTS) tools tests
	@env -u DISPLAY $(PYTHON) -m unittest discover -s tests

# `make verify` — the single local gate; it delegates to `check`, so the tests run once.
verify: check

# `make lint` — ruff: syntax errors, undefined/redefined names (config in ruff.toml).
# Needs `ruff` on PATH (`uv tool install ruff`); the gate is local, CI still runs the
# compile step and the tests. Type checking is deliberately off: see AGENTS.md.
lint:
	@command -v ruff >/dev/null || { echo "make: ruff не найден: uv tool install ruff" >&2; exit 1; }
	@ruff check .

# `make smoke` — один дымовой сценарий на своём Xvfb (:89-:99) с muffin и
# тестовым окном: проверяется реальная раскладка пиксель в пиксель. Нужны
# Xvfb, muffin, dbus-run-session; каждый сценарий поднимает свой дисплей, так
# что живая сессия не задевается. Остальные — `sh tests/run-smoke.sh tests/smoke-*.sh`.
smoke:
	@env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh tests/smoke.sh

clean:
	@rm -rf "$(BUILD)" "$(DIST)" "$(MAN_BUILD)"
	@echo "удалены $(BUILD)/, $(DIST)/ и $(MAN_BUILD)/"

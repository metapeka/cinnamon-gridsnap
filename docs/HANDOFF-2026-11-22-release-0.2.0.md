# Передача: волна «релиз 0.2.0, упаковка и метаданные» (22.11.2026)

Предыдущая — `HANDOFF-2026-11-22-man-i18n.md` (перевод man-страниц и лицензия). Здесь состояние на
конец следующей сессии. Контекст прежней сессии в новый чат не переносится — всё существенное ниже.

## Где что лежит

| Что | Где |
|---|---|
| Проект (живой код) | `/home/db1/thinkrail/gridsnape`, ветка `main` = `9bda341` (влит PR #43 `9930232`, поверх — коммит с `sha256sums`) |
| Репозиторий на GitHub | `metapeka/cinnamon-gridsnap` |
| Влитый PR этой волны | **#43 «Релиз 0.2.0: восьмая man-страница, английские метаданные и рабочий AUR»** |
| Релиз | тег **`v0.2.0`** + GitHub Release с вложенным `gridsnap-0.2.0.tar.gz`; `sha256sums` в `PKGBUILD` — `116e8fc306771fd534d21b333c1aa5f51b278b0cb7421d99b5628c170a98763c` |
| Правила работы агентов | `AGENTS.md` в корне — начинать с него |
| План | `docs/ROADMAP.md`: хвост про `gridsnap-hotcorners` закрыт, «архив Debian» — репозиторная часть готова |
| Версия | `0.2.0` в пяти модулях, `pkgver`, `.TH` man-страницы и `debian/changelog`; сверяет `make check-version` |
| Проверки | `python3 -m unittest discover -s tests` → **1284 OK** (4 skipped); 11 дымовых сценариев — все зелёные |
| Наблюдатели | `tools/ci-watch.sh --pr N`, `tools/worker-watch.sh <dispatch-id>` |

## Что сделано (PR #43)

1. **Версия 0.2.0 и полный гейт версии.** `VERSION` живёт в **пяти** файлах — `zone-tiler.py`,
   `zone-daemon.py`, `zone-hotcorners.py`, `zone-tabstrip.py`, `gridsnap-ctl` — а `make check-version`
   сверял три, и два модуля молча остались бы на старой версии. Гейт переписан на сверку всех файлов
   из `MODULES`/`CLIENTS`, плюс `pkgver` и версия верхней записи `debian/changelog`.
2. **Восьмая man-страница.** У `gridsnap-hotcorners` не было ни своей страницы, ни раздела в общей:
   `lintian` ругался `no-manual-page`, `man gridsnap-hotcorners` ничего не находил, а README на неё
   уже ссылался. Теперь есть раздел «горячие края» (XInput2 raw events, HUD
   `zone-picker --hud --follow`, гашение `edge-tiling` и возврат настройки, ключи `--no-hud`,
   `--dry-run`, `--verbose`) и псевдоним `man/gridsnap-hotcorners.1`; перечисления в тексте и README
   приведены к семи командам. Оба каталога `po/man/` переведены.
3. **Метаданные пакетов — английские.** `Description:` в `debian/control`, `pkgdesc` в `PKGBUILD`,
   запись `0.2.0-1` в `debian/changelog`; из описания убран абзац про способ сборки (Debian Policy
   §3.4 — это текст для пользователя). Держит `tests/test_packaging_metadata.py`.
4. **AUR снова собирается.** `source` смотрит на `refs/tags/v$pkgver.tar.gz`, тег `v0.2.0` поставлен;
   `sha256sums` вписывается после публикации тега (см. грабли ниже). Новый `debian/watch` следит за
   тегами.
5. **CI.** Job «Штатный пакет Debian» снят с `continue-on-error` — `dpkg-buildpackage` + `lintian`
   стали блокирующим гейтом; в `package`-job добавлены проверки восьмой страницы в трёх языках и
   счёт 24 страницы в архиве.
6. **Документы.** README ×3, `docs/ROADMAP.md`, `docs/specs/man-i18n.md`.

## Что важно знать про эту волну (грабли)

- **`make deb` пишет свой `DEBIAN/control`** и дублировал описание: после перевода `debian/control`
  на английский в Makefile остался русский текст. Нашлось разбором собранного `.deb`
  (`dpkg-deb -I`), а не тестом и не глазами. Теперь тексты обязаны совпадать —
  `tests/test_packaging_metadata.py`, и там же проверка, что описания без кириллицы и короткое
  описание в стиле Debian.
- **`make` считает скобки в `$(shell …)` сам.** Проверка changelog через `sed` с `(` в шаблоне
  уронила гейт: `*** unterminated call to function 'shell': missing ')'`. Скобки в подобных
  выражениях — только внутри рецепта (там их видит shell), не в `$(shell …)`.
- **`sha256sums` нельзя посчитать заранее:** архив `…/archive/refs/tags/v0.2.0.tar.gz` появляется
  только после пуша тега. Порядок: PR влит → тег → Release → `curl -L … | sha256sum` → отдельный
  коммит в `main`. До этого в `PKGBUILD` осознанный `('SKIP')` с комментарием.
- **`lintian` на штатном пакете чист полностью** (и `no-manual-page`, и `initial-upload-closes-no-bugs`
  ушли). Проверять только на собранном пакете: `dpkg-buildpackage` оставляет в репозитории
  `debian/gridsnap/` с копиями спек, и `spec_validate` из-за них показывает дубли `id` — сносить
  (`rm -rf debian/gridsnap debian/.debhelper debian/tmp`).
- **Docker-подобного чистого окружения нет:** все проверки идут на машине владельца, где уже стоят
  `po4a`, `gettext`, `debhelper`, `dh-exec`, `lintian` и локали `zh_CN.UTF-8`/`ru_RU.UTF-8`;
  без локалей языковые проверки man-страниц пропускаются.
- **Слитые ветки-остатки нельзя удалить, пока живы их воркспейсы:** `git branch -D` на
  `i18n-interface` и `docs-site-l10n` отказывает (`used by worktree`), а воркспейсы владелец просил
  не трогать. `actions-window-zone` удалена.

## Как проверить, что всё живо

```bash
cd /home/db1/thinkrail/gridsnape            # после git pull: чекаут отставал на 130+ коммитов
env -u DISPLAY python3 -m unittest discover -s tests      # 1284 OK (4 skipped)
make check-version                                        # версия во всех пяти модулях, pkgver, changelog
make check-i18n && make check-man-i18n                    # каталоги gettext и po4a

# архив: 24 страницы (8 × три языка)
tmp=$(mktemp -d); make tarball >/dev/null && tar -xzf dist/gridsnap-0.2.0.tar.gz -C "$tmp"
"$tmp"/gridsnap-0.2.0/install.sh --prefix "$tmp/prefix" --no-launcher
find "$tmp/prefix/share/man" -type f | wc -l              # 24
man -M "$tmp/prefix/share/man" gridsnap-hotcorners | head -20
"$tmp"/gridsnap-0.2.0/install.sh --uninstall --prefix "$tmp/prefix"

# пакеты
make deb && dpkg-deb -I dist/gridsnap_0.2.0_all.deb | sed -n '/Description/,+3p'
dpkg-buildpackage -b -us -uc && lintian ../gridsnap_0.2.0-1_all.deb     # должно быть пусто
rm -rf debian/gridsnap debian/.debhelper debian/tmp

# AUR
bash -n PKGBUILD
curl -sI "https://github.com/metapeka/cinnamon-gridsnap/archive/refs/tags/v0.2.0.tar.gz" | head -1   # 302/200
sha256sum <(curl -sL "https://github.com/metapeka/cinnamon-gridsnap/archive/refs/tags/v0.2.0.tar.gz")

dbus-run-session -- sh tests/run-smoke.sh tests/smoke.sh
sh tools/ci-watch.sh --pr <N>                             # CI: 0 зелено / 1 упало / 2 зависло
```

## Открытые хвосты и мелочи

- **Архив Debian:** остались только внешние шаги — ITP-баг, спонсор, ревью. Репозиторная часть
  готова: лицензия, английские метаданные, `debian/watch`, чистый `lintian`, референсный tarball.
- **`debian/changelog` смешанный** — старая запись `0.1.0-1` русская (это история), новая английская.
- **Остатки прошлых волн:** ветки `i18n-interface`, `docs-site-l10n` и воркспейсы `workspace-1`,
  `workspace-2` живы (удалить ветки мешает то, что они вычекнуты в воркспейсах). Ветки волн 6 и 7
  убраны: `i18n-manpages`, `metapeka/man-i18n-*`, `man-i18n-home`, `fix/actions-contract`,
  `release-0.2.0`, `metapeka/release-pack`; Orca-воркспейсы `man-i18n-*`, `actions-contract` и
  `release-pack` — тоже.
- **Записи Orca** от прошлых волн остаются: Orca не даёт удалять попытки.
- **Китайские тексты машинные** (интерфейс и man-страница) — носителем не вычитаны.

## Очередь: что дальше (`docs/ROADMAP.md`)

1. **DPI (HiDPI-масштаб)** — следующая волна по решению владельца. Это единственный известный баг:
   при масштабе 1.5–2 сетка едет относительно того, что видит пользователь, потому что доли
   считаются от пиксельной рабочей области. Проверяется **без железа**: `Xvfb` + `GDK_SCALE=2` /
   `GDK_DPI_SCALE`, дымовые сценарии уже работают на Xvfb. Развилка для владельца: как хранить и
   показывать зоны при дробном масштабе (1.5 не выражается целыми пикселями).
2. **Wayland-бэкенд (niri)** — ждёт решения про стенд: на машине нет ни `niri`, ни `hyprctl`, ни
   `sway`. Варианты: поставить `sway` и гонять его headless (`WLR_BACKENDS=headless`) либо сначала
   вынести шов бэкенда окон на X11 без смены поведения.
3. **Галерея готовых раскладок** и **внешние подписки на события демона по сокету** — хвосты
   закрытых пунктов, низкий приоритет.

# Передача: GridSnap после волны 1 (2026-11-19)

Хэнд-офф перед новой сессией. Контекст предыдущей сессии: спеки, волна 0 и волна 1
сделаны, влиты в `main` и опубликованы; CI зелёный.

## Где что

| Что | Где |
|---|---|
| Проект (живой код) | `/home/db1/thinkrail/gridsnape`, ветка `main` |
| GitHub | `https://github.com/metapeka/cinnamon-gridsnap` (private), remote `origin` |
| CI | `.github/workflows/ci.yml`; последний зелёный прогон — run `44487212658` |
| Живой инструмент владельца | ярлык и `Super+Alt+1…4` → `zone-tiler.py` этого репозитория |
| Архив-исходник (не трогать) | `/mnt/f/kes/debian_dsh/ws_settings/zone-tiler` на NTFS |

## Что сделано

- **Спеки (spec-graph, 9 узлов)**: `SPEC.md`, `docs/architecture.md`,
  `docs/specs/{zone-tiler,zone-editor,hotkeys,launcher}.md` (`active`),
  `docs/specs/daemon.md` (`gridsnap-daemon`, **draft**), `docs/specs/reflow.md`
  (`zone-reflow`, **draft**).
- **Волна 0**: конфиг `~/.config/gridsnap/zones.conf` с фолбэком на файл рядом с кодом;
  `--version` и коды возврата 0/1/2/3; `apply_zone(window, rect)`; юнит-тесты без X; CI.
- **Волна 1**: секция `[rules]` (glob по `WM_CLASS` / `re:` по `class|title`);
  `zone-daemon.py` — автоукладка по `_NET_CLIENT_LIST`; `[daemon] reflow = off|neighbors`
  и `zone_reflow.py`; чистый `zone_config.py`; редактор сохраняет `[rules]`/`[daemon]`
  и не пишет висячие правила; `install-daemon.sh` + `contrib/gridsnap-daemon.desktop`.
- **Ревью**: первое — Request changes (блокер: утечка X-соединения), исправлено,
  повторное — Approve.

## Ключевые решения (почему так)

- **Демон на `_NET_CLIENT_LIST`**, а не `SubstructureNotify`: muffin репарентит клиента
  в frame-окно, и `MapNotify` приходит без `WM_CLASS`.
- **`apply_zone` закрывает только своё X-соединение**; демон переиспользует одно и
  переживает `SystemExit` — иначе долгоживущий демон исчерпал бы лимит клиентов Xorg.
- **`ConfigParser` с `delimiters=("=",)`**, а `[rules]` читается с сохранением регистра
  (иначе `re:\S…` мутирует в `\s…`).
- **Редактор не сохраняет** конфиг с правилами на отсутствующие зоны.

## Открыто и следующие шаги

- Сверить с владельцем `draft`-спеки (`daemon.md`, `reflow.md`) → перевести в `active`.
- Открытые пункты волны 1: восстановление расширенной зоны, когда новое окно встаёт в
  освобождённую; демон учитывает только монитор 0.
- Пакетирование (`deb`/tar.gz), юнит-проверка геометрии редактора — волна 0/3.
- Волна 2 и далее — по `docs/ROADMAP.md`; **начинать с согласия владельца**.

## Как проверять

- Юнит без X: `env -u DISPLAY python3 -m unittest discover -s tests` (165 тестов).
- Smoke: `sh tests/smoke.sh` (Xvfb + muffin) — все зоны «совпало с зоной».
- CI: `gh run list -R metapeka/cinnamon-gridsnap`.
- Демон вживую: `python3 zone-daemon.py --conf <conf> --verbose`.

## Грабли CI (уже учтены)

- `XDG_SESSION_TYPE=x11` — без него Mutter пишет «Unsupported session type» (нет logind).
- `cinnamon-settings-daemon` — без него muffin падает на схеме
  `org.cinnamon.settings-daemon.peripherals.touchscreen`.
- Готовности ждать опросом, а не фиксированными `sleep`.

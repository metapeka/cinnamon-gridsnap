# Передача: волна «хвосты очереди — справка `--json`, лишний аргумент, проверка «ё»» (22.11.2026)

Предыдущая — `HANDOFF-2026-11-22-yo-and-man-gallery.md` («ё» в именах, галерея в man, изоляция
дымовых сценариев). Здесь состояние на конец следующей сессии; контекст прежней в новый чат не
переносится.

## Где что лежит

| Что | Где |
|---|---|
| Проект (живой код) | `/home/db1/thinkrail/gridsnape`, ветка `main` (после `git pull` — `7ce308e` плюс коммит с этой передачей) |
| Репозиторий на GitHub | `metapeka/cinnamon-gridsnap` |
| Влитый PR этой волны | **#50 «Мелочи очереди: справка --json, отказ на лишний аргумент у profile и проверка «ё»»**, merge-коммит `7ce308e` |
| Ветка волны | `profile-nits-and-yo-check` — влита, вычекнута в этом воркспейсе (`workspace-4`), с `origin` удалена |
| Версия | без изменений — `0.2.0` |
| Правила работы агентов | `AGENTS.md` в корне |
| Проверки | юнит-тесты — **1431 OK** (9 skipped); дымовых `tests/smoke*.sh` — **13**, все зелёные; CI — 5 job'ов, зелёный (PR — run `45083791317`) |
| Наблюдатели | `tools/ci-watch.sh <run-id>` (лучше по `databaseId` из `gh run list`), `tools/worker-watch.sh <dispatch-id>` |
| Стенд проверки «ё» | был в `/tmp/yo-check.*`, лог и снимок — `.thinkrail/context/evidence/` (в git не идут, с воркспейсом пропадут) |

## Что сделано (PR #50, три коммита)

**1. `--json` в справке и man (`d926f5b`).** Ключ был описан как «profile export: печатать JSON в stdout»,
хотя `profile gallery --json` работает. Справка стала «profile export/gallery: печатать JSON (у export — в
stdout, а не в файл)»; в man, в `.SH ПАРАМЕТРЫ`, `--json` и `--name` перечисляют обоих владельцев
(`export`/`gallery` и `import`/`from-gallery`); в блоке `profile` появилась норма про лишний аргумент.
Переводы: gettext — 2 новых `msgid` в `po/en.po`/`po/zh_CN.po`; po4a — 3 `msgid` в `po/man/en.po`/
`zh_CN.po`, `fuzzy` снят. Сверка «не потеряли переводы»: в обоих каталогах было 220 записей, стало 220,
потеряно 3 (ровно переписанные), добавлено 3, изменено 0.

**2. Лишний позиционный аргумент — ошибка (`8d7f591`).** `profile_extra_word(args, action)` плюс проверка
в `cmd_profile`: код 2 с именем действия и словом, у `from-gallery` — с подсказкой «имя профиля задаёт
`--name`». У `export` второе слово — файл, оно не лишнее. Регрессия —
`tests/test_profiles.TestHeadless.test_cli_profile_rejects_extra_positional_words` (шесть действий +
проверка, что `export <имя> <файл>` по-прежнему работает).

**3. Спеки (`12d50c1`).** `docs/specs/profiles.md` — коды возврата и сколько слов берёт каждое действие;
`docs/specs/zone-tiler.md` — та же норма со ссылкой на `profile_extra_word`.

**4. Проверка «ё» живьём — разовым стендом** (решение владельца, постоянного сценария нет). Свой Xvfb
`:88`, свои `XDG_CONFIG_HOME`/`XDG_STATE_HOME`/`XDG_RUNTIME_DIR`, свой `--conf`, песочница
`GSETTINGS_BACKEND=keyfile`. Что получилось: редактор (драйвер загрузил `zone-editor.py` как модуль и
подал «ёлка» в тот же обработчик, который зовёт GTK при наборе) → `ввод 'ёлка': 'right' -> 'ёлка'`,
`on_save(): True`, в файле `ёлка = 0.5, 0, 0.5, 1` в `[zones]` и `ёлка = <Super><Alt>2` в `[keys]`;
`--shot` показал «ёлка» на холсте и в списке зон; `setup-hotkeys.py` в песочнице `keyfile` записал
`custom1 name = 'zone-tiler: ёлка'`, `--remove` вернул код 0 и очистил; живое `custom-list` владельца
(`custom0…custom14`) не тронуто.

Чтобы повторить, достаточно: поднять Xvfb на свободном дисплее, задать свои `XDG_*` и `GSETTINGS_BACKEND=keyfile`,
положить рядом `zones.conf` с зоной и `[keys]`, затем (а) подать имя в `Editor.on_name_changed(entry)` и
вызвать `on_save()`; (б) `python3 setup-hotkeys.py --conf <песочница>` и прочитать
`gsettings get org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings/custom1/ name`.

## Грабли этой волны

- **`msgmerge` без `--no-wrap` перепаковывает каталоги целиком.** Одна изменённая строка дала +927 строк
  в `po/en.po`; с `--no-wrap` диф стал 9 строк. Правку каталогов всегда делать `msgmerge --update --no-wrap
  --backup=none --no-fuzzy-matching`, иначе ревью тонет в переносах строк.
- **Страж на лишний аргумент нельзя ставить на `use-monitors`.** У него уже была своя проверка имени с
  более точным текстом («экраны и профили задаёт секция `[monitors]`»); общий страж ломал существующий
  `test_monitor_profiles.test_explicit_profile_name_is_usage_error`. Оставлено как было.
- **Устаревшие собранные артефакты валят тесты, а не только man.** Кроме известного `man/build` (иначе
  падают `BuiltPagesTest`) есть `locale/*.mo`: при изменении строки `test_i18n_catalogs` падает, пока не
  прогнан `make check-i18n`. Порядок гейтов: `make check-i18n && make check-man-i18n`, потом тесты.
- **`tools/ci-watch.sh --pr N` сразу после `gh pr create`** по-прежнему говорит «у PR нет прогонов»
  (прогон появляется секундой позже). Работает: взять `databaseId` из `gh run list --limit 2` и следить за ним.
- **Стенд проверки требует `sys.path` репозитория.** `zone-editor.py`, загруженный через `importlib` из
  чужого каталога, падает на `import zone_geom`. И для чтения живого хранилища надо снять не только
  `GSETTINGS_BACKEND`, но и `XDG_CONFIG_HOME` — иначе читается песочница, и пустой список выглядит как
  «настройки владельца пропали» (ложная тревога, стоившая пары минут).

## Как проверить, что всё живо

```bash
cd /home/db1/thinkrail/gridsnape            # после git pull
env -u DISPLAY python3 -m unittest discover -s tests      # 1431 OK (9 skipped)
make check-version && make check-i18n && make check-man-i18n
python3 -m compileall -q .

# дымовые — по очереди, у каждого свой дисплей :89-:99
for s in tests/smoke*.sh; do env -u DISPLAY dbus-run-session -- sh tests/run-smoke.sh "$s"; echo "$s rc=$?"; done

# новое поведение глазами
python3 zone-tiler.py profile gallery                     # список раскладок
python3 zone-tiler.py profile gallery mine; echo "rc=$?"  # rc=2: лишнее слово «mine»
python3 zone-tiler.py --help | grep -A1 -- '--json'
```

## Открытые хвосты и мелочи

- **Следующая задача — инверсия языка программы** (владелец подтвердил): сейчас язык исходника русский
  (597 строк, 498 обёрток `_()`), каталогов два — `en` и `zh_CN`, `ru.po` нет; должно стать: английский —
  исходник и дефолт для любой локали, русский и китайский — равноправные каталоги, man-источник английский.
  Цена: 20 файлов кода, 31 файл тестов (2278 строк с русскими литералами), man-источник 661 строка, спеки
  `zone-i18n`/`man-i18n`, правило языка в `AGENTS.md`. Начинать с брейншторм-вопросов владельцу.
- Имя с «ё» в редакторе проверено через тот же обработчик, который зовёт GTK при наборе
  (`on_name_changed`), а не настоящими нажатиями клавиш; постоянного сценария на имя с «ё» нет (решение
  владельца этой волны).
- Столкновения санированных имён (`«Всё»` и `«Вс!»` → одно имя) — по-прежнему не трогали.
- Прежняя очередь: приёмка трёх правок Wayland на стенде; пакет в архиве Debian; расширение Cinnamon для
  Wayland-нативных окон; апплет в панель (на владельце); повторный `setup-hotkeys.py --remove` после
  перезапуска Cinnamon (тоже на владельце).
- **Живой код владельца не тронут:** `/home/db1/thinkrail/gridsnape` остаётся на прежнем коммите со своими
  незакоммиченными `zones.conf` и `templates/`; выкат не заказывали.

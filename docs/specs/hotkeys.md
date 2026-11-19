---
id: gridsnap-hotkeys
type: module-design
status: active
title: "Модуль: горячие клавиши setup-hotkeys"
parent: gridsnap-architecture
depends-on: [zone-tiler]
---

_Черновик по `importing-a-codebase`: восстановлено из кода, владельцем не сверено._

## Ответственность

`setup-hotkeys.py` переносит секцию `[keys]` из `zones.conf` в пользовательские сочетания Cinnamon
через `gsettings`.

## Контракт

- источник пар «зона → сочетание» — CLI раскладчика: `zone-tiler.py keys`;
- имя сочетания — `zone-tiler: <зона>`;
- команда сочетания — `python3 <каталог>/zone-tiler.py apply <зона> --quiet`;
- схема списка — `org.cinnamon.desktop.keybindings` (`custom-list`); сами сочетания —
  `org.cinnamon.desktop.keybindings.custom-keybinding:<базовый путь>`, базовый путь
  `/org/cinnamon/desktop/keybindings/custom-keybindings/`;
- модуль владеет только сочетаниями с именем в префиксе `zone-tiler: `: перед установкой он сбрасывает
  ровно их, а все чужие id сохраняет;
- новые id берутся как `customN`, минуя уже занятые.

Команды: без флагов — установить/обновить; `--list` — только показать пары; `--remove` — удалить сочетания
`zone-tiler`.

## Границы

- Зависит от рабочего `gsettings` и схем Cinnamon; вне Cinnamon не работает.
- Конфиг не парсит: сочетания берёт у раскладчика, поэтому обязан идти с ним в одной версии.
- Чужие сочетания не трогает; раскладку не выполняет.

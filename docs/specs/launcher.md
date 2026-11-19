---
id: gridsnap-launcher
type: module-design
status: active
title: "Модуль: установка ярлыка и иконок"
parent: gridsnap-architecture
references: [zone-editor]
---

_Черновик по `importing-a-codebase`: восстановлено из кода, владельцем не сверено._

## Ответственность

`install-launcher.sh` ставит ярлык Gridsnap в меню Cinnamon (и по флагу — на рабочий стол) вместе с
иконками; `make-icon.py` эти иконки рисует.

## Контракт установки

- ярлыки: `~/.local/share/applications/gridsnap.desktop` и, с `--desktop`, `~/Desktop/gridsnap.desktop`;
- иконки: `~/.local/share/icons/hicolor/<N>x<N>/apps/gridsnap.png` для 16…512 и
  `~/.local/share/icons/hicolor/scalable/apps/gridsnap.svg`;
- `Exec` указывает на редактор: `python3 <каталог>/zone-editor.py`; `Icon=gridsnap`,
  `StartupWMClass=zone-editor.py`;
- `--remove` удаляет ярлыки, иконки и легаси-имена `zone-tiler`.

## Границы

- Только файловая установка «для себя»: пакет `deb`/tar.gz и системная установка — вне модуля.
- Иконки генерируются из кода (`make-icon.py`, cairo + встроенный SVG), `librsvg` не нужен.
- Ссылается на редактор, но продуктовой логики не содержит.

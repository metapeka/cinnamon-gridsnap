# Разведка: что из GridSnap работает под Wayland-сессией Cinnamon

_Замер, 22.11.2026, волна «шов оконного слоя + разведка». Источник — отчёт воркера
`.thinkrail/context/otchet-wayland-recon.md` (842 строки сырого вывода, не коммитится). Здесь — то, что
нужно знать до того, как браться за Wayland, и рецепт стенда, чтобы разведку не повторять._

## Вопрос

Cinnamon перестаёт считать свою Wayland-сессию экспериментальной (Cinnamon 6.8 / Linux Mint 23, выход
~Рождество 2026; X11 остаётся сессией по умолчанию). GridSnap управляет окнами через EWMH. Значит вопрос
не «как поддержать niri» (прежняя разведка целилась в него), а **что останется от нашего пути под
Wayland-сессией Cinnamon, где оконный слой — muffin в роли Wayland-композитора**.

## Стенд: вложенная Wayland-сессия muffin, без установки и без железа

`muffin --help` показывает и `--wayland`, и `--nested`, то есть композитор поднимается внутри обычной
X11-сессии: HiDPI-монитор и Wayland-логин владельца не нужны.

```sh
Xvfb :93 -screen 0 2560x1440x24 -nolisten tcp &

# вложенная сессия: bwrap → dbus-run-session → muffin
bwrap --dev-bind / / --overlay-src /usr/bin --tmp-overlay /usr/bin -- \
  dbus-run-session -- bash -c '
    export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=Cinnamon XDG_SESSION_DESKTOP=cinnamon
    export XDG_RUNTIME_DIR=/tmp/xdg-rt-recon DISPLAY=:93
    exec muffin --wayland --nested --display=:93'
```

Грабли стенда, которые стоит помнить:

- **Xwayland здесь не установлен, а muffin без него не даёт X-слоя вообще**:
  `Failed to init X11 display: Failed to execute child process "/usr/bin/Xwayland"`. Пакет `xwayland`
  пришлось скачать (`apt-get download`) и вынуть из него бинарник, а подложить — **только внутри
  mount-namespace** (`bwrap --overlay-src /usr/bin --tmp-overlay /usr/bin`), потому что путь
  `/usr/bin/Xwayland` зашит в `libmuffin.so.0`, а `/usr/bin` только для чтения. В систему ничего не
  ставилось. В требованиях «Cinnamon Wayland» xwayland надо помнить: на Linux Mint он есть.
- Дисплеи `:97`–`:99` заняты дымовыми сценариями репозитория — брать другие (`:93`).
- Вложенная сессия поднимается **без Cinnamon-shell** (только композитор), поэтому панели и её струта в
  замере не было; `org.Cinnamon` в шине не публикуется.
- Прогоны продукта писали историю `undo` в состояние владельца: держать `XDG_STATE_HOME` в `/tmp`
  (воркер это заметил и прибрал за собой).

## Что замерено

### Работает (`_NET_SUPPORTED` у muffin-Wayland полный, для Xwayland-окон он — полноценный EWMH-WM)

| Операция | Результат |
|---|---|
| `_NET_WM_NAME` корня | `Mutter (Muffin)` — WM-check на месте |
| `_NET_MOVERESIZE_WINDOW` (наш `apply`) | работает: `flags=0x2f01 x=100 y=100 w=400 h=300`, `--verify` совпал |
| `_NET_WM_STATE_MAXIMIZED_VERT/HORZ` | работает, включая снятие с восстановлением геометрии |
| `_NET_WM_DESKTOP` (переезд на стол) | работает, но окно при этом становится `WM_STATE=Iconic` |
| `_NET_ACTIVE_WINDOW` | работает на текущем столе |
| `_NET_CLIENT_LIST`, `_NET_FRAME_EXTENTS`, `_NET_WORKAREA` | читаются |
| мониторы через `xrandr` | работают |
| окно типа DOCK (оверлей пикера, HUD) | рисуется выше клиентов |
| X11-граб клавиатуры (`XGrabKeyboard`) и доставка клавиши | работают |

То есть **~90% нынешнего кода под Wayland-сессией Cinnamon живёт без изменений**, и полноценный второй
бэкенд для X11-окон не нужен.

### Ломается (три правки, по важности)

1. **`_NET_CURRENT_DESKTOP` не публикуется при старте Wayland-сессии** — в трёх запусках подряд свойства
   нет; появляется только после первого реального переключения стола («переключись на тот же» его не
   создаёт). Следствия на продукте: `zone-tiler.py focus` падает с `failed to determine the active
   workspace`, `wmctrl -d` не работает, и всё про «текущий стол» мертво. **Правильно:** считать текущим
   столом 0 (или стол активного окна), а не отказывать.
2. **GDK в Wayland-сессии по умолчанию идёт на wayland-бэкенд.** Рабочая область и масштаб тогда
   приезжают из Wayland (где нет X11-струтов), а окна двигаются в координатах Xwayland — два мира в
   одной программе. **Правильно:** `GDK_BACKEND=x11` (проверено замером) или брать область только из X11.
3. **Окно, уехавшее на другой стол, получает `WM_STATE=Iconic`**, и одним `_NET_ACTIVE_WINDOW` его не
   вернуть: сначала надо переключить стол. **Правильно:** различать «свёрнуто» и «на другом столе» явно
   (это про вкладки и `focus`).

### Не работает в принципе (ни при каких правках)

Для **Wayland-нативных** клиентов X-путь не работает: у muffin нет ни `wlr-foreign-toplevel-management`,
ни `wlr-layer-shell`:

```sh
$ strings libmuffin.so.0 | grep -ciE 'layer_shell|foreign_toplevel'
0
```

Публичного IPC про окна у muffin-Wayland нет — в шине только мониторы и бездействие:

```sh
$ xprop -root _NET_CURRENT_DESKTOP      # главная поломка, см. выше
_NET_CURRENT_DESKTOP:  not found

$ dbus-send --session --dest=org.freedesktop.DBus --print-reply /org/freedesktop/DBus \
    org.freedesktop.DBus.ListNames | grep -i cinnamon
  "org.cinnamon.Muffin.IdleMonitor", "org.cinnamon.Muffin.DisplayConfig", ...
# DisplayConfig — GetResources/ApplyConfiguration/GetCurrentState/... — только мониторы,
# ни ActivateWindow, ни MoveWindow
```

Единственный официальный способ двигать **все** окна, включая Wayland-нативные, — **расширение
Cinnamon** (JS внутри процесса композитора: `Meta.Window.move_resize_frame()`, `get_window_actors()`).
Layer-shell для оверлея при этом не нужен: Xwayland-DOCK-окно уже рисуется выше клиентов (замерено).

## Вывод для продукта

1. **Не бэкенд, а совместимость.** Для Xwayland-окон достаточно трёх правок выше — это небольшая волна,
   а не переписывание.
2. **Расширение Cinnamon** — отдельное решение владельца: без него Wayland-нативные окна зон не увидят.
3. Сегодняшняя модель «доля физической рабочей области» (PR #44) на Wayland требует пересмотра: масштаб
   там per-output и дробный; горячих краёв/HUD и стартового выделения зоны под курсором в пикере не
   будет (глобальной позиции курсора не отдаёт никто), компенсации `_NET_FRAME_EXTENTS` на
   Wayland-нативных окнах тоже, а хоткеи вместо `gsettings` задаёт файл композитора.

## Что осталось невыясненным

- Живая сессия на железе: доигрывают ли анимации сворачивания и смены стола (во вложенной сессии не
  доигрывают и это маскирует поведение); перезапускает ли muffin Xwayland при смене масштаба.
- Панель Cinnamon как Wayland-клиент: как её струт попадает в `_NET_WORKAREA`/Wayland-воркэри
  (вложенная сессия поднималась без shell'а; X11-струт проверен, работает).
- Несколько мониторов и дробный масштаб (`fractional-scale`) — во вложенном стенде был один вывод.
- Проверка самих трёх правок на живой Wayland-сессии — это следующая волна.

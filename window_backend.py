#!/usr/bin/env python3
"""window_backend — единственное место, где продукт говорит с оконной системой.

Сегодня оконная система — X11/EWMH: список окон, рамки, свойства `_NET_*`, команды
`XSendEvent`, рабочая область мониторов. Сюда же переезжают все вызовы `xprop`,
`xwininfo`, `xrandr` и класс работы с `libX11` (сейчас они живут в `zone-tiler.py`
вперемешку с логикой раскладки), чтобы у оконного слоя была **одна** граница.

Зачем: у Cinnamon появится полноценная Wayland-сессия (Cinnamon 6.8 / Linux Mint 23,
выход ~Рождество 2026), где оконный слой другой — EWMH нет, а управление окнами
устроено иначе. Пока весь разговор с окнами разбросан по раскладчику и демону,
любой Wayland-путь — это переписывание; с границей это отдельная реализация того же
интерфейса. Разведка (волна «шов + разведка») замером выясняет, что из этого
интерфейса вообще доступно под muffin в роли Wayland-композитора.

Состояние перехода: сигнатуры и докстринги зафиксированы здесь, тела функций
переносятся из `zone-tiler.py`/`zone-daemon.py` без смены поведения, а сами
`zone-tiler`/`zone-daemon` на время перехода держат обёртки с прежними именами —
вызывающие (демон, пикер, апплет, тесты) ничего не замечают.

Чего здесь намеренно **нет**:

* текстов для пользователя — сообщение и код возврата остаются у CLI (обёртки в
  `zone-tiler.py`); отсюда только исключение с причиной;
* наблюдателя за изменениями (подписка на `PropertyNotify` в цикле демона) — самая
  рискованная часть шва, её переносим отдельным шагом: в этой волне чтение и запись;
* горячих клавиш: `gsettings` — не оконный слой, и на Wayland механизм другой.

Правила для реализаций: поведение и тексты исключений не менять (их сверяют
существующие тесты), X-сервер может отсутствовать — ни один вызов не должен падать
трассировкой вместо понятного исключения.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import zone_units  # noqa: E402  (перевод единиц: логические пиксели GTK ↔ физические X)

# Ошибка оконного слоя: «окна/рабочей области нет или система не ответила».
# Текст — техническая причина; сообщение пользователю и код возврата задаёт CLI.
class WindowBackendError(RuntimeError):
    """Оконная система не ответила или не умеет нужную операцию."""


# --------------------------------------------------------------- чтение окон


def window_class_title(win):
    """WM_CLASS (класс, вторая строка) и заголовок окна через xprop."""
    raise NotImplementedError


def window_types(win):
    """Набор `_NET_WM_WINDOW_TYPE` окна через xprop (пустой при ошибке)."""
    raise NotImplementedError


def read_client_list():
    """Клиентские окна из `_NET_CLIENT_LIST` (EWMH) через `xprop -root`."""
    raise NotImplementedError


def window_workspace(win):
    """Номер виртуального рабочего стола окна (`_NET_WM_DESKTOP`) или None."""
    raise NotImplementedError


def current_workspace():
    """Номер активного рабочего стола (`_NET_CURRENT_DESKTOP`) или None."""
    raise NotImplementedError


def window_iconic(win):
    """Свёрнуто ли окно сейчас — `WM_STATE: Iconic` или `_NET_WM_STATE_HIDDEN`."""
    raise NotImplementedError


def read_active_window():
    """Активное окно и текст ошибки: ``(окно | None, причина | None)``."""
    raise NotImplementedError


def read_frame(window, extents):
    """Фактическая рамка окна (x, y, w, h) по xwininfo с учётом рамки WM."""
    raise NotImplementedError


def read_window_center(win):
    """Центр окна (px, py) по абсолютной геометрии `xwininfo`; None при ошибке."""
    raise NotImplementedError


def frame_extents(win):
    """Толщина рамки/титлбара оконного менеджера: (left, right, top, bottom)."""
    raise NotImplementedError


def read_session_id():
    """Идентификатор X-сессии (`_NET_...`/`xprop`) для различения переиспользованных XID."""
    raise NotImplementedError


# ------------------------------------------------------------- мониторы


def gdk_monitors():
    """``[(логическая рабочая область, масштаб)]`` из GDK; ``[]`` — GDK недоступен."""
    raise NotImplementedError


def monitors():
    """Рабочие области мониторов в физических пикселях X.

    ``WindowBackendError`` — ни GDK, ни резерв `_NET_WORKAREA` не ответили.
    """
    raise NotImplementedError


def monitor_scales():
    """Масштабы мониторов в том же порядке, что `monitors()`/`monitor_outputs()`."""
    raise NotImplementedError


def scale_at_point(point):
    """Масштаб монитора, в чью физическую рабочую область попала точка."""
    raise NotImplementedError


def monitor_outputs():
    """[(имя выхода, (x, y, w, h))] — по одному на монитор."""
    raise NotImplementedError


def xrandr_monitors():
    """Мониторы из `xrandr --listmonitors`; [] если утилиты нет или она упала."""
    raise NotImplementedError


def parse_xrandr_monitors(text):
    """[(имя, (x, y, w, h))] из вывода `xrandr --listmonitors`, в его порядке."""
    raise NotImplementedError


def stitch_monitor_names(outputs, areas):
    """Сшить имена выходов с рабочими областями: [(имя, область)]."""
    raise NotImplementedError


def monitor_for_point(monitors, point):
    """Рабочая область монитора, содержащего точку; иначе ближайшая по центру."""
    raise NotImplementedError


# ------------------------------------------------------------- запись (X11)


class X11:
    """Соединение с X-сервером и команды оконному менеджеру через `XSendEvent`.

    Сюда переезжает одноимённый класс из `zone-tiler.py` без смены поведения:
    `move_resize` (`_NET_MOVERESIZE_WINDOW`), `set_state` (`_NET_WM_STATE`),
    `iconify` (`WM_CHANGE_STATE`), `activate` (`_NET_ACTIVE_WINDOW`),
    `set_current_desktop` (`_NET_CURRENT_DESKTOP`), `atom`, `send`, `close`.

    Ошибка открытия соединения — `WindowBackendError`; чужое соединение не
    закрывается (им владеет вызывающий).
    """

    def __init__(self):
        raise NotImplementedError

    def close(self):
        """Закрыть соединение (своё)."""
        raise NotImplementedError

    def atom(self, name):
        """Атом по имени."""
        raise NotImplementedError

    def send(self, win, msg_type, values):
        """Отправить клиентское сообщение окну (`XSendEvent`)."""
        raise NotImplementedError

    def set_state(self, win, action, first, second=None):
        """`_NET_WM_STATE`: добавить/убрать/переключить состояние окна."""
        raise NotImplementedError

    def move_resize(self, win, x, y, w, h):
        """`_NET_MOVERESIZE_WINDOW`: переставить и изменить размер окна."""
        raise NotImplementedError

    def iconify(self, win):
        """Свернуть окно (`WM_CHANGE_STATE`)."""
        raise NotImplementedError

    def activate(self, win):
        """Поднять и активировать окно (`_NET_ACTIVE_WINDOW`)."""
        raise NotImplementedError

    def set_current_desktop(self, desktop):
        """Перейти на виртуальный стол (`_NET_CURRENT_DESKTOP`)."""
        raise NotImplementedError

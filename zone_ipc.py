#!/usr/bin/env python3
"""zone_ipc — кадрирование и диспетчер команд локального IPC GridSnap.

Чистый модуль: ни X, ни GTK, ни дисплея. Протокол — по одной JSON-строке на
команду (клиент → демон) и по одной JSON-строке на ответ (демон → клиент).
Кадрирование (`LineReader`), разбор команд (`parse_command`) и диспетчер с
**инъекцией обработчиков** (`Dispatcher`) отданы сюда, поэтому всё поведение
покрывается юнит-тестами без X-сервера. Слушающий UNIX-сокет (`Server`) —
тонкая обвязка вокруг того же кадрирования.

Формат запроса:  {"cmd": "status"} | {"cmd": "apply", "zone": "main", "window": 123}
Формат ответа:   {"ok": true, ...} | {"ok": false, "error": "...", "code": "..."}
"""

import contextlib
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from zone_i18n import _  # noqa: E402  (строки интерфейса и их перевод)

# Максимальная длина одной строки протокола: защита от неограниченного буфера.
MAX_LINE = 1 << 20  # 1 МиБ


class ProtocolError(Exception):
    """Битая строка протокола: не JSON, не объект или нет поля `cmd`."""


# ------------------------------------------------------------------ кадрирование


def encode_message(payload):
    """Ответ (или запрос) как JSON-строка протокола: компактный JSON + '\\n'."""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def parse_command(raw):
    """Разобрать одну строку команды в dict. Райзит `ProtocolError`.

    Принимает `bytes` (UTF-8) или `str`. Требует JSON-объект с непустым
    строковым полем `cmd`; остальные поля не проверяются — ими распоряжается
    обработчик конкретной команды.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(_("строка не в UTF-8: {error}").format(error=exc)) from None
    if not isinstance(raw, str):
        raise ProtocolError(_("строка протокола должна быть str или bytes"))
    text = raw.strip()
    if not text:
        raise ProtocolError(_("пустая строка протокола"))
    try:
        request = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(_("не разобрать JSON: {error}").format(error=exc)) from None
    if not isinstance(request, dict):
        raise ProtocolError(_("команда должна быть JSON-объектом"))
    cmd = request.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ProtocolError(_("в команде нет непустого строкового поля 'cmd'"))
    request["cmd"] = cmd.strip()
    return request


# ------------------------------------------------------------------- события
#
# Подписка — единственная команда, после которой соединение становится
# **только на чтение**: демон шлёт в него события и больше команд не разбирает
# (отписка = закрыть соединение). Формат строк — тот же JSON-протокол, поля взяты
# из `status` (`window` — hex-строка, `zone` — имя или None, `workspace` — номер
# стола EWMH, `monitor` — [x, y, w, h]).

# Владелец выбрал: рассказывать про окна и про режим «только эта зона» (замер — в
# `docs/HANDOFF-2026-11-22-gallery-wayland.md`). Подписка идёт **группами**, а не
# отдельными видами: клиенту обычно нужно «всё про окна» или «всё про режим».
EVENT_GROUPS = {
    "window": ("window-appeared", "window-closed", "window-moved"),
    "focus": ("focus-on", "focus-off"),
}
ALL_EVENT_GROUPS = tuple(EVENT_GROUPS)
SUBSCRIBE_CMD = "subscribe"


def event_group(kind):
    """Группа события (`'window'`/`'focus'`); неизвестный вид — `ProtocolError`."""
    for group, kinds in EVENT_GROUPS.items():
        if kind in kinds:
            return group
    raise ProtocolError(_("неизвестный вид события '{kind}'").format(kind=kind))


def parse_subscription(request):
    """Группы событий из запроса подписки. Райзит `ProtocolError`.

    Без поля `events` подписываемся на всё, что демон умеет (список групп — в
    `EVENT_GROUPS`): клиенту, которому нужны все события, незачем их перечислять.
    Пустой список — ошибка, а не «подписка на всё»: иначе опечатка в запросе
    молча давала бы поток, которого клиент не ждал. Порядок групп — как просил
    клиент, без повторов.
    """
    raw = request.get("events")
    if raw is None:
        return ALL_EVENT_GROUPS
    if not isinstance(raw, list) or not raw:
        raise ProtocolError(
            _("поле 'events' должно быть непустым списком групп: {known}").format(
                known=", ".join(ALL_EVENT_GROUPS)
            )
        )
    groups = []
    for item in raw:
        if not isinstance(item, str) or item not in EVENT_GROUPS:
            raise ProtocolError(
                _("неизвестная группа событий '{item}': можно {known}").format(
                    item=item, known=", ".join(ALL_EVENT_GROUPS)
                )
            )
        if item not in groups:
            groups.append(item)
    return tuple(groups)


def subscribe_answer(groups):
    """Ответ на подписку: какие группы приняты (строка протокола, `bytes`)."""
    return encode_message({"ok": True, "subscribed": list(groups)})


def wants_event(groups, kind):
    """Входит ли событие `kind` в подписанные группы."""
    return event_group(kind) in groups


def event_line(kind, **fields):
    """Строка события для подписчика (`bytes`); вид события проверяется.

    Поля кладутся рядом с `event`, порядок не важен, лишние поля не проверяются —
    форму каждого события задают сборщики ниже, а тест их и стережёт.
    """
    event_group(kind)
    payload = {"event": kind}
    payload.update(fields)
    return encode_message(payload)


def event_window_appeared(window, zone=None, workspace=None, monitor=None):
    """Окно появилось: `zone` — куда встало (None — никуда, напр. под `[ignore]`)."""
    return event_line(
        "window-appeared", window=hex(window), zone=zone, workspace=workspace, monitor=monitor
    )


def event_window_closed(window, zone=None):
    """Окно ушло; `zone` — где оно стояло в последний раз (None — не знаем)."""
    return event_line("window-closed", window=hex(window), zone=zone)


def event_window_moved(window, zone, previous=None, workspace=None, monitor=None):
    """Окно сменило зону, не появляясь и не закрываясь (команда, arrange, reflow)."""
    return event_line(
        "window-moved",
        window=hex(window),
        zone=zone,
        previous=previous,
        workspace=workspace,
        monitor=monitor,
    )


def event_focus_on(zone, window=None, minimized=0):
    """Включился режим «только эта зона»: окно `zone`, свернуто `minimized` чужих."""
    return event_line(
        "focus-on",
        zone=zone,
        window=hex(window) if window is not None else None,
        minimized=minimized,
    )


def event_focus_off(zone=None, restored=0, total=0):
    """Режим выключился: развёрнуто `restored` из `total` свёрнутых им окон."""
    return event_line("focus-off", zone=zone, restored=restored, total=total)


class LineReader:
    """Копит байты потока и выдаёт завершённые строки (по '\\n').

    Строки отдаются без завершающего перевода; незавершённый хвост остаётся в
    буфере (`tail`) до следующей порции. Превышение `max_line` на хвосте —
    `ProtocolError`: иначе некорректный клиент выест память демона.
    """

    def __init__(self, max_line=MAX_LINE):
        self.max_line = max_line
        self.buf = b""

    def feed(self, data):
        self.buf += data
        lines = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            lines.append(line)
        if len(self.buf) > self.max_line:
            raise ProtocolError(_("строка длиннее предела протокола"))
        return lines

    def tail(self):
        """Незавершённый хвост буфера (после EOF в нём может лежать команда)."""
        return self.buf


# ------------------------------------------------------------------- диспетчер


class Dispatcher:
    """Раскладывает строку команды по обработчикам, инъектированным снаружи.

    Обработчик получает разобранный запрос-dict и возвращает dict (или None —
    пустой ответ). Исключения обработчика (включая `SystemExit`: `fail()` в
    layout-коде бросает именно его) не вылетают наружу, а превращаются в
    `{"ok": false}`: одна плохая команда не должна ронять демон.
    `KeyboardInterrupt` не перехватывается.
    """

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})

    def register(self, name, handler):
        self.handlers[name] = handler
        return self

    def dispatch(self, raw):
        try:
            request = parse_command(raw)
        except ProtocolError as exc:
            return {"ok": False, "error": str(exc), "code": "bad_request"}
        name = request["cmd"]
        handler = self.handlers.get(name)
        if handler is None:
            return {
                "ok": False,
                "error": _("неизвестная команда: {name}").format(name=name),
                "code": "unknown_command",
            }
        try:
            result = handler(request)
        except (Exception, SystemExit) as exc:
            return {"ok": False, "error": str(exc), "code": "handler_error"}
        if result is None:
            result = {}
        if not isinstance(result, dict):
            return {
                "ok": False,
                "error": _("обработчик вернул не объект"),
                "code": "handler_error",
            }
        response = {"ok": True}
        response.update(result)
        return response


# ------------------------------------------------------------------ соединение


def _send_line(conn, payload):
    try:
        conn.sendall(encode_message(payload))
        return True
    except OSError:
        return False


def serve_connection(conn, dispatcher):
    """Читать команды из соединения до EOF и отвечать строкой на каждую.

    Возвращает список отправленных ответов (dict). Наружу не бросает: битая
    строка даёт ответ об ошибке, обрыв связи просто завершает обслуживание.
    Хвост без '\\n' на EOF тоже разбирается — так короткий клиент может не
    слать завершающий перевод строки.
    """
    reader = LineReader()
    responses = []
    while True:
        try:
            data = conn.recv(4096)
        except OSError:
            break
        if not data:
            tail = reader.tail().strip()
            if tail:
                response = dispatcher.dispatch(tail)
                responses.append(response)
                _send_line(conn, response)
            break
        try:
            lines = reader.feed(data)
        except ProtocolError as exc:
            response = {"ok": False, "error": str(exc), "code": "bad_request"}
            responses.append(response)
            _send_line(conn, response)
            break
        for line in lines:
            if not line.strip():
                continue  # пустую строку молча пропускаем (keepalive)
            response = dispatcher.dispatch(line)
            responses.append(response)
            if not _send_line(conn, response):
                return responses
    return responses


def read_response(conn, timeout=None):
    """Прочитать один JSON-ответ из соединения. Райзит `ProtocolError`/`OSError`."""
    if timeout is not None:
        conn.settimeout(timeout)
    reader = LineReader()
    while True:
        data = conn.recv(4096)
        if not data:
            raise ProtocolError(_("соединение закрыто без ответа"))
        lines = reader.feed(data)
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProtocolError(_("не разобрать ответ: {error}").format(error=exc)) from None
            if not isinstance(obj, dict):
                raise ProtocolError(_("ответ должен быть JSON-объектом"))
            return obj


# --------------------------------------------------------------------- сокет


def socket_dir():
    """Каталог для сокета демона: `$XDG_RUNTIME_DIR/gridsnap`.

    Без `XDG_RUNTIME_DIR` — приватный каталог в /tmp с uid пользователя, чтобы
    разные пользователи машины не мешали друг другу.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "gridsnap")
    return os.path.join(tempfile.gettempdir(), f"gridsnap-{os.getuid()}")


def default_socket_path():
    """Путь сокета по умолчанию: `<socket_dir()>/daemon.sock`."""
    return os.path.join(socket_dir(), "daemon.sock")


def _unlink_quiet(path):
    """Убрать файл сокета; ошибку удаления (уже удалён, нет прав) — глотаем."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def _is_live_socket(path):
    """True, если на `path` уже отвечает живой сервер (демон уже запущен)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class Server:
    """Слушающий UNIX-сокет: принимает соединения и раздаёт их диспетчеру.

    Права: каталог сокета — 0700, сам сокет — 0600. Чужой живой сокет по тому
    же пути не перетирается (`OSError`), мёртвый (остался от упавшего процесса)
    снимается и пересоздаётся.
    """

    def __init__(self, path, dispatcher, mode=0o600, backlog=8):
        self.path = path
        self.dispatcher = dispatcher
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            except OSError as exc:
                raise OSError(_("не создать каталог сокета {dir}: {error}").format(
                    dir=parent, error=exc)) from None
        if os.path.exists(path):
            if _is_live_socket(path):
                raise OSError(_("сокет {path} уже обслуживается другим процессом").format(
                    path=path))
            _unlink_quiet(path)  # мёртвый сокет от упавшего демона
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.bind(path)
            os.chmod(path, mode)
            self.sock.listen(backlog)
            self.sock.setblocking(False)
        except Exception:
            self.sock.close()
            raise

    def fileno(self):
        return self.sock.fileno()

    def accept_all(self, conn_timeout=2.0):
        """Принять все готовые соединения и обслужить их.

        `conn_timeout` ограничивает recv на каждом соединении: даже медленный
        или подвисший клиент не заблокирует X-цикл демона навсегда. Возвращает
        список ответов (для журнала).
        """
        responses = []
        while True:
            try:
                conn, _ = self.sock.accept()
            except BlockingIOError:
                break
            except OSError:
                break
            try:
                conn.settimeout(conn_timeout)
                responses.extend(serve_connection(conn, self.dispatcher))
            finally:
                conn.close()
        return responses

    def close(self):
        """Закрыть сокет и убрать файл. Идемпотентно."""
        with contextlib.suppress(OSError):
            self.sock.close()
        _unlink_quiet(self.path)

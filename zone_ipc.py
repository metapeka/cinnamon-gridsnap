#!/usr/bin/env python3
"""zone_ipc — framing and the command dispatcher of the local GridSnap IPC.

A pure module: no X, no GTK, no display. The protocol is one JSON line per
command (client → daemon) and one JSON line per response (daemon → client).
Framing (`LineReader`), command parsing (`parse_command`) and the dispatcher with
**handler injection** (`Dispatcher`) are given to this module, so all the behaviour
is covered by unit tests without an X server. The listening UNIX socket (`Server`) is
a thin wrapper around the same framing.

Request format:   {"cmd": "status"} | {"cmd": "apply", "zone": "main", "window": 123}
Response format:  {"ok": true, ...} | {"ok": false, "error": "...", "code": "..."}
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

from zone_i18n import _  # noqa: E402  (interface strings and their translation)

# The maximum length of one protocol line: protection against an unbounded buffer.
MAX_LINE = 1 << 20  # 1 MiB


class ProtocolError(Exception):
    """A broken protocol line: not JSON, not an object, or no `cmd` field."""


# ------------------------------------------------------------------- framing


def encode_message(payload):
    """A response (or a request) as a protocol JSON line: compact JSON + '\\n'."""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def parse_command(raw):
    """Parse one command line into a dict. Raises `ProtocolError`.

    Accepts `bytes` (UTF-8) or `str`. Requires a JSON object with a non-empty
    string field `cmd`; the other fields are not checked — they are up to the
    handler of the particular command.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(_("line is not UTF-8: {error}").format(error=exc)) from None
    if not isinstance(raw, str):
        raise ProtocolError(_("a protocol line must be str or bytes"))
    text = raw.strip()
    if not text:
        raise ProtocolError(_("empty protocol line"))
    try:
        request = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(_("cannot parse JSON: {error}").format(error=exc)) from None
    if not isinstance(request, dict):
        raise ProtocolError(_("the command must be a JSON object"))
    cmd = request.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ProtocolError(_("the command has no non-empty string field 'cmd'"))
    request["cmd"] = cmd.strip()
    return request


# -------------------------------------------------------------------- events
#
# Subscription is the only command after which the connection becomes
# **read-only**: the daemon sends events into it and parses no more commands
# (unsubscribing = closing the connection). The line format is the same JSON protocol,
# the fields come from `status` (`window` is a hex string, `zone` is a name or None,
# `workspace` is the number of the EWMH desktop, `monitor` is [x, y, w, h]).

# The owner chose: report on windows and on the "this zone only" mode (the measurement is in
# `docs/HANDOFF-2026-11-22-gallery-wayland.md`). A subscription goes **in groups**, not in
# separate kinds: a client usually wants "everything about windows" or "everything about the mode".
EVENT_GROUPS = {
    "window": ("window-appeared", "window-closed", "window-moved"),
    "focus": ("focus-on", "focus-off"),
}
ALL_EVENT_GROUPS = tuple(EVENT_GROUPS)
SUBSCRIBE_CMD = "subscribe"


def event_group(kind):
    """The group of an event (`'window'`/`'focus'`); an unknown kind raises `ProtocolError`."""
    for group, kinds in EVENT_GROUPS.items():
        if kind in kinds:
            return group
    raise ProtocolError(_("unknown event kind '{kind}'").format(kind=kind))


def parse_subscription(request):
    """The event groups of a subscription request. Raises `ProtocolError`.

    Without the `events` field we subscribe to everything the daemon can do (the list of
    groups is in `EVENT_GROUPS`): a client that needs all the events has no reason to list them.
    An empty list is an error, not "subscribe to everything": otherwise a typo in the request
    would silently give a stream the client was not waiting for. The order of the groups is the one
    the client asked for, without duplicates.
    """
    raw = request.get("events")
    if raw is None:
        return ALL_EVENT_GROUPS
    if not isinstance(raw, list) or not raw:
        raise ProtocolError(
            _("'events' must be a non-empty list of groups: {known}").format(
                known=", ".join(ALL_EVENT_GROUPS)
            )
        )
    groups = []
    for item in raw:
        if not isinstance(item, str) or item not in EVENT_GROUPS:
            raise ProtocolError(
                _("unknown event group '{item}': available {known}").format(
                    item=item, known=", ".join(ALL_EVENT_GROUPS)
                )
            )
        if item not in groups:
            groups.append(item)
    return tuple(groups)


def subscribe_answer(groups):
    """The subscription answer: which groups were accepted (a protocol line, `bytes`)."""
    return encode_message({"ok": True, "subscribed": list(groups)})


class Subscription(dict):
    """An accepted subscription: a handler response after which the connection stays.

    A normal response is sent by the dispatcher of `serve_connection`, which then serves
    the connection as a command one. A subscription is the exception: the handler returns this
    dict (its shape is exactly `subscribe_answer`), the dispatcher passes it through as is,
    and the transport hands the connection over to `Subscribers` and parses no more commands
    in it. Unsubscribing = closing the connection.
    """

    def __init__(self, groups):
        super().__init__(ok=True, subscribed=list(groups))
        self.groups = tuple(groups)


def wants_event(groups, kind):
    """Whether event `kind` belongs to the subscribed groups."""
    return event_group(kind) in groups


def event_line(kind, **fields):
    """A line for a subscriber (`bytes`); the event kind is checked.

    The fields sit next to `event`, the order does not matter, extra fields are not checked —
    the shape of every event is set by the builders below, and the test guards it.
    """
    event_group(kind)
    payload = {"event": kind}
    payload.update(fields)
    return encode_message(payload)


def event_window_appeared(window, zone=None, workspace=None, monitor=None):
    """A window appeared: `zone` is where it landed (None — nowhere, e.g. under `[ignore]`)."""
    return event_line(
        "window-appeared", window=hex(window), zone=zone, workspace=workspace, monitor=monitor
    )


def event_window_closed(window, zone=None):
    """A window left; `zone` is where it stood last (None — we do not know)."""
    return event_line("window-closed", window=hex(window), zone=zone)


def event_window_moved(window, zone, previous=None, workspace=None, monitor=None):
    """A window changed its zone without appearing or closing (a command, arrange, reflow)."""
    return event_line(
        "window-moved",
        window=hex(window),
        zone=zone,
        previous=previous,
        workspace=workspace,
        monitor=monitor,
    )


def event_focus_on(zone, window=None, minimized=0):
    """The "this zone only" mode came on: the window is in `zone`, `minimized` foreign collapsed."""
    return event_line(
        "focus-on",
        zone=zone,
        window=hex(window) if window is not None else None,
        minimized=minimized,
    )


def event_focus_off(zone=None, restored=0, total=0):
    """The mode went off: `restored` of the `total` windows it had collapsed were expanded."""
    return event_line("focus-off", zone=zone, restored=restored, total=total)


class LineReader:
    """Accumulates the bytes of a stream and yields complete lines (split on '\\n').

    Lines are returned without the terminating newline; an unfinished tail stays in the
    buffer (`tail`) until the next portion. Exceeding `max_line` on the tail is
    a `ProtocolError`: otherwise a broken client would eat the memory of the daemon.
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
            raise ProtocolError(_("the line is longer than the protocol limit"))
        return lines

    def tail(self):
        """The unfinished tail of the buffer (after EOF it may still hold a command)."""
        return self.buf


# ------------------------------------------------------------------- dispatcher


class Dispatcher:
    """Routes a command line to the handlers injected from outside.

    A handler receives the parsed request dict and returns a dict (or None —
    an empty response). Handler exceptions (including `SystemExit`: `fail()` in the
    layout code raises exactly that) do not escape outside, but turn into
    `{"ok": false}`: one bad command must not bring the daemon down.
    `KeyboardInterrupt` is not caught.
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
                "error": _("unknown command: {name}").format(name=name),
                "code": "unknown_command",
            }
        try:
            result = handler(request)
        except (Exception, SystemExit) as exc:
            return {"ok": False, "error": str(exc), "code": "handler_error"}
        if result is None:
            result = {}
        if isinstance(result, Subscription):
            # A subscription is not a normal response: its shape is already prepared, and the
            # connection moves on to the subscribers because of it (`serve_connection`). The
            # `{"ok": True}` wrapper would lose both things.
            return result
        if not isinstance(result, dict):
            return {
                "ok": False,
                "error": _("the handler did not return an object"),
                "code": "handler_error",
            }
        response = {"ok": True}
        response.update(result)
        return response


# ------------------------------------------------------------------ connection


def _send_line(conn, payload):
    try:
        conn.sendall(encode_message(payload))
        return True
    except OSError:
        return False


def serve_connection(conn, dispatcher, subscribers=None):
    """Read commands from the connection until EOF and answer each with a line.

    Returns the list of responses sent (dict). It raises nothing outside: a broken
    line gives an error response, a dropped connection merely ends the serving.
    A tail without '\\n' at EOF is parsed too — that way a short client need not
    send the terminating newline.

    The `subscribe` command (its response is a `Subscription`) is the only one after which
    the connection is **not** closed: it goes into `subscribers` (`Subscribers`) and
    from then on serves only events. Without a registry the connection is simply closed:
    there is no subscription, but no stuck client is left behind either.
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
                continue  # skip an empty line silently (keepalive)
            response = dispatcher.dispatch(line)
            responses.append(response)
            if not _send_line(conn, response):
                return responses
            if isinstance(response, Subscription):
                if subscribers is not None:
                    subscribers.add(conn, response.groups)
                return responses
    return responses


def read_response(conn, timeout=None):
    """Read one JSON response from the connection. Raises `ProtocolError`/`OSError`."""
    if timeout is not None:
        conn.settimeout(timeout)
    reader = LineReader()
    while True:
        data = conn.recv(4096)
        if not data:
            raise ProtocolError(_("the connection was closed without a response"))
        lines = reader.feed(data)
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProtocolError(_("cannot parse the response: {error}").format(error=exc)) from None
            if not isinstance(obj, dict):
                raise ProtocolError(_("the response must be a JSON object"))
            return obj


# --------------------------------------------------------------------- socket


def socket_dir():
    """The directory for the daemon socket: `$XDG_RUNTIME_DIR/gridsnap`.

    Without `XDG_RUNTIME_DIR` it is a private directory in /tmp with the uid of the user, so that
    different users of the machine do not get in each other's way.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "gridsnap")
    return os.path.join(tempfile.gettempdir(), f"gridsnap-{os.getuid()}")


def default_socket_path():
    """The default socket path: `<socket_dir()>/daemon.sock`."""
    return os.path.join(socket_dir(), "daemon.sock")


def _unlink_quiet(path):
    """Remove the socket file; an unlink error (already gone, no permission) is swallowed."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def _is_live_socket(path):
    """True if a live server already answers on `path` (the daemon is already running)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class Subscribers:
    """The subscribers of the daemon: non-blocking delivery of events, one line each.

    An event is news, not a queue: what was missed cannot be caught up, so
    a subscriber that does not keep up with reading (`send` did not fit into the socket buffer)
    is **dropped** by the daemon instead of buffering for it. A broken subscriber (`poll`
    saw EOF) is dropped the same way. Nothing is raised outside: a broken client
    must not bring the daemon down.

    A connection lives in the set until it breaks or is dropped; commands in it after
    the subscription are not parsed — only the delivery.
    """

    def __init__(self):
        # Connection -> subscribed groups. The key is the socket itself (identity), not the
        # descriptor number: it is reused after `close`.
        self._subs = {}

    def add(self, conn, groups):
        """Take a connection into the subscribers: from now on we only write to it."""
        conn.setblocking(False)
        self._subs[conn] = tuple(groups)
        return conn

    def has(self, conn):
        """Whether this connection is already a subscriber."""
        return conn in self._subs

    def groups(self, conn):
        """The subscribed groups of a connection (empty if it is not a subscriber)."""
        return self._subs.get(conn, ())

    def filenos(self):
        """The descriptors of the subscribers — for `select` in the daemon loop."""
        out = []
        for conn in self._subs:
            try:
                out.append(conn.fileno())
            except OSError:
                continue
        return out

    def count(self):
        """How many subscribers there are now."""
        return len(self._subs)

    def wants(self, kind):
        """Whether this event is needed by anyone: without it the line is not built."""
        return any(wants_event(groups, kind) for groups in self._subs.values())

    def drop(self, conn):
        """Remove and close a subscriber. `False` — it was not there any more."""
        if self._subs.pop(conn, None) is None:
            return False
        with contextlib.suppress(OSError):
            conn.close()
        return True

    def broadcast(self, kind, line):
        """Send an event line to the subscribers that need its group.

        Returns the number of subscribers the line went to. A slow or already
        broken subscriber is dropped — otherwise the buffer would pile up in the daemon.
        """
        sent = 0
        for conn, groups in list(self._subs.items()):
            if not wants_event(groups, kind):
                continue
            try:
                conn.sendall(line)
            except OSError:  # incl. BlockingIOError: it is not reading — we do not buffer
                self.drop(conn)
                continue
            sent += 1
        return sent

    def poll(self):
        """Take what the subscribers have sent: an empty read is a broken connection.

        After the subscription a connection is read-only, so the bytes it sends
        are simply thrown away: only the disconnect of the client matters.
        """
        dropped = 0
        for conn in list(self._subs):
            try:
                data = conn.recv(4096)
            except BlockingIOError:
                continue
            except OSError:
                dropped += self.drop(conn)
                continue
            if not data:
                dropped += self.drop(conn)
        return dropped

    def close_all(self):
        """Close all the subscribers (daemon exit)."""
        for conn in list(self._subs):
            self.drop(conn)


class Server:
    """A listening UNIX socket: accepts connections and hands them out to the dispatcher.

    Permissions: the socket directory is 0700, the socket itself is 0600. A live socket of someone
    else on the same path is not overwritten (`OSError`), a dead one (left by a crashed process)
    is removed and created again.

    `subscribers` is the registry of subscribers: connections go there after the
    `subscribe` command. The daemon passes in **the same** registry it delivers
    events to (`Daemon.subscribers`); without it the subscription is left without delivery, and
    the connection is simply closed.
    """

    def __init__(self, path, dispatcher, subscribers=None, mode=0o600, backlog=8):
        self.path = path
        self.dispatcher = dispatcher
        self.subscribers = subscribers if subscribers is not None else Subscribers()
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            except OSError as exc:
                raise OSError(_("cannot create the socket directory {dir}: {error}").format(
                    dir=parent, error=exc)) from None
        if os.path.exists(path):
            if _is_live_socket(path):
                raise OSError(_("socket {path} is already served by another process").format(
                    path=path))
            _unlink_quiet(path)  # a dead socket from a crashed daemon
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
        """Accept all the ready connections and serve them.

        `conn_timeout` bounds recv on every connection: even a slow
        or hung client will not block the X loop of the daemon forever. Returns the
        list of responses (for the log).
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
                responses.extend(
                    serve_connection(conn, self.dispatcher, self.subscribers)
                )
            finally:
                # A subscribed connection lives in the registry and is closed there, not
                # here: closing it here would mean cutting the subscription off right
                # after the response.
                if not self.subscribers.has(conn):
                    with contextlib.suppress(OSError):
                        conn.close()
        return responses

    def close(self):
        """Close the subscribers, the socket, and remove the file. Idempotent."""
        self.subscribers.close_all()
        with contextlib.suppress(OSError):
            self.sock.close()
        _unlink_quiet(self.path)

"""Runtime control socket for a permanently-running daemon.

The daemon runs as root under systemd (it needs ``CAP_NET_RAW`` for SocketCAN
and drives ``/dev/serial0``). Operators work from an unprivileged account, so
instead of ``sudo``-ing one-shot CLI invocations we expose a tiny control
surface over an ``AF_UNIX`` stream socket:

    voltdmf-ctl status
    voltdmf-ctl set-mode hold
    voltdmf-ctl setpoint hold | mountain
    voltdmf-ctl arm | disarm | reload

The privilege boundary is the socket's file mode: ``0660 root:voltdmf`` (set by
the ``.socket`` unit), plus adding the operator to the ``voltdmf`` group. No
setuid, no polkit, no D-Bus.

**Transport.** One connection carries one newline-terminated JSON request and
gets one newline-terminated JSON reply, then the server closes. Requests are
``{"cmd": "<name>", ...}``; replies are ``{"ok": true, ...}`` or
``{"ok": false, "error": "..."}``.

**Threading.** ``ControlServer`` runs an accept loop on a daemon thread. Reads
(``status``) are answered straight from a caller-supplied snapshot callback.
Anything that can transmit or mutate daemon state (``set-mode``/``arm``/
``disarm``/``setpoint``/``reload``) is pushed onto a :class:`queue.Queue` and
executed by the daemon's single main-loop thread -- the CAN transmit path and :class:`SafetyGate`
stay strictly single-threaded, exactly as when the reconciler was the only caller.

**systemd socket activation.** When started by systemd the listening socket is
passed as fd 3 with ``$LISTEN_FDS`` / ``$LISTEN_PID`` set;
:func:`inherited_listener` picks it up with no ``python-systemd`` dependency.
For bench work without systemd, ``ControlServer`` can bind a path itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: systemd hands inherited fds starting here (see ``sd_listen_fds(3)``).
SD_LISTEN_FDS_START = 3

#: Default control-socket path -- matches ``ListenStream=`` in voltdmf.socket
#: and ``RuntimeDirectory=voltdmf`` in voltdmf.service.
DEFAULT_SOCKET_PATH = "/run/voltdmf/control.sock"

#: Commands that mutate state / can transmit -- routed through the daemon loop.
_QUEUED_COMMANDS = frozenset(
    {"set-mode", "setpoint", "arm", "disarm", "reload", "walk-test"}
)

_ACCEPT_TIMEOUT_S = 0.5      # so stop() is responsive
_RECV_TIMEOUT_S = 5.0        # a client that opens and stalls must not wedge us
_MAX_REQUEST_BYTES = 64 * 1024
_DEFAULT_REPLY_TIMEOUT_S = 20.0   # a set-mode walk is up to ~4 taps * WALK_GAP_S


@dataclass
class Command:
    """A mutating request handed from the accept thread to the daemon loop."""

    name: str
    args: dict
    reply: "queue.Queue[dict]" = field(default_factory=lambda: queue.Queue(maxsize=1))


def inherited_listener() -> socket.socket | None:
    """Return the systemd-passed listening socket (fd 3), or ``None``.

    Follows the ``sd_listen_fds`` contract: honor the handoff only when
    ``$LISTEN_PID`` names *this* process, take the first fd, and clear the
    environment so a fork/exec child cannot re-inherit the claim.
    """
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    try:
        count = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        log.warning("LISTEN_FDS is not an int; ignoring socket activation")
        count = 0
    for var in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(var, None)
    if count < 1:
        return None
    if count > 1:
        log.warning("systemd passed %d fds; using only the first", count)
    sock = socket.socket(
        family=socket.AF_UNIX, type=socket.SOCK_STREAM, fileno=SD_LISTEN_FDS_START
    )
    sock.setblocking(True)
    return sock


class ControlServer:
    """Accept-loop server for the control socket.

    Parameters
    ----------
    cmd_queue:
        Queue the daemon loop drains; the server ``put``s :class:`Command`s on it.
    status_provider:
        Zero-arg callable returning a JSON-serialisable dict -- answered inline
        on the accept thread for ``status`` (read-only, no queue hop).
    listener:
        A ready listening socket (from :func:`inherited_listener`). If ``None``,
        the server binds ``path`` itself on :meth:`start`.
    path:
        Filesystem path to bind when ``listener`` is ``None`` (bench use).
    on_enqueue:
        Called right after a command is queued, so the daemon can cut its
        inter-loop sleep short and handle it now.
    reply_timeout_s:
        How long a queued command waits for the loop before the client gets a
        "daemon did not respond" error (the command may still run later).
    """

    def __init__(
        self,
        cmd_queue: "queue.Queue[Command]",
        *,
        status_provider: Callable[[], dict],
        listener: socket.socket | None = None,
        path: str | None = None,
        on_enqueue: Callable[[], None] | None = None,
        reply_timeout_s: float = _DEFAULT_REPLY_TIMEOUT_S,
        socket_mode: int = 0o660,
    ) -> None:
        if listener is None and path is None:
            raise ValueError("ControlServer needs an inherited listener or a path")
        self._queue = cmd_queue
        self._status_provider = status_provider
        self._listener = listener
        self._path = path
        self._bound_path: str | None = None  # set if we create the socket file
        self._on_enqueue = on_enqueue or (lambda: None)
        self._reply_timeout_s = reply_timeout_s
        self._socket_mode = socket_mode
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self._listener is None:
            self._listener = self._bind(self._path, self._socket_mode)
            self._bound_path = self._path
        self._listener.settimeout(_ACCEPT_TIMEOUT_S)
        self._thread = threading.Thread(
            target=self._serve, name="voltdmf-control", daemon=True
        )
        self._thread.start()
        log.info(
            "control socket up (%s)",
            self._bound_path or self._path or "inherited fd",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
        if self._bound_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._bound_path)

    @staticmethod
    def _bind(path: str, mode: int) -> socket.socket:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        with contextlib.suppress(OSError):
            os.chmod(path, mode)
        sock.listen(8)
        return sock

    # -- accept loop --------------------------------------------------------
    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                log.exception("control: accept() failed")
                continue
            with conn:
                try:
                    self._handle(conn)
                except Exception:  # never let one bad client kill the server
                    log.exception("control: connection handler crashed")

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(_RECV_TIMEOUT_S)
        buf = bytearray()
        while b"\n" not in buf:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                self._send(conn, {"ok": False, "error": "timed out reading request"})
                return
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_REQUEST_BYTES:
                self._send(conn, {"ok": False, "error": "request too large"})
                return
        raw = bytes(buf).split(b"\n", 1)[0].strip()
        if not raw:
            return
        try:
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            self._send(conn, {"ok": False, "error": f"bad request: {exc}"})
            return
        self._send(conn, self._dispatch(req))

    def _dispatch(self, req: dict) -> dict:
        name = req.get("cmd")
        if not isinstance(name, str):
            return {"ok": False, "error": "missing 'cmd'"}
        if name == "status":
            try:
                return {"ok": True, "state": self._status_provider()}
            except Exception as exc:  # a broken snapshot must not 500 the socket
                log.exception("control: status provider raised")
                return {"ok": False, "error": f"status unavailable: {exc}"}
        if name in _QUEUED_COMMANDS:
            cmd = Command(name=name, args={k: v for k, v in req.items() if k != "cmd"})
            self._queue.put(cmd)
            self._on_enqueue()
            try:
                return cmd.reply.get(timeout=self._reply_timeout_s)
            except queue.Empty:
                return {
                    "ok": False,
                    "error": (
                        f"daemon did not respond within {self._reply_timeout_s:.0f}s "
                        "(command may still apply -- check `status`)"
                    ),
                }
        return {"ok": False, "error": f"unknown command {name!r}"}

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        with contextlib.suppress(OSError):
            conn.sendall((json.dumps(obj) + "\n").encode())

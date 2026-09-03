"""Control socket: fd pickup, the JSON round-trip, and the queue hand-off."""

import json
import os
import queue
import socket
import threading
import time

import pytest

from voltdmf import control
from voltdmf.control import Command, ControlServer, inherited_listener


# --- inherited_listener() (systemd socket activation) ---------------------
def test_inherited_listener_ignores_foreign_pid(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert inherited_listener() is None


def test_inherited_listener_none_when_no_fds(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "0")
    assert inherited_listener() is None


def test_inherited_listener_clears_env(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "0")
    monkeypatch.setenv("LISTEN_FDNAMES", "control")
    inherited_listener()
    assert "LISTEN_PID" not in os.environ
    assert "LISTEN_FDS" not in os.environ
    assert "LISTEN_FDNAMES" not in os.environ


def test_inherited_listener_picks_up_the_passed_fd(tmp_path, monkeypatch):
    # Emulate systemd handing us an already-bound listening socket. Point the
    # pickup at a real fd we own rather than clobbering fd 3 (pytest's).
    path = str(tmp_path / "activated.sock")
    real = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    real.bind(path)
    real.listen(1)
    monkeypatch.setattr(control, "SD_LISTEN_FDS_START", real.fileno())
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")

    got = inherited_listener()
    assert got is not None
    assert got.getsockname() == path
    assert got.family == socket.AF_UNIX
    got.detach()  # shares real's fd -- let `real` own the close
    real.close()


# --- server round-trip helpers -----------------------------------------
def _ask(path, obj, timeout=5.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps(obj) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(bytes(buf).split(b"\n", 1)[0])


class _FakeLoop:
    """Drains the command queue on a thread, like Daemon.run() does."""

    def __init__(self, cmd_queue, handler):
        self._q = cmd_queue
        self._handler = handler
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.seen = []

    def _run(self):
        while not self._stop.is_set():
            try:
                cmd = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            self.seen.append((cmd.name, cmd.args))
            cmd.reply.put(self._handler(cmd))

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2.0)


@pytest.fixture
def server(tmp_path):
    made = {}

    def _make(*, status_provider=lambda: {"ok": 1}, reply_timeout_s=5.0):
        q: "queue.Queue[Command]" = queue.Queue()
        srv = ControlServer(
            q,
            status_provider=status_provider,
            path=str(tmp_path / "control.sock"),
            reply_timeout_s=reply_timeout_s,
        )
        srv.start()
        made["srv"] = srv
        made["q"] = q
        return srv, q, str(tmp_path / "control.sock")

    yield _make
    if "srv" in made:
        made["srv"].stop()


def test_status_answered_inline_without_queue(server):
    srv, q, path = server(status_provider=lambda: {"armed": False, "bus_active": True})
    reply = _ask(path, {"cmd": "status"})
    assert reply == {"ok": True, "state": {"armed": False, "bus_active": True}}
    assert q.empty()  # status never touches the loop


def test_status_provider_failure_is_reported(server):
    def boom():
        raise RuntimeError("snapshot broke")

    _srv, _q, path = server(status_provider=boom)
    reply = _ask(path, {"cmd": "status"})
    assert reply["ok"] is False
    assert "snapshot broke" in reply["error"]


@pytest.mark.parametrize("request_obj,expect_name", [
    ({"cmd": "set-mode", "mode": "hold", "force": False}, "set-mode"),
    ({"cmd": "setpoint", "mode": "mountain"}, "setpoint"),
    ({"cmd": "arm"}, "arm"),
    ({"cmd": "disarm"}, "disarm"),
    ({"cmd": "reload"}, "reload"),
    ({"cmd": "walk-test"}, "walk-test"),
    ({"cmd": "test-mode", "on": True}, "test-mode"),
    ({"cmd": "probe", "mode": "hold"}, "probe"),
])
def test_mutating_commands_go_through_the_queue(server, request_obj, expect_name):
    srv, q, path = server()
    with _FakeLoop(q, lambda cmd: {"ok": True, "handled": cmd.name}) as loop:
        reply = _ask(path, request_obj)
    assert reply == {"ok": True, "handled": expect_name}
    assert loop.seen[0][0] == expect_name


def test_set_mode_args_reach_the_handler(server):
    srv, q, path = server()
    captured = {}

    def handler(cmd):
        captured.update(cmd.args)
        return {"ok": True}

    with _FakeLoop(q, handler):
        _ask(path, {"cmd": "set-mode", "mode": "sport", "force": True})
    assert captured == {"mode": "sport", "force": True}


def test_bad_json_is_rejected(server):
    _srv, _q, path = server()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall(b"{not valid json\n")
        s.shutdown(socket.SHUT_WR)
        buf = s.recv(4096)
    reply = json.loads(buf.split(b"\n", 1)[0])
    assert reply["ok"] is False
    assert "bad request" in reply["error"]


def test_non_object_json_is_rejected(server):
    _srv, _q, path = server()
    reply = _ask(path, [1, 2, 3])
    assert reply["ok"] is False
    assert "object" in reply["error"]


def test_unknown_command_is_rejected(server):
    _srv, _q, path = server()
    reply = _ask(path, {"cmd": "self-destruct"})
    assert reply["ok"] is False
    assert "unknown command" in reply["error"]


def test_missing_cmd_is_rejected(server):
    _srv, _q, path = server()
    reply = _ask(path, {"mode": "hold"})
    assert reply["ok"] is False


def test_reply_timeout_when_loop_never_drains(server):
    _srv, _q, path = server(reply_timeout_s=0.2)
    t0 = time.monotonic()
    reply = _ask(path, {"cmd": "arm"})
    assert 0.15 < time.monotonic() - t0 < 3.0
    assert reply["ok"] is False
    assert "did not respond" in reply["error"]


def test_oversize_request_is_rejected(server):
    _srv, _q, path = server()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(path)
        s.sendall(b'{"cmd":"arm","pad":"' + b"x" * (70 * 1024))
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(bytes(buf).split(b"\n", 1)[0])
    assert reply["ok"] is False
    assert "too large" in reply["error"]


def test_partial_line_then_close_does_not_wedge_server(server):
    _srv, _q, path = server()
    # open, dribble a headerless fragment, hang up without a newline
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        s.sendall(b'{"cmd":"sta')
    # server must still serve the next client
    reply = _ask(path, {"cmd": "status"})
    assert reply["ok"] is True


def test_server_stop_is_idempotent(server):
    srv, _q, _path = server()
    srv.stop()
    srv.stop()  # must not raise


def test_bound_socket_file_removed_on_stop(tmp_path):
    path = str(tmp_path / "gone.sock")
    q: "queue.Queue[Command]" = queue.Queue()
    srv = ControlServer(q, status_provider=dict, path=path)
    srv.start()
    assert os.path.exists(path)
    srv.stop()
    assert not os.path.exists(path)


def test_requires_listener_or_path():
    with pytest.raises(ValueError):
        ControlServer(queue.Queue(), status_provider=dict)

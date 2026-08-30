"""voltdmf-ctl: request shaping, reply handling, exit codes."""

import json
import socket
import threading

import pytest

from voltdmf import ctl


class _CannedServer:
    """One-shot AF_UNIX server: reads a request line, returns a fixed reply.

    Records the parsed request in ``.request`` so tests can assert on shaping.
    """

    def __init__(self, tmp_path, reply):
        self.path = str(tmp_path / "canned.sock")
        self._reply = reply
        self.request = None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)
        self._t = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if buf:
                self.request = json.loads(bytes(buf).split(b"\n", 1)[0])
            if self._reply is not None:
                conn.sendall((json.dumps(self._reply) + "\n").encode())

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._sock.close()
        self._t.join(timeout=2.0)


def test_status_ok_prints_summary_and_exits_zero(tmp_path, capsys):
    reply = {"ok": True, "state": {"armed": True, "transmit_enabled": True,
                                   "drive_mode": "hold", "shift": "drive",
                                   "soc_percent": 30, "speed_mph": None,
                                   "bus_active": True, "triggers": ["on_start"],
                                   "manual_override": None}}
    with _CannedServer(tmp_path, reply) as srv:
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ARMED" in out
    assert "hold" in out


def test_set_mode_request_shape(tmp_path):
    with _CannedServer(tmp_path, {"ok": True, "result": "switched"}) as srv:
        rc = ctl.main(["--socket", srv.path, "set-mode", "sport", "--force"])
    assert rc == 0
    assert srv.request == {"cmd": "set-mode", "mode": "sport", "force": True}


def test_set_mode_defaults_force_false(tmp_path):
    with _CannedServer(tmp_path, {"ok": True, "result": "x"}) as srv:
        ctl.main(["--socket", srv.path, "set-mode", "mountain"])
    assert srv.request == {"cmd": "set-mode", "mode": "mountain", "force": False}


def test_arm_request_shape(tmp_path):
    with _CannedServer(tmp_path, {"ok": True, "armed": True}) as srv:
        ctl.main(["--socket", srv.path, "arm"])
    assert srv.request == {"cmd": "arm"}


def test_ok_false_reply_exits_one(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": False, "error": "daemon disarmed"}) as srv:
        rc = ctl.main(["--socket", srv.path, "set-mode", "hold"])
    assert rc == 1
    assert "daemon disarmed" in capsys.readouterr().err


def test_json_flag_prints_raw_reply(tmp_path, capsys):
    reply = {"ok": True, "result": "switched toward hold with 3 press(es)", "sent": 3}
    with _CannedServer(tmp_path, reply) as srv:
        rc = ctl.main(["--socket", srv.path, "--json", "set-mode", "hold"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == reply


def test_json_and_socket_flags_work_after_the_subcommand(tmp_path, capsys):
    reply = {"ok": True, "result": "x"}
    with _CannedServer(tmp_path, reply) as srv:
        rc = ctl.main(["set-mode", "hold", "--socket", srv.path, "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == reply
    assert srv.request == {"cmd": "set-mode", "mode": "hold", "force": False}


def test_not_running_exits_three(tmp_path, capsys):
    missing = str(tmp_path / "nope.sock")
    rc = ctl.main(["--socket", missing, "status"])
    assert rc == 3
    assert "running" in capsys.readouterr().err.lower()


def test_connection_refused_exits_three(tmp_path, capsys):
    # a path that exists as a socket file but nothing is accepting
    path = str(tmp_path / "dead.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    # not listening -> connect() gets ECONNREFUSED
    rc = ctl.main(["--socket", path, "status"])
    s.close()
    assert rc == 3


def test_empty_reply_exits_three(tmp_path, capsys):
    with _CannedServer(tmp_path, None) as srv:  # server closes without replying
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 3


def test_env_var_supplies_socket_path(tmp_path, monkeypatch):
    with _CannedServer(tmp_path, {"ok": True, "armed": False}) as srv:
        monkeypatch.setenv("VOLTDMF_CONTROL_SOCKET", srv.path)
        rc = ctl.main(["disarm"])
    assert rc == 0
    assert srv.request == {"cmd": "disarm"}


def test_bad_mode_is_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit) as ei:
        ctl.main(["--socket", str(tmp_path / "x"), "set-mode", "ludicrous"])
    assert ei.value.code == 2

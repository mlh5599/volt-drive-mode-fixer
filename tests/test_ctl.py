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
                                   "setpoint": "mountain", "floor_latched": False,
                                   "drive_mode": "hold", "shift": "drive",
                                   "soc_percent": 30, "soc_source": "poll",
                                   "soc_age_s": 4.0, "speed_mph": None,
                                   "uds_replies": 12, "uds_nrcs": 0,
                                   "bus_active": True, "manual_override": None}}
    with _CannedServer(tmp_path, reply) as srv:
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ARMED" in out
    assert "mountain" in out  # setpoint line
    assert "hold" in out      # drive mode


def test_status_shows_floor_latch(tmp_path, capsys):
    reply = {"ok": True, "state": {"armed": True, "transmit_enabled": True,
                                   "setpoint": "mountain", "floor_latched": True,
                                   "drive_mode": "hold", "shift": "drive",
                                   "bus_active": True}}
    with _CannedServer(tmp_path, reply) as srv:
        ctl.main(["--socket", srv.path, "status"])
    out = capsys.readouterr().out.lower()
    assert "floor latched for this key cycle" in out


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


def test_setpoint_request_shape_and_output(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": True, "setpoint": "mountain"}) as srv:
        rc = ctl.main(["--socket", srv.path, "setpoint", "mountain"])
    assert rc == 0
    assert srv.request == {"cmd": "setpoint", "mode": "mountain"}
    assert "setpoint = mountain" in capsys.readouterr().out


def test_walk_test_request_shape_and_output(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": True, "started": True,
                                  "origin": "hold"}) as srv:
        rc = ctl.main(["--socket", srv.path, "walk-test"])
    assert rc == 0
    assert srv.request == {"cmd": "walk-test"}
    out = capsys.readouterr().out
    assert "walk-test started" in out
    assert "hold" in out  # origin


def test_test_mode_request_shape_and_output(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": True, "test_mode": True,
                                  "setpoint": "hold"}) as srv:
        rc = ctl.main(["--socket", srv.path, "test-mode", "on"])
    assert rc == 0
    assert srv.request == {"cmd": "test-mode", "on": True}
    out = capsys.readouterr().out
    assert "test-mode ON" in out
    assert "reconciler suspended" in out


def test_test_mode_off_request_shape(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": True, "test_mode": False,
                                  "setpoint": "hold"}) as srv:
        rc = ctl.main(["--socket", srv.path, "test-mode", "off"])
    assert rc == 0
    assert srv.request == {"cmd": "test-mode", "on": False}
    assert "test-mode OFF" in capsys.readouterr().out


def test_test_mode_rejects_bad_state_at_argparse(tmp_path):
    with pytest.raises(SystemExit) as ei:
        ctl.main(["--socket", str(tmp_path / "x"), "test-mode", "maybe"])
    assert ei.value.code == 2


def test_probe_request_shape_and_output(tmp_path, capsys):
    with _CannedServer(tmp_path, {"ok": True, "started": True, "target": "mountain",
                                  "origin": "normal", "test_mode": True}) as srv:
        rc = ctl.main(["--socket", srv.path, "probe", "mountain"])
    assert rc == 0
    assert srv.request == {"cmd": "probe", "mode": "mountain"}
    out = capsys.readouterr().out
    assert "probe started" in out
    assert "mountain" in out


def test_probe_rejects_bad_mode_at_argparse(tmp_path):
    with pytest.raises(SystemExit) as ei:
        ctl.main(["--socket", str(tmp_path / "x"), "probe", "turbo"])
    assert ei.value.code == 2


def test_status_shows_test_mode_and_probe_verdict(tmp_path, capsys):
    state = {"transmit_enabled": True, "setpoint": "hold", "drive_mode": "normal",
             "test_mode": True,
             "probe": {"target": "hold", "verdict": "CURSOR_ONLY", "taps": 4,
                       "cursor_reached": True, "byte1_after": "normal"}}
    with _CannedServer(tmp_path, {"ok": True, "state": state}) as srv:
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-mode:  ON" in out
    assert "hold -> CURSOR_ONLY" in out


def test_setpoint_next_request_shape(tmp_path, capsys):
    """`next` is what an SW1 tap sends; the daemon resolves the detent."""
    with _CannedServer(tmp_path, {"ok": True, "setpoint": "off",
                                  "previous": "mountain"}) as srv:
        rc = ctl.main(["--socket", srv.path, "setpoint", "next"])
    assert rc == 0
    assert srv.request == {"cmd": "setpoint", "mode": "next"}
    assert "setpoint = off" in capsys.readouterr().out


def test_setpoint_off_request_shape(tmp_path):
    with _CannedServer(tmp_path, {"ok": True, "setpoint": "off"}) as srv:
        rc = ctl.main(["--socket", srv.path, "setpoint", "off"])
    assert rc == 0
    assert srv.request == {"cmd": "setpoint", "mode": "off"}


@pytest.mark.parametrize("bad", ["normal", "sport", "auto", "hold"])
def test_setpoint_rejects_a_non_detent_at_argparse(tmp_path, bad):
    """Bare `hold` is rejected on purpose: with two hold detents, a human
    typing it today could mean either. Config files still accept it."""
    with pytest.raises(SystemExit) as ei:
        ctl.main(["--socket", str(tmp_path / "x"), "setpoint", bad])
    assert ei.value.code == 2


def test_status_shows_the_detent_number_and_what_it_does(tmp_path, capsys):
    state = {"transmit_enabled": True, "setpoint": "hold-soc",
             "position_index": 1, "position_description": "hold the pack at 30%",
             "cycle": ["hold-soc", "hold-now", "mountain", "off"],
             "drive_mode": "normal", "floor_latched": False}
    with _CannedServer(tmp_path, {"ok": True, "state": state}) as srv:
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "selector:   1/4 hold-soc  (hold the pack at 30%)" in out


def test_status_calls_out_that_off_is_not_acting(tmp_path, capsys):
    state = {"transmit_enabled": True, "setpoint": "off", "position_index": 4,
             "position_description": "not acting -- car is on its own",
             "cycle": ["hold-soc", "hold-now", "mountain", "off"],
             "drive_mode": "normal", "floor_latched": False}
    with _CannedServer(tmp_path, {"ok": True, "state": state}) as srv:
        rc = ctl.main(["--socket", srv.path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "selector:   4/4 off" in out
    assert "not acting" in out


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

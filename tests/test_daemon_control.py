"""Daemon control-command handling: arm/disarm, manual set-mode, reload, and
the transmit-enable gate that sits in front of the CAN layer."""

import json

import pytest

from voltdmf.canio import CanInterface
from voltdmf.config import parse_config
from voltdmf.daemon import Daemon
from voltdmf.safety import RequestOutcome
from voltdmf.signals import DriveMode, ShiftPosition
from voltdmf.state import VehicleState


def _config(**over):
    raw = {
        "on_start": {"enabled": True, "target_mode": "mountain"},
        "soc_threshold": {"enabled": True, "target_mode": "hold",
                          "threshold_percent": 25, "reset_percent": 40},
        "trip_mode": {"enabled": False},
    }
    raw.update(over)
    return parse_config(raw)


def _daemon(*, dry_run=False, start_armed=False, config_path=None):
    return Daemon(_config(), lcd=False, dry_run=dry_run, start_armed=start_armed,
                  control_enabled=False, config_path=config_path)


class _FakeGate:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.request_calls = []

    def request_verbose(self, target, state, *, force=False):
        self.calls.append((target, force))
        return self.outcome

    def request(self, target, state):
        self.request_calls.append(target)
        return self.outcome.sent

    def cooldown_remaining(self):
        return 0.0


def _active_state(**kw):
    st = VehicleState(**kw)
    st.mark_signal_seen()
    return st


# --- arm / disarm --------------------------------------------------------
def test_boots_disarmed():
    d = _daemon()
    assert d._transmit_enabled() is False


def test_start_armed_flag():
    assert _daemon(start_armed=True)._transmit_enabled() is True


def test_dry_run_overrides_start_armed():
    assert _daemon(dry_run=True, start_armed=True)._transmit_enabled() is False


def test_arm_then_disarm():
    d = _daemon()
    assert d._handle_command("arm", {}) == {"ok": True, "armed": True}
    assert d._transmit_enabled() is True
    assert d._handle_command("disarm", {}) == {"ok": True, "armed": False}
    assert d._transmit_enabled() is False


def test_arm_refused_under_dry_run():
    d = _daemon(dry_run=True)
    reply = d._handle_command("arm", {})
    assert reply["ok"] is False
    assert "dry-run" in reply["error"]
    assert d._transmit_enabled() is False


# --- set-mode ----------------------------------------------------------
def test_set_mode_refused_when_disarmed():
    d = _daemon()
    d._state = _active_state()
    reply = d._handle_command("set-mode", {"mode": "hold"})
    assert reply["ok"] is False
    assert reply["would_switch_to"] == "hold"


def test_set_mode_unknown_mode():
    d = _daemon(start_armed=True)
    d._state = _active_state()
    reply = d._handle_command("set-mode", {"mode": "ludicrous"})
    assert reply["ok"] is False
    assert "unknown mode" in reply["error"]


def test_set_mode_calls_gate_and_sets_manual_override():
    d = _daemon(start_armed=True)
    d._state = _active_state(shift=ShiftPosition.DRIVE)
    d._gate = _FakeGate(RequestOutcome(True, 3, False, "switched toward hold"))
    reply = d._handle_command("set-mode", {"mode": "hold", "force": True})
    assert reply["ok"] is True
    assert reply["sent"] == 3
    assert d._gate.calls == [(DriveMode.HOLD, True)]
    assert d._manual_target is DriveMode.HOLD


def test_set_mode_blocked_outcome_is_not_ok_and_no_override():
    d = _daemon(start_armed=True)
    d._state = _active_state()
    d._gate = _FakeGate(RequestOutcome(False, 0, True, "blocked: shift is park"))
    reply = d._handle_command("set-mode", {"mode": "sport"})
    assert reply["ok"] is False
    assert reply["result"] == "blocked: shift is park"
    assert d._manual_target is None


def test_trigger_edge_clears_manual_override():
    d = _daemon(start_armed=True)
    d._state = _active_state()
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._manual_target = DriveMode.SPORT
    # on_start trigger fires once on an active bus -> override must clear
    d._scan_triggers()
    assert d._manual_target is None


# --- reload ----------------------------------------------------------
def test_reload_without_path():
    reply = _daemon()._handle_command("reload", {})
    assert reply["ok"] is False
    assert "reload unavailable" in reply["error"]


def test_reload_rereads_file(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "on_start:\n  enabled: false\n  target_mode: normal\n"
        "soc_threshold:\n  enabled: true\n  target_mode: hold\n"
        "  threshold_percent: 20\n  reset_percent: 35\n"
        "trip_mode:\n  enabled: false\n"
    )
    d = _daemon(config_path=str(cfg))
    reply = d._handle_command("reload", {})
    assert reply["ok"] is True
    assert reply["triggers"] == ["soc_threshold"]


def test_reload_bad_config_reports_error(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("on_start: {enabled: true, target_mode: banana}\n")
    d = _daemon(config_path=str(cfg))
    reply = d._handle_command("reload", {})
    assert reply["ok"] is False
    assert "reload failed" in reply["error"]


# --- status snapshot -------------------------------------------------
def test_status_snapshot_is_json_serialisable():
    d = _daemon(start_armed=True)
    d._state = _active_state(soc_percent=42, speed_mph=15,
                             drive_mode=DriveMode.NORMAL,
                             shift=ShiftPosition.DRIVE)
    d._gate = _FakeGate(RequestOutcome(False, 0, False, ""))
    snap = d._status_snapshot()
    json.dumps(snap)  # must not raise
    assert snap["armed"] is True
    assert snap["transmit_enabled"] is True
    assert snap["drive_mode"] == "normal"
    assert snap["triggers"] == ["on_start", "soc_threshold"]


def test_unhandled_command_name():
    reply = _daemon()._handle_command("frobnicate", {})
    assert reply["ok"] is False


# --- CanInterface transmit gate ------------------------------------
def test_can_tx_gate_suppresses_when_false():
    calls = []
    iface = CanInterface.__new__(CanInterface)
    iface._dry_run = False
    iface._tx_gate = lambda: False
    iface._bus = None
    assert iface._tx_suppressed() is True
    # send takes the no-op path (no bus needed, no RuntimeError)
    iface.send_mode_button_press()


def test_can_tx_gate_allows_when_true():
    iface = CanInterface.__new__(CanInterface)
    iface._dry_run = False
    iface._tx_gate = lambda: True
    iface._bus = None
    assert iface._tx_suppressed() is False


def test_can_dry_run_still_wins_over_tx_gate():
    iface = CanInterface.__new__(CanInterface)
    iface._dry_run = True
    iface._tx_gate = lambda: True
    assert iface._tx_suppressed() is True

"""Daemon control-command handling: arm/disarm, manual set-mode, setpoint,
reload, and the transmit-enable gate that sits in front of the CAN layer."""

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
        "policy": {"default_setpoint": "hold", "hold_threshold_percent": 33,
                   "hold_reset_percent": 41, "bar_failsafe_raw": 9},
        "soc_poll": {"enabled": True, "period_seconds": 10},
    }
    raw.update(over)
    return parse_config(raw)


def _daemon(*, start_armed=True, config_path=None):
    return Daemon(_config(), lcd=False, start_armed=start_armed,
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
def test_boots_armed():
    assert _daemon()._transmit_enabled() is True


def test_start_disarmed_flag():
    assert _daemon(start_armed=False)._transmit_enabled() is False


def test_arm_then_disarm():
    d = _daemon(start_armed=False)
    assert d._handle_command("arm", {}) == {"ok": True, "armed": True}
    assert d._transmit_enabled() is True
    assert d._handle_command("disarm", {}) == {"ok": True, "armed": False}
    assert d._transmit_enabled() is False


# --- set-mode ----------------------------------------------------------
def test_set_mode_refused_when_disarmed():
    d = _daemon(start_armed=False)
    d._state = _active_state()
    reply = d._handle_command("set-mode", {"mode": "hold"})
    assert reply["ok"] is False
    assert reply["would_switch_to"] == "hold"


def test_set_mode_unknown_mode():
    d = _daemon()
    d._state = _active_state()
    reply = d._handle_command("set-mode", {"mode": "ludicrous"})
    assert reply["ok"] is False
    assert "unknown mode" in reply["error"]


def test_set_mode_calls_gate_and_sets_manual_override():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE)
    d._gate = _FakeGate(RequestOutcome(True, 3, False, "switched toward hold"))
    reply = d._handle_command("set-mode", {"mode": "hold", "force": True})
    assert reply["ok"] is True
    assert reply["sent"] == 3
    assert d._gate.calls == [(DriveMode.HOLD, True)]
    assert d._manual_target is DriveMode.HOLD


def test_set_mode_blocked_outcome_is_not_ok_and_no_override():
    d = _daemon()
    d._state = _active_state()
    d._gate = _FakeGate(RequestOutcome(False, 0, True, "blocked: shift is park"))
    reply = d._handle_command("set-mode", {"mode": "sport"})
    assert reply["ok"] is False
    assert reply["result"] == "blocked: shift is park"
    assert d._manual_target is None


# --- setpoint --------------------------------------------------------
def test_setpoint_moves_the_reconciler_toggle():
    d = _daemon()
    assert d._reconciler.setpoint is DriveMode.HOLD
    reply = d._handle_command("setpoint", {"mode": "mountain"})
    assert reply == {"ok": True, "setpoint": "mountain"}
    assert d._reconciler.setpoint is DriveMode.MOUNTAIN


@pytest.mark.parametrize("bad", ["normal", "sport", "ludicrous"])
def test_setpoint_rejects_non_setpoint_modes(bad):
    d = _daemon()
    reply = d._handle_command("setpoint", {"mode": bad})
    assert reply["ok"] is False
    assert d._reconciler.setpoint is DriveMode.HOLD


# --- reconcile -----------------------------------------------------
def test_reconcile_acts_when_armed_and_clears_manual_override():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._manual_target = DriveMode.SPORT
    d._reconcile()
    # default setpoint is HOLD, car reads NORMAL -> a walk is requested
    assert d._gate.request_calls == [DriveMode.HOLD]
    assert d._manual_target is None


def test_reconcile_is_logging_only_when_disarmed():
    d = _daemon(start_armed=False)
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._reconcile()
    assert d._gate.request_calls == []


def test_reconcile_noop_when_already_in_desired_mode():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.HOLD)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._reconcile()
    assert d._gate.request_calls == []


def test_reconcile_noop_with_passive_default_on_a_healthy_pack():
    cfg = _config(policy={"default_setpoint": "auto", "hold_threshold_percent": 33,
                          "hold_reset_percent": 41, "bar_failsafe_raw": 9})
    d = Daemon(cfg, lcd=False, control_enabled=False)
    assert d._reconciler.setpoint is None
    d._state = _active_state(shift=ShiftPosition.DRIVE,
                             drive_mode=DriveMode.NORMAL, soc_percent=80)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._reconcile()
    assert d._gate.request_calls == []          # auto + healthy -> no target


def test_reconcile_waits_for_a_decodable_current_mode():
    d = _daemon()  # default_setpoint hold
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=None)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))
    d._reconcile()
    assert d._gate.request_calls == []          # no 0x1F4 yet -> don't walk


# --- reload ----------------------------------------------------------
_RELOAD_YAML = (
    "policy:\n  default_setpoint: hold\n  hold_threshold_percent: 20\n"
    "  hold_reset_percent: 35\n  bar_failsafe_raw: 8\n"
    "soc_poll:\n  enabled: true\n  period_seconds: 15\n"
)


def test_reload_without_path():
    reply = _daemon()._handle_command("reload", {})
    assert reply["ok"] is False
    assert "reload unavailable" in reply["error"]


def test_reload_rereads_file_and_keeps_setpoint(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(_RELOAD_YAML)
    d = _daemon(config_path=str(cfg))
    d._handle_command("setpoint", {"mode": "mountain"})  # driver's live choice
    reply = d._handle_command("reload", {})
    assert reply["ok"] is True
    assert reply["setpoint"] == "mountain"  # survives the reload
    assert reply["floor_latched"] is False
    assert d._config.policy.hold_threshold_percent == 20


def test_reload_bad_config_reports_error(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("policy: {default_setpoint: banana}\n")
    d = _daemon(config_path=str(cfg))
    reply = d._handle_command("reload", {})
    assert reply["ok"] is False
    assert "reload failed" in reply["error"]


# --- status snapshot -------------------------------------------------
def test_status_snapshot_is_json_serialisable():
    d = _daemon()
    d._state = _active_state(soc_percent=42, speed_mph=15,
                             drive_mode=DriveMode.NORMAL,
                             shift=ShiftPosition.DRIVE)
    d._gate = _FakeGate(RequestOutcome(False, 0, False, ""))
    snap = d._status_snapshot()
    json.dumps(snap)  # must not raise
    assert snap["armed"] is True
    assert snap["transmit_enabled"] is True
    assert snap["drive_mode"] == "normal"
    assert snap["setpoint"] == "hold"
    assert snap["floor_latched"] is False
    assert "dry_run" not in snap


def test_unhandled_command_name():
    reply = _daemon()._handle_command("frobnicate", {})
    assert reply["ok"] is False


# --- CanInterface transmit gate ------------------------------------
def test_can_tx_gate_suppresses_when_false():
    iface = CanInterface.__new__(CanInterface)
    iface._tx_gate = lambda: False
    iface._bus = None
    assert iface._tx_suppressed() is True
    # send takes the no-op path (no bus needed, no RuntimeError)
    iface.send_mode_button_press()


def test_can_tx_gate_allows_when_true():
    iface = CanInterface.__new__(CanInterface)
    iface._tx_gate = lambda: True
    iface._bus = None
    assert iface._tx_suppressed() is False


def test_can_tx_gate_absent_means_enabled():
    iface = CanInterface.__new__(CanInterface)
    iface._tx_gate = None
    assert iface._tx_suppressed() is False

"""Daemon control-command handling: arm/disarm, manual set-mode, setpoint,
reload, and the transmit-enable gate that sits in front of the CAN layer."""

import json
import time

import pytest

from voltdmf import daemon as daemon_mod
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


# --- post-walk settle guard --------------------------------------------
def test_reconcile_arms_walk_settle_after_a_sent_walk():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(True, 3, False, "ok"))  # request() -> sent
    d._reconcile()
    assert d._gate.request_calls == [DriveMode.HOLD]
    assert d._walk_settle_until > time.monotonic()


def test_reconcile_holds_off_while_walk_settle_is_active():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(True, 3, False, "ok"))
    d._reconcile()                              # walk 1 -> settle armed
    d._reconcile()                              # byte-1 still NORMAL (commit lag)
    assert d._gate.request_calls == [DriveMode.HOLD]   # not re-requested


def test_reconcile_does_not_arm_settle_when_no_walk_went_out():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(False, 0, True, "blocked"))  # request() -> False
    d._reconcile()
    assert d._walk_settle_until == 0.0


def test_setpoint_command_clears_walk_settle():
    d = _daemon()
    d._walk_settle_until = time.monotonic() + 999
    d._handle_command("setpoint", {"mode": "mountain"})
    assert d._walk_settle_until == 0.0


# --- walk-test (panel mode-walk self-test) ----------------------------
class _FakeController:
    """Stands in for ModeCycleController: each switch_to lands the mode."""

    def __init__(self, state, *, land=True):
        self._state = state
        self._land = land
        self.calls = []

    def switch_to(self, target, force=False):
        self.calls.append((target, force))
        if self._land:
            self._state.drive_mode = target   # simulate the committed byte-1
        return 1


def test_walk_test_refused_when_disarmed():
    d = _daemon(start_armed=False)
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.HOLD)
    reply = d._handle_command("walk-test", {})
    assert reply["ok"] is False
    assert d._walk_test_pending is False
    assert d._last_action == "WALK-TEST: DISARMED"  # driver sees it on the LCD


def test_walk_test_refused_without_a_decoded_mode():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=None)
    reply = d._handle_command("walk-test", {})
    assert reply["ok"] is False
    assert d._walk_test_pending is False
    assert d._last_action == "WALK-TEST: NO BUS"


def test_walk_test_queues_and_returns_started():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.SPORT)
    reply = d._handle_command("walk-test", {})
    assert reply == {"ok": True, "started": True, "origin": "sport"}
    assert d._walk_test_pending is True
    assert d._last_action == "WALK-TEST QUEUED"
    # a second request while one is queued is refused, and does NOT stomp the
    # LCD (it is already showing progress)
    assert d._handle_command("walk-test", {})["ok"] is False
    assert d._last_action == "WALK-TEST QUEUED"


def test_walk_test_cycle_walks_every_mode_then_restores(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.SPORT)
    d._state = st
    d._controller = _FakeController(st)
    d._walk_test_pending = True

    d._run_walk_test_cycle()

    assert d._walk_test_pending is False
    assert [c[0] for c in d._controller.calls] == [
        DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN,
        DriveMode.HOLD, DriveMode.SPORT,          # 4-mode cycle + restore origin
    ]
    assert all(force for _, force in d._controller.calls)
    res = d._walk_test_result
    assert res["ok"] is True
    assert res["origin"] == "sport"
    assert len(res["legs"]) == 5
    assert res["legs"][-1]["restore"] is True
    assert st.drive_mode is DriveMode.SPORT       # left where it started
    assert "OK" in d._last_action
    json.dumps(d._status_snapshot())              # result stays serialisable


def test_walk_test_cycle_reports_fail_when_a_leg_lands_wrong(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.SPORT)
    d._state = st
    d._controller = _FakeController(st, land=False)  # walk never commits
    d._walk_test_pending = True

    d._run_walk_test_cycle()

    res = d._walk_test_result
    assert res["ok"] is False
    assert "FAIL" in d._last_action
    # the SPORT legs still "land" (state never left SPORT); N/M/H do not
    scored = [leg for leg in res["legs"] if not leg["restore"]]
    assert [leg["ok"] for leg in scored] == [False, True, False, False]


# --- test-mode / focused probe -------------------------------------------
class _ProbeController:
    """Stands in for ModeCycleController inside _run_probe: switch_to lands
    the cursor (byte 4), the committed mode (byte 1), both, or neither."""

    def __init__(self, state, *, land_cursor=True, land_byte1=True, taps=3):
        self._state = state
        self._land_cursor = land_cursor
        self._land_byte1 = land_byte1
        self._taps = taps
        self.calls = []

    def switch_to(self, target, force=False):
        self.calls.append((target, force))
        if self._land_cursor:
            self._state.menu_cursor = target
        if self._land_byte1:
            self._state.drive_mode = target
        return self._taps


def test_test_mode_on_off_toggles_flag_and_suspends_reconcile():
    d = _daemon()
    d._reconciler.set_setpoint(DriveMode.HOLD)
    d._state = _active_state(shift=ShiftPosition.DRIVE, drive_mode=DriveMode.NORMAL)
    d._gate = _FakeGate(RequestOutcome(True, 1, False, "ok"))

    reply = d._handle_command("test-mode", {"on": True})
    assert reply["ok"] is True and reply["test_mode"] is True
    assert d._test_mode is True
    d._reconcile()                       # suspended -> gate untouched
    assert d._gate.request_calls == []

    assert d._handle_command("test-mode", {"on": False})["test_mode"] is False
    d._reconcile()                       # resumed -> acts
    assert d._gate.request_calls == [DriveMode.HOLD]


def test_probe_refused_when_disarmed():
    d = _daemon(start_armed=False)
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    reply = d._handle_command("probe", {"mode": "hold"})
    assert reply["ok"] is False
    assert d._probe_pending is None
    assert d._last_action == "PROBE: DISARMED"


def test_probe_refused_without_a_decoded_mode():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=None)
    reply = d._handle_command("probe", {"mode": "hold"})
    assert reply["ok"] is False
    assert d._last_action == "PROBE: NO BUS"
    assert d._probe_pending is None


def test_probe_unknown_mode_is_rejected():
    d = _daemon()
    d._state = _active_state(drive_mode=DriveMode.NORMAL)
    reply = d._handle_command("probe", {"mode": "ludicrous"})
    assert reply["ok"] is False
    assert "unknown mode" in reply["error"]


def test_probe_queues_and_refuses_a_second():
    d = _daemon()
    d._state = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    reply = d._handle_command("probe", {"mode": "mountain"})
    assert reply == {"ok": True, "started": True, "target": "mountain",
                     "origin": "normal", "test_mode": False}
    assert d._probe_pending is DriveMode.MOUNTAIN
    assert d._handle_command("probe", {"mode": "hold"})["ok"] is False
    assert d._probe_pending is DriveMode.MOUNTAIN


def test_run_probe_landed_when_cursor_and_byte1_reach_target(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st
    d._controller = _ProbeController(st)

    d._run_probe(DriveMode.HOLD)

    res = d._probe_result
    assert res["verdict"] == "LANDED"
    assert res["ok"] is True
    assert res["target"] == "hold" and res["origin"] == "normal"
    assert res["cursor_reached"] is True
    assert res["byte1_after"] == "hold"
    assert d._probe_pending is None
    assert d._last_action == "PROBE HOLD LANDED"
    json.dumps(d._status_snapshot())         # probe result stays serialisable


def test_probe_running_is_true_while_the_probe_actually_runs(monkeypatch):
    """``probe_running`` must cover the RUN, not just the queued window.

    It used to be ``_probe_pending is not None``, and ``_run_probe`` clears
    ``_probe_pending`` on its first line -- so the flag read False for the
    entire duration of the probe. A client polling "not probe_running, read
    the result" therefore got handed the PREVIOUS probe's result. That
    produced a full round of bogus verdicts on 2026-09-04: rows reporting
    LANDED whose byte1_after did not match their own target.
    """
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st
    seen = []

    class _Watching(_ProbeController):
        def switch_to(self, target, force=False):
            seen.append(d._status_snapshot()["probe_running"])
            return super().switch_to(target, force)

    d._controller = _Watching(st)
    assert d._status_snapshot()["probe_running"] is False   # idle
    d._handle_command("probe", {"mode": "hold"})
    assert d._status_snapshot()["probe_running"] is True    # queued
    d._run_probe(DriveMode.HOLD)

    assert seen == [True], "flag was False while the walk was in flight"
    assert d._status_snapshot()["probe_running"] is False   # done
    assert d._status_snapshot()["probe_queued"] is False


def test_probe_seq_advances_once_per_probe(monkeypatch):
    """A client can tell a fresh result from a stale one without a flag."""
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st
    d._controller = _ProbeController(st)

    assert d._status_snapshot()["probe_seq"] == 0
    d._run_probe(DriveMode.HOLD)
    assert d._probe_result["seq"] == 1
    assert d._status_snapshot()["probe_seq"] == 1
    d._run_probe(DriveMode.SPORT)
    assert d._probe_result["seq"] == 2
    assert d._status_snapshot()["probe_seq"] == 2


def test_probe_clears_running_when_the_walk_blows_up(monkeypatch):
    """A stuck ``probe_running`` would wedge every later probe's client.

    SafetyGate absorbs an exception from the controller into a blocked
    outcome rather than letting it out, so the probe completes normally with
    a MISS -- but the flag has to be clear either way, which is why
    ``_probe_active`` is released in a ``finally``.
    """
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st

    class _Exploding(_ProbeController):
        def switch_to(self, target, force=False):
            raise RuntimeError("bus went away mid-walk")

    d._controller = _Exploding(st)
    d._run_probe(DriveMode.HOLD)
    assert d._probe_result["verdict"] == "MISS"
    assert d._status_snapshot()["probe_running"] is False
    assert d._status_snapshot()["probe_seq"] == 1


def test_run_probe_cursor_only_when_byte1_never_commits(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st
    d._controller = _ProbeController(st, land_byte1=False)  # parked-car case

    d._run_probe(DriveMode.MOUNTAIN)

    res = d._probe_result
    assert res["verdict"] == "CURSOR_ONLY"
    assert res["ok"] is True
    assert res["cursor_reached"] is True
    assert res["byte1_after"] == "normal"


def test_run_probe_miss_when_cursor_never_arrives(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st
    d._controller = _ProbeController(st, land_cursor=False, land_byte1=False)

    d._run_probe(DriveMode.HOLD)

    res = d._probe_result
    assert res["verdict"] == "MISS"
    assert res["ok"] is False
    assert res["cursor_reached"] is False


def test_run_probe_blocked_by_precondition(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    # REVERSE is blocking even with allow_park=True
    st = _active_state(shift=ShiftPosition.REVERSE, drive_mode=DriveMode.NORMAL)
    d._state = st
    d._controller = _ProbeController(st)

    d._run_probe(DriveMode.HOLD)

    res = d._probe_result
    assert res["verdict"] == "BLOCKED"
    assert res["ok"] is False
    assert d._controller.calls == []         # never tapped


def test_run_probe_arms_and_disarms_the_per_tap_trace(monkeypatch):
    monkeypatch.setattr(daemon_mod, "WALK_TEST_LEG_SETTLE_S", 0.0)
    d = _daemon()
    st = _active_state(shift=ShiftPosition.PARK, drive_mode=DriveMode.NORMAL)
    d._state = st

    seen = []

    class _TracingController(_ProbeController):
        def switch_to(self, target, force=False):
            d._log_walk_tap(1, target)       # emulate the real tap_observer
            seen.append(d._trace_active)
            return super().switch_to(target, force)

    d._controller = _TracingController(st)
    d._run_probe(DriveMode.SPORT)

    assert seen == [True]                    # trace live during the walk
    assert d._trace_active is False          # cleared afterwards
    assert d._trace_tag == "probe"
    assert d._probe_result["taps_trace"][0]["tap"] == 1


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
    d._walk_settle_until = time.monotonic() + 999
    reply = d._handle_command("reload", {})
    assert reply["ok"] is True
    assert reply["setpoint"] == "mountain"  # survives the reload
    assert reply["floor_latched"] is False
    assert d._config.policy.hold_threshold_percent == 20
    assert d._walk_settle_until == 0.0  # re-evaluate against the new policy now


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

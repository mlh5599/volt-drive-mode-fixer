import pytest

from voltdmf.modecycle import ModeSwitchFailed
from voltdmf.safety import (AttemptBudget, MODE_SWITCH_COOLDOWN_S,
                            SafetyGate)
from voltdmf.signals import DriveMode, EngineState, ShiftPosition
from voltdmf.state import VehicleState


class FakeController:
    def __init__(self, result=1, raises=None):
        self.result = result
        self.raises = raises
        self.calls = 0

    def switch_to(self, target):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _state(**kw):
    # Default to a drivable car: DRIVE clears the shift precondition, so tests
    # that aren't about shift gating don't have to spell it out. SafetyGate
    # now blocks on UNKNOWN by default (decode_shift is confirmed on-vehicle).
    kw.setdefault("shift", ShiftPosition.DRIVE)
    st = VehicleState(**kw)
    st.mark_signal_seen()
    st.mark_ignition_seen()
    return st


def test_blocks_when_bus_quiet():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, VehicleState()) is False
    assert ctl.calls == 0


def test_blocks_in_the_rap_window_even_though_the_bus_is_active():
    """The case Session 13/14 exist for: bus_active alone stays True through
    the retained-power window (ignition off, door not opened), so ignition
    must be its own precondition -- see the SafetyGate._precondition_failure
    docstring."""
    ctl = FakeController()
    gate = SafetyGate(ctl)
    st = _state()
    st.last_ignition_signal_monotonic = None  # never seen 0x3ED this cycle
    outcome = gate.request_verbose(DriveMode.HOLD, st)
    assert outcome.sent is False
    assert outcome.blocked is True
    assert "ignition" in outcome.reason
    assert ctl.calls == 0


def test_allows_when_ignition_is_confirmed_on():
    ctl = FakeController(result=1)
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state()) is True
    assert ctl.calls == 1


@pytest.mark.parametrize("shift", list(ShiftPosition))
def test_shift_position_never_blocks_a_switch(shift):
    """PRNDL is not a precondition. The mode button is the centre-stack
    energy-mode menu, not a gear selector -- pressing it in Park is what a
    driver does, and gating on shift only stopped the reconciler settling
    the mode before the car moved."""
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state(shift=shift)) is True
    assert ctl.calls == 1


def test_switches_while_parked():
    """The case that used to be blocked and is now the point: sitting in the
    driveway with the car on, the daemon can walk to the target mode."""
    ctl = FakeController(result=3)
    gate = SafetyGate(ctl)
    outcome = gate.request_verbose(DriveMode.HOLD,
                                   _state(shift=ShiftPosition.PARK, speed_mph=0))
    assert outcome.sent is True
    assert outcome.blocked is False


def test_blocks_on_implausible_speed():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state(speed_mph=250)) is False
    assert ctl.calls == 0


def test_happy_path():
    ctl = FakeController(result=2)
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state(speed_mph=30)) is True
    assert ctl.calls == 1


def test_cooldown_suppresses_second_burst():
    ctl = FakeController()
    clock = FakeClock()
    gate = SafetyGate(ctl, cooldown_s=60.0, monotonic=clock)
    assert gate.request(DriveMode.HOLD, _state()) is True
    clock.t += 30.0
    assert gate.request(DriveMode.HOLD, _state()) is False
    assert ctl.calls == 1
    clock.t += 31.0
    assert gate.request(DriveMode.HOLD, _state()) is True
    assert ctl.calls == 2


def test_fail_passive_on_controller_error():
    ctl = FakeController(raises=RuntimeError("boom"))
    clock = FakeClock()
    gate = SafetyGate(ctl, monotonic=clock)
    # no exception propagates
    assert gate.request(DriveMode.HOLD, _state()) is False
    # cooldown still applied so we don't hammer a failing path
    clock.t += MODE_SWITCH_COOLDOWN_S / 2
    assert gate.request(DriveMode.HOLD, _state()) is False
    assert ctl.calls == 1


def test_default_cooldown_walks_a_manual_change_back_within_seconds():
    """The re-assert budget. Session 11 landed 35/35 legs in <=4 taps, so a
    minute-long cooldown would strand a driver who bumped the stalk in the
    wrong mode for most of that minute."""
    assert MODE_SWITCH_COOLDOWN_S <= 15.0
    ctl = FakeController()
    clock = FakeClock()
    gate = SafetyGate(ctl, monotonic=clock)
    assert gate.request(DriveMode.HOLD, _state()) is True
    clock.t += MODE_SWITCH_COOLDOWN_S + 0.1
    assert gate.request(DriveMode.HOLD, _state()) is True   # driver bumped it
    assert ctl.calls == 2


def test_zero_presses_reports_false():
    ctl = FakeController(result=0)
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state()) is False
    assert ctl.calls == 1


# -- a failed walk must still report the taps it put on the wire ----------
#
# Regression: request_verbose returned presses=0 for ANY exception, so every
# probe MISS row read "taps 0" even after MAX_WALK_TAPS taps went out. That
# under-counted taps on exactly the runs where the dropped-tap rate matters.
class _FailingController:
    def __init__(self, taps):
        self._taps = taps

    def switch_to(self, target, *, force=False):
        raise ModeSwitchFailed(f"never reached {target.value}", taps=self._taps)


def test_failed_walk_reports_the_taps_actually_sent():
    gate = SafetyGate(_FailingController(12), cooldown_s=0.0)
    outcome = gate.request_verbose(DriveMode.HOLD, _state(), force=True)
    assert outcome.presses == 12
    assert outcome.sent is False        # taps went out, the switch did not land
    assert outcome.blocked is True


def test_failed_walk_without_a_tap_count_still_reports_zero():
    """Any other exception has no ``taps``; must not crash on the getattr."""

    class _Boom:
        def switch_to(self, target, *, force=False):
            raise RuntimeError("bus exploded")

    gate = SafetyGate(_Boom(), cooldown_s=0.0)
    outcome = gate.request_verbose(DriveMode.HOLD, _state(), force=True)
    assert outcome.presses == 0
    assert outcome.blocked is True


# -- charge-sustaining block (shared with the reconciler) ----------------
def _spent(**kw):
    """Engine running, car in NORMAL, pack written off -- the 2026-09-05
    drive. A hand-typed ``set-mode hold`` here has to get a sentence back,
    not twelve taps and a timeout."""
    kw.setdefault("drive_mode", DriveMode.NORMAL)
    kw.setdefault("engine_state", EngineState.RUNNING)
    return _state(**kw)


def test_set_mode_hold_is_refused_with_a_reason_not_taps():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    out = gate.request_verbose(DriveMode.HOLD, _spent())
    assert out.blocked is True
    assert out.sent is False
    assert ctl.calls == 0
    assert out.reason.startswith("blocked: engine running in NORMAL")


def test_force_does_not_bypass_the_charge_sustaining_block():
    """force only means "walk even if the car already reads the target" --
    it cannot conjure a menu entry the car is not offering."""
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request_verbose(DriveMode.HOLD, _spent(), force=True).blocked
    assert ctl.calls == 0


def test_sport_is_still_allowed_with_the_engine_running():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.SPORT, _spent()) is True
    assert ctl.calls == 1


def test_no_block_when_the_engine_is_not_running():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _spent(engine_state=EngineState.OFF))
    assert ctl.calls == 1


# -- AttemptBudget: give up on a target the car will not take -------------
def _budget(max_attempts=3):
    return AttemptBudget(max_attempts=max_attempts)


def test_a_fresh_budget_gives_up_on_nothing():
    b = _budget()
    assert b.exhausted(DriveMode.HOLD) is False
    assert b.give_up_reason(DriveMode.HOLD) is None
    assert b.target is None and b.attempts == 0


def test_it_gives_up_only_after_the_last_attempt():
    b = _budget()
    for n in (1, 2):
        assert b.record_attempt(DriveMode.HOLD) == n
        assert b.exhausted(DriveMode.HOLD) is False
    assert b.record_attempt(DriveMode.HOLD) == 3
    assert b.exhausted(DriveMode.HOLD) is True


def test_the_give_up_reason_names_the_mode_and_the_way_out():
    b = _budget()
    for _ in range(3):
        b.record_attempt(DriveMode.MOUNTAIN)
    reason = b.give_up_reason(DriveMode.MOUNTAIN)
    assert "MOUNTAIN" in reason and "3 attempts" in reason
    assert "SW1" in reason  # the driver-facing escape hatch


def test_a_different_target_starts_its_own_count():
    """New intent, new budget -- a spent HOLD budget must not blank MOUNTAIN."""
    b = _budget()
    for _ in range(3):
        b.record_attempt(DriveMode.HOLD)
    assert b.exhausted(DriveMode.MOUNTAIN) is False
    assert b.record_attempt(DriveMode.MOUNTAIN) == 1
    assert b.exhausted(DriveMode.HOLD) is False  # the count moved with it


def test_reaching_the_target_clears_the_count():
    b = _budget()
    b.record_attempt(DriveMode.HOLD)
    b.record_attempt(DriveMode.HOLD)
    b.note_reached(DriveMode.HOLD)
    assert b.attempts == 0 and b.target is None


def test_reaching_some_other_mode_does_not_clear_the_count():
    """The driver bumping the car into SPORT says nothing about our HOLD."""
    b = _budget()
    b.record_attempt(DriveMode.HOLD)
    b.note_reached(DriveMode.SPORT)
    assert b.attempts == 1 and b.target is DriveMode.HOLD


def test_reset_hands_the_whole_budget_back():
    b = _budget()
    for _ in range(3):
        b.record_attempt(DriveMode.HOLD)
    b.reset()
    assert b.exhausted(DriveMode.HOLD) is False
    assert b.snapshot()["gave_up"] is False


def test_snapshot_is_json_shaped():
    b = _budget()
    b.record_attempt(DriveMode.HOLD)
    assert b.snapshot() == {"target": "hold", "attempts": 1,
                            "max_attempts": 3, "gave_up": False}


def test_a_budget_of_one_gives_up_immediately():
    b = _budget(max_attempts=1)
    b.record_attempt(DriveMode.HOLD)
    assert b.exhausted(DriveMode.HOLD) is True


def test_a_budget_below_one_is_a_configuration_error():
    with pytest.raises(ValueError):
        AttemptBudget(max_attempts=0)

import pytest

from voltdmf.modecycle import ModeSwitchFailed
from voltdmf.safety import SafetyGate
from voltdmf.signals import DriveMode, ShiftPosition
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
    return st


def test_blocks_when_bus_quiet():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, VehicleState()) is False
    assert ctl.calls == 0


@pytest.mark.parametrize("shift", [ShiftPosition.PARK, ShiftPosition.REVERSE,
                                   ShiftPosition.NEUTRAL])
def test_blocks_on_non_drive_shift(shift):
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state(shift=shift)) is False
    assert ctl.calls == 0


def test_blocks_on_unknown_shift_by_default():
    ctl = FakeController()
    gate = SafetyGate(ctl)
    assert gate.request(DriveMode.HOLD, _state(shift=ShiftPosition.UNKNOWN)) is False
    assert ctl.calls == 0


def test_allow_unknown_shift_opt_in():
    ctl = FakeController()
    gate = SafetyGate(ctl, allow_unknown_shift=True)
    assert gate.request(DriveMode.HOLD, _state(shift=ShiftPosition.UNKNOWN)) is True
    assert ctl.calls == 1


def test_allow_park_opt_in_lets_park_through():
    # the panel walk-test builds its gate with allow_park=True
    ctl = FakeController()
    gate = SafetyGate(ctl, allow_park=True)
    assert gate.request(DriveMode.HOLD, _state(shift=ShiftPosition.PARK)) is True
    assert ctl.calls == 1


@pytest.mark.parametrize("shift", [ShiftPosition.REVERSE, ShiftPosition.NEUTRAL])
def test_allow_park_still_blocks_reverse_and_neutral(shift):
    ctl = FakeController()
    gate = SafetyGate(ctl, allow_park=True)
    assert gate.request(DriveMode.HOLD, _state(shift=shift)) is False
    assert ctl.calls == 0


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
    clock.t += 10.0
    assert gate.request(DriveMode.HOLD, _state()) is False
    assert ctl.calls == 1


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
    gate = SafetyGate(_FailingController(12), cooldown_s=0.0, allow_park=True)
    outcome = gate.request_verbose(DriveMode.HOLD, _state(), force=True)
    assert outcome.presses == 12
    assert outcome.sent is False        # taps went out, the switch did not land
    assert outcome.blocked is True


def test_failed_walk_without_a_tap_count_still_reports_zero():
    """Any other exception has no ``taps``; must not crash on the getattr."""

    class _Boom:
        def switch_to(self, target, *, force=False):
            raise RuntimeError("bus exploded")

    gate = SafetyGate(_Boom(), cooldown_s=0.0, allow_park=True)
    outcome = gate.request_verbose(DriveMode.HOLD, _state(), force=True)
    assert outcome.presses == 0
    assert outcome.blocked is True

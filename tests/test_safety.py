import pytest

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

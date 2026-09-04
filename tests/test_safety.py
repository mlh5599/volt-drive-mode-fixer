import pytest

from voltdmf.modecycle import ModeSwitchFailed
from voltdmf.safety import MODE_SWITCH_COOLDOWN_S, SafetyGate
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

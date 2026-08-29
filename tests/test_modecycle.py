import pytest

from voltdmf.modecycle import (
    ModeCycleController,
    ModeSwitchFailed,
    ModeUnknownError,
    PressCountingModeTracker,
    presses_to_reach,
)
from voltdmf.signals import DriveMode

N, S, M, H = (
    DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN, DriveMode.HOLD,
)


@pytest.mark.parametrize(
    "current, target, expected",
    [
        (N, N, 0), (N, S, 1), (N, M, 2), (N, H, 3),
        (H, N, 1), (M, N, 2), (S, H, 2), (H, M, 3),
        (M, M, 0),
    ],
)
def test_presses_to_reach(current, target, expected):
    assert presses_to_reach(current, target) == expected


def test_press_counting_tracker_wraps():
    t = PressCountingModeTracker(N)
    t.note_presses(2)
    assert t.get() is M
    t.note_presses(3)  # M -> H -> N -> S ... wait: +3 from M = S
    assert t.get() is S
    t.reset(H)
    assert t.get() is H


class FakePresser:
    def __init__(self):
        self.presses = 0

    def send_mode_button_press(self):
        self.presses += 1


def _controller(presser, mode_seq, **kw):
    seq = list(mode_seq)
    def source():
        return seq.pop(0) if seq else mode_seq[-1]
    return ModeCycleController(presser, source, sleep=lambda _: None, **kw)


def test_switch_sends_computed_presses():
    presser = FakePresser()
    sent_cb = []
    ctl = _controller(presser, [N, H], on_presses_sent=sent_cb.append)
    assert ctl.switch_to(H) == 3
    assert presser.presses == 3
    assert sent_cb == [3]


def test_switch_noop_when_already_there():
    presser = FakePresser()
    ctl = _controller(presser, [M, M])
    assert ctl.switch_to(M) == 0
    assert presser.presses == 0


def test_switch_raises_when_mode_unknown():
    ctl = _controller(FakePresser(), [None])
    with pytest.raises(ModeUnknownError):
        ctl.switch_to(H)


def test_switch_detects_bad_readback():
    presser = FakePresser()
    ctl = _controller(presser, [N, S])  # asked for HOLD, ended on SPORT
    with pytest.raises(ModeSwitchFailed):
        ctl.switch_to(H)

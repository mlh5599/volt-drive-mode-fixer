import pytest

from voltdmf.modecycle import (
    CURSOR_SETTLE_S,
    MAX_WALK_TAPS,
    WALK_GAP_S,
    ModeCycleController,
    ModeSwitchFailed,
    ModeUnknownError,
    PressCountingModeTracker,
    presses_to_reach,
)
from voltdmf.signals import MODE_CYCLE_ORDER, DriveMode

N, S, M, H = (
    DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN, DriveMode.HOLD,
)


@pytest.mark.parametrize(
    "target, expected",
    [(N, 1), (S, 2), (M, 3), (H, 4)],
)
def test_presses_to_reach_is_absolute(target, expected):
    """index(target) + 1, regardless of the current mode (menu reopens on NORMAL)."""
    assert presses_to_reach(target) == expected


def test_walk_tracker_records_absolute_landing():
    t = PressCountingModeTracker(N)
    t.note_walk(2)
    assert t.get() is S
    t.note_walk(4)
    assert t.get() is H
    t.note_walk(1)
    assert t.get() is N
    t.reset(H)
    assert t.get() is H


def test_walk_tracker_note_presses_alias():
    t = PressCountingModeTracker(H)
    t.note_presses(3)  # daemon wires this as on_presses_sent
    assert t.get() is M


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


class WalkingCursor:
    """Fake live menu cursor: opens on NORMAL at the first tap, then steps
    one row per tap, wrapping. Optionally drops or doubles a step."""

    def __init__(self, *, double_at=None, drop_at=None):
        self._tap = 0
        self._idx = 0
        self._double_at = double_at
        self._drop_at = drop_at
        self.reads = 0

    def on_tap(self):
        self._tap += 1
        if self._tap == 1:
            self._idx = 0  # menu opens on NORMAL
        elif self._tap == self._drop_at:
            pass  # a tap that did nothing
        elif self._tap == self._double_at:
            self._idx = (self._idx + 2) % len(MODE_CYCLE_ORDER)
        else:
            self._idx = (self._idx + 1) % len(MODE_CYCLE_ORDER)

    def read(self):
        self.reads += 1
        return MODE_CYCLE_ORDER[self._idx]


def _closed_controller(mode_seq, cursor, **kw):
    presser = FakePresser()
    real_send = presser.send_mode_button_press

    def send():
        real_send()
        cursor.on_tap()

    presser.send_mode_button_press = send
    seq = list(mode_seq)

    def source():
        return seq.pop(0) if seq else mode_seq[-1]

    ctl = ModeCycleController(
        presser, source, menu_cursor_source=cursor.read,
        sleep=lambda _: None, **kw
    )
    return ctl, presser


# -- open-loop (no cursor source) --------------------------------------------

def test_switch_walks_index_plus_one_presses():
    presser = FakePresser()
    sent_cb = []
    ctl = _controller(presser, [N], on_presses_sent=sent_cb.append)
    assert ctl.switch_to(H) == 4
    assert presser.presses == 4
    assert sent_cb == [4]  # canonical presses_to_reach(HOLD)


def test_switch_press_count_ignores_current_mode():
    # Old relative model: SPORT -> NORMAL was 3 presses. Menu model: always 1.
    presser = FakePresser()
    ctl = _controller(presser, [S])
    assert ctl.switch_to(N) == 1
    assert presser.presses == 1


def test_switch_noop_when_already_there():
    presser = FakePresser()
    ctl = _controller(presser, [M])
    assert ctl.switch_to(M) == 0
    assert presser.presses == 0


def test_switch_force_walks_even_when_already_there():
    presser = FakePresser()
    ctl = _controller(presser, [M])
    assert ctl.switch_to(M, force=True) == 3
    assert presser.presses == 3


def test_switch_force_still_refuses_unknown_mode():
    ctl = _controller(FakePresser(), [None])
    with pytest.raises(ModeUnknownError):
        ctl.switch_to(H, force=True)


def test_switch_raises_when_mode_unknown():
    ctl = _controller(FakePresser(), [None])
    with pytest.raises(ModeUnknownError):
        ctl.switch_to(H)


def test_walk_gap_is_between_presses_only():
    gaps = []
    presser = FakePresser()
    seq = [N]
    ctl = ModeCycleController(presser, lambda: seq[-1], sleep=gaps.append)
    ctl.switch_to(H)  # 4 presses -> 3 gaps
    assert gaps == [WALK_GAP_S, WALK_GAP_S, WALK_GAP_S]


def test_walk_gap_default_is_in_the_clean_window():
    assert 1.2 <= WALK_GAP_S <= 2.5


# -- closed loop (cursor source wired) --------------------------------------

@pytest.mark.parametrize("target, taps", [(N, 1), (S, 2), (M, 3), (H, 4)])
def test_closed_loop_stops_when_cursor_hits_target(target, taps):
    # committed mode is deliberately not the target so the walk has work to do
    committed = MODE_CYCLE_ORDER[(MODE_CYCLE_ORDER.index(target) + 2) % 4]
    cur = WalkingCursor()
    ctl, presser = _closed_controller([committed], cur)
    assert ctl.switch_to(target) == taps
    assert presser.presses == taps
    assert cur.read() == target  # left sitting on the target


def test_closed_loop_from_any_committed_mode_still_walks_from_normal():
    # committed HOLD, want MOUNTAIN: menu reopens on NORMAL -> 3 taps.
    cur = WalkingCursor()
    ctl, presser = _closed_controller([H], cur)
    assert ctl.switch_to(M) == 3


def test_closed_loop_recovers_a_dropped_step():
    # tap 3 does nothing; loop just sends more taps until the cursor arrives.
    cur = WalkingCursor(drop_at=3)
    ctl, presser = _closed_controller([N], cur)
    assert ctl.switch_to(M) == 4  # N(open) S _ M
    assert presser.presses == 4


def test_closed_loop_does_not_overshoot_on_a_doubled_step():
    # tap 2 jumps NORMAL->MOUNTAIN; target is MOUNTAIN so we stop right there.
    cur = WalkingCursor(double_at=2)
    ctl, presser = _closed_controller([N], cur)
    assert ctl.switch_to(M) == 2


def test_closed_loop_wraps_to_reach_target_after_overshoot():
    # tap 2 doubles to MOUNTAIN but we want SPORT: keep walking, wrap around.
    cur = WalkingCursor(double_at=2)
    ctl, presser = _closed_controller([N], cur)
    # taps: N(open), M, H, N, S  -> 5
    assert ctl.switch_to(S) == 5


def test_closed_loop_raises_when_cursor_never_arrives():
    class StuckCursor:
        def on_tap(self):
            pass

        def read(self):
            return N

    cur = StuckCursor()
    ctl, presser = _closed_controller([S], cur)
    with pytest.raises(ModeSwitchFailed) as exc:
        ctl.switch_to(H)
    assert presser.presses == MAX_WALK_TAPS
    # The failure has to carry the tap count -- it is the dropped-tap evidence,
    # and SafetyGate reports it. Losing it made every probe MISS read "taps 0".
    assert exc.value.taps == MAX_WALK_TAPS


def test_closed_loop_reports_canonical_count_to_tracker():
    tracker = PressCountingModeTracker(N)
    cur = WalkingCursor(drop_at=3)
    ctl, _ = _closed_controller([N], cur, on_presses_sent=tracker.note_presses)
    ctl.switch_to(M)  # took 4 taps, but tracker should still land on MOUNTAIN
    assert tracker.get() is M


def test_closed_loop_noop_and_force():
    cur = WalkingCursor()
    ctl, presser = _closed_controller([M], cur)
    assert ctl.switch_to(M) == 0
    assert presser.presses == 0
    # force walks a full lap back to MOUNTAIN
    cur2 = WalkingCursor()
    ctl2, presser2 = _closed_controller([M], cur2)
    assert ctl2.switch_to(M, force=True) == 3


def test_closed_loop_calls_tap_observer_once_per_tap_with_target():
    seen = []
    cur = WalkingCursor()
    ctl, presser = _closed_controller(
        [N], cur, tap_observer=lambda tap, target: seen.append((tap, target))
    )
    ctl.switch_to(M)  # N(open), S, M -> 3 taps
    assert seen == [(1, M), (2, M), (3, M)]
    assert len(seen) == presser.presses


def test_tap_observer_not_called_on_the_failure_path_beyond_the_cap():
    class StuckCursor:
        def on_tap(self):
            pass

        def read(self):
            return N

    seen = []
    ctl, _ = _closed_controller(
        [S], StuckCursor(), tap_observer=lambda tap, target: seen.append(tap)
    )
    with pytest.raises(ModeSwitchFailed):
        ctl.switch_to(H)
    assert seen == list(range(1, MAX_WALK_TAPS + 1))  # one per tap, no more


def test_closed_loop_settles_before_reading_cursor():
    sleeps = []
    cur = WalkingCursor()
    presser = FakePresser()
    real_send = presser.send_mode_button_press

    def send():
        real_send()
        cur.on_tap()

    presser.send_mode_button_press = send
    ctl = ModeCycleController(
        presser, lambda: N, menu_cursor_source=cur.read, sleep=sleeps.append
    )
    ctl.switch_to(S)  # 2 taps: settle, gap, settle
    assert sleeps == [CURSOR_SETTLE_S, WALK_GAP_S, CURSOR_SETTLE_S]

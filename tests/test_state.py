"""The range-extender engine read: two signals folded into one tri-state.

The gate this feeds refuses to walk the mode menu, so the interesting cases
here are the ones where the answer is *not yet known* -- ``None`` has to stay
distinct from ``False``, or a daemon that has been up for 20 ms decides the
engine is off and starts tapping.
"""

import pytest

from voltdmf import state as state_module
from voltdmf.signals import EngineState
from voltdmf.state import ENGINE_COUNTER_IDLE_S, VehicleState


@pytest.fixture
def clock(monkeypatch):
    """A hand-cranked ``time.monotonic`` for voltdmf.state."""

    class Clock:
        t = 1000.0

        def advance(self, dt):
            self.t += dt

    c = Clock()
    monkeypatch.setattr(state_module.time, "monotonic", lambda: c.t)
    return c


# -- the 0x3F9 counter ----------------------------------------------------

def test_counter_is_unknown_before_any_frame(clock):
    assert VehicleState().engine_counter_advancing() is None


def test_counter_that_moves_is_advancing(clock):
    st = VehicleState()
    st.note_engine_run_counter(0x0002)
    clock.advance(0.25)
    st.note_engine_run_counter(0x000A)
    assert st.engine_counter_advancing() is True
    assert st.engine_run_counter == 0x000A


def test_a_still_counter_is_unknown_until_the_idle_window_passes(clock):
    """The whole point of the tri-state: "seen once, not moved yet" and
    "watched for seconds and definitely frozen" must not read the same."""
    st = VehicleState()
    st.note_engine_run_counter(0x0100)
    assert st.engine_counter_advancing() is None
    clock.advance(ENGINE_COUNTER_IDLE_S - 0.1)
    st.note_engine_run_counter(0x0100)  # same value -> not a move
    assert st.engine_counter_advancing() is None
    clock.advance(0.2)
    assert st.engine_counter_advancing() is False


def test_a_counter_that_stops_moving_goes_false(clock):
    st = VehicleState()
    st.note_engine_run_counter(1)
    clock.advance(0.25)
    st.note_engine_run_counter(2)
    clock.advance(ENGINE_COUNTER_IDLE_S + 0.1)
    assert st.engine_counter_advancing() is False


def test_the_first_frame_is_never_counted_as_a_move(clock):
    """``engine_run_counter`` starts at None; folding in the first reading is
    a change of the field but not a change of the counter."""
    st = VehicleState()
    st.note_engine_run_counter(9999)
    assert st.engine_counter_moved_monotonic is None


# -- engine_running: 0x4C5 and 0x3F9 together -----------------------------

def test_engine_running_is_none_with_no_evidence(clock):
    assert VehicleState().engine_running is None


def test_4c5_running_wins_immediately(clock):
    """No settling wait on the direct read -- one 0x4C5 frame is enough."""
    st = VehicleState()
    st.engine_state = EngineState.RUNNING
    assert st.engine_running is True


def test_the_4c5_ramp_counts_as_running(clock):
    """The ~1.5 s ramp at either edge of a leg: mid-start is already too
    late to be walking the menu, and mid-shutdown the menu is not back yet."""
    st = VehicleState()
    st.engine_state = EngineState.TRANSITION
    assert st.engine_running is True


def test_4c5_off_reads_false(clock):
    st = VehicleState()
    st.engine_state = EngineState.OFF
    st.note_engine_run_counter(5)
    clock.advance(ENGINE_COUNTER_IDLE_S + 0.1)
    assert st.engine_running is False


def test_a_moving_counter_overrides_a_stale_off(clock):
    """The two never disagreed in any capture; if they ever do, the reading
    that keeps us off the bus wins."""
    st = VehicleState()
    st.engine_state = EngineState.OFF
    st.note_engine_run_counter(1)
    clock.advance(0.25)
    st.note_engine_run_counter(2)
    assert st.engine_running is True


def test_4c5_off_with_an_unsettled_counter_still_reads_false(clock):
    """0x4C5 OFF plus "don't know yet" is not enough to claim the engine is
    running -- it resolves to off, which is the pre-engine-signal behaviour."""
    st = VehicleState()
    st.engine_state = EngineState.OFF
    st.note_engine_run_counter(1)
    assert st.engine_counter_advancing() is None
    assert st.engine_running is False


def test_counter_alone_answers_when_4c5_is_absent(clock):
    """A car that does not put 0x4C5 on the wire still gets an answer."""
    st = VehicleState()
    assert st.engine_state is EngineState.UNKNOWN
    st.note_engine_run_counter(1)
    clock.advance(0.25)
    st.note_engine_run_counter(2)
    assert st.engine_running is True
    clock.advance(ENGINE_COUNTER_IDLE_S + 0.1)
    assert st.engine_running is False


def test_the_counter_carries_the_lead_in_before_4c5_wakes_up(clock):
    """0x3F9 starts moving 33-42 s before 0x4C5 leaves 'off' at the head of
    an engine leg. That whole window has to read as running -- it is exactly
    when a restart on a spent pack would otherwise get walked."""
    st = VehicleState()
    st.engine_state = EngineState.OFF
    for _ in range(4):
        st.note_engine_run_counter(st.engine_run_counter or 0)
        clock.advance(0.25)
    st.note_engine_run_counter(7)
    assert st.engine_running is True


def test_a_mid_leg_fuel_cut_does_not_read_as_engine_off(clock):
    """The counter freezes for 13-43 s on overrun and at rest. 0x4C5 rides
    through those, and its 'running' is what the union reports."""
    st = VehicleState()
    st.engine_state = EngineState.RUNNING
    st.note_engine_run_counter(0x4DF8)
    clock.advance(40.0)                       # a 70 -> 0 -> 35 mph decel
    assert st.engine_counter_advancing() is False
    assert st.engine_running is True

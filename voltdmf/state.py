"""Shared, mutable view of the vehicle, updated from the CAN RX loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .signals import DriveMode, EngineState, ShiftPosition

#: If no known signal frame has been seen for this long, treat the bus as
#: quiet -> car is off (Global A buses go silent with the ignition off).
BUS_QUIET_TIMEOUT_S = 2.0

#: If no 0x3ED frame has been seen for this long, ignition is off. Confirmed
#: 2026-09-19 (Session 14, docs/field-session-log.md): unlike ``bus_active``,
#: 0x3ED itself goes silent through the retained-power ("RAP") window
#: (Session 13) where the rest of the bus -- including 0x1E1/0x1F4/0x1F5/
#: 0x3E9/0x4C5/0x3F9 -- keeps transmitting, so this is what actually
#: distinguishes RAP from true ignition-on. 0x3ED's own transmit rate has not
#: been measured yet (the Pi dropped off the network mid-session before that
#: check could run); this borrows ``BUS_QUIET_TIMEOUT_S`` as a conservative
#: placeholder pending that measurement.
IGNITION_QUIET_TIMEOUT_S = BUS_QUIET_TIMEOUT_S

#: How long the 0x3F9 engine run counter may sit still before we stop calling
#: it a sign of life. It steps on essentially every one of that frame's ~4 Hz
#: slots while the engine is burning fuel, so 3 s is ~12 missed chances --
#: comfortably past RX jitter. It is deliberately NOT sized to ride out the
#: 13-43 s no-fuel stretches seen mid-leg: 0x4C5 covers those, and on a car
#: that has no 0x4C5 a stretched timeout would just move the flapping around.
#: Also the settling time before "seen but never moved" may mean "stopped".
ENGINE_COUNTER_IDLE_S = 3.0


@dataclass
class VehicleState:
    speed_mph: float | None = None
    #: Exact pack charge from the ``22 005B`` diagnostic poll (X * 100 / 255).
    soc_percent: float | None = None
    #: Raw charge byte from that poll (0..255).
    soc_raw: int | None = None
    #: ``time.monotonic()`` of the last poll reply -- for staleness checks.
    soc_percent_monotonic: float | None = None
    #: Coarse passive proxy: 0x096 byte 3 in the ``x F0 0A xx`` mux (~13 %
    #: SOC per count). Failsafe for the reconciler when the poll goes stale.
    soc_bar_raw: int | None = None
    #: Which source last set ``soc_percent`` ("poll") -- ``None`` until a
    #: reply lands.
    soc_source: str | None = None
    #: The 0x7E8..0x7EF id the poll locked onto (``req id + 8``).
    uds_resp_id: int | None = None
    uds_replies: int = 0
    uds_nrcs: int = 0
    shift: ShiftPosition = ShiftPosition.UNKNOWN
    #: Range-extender engine state from 0x4C5 byte 2. The direct read; stays
    #: UNKNOWN until the first decodable frame.
    engine_state: EngineState = EngineState.UNKNOWN
    #: 0x3F9 bytes 1-2 -- the opaque engine run counter, newest frame.
    engine_run_counter: int | None = None
    #: ``time.monotonic()`` when that counter was first seen, and when it last
    #: changed. The pair is what makes "frozen" distinguishable from "not
    #: watched long enough yet".
    engine_counter_seen_monotonic: float | None = None
    engine_counter_moved_monotonic: float | None = None
    #: Current drive mode -- stays ``None`` until the first 0x1F4 frame.
    drive_mode: DriveMode | None = None
    #: Live drive-mode menu cursor, decoded from 0x1F4 bytes 4 AND 5 together
    #: (``signals.decode_menu_cursor``), newest frame. ``None`` means the menu
    #: is CLOSED -- a real reading, not "unknown", so it is assigned
    #: unconditionally: latching the last cursor instead would let a stale
    #: value match a walk target and stop the walk early.
    menu_cursor: DriveMode | None = None
    #: Raw 0x1F4 byte 4, newest frame, whether or not it is in the decode map
    #: -- walk-test diagnostics so an unmapped cursor code is still visible in
    #: the per-tap trace. Does not drive the closed loop. Note byte 4 alone
    #: cannot distinguish NORMAL from a closed menu (both 0x00); pair it with
    #: ``menu_open_hint``.
    menu_cursor_raw: int | None = None
    #: Whether the menu is open at all, i.e. ``menu_cursor is not None``.
    #: Diagnostics only. (This was once read off byte-5 bit 7 alone; that bit
    #: is not an open flag, it is the NORMAL cursor code -- see
    #: ``signals.CURSOR_NORMAL_BIT``.)
    menu_open_hint: bool = False
    last_signal_monotonic: float | None = field(default=None)
    #: ``time.monotonic()`` of the last 0x3ED frame -- the confirmed ignition
    #: signal (see :data:`IGNITION_QUIET_TIMEOUT_S`).
    last_ignition_signal_monotonic: float | None = field(default=None)

    def mark_signal_seen(self) -> None:
        self.last_signal_monotonic = time.monotonic()

    def mark_ignition_seen(self) -> None:
        self.last_ignition_signal_monotonic = time.monotonic()

    @property
    def bus_active(self) -> bool:
        if self.last_signal_monotonic is None:
            return False
        return (time.monotonic() - self.last_signal_monotonic) < BUS_QUIET_TIMEOUT_S

    @property
    def ignition_on(self) -> bool:
        """True if 0x3ED has arrived within :data:`IGNITION_QUIET_TIMEOUT_S`.

        Fails CLOSED on ignorance, like ``bus_active`` and unlike
        ``engine_running``'s union: no frame yet, or the frame has gone
        stale, reads as ignition off. That is the safe default for a signal
        whose only job is gating whether the reconciler is allowed to put
        taps on the wire -- see ``SafetyGate._precondition_failure``.
        """
        if self.last_ignition_signal_monotonic is None:
            return False
        return ((time.monotonic() - self.last_ignition_signal_monotonic)
                < IGNITION_QUIET_TIMEOUT_S)

    def soc_percent_age(self) -> float | None:
        """Seconds since the last poll reply, or ``None`` if none yet."""
        if self.soc_percent_monotonic is None:
            return None
        return time.monotonic() - self.soc_percent_monotonic

    def soc_percent_fresh(self, max_age_s: float) -> bool:
        """True if a poll reply landed within ``max_age_s`` seconds."""
        age = self.soc_percent_age()
        return age is not None and age <= max_age_s

    # -- range-extender engine -------------------------------------------
    def note_engine_run_counter(self, value: int) -> None:
        """Fold a fresh 0x3F9 counter reading in, tracking when it moves."""
        now = time.monotonic()
        if self.engine_counter_seen_monotonic is None:
            self.engine_counter_seen_monotonic = now
        elif value != self.engine_run_counter:
            self.engine_counter_moved_monotonic = now
        self.engine_run_counter = value

    def engine_counter_advancing(self) -> bool | None:
        """Is the 0x3F9 run counter moving? ``None`` == not yet knowable.

        ``None`` is returned both before the first frame and during the first
        :data:`ENGINE_COUNTER_IDLE_S` of watching a still counter -- at that
        point "frozen" and "we only just started looking" are the same
        picture, and reporting the wrong one of those is how a caller ends up
        walking a menu that has nothing on it.
        """
        if self.engine_run_counter is None:
            return None
        now = time.monotonic()
        if self.engine_counter_moved_monotonic is not None:
            return (now - self.engine_counter_moved_monotonic) <= ENGINE_COUNTER_IDLE_S
        assert self.engine_counter_seen_monotonic is not None
        if now - self.engine_counter_seen_monotonic >= ENGINE_COUNTER_IDLE_S:
            return False
        return None

    @property
    def engine_running(self) -> bool | None:
        """Is the car on an engine leg? ``None`` if neither signal has said.

        The union of the two signals, because each is blind where the other
        sees (docs/analysis/session12-engine-signal.md):

        * 0x3F9 tracks fuelling, so it starts moving 33-42 s before 0x4C5
          leaves ``off`` at the head of a leg -- but it also freezes for
          13-43 s at a time on overrun and at rest mid-leg;
        * 0x4C5 rides through those freezes on ``running``, and is the only
          one of the two that does.

        So a moving counter overrides an ``off`` 0x4C5, and a ``running``
        0x4C5 stands on its own. Both errors of this union are in the safe
        direction: it says "running" slightly early and slightly late, and
        the cost of that is only that a mode walk waits.
        """
        advancing = self.engine_counter_advancing()
        if self.engine_state is EngineState.UNKNOWN:
            return advancing
        if self.engine_state is EngineState.OFF:
            return True if advancing else False
        return True  # RUNNING, or the ramp at either edge

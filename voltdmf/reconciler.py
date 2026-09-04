"""The level-triggered reconciler -- pure decision logic, no I/O.

One object holds the two pieces of runtime policy state:

* **selector position** -- a four-position rotary the driver advances with a
  SW1 tap (``voltdmf-ctl setpoint next``). Not persisted; every key cycle
  starts at ``default_position``:

  1. ``hold-soc``  -- the shipped boot default, and the product: *hold the
     pack at ``hold_threshold_percent``*. Passive above it -- the car drives
     on battery exactly as it normally would -- then the SOC-HOLD floor
     engages HOLD and **keeps** it for the rest of the key cycle.
  2. ``hold-now``  -- enforce HOLD immediately, from loop 1, whatever the pack
     reads. "Bank what I have left, starting now."
  3. ``mountain``  -- enforce MOUNTAIN continuously (the floor still wins if
     the pack somehow falls to the threshold anyway).
  4. ``off``       -- do nothing at all. No enforcement, and the SOC-HOLD
     floor is disabled and its latch cleared: the car behaves exactly as if
     the device were not plugged in. This is the only position in which the
     floor does not protect the pack.

* **floor latch** -- the SOC-HOLD floor. Engages when the pack drops to
  ``hold_threshold_percent`` and then stays latched for the rest of the key
  cycle. There is no release percent: "drain to 30 %, then hold, and do not
  let it change". A restart clears it (nothing is persisted), and so does
  tapping round to ``off`` -- the deliberate escape hatch.

:meth:`Reconciler.desired_mode` runs every loop pass and returns the mode the
car *should* be in right now: ``None`` in ``off``; otherwise HOLD whenever the
floor is latched (the floor always wins), then whatever the position asks for,
else ``None`` -- "no target, leave the car wherever the driver has it". The
daemon compares a concrete result to the live ``0x1F4`` mode and, when armed,
asks :class:`voltdmf.safety.SafetyGate` to walk the menu there; ``None`` is a
no-op.

Session 9 calibration: the 3->2 gauge-bar drop sits at ~33.7 % diag SOC and
HOLD is charge-sustaining there. The floor targets "don't fall below 2 bars",
so the shipped engage point is 30 % -- mid-2-bar, and the driver-facing name
of position 1. ``0x096`` byte 3 is a coarse failsafe: if the poll never
answers, b3 dropping to ``bar_failsafe_raw`` (~2 bars) forces HOLD. Because
the latch is now permanent for the key cycle, the failsafe holds fire for
``poll_stale_s`` after start -- long enough for the poll to get a word in --
rather than latching off the coarse proxy before the exact reading has had a
chance to arrive.

Note for anyone reading older code or logs: the selector used to be a two-way
HOLD<->MOUNTAIN toggle with an ``auto`` default, and then a three-position
cycle whose first detent was called plain ``hold``. In both of those, ``auto``
/ ``hold`` named what is now ``hold-soc``, so both legacy names map to it.
``voltdmf-ctl set-mode hold`` still does a one-shot manual switch.
"""

from __future__ import annotations

import time
from enum import Enum

from typing import TYPE_CHECKING

from .signals import DriveMode
from .state import VehicleState

if TYPE_CHECKING:                 # config imports Position from here, so the
    from .config import Config    # runtime import would be circular


class Position(str, Enum):
    """One detent of the SW1 selector. ``str`` so it serialises as its value."""

    #: Passive until the pack reaches the threshold; then HOLD for the drive.
    HOLD_SOC = "hold-soc"
    #: Enforce HOLD right now, whatever the pack reads.
    HOLD_NOW = "hold-now"
    #: Enforce MOUNTAIN continuously (the floor still wins).
    MOUNTAIN = "mountain"
    #: Do nothing; floor disabled. As if the device were not plugged in.
    OFF = "off"


#: SW1 tap order. A fifth tap returns to the first position.
CYCLE: tuple[Position, ...] = (Position.HOLD_SOC, Position.HOLD_NOW,
                               Position.MOUNTAIN, Position.OFF)

#: Older names for what is now ``hold-soc``: ``auto`` from the two-way toggle,
#: ``hold`` from the three-position cycle. Both meant "passive, floor live", so
#: both resolve here -- a config.yaml or a habit from either era keeps working.
LEGACY_POSITION_NAMES: dict[str, Position] = {
    "auto": Position.HOLD_SOC,
    "hold": Position.HOLD_SOC,
}

#: A poll reply older than this is "stale". Also the grace the b3 failsafe
#: gives the poll at startup before it will latch off the coarse proxy.
DEFAULT_POLL_STALE_S = 45.0

#: What each position asks for above the floor. ``None`` == "leave the car
#: alone"; ``OFF`` is absent because it short-circuits before this is read.
_POSITION_TARGET: dict[Position, DriveMode | None] = {
    Position.HOLD_SOC: None,
    Position.HOLD_NOW: DriveMode.HOLD,
    Position.MOUNTAIN: DriveMode.MOUNTAIN,
}


def resolve_position(name: str) -> Position:
    """``Position`` for a wire/config name, accepting the legacy spellings.

    Raises :class:`ValueError` like ``Position(...)`` does, so callers can
    treat the two the same.
    """
    text = str(name).strip().lower()
    if text in LEGACY_POSITION_NAMES:
        return LEGACY_POSITION_NAMES[text]
    return Position(text)


class Reconciler:
    def __init__(
        self,
        *,
        hold_threshold_percent: float,
        bar_failsafe_raw: int,
        default_position: Position = Position.HOLD_SOC,
        poll_stale_s: float = DEFAULT_POLL_STALE_S,
        monotonic=time.monotonic,
    ) -> None:
        if not 0 <= hold_threshold_percent <= 100:
            raise ValueError("hold_threshold_percent must be within 0..100")
        if default_position not in CYCLE:
            raise ValueError(
                f"default_position must be one of {[p.value for p in CYCLE]}, "
                f"not {getattr(default_position, 'value', default_position)!r}")
        self._hold_threshold = hold_threshold_percent
        self._bar_failsafe = bar_failsafe_raw
        self._poll_stale_s = poll_stale_s
        self._monotonic = monotonic
        self._started = monotonic()
        self._position: Position = default_position
        self._floor_latched = False
        self._floor_source: str | None = None  # "poll" | "bar" while latched

    # -- selector position ---------------------------------------------------
    @property
    def position(self) -> Position:
        """The selector detent the driver has SW1 on."""
        return self._position

    @property
    def setpoint_label(self) -> str:
        """The position's wire name -- safe for logs and JSON."""
        return self._position.value

    @property
    def position_index(self) -> int:
        """1-based detent number, for "2/4" style displays."""
        return CYCLE.index(self._position) + 1

    def describe_position(self) -> str:
        """One human-readable line for the driver: what this detent does."""
        if self._position is Position.HOLD_SOC:
            return f"hold the pack at {self._hold_threshold:g}%"
        if self._position is Position.HOLD_NOW:
            return "hold the pack now"
        if self._position is Position.MOUNTAIN:
            return "enforce mountain mode"
        return "not acting -- car is on its own"

    @property
    def floor_latched(self) -> bool:
        return self._floor_latched

    def set_position(self, position: Position) -> None:
        """Jump the selector straight to ``position``."""
        if position not in CYCLE:
            raise ValueError(
                f"position must be one of {[p.value for p in CYCLE]}, "
                f"not {getattr(position, 'value', position)!r}"
            )
        self._position = position
        if position is Position.OFF:
            # Leave nothing latched behind: OFF means the device is not
            # acting, and a latch surviving here would re-assert HOLD the
            # instant the driver taps back to a live position, on a reading
            # that may be minutes stale. It is also the only way to clear a
            # key-cycle latch without restarting -- the deliberate escape.
            self._floor_latched = False
            self._floor_source = None

    def advance(self) -> Position:
        """One SW1 tap: step to the next detent, wrapping. Returns the new one.

        The daemon owns this cycle, not the button helper. A helper keeping its
        own index would drift out of step with the daemon on any restart or
        any ``voltdmf-ctl setpoint`` from the shell, and the driver would then
        need two taps to get one move.
        """
        self.set_position(CYCLE[(CYCLE.index(self._position) + 1) % len(CYCLE)])
        return self._position

    # -- the decision ------------------------------------------------------
    def desired_mode(self, state: VehicleState) -> DriveMode | None:
        """The mode the car should be in now.

        ``OFF`` short-circuits to ``None`` *before* the floor is evaluated --
        that is what makes the position mean "as if the device were not
        plugged in". Otherwise the floor wins when latched, then the position's
        own target, else ``None`` ("leave the car alone"). The daemon acts on
        a concrete result when armed and ignores ``None``.
        """
        if self._position is Position.OFF:
            return None
        self._update_floor(state)
        if self._floor_latched:
            return DriveMode.HOLD
        return _POSITION_TARGET[self._position]

    def _update_floor(self, state: VehicleState) -> None:
        # Latched is latched: the floor holds for the rest of the key cycle.
        # There is no release percent -- "drain to the threshold, then hold,
        # and do not let it change". A restart or a tap round to OFF is what
        # clears it.
        if self._floor_latched:
            return

        # Preferred input: a fresh exact reading from the 22 005B poll.
        if (state.soc_percent is not None
                and state.soc_percent_fresh(self._poll_stale_s)):
            if state.soc_percent <= self._hold_threshold:
                self._floor_latched = True
                self._floor_source = "poll"
            return

        # No fresh poll. The coarse b3 proxy can latch instead -- but only
        # after the poll has had poll_stale_s to answer. The latch is now
        # permanent for the key cycle, so committing the whole drive to HOLD
        # off a ~13 %-per-count proxy in the first seconds after boot, before
        # the exact reading has had a chance to arrive, would be too cheap.
        if self._monotonic() - self._started < self._poll_stale_s:
            return
        if state.soc_bar_raw is not None and state.soc_bar_raw <= self._bar_failsafe:
            self._floor_latched = True
            self._floor_source = "bar"

    # -- introspection --------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "setpoint": self.setpoint_label,      # kept: the wire field name
            "position": self._position.value,
            "position_index": self.position_index,
            "position_description": self.describe_position(),
            "cycle": [p.value for p in CYCLE],
            "floor_latched": self._floor_latched,
            "floor_source": self._floor_source,
            "hold_threshold_percent": self._hold_threshold,
            "bar_failsafe_raw": self._bar_failsafe,
        }


def build_reconciler(
    config: "Config", *, poll_stale_s: float = DEFAULT_POLL_STALE_S
) -> Reconciler:
    """Instantiate the reconciler from a parsed :class:`Config`."""
    p = config.policy
    return Reconciler(
        hold_threshold_percent=p.hold_threshold_percent,
        bar_failsafe_raw=p.bar_failsafe_raw,
        default_position=p.default_position,
        poll_stale_s=poll_stale_s,
    )

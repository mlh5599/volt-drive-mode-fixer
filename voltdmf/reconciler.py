"""The level-triggered reconciler -- pure decision logic, no I/O.

One object holds the two pieces of runtime policy state:

* **selector position** -- a three-position rotary the driver advances with a
  SW1 tap (``voltdmf-ctl setpoint next``). Not persisted; every boot starts at
  ``default_position``:

  1. ``hold``     -- the shipped boot default. Passive on a healthy pack: the
     car drives on battery exactly as it normally would, and the SOC-HOLD
     floor engages HOLD only once the pack falls to
     ``hold_threshold_percent``. This is "hold the battery at 30 %".
  2. ``mountain`` -- enforce MOUNTAIN continuously, from loop 1.
  3. ``off``      -- do nothing at all. No enforcement, and the SOC-HOLD floor
     is disabled and its latch cleared: the car behaves exactly as if the
     device were not plugged in. This is the only position in which the floor
     does not protect the pack.

* **floor latch** -- the SOC-HOLD floor. Engages when the pack drops to
  ``hold_threshold_percent`` and stays latched (in memory only) until a fresh
  poll reads back above ``hold_reset_percent``. Active in ``hold`` and
  ``mountain``, disabled in ``off``.

:meth:`Reconciler.desired_mode` runs every loop pass and returns the mode the
car *should* be in right now: ``None`` in ``off``; otherwise HOLD whenever the
floor is latched (the floor always wins), else MOUNTAIN in the ``mountain``
position, else ``None`` -- "no target, leave the car wherever the driver has
it". The daemon compares a concrete result to the live ``0x1F4`` mode and,
when armed, asks :class:`voltdmf.safety.SafetyGate` to walk the menu there;
``None`` is a no-op.

Session 9 calibration: the 3->2 gauge-bar drop sits at ~33.7 % diag SOC and
HOLD is charge-sustaining there. The floor targets "don't fall below 2 bars":
the shipped engage point is 30 % (mid-2-bar, the driver-facing "hold at 30 %")
and it releases at 41 %, the 3-bar mark. ``0x096`` byte 3 is a coarse
failsafe: if the poll stops answering and b3 drops to ``bar_failsafe_raw``
(~2 bars), force HOLD until a real poll reading returns.

Note for anyone reading older code or logs: the selector used to be a
two-way HOLD<->MOUNTAIN toggle plus an ``auto`` default, where ``setpoint
hold`` meant "enforce HOLD continuously". That position is gone -- position 1
is now named ``hold`` but means the *floor*, which is what today's ``auto``
did. ``voltdmf-ctl set-mode hold`` still does a one-shot manual switch.
"""

from __future__ import annotations

from enum import Enum

from typing import TYPE_CHECKING

from .signals import DriveMode
from .state import VehicleState

if TYPE_CHECKING:                 # config imports Position from here, so the
    from .config import Config    # runtime import would be circular

class Position(str, Enum):
    """One detent of the SW1 selector. ``str`` so it serialises as its value."""

    #: Passive on a healthy pack; the SOC-HOLD floor is the only actor.
    HOLD = "hold"
    #: Enforce MOUNTAIN continuously (the floor still wins if it engages).
    MOUNTAIN = "mountain"
    #: Do nothing; floor disabled. As if the device were not plugged in.
    OFF = "off"


#: SW1 tap order. A fourth tap returns to the first position.
CYCLE: tuple[Position, ...] = (Position.HOLD, Position.MOUNTAIN, Position.OFF)

#: Back-compat: ``auto`` was the old name for what is now ``hold`` -- passive
#: with the floor live. Accepted on the wire and in config so an older
#: config.yaml or a muscle-memory command keeps working.
AUTO = "auto"

#: A poll reply older than this is "stale" -- fall back to the b3 failsafe.
DEFAULT_POLL_STALE_S = 45.0

#: Bars of b3 headroom above the failsafe count before the failsafe releases
#: (it only ever engages off b3; a real poll reading is what clears it).
_BAR_RELEASE_MARGIN = 2


class Reconciler:
    def __init__(
        self,
        *,
        hold_threshold_percent: float,
        hold_reset_percent: float,
        bar_failsafe_raw: int,
        default_position: Position = Position.HOLD,
        poll_stale_s: float = DEFAULT_POLL_STALE_S,
    ) -> None:
        if hold_reset_percent <= hold_threshold_percent:
            raise ValueError("hold_reset_percent must be > hold_threshold_percent")
        if default_position not in CYCLE:
            raise ValueError(
                f"default_position must be one of {[p.value for p in CYCLE]}, "
                f"not {getattr(default_position, 'value', default_position)!r}")
        self._hold_threshold = hold_threshold_percent
        self._hold_reset = hold_reset_percent
        self._bar_failsafe = bar_failsafe_raw
        self._poll_stale_s = poll_stale_s
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
        """``"hold"`` / ``"mountain"`` / ``"off"`` -- safe for logs and JSON."""
        return self._position.value

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
            # that may be minutes stale.
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
        plugged in". Otherwise the floor wins when latched, then MOUNTAIN in
        the mountain position, else ``None`` ("leave the car alone"). The
        daemon acts on a concrete result when armed and ignores ``None``.
        """
        if self._position is Position.OFF:
            return None
        self._update_floor(state)
        if self._floor_latched:
            return DriveMode.HOLD
        if self._position is Position.MOUNTAIN:
            return DriveMode.MOUNTAIN
        return None

    def _update_floor(self, state: VehicleState) -> None:
        # Preferred input: a fresh exact reading from the 22 005B poll.
        if (state.soc_percent is not None
                and state.soc_percent_fresh(self._poll_stale_s)):
            soc = state.soc_percent
            if self._floor_latched:
                if soc >= self._hold_reset:
                    self._floor_latched = False
                    self._floor_source = None
            elif soc <= self._hold_threshold:
                self._floor_latched = True
                self._floor_source = "poll"
            return

        # Poll stale / never answered: the coarse b3 proxy can only *engage*
        # the floor, never release it -- releasing takes a real poll reading
        # back above the reset percent. If the poll never returns the car
        # simply stays in HOLD, which is the safe (charge-sustaining) failure.
        if (not self._floor_latched and state.soc_bar_raw is not None
                and state.soc_bar_raw <= self._bar_failsafe):
            self._floor_latched = True
            self._floor_source = "bar"

    # -- introspection --------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "setpoint": self.setpoint_label,      # kept: the wire field name
            "position": self._position.value,
            "cycle": [p.value for p in CYCLE],
            "floor_latched": self._floor_latched,
            "floor_source": self._floor_source,
            "hold_threshold_percent": self._hold_threshold,
            "hold_reset_percent": self._hold_reset,
            "bar_failsafe_raw": self._bar_failsafe,
        }


def build_reconciler(
    config: "Config", *, poll_stale_s: float = DEFAULT_POLL_STALE_S
) -> Reconciler:
    """Instantiate the reconciler from a parsed :class:`Config`."""
    p = config.policy
    return Reconciler(
        hold_threshold_percent=p.hold_threshold_percent,
        hold_reset_percent=p.hold_reset_percent,
        bar_failsafe_raw=p.bar_failsafe_raw,
        default_position=p.default_position,
        poll_stale_s=poll_stale_s,
    )

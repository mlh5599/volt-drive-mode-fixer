"""The level-triggered reconciler -- pure decision logic, no I/O.

One object holds the two pieces of runtime policy state:

* **setpoint** -- a HOLD <-> MOUNTAIN toggle the driver moves with the panel
  button (``voltdmf-ctl setpoint``). Not persisted; every boot starts at
  ``default_setpoint``.
* **floor latch** -- the SOC-HOLD floor. Engages when the pack drops to
  ``hold_threshold_percent`` and stays latched (in memory only) until a fresh
  poll reads back above ``hold_reset_percent``.

:meth:`Reconciler.desired_mode` runs every loop pass and returns the mode the
car *should* be in right now: the setpoint normally, or HOLD whenever the
floor is latched (the floor always wins). The daemon compares that to the
live ``0x1F4`` mode and, when armed, asks :class:`voltdmf.safety.SafetyGate`
to walk the menu there.

Session 9 calibration: the 3->2 gauge-bar drop sits at ~33.7 % diag SOC and
HOLD is charge-sustaining there, so the floor targets "don't fall below
2 bars" -- engage 33 %, release 41 % (the 3-bar mark). ``0x096`` byte 3 is a
coarse failsafe: if the poll stops answering and b3 drops to
``bar_failsafe_raw`` (~2 bars), force HOLD until a real poll reading returns.
"""

from __future__ import annotations

from .config import Config
from .signals import DriveMode
from .state import VehicleState

#: The only two modes the setpoint toggle can take.
SETPOINTS: tuple[DriveMode, ...] = (DriveMode.HOLD, DriveMode.MOUNTAIN)

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
        default_setpoint: DriveMode = DriveMode.HOLD,
        poll_stale_s: float = DEFAULT_POLL_STALE_S,
    ) -> None:
        if hold_reset_percent <= hold_threshold_percent:
            raise ValueError("hold_reset_percent must be > hold_threshold_percent")
        if default_setpoint not in SETPOINTS:
            raise ValueError("default_setpoint must be hold or mountain")
        self._hold_threshold = hold_threshold_percent
        self._hold_reset = hold_reset_percent
        self._bar_failsafe = bar_failsafe_raw
        self._poll_stale_s = poll_stale_s
        self._setpoint = default_setpoint
        self._floor_latched = False
        self._floor_source: str | None = None  # "poll" | "bar" while latched

    # -- setpoint -------------------------------------------------------------
    @property
    def setpoint(self) -> DriveMode:
        return self._setpoint

    @property
    def floor_latched(self) -> bool:
        return self._floor_latched

    def set_setpoint(self, mode: DriveMode) -> None:
        """Move the HOLD <-> MOUNTAIN toggle. Rejects any other mode."""
        if mode not in SETPOINTS:
            raise ValueError(
                f"setpoint must be one of {[m.value for m in SETPOINTS]}, "
                f"not {getattr(mode, 'value', mode)!r}"
            )
        self._setpoint = mode

    # -- the decision ------------------------------------------------------
    def desired_mode(self, state: VehicleState) -> DriveMode:
        """The mode the car should be in now: HOLD if the floor is latched,
        otherwise the current setpoint. Always concrete -- the daemon decides
        whether to act on it (armed) or just log it (disarmed)."""
        self._update_floor(state)
        return DriveMode.HOLD if self._floor_latched else self._setpoint

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
            "setpoint": self._setpoint.value,
            "floor_latched": self._floor_latched,
            "floor_source": self._floor_source,
            "hold_threshold_percent": self._hold_threshold,
            "hold_reset_percent": self._hold_reset,
            "bar_failsafe_raw": self._bar_failsafe,
        }


def build_reconciler(
    config: Config, *, poll_stale_s: float = DEFAULT_POLL_STALE_S
) -> Reconciler:
    """Instantiate the reconciler from a parsed :class:`Config`."""
    p = config.policy
    return Reconciler(
        hold_threshold_percent=p.hold_threshold_percent,
        hold_reset_percent=p.hold_reset_percent,
        bar_failsafe_raw=p.bar_failsafe_raw,
        default_setpoint=p.default_setpoint,
        poll_stale_s=poll_stale_s,
    )

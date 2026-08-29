"""Safety gate around the mode-cycle controller.

Everything that could result in a transmission goes through :meth:`SafetyGate.request`.
Responsibilities (DESIGN.md "Safety model"):

* preconditions -- only inject in a state where switching makes sense
* rate limiting -- one burst per cooldown, never a sustained/looping TX
* fail-passive -- any error stops transmitting and returns cleanly; the
  caller's loop keeps reading the bus but we do not retry mid-burst
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .modecycle import ModeCycleController
from .signals import DriveMode, ShiftPosition
from .state import VehicleState

log = logging.getLogger(__name__)

#: Minimum wall time between two mode-switch bursts (reference project value).
MODE_SWITCH_COOLDOWN_S = 60.0

#: Above this the speed signal is almost certainly garbage -> don't act on it.
MAX_PLAUSIBLE_SPEED_MPH = 100.0

_BLOCKING_SHIFTS = {ShiftPosition.PARK, ShiftPosition.REVERSE, ShiftPosition.NEUTRAL}


class SafetyGate:
    def __init__(
        self,
        controller: ModeCycleController,
        *,
        cooldown_s: float = MODE_SWITCH_COOLDOWN_S,
        allow_unknown_shift: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._cooldown_s = cooldown_s
        # Shift decode is a Phase C stub today (always UNKNOWN); keeping this
        # True lets bench testing proceed. Flip to False once decode_shift works.
        self._allow_unknown_shift = allow_unknown_shift
        self._monotonic = monotonic
        self._last_switch: float | None = None

    def _precondition_failure(self, state: VehicleState) -> str | None:
        if not state.bus_active:
            return "bus is quiet (car off?)"
        if state.shift in _BLOCKING_SHIFTS:
            return f"shift is {state.shift.value}"
        if state.shift is ShiftPosition.UNKNOWN and not self._allow_unknown_shift:
            return "shift position unknown"
        if state.speed_mph is not None and state.speed_mph > MAX_PLAUSIBLE_SPEED_MPH:
            return f"implausible speed {state.speed_mph:.0f} mph"
        return None

    def _in_cooldown(self) -> bool:
        if self._last_switch is None:
            return False
        return (self._monotonic() - self._last_switch) < self._cooldown_s

    def request(self, target: DriveMode, state: VehicleState) -> bool:
        """Attempt to switch to ``target``. Returns True only if presses were sent."""
        reason = self._precondition_failure(state)
        if reason is not None:
            log.info("mode switch to %s blocked: %s", target.value, reason)
            return False
        if self._in_cooldown():
            log.debug("mode switch to %s suppressed: within cooldown", target.value)
            return False

        try:
            sent = self._controller.switch_to(target)
        except Exception:  # fail-passive: never propagate into the RX loop
            log.exception("mode switch to %s failed; staying passive", target.value)
            self._last_switch = self._monotonic()  # apply cooldown even on failure
            return False

        self._last_switch = self._monotonic()
        if sent == 0:
            log.info("already in %s; nothing to do", target.value)
            return False
        log.info("switched toward %s with %d press(es)", target.value, sent)
        return True

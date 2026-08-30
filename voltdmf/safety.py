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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RequestOutcome:
    """What :meth:`SafetyGate.request_verbose` did, for a human-facing reply.

    ``sent`` -- presses actually went on the wire.
    ``blocked`` -- a precondition, the cooldown, or an error stopped it (as
    opposed to a clean no-op because the car already reads ``target``).
    ``reason`` -- always a short human-readable phrase.
    """

    sent: bool
    presses: int
    blocked: bool
    reason: str


class SafetyGate:
    def __init__(
        self,
        controller: ModeCycleController,
        *,
        cooldown_s: float = MODE_SWITCH_COOLDOWN_S,
        allow_unknown_shift: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._cooldown_s = cooldown_s
        # decode_shift is confirmed on-vehicle (0x1F5 byte 3 PRNDL, session 4,
        # 2026-08-29), so UNKNOWN now means a short/garbled shift frame, not a
        # missing decoder -- treat it as blocking. Callers that genuinely have
        # no shift signal (bench rigs without 0x1F5) can still pass True.
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
        return self.cooldown_remaining() > 0.0

    def cooldown_remaining(self) -> float:
        """Seconds left before another burst is allowed (0.0 if none)."""
        if self._last_switch is None:
            return 0.0
        return max(0.0, self._cooldown_s - (self._monotonic() - self._last_switch))

    def request(self, target: DriveMode, state: VehicleState) -> bool:
        """Attempt to switch to ``target``. Returns True only if presses were sent."""
        return self.request_verbose(target, state).sent

    def request_verbose(
        self, target: DriveMode, state: VehicleState, *, force: bool = False
    ) -> RequestOutcome:
        """Like :meth:`request` but returns a :class:`RequestOutcome` with a
        human-readable reason -- used by the control socket's ``set-mode``.

        ``force`` walks the menu even when the mode source already reads
        ``target`` (passed through to :meth:`ModeCycleController.switch_to`).
        Preconditions, the cooldown, and the press cap are *not* bypassable.
        """
        reason = self._precondition_failure(state)
        if reason is not None:
            log.info("mode switch to %s blocked: %s", target.value, reason)
            return RequestOutcome(False, 0, True, f"blocked: {reason}")
        if self._in_cooldown():
            left = self.cooldown_remaining()
            log.debug("mode switch to %s suppressed: within cooldown", target.value)
            return RequestOutcome(
                False, 0, True, f"blocked: within cooldown ({left:.0f}s left)"
            )

        try:
            sent = (
                self._controller.switch_to(target, force=True)
                if force
                else self._controller.switch_to(target)
            )
        except Exception as exc:  # fail-passive: never propagate into the RX loop
            log.exception("mode switch to %s failed; staying passive", target.value)
            self._last_switch = self._monotonic()  # apply cooldown even on failure
            return RequestOutcome(False, 0, True, f"switch failed: {exc}")

        self._last_switch = self._monotonic()
        if sent == 0:
            log.info("already in %s; nothing to do", target.value)
            return RequestOutcome(False, 0, False, f"already in {target.value}")
        log.info("switched toward %s with %d press(es)", target.value, sent)
        return RequestOutcome(
            True, sent, False, f"switched toward {target.value} with {sent} press(es)"
        )

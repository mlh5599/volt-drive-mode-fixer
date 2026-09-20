"""Safety gate around the mode-cycle controller.

Everything that could result in a transmission goes through :meth:`SafetyGate.request`.
Responsibilities (DESIGN.md "Safety model"):

* preconditions -- only inject in a state where switching makes sense
* rate limiting -- one burst per cooldown, never a sustained/looping TX
* giving up -- :class:`AttemptBudget` stops a level-triggered reconciler from
  re-walking a menu that will not take the mode, for the whole drive
* fail-passive -- any error stops transmitting and returns cleanly; the
  caller's loop keeps reading the bus but we do not retry mid-burst
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .modecycle import ModeCycleController
from .reconciler import charge_sustaining_block
from .signals import DriveMode
from .state import VehicleState

log = logging.getLogger(__name__)

#: Minimum wall time between two mode-switch bursts. Started at the reference
#: project's 60 s, back when a walk was unproven and a runaway one was the
#: thing to fear. Session 11 landed 35/35 legs in <=4 taps, so the risk now
#: runs the other way: at 60 s a driver who bumps the stalk could spend most
#: of a minute in the wrong mode before the reconciler pulls it back. 10 s
#: still rules out a sustained/looping TX -- one short burst per 10 s is
#: nothing next to the ~32 Hz the module itself puts on 0x1E1 -- while making
#: "force it" mean seconds.
MODE_SWITCH_COOLDOWN_S = 10.0

#: Above this the speed signal is almost certainly garbage -> don't act on it.
MAX_PLAUSIBLE_SPEED_MPH = 100.0

#: How many walks the reconciler will spend on one target before it gives up
#: on that target for the rest of the key cycle (:class:`AttemptBudget`).
#:
#: Three, because a walk is already a closed loop that tries hard by itself:
#: it taps up to ``MAX_WALK_TAPS`` (12) times, reading the cursor back after
#: each, so it recovers dropped taps and coalesced steps *within* one attempt.
#: A whole walk failing therefore does not mean "a tap went missing" -- it
#: means the menu is not doing what the model says, and a fourth identical
#: walk is not new information. Three costs at most ~1 minute of tapping in
#: the worst case, and in the measured clean case never fires at all.
MAX_TARGET_ATTEMPTS = 3

# Shift position is deliberately NOT a precondition. The thing being pressed
# is the centre-stack energy-mode menu (Normal/Sport/Mountain/Hold), not a
# gear selector: it changes how the car spends the pack, never what the
# driveline does. Pressing it stopped in Park is exactly what a driver does,
# so gating on PRNDL only bought a false sense of caution -- and it cost
# real behaviour, because it meant the reconciler could not settle the mode
# in the driveway before a drive, or while sitting at a charger. Shift is
# still decoded and reported; it just does not block a switch.


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


class AttemptBudget:
    """Give up on a target the car refuses to take.

    The gap this closes, from the 2026-09-04 drive: the reconciler is
    level-triggered, so "mode != target" stays true forever if the walk never
    lands, and the only thing between it and a permanent 10 s tap cycle was
    the cooldown. :func:`voltdmf.reconciler.charge_sustaining_block` handles
    the one failure we understand (a spent pack takes HOLD off the menu);
    this handles every other one, without needing to know what it is.

    One target and one count, because the reconciler only ever pursues one
    mode at a time -- asking for a different mode is new intent and starts
    the count over. An attempt is *a walk that put taps on the wire*, not a
    loop pass: a walk suppressed by the cooldown, by a precondition, or by
    the ICE block costs nothing and is not counted, so a car sitting with a
    quiet bus does not burn the budget.

    Nothing here is persisted and nothing expires on a timer. It clears when
    the car reaches the target (however it got there -- the driver's own
    button counts), when the target changes, and on the explicit-intent
    events the daemon routes through :meth:`reset`: a SW1 tap, a
    ``set-mode``, an ``arm``, a ``reload``, and the bus going quiet, which is
    this project's key-cycle boundary.
    """

    def __init__(self, max_attempts: int = MAX_TARGET_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._max = max_attempts
        self._target: DriveMode | None = None
        self._attempts = 0

    @property
    def target(self) -> DriveMode | None:
        """The mode the current count is about, or ``None`` if idle."""
        return self._target

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def max_attempts(self) -> int:
        return self._max

    def exhausted(self, target: DriveMode) -> bool:
        """Has ``target`` used up the budget?"""
        return target is self._target and self._attempts >= self._max

    def give_up_reason(self, target: DriveMode) -> str | None:
        """Human-readable "why not", or ``None`` to keep trying."""
        if not self.exhausted(target):
            return None
        return (f"{target.value.upper()} unreachable -- gave up after "
                f"{self._attempts} attempts (tap SW1 or run set-mode to retry)")

    def record_attempt(self, target: DriveMode) -> int:
        """Count one walk that went on the wire. Returns the new count."""
        if target is not self._target:
            self._target = target
            self._attempts = 0
        self._attempts += 1
        return self._attempts

    def note_reached(self, target: DriveMode) -> None:
        """The car is now in ``target`` -- whatever we were counting is done."""
        if target is self._target:
            self.reset()

    def reset(self) -> None:
        self._target = None
        self._attempts = 0

    def snapshot(self) -> dict:
        return {
            "target": self._target.value if self._target else None,
            "attempts": self._attempts,
            "max_attempts": self._max,
            "gave_up": (self._target is not None
                        and self.exhausted(self._target)),
        }


class SafetyGate:
    def __init__(
        self,
        controller: ModeCycleController,
        *,
        cooldown_s: float = MODE_SWITCH_COOLDOWN_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._cooldown_s = cooldown_s
        self._monotonic = monotonic
        self._last_switch: float | None = None

    def _precondition_failure(
        self, target: DriveMode, state: VehicleState
    ) -> str | None:
        """Why a switch to ``target`` must not go out now, or ``None``.

        Three checks about the *bus* rather than the driveline -- a quiet bus
        means nobody is listening, ignition-off means the ignition itself is
        confirmed off even if the bus is not quiet, and an implausible speed
        means the frames we are reading are garbage (see the note on shift
        above) -- plus one about the target: a mode the car has taken off the
        menu cannot be walked to, so trying only spends taps.

        That last one lives here rather than only in the reconciler so that a
        hand-typed ``voltdmf-ctl set-mode hold`` gets the same answer, and
        gets it as a sentence instead of as twelve taps and a timeout.

        The ignition check is what actually covers the retained-power (RAP)
        window (docs/field-session-log.md Session 13): the car has a window
        -- ignition off, door not opened -- where the cluster and most of the
        bus (0x1E1/0x1F4/0x1F5/0x3E9/0x4C5/0x3F9) all keep transmitting, so
        ``bus_active`` alone stays True right through it. 0x3ED (Session 14)
        is confirmed to go silent for the whole window, so ``ignition_on`` is
        checked separately rather than folded into ``bus_active``.
        """
        if not state.bus_active:
            return "bus is quiet (car off?)"
        if not state.ignition_on:
            return "ignition is off (RAP window?)"
        if state.speed_mph is not None and state.speed_mph > MAX_PLAUSIBLE_SPEED_MPH:
            return f"implausible speed {state.speed_mph:.0f} mph"
        return charge_sustaining_block(state, target)

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
        reason = self._precondition_failure(target, state)
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
            # A failed walk still put taps on the wire, and that count is the
            # dropped-tap evidence -- report it instead of a misleading 0.
            # ``sent`` stays False: the walk did not achieve the switch.
            return RequestOutcome(
                False, getattr(exc, "taps", 0), True, f"switch failed: {exc}"
            )

        self._last_switch = self._monotonic()
        if sent == 0:
            log.info("already in %s; nothing to do", target.value)
            return RequestOutcome(False, 0, False, f"already in {target.value}")
        log.info("switched toward %s with %d press(es)", target.value, sent)
        return RequestOutcome(
            True, sent, False, f"switched toward {target.value} with {sent} press(es)"
        )

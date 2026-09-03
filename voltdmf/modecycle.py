"""Mode-cycle controller.

The car's drive-mode control is a *menu walk*, not a direct selector
(``docs/signals-confirmed.md`` "Drive-mode MENU model", owner-observed +
on-car injection sweeps 2026-08-29):

* **Cold menu** (no recent press) -- the first press opens the menu on
  NORMAL with no step, whatever mode is latched; further presses step.
  Reaching a mode from cold is ``index(target) + 1`` presses.
* **Warm menu** (a press within ~3 s of the last commit) -- the cursor is
  already on the committed mode and the first press opens *and* steps.
  Presses to target = ``(index(target) - index(current)) mod 4`` (on-car
  2026-08-29: SPORT->MOUNTAIN, MOUNTAIN->HOLD, HOLD->NORMAL each one tap).
* **Either way** -- each press steps once only if presses are >= ~1.2 s
  apart; closer coalesces into extra steps (the old "steps too far" bug,
  ``WALK_GAP_S`` at 0.75 s). The press *count* is context-dependent, so the
  reliable path closes the loop on the byte-4 cursor rather than counting.
* **~3 s with no press** -- the menu times out and the cursor commits;
  ``0x1F4`` byte 1 (the committed mode) updates then.

Two readbacks off ``0x1F4`` matter here:

* bytes 4+5 -- ONE field, the **live menu cursor**, stepping ~40 ms after each
  tap (``signals.decode_menu_cursor``); ``None`` = menu closed. We close the
  loop on this: tap, read the cursor, stop once it is on the target, let it
  commit.
* byte 1 -- the **committed mode** (``signals.decode_drive_mode``), advisory
  only here: it lags the commit ~3 s and, on a *parked* car, reverts toward
  NORMAL a few seconds later. Callers that need a hard confirmation poll it
  over the following seconds (and really want the car moving).

If no cursor source is wired, :meth:`ModeCycleController.switch_to` falls back
to the open-loop ``index(target) + 1`` walk. That is correct from *any* start
mode -- the menu always opens on NORMAL, so the count never depends on where
you were (measured 2026-09-03, ``tools/press_calibrate.py model``). The closed
loop buys early exit and a hard failure when a tap does not register, not
correctness.

This module computes the walk and drives the (injected) transmit function.
It is deliberately ignorant of *which* trigger asked.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from .signals import MODE_CYCLE_ORDER, DriveMode

#: Reaching HOLD is the longest clean walk: menu-open (NORMAL) + 3 cursor
#: steps. The closed loop is allowed one extra lap of the 4-cycle to recover
#: a missed step before it gives up -- see ``MAX_WALK_TAPS``.
MAX_WALK_PRESSES = 4

#: Back-compat alias for the pre-menu-model name.
MAX_PRESSES_PER_BURST = MAX_WALK_PRESSES

#: Hard cap on taps the closed loop will send for one switch. Hitting this
#: raises ``ModeSwitchFailed`` rather than hammering the cluster (sustained
#: sub-2 s injection drives the CAN TEC up and the cluster starts ignoring us).
#:
#: Sized against the measured dropped-tap rate, not just the clean walk. A
#: single injected frame is occasionally not registered, so a 4-tap walk to
#: HOLD needs 4 *successes*, not 4 taps. In the 2026-09-03 12-probe run (at a
#: ~21% drop rate, before the Notifier-race fix) HOLD twice needed 6 and 7
#: taps -- 8 left almost no headroom, ~1.3% of HOLD walks would have failed.
#: 12 puts that under 1e-6 even at that drop rate, and costs nothing on a
#: clean walk because the loop stops the moment the cursor reads the target.
#: At ~1.6 s per tap the worst case is ~19 s.
MAX_WALK_TAPS = 12

#: Silence left between the presses *within* a walk -- the "walk-gap".
#: Bounded on both sides, and the reason the walk used to overshoot:
#:  * lower -- presses closer than ~1.2 s get coalesced into extra cursor
#:    steps (measured 2026-08-29: 0.75 s spacing walked 2 taps = 3 steps).
#:  * upper -- the next press must land inside the ~3 s menu-open window, and
#:    that budget already includes the ~400 ms injected press itself, so keep
#:    it under ~2.5 s.
#: 1.4 s sits in the clean middle. Kept as a local constant so this
#: pure-logic module does not import the CAN transport.
WALK_GAP_S = 1.4

#: Settle time after a tap before reading the live cursor back (byte 4 moves
#: ~40 ms after the tap; give it margin against RX jitter).
CURSOR_SETTLE_S = 0.2

#: Deprecated alias. This value was previously mis-described as a post-switch
#: cooldown; it is the intra-walk gap (see above).
BUTTON_PRESS_COOLDOWN_S = WALK_GAP_S


class ModeUnknownError(RuntimeError):
    """Current mode could not be determined -- refuse to inject blindly."""


class ModeSwitchFailed(RuntimeError):
    """The menu walk did not reach the requested mode.

    Raised by :meth:`ModeCycleController.switch_to` when it is running the
    closed loop (a ``menu_cursor_source`` is wired) and the live cursor never
    lands on the target within ``MAX_WALK_TAPS`` -- the bus is degraded, the
    menu is not opening, or the cluster is ignoring us. It is *not* raised for
    a byte-1 commit mismatch: ``0x1F4`` byte 1 lags the commit ~3 s and
    reverts toward NORMAL on a parked car, so callers that want a hard commit
    check poll it themselves over the following seconds
    (``tools/set_mode.py``, the daemon loop).
    """


class ButtonPresser(Protocol):
    def send_mode_button_press(self) -> None: ...


def presses_to_reach(target: DriveMode) -> int:
    """Presses to walk the drive-mode menu from a *cold* open to ``target``.

    ``index(target) + 1`` -- one press opens the menu on NORMAL, then one per
    cursor step. Range 1..4. Pure function -- the core unit-tested piece.

    Only valid from a cold menu (car resting, no recent press). From a warm
    menu the first press steps from the current mode instead, so this
    over-counts; the closed loop (``_walk_closed_loop``) does not rely on it.
    """
    return MODE_CYCLE_ORDER.index(target) + 1


class PressCountingModeTracker:
    """Fallback current-mode source: remember where the last walk landed.

    Only as good as its starting assumption, and drifts if the driver also
    uses the physical button. Prefer the real status signal (``0x1F4`` byte 1,
    ``signals.decode_drive_mode``) once it is wired into the daemon.
    """

    def __init__(self, start: DriveMode = DriveMode.NORMAL) -> None:
        self._mode = start

    def get(self) -> DriveMode | None:
        return self._mode

    def note_walk(self, presses: int) -> None:
        """Record that a walk of ``presses`` presses just committed.

        Under the menu model a walk lands on an *absolute* cursor index --
        ``MODE_CYCLE_ORDER[presses - 1]`` -- not ``current + presses``.
        """
        if presses >= 1:
            self._mode = MODE_CYCLE_ORDER[(presses - 1) % len(MODE_CYCLE_ORDER)]

    #: Back-compat alias -- the daemon wires this as ``on_presses_sent``.
    note_presses = note_walk

    def reset(self, mode: DriveMode) -> None:
        self._mode = mode


class ModeCycleController:
    def __init__(
        self,
        presser: ButtonPresser,
        current_mode_source: Callable[[], DriveMode | None],
        *,
        menu_cursor_source: Callable[[], DriveMode | None] | None = None,
        on_presses_sent: Callable[[int], None] | None = None,
        tap_observer: Callable[[int, DriveMode], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._presser = presser
        self._current_mode_source = current_mode_source
        self._menu_cursor_source = menu_cursor_source
        self._on_presses_sent = on_presses_sent
        #: Optional diagnostics hook: called once per closed-loop tap, after
        #: the cursor settle, with ``(tap_number, target)``. The daemon wires
        #: this to log the per-tap 0x1F4 cursor/raw/byte-1 trace during a
        #: walk-test. Never affects control flow.
        self._tap_observer = tap_observer
        self._sleep = sleep

    def switch_to(self, target: DriveMode, *, force: bool = False) -> int:
        """Walk the drive-mode menu to ``target``. Returns taps sent
        (0 if already there and ``force`` is not set).

        Raises :class:`ModeUnknownError` if the committed-mode source returns
        ``None`` -- we do not transmit into a bus we cannot observe.

        With ``force=True`` the walk runs even when the committed-mode source
        already reads ``target`` (the source still must not be ``None``).
        Useful when that readback is not trustworthy -- e.g. a parked car,
        where ``0x1F4`` byte 1 reverts a few seconds after a commit.

        **Closed loop** when a ``menu_cursor_source`` is wired: tap, let the
        cursor settle, read it back; stop the instant it is on ``target``,
        then leave the bus quiet so the menu commits ~3 s later. Raises
        :class:`ModeSwitchFailed` if the cursor never lands within
        ``MAX_WALK_TAPS`` taps.

        **Open loop** otherwise: send ``presses_to_reach(target)`` taps
        ``WALK_GAP_S`` apart and trust the menu model. Does not verify the
        landing -- callers poll ``0x1F4`` byte 1 over the next few seconds.
        """
        current = self._current_mode_source()
        if current is None:
            raise ModeUnknownError("drive-mode status unreadable; not injecting")
        if current == target and not force:
            return 0

        if self._menu_cursor_source is not None:
            return self._walk_closed_loop(target)
        return self._walk_open_loop(target)

    # -- walk strategies ------------------------------------------------------
    def _walk_open_loop(self, target: DriveMode) -> int:
        count = presses_to_reach(target)
        if count > MAX_WALK_PRESSES:  # unreachable for the 4 real modes; belt-and-braces
            raise ModeUnknownError(
                f"computed {count} presses (> {MAX_WALK_PRESSES}); refusing"
            )
        for i in range(count):
            self._presser.send_mode_button_press()
            if i != count - 1:
                self._sleep(WALK_GAP_S)
        self._report(target)
        return count

    def _walk_closed_loop(self, target: DriveMode) -> int:
        """Tap until the live menu cursor reads ``target``; then stop.

        The menu always opens on NORMAL, so tap 1 opens it (cursor -> NORMAL)
        and each further tap steps the cursor one row. Reading the cursor back
        after every tap means a coalesced double-step or a dropped tap just
        changes how many more taps we send -- we never overshoot past the
        target and hold, because we stop the moment the cursor matches.
        """
        taps = 0
        for _ in range(MAX_WALK_TAPS):
            self._presser.send_mode_button_press()
            taps += 1
            self._sleep(CURSOR_SETTLE_S)
            cursor = self._menu_cursor_source()
            if self._tap_observer is not None:
                self._tap_observer(taps, target)
            if cursor == target:
                self._report(target)
                return taps
            self._sleep(WALK_GAP_S)
        # No _report on the failure path: we tapped MAX_WALK_TAPS times and the
        # cursor is somewhere unknown -- claiming it landed on `target` would
        # poison a PressCountingModeTracker. Caller handles the exception.
        raise ModeSwitchFailed(
            f"menu cursor never reached {target.value} in {taps} taps "
            f"(bus degraded, menu not opening, or cluster ignoring us)"
        )

    def _report(self, landed: DriveMode) -> None:
        """Tell ``on_presses_sent`` where the walk left the cursor.

        We pass the canonical ``presses_to_reach(landed)`` rather than the raw
        tap count so a ``PressCountingModeTracker`` behind this still resolves
        to the right mode even when the closed loop needed a coalesced-step or
        recovery tap. The true tap count is ``switch_to``'s return value.
        """
        if self._on_presses_sent is not None:
            self._on_presses_sent(presses_to_reach(landed))

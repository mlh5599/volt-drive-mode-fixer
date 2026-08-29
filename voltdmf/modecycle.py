"""Mode-cycle controller.

The car has one button that cycles NORMAL -> SPORT -> MOUNTAIN -> HOLD ->
NORMAL. Reaching a target mode means sending that one button-press message
the right number of times, which requires knowing the *current* mode. That
is why a current-mode source is mandatory here, not optional (DESIGN.md
"Trigger strategies" / "Open items").

This module computes the press count and drives the (injected) transmit
function. It is deliberately ignorant of *which* trigger asked.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from .signals import MODE_CYCLE_ORDER, DriveMode

#: Longest possible cycle is NORMAL -> HOLD = 3 presses. Anything asking for
#: more than this is a bug; the safety layer also enforces it.
MAX_PRESSES_PER_BURST = 3

#: Realistic spacing between individual presses (reference project value).
BUTTON_PRESS_COOLDOWN_S = 0.75


class ModeUnknownError(RuntimeError):
    """Current mode could not be determined -- refuse to inject blindly."""


class ModeSwitchFailed(RuntimeError):
    """Post-send readback shows we did not land on the requested mode."""


class ButtonPresser(Protocol):
    def send_mode_button_press(self) -> None: ...


def presses_to_reach(current: DriveMode, target: DriveMode) -> int:
    """Number of button presses to cycle from ``current`` to ``target``.

    0 if already there. Pure function -- the core unit-tested piece.
    """
    order = MODE_CYCLE_ORDER
    return (order.index(target) - order.index(current)) % len(order)


class PressCountingModeTracker:
    """Fallback current-mode source: count presses from a known start.

    Only as good as its starting assumption, and drifts if the driver also
    uses the physical button. Prefer a real status signal once Phase C finds
    one (DESIGN.md "Open items").
    """

    def __init__(self, start: DriveMode = DriveMode.NORMAL) -> None:
        self._mode = start

    def get(self) -> DriveMode | None:
        return self._mode

    def note_presses(self, count: int) -> None:
        order = MODE_CYCLE_ORDER
        self._mode = order[(order.index(self._mode) + count) % len(order)]

    def reset(self, mode: DriveMode) -> None:
        self._mode = mode


class ModeCycleController:
    def __init__(
        self,
        presser: ButtonPresser,
        current_mode_source: Callable[[], DriveMode | None],
        *,
        on_presses_sent: Callable[[int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._presser = presser
        self._current_mode_source = current_mode_source
        self._on_presses_sent = on_presses_sent
        self._sleep = sleep

    def switch_to(self, target: DriveMode) -> int:
        """Cycle to ``target``. Returns the number of presses sent.

        Raises :class:`ModeUnknownError` if the current mode is unknown, and
        :class:`ModeSwitchFailed` if a post-send readback disagrees with the
        target (only checked when the source still returns a mode afterward).
        """
        current = self._current_mode_source()
        if current is None:
            raise ModeUnknownError("current drive mode is unknown; not injecting")

        count = presses_to_reach(current, target)
        if count == 0:
            return 0
        if count > MAX_PRESSES_PER_BURST:
            raise ModeUnknownError(
                f"computed {count} presses (> {MAX_PRESSES_PER_BURST}); refusing"
            )

        for i in range(count):
            self._presser.send_mode_button_press()
            if i != count - 1:
                self._sleep(BUTTON_PRESS_COOLDOWN_S)

        if self._on_presses_sent is not None:
            self._on_presses_sent(count)

        after = self._current_mode_source()
        if after is not None and after != target:
            raise ModeSwitchFailed(
                f"wanted {target.value}, readback says {after.value}"
            )
        return count

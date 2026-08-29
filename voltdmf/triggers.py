"""Trigger evaluators -- pure decision logic, no I/O.

Each trigger decides *when* to fire and *which* mode to request. It never
talks to the bus; :mod:`voltdmf.modecycle` (via :mod:`voltdmf.safety`) is the
only thing that transmits.
"""

from __future__ import annotations

from .config import Config
from .signals import DriveMode
from .state import VehicleState


class Trigger:
    name: str = "trigger"

    def evaluate(self, state: VehicleState) -> DriveMode | None:
        """Return a target mode to switch to, or ``None`` to do nothing."""
        raise NotImplementedError


class OnStartTrigger(Trigger):
    """Fire once, the first time we see a live drive cycle after process start.

    The Pi is powered from the switched accessory socket, so the daemon only
    runs while the car is on -- process start *is* the on-start event
    (DESIGN.md). We still wait for ``bus_active`` so a crash-restart mid-drive
    doesn't fire before we can read the real vehicle state.
    """

    name = "on_start"

    def __init__(self, target: DriveMode) -> None:
        self._target = target
        self._fired = False

    def evaluate(self, state: VehicleState) -> DriveMode | None:
        if self._fired or not state.bus_active:
            return None
        self._fired = True
        return self._target


class SocThresholdTrigger(Trigger):
    """Fire once when SOC crosses below ``threshold``; re-arm above ``reset``.

    The latch stops it re-firing while SOC hovers near the threshold; the
    re-arm lets it fire again on a later drive after the pack has charged.
    """

    name = "soc_threshold"

    def __init__(self, target: DriveMode, threshold_percent: float,
                 reset_percent: float) -> None:
        if reset_percent <= threshold_percent:
            raise ValueError("reset_percent must be > threshold_percent")
        self._target = target
        self._threshold = threshold_percent
        self._reset = reset_percent
        self._latched = False

    def evaluate(self, state: VehicleState) -> DriveMode | None:
        soc = state.soc_percent
        if soc is None:
            return None
        if self._latched:
            if soc >= self._reset:
                self._latched = False
            return None
        if soc <= self._threshold:
            self._latched = True
            return self._target
        return None


def build_triggers(config: Config) -> list[Trigger]:
    """Instantiate the enabled triggers from config, in evaluation order."""
    triggers: list[Trigger] = []
    if config.on_start.enabled:
        triggers.append(OnStartTrigger(config.on_start.target_mode))
    if config.soc_threshold.enabled:
        triggers.append(SocThresholdTrigger(
            config.soc_threshold.target_mode,
            config.soc_threshold.threshold_percent,
            config.soc_threshold.reset_percent,
        ))
    return triggers

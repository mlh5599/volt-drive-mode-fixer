"""Shared, mutable view of the vehicle, updated from the CAN RX loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .signals import DriveMode, ShiftPosition

#: If no known signal frame has been seen for this long, treat the bus as
#: quiet -> car is off (Global A buses go silent with the ignition off).
BUS_QUIET_TIMEOUT_S = 2.0


@dataclass
class VehicleState:
    speed_mph: float | None = None
    soc_percent: float | None = None
    soc_raw: int | None = None
    shift: ShiftPosition = ShiftPosition.UNKNOWN
    #: Current drive mode -- stays ``None`` until Phase C finds a status
    #: signal (or the press-counting fallback populates it).
    drive_mode: DriveMode | None = None
    last_signal_monotonic: float | None = field(default=None)

    def mark_signal_seen(self) -> None:
        self.last_signal_monotonic = time.monotonic()

    @property
    def bus_active(self) -> bool:
        if self.last_signal_monotonic is None:
            return False
        return (time.monotonic() - self.last_signal_monotonic) < BUS_QUIET_TIMEOUT_S

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
    #: Exact pack charge from the ``22 005B`` diagnostic poll (X * 100 / 255).
    soc_percent: float | None = None
    #: Raw charge byte from that poll (0..255).
    soc_raw: int | None = None
    #: ``time.monotonic()`` of the last poll reply -- for staleness checks.
    soc_percent_monotonic: float | None = None
    #: Coarse passive proxy: 0x096 byte 3 in the ``x F0 0A xx`` mux (~13 %
    #: SOC per count). Failsafe for the reconciler when the poll goes stale.
    soc_bar_raw: int | None = None
    #: Which source last set ``soc_percent`` ("poll") -- ``None`` until a
    #: reply lands.
    soc_source: str | None = None
    #: The 0x7E8..0x7EF id the poll locked onto (``req id + 8``).
    uds_resp_id: int | None = None
    uds_replies: int = 0
    uds_nrcs: int = 0
    shift: ShiftPosition = ShiftPosition.UNKNOWN
    #: Current drive mode -- stays ``None`` until the first 0x1F4 frame.
    drive_mode: DriveMode | None = None
    #: Live drive-mode menu cursor (0x1F4 byte 4). Set only while the menu is
    #: open (byte 5 bit 7); ``None`` when it is closed -- an idle frame carries
    #: byte 4 == 0x00, which would otherwise read as NORMAL. The reconciler's
    #: mode walk closes its loop on this (``ModeCycleController`` cursor source).
    menu_cursor: DriveMode | None = None
    last_signal_monotonic: float | None = field(default=None)

    def mark_signal_seen(self) -> None:
        self.last_signal_monotonic = time.monotonic()

    @property
    def bus_active(self) -> bool:
        if self.last_signal_monotonic is None:
            return False
        return (time.monotonic() - self.last_signal_monotonic) < BUS_QUIET_TIMEOUT_S

    def soc_percent_age(self) -> float | None:
        """Seconds since the last poll reply, or ``None`` if none yet."""
        if self.soc_percent_monotonic is None:
            return None
        return time.monotonic() - self.soc_percent_monotonic

    def soc_percent_fresh(self, max_age_s: float) -> bool:
        """True if a poll reply landed within ``max_age_s`` seconds."""
        age = self.soc_percent_age()
        return age is not None and age <= max_age_s

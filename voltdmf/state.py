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
    #: Newest live 0x1E1 button frame (>= 7 B) as ``(payload, monotonic)``.
    #: Stored as ONE tuple so a reader always gets a matched pair -- the
    #: tracking-echo press reads this from another thread.
    #:
    #: This is the frame ``CanInterface.send_mode_button_press`` mirrors. It
    #: comes through the RX listener rather than a second ``bus.recv()``
    #: because when a ``can.Notifier`` is running it owns the socket, and a
    #: competing ``recv()`` loses most frames to it -- measured as a ~21%
    #: dropped-tap rate in the daemon vs. 0/22 for the same press logic with
    #: a single reader (2026-09-03).
    button_frame: tuple[bytes, float] | None = None
    #: Current drive mode -- stays ``None`` until the first 0x1F4 frame.
    drive_mode: DriveMode | None = None
    #: Live drive-mode menu cursor, decoded from 0x1F4 bytes 4 AND 5 together
    #: (``signals.decode_menu_cursor``), newest frame. ``None`` means the menu
    #: is CLOSED -- a real reading, not "unknown", so it is assigned
    #: unconditionally: latching the last cursor instead would let a stale
    #: value match a walk target and stop the walk early.
    menu_cursor: DriveMode | None = None
    #: Raw 0x1F4 byte 4, newest frame, whether or not it is in the decode map
    #: -- walk-test diagnostics so an unmapped cursor code is still visible in
    #: the per-tap trace. Does not drive the closed loop. Note byte 4 alone
    #: cannot distinguish NORMAL from a closed menu (both 0x00); pair it with
    #: ``menu_open_hint``.
    menu_cursor_raw: int | None = None
    #: Whether the menu is open at all, i.e. ``menu_cursor is not None``.
    #: Diagnostics only. (This was once read off byte-5 bit 7 alone; that bit
    #: is not an open flag, it is the NORMAL cursor code -- see
    #: ``signals.CURSOR_NORMAL_BIT``.)
    menu_open_hint: bool = False
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

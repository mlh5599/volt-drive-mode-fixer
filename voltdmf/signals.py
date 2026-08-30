"""CAN signal identifiers and hardcoded frame decoders.

We parse the handful of frames this project cares about directly with
``struct`` rather than pulling in ``opendbc`` / ``cantools`` -- the prior-art
project (``vix597/chevy-volt-trip-mode``) does the same, and current
``opendbc`` has restructured away the ``gm_global_a_powertrain.dbc`` the
design doc referenced.

Every entry in :data:`SIGNAL_IDS` carries a ``confirmed`` flag. Nothing here
is confirmed on the actual Gen 1 vehicle yet -- the values are the Gen 2
(2017) candidates from the reference project plus OVMS notes. DESIGN.md
Phase C (``tools/``) is where these get verified and the flags flipped.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum


class DriveMode(str, Enum):
    """The four modes the physical button cycles through, in cycle order."""

    NORMAL = "normal"
    SPORT = "sport"
    MOUNTAIN = "mountain"
    HOLD = "hold"


#: Fixed order the single mode button cycles through (DESIGN.md "Vehicle context").
MODE_CYCLE_ORDER: tuple[DriveMode, ...] = (
    DriveMode.NORMAL,
    DriveMode.SPORT,
    DriveMode.MOUNTAIN,
    DriveMode.HOLD,
)


class ShiftPosition(str, Enum):
    PARK = "park"
    REVERSE = "reverse"
    NEUTRAL = "neutral"
    DRIVE = "drive"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SignalId:
    name: str
    addr: int
    #: True once verified on the actual Gen 1 car (DESIGN.md Phase C).
    confirmed: bool
    note: str = ""


SIGNAL_IDS: dict[str, SignalId] = {
    "soc": SignalId(
        "EV battery SOC",
        0x206,
        confirmed=False,
        note="bytes 1-2; ~0.25 kWh/count per OVMS notes; scaling needs calibration",
    ),
    "speed": SignalId(
        "Vehicle speed",
        0x3E9,
        confirmed=False,
        note="bytes 0-1 big-endian / 64 -> km/h (x0.621371 -> mph). Matches "
        "the GM Volt reverse-engineering wiki (\"3E9 Bytes 1-2 Speed 1/64 "
        "KPH\") and the Session-8 full-drain capture: ~0 at rest, ~63 mph "
        "highway cruise, ~8 mph at the mid-drive turnaround (+1240 s). DLC 8, "
        "10 Hz; bytes 2 & 6 are a mux/rolling counter, bytes 0-1 carry the "
        "speed word regardless. Still confirmed=False -- not yet checked "
        "against a reference speedo.",
    ),
    "shift": SignalId(
        "Shift / PRNDL position",
        0x1F5,
        confirmed=True,
        note="Confirmed 2026-08-29 on-vehicle (session 4, drive). byte 3 = "
        "PRNDL detent: 1 PARK, 2 REVERSE, 3 NEUTRAL, 4 DRIVE, 5 LOW. Stepped "
        "1->2->3->4->5 in order during a parked P-R-N-D-L walk and held 4 "
        "(DRIVE) rock-steady for the entire 9-minute drive. bytes 0-1 co-vary "
        "but look like a counter/checksum. 0x135 byte 0 also tracks the "
        "shifter but with a messier non-sequential encoding -- 0x1F5 byte 3 "
        "is the clean signal.",
    ),
    "drive_mode_button": SignalId(
        "Drive mode cycle button press (TX only)",
        0x1E1,
        confirmed=True,
        note="Confirmed 2026-08-29 on-vehicle (session 4, drive). 0x1E1 "
        "\"ASCMSteeringButton\", byte 4 bit 7 = drive-mode button pressed "
        "(byte 4 goes 00/01/02/03 -> 80/81/82/83 during a physical press; low "
        "bits are a rolling counter). Same ID/bit the Gen 2 prior art injects. "
        "The receiver counts button edges, not hold time. The tracking-echo "
        "injection (voltdmf/canio.send_mode_button_press) drove a closed-loop "
        "walk to all four modes on the road -- each committed in 2.8 s and "
        "held the full 90 s while moving (21-51 mph), can0 ERROR-ACTIVE "
        "throughout. Verified independently in the raw capture: 0x1F4 byte 1 "
        "held N/S/M/H for each 90 s block.",
    ),
    "drive_mode_status": SignalId(
        "Current drive mode (status)",
        0x1F4,
        confirmed=True,
        note="Confirmed 2026-08-29 on-vehicle (Gen 1, HS-CAN 500k). byte 1 "
        "latched mode: 0x00 NORMAL, 0x80 SPORT, 0x20 MOUNTAIN, 0x08 HOLD "
        "(720/720 frames steady per mode across a full button walk).",
    ),
}

#: 0x135 also moves with the shifter (byte 0: 0/1/2/3, non-sequential) but
#: 0x1F5 byte 3 is the clean PRNDL enum. Kept known so the RX path still
#: counts it as a signal frame; not decoded.
_ALT_SHIFT_ADDR = 0x135


# --- SOC (0x206) -------------------------------------------------------------
#
# DESIGN.md: "Bytes 1-2, ~0.25 kWh/count granularity per OVMS notes. Needs
# calibration against the vehicle's dash %/kWh readings."
#
# TODO_CALIBRATE: every constant below is a guess until watch_soc.py has been
# run against the dash. Treat decode_soc_raw() output as *relative* until then.
SOC_KWH_PER_COUNT = 0.25  # TODO_CALIBRATE
GEN1_PACK_USABLE_KWH = 10.8  # TODO_CALIBRATE (rough Gen 1 usable capacity)


def decode_soc_raw(data: bytes) -> int | None:
    """Raw 16-bit value from bytes 1-2 of frame 0x206, big-endian.

    Returns ``None`` if the frame is too short. Byte order and offset are
    unverified -- see TODO_CALIBRATE above.
    """
    if len(data) < 3:
        return None
    return struct.unpack_from(">H", data, 1)[0]


def soc_percent_from_raw(raw: int) -> float:
    """Best-effort raw -> percent using the (uncalibrated) constants above."""
    kwh = raw * SOC_KWH_PER_COUNT
    return max(0.0, min(100.0, 100.0 * kwh / GEN1_PACK_USABLE_KWH))


def decode_soc_percent(data: bytes) -> float | None:
    raw = decode_soc_raw(data)
    return None if raw is None else soc_percent_from_raw(raw)


# --- Vehicle speed (0x3E9) -------------------------------------------------
_SPEED_KMH_PER_COUNT = 1.0 / 64.0
_MPH_PER_KMH = 0.621371


def decode_speed_kmh(data: bytes) -> float | None:
    """Bytes 0-1, big-endian, / 64 -> km/h.

    Per the GM Volt reverse-engineering wiki and cross-checked against the
    Session-8 full-drain capture (see the ``"speed"`` note above). Bytes 2 &
    6 of this frame are a mux/rolling counter; bytes 0-1 hold the speed word
    regardless, so no de-muxing is needed here.
    """
    if len(data) < 2:
        return None
    return (struct.unpack_from(">H", data, 0)[0]) * _SPEED_KMH_PER_COUNT


def decode_speed_mph(data: bytes) -> float | None:
    """Bytes 0-1, big-endian, / 64 -> km/h -> mph."""
    kmh = decode_speed_kmh(data)
    return None if kmh is None else kmh * _MPH_PER_KMH


# --- Drive mode status (0x1F4) -----------------------------------------
#
# Confirmed 2026-08-29 on-vehicle (Gen 1, HS-CAN 500k). 6-byte message from a
# body module at ~40 Hz: `00 <mode> 00 00 <cursor> <menu_open>`.
#   byte 1  latched / committed mode (values below). Changes ONLY on commit,
#           ~3.0 s after the last button tap.
#   byte 4  LIVE drive-mode MENU CURSOR -- steps with every button edge, in
#           walk order, ~40 ms after the tap (00 N / 80 S / 40 M / 20 H).
#           (An earlier note called this a "hold-time ramp"; the frame-count
#           sweep on 2026-08-29 showed it is the menu cursor -- one clean step
#           per tap when taps are >= ~1.2 s apart.)
#   byte 5  menu-open hint: 0x80 while the drive-mode menu is open, clears
#           ~3 s after the last tap (coincident with the commit). Flickers
#           mid-walk on the injected path, so treat as a hint, not gospel.
# Every one of a 9-transition button walk (NORMAL/SPORT/MOUNTAIN/HOLD dwell +
# 5 taps) matched byte 1 exactly, in cycle order.
_DRIVE_MODE_BY_BYTE1: dict[int, DriveMode] = {
    0x00: DriveMode.NORMAL,
    0x80: DriveMode.SPORT,
    0x20: DriveMode.MOUNTAIN,
    0x08: DriveMode.HOLD,
}

#: 0x1F4 byte 4 -- the live menu cursor. NOTE the byte-4 codes differ from the
#: byte-1 codes: MOUNTAIN is 0x40 here (0x20 in byte 1) and HOLD is 0x20 here
#: (0x08 in byte 1). NORMAL is 0x00 in both, which is also what an idle
#: (menu-closed) frame carries -- so a 0x00 cursor is only meaningful right
#: after a tap.
_MENU_CURSOR_BY_BYTE4: dict[int, DriveMode] = {
    0x00: DriveMode.NORMAL,
    0x80: DriveMode.SPORT,
    0x40: DriveMode.MOUNTAIN,
    0x20: DriveMode.HOLD,
}

#: 0x1F4 byte 5 bit 7 -- drive-mode menu is open.
MENU_OPEN_BIT = 0x80


def decode_drive_mode(data: bytes) -> DriveMode | None:
    """Latched drive mode from byte 1 of frame 0x1F4.

    Returns ``None`` for a short frame or an unrecognised byte-1 value.
    Bytes 4-5 (menu cursor / open flag) are intentionally ignored -- byte 1
    holds the *committed* mode steady even mid-walk.
    """
    if len(data) < 2:
        return None
    return _DRIVE_MODE_BY_BYTE1.get(data[1])


def decode_menu_cursor(data: bytes) -> DriveMode | None:
    """Live drive-mode menu cursor from byte 4 of frame 0x1F4.

    This tracks the menu highlight as it walks (one step per button edge),
    ~40 ms behind the tap -- unlike :func:`decode_drive_mode`, which only
    moves on the commit ~3 s later. Use it to close the loop on a menu walk.

    Returns ``None`` for a short frame or an unrecognised value. A ``0x00``
    result is ``NORMAL`` but is also what an idle/closed frame carries, so
    only trust it in the window just after a tap.
    """
    if len(data) < 5:
        return None
    return _MENU_CURSOR_BY_BYTE4.get(data[4])


def menu_is_open(data: bytes) -> bool:
    """True if frame 0x1F4 says the drive-mode menu is currently open."""
    return len(data) >= 6 and bool(data[5] & MENU_OPEN_BIT)


# --- Shift position (0x1F5 byte 3) -------------------------------------
#
# Confirmed 2026-08-29 on-vehicle (session 4). Frame 0x1F5, ~40 Hz. Byte 3 is
# the PRNDL detent as a small enum; it stepped 1..5 in order through a parked
# P-R-N-D-L walk and then sat on 4 (DRIVE) for the whole 9-minute drive
# regardless of speed. Bytes 0-1 co-vary (counter/checksum), bytes 4-7 idle.
_SHIFT_BY_BYTE3: dict[int, ShiftPosition] = {
    0x01: ShiftPosition.PARK,
    0x02: ShiftPosition.REVERSE,
    0x03: ShiftPosition.NEUTRAL,
    0x04: ShiftPosition.DRIVE,
    0x05: ShiftPosition.LOW,
}


def decode_shift(data: bytes) -> ShiftPosition:
    """PRNDL detent from byte 3 of frame 0x1F5.

    Returns :data:`ShiftPosition.UNKNOWN` for a short frame or a byte-3 value
    outside 1..5 (never observed on this car, but stay defensive -- the
    daemon's SafetyGate treats UNKNOWN as non-blocking only while
    ``allow_unknown_shift`` is set).
    """
    if len(data) < 4:
        return ShiftPosition.UNKNOWN
    return _SHIFT_BY_BYTE3.get(data[3], ShiftPosition.UNKNOWN)


def is_signal_frame(addr: int) -> bool:
    """True if ``addr`` is one we know how to decode into VehicleState."""
    known = {SIGNAL_IDS["soc"].addr, SIGNAL_IDS["speed"].addr,
             SIGNAL_IDS["shift"].addr, SIGNAL_IDS["drive_mode_status"].addr,
             _ALT_SHIFT_ADDR}
    return addr in known

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
        note="first 2 bytes big-endian, /100 -> mph (reference project, Gen 2)",
    ),
    "shift": SignalId(
        "Shift / PRNDL position",
        0x135,
        confirmed=False,
        note="also seen as 0x1F5; byte layout undocumented, discover in Phase C",
    ),
    "drive_mode_button": SignalId(
        "Drive mode cycle button press (TX only)",
        0x1E1,
        confirmed=False,
        note="ADDRESS confirmed 2026-08-29 on-vehicle (session 2): 0x1E1 "
        "\"ASCMSteeringButton\", byte 4 bit 7 = drive-mode button pressed "
        "(byte 4 goes 00/01/02/03 -> 80/81/82/83 during a physical press; low "
        "bits are a rolling counter). Same ID/bit the Gen 2 prior art injects. "
        "The receiver counts button edges, not hold time. INJECTION efficacy "
        "is unproven -- Phase C.5 (a module also streams 0x1E1 at ~40 Hz, so "
        "the injected burst must go out back-to-back at line rate).",
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

_ALT_SHIFT_ADDR = 0x1F5


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
def decode_speed_mph(data: bytes) -> float | None:
    """First two bytes, big-endian, /100 -> mph (reference project)."""
    if len(data) < 2:
        return None
    return struct.unpack_from(">H", data, 0)[0] / 100.0


# --- Drive mode status (0x1F4) -----------------------------------------
#
# Confirmed 2026-08-29 on-vehicle (Gen 1, HS-CAN 500k). 6-byte message from a
# body module at ~40 Hz: `00 <mode> 00 00 <btn_ramp> <btn_down>`.
#   byte 1  latched current mode (values below)
#   byte 4  button hold-time ramp (0x80 -> 0x40 -> 0x20 the longer it's held)
#   byte 5  0x80 while the button is physically pressed, else 0x00
# Every one of a 9-transition button walk (NORMAL/SPORT/MOUNTAIN/HOLD dwell +
# 5 taps) matched this exactly, in cycle order.
_DRIVE_MODE_BY_BYTE1: dict[int, DriveMode] = {
    0x00: DriveMode.NORMAL,
    0x80: DriveMode.SPORT,
    0x20: DriveMode.MOUNTAIN,
    0x08: DriveMode.HOLD,
}


def decode_drive_mode(data: bytes) -> DriveMode | None:
    """Latched drive mode from byte 1 of frame 0x1F4.

    Returns ``None`` for a short frame or an unrecognised byte-1 value.
    Bytes 4-5 (momentary button activity) are intentionally ignored -- byte 1
    holds the current mode steady even mid-press.
    """
    if len(data) < 2:
        return None
    return _DRIVE_MODE_BY_BYTE1.get(data[1])


# --- Shift position (0x135 / 0x1F5) --------------------------------------
def decode_shift(data: bytes) -> ShiftPosition:  # noqa: ARG001
    """Not decodable yet -- byte layout is undocumented for this car.

    Kept as a typed stub so callers can already depend on the interface;
    fill in once cycle_modes.py / mode_diff.py reveal the layout in Phase C.
    """
    return ShiftPosition.UNKNOWN


def is_signal_frame(addr: int) -> bool:
    """True if ``addr`` is one we know how to decode into VehicleState."""
    known = {SIGNAL_IDS["soc"].addr, SIGNAL_IDS["speed"].addr,
             SIGNAL_IDS["shift"].addr, _ALT_SHIFT_ADDR}
    return addr in known

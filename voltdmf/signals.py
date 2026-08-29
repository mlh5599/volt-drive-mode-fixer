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
        note="bit 39; DriveModeButton in old gm_global_a_powertrain.dbc, Gen 2 only",
    ),
    "drive_mode_status": SignalId(
        "Current drive mode (status)",
        0x000,
        confirmed=False,
        note="NOT FOUND YET -- hard requirement for modecycle; find in Phase C",
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

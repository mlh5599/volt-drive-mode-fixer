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


# --- Battery pack SOC ------------------------------------------------------
#
# 0x206 (the opendbc Gen 2 candidate) is NOT on this Gen 1 HS-CAN bus
# (confirmed absent, Sessions 6-9), and no broadcast frame carries pack SOC
# at a usable resolution -- Session 9 falsified the Session-8 candidates
# (0x3E3 / 0x228 / 0x186 are powertrain load, not charge). The only exact
# read is the UDS diagnostic poll 22 005B; a coarse passive proxy lives in
# 0x096 byte 3.

# UDS "Hybrid/EV Battery Pack Remaining Charge", DID 0x005B, service 0x22.
#   request   7E0#0322005B55555555  (try 7E4 then 7E0; lock onto the answerer)
#   response  7E8#0462005B<XX>...    SOC% = XX * 100 / 255
# Session 9 on-road: 185 replies / 186 requests over a 31-min drive, can0
# ERROR-ACTIVE throughout; decode exact and linear (0xE5 -> 89.8 %,
# 0x54 -> 32.9 %). Stays in the default diagnostic session, service 22 only
# -- no session switch, no TesterPresent -- so it cannot suppress the normal
# broadcasts.
UDS_SOC_DID = 0x005B
UDS_SOC_REQ_IDS: tuple[int, ...] = (0x7E4, 0x7E0)
UDS_RESP_ID_LO = 0x7E8
UDS_RESP_ID_HI = 0x7EF


def uds_soc_request_payload() -> bytes:
    """The 8-byte ISO-TP single-frame request body for ``22 005B`` (0x55 pad)."""
    return bytes((0x03, 0x22, UDS_SOC_DID >> 8, UDS_SOC_DID & 0xFF,
                  0x55, 0x55, 0x55, 0x55))


def uds_soc_percent(raw: int) -> float:
    """UDS pack-charge byte -> percent (``X * 100 / 255``)."""
    return raw * 100.0 / 255.0


def decode_uds_soc(data: bytes) -> tuple[str, int | None]:
    """Classify a 0x7E8..0x7EF frame that may carry a ``22 005B`` reply.

    Returns ``("ok", raw)`` for a positive response (``raw`` is the charge
    byte -- convert with :func:`uds_soc_percent`), ``("nrc", None)`` for a
    negative response to service 0x22, or ``("other", None)`` for anything
    else (a different PID/service, or a short frame).
    """
    if (len(data) >= 5 and data[0] >= 4 and data[1] == 0x62
            and (data[2] << 8 | data[3]) == UDS_SOC_DID):
        return ("ok", data[4])
    if len(data) >= 3 and data[1] == 0x7F and data[2] == 0x22:
        return ("nrc", None)
    return ("other", None)


# --- Coarse passive SOC proxy (0x096 byte 3) -----------------------------
#
# Session 9: in the "x F0 0A xx" mux frame (byte 1 == 0xF0 and byte 2 ==
# 0x0A) byte 3 steps 13 -> 9 across the whole usable pack -- monotone with
# charge (r = 0.95 vs the diag poll) and it flattens during HOLD like SOC
# does, but only ~13 % SOC per count. So it is a sanity check / poll-failure
# failsafe, not a control input: b3 <= 9 is roughly 2 gauge bars (~30 % SOC).
SOC_BAR_ADDR = 0x096
_SOC_BAR_MUX = (0xF0, 0x0A)


def decode_soc_bar_raw(data: bytes) -> int | None:
    """Byte 3 of the 0x096 ``x F0 0A xx`` mux frame, or ``None``.

    0x096 is multiplexed and only these frames carry the slow byte-3 value;
    returns ``None`` for a short frame or any non-mux 0x096 frame.
    """
    if len(data) < 4 or (data[1], data[2]) != _SOC_BAR_MUX:
        return None
    return data[3]


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
#   bytes LIVE drive-mode MENU CURSOR, spanning BOTH byte 4 and byte 5 --
#   4+5   they are one field, not two. Confirmed 2026-09-03 by a 2432-frame
#         0x1F4 capture across a full menu walk:
#             b4=0x00 b5=0x80  cursor on NORMAL   (403 frames)
#             b4=0x80 b5=0x00  cursor on SPORT
#             b4=0x40 b5=0x00  cursor on MOUNTAIN
#             b4=0x20 b5=0x00  cursor on HOLD
#             b4=0x00 b5=0x00  menu CLOSED, no cursor  (439 frames)
#         b5 bit 7 was set on 403 frames and every single one carried
#         b4 == 0x00 -- zero exceptions. So bit 7 is not a "menu open" flag
#         that happens to flicker; it *is* the NORMAL cursor code, which is
#         why NORMAL cannot be read out of byte 4 alone. Two earlier readings
#         of this byte (a "hold-time ramp", then a flickering "menu-open
#         hint") were both artifacts of decoding byte 4 in isolation.
# Every one of a 9-transition button walk (NORMAL/SPORT/MOUNTAIN/HOLD dwell +
# 5 taps) matched byte 1 exactly, in cycle order.
_DRIVE_MODE_BY_BYTE1: dict[int, DriveMode] = {
    0x00: DriveMode.NORMAL,
    0x80: DriveMode.SPORT,
    0x20: DriveMode.MOUNTAIN,
    0x08: DriveMode.HOLD,
}

#: 0x1F4 byte 4 -- the non-NORMAL menu-cursor codes. NOTE they differ from the
#: byte-1 codes: MOUNTAIN is 0x40 here (0x20 in byte 1) and HOLD is 0x20 here
#: (0x08 in byte 1). NORMAL is deliberately absent -- it is carried by
#: ``CURSOR_NORMAL_BIT`` in byte 5, and byte 4 == 0x00 with that bit clear
#: means the menu is CLOSED.
_MENU_CURSOR_BY_BYTE4: dict[int, DriveMode] = {
    0x80: DriveMode.SPORT,
    0x40: DriveMode.MOUNTAIN,
    0x20: DriveMode.HOLD,
}

#: 0x1F4 byte 5 bit 7 -- the menu cursor is on NORMAL. This is the NORMAL
#: cursor code itself (see the block comment above), which is why byte 4 is
#: always 0x00 whenever it is set.
CURSOR_NORMAL_BIT = 0x80

#: Back-compat alias for the old (mis-named) constant. Same bit; it does not
#: mean "menu open" -- use :func:`menu_is_open` for that.
MENU_OPEN_BIT = CURSOR_NORMAL_BIT


def decode_drive_mode(data: bytes) -> DriveMode | None:
    """Latched drive mode from byte 1 of frame 0x1F4.

    Returns ``None`` for a short frame or an unrecognized byte-1 value.
    Bytes 4-5 (menu cursor / open flag) are intentionally ignored -- byte 1
    holds the *committed* mode steady even mid-walk.
    """
    if len(data) < 2:
        return None
    return _DRIVE_MODE_BY_BYTE1.get(data[1])


def decode_menu_cursor(data: bytes) -> DriveMode | None:
    """Live drive-mode menu cursor from frame 0x1F4, bytes 4 **and** 5.

    Tracks the menu highlight as it walks (one step per button edge), ~40 ms
    behind the tap -- unlike :func:`decode_drive_mode`, which only moves on
    the commit ~3 s later. Use it to close the loop on a menu walk.

    Returns ``None`` when the menu is CLOSED (no cursor to report), and for a
    short or unrecognized frame. Because closed is now distinguishable from
    "cursor on NORMAL", the result is trustworthy at any time -- it does not
    have to be sampled in a window just after a tap.
    """
    if len(data) < 6:
        return None
    b4, b5 = data[4], data[5]
    if b5 & CURSOR_NORMAL_BIT:
        # NORMAL is signalled in byte 5; byte 4 is always 0x00 alongside it.
        return DriveMode.NORMAL if b4 == 0x00 else None
    if b4 == 0x00:
        return None  # menu closed -- no cursor
    return _MENU_CURSOR_BY_BYTE4.get(b4)


def menu_is_open(data: bytes) -> bool:
    """True if frame 0x1F4 says the drive-mode menu is currently open.

    Equivalent to "a cursor can be read": the menu is open exactly when
    :func:`decode_menu_cursor` resolves to a mode.
    """
    return decode_menu_cursor(data) is not None


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
    outside 1..5 (never observed on this car, but stay defensive). Shift is
    reported, not enforced: the SafetyGate does not gate a mode switch on
    PRNDL -- see the note in :mod:`voltdmf.safety`.
    """
    if len(data) < 4:
        return ShiftPosition.UNKNOWN
    return _SHIFT_BY_BYTE3.get(data[3], ShiftPosition.UNKNOWN)


def is_signal_frame(addr: int) -> bool:
    """True if ``addr`` is one we know how to decode into VehicleState."""
    if addr == SOC_BAR_ADDR or UDS_RESP_ID_LO <= addr <= UDS_RESP_ID_HI:
        return True
    known = {SIGNAL_IDS["speed"].addr, SIGNAL_IDS["shift"].addr,
             SIGNAL_IDS["drive_mode_status"].addr, _ALT_SHIFT_ADDR}
    return addr in known

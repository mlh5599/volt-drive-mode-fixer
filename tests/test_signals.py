import pytest

from voltdmf import signals
from voltdmf.signals import DriveMode, ShiftPosition


@pytest.mark.parametrize(
    "data, expected_kmh",
    [
        (bytes([0x16, 0x80]), 90.0),         # wiki example: 5760 counts == 90 km/h
        (bytes([0x00, 0x00]), 0.0),
        (bytes([0x00, 0x40, 0xFF]), 1.0),    # 64 counts == 1 km/h; extra bytes ignored
    ],
)
def test_decode_speed_kmh(data, expected_kmh):
    assert signals.decode_speed_kmh(data) == pytest.approx(expected_kmh)


def test_decode_speed_mph_from_kmh():
    # 5760 counts == 90 km/h == 55.923 mph
    assert signals.decode_speed_mph(bytes([0x16, 0x80])) == pytest.approx(55.923, abs=1e-2)


def test_decode_speed_short_frame():
    assert signals.decode_speed_kmh(b"\x01") is None
    assert signals.decode_speed_mph(b"\x01") is None


def test_uds_soc_request_payload():
    # ISO-TP single frame: 03 22 00 5B then 0x55 padding.
    assert signals.uds_soc_request_payload() == bytes.fromhex("0322005B55555555")


@pytest.mark.parametrize(
    "raw, pct",
    [(0, 0.0), (255, 100.0), (0x54, 32.94), (0xE5, 89.80)],
)
def test_uds_soc_percent(raw, pct):
    assert signals.uds_soc_percent(raw) == pytest.approx(pct, abs=1e-2)


def test_decode_uds_soc_positive():
    # 7E8#04 62 005B <raw> ...
    kind, raw = signals.decode_uds_soc(bytes([0x04, 0x62, 0x00, 0x5B, 0x54, 0, 0]))
    assert kind == "ok"
    assert raw == 0x54


def test_decode_uds_soc_negative_response():
    # 7E8#03 7F 22 <nrc> ...
    kind, raw = signals.decode_uds_soc(bytes([0x03, 0x7F, 0x22, 0x31, 0, 0, 0]))
    assert kind == "nrc"
    assert raw is None


def test_decode_uds_soc_other_and_short():
    # right service, wrong DID -> not ours
    assert signals.decode_uds_soc(bytes([0x04, 0x62, 0x00, 0x60, 0x11])) == ("other", None)
    # too short to carry the charge byte
    assert signals.decode_uds_soc(bytes([0x04, 0x62, 0x00, 0x5B])) == ("other", None)


@pytest.mark.parametrize(
    "data, expected",
    [
        (bytes([0x00, 0xF0, 0x0A, 0x09, 0, 0, 0, 0]), 9),   # mux hit
        (bytes([0x00, 0xF0, 0x0A, 0x0D, 0, 0, 0, 0]), 13),
        (bytes([0x00, 0xF0, 0x0B, 0x09, 0, 0, 0, 0]), None),  # wrong mux byte 2
        (bytes([0x00, 0xE0, 0x0A, 0x09, 0, 0, 0, 0]), None),  # wrong mux byte 1
        (bytes([0x00, 0xF0, 0x0A]), None),                    # short
    ],
)
def test_decode_soc_bar_raw(data, expected):
    assert signals.decode_soc_bar_raw(data) == expected


@pytest.mark.parametrize(
    "byte3, expected",
    [
        (0x01, ShiftPosition.PARK),
        (0x02, ShiftPosition.REVERSE),
        (0x03, ShiftPosition.NEUTRAL),
        (0x04, ShiftPosition.DRIVE),
        (0x05, ShiftPosition.LOW),
    ],
)
def test_decode_shift_prndl(byte3, expected):
    # 0x1F5 layout: <cksum> <cksum> 00 <prndl> 00 00 08 00
    assert signals.decode_shift(bytes([0x0F, 0x0D, 0x00, byte3, 0, 0, 8, 0])) is expected


def test_decode_shift_unknown_and_short():
    assert signals.decode_shift(bytes([0, 0, 0, 0x00])) is ShiftPosition.UNKNOWN
    assert signals.decode_shift(bytes([0, 0, 0, 0x07])) is ShiftPosition.UNKNOWN
    assert signals.decode_shift(b"\x00\x00\x00") is ShiftPosition.UNKNOWN


@pytest.mark.parametrize(
    "byte1, expected",
    [
        (0x00, DriveMode.NORMAL),
        (0x80, DriveMode.SPORT),
        (0x20, DriveMode.MOUNTAIN),
        (0x08, DriveMode.HOLD),
    ],
)
def test_decode_drive_mode(byte1, expected):
    # 0x1F4 layout: 00 <mode> 00 00 <btn_ramp> <btn_down>
    assert signals.decode_drive_mode(bytes([0x00, byte1, 0, 0, 0, 0x80])) is expected


def test_decode_drive_mode_unknown_and_short():
    assert signals.decode_drive_mode(bytes([0x00, 0x40, 0, 0, 0, 0])) is None
    assert signals.decode_drive_mode(b"\x00") is None


# --- menu cursor (0x1F4 bytes 4+5) -------------------------------------
#
# Ground truth: a 2432-frame 0x1F4 capture across a full menu walk,
# 2026-09-03. The cursor is one field spanning bytes 4 AND 5. Every one of
# the 403 frames with byte-5 bit 7 set carried byte 4 == 0x00.
def _f4(byte4, byte5, mode=0x00):
    """A 6-byte 0x1F4 frame: 00 <mode> 00 00 <b4> <b5>."""
    return bytes([0x00, mode, 0, 0, byte4, byte5])


@pytest.mark.parametrize(
    "byte4,byte5,expected",
    [
        (0x00, 0x80, DriveMode.NORMAL),    # NORMAL lives in byte 5
        (0x80, 0x00, DriveMode.SPORT),
        (0x40, 0x00, DriveMode.MOUNTAIN),
        (0x20, 0x00, DriveMode.HOLD),
        (0x00, 0x00, None),                # menu CLOSED -- not NORMAL
    ],
)
def test_decode_menu_cursor(byte4, byte5, expected):
    assert signals.decode_menu_cursor(_f4(byte4, byte5)) is expected


def test_decode_menu_cursor_closed_is_distinct_from_normal():
    """The distinction the byte-4-only decode could not make.

    Reading byte 4 alone maps both a closed menu and a NORMAL cursor to 0x00,
    which is what made every previous walk fix wrong in one direction or the
    other -- gate on bit 7 and the cursor is always NORMAL; ungate it and a
    closed menu reads as NORMAL.
    """
    assert signals.decode_menu_cursor(_f4(0x00, 0x00)) is None
    assert signals.decode_menu_cursor(_f4(0x00, 0x80)) is DriveMode.NORMAL


def test_decode_menu_cursor_unknown_and_short():
    assert signals.decode_menu_cursor(_f4(0x11, 0x00)) is None   # unmapped code
    assert signals.decode_menu_cursor(bytes([0x00, 0x08, 0, 0, 0x40])) is None  # no byte 5
    assert signals.decode_menu_cursor(b"\x00") is None


def test_menu_is_open_tracks_the_cursor():
    assert signals.menu_is_open(_f4(0x00, 0x80)) is True    # cursor on NORMAL
    assert signals.menu_is_open(_f4(0x20, 0x00)) is True    # cursor on HOLD
    assert signals.menu_is_open(_f4(0x00, 0x00)) is False   # closed


def test_menu_cursor_is_independent_of_the_committed_mode():
    """byte 1 lags the commit ~3 s; the cursor must not be read from it."""
    # committed HOLD (byte1 0x08) while the cursor already sits on SPORT
    assert signals.decode_menu_cursor(_f4(0x80, 0x00, mode=0x08)) is DriveMode.SPORT
    assert signals.decode_drive_mode(_f4(0x80, 0x00, mode=0x08)) is DriveMode.HOLD


def test_mode_cycle_order():
    assert signals.MODE_CYCLE_ORDER == (
        DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN, DriveMode.HOLD,
    )


def test_is_signal_frame():
    assert signals.is_signal_frame(0x096)   # coarse passive SOC proxy
    assert signals.is_signal_frame(0x7E8)   # UDS SOC poll reply (low)
    assert signals.is_signal_frame(0x7EF)   # UDS SOC poll reply (high)
    assert signals.is_signal_frame(0x3E9)
    assert signals.is_signal_frame(0x135)
    assert signals.is_signal_frame(0x1F5)
    assert signals.is_signal_frame(0x1F4)  # drive-mode status, ~40 Hz
    assert not signals.is_signal_frame(0x1E1)
    assert not signals.is_signal_frame(0x206)  # not on this bus
    assert not signals.is_signal_frame(0x7E0)  # request id, not a reply


def test_confirmed_signal_set():
    # Phase C, 2026-08-29 (session 4, drive): drive-mode status (0x1F4 b1),
    # the button injection (0x1E1), and PRNDL (0x1F5 b3) are all confirmed
    # on-vehicle. Speed still needs a longer discharge drive; SOC is a UDS
    # poll now, not a SIGNAL_IDS entry.
    confirmed = {name for name, s in signals.SIGNAL_IDS.items() if s.confirmed}
    assert confirmed == {"drive_mode_status", "drive_mode_button", "shift"}
    assert signals.SIGNAL_IDS["drive_mode_status"].addr == 0x1F4
    assert signals.SIGNAL_IDS["drive_mode_button"].addr == 0x1E1
    assert signals.SIGNAL_IDS["shift"].addr == 0x1F5
    assert "soc" not in signals.SIGNAL_IDS

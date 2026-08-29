import pytest

from voltdmf import signals
from voltdmf.signals import DriveMode, ShiftPosition


@pytest.mark.parametrize(
    "data, expected",
    [
        (bytes([0x13, 0x88]), 50.0),          # 5000 / 100
        (bytes([0x00, 0x00]), 0.0),
        (bytes([0xFF, 0xFF, 0x00]), 655.35),  # extra bytes ignored
    ],
)
def test_decode_speed_mph(data, expected):
    assert signals.decode_speed_mph(data) == pytest.approx(expected)


def test_decode_speed_mph_short_frame():
    assert signals.decode_speed_mph(b"\x01") is None


@pytest.mark.parametrize(
    "data, expected_raw",
    [
        (bytes([0x00, 0x27, 0x10]), 0x2710),        # bytes 1-2 big-endian
        (bytes([0xAA, 0x00, 0x00, 0xFF]), 0x0000),   # byte 0 is not part of it
        (bytes([0x00, 0x12, 0x34, 0x56]), 0x1234),
    ],
)
def test_decode_soc_raw(data, expected_raw):
    assert signals.decode_soc_raw(data) == expected_raw


def test_decode_soc_raw_short_frame():
    assert signals.decode_soc_raw(b"\x00\x01") is None


def test_soc_percent_is_clamped():
    assert signals.soc_percent_from_raw(0) == 0.0
    assert signals.soc_percent_from_raw(10**9) == 100.0
    mid = signals.soc_percent_from_raw(20)
    assert 0.0 <= mid <= 100.0


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


def test_mode_cycle_order():
    assert signals.MODE_CYCLE_ORDER == (
        DriveMode.NORMAL, DriveMode.SPORT, DriveMode.MOUNTAIN, DriveMode.HOLD,
    )


def test_is_signal_frame():
    assert signals.is_signal_frame(0x206)
    assert signals.is_signal_frame(0x3E9)
    assert signals.is_signal_frame(0x135)
    assert signals.is_signal_frame(0x1F5)
    assert not signals.is_signal_frame(0x1E1)


def test_confirmed_signal_set():
    # Phase C, 2026-08-29 (session 4, drive): drive-mode status (0x1F4 b1),
    # the button injection (0x1E1), and PRNDL (0x1F5 b3) are all confirmed
    # on-vehicle. SOC and speed still need a longer discharge drive.
    confirmed = {name for name, s in signals.SIGNAL_IDS.items() if s.confirmed}
    assert confirmed == {"drive_mode_status", "drive_mode_button", "shift"}
    assert signals.SIGNAL_IDS["drive_mode_status"].addr == 0x1F4
    assert signals.SIGNAL_IDS["drive_mode_button"].addr == 0x1E1
    assert signals.SIGNAL_IDS["shift"].addr == 0x1F5
    assert not signals.SIGNAL_IDS["soc"].confirmed

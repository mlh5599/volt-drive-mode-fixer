"""RX-reader tests: the 0x1F4 status/cursor reads must return the NEWEST
queued frame, not the oldest.

Field session 3 (2026-08-29): the closed-loop walk committed MOUNTAIN when
asked for SPORT because ``read_menu_cursor`` returned a stale frame from the
RX backlog -- ``recv()`` hands back the oldest queued frame first, and the
socket buffers 0x1F4 at ~40 Hz while ``send_mode_button_press`` runs.
"""

from voltdmf.canio import MODE_STATUS_ADDR, CanInterface
from voltdmf.signals import DriveMode


class _Frame:
    def __init__(self, arbitration_id, data):
        self.arbitration_id = arbitration_id
        self.data = data


class _FakeBus:
    """recv() pops the oldest frame, then returns None (queue drained)."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.recv_calls = 0

    def recv(self, timeout=0.0):
        self.recv_calls += 1
        return self._frames.pop(0) if self._frames else None


def _cursor_frame(byte4):
    return _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0x00, 0x00, byte4, 0x00)))


def _iface_with(frames):
    iface = CanInterface.__new__(CanInterface)
    iface._bus = _FakeBus(frames)
    return iface


def test_read_menu_cursor_returns_newest_not_oldest():
    # backlog: cursor stepped NORMAL -> SPORT -> MOUNTAIN; newest is MOUNTAIN
    iface = _iface_with([
        _cursor_frame(0x00),   # NORMAL  (stale)
        _cursor_frame(0x80),   # SPORT   (stale -- this is what the old code returned)
        _cursor_frame(0x40),   # MOUNTAIN (current)
    ])
    assert iface.read_menu_cursor(timeout=0.1) is DriveMode.MOUNTAIN


def test_read_menu_cursor_drains_the_whole_backlog():
    bus = _FakeBus([_cursor_frame(0x00), _cursor_frame(0x80), _cursor_frame(0x20)])
    iface = CanInterface.__new__(CanInterface)
    iface._bus = bus
    assert iface.read_menu_cursor(timeout=0.1) is DriveMode.HOLD
    # 3 frames + 1 None to learn the queue is empty
    assert bus.recv_calls == 4


def test_read_menu_cursor_ignores_other_ids():
    iface = _iface_with([
        _Frame(0x1E1, bytes(7)),
        _cursor_frame(0x80),
        _Frame(0x206, bytes(8)),
    ])
    assert iface.read_menu_cursor(timeout=0.1) is DriveMode.SPORT


def test_read_menu_cursor_none_when_no_status_frame():
    iface = _iface_with([_Frame(0x1E1, bytes(7)), _Frame(0x206, bytes(8))])
    assert iface.read_menu_cursor(timeout=0.05) is None


def test_read_drive_mode_returns_newest_committed_byte1():
    # byte 1 codes: 00 N / 80 S / 20 M / 08 H
    iface = _iface_with([
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0, 0))),
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x80, 0, 0, 0, 0))),
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x20, 0, 0, 0, 0))),
    ])
    assert iface.read_drive_mode(timeout=0.1) is DriveMode.MOUNTAIN

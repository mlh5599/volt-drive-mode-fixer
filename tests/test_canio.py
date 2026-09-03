"""RX-reader tests: the 0x1F4 status/cursor reads must return the NEWEST
queued frame, not the oldest.

Field session 3 (2026-08-29): the closed-loop walk committed MOUNTAIN when
asked for SPORT because ``read_menu_cursor`` returned a stale frame from the
RX backlog -- ``recv()`` hands back the oldest queued frame first, and the
socket buffers 0x1F4 at ~40 Hz while ``send_mode_button_press`` runs.
"""

from voltdmf import canio as canio_module
from voltdmf import signals
from voltdmf.canio import MODE_STATUS_ADDR, CanInterface, _DecodeListener
from voltdmf.signals import DriveMode, ShiftPosition
from voltdmf.state import VehicleState


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

    def shutdown(self):
        self.shut_down = True


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


# -- _DecodeListener: 0x1F4 byte 1 feeds VehicleState.drive_mode ---------
def test_decode_listener_populates_drive_mode_from_1f4():
    state = VehicleState()
    listener = _DecodeListener(state)
    assert state.drive_mode is None

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x20, 0, 0, 0, 0))))
    assert state.drive_mode is DriveMode.MOUNTAIN

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x80, 0, 0, 0, 0))))
    assert state.drive_mode is DriveMode.SPORT


def test_decode_listener_leaves_drive_mode_alone_on_unknown_byte1():
    state = VehicleState()
    state.drive_mode = DriveMode.HOLD
    listener = _DecodeListener(state)

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0xFF, 0, 0, 0, 0))))
    assert state.drive_mode is DriveMode.HOLD  # undecodable -> unchanged


# -- _DecodeListener: 0x1F4 bytes 4+5 feed VehicleState.menu_cursor -----
#
# The cursor is ONE field spanning bytes 4 and 5, confirmed 2026-09-03 from a
# 2432-frame 0x1F4 capture across a full walk:
#     b4=0x00 b5=0x80  NORMAL      b4=0x40 b5=0x00  MOUNTAIN
#     b4=0x80 b5=0x00  SPORT       b4=0x20 b5=0x00  HOLD
#     b4=0x00 b5=0x00  menu CLOSED (no cursor)
# All 403 frames with b5 bit 7 set carried b4 == 0x00 -- zero exceptions -- so
# bit 7 is the NORMAL cursor code, not a separate "menu open" flag, and a
# frame with b4 != 0 AND bit 7 set does not occur on the wire.
_NORM = 0x80   # 0x1F4 byte 5: cursor is on NORMAL


def test_decode_listener_populates_menu_cursor_from_bytes_4_and_5():
    state = VehicleState()
    listener = _DecodeListener(state)
    assert state.menu_cursor is None

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x40, 0x00, 0, 0))))
    assert state.menu_cursor is DriveMode.MOUNTAIN

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x20, 0x00, 0, 0))))
    assert state.menu_cursor is DriveMode.HOLD

    # NORMAL is carried by byte 5, not byte 4
    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x00, _NORM, 0, 0))))
    assert state.menu_cursor is DriveMode.NORMAL


def test_decode_listener_clears_cursor_when_the_menu_closes():
    """b4=0 b5=0 is menu-CLOSED and must null the cursor.

    Latching the last reading instead would let a stale value match the walk's
    target and stop it early -- and it is exactly what made a closed-loop walk
    read a cursor that was no longer on screen (session 10).
    """
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x80, 0x00, 0, 0))))
    assert state.menu_cursor is DriveMode.SPORT

    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x08, 0, 0, 0x00, 0x00, 0, 0))))
    assert state.menu_cursor is None                # menu closed
    assert state.drive_mode is DriveMode.HOLD       # byte 1 still decoded


def test_decode_listener_records_raw_cursor_and_open_hint_for_diagnostics():
    state = VehicleState()
    listener = _DecodeListener(state)

    # an unmapped byte-4 code: decoded cursor stays None, raw is still captured
    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x11, 0x00, 0, 0))))
    assert state.menu_cursor is None
    assert state.menu_cursor_raw == 0x11
    assert state.menu_open_hint is False

    # a real open frame -> hint True and the cursor decodes
    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x40, 0x00, 0, 0))))
    assert state.menu_cursor_raw == 0x40
    assert state.menu_open_hint is True


def test_decode_listener_ignores_short_frame_without_byte5():
    """A frame too short to carry byte 5 cannot resolve a cursor at all."""
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(
        _Frame(MODE_STATUS_ADDR, bytes((0x00, 0x00, 0, 0, 0x40, 0x00, 0, 0))))
    assert state.menu_cursor is DriveMode.MOUNTAIN

    listener.on_message_received(_Frame(MODE_STATUS_ADDR, bytes((0x00, 0x08))))
    assert state.menu_cursor is None                # cannot decode -> no cursor
    assert state.drive_mode is DriveMode.HOLD


# -- the live 0x1E1 the tracking-echo press mirrors ---------------------
#
# In the daemon a can.Notifier owns the socket, so the press path must take
# the module's live 0x1E1 from the listener instead of racing the Notifier
# with its own recv(). That race cost ~21% dropped taps on-vehicle
# (2026-09-03) against 0/22 with a single reader.
def test_decode_listener_stashes_the_live_button_frame():
    state = VehicleState()
    listener = _DecodeListener(state)
    assert state.button_frame is None

    payload = bytes((0x00, 0x11, 0x22, 0x33, 0x05, 0x66, 0x77))
    listener.on_message_received(_Frame(0x1E1, payload))
    frame, stamp = state.button_frame
    assert frame == payload
    assert stamp > 0

    # a newer frame replaces it, with a fresh timestamp
    newer = bytes((0x00, 0x11, 0x22, 0x33, 0x04, 0x66, 0x77))
    listener.on_message_received(_Frame(0x1E1, newer))
    assert state.button_frame[0] == newer
    assert state.button_frame[1] >= stamp


def test_decode_listener_ignores_a_short_button_frame():
    """< 7 B cannot carry the counter tail the echo has to mirror."""
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(_Frame(0x1E1, bytes(4)))
    assert state.button_frame is None


def _iface_reading_state(state):
    iface = CanInterface.__new__(CanInterface)
    iface._bus = None            # must NOT be touched on the state path
    iface._rx_state = state
    iface._last_button_stamp = 0.0
    return iface


def test_next_button_frame_prefers_the_listener_over_the_socket():
    state = VehicleState()
    payload = bytes((0x00, 0x11, 0x22, 0x33, 0x05, 0x66, 0x77))
    state.button_frame = (payload, 100.0)
    iface = _iface_reading_state(state)
    # _bus is None: reaching for the socket here would raise
    assert iface._next_button_frame(0.05) == payload


def test_next_button_frame_will_not_reuse_the_same_frame():
    """Each press frame must mirror a NEW module frame, not the last one.

    Re-sending the same counter is the "static frame with a frozen counter"
    the cluster was seen to ignore in session 2.
    """
    state = VehicleState()
    first = bytes((0x00, 0x11, 0x22, 0x33, 0x05, 0x66, 0x77))
    state.button_frame = (first, 100.0)
    iface = _iface_reading_state(state)
    assert iface._next_button_frame(0.05) == first
    # nothing new arrived -> times out rather than echoing `first` again
    assert iface._next_button_frame(0.02) is None
    # a genuinely newer frame is taken
    second = bytes((0x00, 0x11, 0x22, 0x33, 0x04, 0x66, 0x77))
    state.button_frame = (second, 101.0)
    assert iface._next_button_frame(0.05) == second


def test_next_button_frame_times_out_when_the_bus_is_quiet():
    iface = _iface_reading_state(VehicleState())   # never any frame
    assert iface._next_button_frame(0.02) is None


def test_start_rx_switches_the_press_path_to_the_state():
    """Wiring check: start_rx must arm the state source, close must clear it."""
    state = VehicleState()
    iface = CanInterface.__new__(CanInterface)
    iface._bus = _FakeBus([])
    iface._notifier = None
    iface._rx_state = None
    iface._last_button_stamp = 99.0

    captured = {}

    class _FakeNotifier:
        def __init__(self, bus, listeners):
            captured["listeners"] = listeners

        def stop(self):
            captured["stopped"] = True

    real_notifier = canio_module.can.Notifier
    canio_module.can.Notifier = _FakeNotifier
    try:
        iface.start_rx(state)
        assert iface._rx_state is state
        assert iface._last_button_stamp == 0.0   # reset, or the first tap stalls
        iface.close()
        assert iface._rx_state is None
    finally:
        canio_module.can.Notifier = real_notifier


def test_decode_listener_ignores_non_signal_frames():
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(_Frame(0x1E1, bytes(7)))
    listener.on_message_received(_Frame(0x206, bytes(8)))  # not on this bus
    assert state.drive_mode is None
    assert state.shift is ShiftPosition.UNKNOWN
    assert state.soc_percent is None


# -- _DecodeListener: SOC poll reply (0x7E8..0x7EF) --------------------
def test_decode_listener_folds_in_a_uds_soc_reply():
    state = VehicleState()
    listener = _DecodeListener(state)
    # 7E8#04 62 005B 54 ...  -> raw 0x54 == 32.9 %
    listener.on_message_received(
        _Frame(0x7E8, bytes((0x04, 0x62, 0x00, 0x5B, 0x54, 0, 0, 0))))
    assert state.soc_raw == 0x54
    assert state.soc_percent == signals.uds_soc_percent(0x54)
    assert state.soc_source == "poll"
    assert state.uds_resp_id == 0x7E8
    assert state.uds_replies == 1
    assert state.soc_percent_monotonic is not None


def test_decode_listener_counts_a_negative_response():
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(
        _Frame(0x7E8, bytes((0x03, 0x7F, 0x22, 0x31, 0, 0, 0, 0))))
    assert state.uds_nrcs == 1
    assert state.soc_percent is None


# -- _DecodeListener: 0x096 byte 3 coarse proxy ---------------------
def test_decode_listener_reads_the_soc_bar_proxy():
    state = VehicleState()
    listener = _DecodeListener(state)
    listener.on_message_received(
        _Frame(0x096, bytes((0x00, 0xF0, 0x0A, 0x09, 0, 0, 0, 0))))
    assert state.soc_bar_raw == 9
    # a non-mux 0x096 frame must not clobber it
    listener.on_message_received(
        _Frame(0x096, bytes((0x00, 0x00, 0x00, 0x00, 0, 0, 0, 0))))
    assert state.soc_bar_raw == 9


# -- send_soc_poll: the second (ungated) transmit path -----------------
class _SendBus:
    def __init__(self):
        self.sent = []

    def send(self, msg, timeout=None):
        self.sent.append(msg)


def test_send_soc_poll_emits_the_fixed_request_frame():
    iface = CanInterface.__new__(CanInterface)
    iface._bus = bus = _SendBus()
    iface._tx_gate = lambda: True
    iface.send_soc_poll(0x7E0)
    assert len(bus.sent) == 1
    msg = bus.sent[0]
    assert msg.arbitration_id == 0x7E0
    assert bytes(msg.data) == bytes.fromhex("0322005B55555555")
    assert msg.is_extended_id is False


def test_send_soc_poll_runs_even_while_disarmed():
    iface = CanInterface.__new__(CanInterface)
    iface._bus = bus = _SendBus()
    iface._tx_gate = lambda: False  # disarmed
    iface.send_soc_poll(0x7E4)
    assert len(bus.sent) == 1
    assert bus.sent[0].arbitration_id == 0x7E4

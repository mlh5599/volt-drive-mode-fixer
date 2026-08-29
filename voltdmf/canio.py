"""SocketCAN I/O: the RX decode loop and the *single* transmit path.

There is exactly one function in this whole project that puts a frame on the
bus -- :meth:`CanInterface.send_mode_button_press`. It hard-codes the one
address and payload the project needs. There is no "send arbitrary frame"
path anywhere (DESIGN.md "Safety model").
"""

from __future__ import annotations

import logging

import can

from . import signals
from .signals import SIGNAL_IDS
from .state import VehicleState

log = logging.getLogger(__name__)

# --- The one message this project is allowed to transmit -------------------
#
# UNCONFIRMED: these are the Gen 2 (2017) values from vix597/chevy-volt-trip-mode
# (MSG_ID 0x1E1, payload 00 00 00 00 80 00 00 -- the 0x80 is bit 39). Phase C
# (tools/mode_diff.py) confirms or replaces them for this Gen 1 car.
MODE_BUTTON_ADDR_UNCONFIRMED = 0x1E1
MODE_BUTTON_PAYLOAD_UNCONFIRMED = bytes.fromhex("00000000800000")

#: One logical "press" = this many identical frames back to back (reference
#: project's SEND_CLUSTER_SIZE). Also UNCONFIRMED for Gen 1.
SEND_CLUSTER_SIZE = 50


class CanInterface:
    def __init__(self, channel: str = "can0", *, dry_run: bool = False) -> None:
        self._channel = channel
        self._dry_run = dry_run
        self._bus: can.BusABC | None = None
        self._notifier: can.Notifier | None = None
        self._tx_frame = can.Message(
            arbitration_id=MODE_BUTTON_ADDR_UNCONFIRMED,
            data=MODE_BUTTON_PAYLOAD_UNCONFIRMED,
            is_extended_id=False,
        )

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        self._bus = can.Bus(interface="socketcan", channel=self._channel)
        log.info("opened %s (dry_run=%s)", self._channel, self._dry_run)

    def start_rx(self, state: VehicleState) -> None:
        if self._bus is None:
            raise RuntimeError("open() first")
        self._notifier = can.Notifier(self._bus, [_DecodeListener(state)])

    def close(self) -> None:
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None

    def __enter__(self) -> "CanInterface":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the only transmit path ----------------------------------------
    def send_mode_button_press(self) -> None:
        """Emit one logical button press (a burst of identical frames)."""
        if self._dry_run:
            log.info("[dry-run] would send %d x %s#%s", SEND_CLUSTER_SIZE,
                     hex(self._tx_frame.arbitration_id),
                     self._tx_frame.data.hex())
            return
        if self._bus is None:
            raise RuntimeError("open() first")
        for _ in range(SEND_CLUSTER_SIZE):
            self._bus.send(self._tx_frame)


class _DecodeListener(can.Listener):
    """Decodes the handful of known frames into the shared VehicleState."""

    def __init__(self, state: VehicleState) -> None:
        self._state = state
        self._soc_addr = SIGNAL_IDS["soc"].addr
        self._speed_addr = SIGNAL_IDS["speed"].addr

    def on_message_received(self, msg: can.Message) -> None:
        addr = msg.arbitration_id
        if not signals.is_signal_frame(addr):
            return
        data = bytes(msg.data)
        if addr == self._soc_addr:
            raw = signals.decode_soc_raw(data)
            if raw is not None:
                self._state.soc_raw = raw
                self._state.soc_percent = signals.soc_percent_from_raw(raw)
        elif addr == self._speed_addr:
            mph = signals.decode_speed_mph(data)
            if mph is not None:
                self._state.speed_mph = mph
        else:  # a shift-position frame (0x135 / 0x1F5)
            self._state.shift = signals.decode_shift(data)
        self._state.mark_signal_seen()

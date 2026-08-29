"""SocketCAN I/O: the RX decode loop and the *single* transmit path.

There is exactly one function in this whole project that puts a frame on the
bus -- :meth:`CanInterface.send_mode_button_press`. It hard-codes the one
address and payload the project needs. There is no "send arbitrary frame"
path anywhere (DESIGN.md "Safety model").
"""

from __future__ import annotations

import logging
import time

import can

from . import signals
from .signals import SIGNAL_IDS, DriveMode
from .state import VehicleState

log = logging.getLogger(__name__)

# --- The one message this project is allowed to transmit -------------------
#
# Confirmed 2026-08-29 on-vehicle: on the Gen 1 HS-CAN (500k) the drive-mode
# button and the current-mode status share arbitration ID 0x1F4. The body
# module streams `00 <mode> 00 00 00 00` at ~40 Hz (byte 1 = latched mode). A
# physical tap drives byte 5 -> 0x80 ("button down") for ~0.3 s, byte 4 then
# ramps 80->40->20 while held, and the mode latches ~2-3 s after release.
#
# On-vehicle findings 2026-08-29:
#  * The press is DURATION-gated: ~0.3 s of byte5=0x80 only wakes the drive-mode
#    screen; ~1.2 s registered as ~3 presses (auto-repeat). One solid block of
#    ~0.45 s = one press.
#  * The byte4 decay ramp the real module emits on release (0x80->0x40->0x20)
#    AUTO-REPEATS when injected -- each value counts as another press -- so we
#    do NOT reproduce it. PRESS_RAMP_VALUES is kept (empty) as a tuning hook.
#  * TX is hard on this controller (RX is clean; suspect a marginal CAN-H/L
#    solder joint) -- keep the frame count low and pair with `restart-ms`.
# We stuff frames at PRESS_FRAME_INTERVAL_S (above the module's native ~40 Hz)
# so the consumer sees a continuous press. Byte 1 mirrors the live mode.
#
# INJECTION EFFICACY IS NOT YET CONFIRMED -- tools/inject_test.py (Phase C.5)
# is the gate. Until it passes, the daemon must stay in --dry-run.
MODE_BUTTON_ADDR = 0x1F4
PRESS_FRAME_INTERVAL_S = 0.010   # 100 Hz
PRESS_DOWN_FRAMES = 45           # 0.45 s of byte5 = 0x80 -> one press
PRESS_RAMP_VALUES: tuple[int, ...] = ()   # byte4 ramp disabled (auto-repeats)
PRESS_RAMP_FRAMES = 14
PRESS_IDLE_FRAMES = 0

#: Frames per logical press (for callers/tests).
SEND_CLUSTER_SIZE = (
    PRESS_DOWN_FRAMES + PRESS_RAMP_FRAMES * len(PRESS_RAMP_VALUES) + PRESS_IDLE_FRAMES
)

_MODE_TO_BYTE1 = {v: k for k, v in signals._DRIVE_MODE_BY_BYTE1.items()}


class CanInterface:
    def __init__(self, channel: str = "can0", *, dry_run: bool = False) -> None:
        self._channel = channel
        self._dry_run = dry_run
        self._bus: can.BusABC | None = None
        self._notifier: can.Notifier | None = None

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

    # -- observation helper (used by the injection test) -----------------
    def read_drive_mode(self, timeout: float = 1.0) -> DriveMode | None:
        """Return the current drive mode from the next decodable 0x1F4 frame."""
        if self._bus is None:
            raise RuntimeError("open() first")
        end = time.time() + timeout
        while time.time() < end:
            msg = self._bus.recv(timeout=max(0.0, end - time.time()))
            if msg is not None and msg.arbitration_id == MODE_BUTTON_ADDR:
                mode = signals.decode_drive_mode(bytes(msg.data))
                if mode is not None:
                    return mode
        return None

    # -- the only transmit path ----------------------------------------
    def _frame(self, mode_byte1: int, byte4: int, byte5: int) -> can.Message:
        return can.Message(
            arbitration_id=MODE_BUTTON_ADDR,
            data=bytes((0x00, mode_byte1, 0x00, 0x00, byte4, byte5)),
            is_extended_id=False,
        )

    def _stuff(self, frame: can.Message, count: int) -> None:
        for _ in range(count):
            self._bus.send(frame)
            time.sleep(PRESS_FRAME_INTERVAL_S)

    def send_mode_button_press(self) -> None:
        """Inject one logical button press on 0x1F4.

        One solid byte5=0x80 block (~0.45 s, PRESS_DOWN_FRAMES @
        PRESS_FRAME_INTERVAL_S), no byte4 ramp -- the press is duration-gated
        and the ramp auto-repeats (see the module comment). Byte 1 mirrors the
        mode currently on the bus so the injected frames never disagree with
        the real sender. The car commits the new mode ~2-3 s after this returns.
        """
        current = None if self._dry_run else self.read_drive_mode(timeout=0.5)
        mode_byte1 = _MODE_TO_BYTE1.get(current, 0x00)
        if self._dry_run:
            ramp = ("/".join(f"0x{v:02x}" for v in PRESS_RAMP_VALUES)
                    + f" x{PRESS_RAMP_FRAMES} each" if PRESS_RAMP_VALUES else "none")
            log.info("[dry-run] would inject 0x1F4 press: byte5=0x80 x%d, "
                     "byte4 ramp %s, trailing idle x%d @ %.0f Hz (byte1=0x%02x)",
                     PRESS_DOWN_FRAMES, ramp, PRESS_IDLE_FRAMES,
                     1.0 / PRESS_FRAME_INTERVAL_S, mode_byte1)
            return
        if self._bus is None:
            raise RuntimeError("open() first")
        if current is None:
            log.warning("no 0x1F4 seen before inject; using byte1=0x00 (NORMAL)")
        self._stuff(self._frame(mode_byte1, 0x00, 0x80), PRESS_DOWN_FRAMES)
        for ramp in PRESS_RAMP_VALUES:
            self._stuff(self._frame(mode_byte1, ramp, 0x00), PRESS_RAMP_FRAMES)
        self._stuff(self._frame(mode_byte1, 0x00, 0x00), PRESS_IDLE_FRAMES)


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

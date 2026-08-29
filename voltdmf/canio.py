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
# physical tap drives byte 5 -> 0x80 ("button down"), byte 4 then ramps
# 80->40->20 while held, and the mode latches ~2-3 s after release.
#
# Injection model: BURST-AND-RELEASE (mirrors vix597/chevy-volt-trip-mode).
# One logical press = a short burst of byte5=0x80 frames, then we STOP
# transmitting -- the body module's own ~40 Hz idle 0x1F4 frames are the
# "button up". We never send a release frame and never reproduce the byte 4
# ramp (on-vehicle, an injected ramp AUTO-REPEATS -- each value lands as
# another press). The caller must leave >= RELEASE_GAP_S of silence before
# the next press; that gap both separates one counted press from the next and
# gives the MCP2515's transmit-error counter room to recover (TX is hard on
# this controller while RX stays clean -- suspect a marginal CAN-H/L joint).
# Byte 1 mirrors the live bus so our frames never disagree on the mode.
#
# On-vehicle findings 2026-08-29: ~0.3 s burst only woke the drive-mode
# screen; ~1.2 s continuous (with the ramp) counted as ~3 presses. Burst
# length still needs a sweep (tools/inject_test.py --burst-ms); the
# burst-then-release *structure* is the fix for the multi-step overshoot.
#
# INJECTION EFFICACY IS NOT YET CONFIRMED -- tools/inject_test.py (Phase C.5)
# is the gate. Until it passes, the daemon must stay in --dry-run.
MODE_BUTTON_ADDR = 0x1F4
PRESS_FRAME_INTERVAL_S = 0.010   # 100 Hz within a burst
PRESS_BURST_FRAMES = 45          # 0.45 s burst of byte5 = 0x80 (sweep start)
RELEASE_GAP_S = 0.75            # min silence after a burst = the "button up"
                                # (reference project's BUTTON_PRESS_COOLDOWN)

#: Frames per logical press (compat alias for callers/tests).
SEND_CLUSTER_SIZE = PRESS_BURST_FRAMES

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
    def _press_frame(self, mode_byte1: int) -> can.Message:
        """The 'button down' frame: byte 5 = 0x80, byte 1 mirrors the mode."""
        return can.Message(
            arbitration_id=MODE_BUTTON_ADDR,
            data=bytes((0x00, mode_byte1, 0x00, 0x00, 0x00, 0x80)),
            is_extended_id=False,
        )

    def send_mode_button_press(self) -> None:
        """Inject ONE logical button press on 0x1F4: burst-and-release.

        Sends a burst of PRESS_BURST_FRAMES 'button down' frames (byte5=0x80)
        at PRESS_FRAME_INTERVAL_S, then returns without any release frame --
        the body module's own ~40 Hz idle 0x1F4 stream is the "button up".
        The caller MUST leave >= RELEASE_GAP_S of silence before the next
        press (ModeCycleController and tools/inject_test.py both do). Byte 1
        mirrors the mode currently on the bus. The car commits the new mode
        ~2-3 s after this returns.
        """
        current = None if self._dry_run else self.read_drive_mode(timeout=0.5)
        mode_byte1 = _MODE_TO_BYTE1.get(current, 0x00)
        burst_s = PRESS_BURST_FRAMES * PRESS_FRAME_INTERVAL_S
        if self._dry_run:
            log.info("[dry-run] would inject 0x1F4 press: byte5=0x80 x%d @ "
                     "%.0f Hz (%.2fs burst), then release (stop TX) for "
                     ">=%.2fs (byte1=0x%02x)",
                     PRESS_BURST_FRAMES, 1.0 / PRESS_FRAME_INTERVAL_S, burst_s,
                     RELEASE_GAP_S, mode_byte1)
            return
        if self._bus is None:
            raise RuntimeError("open() first")
        if current is None:
            log.warning("no 0x1F4 seen before inject; using byte1=0x00 (NORMAL)")
        frame = self._press_frame(mode_byte1)
        for _ in range(PRESS_BURST_FRAMES):
            self._bus.send(frame)
            time.sleep(PRESS_FRAME_INTERVAL_S)
        # No release frame on purpose -- see the docstring / module comment.


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

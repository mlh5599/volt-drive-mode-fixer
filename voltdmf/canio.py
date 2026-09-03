"""SocketCAN I/O: the RX decode loop and the transmit paths.

Exactly two functions in this whole project put a frame on the bus, each
hard-coding its one address and payload:

* :meth:`CanInterface.send_mode_button_press` -- the drive-mode button tap on
  0x1E1. Gated: only transmits while the daemon is armed.
* :meth:`CanInterface.send_soc_poll` -- one ``22 005B`` UDS request. Ungated:
  it is a read, runs armed or disarmed, and every drive wants ground-truth
  SOC.

There is no "send arbitrary frame" path anywhere (DESIGN.md "Safety model").
"""

from __future__ import annotations

import logging
import time
from typing import Callable

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
# Two different IDs:
#   MODE_STATUS_ADDR 0x1F4 -- body module streams `00 <mode> 00 00 <ramp>
#     <btn>` at ~40 Hz; byte 1 = latched mode. READ ONLY (read_drive_mode).
#   MODE_BUTTON_ADDR 0x1E1 -- "ASCMSteeringButton". byte 4 bit 7 = drive-mode
#     button pressed. This is what we TRANSMIT. Confirmed session 2
#     (2026-08-29): during a physical press 0x1E1 byte 4 goes 00/01/02/03 ->
#     80/81/82/83 (low bits are a rolling counter; bit 7 is the press flag).
#     Same ID/bit/payload the Gen 2 prior art (vix597/chevy-volt-trip-mode)
#     injects successfully.
#
# Injection model: TRACKING ECHO PRESS.
# 0x1E1 is NOT a static frame here. byte 4 low 2 bits are a rolling counter,
# bytes 5-6 are a counter-derived tail, and the module streams the frame at
# ~40 Hz. A captured *physical* press (session 2, 2026-08-29) is just ~14
# consecutive frames with byte 4 bit 7 set, the counter still advancing
# normally (..83 82 81 80..), then bit 7 clears -- ~350 ms total.
#
# So we replay exactly that: for PRESS_TRACK_FRAMES iterations, wait for the
# module's next live 0x1E1, OR 0x80 into its byte 4, and send that back into
# the ~24 ms gap before the module's next frame. Every injected frame carries
# the module's current counter + tail (only bit 7 differs), and it lands
# *between* module frames rather than colliding with one -- so the cluster's
# ~40 Hz poll sees a clean run of "pressed" frames with a valid advancing
# counter, then a release edge when we stop. The caller leaves >=
# RELEASE_GAP_S of silence before the next press.
#
# What did NOT work (session 2, on-vehicle): the wrong ID (0x1F4, a status
# echo); time.sleep()-paced 0x1E1 that collided with the module (TEC ->
# ERROR-PASSIVE); a static 0x1E1 frame with a frozen counter (ignored / only
# woke the menu); and a blind back-to-back blast of a frozen echo frame --
# long blasts get punched through by module frames at random points, so the
# cluster saw an unpredictable 0-2 presses per burst.
#
# The cluster also RATE-LIMITS: injected presses ~5 s apart register, presses
# ~2 s apart are all ignored. And this menu takes a wake press first (press 1
# from an idle menu only lights it up). Both are the caller's problem
# (ModeCycleController / tools/inject_test.py), not this function's.
#
# Injection efficacy is on-road-confirmed: the tracking-echo press drove a
# closed-loop walk to all four modes (2026-08-29, see the 0x1E1 note in
# signals.py). tools/inject_test.py remains the bench harness for the TX path.
MODE_STATUS_ADDR = 0x1F4
MODE_BUTTON_ADDR = 0x1E1
PRESS_BYTE4 = 0x80               # 0x1E1 byte 4 bit 7 = button pressed
PRESS_TRACK_FRAMES = 16         # bit-7-set frames per press (~14 in a real one)
_TRACK_FRAME_TIMEOUT_S = 0.1    # give up waiting for the next live 0x1E1
RELEASE_GAP_S = 0.75            # min silence after a press = the "button up"
                                # (reference project's BUTTON_PRESS_COOLDOWN)
_TX_SEND_TIMEOUT_S = 0.1        # let python-can wait for a TX slot instead of
                                # raising "Transmit buffer full" immediately

# Back-compat: the old blast knobs some callers/tests still poke.
PRESS_FRAME_INTERVAL_S = 0.0
PRESS_BURST_FRAMES = PRESS_TRACK_FRAMES

#: Fallback 0x1E1 "button down" payload (no live counter) -- matches
#: vix597/chevy-volt-trip-mode's PRESS_MSG. Used only if no live 0x1E1 can be
#: read at all; the cluster has been seen to ignore this on the Gen 1.
_STATIC_PRESS_PAYLOAD = bytes((0x00, 0x00, 0x00, 0x00, PRESS_BYTE4, 0x00, 0x00))

#: Frames per logical press (compat alias for callers/tests).
SEND_CLUSTER_SIZE = PRESS_TRACK_FRAMES


class CanInterface:
    def __init__(
        self,
        channel: str = "can0",
        *,
        tx_gate: Callable[[], bool] | None = None,
    ) -> None:
        self._channel = channel
        # Runtime transmit enable, checked live on every button press. This is
        # the toggle the control socket's arm/disarm flips -- the project's one
        # and only TX lock now (``--dry-run`` is gone). When it returns False
        # ``send_mode_button_press`` logs the intended press and transmits
        # nothing. ``send_soc_poll`` ignores this -- a poll is a read.
        self._tx_gate = tx_gate
        self._bus: can.BusABC | None = None
        self._notifier: can.Notifier | None = None

    def _tx_suppressed(self) -> bool:
        return self._tx_gate is not None and not self._tx_gate()

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        self._bus = can.Bus(interface="socketcan", channel=self._channel)
        log.info("opened %s", self._channel)

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

    # -- observation helpers (used by the injection test) ---------------
    def _latest_status(self, decode, timeout: float) -> DriveMode | None:
        """Newest decodable 0x1F4 currently in the RX queue.

        ``recv()`` hands back the OLDEST queued frame first, and this socket
        buffers 0x1F4 at ~40 Hz the whole time ``send_mode_button_press()``
        runs (that path only drains 0x1E1). Returning the first 0x1F4 we see
        gives the caller a reading that can be >1 s stale -- enough to desync
        the closed-loop walk: it read "SPORT" while the cursor had already
        stepped to MOUNTAIN (field session 3, 2026-08-29). So block up to
        ``timeout`` for the first hit, then sweep the rest of the queue
        non-blocking and keep only the newest.
        """
        if self._bus is None:
            raise RuntimeError("open() first")
        end = time.time() + timeout
        latest: DriveMode | None = None
        while True:
            wait = 0.0 if latest is not None else max(0.0, end - time.time())
            msg = self._bus.recv(timeout=wait)
            if msg is None:
                if latest is not None or time.time() >= end:
                    return latest
                continue
            if msg.arbitration_id != MODE_STATUS_ADDR:
                continue
            decoded = decode(bytes(msg.data))
            if decoded is not None:
                latest = decoded

    def read_drive_mode(self, timeout: float = 1.0) -> DriveMode | None:
        """Return the *committed* drive mode (0x1F4 byte 1 -- lags a menu
        commit by ~3 s), from the newest frame in the RX queue."""
        return self._latest_status(signals.decode_drive_mode, timeout)

    def read_menu_cursor(self, timeout: float = 0.5) -> DriveMode | None:
        """Return the LIVE drive-mode menu cursor (0x1F4 byte 4 -- steps
        ~40 ms after each button tap; see ``signals.decode_menu_cursor``),
        from the newest frame in the RX queue. Used to close the loop on a
        walk, so it MUST be current -- see :meth:`_latest_status`."""
        return self._latest_status(signals.decode_menu_cursor, timeout)

    # -- the only transmit path ----------------------------------------
    def _next_button_frame(self, timeout: float) -> bytes | None:
        """Block up to ``timeout`` for the module's NEXT live 0x1E1 (>=7 B).

        Unlike _latest_button_frame this does not drain a backlog -- it
        returns on the first 0x1E1 off the wire so the caller can reply into
        the gap right behind it. Same RX-queue caveat: standalone use only.
        """
        if self._bus is None:
            raise RuntimeError("open() first")
        end = time.time() + timeout
        while time.time() < end:
            msg = self._bus.recv(timeout=max(0.0, end - time.time()))
            if (msg is not None and msg.arbitration_id == MODE_BUTTON_ADDR
                    and len(msg.data) >= 7):
                return bytes(msg.data)
        return None

    def send_mode_button_press(self) -> None:
        """Inject ONE logical button press on 0x1E1: a tracking echo press.

        For PRESS_TRACK_FRAMES iterations: wait for the module's next live
        0x1E1, OR 0x80 into its byte 4, and send that straight back into the
        ~24 ms gap before the module's next frame. The result on the wire is
        ~PRESS_TRACK_FRAMES frames with bit 7 set and the counter still
        advancing normally -- a replica of the captured ~14-frame physical
        press -- followed by a release edge when we stop. No collisions with
        the module (we transmit between its frames, not on top of them).

        The caller MUST leave >= RELEASE_GAP_S of silence before the next
        press, must send a wake press first if the menu is idle, and must
        space presses ~5 s apart (the cluster rate-limits) -- see the module
        comment. The car commits the new mode ~2-3 s later.
        """
        if self._tx_suppressed():
            log.info("[disarmed] would inject 0x1E1 press: track module + "
                     "byte4|=0x%02x x%d, then stop TX for >=%.2fs",
                     PRESS_BYTE4, PRESS_TRACK_FRAMES, RELEASE_GAP_S)
            return
        if self._bus is None:
            raise RuntimeError("open() first")
        sent = misses = 0
        for _ in range(PRESS_TRACK_FRAMES):
            base = self._next_button_frame(_TRACK_FRAME_TIMEOUT_S)
            if base is None:
                misses += 1
                if misses > 3:
                    break  # bus went quiet -- stop rather than spin
                continue
            buf = bytearray(base)
            buf[4] |= PRESS_BYTE4
            frame = can.Message(arbitration_id=MODE_BUTTON_ADDR,
                                data=bytes(buf), is_extended_id=False)
            try:
                self._bus.send(frame, timeout=_TX_SEND_TIMEOUT_S)
            except can.CanError as exc:
                log.warning("0x1E1 press: TX backpressure after %d/%d (%s)",
                            sent, PRESS_TRACK_FRAMES, exc)
                time.sleep(0.005)
                continue
            sent += 1
        if sent == 0:
            log.warning("0x1E1 press: no live frame to track -- sending static "
                        "fallback once (cluster may ignore it)")
            try:
                self._bus.send(
                    can.Message(arbitration_id=MODE_BUTTON_ADDR,
                                data=_STATIC_PRESS_PAYLOAD, is_extended_id=False),
                    timeout=_TX_SEND_TIMEOUT_S)
            except can.CanError:
                pass
        else:
            log.debug("0x1E1 press: %d/%d tracked frames on the wire",
                      sent, PRESS_TRACK_FRAMES)
        # No explicit release frame -- the module's own bit-7-clear stream is it.

    def send_soc_poll(self, req_id: int) -> None:
        """Transmit ONE ``22 005B`` UDS request -- the only other frame this
        project puts on the bus.

        A fixed 8-byte ISO-TP single frame (``03 22 00 5B 55 55 55 55``) to
        ``req_id``. The caller cycles 0x7E4 / 0x7E0 until an ECU answers, then
        pins the id that did. Runs regardless of arm state -- a poll is a read,
        and every drive wants ground-truth SOC. Stays in the default
        diagnostic session, service 0x22 only (no session control, no
        TesterPresent), so it never suppresses the normal broadcasts.
        """
        if self._bus is None:
            raise RuntimeError("open() first")
        frame = can.Message(arbitration_id=req_id,
                            data=signals.uds_soc_request_payload(),
                            is_extended_id=False)
        try:
            self._bus.send(frame, timeout=_TX_SEND_TIMEOUT_S)
        except can.CanError as exc:
            log.warning("SOC poll: TX failed for 0x%03X (%s)", req_id, exc)


class _DecodeListener(can.Listener):
    """Decodes the handful of known frames into the shared VehicleState."""

    def __init__(self, state: VehicleState) -> None:
        self._state = state
        self._speed_addr = SIGNAL_IDS["speed"].addr
        self._shift_addr = SIGNAL_IDS["shift"].addr  # 0x1F5 (0x135 not decoded)
        self._status_addr = MODE_STATUS_ADDR  # 0x1F4 byte 1 = committed mode

    def on_message_received(self, msg: can.Message) -> None:
        addr = msg.arbitration_id
        if not signals.is_signal_frame(addr):
            return
        data = bytes(msg.data)
        if addr == signals.SOC_BAR_ADDR:  # 0x096 -- coarse passive SOC proxy
            raw = signals.decode_soc_bar_raw(data)
            if raw is not None:
                self._state.soc_bar_raw = raw
        elif signals.UDS_RESP_ID_LO <= addr <= signals.UDS_RESP_ID_HI:
            self._ingest_uds_soc(addr, data)
        elif addr == self._speed_addr:
            mph = signals.decode_speed_mph(data)
            if mph is not None:
                self._state.speed_mph = mph
        elif addr == self._shift_addr:  # 0x1F5 byte 3 = PRNDL
            self._state.shift = signals.decode_shift(data)
        elif addr == self._status_addr:  # 0x1F4 byte 1 = committed drive mode
            mode = signals.decode_drive_mode(data)
            if mode is not None:
                self._state.drive_mode = mode
            # byte 4 = the LIVE menu cursor (steps ~40 ms after each tap), but
            # 0x00 doubles as "menu idle/closed", so only trust it while byte 5
            # bit 7 says the menu is open. Feeds the reconciler's closed loop.
            self._state.menu_cursor = (
                signals.decode_menu_cursor(data)
                if signals.menu_is_open(data) else None
            )
        # 0x135 is a known signal frame (keeps mark_signal_seen fresh) but its
        # shifter encoding is messier than 0x1F5 -- not decoded.
        self._state.mark_signal_seen()

    def _ingest_uds_soc(self, addr: int, data: bytes) -> None:
        """Fold a 0x7E8..0x7EF frame (our ``22 005B`` poll reply) into state."""
        kind, raw = signals.decode_uds_soc(data)
        if kind == "ok" and raw is not None:
            self._state.soc_raw = raw
            self._state.soc_percent = signals.uds_soc_percent(raw)
            self._state.soc_percent_monotonic = time.monotonic()
            self._state.soc_source = "poll"
            self._state.uds_resp_id = addr
            self._state.uds_replies += 1
        elif kind == "nrc":
            self._state.uds_nrcs += 1

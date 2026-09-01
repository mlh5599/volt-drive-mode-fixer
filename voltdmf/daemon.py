"""Wire-up and main loop.

    config -> CAN bus -> VehicleState (RX loop) -> reconciler -> safety gate -> mode cycle

The loop only *reads* and *evaluates*; the safety gate is the sole route to a
mode-button transmission, and any exception in the loop body is swallowed so
the daemon stays passive rather than dying or retrying mid-burst (DESIGN.md
"Safety model"). The one other thing the loop transmits is the ``22 005B``
SOC poll -- a read, sent armed or disarmed.

The daemon runs permanently under systemd as root. It **boots armed**, but the
shipped config's ``default_setpoint: auto`` means it enforces nothing until
the driver picks HOLD/MOUNTAIN (panel SW1) or the SOC-HOLD floor engages -- a
restart on a healthy pack leaves the car where it is. ``voltdmf-ctl disarm``
is the mid-drive stop. Operators steer it from an unprivileged account through the
control socket (:mod:`voltdmf.control`, ``voltdmf-ctl``): ``status`` is
answered read-only on the socket thread; ``set-mode`` / ``setpoint`` / ``arm``
/ ``disarm`` / ``reload`` are queued and executed here on the single loop
thread, so the transmit path stays single-threaded.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import control
from .canio import CanInterface
from .config import Config, ConfigError, load_config
from .lcddash import LcdDashboard
from .modecycle import ModeCycleController
from .reconciler import Reconciler, build_reconciler
from .safety import SafetyGate
from .signals import UDS_SOC_REQ_IDS, DriveMode
from .state import VehicleState

log = logging.getLogger(__name__)

#: How often to run the reconciler / service the poller.
LOOP_PERIOD_S = 1.0

#: Wait this long for the bus to come alive before reconciling, so a
#: crash-restart mid-drive reads real state before the first walk.
BUS_WARMUP_TIMEOUT_S = 10.0

#: Cadence of the periodic trip line to the journal (both armed and disarmed).
TRIP_LOG_PERIOD_S = 30.0

#: A poll reply older than this is treated as stale by the reconciler (it then
#: leans on the 0x096 b3 failsafe).
POLL_STALE_S = 45.0


class Daemon:
    def __init__(self, config: Config, *, channel: str = "can0",
                 lcd: bool = True,
                 lcd_port: str = "/dev/serial0", lcd_baud: int = 9600,
                 lcd_backlight: int = 45,
                 control_enabled: bool = True,
                 control_socket_path: str | None = None,
                 config_path: str | None = None,
                 start_armed: bool = True) -> None:
        self._config = config
        self._config_path = config_path
        self._channel = channel
        self._lcd = lcd
        self._lcd_opts = dict(port=lcd_port, baud=lcd_baud,
                              backlight=lcd_backlight)
        self._control_enabled = control_enabled
        self._control_socket_path = control_socket_path
        self._stop = threading.Event()
        # Set whenever the loop should stop its inter-iteration sleep early: on
        # shutdown, or when a control command is waiting in the queue.
        self._wake = threading.Event()
        self._last_action: str | None = None

        # The one transmit lock now that ``--dry-run`` is gone. The daemon
        # boots armed; ``voltdmf-ctl disarm`` flips this off mid-drive.
        self._armed = bool(start_armed)

        # A manual `set-mode` sets this; informational only (shown in
        # `status`). The reconciler owns the mode and walks a hand-change back.
        self._manual_target: DriveMode | None = None

        # Live objects, populated for the duration of run() so control-command
        # handlers and the status snapshot can reach them.
        self._state: VehicleState | None = None
        self._reconciler: Reconciler = build_reconciler(
            config, poll_stale_s=POLL_STALE_S)
        self._gate: SafetyGate | None = None
        self._started = time.monotonic()

        # SOC poller bookkeeping (see _service_soc_poll).
        self._next_poll_at = 0.0
        self._poll_req_cursor = 0
        self._next_trip_log_at = 0.0

        self._cmd_queue: "queue.Queue[control.Command]" = queue.Queue()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- transmit gating --------------------------------------------------
    def _transmit_enabled(self) -> bool:
        """The live predicate the CAN layer consults before every press."""
        return self._armed

    def run(self) -> None:
        state = VehicleState()
        self._state = state
        self._reconciler = build_reconciler(self._config, poll_stale_s=POLL_STALE_S)
        self._started = time.monotonic()
        self._next_poll_at = time.monotonic()
        self._next_trip_log_at = time.monotonic() + TRIP_LOG_PERIOD_S

        with CanInterface(self._channel, tx_gate=self._transmit_enabled) as can_if:
            can_if.start_rx(state)
            # Current mode is read straight off the bus: the RX listener
            # decodes 0x1F4 byte 1 (the committed drive mode) into
            # state.drive_mode. It stays None until the first 0x1F4 frame
            # arrives, and ModeCycleController turns that into a
            # ModeUnknownError rather than injecting -- the fail-safe we want
            # on a bus we cannot observe.
            controller = ModeCycleController(
                can_if,
                lambda: state.drive_mode,
            )
            self._gate = SafetyGate(controller)

            server = self._start_control_server()
            dash = None
            if self._lcd:
                # arm/disarm gates CAN *transmission* only; the watch screen is
                # a display, so it always drives the real panel. The disarmed
                # state still shows on it -- see the "OFF " tag in _lcd_status.
                dash = LcdDashboard(state, self._lcd_status,
                                    channel=self._channel, **self._lcd_opts)
                dash.start()

            try:
                self._await_bus(state)
                log.info("reconciler active (setpoint=%s); entering loop%s",
                         self._reconciler.setpoint_label, self._mode_tag())

                while not self._stop.is_set():
                    self._drain_commands()
                    try:
                        self._service_soc_poll(can_if)
                        self._reconcile()
                        self._maybe_log_trip()
                    except Exception:  # fail-passive
                        log.exception("loop iteration failed; continuing passively")
                    self._wait_next()
            finally:
                if dash is not None:
                    dash.stop()
                if server is not None:
                    server.stop()

        self._state = None
        self._gate = None
        log.info("daemon stopped")

    # -- control plane --------------------------------------------------
    def _start_control_server(self) -> control.ControlServer | None:
        if not self._control_enabled:
            log.info("control socket disabled (--no-control)")
            return None
        listener = control.inherited_listener()
        if listener is None and self._control_socket_path is None:
            log.info("no control socket (not socket-activated, no --control-socket)")
            return None
        server = control.ControlServer(
            self._cmd_queue,
            status_provider=self._status_snapshot,
            listener=listener,
            path=self._control_socket_path,
            on_enqueue=self._wake.set,
        )
        server.start()
        return server

    # -- SOC poll -------------------------------------------------------
    def _service_soc_poll(self, can_if: CanInterface) -> None:
        """Emit one ``22 005B`` request when the period is up.

        Runs armed or disarmed (a poll is a read). Before an ECU has
        answered, cycle 0x7E4 / 0x7E0; once ``state.uds_resp_id`` is set, pin
        the id that replied (request id = response id - 8).
        """
        if not self._config.soc_poll.enabled:
            return
        now = time.monotonic()
        if now < self._next_poll_at:
            return
        self._next_poll_at = now + self._config.soc_poll.period_seconds

        assert self._state is not None
        locked = self._state.uds_resp_id
        if locked is not None:
            req_id = locked - 8
        else:
            req_id = UDS_SOC_REQ_IDS[self._poll_req_cursor % len(UDS_SOC_REQ_IDS)]
            self._poll_req_cursor += 1
        try:
            can_if.send_soc_poll(req_id)
        except Exception:  # never let a poll TX failure stall the loop
            log.exception("SOC poll transmit failed")

    # -- reconcile ----------------------------------------------------
    def _reconcile(self) -> None:
        """One pass of the level-triggered reconciler: figure out the mode the
        car should be in and, when armed, ask the gate to walk it there."""
        assert self._state is not None and self._gate is not None
        state = self._state
        # No decodable current mode yet -- bus quiet, or only just up. There is
        # nothing to compare against and ModeCycleController would not know
        # where the menu cursor is, so the reconciler waits. (Fresh boot on a
        # healthy pack lands here until the first 0x1F4 frame decodes.)
        if state.drive_mode is None:
            return
        desired = self._reconciler.desired_mode(state)
        # desired is None => passive setpoint (auto) and the floor is clear:
        # enforce nothing, leave the car wherever the driver has it.
        if desired is None or desired == state.drive_mode:
            return

        floor = "floor" if self._reconciler.floor_latched else "set"
        self._manual_target = None  # the reconciler owns the mode
        actual = state.drive_mode.value if state.drive_mode else None
        if self._armed:
            self._last_action = (
                f"{self._action_prefix()}{floor}->{desired.value.upper()}")
            log.info("reconcile: %s -> %s (%s)", actual, desired.value, floor)
            self._gate.request(desired, state)
        else:
            self._last_action = f"OFF {floor}->{desired.value.upper()}"
            log.info("reconcile (disarmed): %s -> %s (%s) -- not acting",
                     actual, desired.value, floor)

    def _maybe_log_trip(self) -> None:
        now = time.monotonic()
        if now < self._next_trip_log_at:
            return
        self._next_trip_log_at = now + TRIP_LOG_PERIOD_S
        self._log_trip_line()

    def _log_trip_line(self) -> None:
        st = self._state
        if st is None:
            return
        age = st.soc_percent_age()
        soc = ("--" if st.soc_percent is None
               else f"{st.soc_percent:.1f}%")
        raw = "--" if st.soc_raw is None else f"0x{st.soc_raw:02X}"
        resp = "--" if st.uds_resp_id is None else f"0x{st.uds_resp_id:03X}"
        age_s = "--" if age is None else f"{age:.0f}s"
        b3 = "--" if st.soc_bar_raw is None else str(st.soc_bar_raw)
        floor = "latched" if self._reconciler.floor_latched else "off"
        mode = st.drive_mode.value if st.drive_mode else "?"
        log.info(
            "trip: soc=%s (%s @%s, age %s) b3=%s floor=%s setpoint=%s "
            "mode=%s armed=%s replies=%d nrc=%d",
            soc, raw, resp, age_s, b3, floor, self._reconciler.setpoint_label,
            mode, self._armed, st.uds_replies, st.uds_nrcs,
        )

    def _wait_next(self) -> None:
        """Sleep between iterations, but return at once if a command arrives
        (``_wake`` set by ``on_enqueue``) or we are shutting down."""
        self._wake.wait(LOOP_PERIOD_S)
        self._wake.clear()

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                return
            try:
                result = self._handle_command(cmd.name, cmd.args)
            except Exception as exc:  # never let a bad command kill the loop
                log.exception("control command %r failed", cmd.name)
                result = {"ok": False, "error": f"internal error: {exc}"}
            try:
                cmd.reply.put_nowait(result)
            except queue.Full:  # client already gave up; nothing to do
                pass

    def _handle_command(self, name: str, args: dict) -> dict:
        if name == "arm":
            self._armed = True
            log.warning("ARMED via control socket -- transmission enabled")
            return {"ok": True, "armed": True}
        if name == "disarm":
            self._armed = False
            log.warning("DISARMED via control socket -- transmission suppressed")
            return {"ok": True, "armed": False}
        if name == "set-mode":
            return self._cmd_set_mode(args)
        if name == "setpoint":
            return self._cmd_setpoint(args)
        if name == "reload":
            return self._cmd_reload()
        return {"ok": False, "error": f"unhandled command {name!r}"}

    def _cmd_set_mode(self, args: dict) -> dict:
        raw = args.get("mode")
        try:
            mode = DriveMode(raw)
        except ValueError:
            return {"ok": False, "error": f"unknown mode {raw!r}"}
        force = bool(args.get("force", False))
        if not self._transmit_enabled():
            return {"ok": False,
                    "error": "daemon disarmed; run `voltdmf-ctl arm` first",
                    "would_switch_to": mode.value}
        assert self._gate is not None and self._state is not None
        outcome = self._gate.request_verbose(mode, self._state, force=force)
        self._last_action = f"MANUAL {mode.value.upper()}"
        if outcome.sent:
            self._manual_target = mode
        return {
            "ok": not outcome.blocked,
            "result": outcome.reason,
            "sent": outcome.presses,
            "drive_mode": (self._state.drive_mode.value
                           if self._state.drive_mode else None),
        }

    def _cmd_setpoint(self, args: dict) -> dict:
        raw = args.get("mode")
        try:
            mode = DriveMode(str(raw).lower())
        except ValueError:
            return {"ok": False, "error": f"unknown mode {raw!r}"}
        try:
            self._reconciler.set_setpoint(mode)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        log.warning("setpoint -> %s via control socket", mode.value)
        self._last_action = f"SETPOINT {mode.value.upper()}"
        self._wake.set()  # reconcile on the next tick, not after the full sleep
        return {"ok": True, "setpoint": self._reconciler.setpoint_label}

    def _cmd_reload(self) -> dict:
        if self._config_path is None:
            return {"ok": False,
                    "error": "reload unavailable (daemon started without a config path)"}
        try:
            new_config = load_config(self._config_path)
            new_reconciler = build_reconciler(new_config, poll_stale_s=POLL_STALE_S)
        except (OSError, ConfigError) as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}
        # The setpoint is the driver's live choice -- carry it across the
        # reload. The SOC-floor latch is in-memory state and is dropped. If the
        # driver never picked one (still passive), leave the new reconciler at
        # its own default_setpoint.
        carried = self._reconciler.setpoint
        if carried is not None:
            new_reconciler.set_setpoint(carried)
        self._config = new_config
        self._reconciler = new_reconciler
        log.info("config reloaded via control socket (setpoint=%s, floor latch cleared)",
                 new_reconciler.setpoint_label)
        return {"ok": True, "setpoint": new_reconciler.setpoint_label,
                "floor_latched": False}

    def _status_snapshot(self) -> dict:
        st = self._state
        gate = self._gate
        rec = self._reconciler
        age = st.soc_percent_age() if st is not None else None
        return {
            "armed": self._armed,
            "transmit_enabled": self._transmit_enabled(),
            "setpoint": rec.setpoint_label,
            "floor_latched": rec.floor_latched,
            "reconciler": rec.snapshot(),
            "drive_mode": (st.drive_mode.value
                           if st is not None and st.drive_mode else None),
            "shift": st.shift.value if st is not None else None,
            "soc_percent": (round(st.soc_percent, 1)
                            if st is not None and st.soc_percent is not None
                            else None),
            "soc_raw": st.soc_raw if st is not None else None,
            "soc_bar_raw": st.soc_bar_raw if st is not None else None,
            "soc_source": st.soc_source if st is not None else None,
            "soc_age_s": round(age, 1) if age is not None else None,
            "uds_replies": st.uds_replies if st is not None else None,
            "uds_nrcs": st.uds_nrcs if st is not None else None,
            "uds_resp_id": st.uds_resp_id if st is not None else None,
            "speed_mph": st.speed_mph if st is not None else None,
            "bus_active": bool(st is not None and st.bus_active),
            "manual_override": (self._manual_target.value
                                if self._manual_target else None),
            "cooldown_remaining_s": (round(gate.cooldown_remaining(), 1)
                                     if gate is not None else None),
            "last_action": self._last_action,
            "uptime_s": round(time.monotonic() - self._started, 1),
        }

    # -- labels -------------------------------------------------------------
    def _mode_tag(self) -> str:
        return "" if self._armed else " (disarmed)"

    def _action_prefix(self) -> str:
        return "" if self._armed else "OFF "

    def _lcd_status(self) -> str:
        """One-line (<=20 char) summary of what the fixer is doing, for the
        watch screen's bottom row."""
        if self._last_action is not None:
            return self._last_action
        p = self._action_prefix()
        if self._reconciler.floor_latched:
            return f"{p}SOC-FLOOR -> HOLD"
        sp = self._reconciler.setpoint
        if sp is None:
            return f"{p}auto (no target)"
        return f"{p}hold {sp.value.upper()}"

    def _await_bus(self, state: VehicleState) -> None:
        deadline = time.monotonic() + BUS_WARMUP_TIMEOUT_S
        while not state.bus_active and time.monotonic() < deadline:
            if self._stop.wait(0.2):
                return
        if not state.bus_active:
            log.warning("no CAN traffic after %.0fs; continuing anyway",
                        BUS_WARMUP_TIMEOUT_S)

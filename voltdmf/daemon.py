"""Wire-up and main loop.

    config -> CAN bus -> VehicleState (RX loop) -> triggers -> safety gate -> mode cycle

The loop only *reads* and *evaluates*; the safety gate is the sole route to a
transmission, and any exception in the loop body is swallowed so the daemon
stays passive rather than dying or retrying mid-burst (DESIGN.md "Safety model").

The daemon runs permanently under systemd as root. Operators steer it from an
unprivileged account through the control socket (:mod:`voltdmf.control`,
``voltdmf-ctl``): ``status`` is answered read-only on the socket thread;
``set-mode`` / ``arm`` / ``disarm`` / ``reload`` are queued and executed here on
the single loop thread, so the transmit path stays single-threaded.
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
from .modecycle import ModeCycleController, PressCountingModeTracker
from .safety import SafetyGate
from .signals import DriveMode
from .state import VehicleState
from .triggers import build_triggers

log = logging.getLogger(__name__)

#: How often to evaluate triggers.
LOOP_PERIOD_S = 1.0

#: Wait this long for the bus to come alive before evaluating anything, so a
#: crash-restart mid-drive reads real state before the on-start trigger fires.
BUS_WARMUP_TIMEOUT_S = 10.0

# UNCONFIRMED: assumes the car powers up in NORMAL every ignition cycle.
# tools/ignition_check.py verifies this; until then the press-counting mode
# tracker (and therefore every switch) is only as right as this line.
ASSUMED_START_MODE = DriveMode.NORMAL


class Daemon:
    def __init__(self, config: Config, *, channel: str = "can0",
                 dry_run: bool = False, lcd: bool = True,
                 lcd_port: str = "/dev/serial0", lcd_baud: int = 9600,
                 lcd_backlight: int = 45,
                 control_enabled: bool = True,
                 control_socket_path: str | None = None,
                 config_path: str | None = None,
                 start_armed: bool = False) -> None:
        self._config = config
        self._config_path = config_path
        self._channel = channel
        self._dry_run = dry_run
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

        # Runtime transmit enable. Separate from --dry-run (the immutable
        # session lock): a non-dry-run daemon still boots disarmed and an
        # operator must `voltdmf-ctl arm` it. --dry-run wins regardless.
        self._armed = bool(start_armed) and not dry_run

        # A manual `set-mode` sets this; it is informational only (shown in
        # `status`). The next real trigger edge clears it and reclaims control.
        self._manual_target: DriveMode | None = None

        # Live objects, populated for the duration of run() so control-command
        # handlers and the status snapshot can reach them.
        self._state: VehicleState | None = None
        self._triggers: list = build_triggers(config)
        self._gate: SafetyGate | None = None
        self._started = time.monotonic()

        self._cmd_queue: "queue.Queue[control.Command]" = queue.Queue()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- transmit gating --------------------------------------------------
    def _transmit_enabled(self) -> bool:
        """The live predicate the CAN layer consults before every press."""
        return (not self._dry_run) and self._armed

    def run(self) -> None:
        state = VehicleState()
        self._state = state
        self._triggers = build_triggers(self._config)
        self._started = time.monotonic()
        mode_tracker = PressCountingModeTracker(ASSUMED_START_MODE)

        with CanInterface(self._channel, dry_run=self._dry_run,
                          tx_gate=self._transmit_enabled) as can_if:
            can_if.start_rx(state)
            controller = ModeCycleController(
                can_if,
                mode_tracker.get,
                on_presses_sent=mode_tracker.note_presses,
            )
            self._gate = SafetyGate(controller)

            server = self._start_control_server()
            dash = None
            if self._lcd:
                # --dry-run gates CAN *transmission* only; the watch screen is
                # a display, so it always drives the real panel. (The dry-run
                # state still shows on it -- see the "DRY " tag in _lcd_status.)
                dash = LcdDashboard(state, self._lcd_status,
                                    channel=self._channel, **self._lcd_opts)
                dash.start()

            try:
                self._await_bus(state)
                log.info("%d trigger(s) active; entering loop%s",
                         len(self._triggers),
                         self._mode_tag())

                while not self._stop.is_set():
                    self._drain_commands()
                    try:
                        self._scan_triggers()
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

    def _scan_triggers(self) -> None:
        """One pass over the triggers: the only place a trigger edge reaches
        the gate. A real edge also clears any manual override."""
        assert self._state is not None and self._gate is not None
        for trigger in self._triggers:
            target = trigger.evaluate(self._state)
            if target is not None:
                log.info("%s -> requesting %s", trigger.name, target.value)
                self._manual_target = None  # a real edge reclaims control
                self._last_action = (
                    f"{self._action_prefix()}"
                    f"{trigger.name[:9]} {target.value.upper()}")
                self._gate.request(target, self._state)

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
            if self._dry_run:
                return {"ok": False,
                        "error": "session is --dry-run; restart without it to arm"}
            self._armed = True
            log.warning("ARMED via control socket -- transmission enabled")
            return {"ok": True, "armed": True}
        if name == "disarm":
            self._armed = False
            log.warning("DISARMED via control socket -- transmission suppressed")
            return {"ok": True, "armed": False}
        if name == "set-mode":
            return self._cmd_set_mode(args)
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
            why = "dry-run session" if self._dry_run else "daemon disarmed"
            return {"ok": False,
                    "error": f"{why}; run `voltdmf-ctl arm` first",
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

    def _cmd_reload(self) -> dict:
        if self._config_path is None:
            return {"ok": False,
                    "error": "reload unavailable (daemon started without a config path)"}
        try:
            new_config = load_config(self._config_path)
            new_triggers = build_triggers(new_config)
        except (OSError, ConfigError) as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}
        self._config = new_config
        self._triggers = new_triggers  # trigger latches reset -- on-start may re-fire
        log.info("config reloaded via control socket: %d trigger(s)",
                 len(new_triggers))
        return {"ok": True, "triggers": [t.name for t in new_triggers]}

    def _status_snapshot(self) -> dict:
        st = self._state
        gate = self._gate
        return {
            "armed": self._armed,
            "dry_run": self._dry_run,
            "transmit_enabled": self._transmit_enabled(),
            "drive_mode": (st.drive_mode.value
                           if st is not None and st.drive_mode else None),
            "shift": st.shift.value if st is not None else None,
            "soc_percent": st.soc_percent if st is not None else None,
            "speed_mph": st.speed_mph if st is not None else None,
            "bus_active": bool(st is not None and st.bus_active),
            "manual_override": (self._manual_target.value
                                if self._manual_target else None),
            "triggers": [t.name for t in self._triggers],
            "cooldown_remaining_s": (round(gate.cooldown_remaining(), 1)
                                     if gate is not None else None),
            "last_action": self._last_action,
            "uptime_s": round(time.monotonic() - self._started, 1),
        }

    # -- labels -------------------------------------------------------------
    def _mode_tag(self) -> str:
        if self._dry_run:
            return " (dry-run)"
        return "" if self._armed else " (disarmed)"

    def _action_prefix(self) -> str:
        if self._dry_run:
            return "DRY "
        return "" if self._armed else "OFF "

    def _lcd_status(self) -> str:
        """One-line (<=20 char) summary of what the fixer is doing, for the
        watch screen's bottom row."""
        p = self._action_prefix()
        if self._last_action is not None:
            return self._last_action
        if self._config.on_start.enabled:
            return f"{p}arm {self._config.on_start.target_mode.value.upper()} @start"
        if self._config.soc_threshold.enabled:
            st = self._config.soc_threshold
            return (f"{p}arm {st.target_mode.value.upper()} "
                    f"<{st.threshold_percent:.0f}%")
        return f"{p}idle (no triggers)"

    def _await_bus(self, state: VehicleState) -> None:
        deadline = time.monotonic() + BUS_WARMUP_TIMEOUT_S
        while not state.bus_active and time.monotonic() < deadline:
            if self._stop.wait(0.2):
                return
        if not state.bus_active:
            log.warning("no CAN traffic after %.0fs; continuing anyway",
                        BUS_WARMUP_TIMEOUT_S)

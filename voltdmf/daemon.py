"""Wire-up and main loop.

    config -> CAN bus -> VehicleState (RX loop) -> triggers -> safety gate -> mode cycle

The loop only *reads* and *evaluates*; the safety gate is the sole route to a
transmission, and any exception in the loop body is swallowed so the daemon
stays passive rather than dying or retrying mid-burst (DESIGN.md "Safety model").
"""

from __future__ import annotations

import logging
import threading
import time

from .canio import CanInterface
from .config import Config
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
                 lcd_backlight: int = 45) -> None:
        self._config = config
        self._channel = channel
        self._dry_run = dry_run
        self._lcd = lcd
        self._lcd_opts = dict(port=lcd_port, baud=lcd_baud,
                              backlight=lcd_backlight)
        self._stop = threading.Event()
        self._last_action: str | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        state = VehicleState()
        triggers = build_triggers(self._config)
        mode_tracker = PressCountingModeTracker(ASSUMED_START_MODE)

        with CanInterface(self._channel, dry_run=self._dry_run) as can_if:
            can_if.start_rx(state)
            controller = ModeCycleController(
                can_if,
                mode_tracker.get,
                on_presses_sent=mode_tracker.note_presses,
            )
            gate = SafetyGate(controller)

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
                         len(triggers), " (dry-run)" if self._dry_run else "")

                while not self._stop.is_set():
                    try:
                        for trigger in triggers:
                            target = trigger.evaluate(state)
                            if target is not None:
                                log.info("%s -> requesting %s",
                                         trigger.name, target.value)
                                self._last_action = (
                                    f"{'DRY ' if self._dry_run else ''}"
                                    f"{trigger.name[:9]} {target.value.upper()}")
                                gate.request(target, state)
                    except Exception:  # fail-passive
                        log.exception("loop iteration failed; continuing passively")
                    self._stop.wait(LOOP_PERIOD_S)
            finally:
                if dash is not None:
                    dash.stop()

        log.info("daemon stopped")

    def _lcd_status(self) -> str:
        """One-line (<=20 char) summary of what the fixer is doing, for the
        watch screen's bottom row."""
        p = "DRY " if self._dry_run else ""
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

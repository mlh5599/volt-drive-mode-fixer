"""Wire-up and main loop.

    config -> CAN bus -> VehicleState (RX loop) -> reconciler -> safety gate -> mode cycle

The loop only *reads* and *evaluates*; the safety gate is the sole route to a
mode-button transmission, and any exception in the loop body is swallowed so
the daemon stays passive rather than dying or retrying mid-burst (DESIGN.md
"Safety model"). The one other thing the loop transmits is the ``22 005B``
SOC poll -- a read, sent armed or disarmed.

The daemon runs permanently under systemd as root. It **boots armed**, but the
shipped config's ``default_position: hold-soc`` is passive: it enforces nothing
until the SOC-HOLD floor engages or the driver taps SW1 forward -- a restart on
a healthy pack leaves the car where it is. ``voltdmf-ctl disarm``
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
from .reconciler import CYCLE, Position, Reconciler, build_reconciler
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

#: After a walk goes on the wire, skip re-evaluating for this long. 0x1F4
#: byte 1 (the committed mode the reconciler compares against) lags the menu
#: commit by ~3 s, so without this the level-triggered loop re-requests the
#: same target every tick until the commit lands -- log spam, and a needless
#: second trip through the walk machinery. The 60 s SafetyGate cooldown is
#: still the hard rate limit; this just quiets the gap.
WALK_SETTLE_S = 5.0

#: Panel walk-test (``voltdmf-ctl walk-test`` / SW1 solo hold >= 8 s): the
#: drive modes it steps the closed-loop walk through, in order. After the last
#: it walks back to whatever mode the car started in.
WALK_TEST_ORDER = (DriveMode.NORMAL, DriveMode.SPORT,
                   DriveMode.MOUNTAIN, DriveMode.HOLD)

#: Pause after each walk-test leg so the ~3 s 0x1F4 byte-1 commit lands before
#: the leg is scored. The test drives its own cooldown-free SafetyGate, so
#: this doubles as the inter-leg spacing; the real 60 s gate is untouched.
WALK_TEST_LEG_SETTLE_S = 4.0

#: Focused probe (``voltdmf-ctl probe``): tick of the background cursor
#: sampler. Fast enough to catch the byte-4 cursor stepping between the 0.2 s
#: post-tap reads the closed loop actually consults.
PROBE_SAMPLE_PERIOD_S = 0.05


class _CursorSampler(threading.Thread):
    """Background dense sampler for one :meth:`Daemon._run_probe`.

    Snapshots the two 0x1F4 readbacks (``menu_cursor`` / ``menu_cursor_raw``
    byte 4, ``drive_mode`` byte 1) plus the menu-open hint every
    ``PROBE_SAMPLE_PERIOD_S`` and keeps a row whenever any of them changes,
    with a 1 s heartbeat, timestamped from sampler start. Read-only against
    :class:`VehicleState` -- it never drives anything; it just puts the whole
    cursor trajectory on the record so a probe that "searched" is diagnosable
    from the journal alone. Stops on :meth:`stop`.
    """

    def __init__(self, state: VehicleState) -> None:
        super().__init__(name="probe-sampler", daemon=True)
        self._st = state
        # NB: not ``self._stop`` -- that name is a method on threading.Thread
        # and shadowing it breaks join().
        self._halt = threading.Event()
        self.samples: list[dict] = []
        self._t0 = time.monotonic()

    def _row(self, t: float) -> dict:
        st = self._st
        return {
            "t": round(t, 3),
            "cursor": st.menu_cursor.value if st.menu_cursor else None,
            "raw": st.menu_cursor_raw,
            "byte1": st.drive_mode.value if st.drive_mode else None,
            "open": st.menu_open_hint,
        }

    def run(self) -> None:
        last_key: tuple | None = None
        next_hb = 0.0
        while not self._halt.is_set():
            t = time.monotonic() - self._t0
            row = self._row(t)
            key = (row["cursor"], row["raw"], row["byte1"], row["open"])
            if key != last_key or t >= next_hb:
                self.samples.append(row)
                if key != last_key:
                    raw = row["raw"]
                    log.warning(
                        "probe sample t=+%.2fs cursor=%s raw=%s byte1=%s open=%s",
                        t, row["cursor"],
                        f"0x{raw:02x}" if raw is not None else None,
                        row["byte1"], row["open"])
                last_key = key
                next_hb = t + 1.0
            self._halt.wait(PROBE_SAMPLE_PERIOD_S)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=1.0)


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
        # The controller the real gate wraps -- kept so _run_walk_test_cycle can
        # build its own short-lived gate around the same (single) TX path.
        self._controller: ModeCycleController | None = None
        self._started = time.monotonic()

        # Panel walk-test: a queued `walk-test` command just sets the flag; the
        # loop thread runs the ~1 min cycle inline on its next pass (so the TX
        # path stays single-threaded and the reconciler is naturally skipped).
        self._walk_test_pending = False
        self._walk_test_result: dict | None = None

        # Per-tap 0x1F4 trace shared by the walk-test and the focused probe.
        # _trace_active gates _log_walk_tap so a normal reconcile walk does not
        # spam the journal; _trace_tag labels the lines; _trace collects the
        # structured rows for the status snapshot.
        self._trace_active = False
        self._trace_tag = "walk-test"
        self._trace: list[dict] = []

        # Interactive focused walk probe (voltdmf-ctl test-mode / probe).
        # test-mode suspends the reconciler so nothing re-asserts a setpoint
        # between probes; it is in-memory, so a daemon restart resumes
        # protection on its own. _probe_pending is set by the queued command
        # and run inline on the loop's next pass (like the walk-test).
        self._test_mode = False
        self._probe_pending: DriveMode | None = None
        #: True for the DURATION of _run_probe. Distinct from _probe_pending,
        #: which _run_probe clears on its first line -- so a status field
        #: derived from _probe_pending alone reads False while the probe is
        #: actually running, and a client polling "not running" gets handed the
        #: PREVIOUS probe's result. That misread produced a whole round of
        #: bogus verdicts on 2026-09-04 (rows reporting LANDED with a byte1
        #: that did not match their own target).
        self._probe_active = False
        #: Bumped once per completed probe. Lets a client tell a fresh result
        #: from a stale one without having to trust any running flag.
        self._probe_seq = 0
        self._probe_result: dict | None = None

        # SOC poller bookkeeping (see _service_soc_poll).
        self._next_poll_at = 0.0
        self._poll_req_cursor = 0
        self._next_trip_log_at = 0.0

        # monotonic() before which _reconcile() holds off after a dispatched
        # walk -- see WALK_SETTLE_S.
        self._walk_settle_until = 0.0

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
            self._controller = self._build_controller(can_if, state)
            self._gate = SafetyGate(self._controller)

            server = self._start_control_server()
            dash = None
            if self._lcd:
                # arm/disarm gates CAN *transmission* only; the watch screen is
                # a display, so it always drives the real panel. The disarmed
                # state still shows on it -- see the "DIS " tag in _lcd_status.
                dash = LcdDashboard(state, self._lcd_status,
                                    selector_fn=self._lcd_selector,
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
                        if self._walk_test_pending:
                            self._run_walk_test_cycle()
                        elif self._probe_pending is not None:
                            self._run_probe(self._probe_pending)
                        elif not self._test_mode:
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
        self._controller = None
        log.info("daemon stopped")

    def _build_controller(self, can_if: CanInterface,
                          state: VehicleState) -> ModeCycleController:
        """Wire the mode-cycle controller to the two 0x1F4 readbacks.

        Both come off the RX listener -- the ``can.Notifier`` owns the bus, so
        a second manual ``recv()`` would fight it:

        * ``state.drive_mode`` -- 0x1F4 byte 1, the *committed* mode. ``None``
          until the first 0x1F4 frame; :class:`ModeCycleController` turns that
          into a ``ModeUnknownError`` rather than injecting blind.
        * ``state.menu_cursor`` -- 0x1F4 bytes 4+5, the *live* menu cursor.
          Passing it makes ``switch_to()`` run the closed loop: tap, let the
          cursor settle, stop the instant it reads the target. Without it the
          walk is open-loop ``index(target)+1``, which is also correct from
          any mode -- the menu always opens on NORMAL (measured 2026-09-03,
          tools/press_calibrate.py). The closed loop buys early exit and a
          hard failure when a tap does not register, not correctness.
        """
        return ModeCycleController(
            can_if,
            lambda: state.drive_mode,
            menu_cursor_source=lambda: state.menu_cursor,
            tap_observer=self._log_walk_tap,
        )

    def _log_walk_tap(self, tap: int, target: DriveMode) -> None:
        """Per-tap 0x1F4 trace, emitted only while a walk-test or focused probe
        is running. The closed-loop match is blind on the injected path -- this
        line shows what the cursor readback actually did after each tap so a
        failing leg is diagnosable from the journal alone. Rows are also kept
        on ``self._trace`` for the status snapshot."""
        if not self._trace_active:
            return
        st = self._state
        if st is None:
            return
        raw = st.menu_cursor_raw
        row = {
            "tap": tap,
            "target": target.value,
            "cursor": st.menu_cursor.value if st.menu_cursor else None,
            "raw": raw,
            "byte1": st.drive_mode.value if st.drive_mode else None,
            "open": st.menu_open_hint,
        }
        self._trace.append(row)
        log.warning(
            "%s tap %d -> %s: cursor=%s raw=%s byte1=%s open=%s",
            self._trace_tag, tap, target.value, row["cursor"],
            f"0x{raw:02x}" if raw is not None else None,
            row["byte1"], row["open"],
        )

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
        # An interactive probe session owns the mode -- do not re-assert a
        # setpoint between probes. (The loop already skips this call while
        # test-mode is on; this guard also covers a direct call.)
        if self._test_mode:
            return
        # Just dispatched a walk -- give the ~3 s byte-1 commit lag time to
        # resolve before comparing again (see WALK_SETTLE_S).
        if time.monotonic() < self._walk_settle_until:
            return
        # No decodable current mode yet -- bus quiet, or only just up. There is
        # nothing to compare against and ModeCycleController would not know
        # where the menu cursor is, so the reconciler waits. (Fresh boot on a
        # healthy pack lands here until the first 0x1F4 frame decodes.)
        if state.drive_mode is None:
            return
        desired = self._reconciler.desired_mode(state)
        # desired is None => the selector is on `hold-soc` with the floor
        # still clear, or on `off`: enforce nothing, leave the car alone.
        if desired is None or desired == state.drive_mode:
            return

        floor = "floor" if self._reconciler.floor_latched else "set"
        self._manual_target = None  # the reconciler owns the mode
        actual = state.drive_mode.value if state.drive_mode else None
        if self._armed:
            self._last_action = (
                f"{self._action_prefix()}{floor}->{desired.value.upper()}")
            log.info("reconcile: %s -> %s (%s)", actual, desired.value, floor)
            if self._gate.request(desired, state):
                self._walk_settle_until = time.monotonic() + WALK_SETTLE_S
        else:
            self._last_action = f"DIS {floor}->{desired.value.upper()}"
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
            "mode=%s armed=%s replies=%d nrc=%d%s",
            soc, raw, resp, age_s, b3, floor, self._reconciler.setpoint_label,
            mode, self._armed, st.uds_replies, st.uds_nrcs,
            " TEST-MODE" if self._test_mode else "",
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
        if name == "walk-test":
            return self._cmd_walk_test()
        if name == "test-mode":
            return self._cmd_test_mode(args)
        if name == "probe":
            return self._cmd_probe(args)
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
        """Move the four-position selector: an explicit detent, or ``next``.

        ``next`` is what the SW1 tap sends. The daemon owns the cycle so the
        button helper cannot drift out of step with it.
        """
        raw = str(args.get("mode", "")).lower()
        before = self._reconciler.position
        if raw == "next":
            self._reconciler.advance()
        else:
            try:
                self._reconciler.set_position(Position(raw))
            except ValueError:
                allowed = ", ".join(p.value for p in CYCLE)
                return {"ok": False,
                        "error": f"unknown position {raw!r} (want: {allowed}, next)"}
        after = self._reconciler.position
        log.warning("selector %s -> %s via control socket",
                    before.value, after.value)
        self._last_action = f"SEL {after.value.upper()}"
        self._walk_settle_until = 0.0  # an explicit choice acts now
        self._wake.set()  # reconcile on the next tick, not after the full sleep
        return {"ok": True, "setpoint": after.value,
                "position": after.value, "previous": before.value,
                "description": self._reconciler.describe_position()}

    def _cmd_reload(self) -> dict:
        if self._config_path is None:
            return {"ok": False,
                    "error": "reload unavailable (daemon started without a config path)"}
        try:
            new_config = load_config(self._config_path)
            new_reconciler = build_reconciler(new_config, poll_stale_s=POLL_STALE_S)
        except (OSError, ConfigError) as exc:
            return {"ok": False, "error": f"reload failed: {exc}"}
        # The selector position is the driver's live choice -- carry it across
        # the reload rather than snapping the car back to the config default
        # under someone's hand. The SOC-floor latch is in-memory state and is
        # dropped; the next pass re-derives it from a live reading.
        new_reconciler.set_position(self._reconciler.position)
        self._config = new_config
        self._reconciler = new_reconciler
        self._walk_settle_until = 0.0  # re-evaluate against the new policy now
        log.info("config reloaded via control socket (selector=%s, floor latch cleared)",
                 new_reconciler.setpoint_label)
        return {"ok": True, "setpoint": new_reconciler.setpoint_label,
                "position": new_reconciler.setpoint_label,
                "floor_latched": False}

    # -- walk-test (panel self-test of the closed-loop mode walk) ---------
    def _cmd_walk_test(self) -> dict:
        """Queue the closed-loop mode-walk self-test. Returns immediately; the
        loop thread runs the cycle on its next pass (see _run_walk_test_cycle)
        and reports progress/result on the LCD watch screen + the journal."""
        # Surface every refusal on the LCD watch screen too -- the panel
        # gesture is the whole point of this command, and the driver has no
        # other feedback channel in the car.
        if not self._transmit_enabled():
            self._last_action = "WALK-TEST: DISARMED"
            return {"ok": False,
                    "error": "daemon disarmed; run `voltdmf-ctl arm` first"}
        if self._walk_test_pending:
            # already running -- the LCD is already showing WALK TEST n/N
            return {"ok": False, "error": "walk-test already queued"}
        st = self._state
        if st is None or st.drive_mode is None:
            self._last_action = "WALK-TEST: NO BUS"
            return {"ok": False,
                    "error": "no drive mode decoded yet (bus quiet / car off?)"}
        self._walk_test_pending = True
        self._last_action = "WALK-TEST QUEUED"
        self._wake.set()
        log.warning("walk-test queued (origin=%s) -- reconciler paused ~1 min",
                    st.drive_mode.value)
        return {"ok": True, "started": True, "origin": st.drive_mode.value}

    def _run_walk_test_cycle(self) -> None:
        """Step the closed-loop walk through every drive mode, score each
        landing off 0x1F4 byte 1, then walk back to the starting mode. Runs on
        the loop thread with the reconciler skipped; drives its own
        cooldown-free :class:`SafetyGate` around the shared
        controller so the real 60 s gate and single TX path are untouched."""
        self._walk_test_pending = False
        st = self._state
        if st is None or self._controller is None or st.drive_mode is None:
            self._walk_test_result = {"ok": False, "summary": "walk-test: aborted"}
            self._last_action = "WALK-TEST ABORT"
            log.warning("walk-test: aborted before start (no mode / no controller)")
            return

        origin = st.drive_mode
        gate = SafetyGate(self._controller, cooldown_s=0.0)
        log.warning("walk-test: start (origin=%s)", origin.value)

        sequence = list(WALK_TEST_ORDER) + [origin]  # cycle every mode, then restore
        legs: list[dict] = []
        self._trace = []
        self._trace_tag = "walk-test"
        self._trace_active = True  # arm the per-tap 0x1F4 trace
        try:
            for i, target in enumerate(sequence, start=1):
                restore = i == len(sequence)
                self._last_action = f"WALK TEST {i}/{len(sequence)}"
                legs.append(self._walk_test_leg(gate, target, st, restore=restore))
                if self._stop.is_set():
                    log.warning("walk-test: interrupted by shutdown")
                    break
        finally:
            self._trace_active = False

        self._manual_target = None
        scored = [leg for leg in legs if not leg["restore"]]
        passed = sum(1 for leg in scored if leg["ok"])
        all_ok = bool(legs) and all(leg["ok"] for leg in legs)
        summary = (f"walk-test {passed}/{len(scored)} legs OK"
                   + ("" if all_ok else " -- see journal"))
        self._walk_test_result = {"ok": all_ok, "summary": summary,
                                  "origin": origin.value, "legs": legs}
        self._last_action = (f"WALK-TEST {passed}/{len(scored)} OK" if all_ok
                             else f"WALK-TEST {passed}/{len(scored)} FAIL")
        (log.info if all_ok else log.warning)("walk-test: done -- %s", summary)
        self._walk_settle_until = 0.0  # let the reconciler re-evaluate at once
        self._wake.set()

    def _walk_test_leg(self, gate: SafetyGate, target: DriveMode,
                       st: VehicleState, *, restore: bool) -> dict:
        before = st.drive_mode
        outcome = gate.request_verbose(target, st, force=True)
        self._stop.wait(WALK_TEST_LEG_SETTLE_S)  # let 0x1F4 byte 1 commit
        landed = st.drive_mode
        ok = (not outcome.blocked) and landed is target
        tag = "restore" if restore else "leg"
        (log.info if ok else log.warning)(
            "walk-test %s: %s -> %s  taps=%d  landed=%s  %s%s",
            tag, before.value if before else "?", target.value, outcome.presses,
            landed.value if landed else "?", "OK" if ok else "FAIL",
            "" if not outcome.blocked else f"  [{outcome.reason}]")
        return {"target": target.value,
                "from": before.value if before else None,
                "taps": outcome.presses,
                "landed": landed.value if landed else None,
                "blocked": outcome.blocked,
                "ok": ok,
                "restore": restore}

    # -- focused interactive walk probe (voltdmf-ctl test-mode / probe) -----
    def _cmd_test_mode(self, args: dict) -> dict:
        """Suspend or resume the reconciler for an interactive probe session.

        In memory only: a daemon restart comes back with protection live. The
        SOC poll and the trip log keep running while it is on -- only the
        mode-enforcing reconcile pass is skipped, so nothing walks the car
        between probes.
        """
        on = bool(args.get("on"))
        self._test_mode = on
        self._last_action = "TEST-MODE ON" if on else "TEST-MODE OFF"
        log.warning("TEST-MODE %s via control socket -- reconciler %s",
                    "ON" if on else "OFF",
                    "suspended (probes only)" if on else "resumed")
        if not on:
            self._walk_settle_until = 0.0  # let the reconciler re-assert now
            self._wake.set()
        return {"ok": True, "test_mode": on,
                "setpoint": self._reconciler.setpoint_label}

    def _cmd_probe(self, args: dict) -> dict:
        """Queue one operator-chosen closed-loop walk to ``mode`` with the
        dense 0x1F4 trace armed. Fire-and-forget: the loop thread runs it on
        the next pass and records the result in ``status`` (``probe``) + the
        journal. Independent of test-mode, but meant to be run with test-mode
        on so the reconciler does not walk the car back between probes."""
        raw = args.get("mode")
        try:
            target = DriveMode(str(raw).lower())
        except ValueError:
            return {"ok": False, "error": f"unknown mode {raw!r}"}
        if not self._transmit_enabled():
            self._last_action = "PROBE: DISARMED"
            return {"ok": False,
                    "error": "daemon disarmed; run `voltdmf-ctl arm` first"}
        if self._walk_test_pending or self._probe_pending is not None:
            return {"ok": False, "error": "a walk-test or probe is already queued"}
        st = self._state
        if st is None or st.drive_mode is None:
            self._last_action = "PROBE: NO BUS"
            return {"ok": False,
                    "error": "no drive mode decoded yet (bus quiet / car off?)"}
        self._probe_pending = target
        self._last_action = f"PROBE {target.value.upper()} QUEUED"
        self._wake.set()
        log.warning("probe queued: target=%s origin=%s test_mode=%s",
                    target.value, st.drive_mode.value, self._test_mode)
        return {"ok": True, "started": True, "target": target.value,
                "origin": st.drive_mode.value, "test_mode": self._test_mode}

    def _run_probe(self, target: DriveMode) -> None:
        """One focused closed-loop walk to ``target``, densely traced. Runs on
        the loop thread with its own cooldown-free SafetyGate
        around the shared controller -- like a one-leg walk-test, but with a
        background ~50 ms cursor sampler so the whole byte-4 trajectory is on
        record, not just the 0.2 s post-tap snapshots the closed loop reads.

        Verdicts:

        * ``LANDED``      -- cursor reached the target during the walk *and*
          0x1F4 byte 1 reads the target after the commit-watch window.
        * ``CURSOR_ONLY`` -- cursor reached the target but byte 1 did not
          commit / reverted (expected on a parked car).
        * ``MISS``        -- MAX_WALK_TAPS taps, the cursor never got there.
        * ``BLOCKED``     -- a SafetyGate precondition stopped the walk.
        """
        self._probe_pending = None
        self._probe_active = True
        self._probe_seq += 1        # bump FIRST so both exits stamp the same
        try:                        # seq the status block is already showing
            self._run_probe_inner(target)
        finally:
            self._probe_active = False

    def _run_probe_inner(self, target: DriveMode) -> None:
        """The probe body. Split out only so :meth:`_run_probe` can hold
        ``_probe_active`` across every exit path, including an exception."""
        st = self._state
        if st is None or self._controller is None or st.drive_mode is None:
            self._probe_result = {"ok": False, "verdict": "ABORT",
                                  "target": target.value,
                                  "seq": self._probe_seq}
            self._last_action = "PROBE ABORT"
            log.warning("probe: aborted before start (no mode / no controller)")
            return

        origin = st.drive_mode
        gate = SafetyGate(self._controller, cooldown_s=0.0)
        self._trace = []
        self._trace_tag = "probe"
        sampler = _CursorSampler(st)
        log.warning("probe: start target=%s origin=%s test_mode=%s",
                    target.value, origin.value, self._test_mode)
        self._trace_active = True
        sampler.start()
        try:
            outcome = gate.request_verbose(target, st, force=True)
        finally:
            self._trace_active = False
        cursor_at_end = st.menu_cursor
        cursor_reached = (
            (cursor_at_end is not None and cursor_at_end == target)
            or any(r["cursor"] == target.value for r in self._trace)
            or any(s["cursor"] == target.value for s in sampler.samples)
        )
        log.warning("probe: walk returned taps=%d blocked=%s cursor_reached=%s [%s]",
                    outcome.presses, outcome.blocked, cursor_reached, outcome.reason)
        self._stop.wait(WALK_TEST_LEG_SETTLE_S)  # watch byte 1 for a commit
        sampler.stop()
        landed = st.drive_mode
        self._manual_target = None

        if outcome.reason.startswith("blocked:"):
            verdict = "BLOCKED"
        elif landed is target and cursor_reached:
            verdict = "LANDED"
        elif cursor_reached:
            verdict = "CURSOR_ONLY"
        else:
            verdict = "MISS"

        self._probe_result = {
            "ok": verdict in ("LANDED", "CURSOR_ONLY"),
            "seq": self._probe_seq,
            "verdict": verdict,
            "target": target.value,
            "origin": origin.value,
            "taps": outcome.presses,
            "blocked": outcome.blocked,
            "reason": outcome.reason,
            "cursor_reached": cursor_reached,
            "cursor_at_walk_end": cursor_at_end.value if cursor_at_end else None,
            "byte1_after": landed.value if landed else None,
            "settle_s": WALK_TEST_LEG_SETTLE_S,
            "taps_trace": list(self._trace),
            "samples": sampler.samples,
        }
        self._last_action = f"PROBE {target.value.upper()} {verdict}"
        (log.info if verdict == "LANDED" else log.warning)(
            "probe: DONE target=%s verdict=%s taps=%d cursor_reached=%s "
            "byte1_after=%s samples=%d",
            target.value, verdict, outcome.presses, cursor_reached,
            landed.value if landed else "?", len(sampler.samples),
        )
        self._walk_settle_until = 0.0
        self._wake.set()

    def _status_snapshot(self) -> dict:
        st = self._state
        gate = self._gate
        rec = self._reconciler
        age = st.soc_percent_age() if st is not None else None
        return {
            "armed": self._armed,
            "transmit_enabled": self._transmit_enabled(),
            "setpoint": rec.setpoint_label,
            "position_index": rec.position_index,
            "position_description": rec.describe_position(),
            "cycle": [p.value for p in CYCLE],
            "floor_latched": rec.floor_latched,
            "reconciler": rec.snapshot(),
            "drive_mode": (st.drive_mode.value
                           if st is not None and st.drive_mode else None),
            "shift": st.shift.value if st is not None else None,
            "menu_cursor": (st.menu_cursor.value
                            if st is not None and st.menu_cursor else None),
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
            "walk_test": self._walk_test_result,
            "walk_test_running": self._walk_test_pending,
            "test_mode": self._test_mode,
            "probe": self._probe_result,
            "probe_running": (self._probe_pending is not None
                              or self._probe_active),
            "probe_queued": self._probe_pending is not None,
            "probe_seq": self._probe_seq,
            "uptime_s": round(time.monotonic() - self._started, 1),
        }

    # -- labels -------------------------------------------------------------
    def _mode_tag(self) -> str:
        return "" if self._armed else " (disarmed)"

    def _action_prefix(self) -> str:
        # "DIS", not "OFF": `off` is now a selector position, and the two
        # states are independent -- the driver must be able to tell a
        # disarmed daemon from a selector parked on off.
        return "" if self._armed else "DIS "

    def _lcd_selector(self) -> dict:
        """The reconciler position + SOC freshness, for the watch screen."""
        st = self._state
        fresh = bool(
            st is not None and st.soc_source == "poll"
            and st.soc_percent_fresh(self._reconciler.poll_stale_s)
        )
        return {"label": self._reconciler.setpoint_label,
                "index": self._reconciler.position_index,
                "cycle_len": len(CYCLE),
                "floor_latched": self._reconciler.floor_latched,
                "soc_fresh": fresh}

    def _lcd_status(self) -> str:
        """One-line (<=20 char) summary of what the fixer is doing, for the
        watch screen's bottom row."""
        if self._last_action is not None:
            return self._last_action
        p = self._action_prefix()
        if self._reconciler.floor_latched:
            return f"{p}SOC-FLOOR->HOLD"   # 19 cols with the "DIS " prefix
        pos = self._reconciler.position
        # "enforcing", not "hold": with two hold detents, "hold HOLD" no
        # longer says which one the driver is on.
        if pos is Position.HOLD_NOW:
            return f"{p}enforcing HOLD"
        if pos is Position.MOUNTAIN:
            return f"{p}enforcing MTN"
        if pos is Position.OFF:
            return f"{p}OFF - not acting"
        thresh = self._reconciler.snapshot()["hold_threshold_percent"]
        return f"{p}armed for {thresh:g}%"

    def _await_bus(self, state: VehicleState) -> None:
        deadline = time.monotonic() + BUS_WARMUP_TIMEOUT_S
        while not state.bus_active and time.monotonic() < deadline:
            if self._stop.wait(0.2):
                return
        if not state.bus_active:
            log.warning("no CAN traffic after %.0fs; continuing anyway",
                        BUS_WARMUP_TIMEOUT_S)

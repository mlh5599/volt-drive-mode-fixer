# On-Vehicle Field Session Log

Running narrative of the field sessions against the real car. Pairs with
`phase-c-field-checklist.md` (the procedure) and `signals-confirmed.md` (the
decoded-signal reference). Newest session first.

---

## Session 8 — 2026-08-30 (in-car — full-drain SOC drive captured + analyzed; SOC narrowed to 3 candidates, still no scale; speed/accel logging fixed; wiki cross-check)

The SOC discharge drive finally happened. One continuous highway run took
the gauge from **10/10 → 0/10**; all ten bar-drops were hand-marked on the
panel buttons. `captures/candump-2026-08-30_131932.log` (213.9 MB, ~29 min)
+ `captures/vdmf-soclog-20260830-131932.log`. Clean capture, no GPIO
trouble this time (Session 7's three fixes held).

### Analysis — which frame carries SOC

Method: cross every monotonically-falling byte/word against the 10
GAUGE-DOWN offsets, then a **timer-rejection test** — Pearson correlation
between each segment's value-delta and its wall-clock duration. `r ≈ +1` →
elapsed-time counter; `r ≈ 0` → energy-paced (SOC-like). The final bar is
the discriminator: `1→0` fell in **53 s** vs a ~170 s average for the other
nine, so a true SOC field spends near-average travel there while a clock
barely moves.

- **Excluded — elapsed-time counters** (`delta ∝ duration`, `r ≈ 0.85–0.97`):
  `0x4CB`, `0x3DD`, `0x137`, `0x4E9`. These are what fooled earlier
  `mine_capture.py --monotonic` runs.
- **SOC candidates** (delta uncorrelated with duration; all flatten through
  the mid-drive turnaround and steepen after):
  - `0x3E3` bytes 0/1/6 — triple-redundant 8-bit, 201→161 over the drain,
    timer-corr ≈ 0.00. **Strongest.** (`0x3E3` bytes 2 & 4 rise-then-fall →
    probably pack temperature.)
  - `0x228` byte 2 — 96→79, timer-corr −0.13, cleanest monotonicity but
    coarse (≈ 1.5 counts/bar).
  - `0x186` byte 6 @ 80 Hz — 130→63, finer resolution but noisy (±8),
    guess-o-meter-like; briefly regen-ticked *up* at the turnaround.
  - `0x2C7` bytes 1–2 — sags **and recovers** (V-shaped); that's pack
    voltage under load, not a charge count. Excluded.
- **No absolute scale.** No reading at a known SOC was logged, so none of
  these can be turned into `SOC_KWH_PER_COUNT` yet.
- **Charge-mode 8/12 A toggles not captured** — the car left Park at +15.7 s,
  before any toggling. Deprioritized (owner's call).

Visual write-up — normalized overlay, per-bar table, timer-rejection
scorecard, native-scale shapes, speed trace — is in the repo at
[`analysis/session8-soc-candidates.md`](analysis/session8-soc-candidates.md)
(charts regenerable from the capture via `tools/soc_report.py`):

![Session-8 SOC candidates: normalized completion of every candidate and
rejected timer on one axis, with speed and the ten gauge-bar drops](analysis/img/session8-overlay.svg)

### GM Volt reverse-engineering wiki cross-check

<https://vehicle-reverse-engineering.fandom.com/wiki/GM_Volt> — checked its
IDs against the capture (`tools/` scratch script `wikichk.py`):

- **`0x206` SOC — confirmed absent** here (0 frames in 213 MB). The wiki's
  "206 Bytes 1-2 Battery SOC .250 kWh" is Gen 2 / another year.
- **`0x3E9` speed — present** (17 340 frames, 10 Hz). Decode is **bytes 0–1
  big-endian ÷ 64 → km/h** ("3E9 Bytes 1-2 Speed 1/64 KPH"; the wiki's hex
  literal `0x1580` for 90 km/h is a typo — 90 km/h is 5760 counts). Verified
  against the capture: ~0 at rest, ~63 mph highway cruise, dip to ~8 mph at
  the turnaround (**+1240 s** — this is the real turnaround; an earlier
  analysis draft had mislabeled it +830–990 s). Bytes 2 & 6 are a
  mux/rolling counter; bytes 0–1 hold the speed word regardless.
- **Diagnostic SOC exists as a poll, not a broadcast:** `22 005B`
  "Hybrid/EV Battery Pack Remaining Charge", `X·100/255` %, 1 byte, mode-22
  request. Reinforces that a clean filtered SOC may be diagnostic-only
  (like the per-cell voltages behind the BECM). Session 9 polls this every
  ~10 s (`soc_log.py --diag-soc`) for continuous ground-truth to calibrate
  the broadcast candidates against — active TX, but default-session /
  mode-22 only so it won't suppress the broadcasts.
- Accelerator position (context for load): `0x0C9` b5 / `0x1A1` b7 (pedal
  only, 0 during cruise), `0x1C3` b7 (includes cruise output). Brake:
  `0x0F1` b2. Odometer: `0x120` b1–4, 1/64 km.

### Code / tooling this session

- **`tools/soc_log.py` — speed logging fixed + accel added.** The old
  `SpeedProbe` needed `python-can` (not installed for the system
  interpreter the unit runs) and used a wrong `÷100` decode at the wrong
  offset, so every Session-8 mark logged `spd~n/a`. Rewritten to read a
  dedicated `candump -L can0,3E9:7FF` pipe (stdlib only) with the correct
  `0x3E9` decode, and to derive a longitudinal accel (least-squares slope
  over ~1.2 s of samples — no IMU). Marks now read
  `spd~44mph  a~-0.3  gauge=6/10`.
- **`voltdmf/signals.py`** — `decode_speed_mph` corrected; new
  `decode_speed_kmh`; `"speed"` note rewritten (still `confirmed=False` —
  not speedo-checked). `tests/test_signals.py` updated (wiki's 5760-count =
  90 km/h vector).
- **`tools/soc_log.py` — Session-9 additions.** An always-on passive
  `CandidateProbe` (`candump -L can0,186:7FF,228:7FF,3E3:7FF`) stamps every
  broadcast candidate's raw byte on every log line
  (`cand[3E3.0=161 3E3.1=161 3E3.6=160 228.2=79 186.6=63]`). New opt-in
  `--diag-soc` (the one transmit path; still needs `--yes`) polls UDS
  `22 005B` every `--diag-soc-every` s, auto-detects the responding ECU
  (0x7E4 → 0x7E0, `--diag-req-id` to pin), and stamps `soc22=61.2%(0x9C@7EC)`;
  it self-disables with one log line if nothing answers. The deployed
  `voltdmf-soclog.service` / ansible `voltdmf` role keep the passive
  `ExecStart` — add `--diag-soc` (and bump `voltdmf_version`) before a drive.
- **`tools/soc_report.py` (new)** — stdlib-only, read-only. `extract` parses
  the marks log + capture into `docs/analysis/data/session8-soc.json`;
  `render` draws the overlay / native-scale SVGs under `docs/analysis/img/`.
  The visual write-up lives at `docs/analysis/session8-soc-candidates.md`
  (this replaces the old published-artifact link).
- `tests/test_soc_log.py` (new) covers `_uds_soc_percent`, the
  `CandidateProbe` line parser, and `DiagSocProbe` positive/negative-response
  matching + ECU lock-on. Full suite green (183).

### Decision — SOC-HOLD floor = 2 gauge bars

The `1→0` bar took 53 s. The bottom of the gauge is a cliff, so the floor
has to engage while **2 bars** are still showing — no margin to wait for a
lower reading. Recorded in `DESIGN.md` ("Mode policy"); `hold_threshold_percent`
placeholder moved 18 → 20 (~2 bars on the 10-bar gauge), `hold_reset_percent`
25 → 30 (~3 bars). Real values come from the candidate's raw reading at the
2-bar / 3-bar crossings once it's calibrated.

### Next session — the scaling-anchor drive

Planned by the owner; written into `phase-c-field-checklist.md` §2b:

Run it with `soc_log.py --yes --minutes 90 --buttons --lcd --diag-soc` (the
`--diag-soc` flag is new — see "Code / tooling" above). Every log line now
carries the raw value of all three broadcast candidates
(`cand[3E3.0=… 228.2=… 186.6=…]`), and `--diag-soc` adds a `22 005B` poll
every 10 s (`soc22=…%`) — the diagnostic poll is now **part of the run**, not
optional, so the broadcast candidates and the diagnostic percent are logged
side by side for a direct raw→% fit.

1. Start at a **full charge**. Engine on, sit in Park **~2 min** — pins the
   "100 % display" raw value for `0x3E3 b0` / `0x228 b2` (the anchor this
   drive was missing).
2. Drive as **constant** as possible (steady speed/load) so d(candidate)/d(SOC)
   is clean — now with real `spd~`/`a~` in every mark to correlate against.
3. At the **2-bar mark**, trigger **HOLD** and drive **~10 min** steady. A
   charge-sustaining segment where a genuine SOC field stops falling (or
   rises slightly) is both a strong candidate confirmation and the second
   anchor for the slope.

`--diag-soc` is active TX (UDS `22 005B`, default session, mode-22 only). The
deployed `voltdmf-soclog.service` / the ansible `voltdmf` role still launch
the passive form — add `--diag-soc` to that ExecStart (and bump
`voltdmf_version`) before the drive, or start `soc_log.py` by hand with the
flag. `voltdmf/signals.py` SOC constants stay untouched until an anchor lands.

**Engine-RPM backstop — rejected (owner, 2026-08-30).** An idea to force HOLD
after N minutes of sustained gas-engine RPM is no use here: it fires *after*
depletion, and HOLD is not even selectable once the pack is depleted. The
whole point is prevention before the cliff, so no post-depletion signal can
be the trigger.

---

## Session 7 — 2026-08-30 (in-car — panel-launched SOC capture works end-to-end after three GPIO hand-off fixes)

Short in-car test of Session 6's button helper: the **SW1+SW2 held 5 s →
`voltdmf-soclog.service`** gesture. The launch worked first try, but
`soc_log.py`'s own buttons (A/B gauge, hold-both stop) took three rounds to
come right. No SOC data captured — the point was the button path; the
discharge drive is still to do.

**Round 1 — `5b951da` (did not fix it).** `button_helper._wait_out_unit`
trusted the exit of a polkit `systemctl start` as "the oneshot finished".
Over the unprivileged `User=voltdmf-btn` path that call returns as soon as
the job is *queued*, so the helper re-grabbed SW1/SW2 a few seconds into the
90-min capture and `soc_log.py` came up `buttons off … 'GPIO busy'`. Changed
to poll `systemctl is-active`, added a settle after the release, plus a retry
loop in `soc_log.py`. Still dead on the car.

**Round 2 — `e69cb44` (fixed the buttons).** Root cause: gpiozero's
`Button.close()` drops the Python object but leaves lgpio's `gpiochip`
handle open, so the kernel lines stay claimed by `voltdmf-btn` for the whole
capture — every `soc_log.py` retry hits `'GPIO busy'`. Proved on `voltpi`
against pins 24/23: after `Button.close()` only, a fresh process gets
`error('GPIO busy')`; after `Device.pin_factory.close()` it claims OK. Fix:
`_release_buttons()` now also closes the gpiozero pin factory
(`Device.pin_factory = None`; the next `Button()` rebuilds it), which shuts
the gpiochip fd so the kernel frees every line. `soc_log.py`'s acquire loop
does the same teardown between tries, widened to ~15 s. Also hardened
`_unit_running()` — a transient `systemctl is-active` failure now reads as
"still up" (3 tries), never "finished", so a systemctl hiccup can't make the
helper snatch the lines back mid-capture. **A/B gauge taps and the stop then
worked in the car.**

**Round 3 — `55fe176` (stop gesture no longer leaks a gauge move).** You
can't press both pads on the same millisecond, so whichever landed first
fired its `when_pressed` (`GAUGE-UP`/`DOWN`) before the hold-both stop was
recognized — every stop left a stray gauge line just before `END`. Dropped
the callbacks: the main loop's `tick()` (~5 Hz) reads both pads by
`is_pressed` edge and commits a tap only on *release*, and only if the other
pad was never down during that press. A staggered two-button hold now
records just the stop. State machine unit-tested off-Pi (solo taps,
staggered stop, long-solo-then-stop, sequential taps).

**State.** `voltdmf_version` → `55fe176`, converged with `--tags voltdmf`.
Helper healthy (`LGPIOFactory`, holding BCM 24/23 at idle), daemon up,
`voltdmf_dry_run` stays `true`. The panel-launched SOC capture is now a
clean "hold both 5 s, drive, hold both 3 s to stop"; the SOC discharge drive
itself is the next in-car task.

---

## Session 6 — 2026-08-30 (keyboard + Pi bench — LCD-lock fix landed; `~/vdmf` drift fixed; SOC-drive bench prep complete; reconciler design shift)

No car. `voltpi` on the bench with `can0` up (no bus), the SparkFun 4x20, and
the two PiCAN2 switches attached. Goal: get everything that doesn't need the
car out of the way so the SOC discharge drive is a pure "start it and drive".

**LCD hand-off lock — fix landed and verified.** volt repo `9dc7a77`
(`lcdlock._default_lock_path()` → `/run/lock/voltdmf-lcd.lock`), `voltdmf_version`
bumped to `312ea1d`, converged with `ansible-playbook playbooks/voltpi.yml
--tags voltdmf`. Before this the daemon (user `voltdmf`, `ProtectHome=`) and
`mike`'s tools wrote different paths and the hand-off never fired. Now a bench
`soc_log.py --lcd` run makes the daemon log `LCD claimed by another process;
releasing the panel` ~1 s after `lcdlock.claim()`, then `LCD free again;
resuming the watch screen` the instant the tool exits. `/run/lock/voltdmf-lcd.lock`
is gone afterward. No settle-sleep between `claim()` and the port open is
needed — the race is benign. Full writeup in the "Follow-ups" section below.

**`~/vdmf` clone drift — fixed.** The bench clone had drifted 25 commits
behind `origin/main`, with `tools/soc_log.py` + `tools/mine_capture.py`
present only as *untracked* ad-hoc copies at an unknown version. Refreshed to
`312ea1d` (== the deployed `voltdmf_version`) with `git stash push -u` →
`git fetch` → `git reset --hard origin/main` → `git clean -fd`. Drifted state
kept recoverable: stash `pre-SOC-drift`, plus
`~/vdmf-preSOC-tracked-20260829-234738.patch` and
`~/vdmf-preSOC-untracked-20260829-234738.tgz` on the Pi. The refresh step is
now written into the SOC-discovery-drive procedure (Bench prep block).

**SOC discharge drive — bench prep A1–A4 complete.** Everything not needing
the car is done:
- **A1/A2** clone at `312ea1d`; `soc_log.py --dry-run` clean.
- **A3** `button_check.py`: BCM 24/23 claim clean, idle levels both `up`,
  presses register through the 50 ms debounce, `hold both 3s -> stop gesture
  OK` fires. (`--stop-hold` default is 3 s, not 4.)
- **A4** `soc_log.py --yes --minutes 1 --buttons --lcd`: spawns `candump -l`,
  opens the LCD, logs `GAUGE-DOWN`/`GAUGE-UP` on taps, self-stops at
  `--minutes`, releases the lock; the two-way LCD hand-off works (timings
  above). Expected noise: `speed probe off (No module named 'can')` — system
  Python has no python-can, marks show `spd~n/a`; the raw candump capture is
  the deliverable and still carries `gauge=N/10` on every line.

On the drive itself only procedure steps 1 and 3–6 remain, all in-car. The
other two gates before `voltdmf_dry_run: false` (stationary injection sweep,
`ignition_check.py`) are unchanged.

**Design shift — continuous reconciler (spec only, not built).** Since ignition
is effectively always on (the Pi only has power while the car runs), there is
no ignition edge, so the two edge-triggered strategies (`OnStartTrigger`,
`SocThresholdTrigger`) are being replaced by one level-triggered reconciler:
each loop pass computes `desired_mode(setpoint, soc, floor_latched)` and walks
the car there through the unchanged `SafetyGate` → `ModeCycleController`.
Setpoint is a 2-state toggle HOLD ⇄ MOUNTAIN on one panel button (via a tiny
system-Python `gpiozero` helper calling `voltdmf-ctl setpoint …`); the SOC-HOLD
floor always wins; no persisted state (read-only root + overlayfs coming) so
every boot starts in HOLD. Written up in `DESIGN.md` "Mode policy — continuous
reconciler"; `ignition_check.py` drops off the blocker list. Implementation
waits on the SOC signal. Next: the SOC discharge drive tomorrow.

---

## Session 5 — 2026-08-29 (keyboard + Pi bench — SOC-log tool; daemon LCD watch screen; 0x1F4 → VehicleState)

No car, but the Pi (`voltpi`) was on the bench with `can0` up and the
SparkFun 4x20 + the two PiCAN2 switches attached, so the buttons and the
watch screen were both bench-tested for real. Work on branches
`phase-c-soc-discovery` (the SOC tool, merged) and `phase-c-lcd-watch` (the
watch screen).

### Done

1. **`tools/soc_log.py`** (new, on `phase-c-soc-discovery`, committed +
   pushed). Passive SOC-discovery drive log — the session-4 next-step #1
   tool. **Never transmits.** Start it parked, `--minutes N`, drive the pack
   down; it spawns a full `candump -l` capture (the deliverable) and drops
   `SOC-MARK` time anchors every `--mark-every` s. With `--buttons`, two
   PiCAN2 switch pads SW1 / SW2 (`gpiozero`, BCM 24 / 23 to GND — the
   board's own button option) track the 10-increment dash battery
   gauge hands-free: **A = an increment just dropped, B = it climbed back
   one**. The running level is clamped `[0, --bars]` and stamped absolute on
   every log line (`gauge=7/10`), so no parallel voice memo is needed. Hold
   **both** buttons `--stop-hold` s for a clean stop without SSH. `--lcd`
   mirrors the `t+<s>` clock + live gauge level.

2. **Daemon LCD watch screen** (`phase-c-lcd-watch`, not yet committed).
   `python -m voltdmf` now brings the SparkFun 4x20 up itself in a
   background thread and paints an idle status screen whenever nothing else
   wants the panel:

   ```
   DMF     39s 21:02:54    row 0  uptime + wall clock (proof it's alive)
   mode   NORMAL           row 1  committed mode, 0x1F4 byte 1
   gear P    bus ACTIVE    row 2  PRNDL gear + can0 error state
   DRY on_start MOUNTAIN   row 3  what the fixer is armed to do / last did
   ```

   - `voltdmf/lcddash.py` (new) — `LcdDashboard` thread + a pure
     `render_screen()`. Fail-soft: never raises into the daemon loop, a
     missing/unusable serial port is just a dark screen with one warning.
   - `voltdmf/lcdlock.py` (new) — advisory hand-off lock at
     `/run/lock/voltdmf-lcd.lock` (was `~/.voltdmf-lcd.lock`; `VOLTDMF_LCD_LOCK`
     override), `"<pid>: <what>"`.
     `tools/lcd.py`, `drive_log.py --lcd`, `soc_log.py --lcd` take it; the
     watch thread sees it, closes its port, idles, and resumes on release.
     A lock left by a dead pid is ignored.
   - `python -m voltdmf` gets `--no-lcd` / `--lcd-port` / `--lcd-baud` /
     `--lcd-backlight`; watch mode is the default.

3. **`0x1F4` byte 1 → `VehicleState.drive_mode`** — session-4 next-step #2,
   the `_DecodeListener` half. `0x1F4` added to `signals.is_signal_frame()`
   (also sharpens bus-liveness detection — it streams ~40 Hz) and decoded
   into `state.drive_mode` in the RX loop, so the watch screen shows the
   live committed mode. Wiring `0x1F4` in as `daemon.py`'s
   `current_mode_source` / `menu_cursor_source` for the closed loop is still
   open.

### Bench test on the Pi — both PASS

- **SOC-log buttons.** New `tools/button_check.py` (press/release events,
  hold-both stop gesture, `--scan` to find miswired pins). First runs found
  nothing on the old BCM 5/6 default — the buttons are soldered to the
  **PiCAN2 switch pads: SW1 = BCM 24, SW2 = BCM 23**. Repointed the defaults
  (`--button-a-gpio 24` / `--button-b-gpio 23`, A = gauge-down = SW1) in
  `soc_log.py` + `button_check.py`. Re-test: 6 A-presses / 7 B-presses all
  clean and debounced, both-held stop fired at exactly 3 s, pins read `up`
  at rest.
- **Watch screen.** First bench run painted a **blank panel** — the daemon
  runs `--dry-run` on the Pi (the installed `voltdmf.service` does), and
  that was being passed straight into `SerLcd(dry_run=True)`, so the thread
  only ever built an in-memory image. Fixed: `--dry-run` gates CAN TX only;
  `daemon.py` no longer hands its dry-run flag to `LcdDashboard`, so the
  watch screen always drives the real panel (the dry-run state still shows
  as the `DRY ` tag on row 3). Re-run: all four rows rendered correctly on
  the SparkFun 4x20, `0x1F5` decoded `gear P`, no LCD warnings, clean
  thread start + stop.

### Code / tooling this session

| File | Change |
|---|---|
| `tools/soc_log.py` (new) | Passive SOC drive log: `candump -l` capture spawn, `SOC-MARK` anchors, 2-button 10-increment gauge tracker (BCM 24/23 = PiCAN2 SW1/SW2), both-held stop, optional `--lcd`. Never transmits. |
| `tools/button_check.py` (new) | Bench check for the SOC-log buttons: press/release events, hold-both stop test, `--scan` mode. No CAN, no LCD. |
| `voltdmf/lcddash.py` (new) | `LcdDashboard` background thread + pure `render_screen()` — the daemon's idle watch screen. Fail-soft. Always drives the real panel (not gated by daemon `--dry-run`). |
| `voltdmf/lcdlock.py` (new) | Advisory single-writer LCD hand-off lock (pid file, stale-pid aware, `hold()` context manager). |
| `voltdmf/daemon.py` | Starts/stops `LcdDashboard` by default; `_lcd_status()` callback for row 3 (`DRY ` tag under `--dry-run`); `--no-lcd` and `--lcd-*` knobs via `__main__.py`. Does **not** pass `--dry-run` to the LCD — that flag is CAN-TX only. |
| `voltdmf/canio.py` | `_DecodeListener` decodes `0x1F4` byte 1 into `state.drive_mode`. |
| `voltdmf/signals.py` | `0x1F4` added to `is_signal_frame()`. |
| `tools/lcd.py`, `tools/drive_log.py`, `tools/soc_log.py` | Take the LCD hand-off lock while they own the panel. |
| `tests/test_lcdlock.py`, `tests/test_lcddash.py` (new), `tests/test_canio.py`, `tests/test_signals.py` | Lock round-trip / stale-pid; watch-screen render + yield/resume + real-panel-by-default; `0x1F4` → `drive_mode`; `is_signal_frame(0x1F4)`. Full suite: 116 passed. |

### Wrap-up (end of session 5)

All Phase C work is now on `main`: `phase-c-soc-discovery` and
`phase-c-lcd-watch` merged via `--no-ff` (`c858e6c`), pushed, and all three
`phase-c-*` branches deleted local + remote. 116 tests green on `main`.

### Next session — pick up here

1. **SOC discharge drive** with `tools/soc_log.py` — the real run: pack
   near-full → well down, `--buttons`, then `mine_capture.py --monotonic`
   anchored to the `GAUGE-DOWN` timestamps. This is the one remaining
   unconfirmed signal (`soc`); everything else (`drive_mode_status`,
   `drive_mode_button`, `shift`) is confirmed on-vehicle. **Bench prep A1–A4
   done 2026-08-30 (Session 6) — clone refreshed, buttons + LCD hand-off
   verified on the Pi; only the in-car steps 1, 3–6 of the procedure remain.**
2. **Move the daemon onto the `0x1F4` closed loop** — the RX decode is done;
   still need `daemon.py` to use it as `current_mode_source` /
   `menu_cursor_source` instead of the press-counting tracker. Consider
   `SafetyGate(allow_unknown_shift=False)` now that `0x1F5` decodes.
3. **Make the installed daemon the idle display.** ✅ Done. (a) `/opt/voltdmf`
   redeployed via Ansible (host_vars SHA pin, `6fbf2cd`). (b) service user has
   `SupplementaryGroups=dialout` (`7388119`) — watch screen starts clean.
   (c) hand-off lock moved off `~/.voltdmf-lcd.lock` (daemon runs as `voltdmf`
   with `ProtectHome=`, no shared `$HOME` with `mike`'s tools) to
   `/run/lock/voltdmf-lcd.lock` — `lcdlock._default_lock_path()` prefers
   `/run/lock` (1777 tmpfs), and the unit sets `Environment=VOLTDMF_LCD_LOCK`
   from the new `voltdmf_lcd_lock_path` default to match.
4. Default detached `drive_log.py` / `soc_log.py` runs to `--no-bounce`, or
   fix the `/etc/sudoers.d/50-voltdmf-canlink` match.
5. OBD-II DTC scan.
6. `rm /etc/sudoers.d/50-voltdmf-canlink` on the Pi once field work is done.

---

## Session 4 — 2026-08-29 (first drive — mode-hold PASS on the road; PRNDL decoded)

**Major milestone.** `tools/drive_log.py` ran unattended through a ~9-minute
drive (owner driving, no live SSH). Battery not full (rehearsal run to shake
out the routine), but enough for the mode work.

Setup on the Pi: `~/vdmf` clone + scp'd `voltdmf/{canio,lcd}.py`,
`tools/{drive_log,lcd,mine_capture}.py`. Launched detached with
`setsid nohup … &`. A SparkFun serial 4x20 LCD on the Pi UART
(`/dev/serial0`, `--lcd-backlight 35`) mirrored the shared `t+<s>` clock /
phase / mode / `can0` state and flashed a "SAY" prompt on each SOC-MARK, so
a voice memo of the dash lines up with the log and the capture.

Output pulled home: `vdmf-drivelog-20260829-185232.log` (event log),
`candump-2026-08-29_185234.log` (66.8 MB raw `candump -l`).

### Results

1. **Mode hold — PASS, all four modes, while moving.** `sequence = sport,
   mountain, hold, normal`, `--hold 90`. Each walked through the production
   closed loop, `0x1F4` byte 1 committed at **+2.8 s**, and held the full
   90 s while driving:

   | slot | mode | walk | committed | held 90 s | speed during hold |
   |---|---|---|---|---|---|
   | 1/4 | SPORT | 1 tap | +2.8 s | ✅ | ~21–31 mph |
   | 2/4 | MOUNTAIN | 1 tap | +2.8 s | ✅ | ~31 mph |
   | 3/4 | HOLD | 4 taps | +2.8 s | ✅ | ~21 mph |
   | 4/4 | NORMAL | 1 tap | +2.8 s | ✅ | ~51 mph |

   Confirmed independently by mining the raw capture: `0x1F4` byte 1 =
   `00`/`80`/`20`/`08` for each block, in order, for the whole 90 s.
   **This answers session 3's open question** — MOUNTAIN and HOLD (which
   drift toward NORMAL within ~1 min parked) hold fine while moving.

2. **The HOLD walk needed 4 taps** (cold-menu `index+1`) while SPORT /
   MOUNTAIN / NORMAL took 1 (warm behavior) at near-identical idle gaps.
   Menu-open state isn't fully predictable on the road; the closed loop
   absorbed it every time. `presses_to_reach` stays a dry-run planner only.

3. **PRNDL — decoded.** `0x1F5` byte 3: `1`P `2`R `3`N `4`D `5`L. Parked
   shift routine stepped byte 3 `1→2→3→4→5` in order; the entire moving
   portion held `4` (DRIVE) regardless of speed; a brief `2` (REVERSE)
   during pull-away. `0x135` byte 0 also moves with the shifter but with a
   messier non-sequential encoding — `0x1F5` byte 3 is the clean signal.
   Wrote `decode_shift()`, flipped `shift` → `0x1F5` / `confirmed=True`.

4. **SOC — still open.** `tools/mine_capture.py --monotonic` over the full
   capture found only artifacts: `0x97` (multiplexed frame, mux-blend),
   `0x4C5` byte 2 (2-state flag `0xDD`/`0x49`), `0xB9` (mux counter). The
   dash has no % and 9 minutes barely moves the coarse battery bar. Needs a
   longer discharge drive; the SOC-focused rewrite of `drive_log.py`
   (`tools/soc_log.py`) is the next tool.

5. **No bus degradation.** `can0` ERROR-ACTIVE start to finish. The
   `sudo ip link` bounce at startup failed under `setsid` (no tty for a
   password prompt; the NOPASSWD sudoers rule didn't match) — harmless, the
   bus was already up from boot, but detached runs should default to
   `--no-bounce`.

### Code / tooling this session

| File | Change |
|---|---|
| `voltdmf/lcd.py` (new) | `SerLcd` driver for the SparkFun serial 4x20 on the Pi UART. stdlib `termios`, no pyserial. In-memory screen image; `dry_run` keeps only the image. Backlight control (dimming is the brown-out fix), boot-splash wait, skip-unchanged-row writes, periodic `refresh()`. |
| `tools/lcd.py` (new) | CLI: `--selftest` / `--message` / `--watch` ride-along dashboard. |
| `tools/mine_capture.py` (new) | Offline `candump -l` miner, stdlib, read-only: `--ids`, `--monotonic` (SOC hunt), `--shift-window`, `--series`. |
| `tools/drive_log.py` | Added the parked `--shift-routine` phase, `SOC-MARK` timeline anchors, a full `candump -l` capture spawn, and the `--lcd` mirror. |
| `voltdmf/signals.py` | `decode_shift()` on `0x1F5` byte 3 + `_SHIFT_BY_BYTE3`; `shift` → `0x1F5` `confirmed=True`; `_ALT_SHIFT_ADDR` → `0x135`; `drive_mode_button` → `confirmed=True` (on-road injection PASS). |
| `voltdmf/canio.py` | `_DecodeListener` decodes shift only from `0x1F5` (`0x135` stays a liveness-only signal frame). |
| `tests/test_lcd.py` (new), `tests/test_signals.py` | LCD screen-image tests; real PRNDL decode tests; `test_confirmed_signal_set` now expects `{drive_mode_status, drive_mode_button, shift}`. Full suite: 93 passed. |

### Next session — pick up here

1. **SOC discharge drive** with `tools/soc_log.py` (the SOC-focused rewrite —
   passive, no TX): longer run, pack near-full → well down. Spawns the raw
   `candump -l` capture and, with `--buttons`, tracks the 10-increment dash
   battery gauge from the two PiCAN2 switch pads SW1 / SW2 (BCM 24 / 23 to
   GND, `gpiozero`): **A = an increment just dropped, B = it climbed back one**.
   The tool keeps the running level so every log line has the absolute
   reading (`gauge=7/10`) — no voice memo. `SOC-MARK` anchors every
   `--mark-every` s are the backstop; hold both buttons to stop. Afterward
   `mine_capture.py --monotonic` and anchor the top field to the
   `GAUGE-DOWN` timestamps.
2. **Move the daemon onto the closed loop** (still open from session 3):
   wire `0x1F4` into `daemon.py` RX as `current_mode_source` /
   `menu_cursor_source`; add `0x1F4` to `_DecodeListener`. Consider
   `SafetyGate(allow_unknown_shift=False)` now that `0x1F5` decodes.
3. Default detached `drive_log.py` / `soc_log.py` runs to `--no-bounce`, or
   fix the `/etc/sudoers.d/50-voltdmf-canlink` match.
4. OBD-II DTC scan.
5. `rm /etc/sudoers.d/50-voltdmf-canlink` on the Pi once field work is done.

---

## Session 3 — 2026-08-29 (Phase C.5 — found a live cursor; fixed the overshoot; closed the loop)

Autonomous troubleshooting from the Pi, car in Park / READY, owner not
watching the dash (byte-1 readback stands in for the dash — it always
matched in earlier sessions). Three inject-and-sample scripts on the Pi
(`tools/explore_walk.py`, `explore_frames.py`, `explore_gap.py` — scratch
copies, not committed): a raw socket sampled `0x1F4` while `CanInterface`
injected on `0x1E1` from a second socket.

### What we found

1. **`0x1F4` byte 4 is a LIVE menu cursor.** It steps `00 → 80 → 40 → 20`
   (N/S/M/H) ~40 ms after each button edge, in menu-walk order — *not* the
   "hold-time ramp" an earlier note guessed. Byte 1 (committed mode) still
   only moves on the commit, +3.0 s after the last tap. Byte 5 `0x80` =
   menu open, clears at the commit (flickers mid-walk on the injected path).
2. **The overshoot was tap spacing, not frame count.** `WALK_GAP_S` was
   `0.75 s`. Measured, 2 taps from NORMAL: → SPORT at 1.5–2.0 s spacing,
   → MOUNTAIN at 0.75–1.0 s, → HOLD at 0.5 s. Taps closer than ~1.2 s get
   coalesced / auto-repeated into extra cursor steps. `PRESS_TRACK_FRAMES`
   ∈ {1,2,3,6,16} all walked cleanly at 1.2 s spacing — frame count is not
   the lever.
3. **Menu open point depends on how cold the menu is.** From a *cold* menu
   (car resting in a mode, no recent presses) it opens on NORMAL: tap 1
   opens with no step, taps 2..N step — `index+1` to the target. From a
   *warm* menu (a press within ~3 s of the last commit, e.g. back-to-back
   `set_mode.py` runs) the cursor is already on the current mode and tap 1
   opens *and* steps — taps to target = `(index(target) − index(current))
   mod 4`. The earlier "always opens on NORMAL" was all cold-menu presses.
   The closed loop is immune to the difference (it stops on cursor match);
   the open-loop `presses_to_reach` is only right from a cold NORMAL.
4. **Parked car reverts / rate-limits.** In `explore_gap.py` (~90 taps over
   ~12 min) the cursor kept walking to the target correctly but byte 1
   stopped committing partway through, and `can0` drifted ERROR-ACTIVE →
   PASSIVE → WARNING. Bouncing `can0` and waiting a minute or two returns
   `0x1F4` to a resting `00 00 00 00 00 00` (NORMAL). Real mode-hold
   verification needs the car moving.

### On-car validation of the closed loop (2026-08-29, later)

After fixing a stale-read bug in `read_menu_cursor` (it returned the
*oldest* queued `0x1F4`, not the newest — the RX socket buffers ~40 Hz of
`0x1F4` while `send_mode_button_press` runs and only drains `0x1E1`; the
first run committed MOUNTAIN when asked for SPORT because the loop read a
stale "SPORT" frame while the cursor had already stepped to MOUNTAIN). Fix:
`CanInterface._latest_status` drains the queue to the newest frame; both
`read_drive_mode` and `read_menu_cursor` use it. Covered by
`tests/test_canio.py`.

`tools/set_mode.py --yes-stationary --target <mode>` on the Pi, car in Park
/ READY, five runs:

| from | target | taps | committed (0x1F4 b1 @ +2.8 s) |
|---|---|---|---|
| normal   | sport    | 2 | sport ✓ |
| sport    | mountain | 1 | mountain ✓ |
| mountain | hold     | 1 | hold ✓ |
| hold     | normal   | 1 | normal ✓ |
| normal   | hold     | 4 | hold ✓ |

- **All four modes select and commit correctly.** The closed loop is a
  PASS for selection. `can0` stayed ERROR-ACTIVE across all five runs — no
  degradation (short runs, 1.4 s gap).
- **Tap counts confirm the cold/warm-menu split** (row 1/5 = `index+1` from
  cold NORMAL; rows 2–4 = one relative step from a warm non-NORMAL mode).
- **Parked revert is mode-selective.** SPORT held between runs; the earlier
  MOUNTAIN commit had reverted to NORMAL by the next run. SPORT (throttle
  map) sticks parked; MOUNTAIN / HOLD (battery-management) still need a
  drive to prove they *hold* — selection itself is proven.

### What changed in the code (committed separately)

- `signals.py`: `decode_menu_cursor()` (byte 4), `menu_is_open()` (byte 5),
  documented the real byte-4/5 meaning.
- `canio.py`: `CanInterface.read_menu_cursor()`; `_latest_status()` drains
  the RX queue to the *newest* `0x1F4` (both status reads use it — the
  first-frame version handed the closed loop stale readings).
- `modecycle.py`: `WALK_GAP_S` 0.75 → **1.4**; `switch_to()` now runs a
  **closed loop** when a `menu_cursor_source` is wired — tap, settle, read
  the cursor, stop the instant it is on the target (so a coalesced double
  or a dropped tap only changes how many more taps we send, never an
  overshoot-and-hold). `MAX_WALK_TAPS` (8) cap → `ModeSwitchFailed`. Blind
  `index+1` walk is still the fallback with no cursor source (the daemon).
- `tools/set_mode.py`: wires `read_menu_cursor` as the cursor source;
  `--walk-gap` guard is now `~1.2–2.5 s`; stale `--frames` advice removed.
- `tests/test_modecycle.py`: closed-loop coverage (stop-on-target, dropped
  step recovery, no overshoot on a doubled step, wrap-around, tap cap).

### Next session — pick up here

Stationary selection is validated (table above). What's left:

1. **On a drive**: `tools/set_mode.py --yes-stationary --target <mode>` for
   each of sport / mountain / hold / normal, confirm on the dash, and
   re-check ~30 s later that MOUNTAIN and HOLD *hold* (they reverted
   parked). Then OBD-II DTC scan.
2. If that passes: flip `drive_mode_button.confirmed = True` in
   `voltdmf/signals.py`.
3. **Move the daemon onto the closed loop.** `daemon.py` still uses the
   open-loop `presses_to_reach` + `PressCountingModeTracker`; the on-car
   runs showed that count is only right from a cold NORMAL (warm menu steps
   relative to the current mode). Wire `0x1F4` into the RX path so the
   daemon can pass `read_drive_mode` as `current_mode_source` and
   `read_menu_cursor` as `menu_cursor_source`; add `0x1F4` to
   `signals.is_signal_frame` and the `_DecodeListener`.
4. Housekeeping: `rm /etc/sudoers.d/50-voltdmf-canlink` on the Pi once field
   work is done.

---

## Session 2 — 2026-08-29 (Phase C.5 — injection reproduces the menu walk; not yet an automated PASS)

Car in Park, brake set, full READY. Added a scoped sudoers drop-in on the Pi
(`/etc/sudoers.d/50-voltdmf-canlink`,
`mike ALL=(root) NOPASSWD: /usr/bin/ip link set can0 *`) so the bus can be
cycled remotely; `rm` it when field work is truly done (still present — we
paused, not finished).

### The "blocked on hardware" call from earlier in the session was WRONG

An earlier pass this session diagnosed a marginal PiCAN2 TX solder joint.
The owner rejected that ("Soldering is fine"), which forced a re-diagnosis.
The real causes of the failed injection were all in software/approach:

1. **Wrong ID.** `0x1F4` is the drive-mode **status/echo** (byte 1 = latched
   mode — that part is still confirmed). It is *not* the button input.
   Time-paced frames we transmitted on `0x1F4` collided destructively with
   the body module's own 40 Hz `0x1F4` stream (same arbitration ID, different
   payload → bit errors → TEC ran to ERROR-PASSIVE). That "intermittent TX"
   was **same-ID contention**, not a bad joint.
2. **`0x1E1` is the button after all — it was retired on a shallow look.**
   Session 1 / early Session 2 checked only bytes 0–3 (always `00`). The full
   per-byte diff of a physical press shows **byte 4 bit 7 (`0x80`) = drive-mode
   button pressed**. This is the same ID/bit the Gen 2 prior art
   (`vix597/chevy-volt-trip-mode`) injects.
3. **A static `0x1E1` frame is ignored.** `0x1E1` low 2 bits of byte 4 are a
   rolling counter and bytes 5–6 are a counter-derived tail. A fixed
   `00 00 00 00 80 00 00` reaches the bus but the cluster drops it (only the
   drive-mode screen woke).

### `0x1E1` "ASCMSteeringButton" — captured physical press

7-byte frame, ~40 Hz. Idle: `00 00 00 00 0c YY ZZ` with counter `c` = 0..3 in
byte 4's low bits and `(YY,ZZ)` = `(1C,C0)/(10,F0)/(14,E0)/(18,D0)` for
counter `0/1/2/3`. A **real single press** is just **~14 consecutive frames
with byte 4 bit 7 set** (`83 82 81 80 …` — the counter keeps advancing, tail
still tracks it), then bit 7 clears. ~350 ms total. No change to bytes 0–3.

### Injection method that works — TRACKING ECHO

`voltdmf/canio.py::send_mode_button_press()` rewritten: for
`PRESS_TRACK_FRAMES` (≈16) iterations, wait for the module's next live
`0x1E1`, OR `0x80` into its byte 4, and send that back into the ~24 ms gap
before the module's next frame. Result on the wire: a near-exact replica of
the 14-frame physical press, counter valid and advancing, **no collisions**
(we transmit between the module's frames, never on top of them).

- Across ~10 on-vehicle runs `can0` stayed **ERROR-ACTIVE**, **zero error
  frames**, every tracked frame made it onto the wire (verified by
  `candump can0,1E1:7FF` — e.g. 176/176 across a full cycle).
- The old blind back-to-back blast is gone: long blasts get punched through
  by module frames at random points, so the cluster saw an unpredictable
  0–2 presses per burst.

### Drive-mode MENU model (owner watched the dash and described it)

- **Menu CLOSED** → any single press opens the menu and **selects NORMAL**,
  whatever the current mode is.
- **Menu OPEN** (next press within ~2 s) → each press **walks the cursor**
  `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL → …`.
- **~3 s with no press** → the menu times out, the cursor commits, and the
  next press starts over from NORMAL.

So reaching a mode is a **walk**: `index + 1` rapid presses (NORMAL 1, SPORT
2, MOUNTAIN 3, HOLD 4), each < ~2 s apart, then stop and let it commit. This
also means "reset to NORMAL" for the daemon is a *single* isolated press.

### Result

- **A full injected walk `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL` was
  reproduced and visually confirmed on the dash by the owner** ("the walk
  looked right"), with `can0` ERROR-ACTIVE and zero error frames. The
  `0x1F4` byte-1 timeline for that run stepped `00 → 80 → 20 → 08 → 00` in
  lock-step.
- **Not yet a clean automated PASS.** Two things stop `tools/inject_test.py`
  from self-verifying on a parked car:
  - `0x1F4` byte 1 **lags the commit by ~7–9 s** and, with the car stationary
    in Park, the cluster **reverts toward NORMAL** a few seconds after
    committing a non-NORMAL mode. So byte 1 is an unreliable commit signal
    here — real verification needs a drive (where the modes hold).
  - Walk **timing is sensitive**: the ~400 ms tracking press + `--walk-gap`
    can exceed the ~2 s menu window, so a 4-press walk sometimes times out
    mid-walk and lands short (e.g. on SPORT instead of HOLD). Needs a
    known-good physical-walk capture to tune against.

### Code / tooling changed this session

| File | Change |
|---|---|
| `voltdmf/canio.py` | TX path retargeted `0x1F4` → **`0x1E1`**. `send_mode_button_press()` is now the tracking-echo press (`PRESS_TRACK_FRAMES`, `_next_button_frame()` helper, `can.CanError` back-off, static fallback). `read_drive_mode()` still reads `0x1F4`. Blast/pacing knobs kept only as back-compat aliases. |
| `voltdmf/signals.py` | `drive_mode_button` addr `0x1F4` → `0x1E1`, `confirmed=False`, note rewritten (address confirmed, injection efficacy still unproven). `drive_mode_status` `0x1F4` `confirmed=True` unchanged. |
| `tools/inject_test.py` | Rebuilt around the menu-walk model: `--target <mode>` / `--cycle`, `--walk-gap`, `--commit` (polls `0x1F4`), `--frames`, `--repeat`. Old `--steps` / `--rate-hz` / `--burst-ms` removed. |

Local `pytest`: **59 passed**. No signal-decode behavior changed, so no test
edits were needed.

### Next session — pick up here

1. **Capture a known-good physical walk.** `candump -l 'can0,1E1:7FF,1F4:7FF'`,
   then owner does NORMAL → … → HOLD as 4 fast presses. Measure: the real
   menu-open window, the physical inter-press interval, and how long byte 1
   takes to latch. Set `--walk-gap` (and `PRESS_TRACK_FRAMES` if needed) so
   all 4 injected presses land inside one menu session.
2. Per-mode check through the shipping code:
   `tools/set_mode.py --yes-stationary --target <mode>` (drives the real
   `ModeCycleController.switch_to()` + `canio` TX, then prints the `0x1F4`
   timeline). Owner confirms each on the dash; `can0` must stay ERROR-ACTIVE,
   zero error frames. Use `inject_test.py --cycle` / `--frames` for the wider
   timing sweep.
3. **Real verification is the first dry-run drive** — that is where modes
   actually commit and hold, so the daemon (still `--dry-run`) can be checked
   against `journalctl` while someone else drives. Do the OBD-II DTC scan
   here.
4. When the walk is reliable: bake `PRESS_TRACK_FRAMES` / `RELEASE_GAP_S` /
   the walk-gap into `voltdmf/canio.py` + `voltdmf/modecycle.py`, flip
   `drive_mode_button.confirmed = True`, and record the numbers here.

### `voltdmf/modecycle.py` — menu model applied (keyboard session, 2026-08-29)

Done. `ModeCycleController.switch_to()` no longer does a relative "N presses =
N cursor steps from current mode" walk.

- `presses_to_reach(target)` — signature dropped `current`; returns the
  **absolute** `index(target) + 1` (NORMAL 1, SPORT 2, MOUNTAIN 3, HOLD 4),
  because the menu always reopens on NORMAL.
- `switch_to()` — reads the status source only to no-op when already on
  target / refuse when it is `None`; then fires `index+1` presses `WALK_GAP_S`
  apart. **No synchronous readback** any more (0x1F4 lags a commit ~7–9 s and
  reverts parked, so the old post-send check always false-failed). Callers
  verify against 0x1F4 over the following seconds.
- `MAX_PRESSES_PER_BURST` 3 → `MAX_WALK_PRESSES` 4 (alias kept).
  `BUTTON_PRESS_COOLDOWN_S` → `WALK_GAP_S` (alias kept), re-documented as the
  intra-walk gap with both bounds (< ~1.5 s to stay in the menu window,
  ≥ `canio.RELEASE_GAP_S` for button-up + TEC recovery).
- `PressCountingModeTracker.note_walk(presses)` sets the mode to the
  **absolute** `MODE_CYCLE_ORDER[presses-1]`; `note_presses` kept as the
  alias the daemon wires to `on_presses_sent`.
- `pytest`: 56 passed (relative-walk tests replaced).

Still open: `WALK_GAP_S = 0.75` is a placeholder until the physical-walk
capture (step 1 above); it is not yet re-coupled to `canio.PRESS_TRACK_FRAMES`
/ `RELEASE_GAP_S`, and `drive_mode_button.confirmed` stays `False`.

### Left running / state

- Daemon (`/opt/voltdmf/repo`, systemd) still **`--dry-run`**;
  `voltdmf` + `voltdmf-can0-up` both `active`.
- `can0` up, ERROR-ACTIVE, but **`restart-ms` got reset to 0** at some point
  (a `systemctl` touch of `voltdmf-can0-up`). Next session bring it up with
  `sudo ip link set can0 down && sudo ip link set can0 up type can bitrate
  500000 restart-ms 100` before any TX.
- Dash last seen **SPORT** on `0x1F4` (bouncing NORMAL↔SPORT on the parked
  car's own revert). No new DTCs observed; proper OBD-II scan still owed.
- `~/vdmf` on the Pi = plain clone + scp'd `voltdmf/canio.py` and
  `tools/inject_test.py`.

---

## Session 1 — 2026-08-29 (Phase A + Phase C partial + Phase C.5 started)

### Done

**Phase A — `can0` bring-up: PASS.**

- MCP2515 / PiCAN2 comes up clean at **500 kbit/s** on OBD-II pins 6/14
  (GM Global A HS powertrain bus).
- `ip -details link show can0` → `ERROR-ACTIVE`, error counters at zero, no
  fallback to a lower bitrate needed.
- `candump can0` streams steadily; RX decode path is solid all session.

**Phase C — current drive-mode STATUS signal: CONFIRMED.**

- Method that worked: 20 s stationary dwell in each mode
  (NORMAL → SPORT → MOUNTAIN → HOLD, Park + brake) plus 5 timestamped button
  taps, all in one `candump -l` log, then an offline per-byte
  "steady within a mode / differs across modes" scan.
- **`0x1F4`** is the drive-mode message on the 500k HS bus, 6-byte payload
  `00 <mode> 00 00 <btn_ramp> <btn_down>`, ~40 Hz from a body module.
- **Byte 1 = latched current mode** — 720/720 frames steady in each mode:

  | Mode | `0x1F4` byte 1 |
  |---|---|
  | NORMAL | `0x00` |
  | SPORT | `0x80` |
  | MOUNTAIN | `0x20` |
  | HOLD | `0x08` |

- Byte 5 = `0x80` while the button is physically held, else `0x00`.
- Byte 4 = a decay ramp `0x80 → 0x40 → 0x20` (~0.27 s/step) the button module
  runs *after* release; the mode latches ~2–3 s later.
- Secondary echoes: `0x287` byte 1 tracks mode (`00/80/08/10`); `0x3D1` byte 0
  only separates SPORT/MOUNTAIN. Not needed — `0x1F4` byte 1 is authoritative.
- The earlier failure was the wrong candidate ID: `0x1E1` (from the Gen 2
  reference project) does not exist on this bus, and diffing on a running car
  was too noisy. `0x1E1` is fully retired now.

**Phase C.5 — injection: STARTED, not passed.**

- Our injected `0x1F4` frames **do reach the cluster** — every shot woke the
  drive-mode screen on the dash. The car listens to us.
- Got the mode to actually move: one shot from NORMAL landed on HOLD
  (three steps) with the byte4 ramp included.

### Findings

1. **The press is duration-gated, not edge-gated.**
   - ~0.3 s of `byte5=0x80` → only wakes the screen, no mode change.
   - ~1.2 s (down-hold + the 3-value byte4 ramp) → counted as ~3 presses.
   - Working theory: the consumer counts one press per ~0.4 s that the button
     reads active, i.e. it auto-repeats like a held key.
2. **The byte4 release ramp auto-repeats when injected** — each of
   `0x80 / 0x40 / 0x20` lands as another press. So we do **not** reproduce it;
   `PRESS_RAMP_VALUES` is now empty.
3. **Transmitting is hard on this MCP2515 while RX stays perfectly clean.**
   - The controller walks `ERROR-ACTIVE → ERROR-PASSIVE → BUS-OFF` over a
     handful of injection shots. BUS-OFF count went 0 → 3 across the session.
   - `restart-ms 0` (what the `voltdmf-can0-up` unit currently sets) means no
     auto-recovery, so later shots fired into a dead interface (thousands of
     TX drops).
   - RX-clean + TX-degrading points at the **physical layer** — most likely a
     marginal solder joint on the CAN-H / CAN-L tap. Reflow when the Pi is
     next out of the car.
   - Compounding factor: the real body module keeps streaming its 40 Hz idle
     `0x1F4` while we inject, and an over-aggressive flood (200 Hz / 250+
     frames) both looked like one solid press *and* couldn't keep the bus
     alive.
4. The car kept dropping out of READY during the session (bus goes quiet →
   our TX gets no ACK → TEC runs away → BUS-OFF). Needs the car in **full
   READY** for every injection attempt, not just accessory/RUN.

### Mitigations applied this session

- Manual bring-up with recovery: `sudo ip link set can0 up type can bitrate
  500000 restart-ms 100` (self-heals BUS-OFF in 0.1 s).
- Dialed injection down; made rate / duration / ramp tunable from the CLI
  (`tools/inject_test.py --rate-hz --down-ms --no-ramp`).
- Added a pre-flight + per-shot guard to `inject_test.py`: it refuses to
  transmit unless `can0` is `ERROR-ACTIVE`, and aborts the run if the state
  degrades mid-sequence.

### Code changes committed this session

| File | Change |
|---|---|
| `voltdmf/signals.py` | `drive_mode_status` → `0x1F4`, `confirmed=True`. `drive_mode_button` → `0x1F4` (address confirmed, injection unproven). New `decode_drive_mode()` (byte-1 lookup) + `_DRIVE_MODE_BY_BYTE1`. |
| `voltdmf/canio.py` | Single TX path retargeted to `0x1F4`. New press model: one solid `byte5=0x80` block, **100 Hz, 0.45 s, no byte4 ramp**. New `read_drive_mode()` RX helper. `_tx_frame` / `*_UNCONFIRMED` constants removed. |
| `tools/inject_test.py` | Rewritten for on-vehicle use: before/after mode read-back per shot, `can0` health guard, `--presses/--gap/--shots/--settle/--rate-hz/--down-ms/--ramp` knobs. |
| `tests/test_signals.py` | Dropped `test_nothing_is_confirmed_yet`; added `test_confirmed_signal_set`, `test_decode_drive_mode`, `test_decode_drive_mode_unknown_and_short`. |
| `docs/signals-confirmed.md` | Status signal section filled in; button/injection section rewritten with the Phase C.5 findings. |

Local `pytest`: **59 passed**.

### Post-session — reference-project review (`vix597/chevy-volt-trip-mode`)

Compared our approach against the Gen 2 prior art. It solved an easier
problem (different message `0x1e1`, no hold-timer ramp) but its *structure*
is the fix for both our symptoms:

- **One press = a short burst of identical "button down" frames, then STOP
  transmitting.** It never sends a release frame — the real steering
  module's own idle `0x1e1` frames are the "button up". No ramp.
- **`BUTTON_PRESS_COOLDOWN = 0.75 s` between bursts.** Multi-step switches
  are discrete bursts 0.75 s apart, never a continuous hold. That gap both
  separates one counted press from the next (our overshoot) and gives the
  transmit-error counter room to recover (our BUS-OFF).
- It blind-counts with a 60 s cooldown and no status signal; we have
  `0x1F4` byte 1, so we can close the loop instead.

**Applied to the code (committed this session, on the branch):**

- `voltdmf/canio.py` — `send_mode_button_press()` is now burst-and-release:
  `PRESS_BURST_FRAMES` frames of byte5=0x80 at `PRESS_FRAME_INTERVAL_S`,
  then return, no release frame. New `RELEASE_GAP_S = 0.75` (callers must
  leave that much silence before the next press). `PRESS_RAMP_*` /
  `PRESS_IDLE_FRAMES` gone.
- `tools/inject_test.py` — rewritten closed-loop: reads `0x1F4` after every
  press, prints the transition + `can0` state, stops when the dash reaches
  the goal or hits `--max-presses`. `--steps N` (advance N single steps) or
  `--target <mode>`. Knobs: `--burst-ms`, `--rate-hz`, `--gap` (floored at
  `RELEASE_GAP_S`), `--settle`, `--step-confirm`.

### Next session — pick up here — SOC discovery drive

Goal: find the message ID + byte offset that carries pack state-of-charge on
this Gen 1 bus, and enough points to calibrate `SOC_KWH_PER_COUNT` /
`GEN1_PACK_USABLE_KWH` in `voltdmf/signals.py`. `0x206` (the Gen 2 candidate)
is absent here, so this is a from-scratch hunt and it needs a real discharge:
start the drive with a high battery gauge and run it down several increments.

The Gen 1 dash shows **no SOC %** — only EV range (miles) and a 10-increment
battery gauge. `tools/soc_log.py` is the capture tool: it's **passive** (never
transmits), spawns `candump -l` for the raw log, and takes two panel buttons
so the driver can mark each gauge-increment change without looking away.

**Bench prep (at a keyboard, before the drive) — refresh the `~/vdmf` clone.**
`~/vdmf` is a by-hand rsync/scp target and drifts: on 2026-08-29 it was
25 commits behind `origin/main` with `tools/soc_log.py` + `tools/mine_capture.py`
present only as *untracked* ad-hoc copies at an unknown version, so their CLI
didn't match this writeup. It must match the SHA the deployed daemon runs
(`voltdmf_version` in the ansible host_vars — `312ea1d` as of 2026-08-30).
Fix it and keep the drifted state recoverable:
```
ssh voltpi 'cd ~/vdmf && \
  git stash push -u -m pre-SOC-drift && \
  git fetch -q origin && git reset --hard origin/main && git clean -fd && \
  git rev-parse --short HEAD && git status -s'
```
Expect `HEAD` = the deployed pin, `git status -s` empty, and
`tools/{soc_log,mine_capture,ignition_check}.py` all tracked. Recover the old
state with `git stash pop` if any of that diff ever matters (it didn't —
intermediate work already committed + pushed in the 25 commits). Then a
no-hardware sanity check:
```
cd ~/vdmf && /usr/bin/python3 tools/soc_log.py --dry-run --minutes 90 --buttons --lcd
```
Done 2026-08-29: clone now at `312ea1d`, dry-run clean. Backups on the Pi:
`~/vdmf-preSOC-tracked-20260829-234738.patch`,
`~/vdmf-preSOC-untracked-20260829-234738.tgz`.

**Bench-verified on the Pi 2026-08-30 (car not present, `can0` up but no
bus):**
- `tools/button_check.py` — BCM 24/23 claim clean, idle levels both `up`;
  presses register through the 50 ms debounce; `hold both 3s -> stop gesture
  OK` fires.
- `tools/soc_log.py --yes --minutes 1 --buttons --lcd` — spawns `candump -l`,
  opens the LCD, logs `GAUGE-DOWN`/`GAUGE-UP` on button taps, self-stops at
  `--minutes`, releases the hand-off lock. LCD hand-off works both ways with
  no settle-sleep: daemon logs `LCD claimed by another process; releasing the
  panel` ~1 s after `lcdlock.claim()`, then `LCD free again; resuming the
  watch screen` the instant `soc_log` exits; `/run/lock/voltdmf-lcd.lock`
  gone afterward. Expected noise: `speed probe off (No module named 'can')` —
  system Python has no python-can, so marks show `spd~n/a`; the raw candump
  capture is the deliverable and carries gauge level on every line anyway.
So on the drive itself, only steps 1 and 3–6 remain.

1. `can0` is already up from boot (500k, `restart-ms 100`) — nothing to bring
   up. Quick check over SSH before pulling away: `ip -br link show can0` = UP.
2. Start the logger from the bench copy, using the **system** Python (it has
   `gpiozero`; the venv doesn't):
   ```
   cd ~/vdmf && /usr/bin/python3 tools/soc_log.py --yes --minutes 90 --buttons --lcd
   ```
   - `--buttons`: SW1 (BCM 24) = gauge dropped one increment, SW2 (BCM 23) =
     gauge rose one. Press the moment the dash gauge ticks.
   - `--lcd`: mirrors status to the 4x20 so the driver can confirm it's
     logging without a laptop. Add `--bars-start N` if the gauge isn't full
     at key-on.
   - `--mark-every` drops a periodic SOC-MARK anchor (default 120 s) even
     when the gauge is steady — leave it on.
   - It self-stops after `--minutes`; or hold BOTH buttons `--stop-hold` s
     when parked at the end.
3. Drive to discharge: get the gauge down by at least 4–5 increments (charge-
   sustaining hills or a longer flat run both work). Don't touch the Pi while
   moving — the buttons and the periodic marks are the whole interface.
4. Back at a keyboard, pull the logs (`~/candump-<ts>.log` and the
   `~/vdmf-soclog-<ts>.log` event log) and mine them:
   ```
   tools/mine_capture.py ~/candump-<ts>.log --ids
   tools/mine_capture.py ~/candump-<ts>.log --monotonic --top 25
   tools/mine_capture.py ~/candump-<ts>.log --series <ID>:<OFF>:<W>[:le] --every 20
   ```
   Cross the `--monotonic` candidates against the button/MARK timestamps in
   the event log: the SOC field should step down in step with the gauge
   presses and hold flat between them. Confirm the raw→kWh (or raw→%) scale
   from two well-separated marks.
5. Set `SOC_KWH_PER_COUNT` / `GEN1_PACK_USABLE_KWH` (or a direct percent
   decode) in `voltdmf/signals.py`, flip `soc.confirmed = True`, add a
   `decode_soc` + wire it into `_DecodeListener` / `is_signal_frame`, and
   record the ID/offset/scale here.
6. Same drive, spare time: run `tools/ignition_check.py` at engine-off to
   settle the ignition-behavior question (bus-quiet delay, mode persistence
   across a key cycle).

### Deferred — stationary injection validation (burst-and-release)

Can run as a ~10 min warm-up in Park at the **start** of the SOC drive (it
clears the last injection gate), or stay its own session. Not blocking the
SOC hunt either way.

1. **Car in full READY**, foot on brake, Park. Confirm `candump can0` streams
   steadily *before* touching anything.
2. Redeploy to the Pi:
   ```
   scp voltdmf/canio.py    voltpi.haguehome.lan:~/vdmf/voltdmf/
   scp tools/inject_test.py voltpi.haguehome.lan:~/vdmf/tools/
   ```
   (`~/vdmf` on the Pi is a plain clone with our files scp'd over; the systemd
   daemon at `/opt/voltdmf/repo` is untouched and still dry-run.)
3. `can0` now comes up from boot at 500k **with `restart-ms 100`** (ansible
   converge 2026-08-29, `665ed5d`) — no manual `ip link` needed. Confirm:
   ```
   ip -details link show can0     # expect: can state ERROR-ACTIVE, restart-ms 100
   ```
   If you *do* bounce it, re-add `restart-ms 100` by hand; a plain
   `systemctl restart voltdmf-can0-up` is now fine too (the unit carries it).
4. Second shell: `candump can0 | grep -iE 'err'`.
5. Closed-loop sweep — one step at a time, watching the dash and `can0`:
   ```
   /opt/voltdmf/venv/bin/python ~/vdmf/tools/inject_test.py \
       --yes-stationary --steps 1 --burst-ms 450
   ```
   - Overshoots (one run advances >1 step) → lower `--burst-ms` (350, 300).
   - Wake-only / "no change" → raise `--burst-ms` (550) or add `--rate-hz 150`.
   - Once single steps are clean, do a full lap: `--steps 4` should return to
     the start in exactly 4 presses, `can0` staying `ERROR-ACTIVE`, zero
     error frames. Or target a specific mode: `--target hold`.
6. When a config wins: set `voltdmf/canio.py` `PRESS_BURST_FRAMES` /
   `PRESS_FRAME_INTERVAL_S` to the winning values, flip
   `drive_mode_button.confirmed = True`, and record the numbers here.
7. OBD-II DTC scan; clear only DTCs we caused and understand.

### Still needs a real drive (deferred, not attempted)

- **SOC signal** — `0x206` never appears on this bus (Gen-2 ID is wrong for
  Gen 1). This is now the headline next session — see "SOC discovery drive"
  above (`tools/soc_log.py` capture → `tools/mine_capture.py --monotonic`).
- **Shift / PRNDL** — ✅ RESOLVED on the road (session 4). `0x1F5` byte 3 is
  the PRNDL detent (1 PARK … 5 LOW): stepped 1→5 in order through a parked
  P-R-N-D-L walk, then held 4 (DRIVE) for the whole 9-minute drive.
  `signals.decode_shift` is live; `SafetyGate` now blocks on `UNKNOWN` by
  default (`allow_unknown_shift=False`, commit `4013ea3`). `0x135` byte 0
  also tracks the shifter but with a messier non-sequential encoding — left
  undecoded.
- **Ignition behavior** — does the bus go quiet with the car off and how
  long after; is the drive mode remembered or reset to NORMAL on restart.
  Run `tools/ignition_check.py`.

### Follow-ups for "back at a keyboard" (no car needed)

- ✅ **DONE — commit `4013ea3`.** `voltdmf/daemon.py` now reads current mode
  straight off the bus: `current_mode_source = lambda: state.drive_mode`,
  replacing `PressCountingModeTracker`. `ASSUMED_START_MODE` and the
  `on_presses_sent` wiring are gone. `state.drive_mode` is `None` until the
  first `0x1F4` frame, which `ModeCycleController` turns into
  `ModeUnknownError` → no injection — the fail-safe on a bus we can't observe.
- ✅ **DONE — already in `c9acafa`.** `0x1F4` is in `signals.is_signal_frame`
  and the RX `_DecodeListener` decodes byte 1 into `state.drive_mode`.
- ✅ **DONE — commit `4013ea3`.** `SafetyGate` default `allow_unknown_shift`
  flipped `True` → `False`. `decode_shift` (`0x1F5` byte 3) is confirmed, so
  an `UNKNOWN` shift now means a short/garbled frame and blocks injection.
  Bench rigs without a `0x1F5` stream pass `allow_unknown_shift=True`.
- ✅ **DONE — homelab-ansible `onboard-voltpi` @ `665ed5d`.**
  `voltdmf-can0-up.service.j2` now sets `restart-ms {{ voltdmf_can_restart_ms }}`
  on the `type can` line; new `voltdmf_can_restart_ms: 100` default. Applies
  Converged onto voltpi 2026-08-29 with
  `ansible-playbook playbooks/voltpi.yml --tags voltdmf`; `can0` now boots
  `ERROR-ACTIVE restart-ms 100`, no hand `ip link` needed.
- ✅ **DONE — homelab-ansible `onboard-voltpi` @ `c76c592` + `6fbf2cd`,
  converged + verified on voltpi 2026-08-29.** `roles/voltdmf` now deploys the
  runtime control socket: `voltdmf.socket` (systemd-activated
  `/run/voltdmf/control.sock`, `0660 voltdmf:voltdmf`), `voltdmf.service` gains
  `Requires=/After=voltdmf.socket` + `RuntimeDirectory=voltdmf`, and a
  `/usr/local/bin/voltdmf-ctl` symlink. `voltdmf-ctl status` works over the
  socket; daemon logs `control socket up (inherited fd)`, still
  `armed=False, dry_run=True`. Operators in the `voltdmf` group get
  `voltdmf-ctl status | set-mode <m> | arm | disarm | reload` with no sudo.
- ✅ **DONE — `6fbf2cd`.** Bumped `voltdmf_version` in
  `inventories/production/host_vars/voltpi.haguehome.lan/vars.yml` to
  `380e924` (confirmed-signals + control-socket `main`) — done early, out of
  order, because the control socket needs `voltdmf/control.py`. This also
  unstuck `/opt/voltdmf/repo`, which the `git` task had left frozen at the
  2026-08-28 scaffold (`voltdmf_version` must be a full SHA, not `main`).
  `voltdmf_dry_run` stays `true`.
- ✅ **DONE — homelab-ansible `onboard-voltpi` @ `7388119`, converged +
  verified on voltpi 2026-08-30.** Fixes item 3(b) above and a latent
  control-socket bug:
  - `voltdmf.service` gains `SupplementaryGroups=dialout` (new
    `voltdmf_service_supplementary_groups` default), so the daemon can open
    `/dev/serial0` for the LCD watch screen. Log now shows `LCD watch screen
    thread started` with no `Permission denied` after it; process `Groups:
    20 984`. No change to the system user or the device mode.
  - `RuntimeDirectoryPreserve=yes` on **both** `voltdmf.service` and
    `voltdmf.socket`. Deploying the `dialout` change exposed it: a bare
    `systemctl restart voltdmf.service` (the `Restart voltdmf` handler)
    removed + recreated the shared `/run/voltdmf`, deleting the endpoint
    `voltdmf.socket` had bound — the `.socket` unit stays active and keeps
    handing the daemon its inherited fd, so the daemon looks fine, but
    `voltdmf-ctl` gets `FileNotFoundError` (no path left to connect to). The
    socket-rebind handler chain only runs when the `.socket` template
    changes, so a service-only restart slipped past. Preserve keeps the dir
    (and the socket file) across a restart of either unit; the socket's
    `RemoveOnStop=true` still clears the file on a real `.socket` stop.
  - Item 3(c) — LCD hand-off lock. `lcdlock.LOCK_PATH` defaulted to
    `~/.voltdmf-lcd.lock`, but the daemon (`voltdmf`, `ProtectHome=`) and the
    login user's tools share no writable `$HOME`, so the tool wrote
    `/home/mike/...` while the daemon watched `/opt/voltdmf/...` — hand-off
    never fired. Now `lcdlock._default_lock_path()` prefers
    `/run/lock/voltdmf-lcd.lock` (1777 tmpfs, both can reach it), `$HOME`
    dotfile only as an off-Pi fallback; `VOLTDMF_LCD_LOCK` still overrides.
    The unit sets `Environment=VOLTDMF_LCD_LOCK` from the new
    `voltdmf_lcd_lock_path` ansible default so the two stay pinned together.
    ✅ **Landed on voltpi 2026-08-30** — volt repo `9dc7a77` (lcdlock
    `_default_lock_path()`), `voltdmf_version` bumped to `312ea1d` and
    converged with `--tags voltdmf`. Verified: installed `lcdlock.LOCK_PATH`
    = `/run/lock/voltdmf-lcd.lock`, `VOLTDMF_LCD_LOCK` in `/proc/<pid>/environ`,
    and a bench `soc_log.py --lcd` run drives the daemon to log `LCD claimed
    by another process; releasing the panel` then `LCD free again` on exit —
    the hand-off now actually fires (see Session 6).

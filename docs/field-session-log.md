# On-Vehicle Field Session Log

Running narrative of the field sessions against the real car. Pairs with
`phase-c-field-checklist.md` (the procedure) and `signals-confirmed.md` (the
decoded-signal reference). Newest session first.

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
   MOUNTAIN / NORMAL took 1 (warm behaviour) at near-identical idle gaps.
   Menu-open state isn't fully predictable on the road; the closed loop
   absorbed it every time. `presses_to_reach` stays a dry-run planner only.

3. **PRNDL — decoded.** `0x1F5` byte 3: `1`P `2`R `3`N `4`D `5`L. Parked
   shift routine stepped byte 3 `1→2→3→4→5` in order; the entire moving
   portion held `4` (DRIVE) regardless of speed; a brief `2` (REVERSE)
   during pull-away. `0x135` byte 0 also moves with the shifter but with a
   messier non-sequential encoding — `0x1F5` byte 3 is the clean signal.
   Wrote `decode_shift()`, flipped `shift` → `0x1F5` / `confirmed=True`.

4. **SOC — still open.** `tools/mine_capture.py --monotonic` over the full
   capture found only artefacts: `0x97` (multiplexed frame, mux-blend),
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
   `candump -l` capture, drops `SOC-MARK` anchors every `--mark-every` s, and
   with `--buttons` takes two panel buttons on the Pi header (GPIO 5 / 6 to
   GND, `gpiozero`) for hand marks the instant the EV range or a bar ticks
   (`BTN-RANGE` / `BTN-BAR`); hold both to stop. Narrate EV-range /
   battery-bar into a voice memo on every mark, then `mine_capture.py
   --monotonic` and anchor the top field to the readings.
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

Local `pytest`: **59 passed**. No signal-decode behaviour changed, so no test
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

### Next session — pick up here

1. **Car in full READY**, foot on brake, Park. Confirm `candump can0` streams
   steadily *before* touching anything.
2. Redeploy to the Pi:
   ```
   scp voltdmf/canio.py    voltpi.haguehome.lan:~/vdmf/voltdmf/
   scp tools/inject_test.py voltpi.haguehome.lan:~/vdmf/tools/
   ```
   (`~/vdmf` on the Pi is a plain clone with our files scp'd over; the systemd
   daemon at `/opt/voltdmf/repo` is untouched and still dry-run.)
3. Bring the bus up **with** auto-recovery — do **not** use
   `systemctl restart voltdmf-can0-up` (that unit sets `restart-ms 0`):
   ```
   sudo ip link set can0 down
   sudo ip link set can0 up type can bitrate 500000 restart-ms 100
   ip -details link show can0     # expect: can state ERROR-ACTIVE
   ```
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
  Gen 1). Needs a discharge drive with `tools/watch_soc.py` to find the real
  ID + scaling for `SOC_KWH_PER_COUNT` / `GEN1_PACK_USABLE_KWH`.
- **Shift / PRNDL** — `0x135` bytes 0–6 constant in Park (byte 7 is a free
  counter), `0x1F5` static. Nothing to decode without moving the shifter.
  Keeps `SafetyGate(allow_unknown_shift=True)` for now.
- **Ignition behaviour** — does the bus go quiet with the car off and how
  long after; is the drive mode remembered or reset to NORMAL on restart.
  Run `tools/ignition_check.py`.

### Follow-ups for "back at a keyboard" (no car needed)

- Wire `signals.decode_drive_mode` into `voltdmf/daemon.py` as
  `current_mode_source`, replacing `PressCountingModeTracker`.
- Add `0x1F4` to `signals.is_signal_frame` and the RX `_DecodeListener`
  (left out on purpose this session to keep the running daemon untouched).
- homelab-ansible `roles/voltdmf`: change
  `templates/voltdmf-can0-up.service.j2` to `restart-ms 100` and add a
  `voltdmf_can_restart_ms` default — an in-car CAN device must self-recover
  from BUS-OFF.
- Once injection passes: bump `voltdmf_version` in
  `inventories/production/host_vars/voltpi.haguehome.lan/vars.yml` to the
  confirmed-signals commit. Keep `voltdmf_dry_run: true`.

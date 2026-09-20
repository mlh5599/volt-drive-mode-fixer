# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status (2026-09-05, through session 12):** the range-extender engine is now
readable too -- `0x4C5` byte 2 with `0x3F9` bytes 1-2 as corroboration, both
`confirmed=True`, mined offline from the existing captures (section below,
`docs/analysis/session12-engine-signal.md`). SOC was resolved in session 9 as
the `22 005B` UDS poll, not a broadcast frame (`docs/analysis/session9-soc-anchor.md`);
the paragraph below predates both and is kept as the session-8 snapshot.

**Status (2026-08-30, through session 8):** `drive_mode_button` (`0x1E1`),
`drive_mode_status` (`0x1F4` byte 1) and `shift`/PRNDL (`0x1F5` byte 3) are
`confirmed=True` in `voltdmf/signals.py` and validated on-road; `0x1F4` byte 1
is the daemon's current-mode source and `SafetyGate` runs with
`allow_unknown_shift=False`. **SOC is the one signal still open.** The
Session-8 full-drain drive (10/10 → 0/10, all bar-drops hand-marked) narrowed
it to three energy-linked broadcast candidates — `0x3E3` b0/b1/b6, `0x228`
b2, `0x186` b6 — and excluded four elapsed-time counters, but logged no
reading at a known SOC, so none can be scaled yet. See the "SOC" section
below, `docs/field-session-log.md` Session 8, and
`docs/analysis/session8-soc-candidates.md`. Ignition behavior is still
unobservable (key-off kills Pi power).

**Session-4 milestone (2026-08-29, first drive):** `tools/drive_log.py` ran
unattended through a 9-minute drive: the closed-loop injection (`0x1E1`
tracking echo) walked to all four modes, each **committed in 2.8 s and held
the full 90 s while moving** (21–51 mph), `can0` ERROR-ACTIVE throughout.
Confirmed twice over — the daemon's own `0x1F4` byte-1 readback and an
independent mine of the raw `candump` capture. This closed the session-3 open
question: MOUNTAIN and HOLD, which drift toward NORMAL within ~1 min *parked*,
do **not** revert while moving.

This file is the Phase C deliverable (DESIGN.md). Fill it in from the
`tools/` output, then update `voltdmf/signals.py` / `voltdmf/canio.py` and
flip the `confirmed` flags.

Signal IDs and scaling can vary by model year and market. Record enough to
reproduce the decode; do not put vehicle-identifying details (VIN, plate) in
a checked-in file.

| Field | Value |
|---|---|
| Vehicle | 2014 Chevy Volt (Gen 1), Premier/Premium trim |
| Bus | OBD-II pins 6/14, HS-CAN, **500** kbit/s (Phase A: ERROR-ACTIVE, zero error counters, no fallback) |
| Date tested | 2026-08-29 |
| Tool used | `candump -l` + `tools/analyze` offline scan; `tools/mode_diff.py` / `cycle_modes.py` earlier (inconclusive on a running car) |
| Method | 20 s stationary dwell per mode (Park, brake set) + 5 timestamped taps, one candump log, per-byte "steady within a mode / differs across modes" scan |

## SOC  (NOT CONFIRMED — narrowed to 3 candidates in session 8; still needs a scaling anchor)

- `0x206` **never appears** on this bus (re-confirmed 0 frames in the
  Session-8 213 MB capture). The Gen-2 SOC ID is wrong for Gen 1.
- **Session 4** (9-minute drive): `mine_capture.py --monotonic` surfaced only
  artifacts (`0x97` mux blend, `0x4C5` b2 2-state flag, `0xB9` mux counter) —
  too short to move the coarse battery bar.
- **Session 8** (full-drain drive, 10/10 → 0/10, all ten bar-drops
  hand-marked): a **timer-rejection test** (per-segment value-delta vs
  wall-clock duration, over the nine segments between consecutive drops)
  separated energy-paced fields from elapsed-time counters. Full write-up +
  regenerable charts: `docs/analysis/session8-soc-candidates.md`.
  - **Excluded — elapsed-time counters** (`r ≈ 0.85–0.97`): `0x4CB`, `0x3DD`,
    `0x137`, `0x4E9`. These fooled the earlier `--monotonic` runs.
  - **SOC candidates** (`r ≈ 0`; all flatten through the mid-drive
    turnaround):
    - `0x3E3` **bytes 0/1/6** — triple-redundant 8-bit, 201 → 161 over the
      drain, `r ≈ 0.00`. Strongest. (`0x3E3` b2 & b4 rise-then-fall →
      probably pack temperature.)
    - `0x228` **byte 2** — 96 → 79, `r ≈ −0.14`, cleanest monotonicity but
      coarse (≈ 1.5 counts/bar).
    - `0x186` **byte 6** @ 80 Hz — 130 → 63, finest resolution but noisy
      (±8); briefly regen-ticked *up* at the turnaround.
    - `0x2C7` b1–2 — V-shaped (sags **and** recovers): pack voltage under
      load, not a charge count. Excluded.
- **No scaling anchor yet.** No reading at a known SOC was logged. The GM
  Volt RE wiki carries battery charge % only as **diagnostic PID `22 005B`**
  (`raw · 100/255`), not a broadcast field.
- **Next — the Session-9 anchor drive** (`docs/phase-c-field-checklist.md`
  §2b): start at a full charge, engine-on Park idle ~2 min (pins the "100 %"
  raw value), drive a steady pace, then ~10 min in HOLD from the 2-bar mark
  (charge-sustaining second anchor). `soc_log.py` now stamps every
  candidate's raw byte on each line and, with `--diag-soc` (its one transmit
  path, now part of the run), polls `22 005B` every ~10 s for a continuous
  known-SOC reference to fit against.
- Chosen candidate: `______`  ·  Byte layout: `______` (offset, width, endianness)
- Raw -> percent: `______` (scale / offset)  ·  Calibration points (raw : `22 005B` % : bars):
  - `______`
- => `voltdmf/signals.py`: SOC decode + `soc.confirmed = True`;
  `hold_threshold_percent` / `hold_reset_percent` from the raw value at the
  2-bar / 3-bar crossings.

## Drive-mode button press (TX) -- `0x1E1` byte 4 bit 7  (CONFIRMED 2026-08-29, session 4 — injection PASS on the road)

- **`0x1E1` "ASCMSteeringButton"** -- 7-byte frame, streamed by a module at
  ~40 Hz. This is the button INPUT; `0x1F4` (above) is only the status echo.
  Same ID/bit the Gen 2 prior art (`vix597/chevy-volt-trip-mode`) injects.
- Idle frame: `00 00 00 00 0c YY ZZ`.
  - byte 4 low 2 bits = a rolling counter `0..3`
  - bytes 5–6 = a counter-derived tail: counter `0/1/2/3` ->
    `(1C,C0)/(10,F0)/(14,E0)/(18,D0)`
  - bytes 0–3 = always `00`
- **A real physical press = only ~14 consecutive frames with byte 4 bit 7
  set** (`83 82 81 80 …` — the counter keeps advancing, the tail keeps
  tracking it), then bit 7 clears. ~350 ms total. Nothing else in the frame
  changes. (The earlier "`0x1E1` is not the button" call was made by
  diffing only bytes 0–3 — wrong.)
- **The cluster mostly counts button edges** — but a *held* button
  auto-repeats the menu and taps closer than ~1.2 s coalesce into extra
  steps (session-3 sweeps). Treat "1 tap = 1 step" as true only for
  well-spaced isolated taps; otherwise close the loop on the byte-4 cursor.

### Drive-mode MENU model (owner watched the dash + on-car injection, 2026-08-29)

- **Cold menu** (no press for a while) → the first press opens the menu on
  **NORMAL** with no step, whatever the committed mode. Further presses walk
  the cursor `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL → …`. Reaching a
  mode from cold is `index + 1` presses (NORMAL 1, SPORT 2, MOUNTAIN 3,
  HOLD 4).
- **Warm menu** (a press within ~3 s of the last commit — e.g. back-to-back
  `set_mode.py` runs) → the cursor is already on the committed mode and the
  first press opens *and* steps. Presses to target =
  `(index(target) − index(current)) mod 4`. Confirmed on-car: SPORT→MOUNTAIN,
  MOUNTAIN→HOLD, HOLD→NORMAL each took **one** tap.
- **One step per press** only when presses are ~1.2–2.5 s apart (tighter
  coalesces into extra steps; see the byte-4 cursor section).
- **~3 s idle** → menu times out, cursor commits.
- So the press *count* is context-dependent — **close the loop on the byte-4
  cursor** instead of trusting a count. The open-loop `presses_to_reach`
  (`index + 1`) is only correct from a cold NORMAL; the daemon's
  "single press = reset to NORMAL" assumption holds only from a cold menu
  and needs the closed loop once `0x1F4` is wired into RX.

### Injection -- TRACKING ECHO (`voltdmf/canio.py::send_mode_button_press`)

- For `PRESS_TRACK_FRAMES` (~16) iterations: wait for the module's next live
  `0x1E1`, OR `0x80` into its byte 4, send it back into the ~24 ms gap
  before the module's next frame. Result on the wire: a replica of the
  ~14-frame physical press with a valid advancing counter, landing *between*
  module frames — no same-ID collisions. Caller leaves `RELEASE_GAP_S`
  (0.75 s) of silence, then the next press.
- On-vehicle result: `can0` stayed **ERROR-ACTIVE** with **zero error
  frames** across ~10 runs; every tracked frame reached the wire (verified
  by `candump can0,1E1:7FF`). **A full injected `NORMAL → SPORT → MOUNTAIN →
  HOLD → NORMAL` walk was visually confirmed on the dash by the owner**, with
  `0x1F4` byte 1 stepping `00 → 80 → 20 → 08 → 00` in lock-step.
- What did NOT work: transmitting on `0x1F4` (same-ID contention with the
  module → TEC → ERROR-PASSIVE); a static `0x1E1` frame with a frozen
  counter (cluster ignores it, only the menu screen woke); a blind
  back-to-back blast of a frozen echo (module frames punch through it at
  random points → 0–2 presses per burst, unpredictable).

### `0x1F4` bytes 4+5 — LIVE menu cursor (CORRECTED 2026-09-03, session 10)

**The cursor is ONE field spanning bytes 4 and 5.** Established from a
2432-frame `0x1F4` capture across a full menu walk. Byte 5 bit 7 is not a
"menu open" flag — it is the NORMAL cursor code. All 403 frames with that bit
set carried byte 4 `00`, zero exceptions; a frame with byte 4 ≠ 0 *and* bit 7
set does not occur on the wire.

| b4 | b5 | cursor |
|---|---|---|
| `00` | `80` | **NORMAL** |
| `80` | `00` | **SPORT** |
| `40` | `00` | **MOUNTAIN** |
| `20` | `00` | **HOLD** |
| `00` | `00` | **menu CLOSED** — no cursor (439 frames) |

Byte 1 is unchanged and independent: the *committed* mode (`00` N `80` S
`20` M `08` H), moving only on commit, +3.0 s after the last tap.

Why this matters: **byte 4 alone cannot tell NORMAL from a closed menu** —
both are `00`. That single ambiguity produced three wrong fixes in a row,
each correct against the other's failure and wrong on its own:

- `0652b38` gated `state.menu_cursor` on byte-5 bit 7, so mid-walk (cursor on
  SPORT/MOUNTAIN/HOLD, bit 7 clear) the cursor read `None` at nearly every
  check and every non-NORMAL walk-test leg hit `ModeSwitchFailed`.
- `03daa48` ungated it and read byte 4 alone, so a *closed* menu read as
  NORMAL — a stale-looking match that can stop a walk early.
- Both premises were artifacts of decoding byte 4 in isolation. So were the
  earlier "hold-time ramp" and "flickering menu-open hint" readings.

Decoders: `signals.decode_menu_cursor()` (bytes 4+5, `None` = closed),
`signals.menu_is_open()`; reader `CanInterface.read_menu_cursor()`.
`_DecodeListener` assigns `state.menu_cursor` **unconditionally** — the `None`
is real information (the menu shut), and latching the last value instead would
let a stale cursor match a walk target.

**Walk timing (re-measured 2026-09-03, `tools/press_calibrate.py`)**

- Menu **always opens on NORMAL**, from any latched mode — confirmed 5/5 by
  `press_calibrate.py model`. So `presses_to_reach(target)` is
  `index(target) + 1` from *any* mode, and the open-loop walk was correct all
  along. The closed loop buys early exit and a hard failure on a dropped tap,
  not correctness.
- **One step per tap at ≥ ~1.2 s spacing.** `WALK_GAP_S` = **1.4 s** (clean
  window ~1.2–2.5 s); at 0.75 s taps coalesced into extra steps.
- **Frame count matters after all** — the cluster **key-repeats** a held
  button, with a measured repeat period of **~50 ms**. Steps per press:

  | frames | steps per tap | notes |
  |---|---|---|
  | 16 | ~5–6 | the old `PRESS_TRACK_FRAMES`; the dominant bug |
  | 8 | 2 | |
  | 2 | 1, but **double-steps ~1 in 8** | 3/24 across all runs |
  | **1** | **1, 22/22** | never a double; ~5–10% dropped |

  The session-3 note here previously claimed {1,2,3,6,16} were all
  equivalent; that sweep only ever checked the first step of a N→S→M walk,
  which a key-repeat overshoot can still satisfy. The 16-frame press is why
  the menu spun at 50–75 ms/step and why a restore leg burned 8 taps without
  byte 1 ever leaving HOLD.

  The 2-frame double-step was caught live by the probe's dense sampler: on
  one tap the cursor read `mountain` at t=3.520 and `hold` at t=3.571 — 51 ms
  apart, i.e. exactly one key-repeat. Two frames (~50 ms of "held") sits
  right on the repeat boundary.

- **`PRESS_TRACK_FRAMES` is 1, and that is a deliberate trade.** One frame is
  occasionally *dropped* (~5–10%). That is the right failure: a dropped tap
  is visible to the closed loop — the cursor simply did not move — and costs
  one extra tap out of `MAX_WALK_TAPS` (8, against a 4-tap worst case), and
  the loop's 1.6 s per iteration stays inside the ~3 s menu-open window so
  nothing commits early. An overshoot commits the **wrong mode** and is not
  recoverable the same way. Never overshoot; let the closed loop absorb the
  drops. This is the one thing the closed loop genuinely buys — see the
  open-loop note above.

### On-road validation (2026-08-29, session 4 — `tools/drive_log.py`)

- **Mode hold: PASSES for all four modes.** `sequence = sport, mountain,
  hold, normal`, `--hold 90`. Every mode walked through the production
  closed loop, committed at **+2.8 s**, and `0x1F4` byte 1 stayed on it for
  the full 90 s while driving (SPORT ~21–31 mph, MOUNTAIN ~31, HOLD ~21,
  NORMAL ~51). Independent cross-check: mining the raw capture, `0x1F4`
  byte 1 = `00`/`80`/`20`/`08` for each block, in order.
- **This settles the session-3 open item.** MOUNTAIN and HOLD hold while
  moving; their parked drift toward NORMAL is stationary-only.
- **Walk tap counts on the road:** SPORT 1, MOUNTAIN 1, HOLD 4, NORMAL 1.
  The HOLD walk took the full cold-menu count (`index+1 = 4`) while the
  other three behaved warm (1 tap) at near-identical idle gaps — menu-open
  state is *not* fully predictable on the road, but the closed loop landed
  every time. Keep the closed loop; `presses_to_reach` stays a dry-run
  planner only.
- **No bus degradation.** `can0` ERROR-ACTIVE start to finish; no BUS-OFF /
  PASSIVE / WARNING over the full 9-minute session (4 walks + holds). The
  session-2 rate-limit was ~90 taps over ~12 min — this run sent ~7.
- **`can0` bounce under `setsid` failed** (`sudo: a terminal is required`;
  the NOPASSWD sudoers rule didn't match). Harmless here — `can0` was
  already up from boot — but detached runs should default to `--no-bounce`.

### Next

1. ~~Flip `drive_mode_button.confirmed = True`~~ — **done** (session 4).
2. ~~Wire `0x1F4` into the daemon RX path as the current-mode source~~ —
   **done** (`4013ea3`): `_DecodeListener` decodes `0x1F4` byte 1 into
   `state.drive_mode`, the daemon's `current_mode_source` is
   `lambda: state.drive_mode`, and `PressCountingModeTracker` was removed.
3. ~~`SafetyGate(allow_unknown_shift=False)` in the daemon~~ — **done**
   (`4013ea3`): that is now the `SafetyGate` default and the daemon takes it.
4. OBD-II DTC scan (still owed — no scan tool on the drive).
5. **SOC** — the Session-9 anchor drive (see the "SOC" section above), then
   set the decode + thresholds in `voltdmf/signals.py`. Last blocker on a
   non-dry-run daemon.

### `voltdmf/modecycle.py` state (session 3)

- `presses_to_reach(target)` = `index + 1`. Correct only from a **cold
  NORMAL** menu — on-car runs showed a warm menu steps relative to the
  current mode. Still fine as the open-loop / dry-run planner; the real
  path is the closed loop.
- `switch_to()` runs the **closed loop** when a `menu_cursor_source` is
  wired (`set_mode.py` does): tap → `CURSOR_SETTLE_S` → read cursor → stop
  on match, else `WALK_GAP_S` and tap again; `MAX_WALK_TAPS` (8) cap →
  `ModeSwitchFailed`. Falls back to the blind `index+1` walk with no cursor
  source (the daemon, for now).
- `WALK_GAP_S` = **1.4** (was 0.75). Not coupled to
  `canio.PRESS_TRACK_FRAMES` — the sweep showed frame count is not the
  lever.

## Current-mode status -- `0x1F4` byte 1  (CONFIRMED 2026-08-29)

- 6-byte message, ~40 Hz. Byte 1 = latched drive mode, 720/720 frames steady
  in each mode across a full NORMAL->SPORT->MOUNTAIN->HOLD walk:

  | Mode | `0x1F4` byte 1 |
  |---|---|
  | NORMAL | `0x00` |
  | SPORT | `0x80` |
  | MOUNTAIN | `0x20` |
  | HOLD | `0x08` |

- Secondary cross-check: `0x287` byte 1 also tracks mode (`00`/`80`/`08`/`10`).
  `0x3D1` byte 0 only separates SPORT (`0x41`) / MOUNTAIN (`0x21`) -- ignore.
- Implemented: `voltdmf/signals.decode_drive_mode()` (byte 1 lookup).
- **Wired into the daemon** (`4013ea3`): `_DecodeListener` decodes `0x1F4`
  byte 1 into `state.drive_mode`; the daemon's `current_mode_source` reads
  that; `PressCountingModeTracker` is gone.

## Shift / PRNDL -- `0x1F5` byte 3  (CONFIRMED 2026-08-29, session 4)

- Frame **`0x1F5`**, ~40 Hz. **Byte 3 = PRNDL detent**, a small enum:

  | byte 3 | position |
  |---|---|
  | `0x01` | PARK |
  | `0x02` | REVERSE |
  | `0x03` | NEUTRAL |
  | `0x04` | DRIVE |
  | `0x05` | LOW |

- Evidence: in the parked `--shift-routine` phase byte 3 stepped
  `1 → 2 → 3 → 4 → 5` in order, ~4–5 s apart (a deliberate slow P-R-N-D-L
  walk), then returned to `1` (Park). For the **entire 9-minute moving
  portion** of the drive byte 3 held `4` (DRIVE), independent of speed. It
  also caught a brief `2` (REVERSE) during pull-away — backed out before
  going forward.
- Rest-of-frame: bytes 0–1 co-vary with the detent but look like a
  counter/checksum (`0f 0d` / `0e 0a` / `0d 0d` / `0a 0a`), not decoded;
  bytes 4–7 idle (`00 00 08 00`, byte 6 wobbles `08`/`09`/`0a`).
- `0x135` byte 0 *also* tracks the shifter (`0/3/1/2` over the walk) but the
  encoding is non-sequential and noisier — `0x1F5` byte 3 is the clean one.
  `0x135` is kept in `is_signal_frame` (RX liveness) but not decoded.
- Implemented: `voltdmf/signals.decode_shift()` (byte-3 lookup),
  `_SHIFT_BY_BYTE3`; `SIGNAL_IDS["shift"]` → `0x1F5`, `confirmed=True`;
  `_ALT_SHIFT_ADDR` → `0x135`. `_DecodeListener` decodes only `0x1F5`.
- **Done** (`4013ea3`): `allow_unknown_shift=False` is now the `SafetyGate`
  default and the daemon takes it, so injection is blocked on UNKNOWN / short
  `0x1F5` as well as on any non-DRIVE detent.

## Range-extender engine -- `0x4C5` byte 2 + `0x3F9` bytes 1-2  (CONFIRMED 2026-09-05, offline)

Found without a drive, by mining the three captures already in `captures/`:
two of them contain a labelled engine start (session 8's full drain into
forced charge-sustaining, session 9's driver-selected HOLD leg) and the third
is EV-only, the negative control. Full write-up with the timings and the
speed cross-reference: `docs/analysis/session12-engine-signal.md`. Regenerate
any of it with `tools/engine_check.py <capture> [--since <epoch>]`.

- **`0x4C5`**, ~2 Hz, 5 bytes. **Byte 2 is the only byte that ever changes**;
  the other four are always `00`.

  | byte 2 | frames (all 3 captures) | meaning |
  |---|---|---|
  | `0x49` | 6 309 | off -- the car is an EV |
  | `0x59` / `0x87` / `0xB5` | 4 each | the ~1.5 s ramp between states |
  | `0xDD` | 1 414 | on an engine leg |

  The ramp values appear at **both** edges (session 9 has a
  `0xDD → 0xB5 → 0x87 → 0x59 → 0x49` shutdown), so they decode to
  `EngineState.TRANSITION`, not "starting". `0xDD` means *on an engine leg*
  rather than *crank turning right now*: it held for the whole 701 s of the
  session-9 leg, including five stretches where the engine was not burning
  fuel. That is the wider reading and the one this project wants -- the menu
  entry is missing across those gaps too.

- **`0x3F9`**, ~4 Hz, 8 bytes. **Bytes 1-2 big-endian are a monotone
  accumulator**; byte 0 was `0x00` in all 15 478 frames (left out of the
  decode on purpose -- an unrelated flag byte there would read as a false
  "engine running"), bytes 3-7 are a near-static `28 51 59 89 6x`.
  **0 steps** across the whole EV-only capture and both EV legs; 160 steps in
  session 8's 40 s engine tail, 2 349 in session 9's leg. Rate 4-159
  counts/s, median 60 -- unit unpinned and unused, only *did it change
  recently* is read.

  It tracks **fuelling, not rotation**: it leads `0x4C5` by 32.8 s (session
  8) / 41.9 s (session 9) at the head of a leg, and freezes for 12.7-43.3 s
  mid-leg. One of those freezes lines up exactly with a 70 mph → standstill →
  35 mph decel on `0x3E9` -- overrun fuel cut, then a stop.

- **Read as a union** (`VehicleState.engine_running`): a moving counter
  overrides an `off` `0x4C5`, and a `running` `0x4C5` stands on its own.
  Neither alone covers a leg, and they are blind in opposite places. Both
  errors of the union are in the safe direction (slightly early, slightly
  late), and the cost is only a mode walk that waits.

- **Why it exists.** On the 2026-09-04 drive the pack was spent, the car came
  up with the engine running and stuck in NORMAL, and the centre stack stopped
  offering HOLD/MOUNTAIN entirely -- so the reconciler walked the menu, failed,
  and retried every 10 s cooldown for the rest of the drive. Session 8 caught
  that exact state on the wire: engine on at the 0-bar mark with `0x1F4` byte 1
  still `0x00` (NORMAL). Session 9's leg, by contrast, ran with byte 1 at
  `0x08` (HOLD) -- same engine, complete menu, because HOLD was selected
  before the pack was written off.

- Implemented: `signals.decode_engine_state()` / `decode_engine_run_counter()`
  (+ `EngineState`, both `confirmed=True` in `SIGNAL_IDS`, both in
  `is_signal_frame`); `state.VehicleState.engine_running` /
  `engine_counter_advancing()` (tri-state -- "not watched long enough yet"
  never reads as "off"); `reconciler.charge_sustaining_block()`, used by both
  `Reconciler.desired_mode` and `SafetyGate._precondition_failure`;
  `voltdmf-ctl status` prints the engine read and any `NOT ACTING:` reason;
  the LCD SOC row gains an `ICE` / `NO HOLD` tag.

## Ignition on/off -- `0x3ED` (presence)  (CONFIRMED 2026-09-19, session 14)

- **Bus goes quiet with car off?** *Not always* -- the finding that started
  this hunt (Session 13, same day): a real ignition-off without opening a
  door leaves the cluster, the Pi, and cluster-related traffic
  (`0x1E1`/`0x1F4`/`0x1F5`/`0x3E9`/`0x4C5`/`0x3F9`) all live through a
  retained-accessory-power-like window ("RAP"). `state.bus_active` cannot
  tell RAP from true ignition-on, because it is fed by that same traffic.
- **The signal:** arbitration ID `0x3ED`. Present with a constant payload
  `80 00 00 00 00 FF` whenever the ignition is on. The *entire ID* stops
  arriving -- not a byte within it changing -- the instant the ignition goes
  off, and stays absent through the RAP window while the rest of the bus
  above keeps transmitting unchanged. Presence is the signal; there is no
  payload field to decode.
- **Method:** in-car ON -> OFF (no door open, into RAP) -> ON capture,
  coordinated live over chat (owner operating the ignition, reporting state
  changes) rather than through `tools/ignition_diff.py`'s interactive
  prompts directly -- captured with ad-hoc `candump` snapshots over SSH and
  diffed by hand (same appear/disappear-ID logic `ignition_diff.py`
  implements). Reproduced across **two independent ON/OFF/ON cycles** before
  being treated as confirmed, per this project's usual bar.
- **Not yet measured:** `0x3ED`'s own transmit period -- the Pi dropped off
  the network mid-session before a frame-rate check could run.
  `IGNITION_QUIET_TIMEOUT_S` (`voltdmf/state.py`) borrows
  `BUS_QUIET_TIMEOUT_S` (2.0 s) as a documented placeholder pending that
  measurement.
- Implemented: `signals.IGNITION_ADDR` / `SIGNAL_IDS["ignition"]`
  (`confirmed=True`, in `is_signal_frame`); `state.VehicleState.ignition_on`
  (fails **closed** on ignorance, unlike `engine_running`'s fail-open union
  -- see the property's docstring); `_DecodeListener` marks it in
  `canio.py`; `SafetyGate._precondition_failure()` gates both the
  reconciler's auto-walk and manual `set-mode` on it; the daemon hands the
  retry budget back on ignition-loss the same as bus-quiet; the LCD's
  `_bus_tag()` reports a distinct `"RAP"` state. Full write-up:
  `docs/field-session-log.md` Session 14.

## Charge current setpoint — 8 A / 12 A Level 1  (stretch goal — NOT CONFIRMED)

Goal: force 12 A 120 V charging (`DESIGN.md` → "Stretch goal — force 12 A
Level 1 charging"). The center-stack setting reverts to 8 A on every Park
exit; the owner always charges at home on a known-good circuit.

- Capture method: charge-mode prelude on the SOC drive — see
  `docs/phase-c-field-checklist.md` §2e. Toggle 8↔12 A a few times in Park,
  then shift to Drive (forced revert). Correlate the offline
  `mine_capture.py --shift-window` transitions against `0x1F5` byte 3 leaving
  `0x01` (Park).
- Setpoint frame ID: `______`
- Byte / offset / encoding: `______` (expected `0x08`↔`0x0C` A, or
  `0x28`↔`0x3C` in 0.2 A units)
- Rolling counter / checksum in the frame? `______`
- HMI re-assert cadence (→ single post-revert TX vs continuous): `______`
- OVMS reference: charger *telemetry* (read-only) is `0x5EC` B2 charging
  current @ 0.2 A, B3 charging voltage @ 2 V, via a `0x7E4` diag request; no
  published *command* for the user 8/12 A selection.
- => injection lives in `voltdmf/canio.py` behind the same `--dry-run` gate;
  not started.

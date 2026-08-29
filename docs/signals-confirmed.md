# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status (2026-08-29, session 4 — first drive): the drive-mode fix is
validated on-road.** `tools/drive_log.py` ran unattended through a 9-minute
drive: the closed-loop injection (`0x1E1` tracking echo) walked to all four
modes, each **committed in 2.8 s and held the full 90 s while moving**
(21–51 mph), `can0` ERROR-ACTIVE throughout. Confirmed twice over — the
daemon's own `0x1F4` byte-1 readback and an independent mine of the raw
`candump` capture both show byte 1 latched at N/S/M/H for each 90 s block.
This closes the open question from session 3: MOUNTAIN and HOLD, which drift
toward NORMAL within ~1 min *parked*, do **not** revert while moving. The
parked reversion is a stationary-only behaviour.

`drive_mode_button` (`0x1E1`), `drive_mode_status` (`0x1F4` byte 1) and
`shift`/PRNDL (`0x1F5` byte 3, new this session — `1`P `2`R `3`N `4`D `5`L)
are now `confirmed=True` in `voltdmf/signals.py`. **SOC is still open** — the
monotonic-frame scan over this capture found only mux-blend / 2-state-flag
artefacts; the dash shows no %, and 9 minutes is too short to move the
coarse battery bar. Needs a longer discharge drive. Ignition behaviour is
still unobservable (key-off kills Pi power).

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

## SOC -- `0x206`  (NOT CONFIRMED — still open after session 4)

- `0x206` **never appears** on this bus. The Gen-2 SOC ID is wrong for Gen 1.
- Session 4 drive: `tools/mine_capture.py --monotonic` over the full 66 MB
  capture surfaced no credible battery-energy field. Top hits were all
  artefacts:
  - `0x97` — a **multiplexed** frame; the scanner blended muxes into a
    fake "flat then ramp to exactly 0 at capture end" shape.
  - `0x4C5` byte 2 — a **2-state flag** (`0xDD`/`0x49`), not a gauge; bucket
    averaging made it look monotonic.
  - `0xB9` — a rolling mux counter.
- Why nothing stuck: this dash has **no SOC %**, only EV-range miles + a
  coarse battery bar, and a 9-minute drive barely moves one bar. The
  `SOC-MARK` timeline anchors fired at +60.7, +126, +181, +240, +302, +362,
  +421, +480, +542 s; the owner's voice memo of range/bars at those marks
  was not yet transcribed.
- Next: a **longer discharge drive** (take the pack from near-full to well
  down) with `tools/soc_log.py`, then re-run `mine_capture.py --monotonic`
  and anchor the top field against the narrated range/bar readings.
- Byte layout: `______` (offset, width, endianness)
- Raw -> value: `______` (scale / offset)
- Calibration points (raw value : dash EV range mi : battery bars):
  - `______`
- => `voltdmf/signals.py`: `SOC_KWH_PER_COUNT = ___`, `GEN1_PACK_USABLE_KWH = ___`

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

### `0x1F4` byte 4 — LIVE menu cursor (CONFIRMED 2026-08-29, session 3)

Two Pi-side injection sweeps (`explore_frames.py`, `explore_gap.py`; a raw
socket sampled `0x1F4` while `CanInterface` injected on `0x1E1`):

| byte | role | codes |
|---|---|---|
| **byte 1** | committed mode; moves only on commit, +3.0 s after last tap | `00` N `80` S `20` M `08` H |
| **byte 4** | **live menu cursor**; steps ~40 ms after each button edge | `00` N `80` S `40` M `20` H |
| **byte 5** | menu-open hint (`0x80` open); clears at the commit; flickers mid-walk on the injected path | `00` / `80` |

- Menu **always opens on NORMAL** (seen from HOLD, MOUNTAIN, SPORT starts).
  Tap 1 opens (cursor → NORMAL, no step); taps 2..N step one row each.
- **One clean step per tap when taps are ≥ ~1.2 s apart.** Measured
  overshoot at tighter spacing: 2 taps from NORMAL → SPORT at 1.5–2.0 s,
  → MOUNTAIN at 0.75–1.0 s, → HOLD at 0.5 s. `WALK_GAP_S` was **0.75** —
  that was the "steps too far" bug. Now **1.4 s** (clean window ~1.2–2.5 s).
- **Frame count did not matter**: `PRESS_TRACK_FRAMES` ∈ {1,2,3,6,16} all
  walked N→S→M cleanly at 1.2 s spacing.
- New decoders: `signals.decode_menu_cursor()`, `signals.menu_is_open()`;
  new reader `CanInterface.read_menu_cursor()`.

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
2. Wire `0x1F4` into the daemon RX path so `daemon.py` uses `read_drive_mode`
   as `current_mode_source` and `read_menu_cursor` as `menu_cursor_source`
   (the closed loop) — the open-loop count is only right from a cold NORMAL.
   Currently only `set_mode.py` / `drive_log.py` run the closed loop.
3. OBD-II DTC scan (still owed — no scan tool on the drive).

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
- TODO: wire it as `current_mode_source` in `voltdmf/daemon.py` (replacing the
  press-counting tracker) and add `0x1F4` to `is_signal_frame` / the RX listener.

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
- Follow-up: `voltdmf/daemon.py` can now construct
  `SafetyGate(allow_unknown_shift=False)` once `0x1F5` is confirmed live on
  the daemon's RX path (it already is in `is_signal_frame`). Left as a
  deliberate daemon change, not done here.

## Ignition behaviour (from `ignition_check.py`)  (NOT CONFIRMED)

- Bus goes quiet with car off? `yes / no`
- Mode resets to NORMAL on ignition, or remembered? `______`
- Seconds from "on" to first frames: `______`
- => sets `ASSUMED_START_MODE` validity in `voltdmf/daemon.py`

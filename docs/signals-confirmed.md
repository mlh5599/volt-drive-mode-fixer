# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status (2026-08-29, session 3): the drive-mode menu now has a LIVE cursor
readback — `0x1F4` byte 4 (`00` N / `80` S / `40` M / `20` H) steps ~40 ms
after every button edge, distinct from byte 1 (the committed mode, which
only moves on the ~3 s commit). `voltdmf/modecycle.py` closes the loop on
it: tap, read the cursor, stop when it is on the target. The old
"steps too far" overshoot was `WALK_GAP_S` at 0.75 s — on-car sweeps showed
taps closer than ~1.2 s coalesce into extra cursor steps; frame count
(1..16) did not matter. `WALK_GAP_S` is now 1.4 s. A second bug: the cursor
read returned the OLDEST queued `0x1F4`, not the newest — fixed
(`_latest_status` drains the RX queue). With both fixes the closed loop
**selected and committed all four modes correctly on the parked car**
(sport/mountain/hold/normal, `can0` ERROR-ACTIVE throughout). Still parked:
MOUNTAIN and HOLD (battery modes) reverted toward NORMAL after the commit —
a drive is needed to prove they *hold*, not to select them. SPORT held.
SOC, shift, ignition still need a drive.**

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

## SOC -- `0x206`  (NOT CONFIRMED)

- `0x206` **never appears** on this bus. The Gen-2 SOC ID is wrong for Gen 1.
- Needs a real discharge (a drive) with `tools/watch_soc.py` to find the
  actual ID and scaling.
- Byte layout: `______` (offset, width, endianness)
- Raw -> value: `______` (scale / offset)
- Calibration points (raw value : dash % : dash kWh):
  - `______`
- => `voltdmf/signals.py`: `SOC_KWH_PER_COUNT = ___`, `GEN1_PACK_USABLE_KWH = ___`

## Drive-mode button press (TX) -- `0x1E1` byte 4 bit 7  (ADDRESS confirmed; injection efficacy not yet a clean PASS)

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

### What the parked car still can't tell us

- **Selection: PASSES.** The closed loop selected and committed all four
  modes on the parked car (see field log). `can0` stayed ERROR-ACTIVE.
- **Mode hold: unproven for MOUNTAIN / HOLD.** `0x1F4` byte 1 lags the
  commit ~3 s, and in Park the cluster reverts MOUNTAIN and HOLD toward
  NORMAL within a minute or so (battery-management modes with engagement
  conditions). SPORT held. Whether MOUNTAIN / HOLD *keep* needs a drive.
- **Cluster rate-limit / TEC** (from the earlier sweep, not the validation
  runs): ~90 taps over ~12 min drove `can0` ERROR-ACTIVE → PASSIVE →
  WARNING and commits stopped landing. Keep runs short, bounce `can0`
  between them, wait for `0x1F4` to rest at `00 00 00 00 00 00`. The
  five short validation runs showed none of this.

### Next

1. **On a drive**: `tools/set_mode.py --yes-stationary --target <mode>` for
   each mode, confirm on the dash, re-check MOUNTAIN / HOLD ~30 s later,
   OBD-II DTC scan.
2. If every mode holds on a drive: flip `drive_mode_button.confirmed = True`
   (`WALK_GAP_S` / cursor codes are already baked into `signals.py` /
   `modecycle.py`).
3. Wire `0x1F4` into the daemon RX path so `daemon.py` uses `read_drive_mode`
   as `current_mode_source` and `read_menu_cursor` as `menu_cursor_source`
   (the closed loop) — the open-loop count is only right from a cold NORMAL.
   Currently only `set_mode.py` runs the closed loop.

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

## Shift / PRNDL -- `0x135` / `0x1F5`  (NOT CONFIRMED)

- In Park, `0x135` bytes 0-6 are constant (`00 00 1c 76 8a 0c 1a`); byte 7 is
  a free-running counter. `0x1F5` is static. Nothing to decode without moving
  the shifter.
- Needs a drive: `candump 'can0,135:7FF' 'can0,1F5:7FF'` while moving P-R-N-D-L.
- Address / byte / values: `______`
- => implement `voltdmf/signals.decode_shift()`; then set
  `SafetyGate(allow_unknown_shift=False)` in `voltdmf/daemon.py`

## Ignition behaviour (from `ignition_check.py`)  (NOT CONFIRMED)

- Bus goes quiet with car off? `yes / no`
- Mode resets to NORMAL on ignition, or remembered? `______`
- Seconds from "on" to first frames: `______`
- => sets `ASSUMED_START_MODE` validity in `voltdmf/daemon.py`

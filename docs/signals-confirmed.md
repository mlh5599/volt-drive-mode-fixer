# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status (2026-08-29, session 2): current-mode STATUS signal confirmed
(`0x1F4` byte 1). The button INPUT is `0x1E1` byte 4 bit 7 (an earlier call
this session that `0x1E1` "is not the button" was wrong — it only checked
bytes 0–3). Injection by "tracking echo" (reply to each live `0x1E1` in its
gap with bit 7 set) reproduced a full owner-confirmed drive-mode menu walk
with `can0` ERROR-ACTIVE and zero error frames — but Phase C.5 is NOT a
clean automated PASS yet: on a parked car `0x1F4` byte 1 lags the commit and
reverts to NORMAL, and multi-press walk timing is not run-to-run
consistent. SOC, shift, and ignition behaviour still need a drive.**

This file is the Phase C deliverable (DESIGN.md). Fill it in from the
`tools/` output, then update `voltdmf/signals.py` / `voltdmf/canio.py` and
flip the `confirmed` flags.

Signal IDs and scaling can vary by model year and market. Record enough to
reproduce the decode; do not put vehicle-identifying details (VIN, plate) in
a checked-in file.

| Field | Value |
|---|---|
| Vehicle | 2014–2015 Chevy Volt (Gen 1), Premier/Premium trim (exact year 2014 or 2015, owner unsure — both are late Gen 1, identical powertrain) |
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
- **The cluster counts button edges, not hold time** (confirmed via `0x1F4`
  earlier: a 3 s hold = one step, a 0.6 s triple-tap = three).

### Drive-mode MENU model (owner watched the dash, 2026-08-29)

- **Menu CLOSED** → any single press opens the menu and selects **NORMAL**,
  whatever the current mode.
- **Menu OPEN** (next press within ~2 s) → each press walks the cursor
  `NORMAL → SPORT → MOUNTAIN → HOLD → NORMAL → …`.
- **~3 s idle** → menu times out, cursor commits, next press restarts at
  NORMAL.
- So reaching a mode is a **walk**: `index + 1` presses < ~2 s apart
  (NORMAL 1, SPORT 2, MOUNTAIN 3, HOLD 4), then stop. "Reset to NORMAL" for
  the daemon is a single isolated press.

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

### Why Phase C.5 is not a clean automated PASS yet

- `0x1F4` byte 1 **lags the commit ~7–9 s** and, on a stationary car in
  Park, the cluster **reverts toward NORMAL** a few seconds after committing
  a non-NORMAL mode. So `tools/inject_test.py` can't reliably self-verify a
  parked walk — real verification needs a drive (modes hold there).
- Walk **timing is sensitive**: the ~400 ms tracking press + `--walk-gap`
  can exceed the ~2 s menu window, so a 4-press walk sometimes times out
  mid-walk and lands short (SPORT instead of HOLD).

### Next

1. Capture a known-good **physical** 4-press walk
   (`candump -l 'can0,1E1:7FF,1F4:7FF'`) to measure the real menu-open
   window, inter-press interval, and byte-1 latch delay.
2. Tune `--walk-gap` / `PRESS_TRACK_FRAMES` so all 4 injected presses land
   in one menu session; re-run `inject_test.py --cycle`, owner confirms each
   step, `can0` ERROR-ACTIVE / zero error frames.
3. Real verification on the first dry-run drive; OBD-II DTC scan there.
4. Then bake the numbers into `canio.py` + `modecycle.py` and flip
   `drive_mode_button.confirmed = True`.
- `voltdmf/modecycle.py` also needs the menu model — its `switch_to()` still
  assumes "N presses = N steps from the current mode".

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

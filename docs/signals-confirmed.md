# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status (2026-08-29): current-mode STATUS signal confirmed on-vehicle
(`0x1F4` byte 1). Button *injection* not yet proven (Phase C.5). SOC, shift,
and ignition behaviour still need a drive.**

This file is the Phase C deliverable (DESIGN.md). Fill it in from the
`tools/` output, then update `voltdmf/signals.py` / `voltdmf/canio.py` and
flip the `confirmed` flags.

Signal IDs and scaling can vary by model year and market. Record enough to
reproduce the decode; do not put vehicle-identifying details (VIN, plate) in
a checked-in file.

| Field | Value |
|---|---|
| Vehicle | 20__ Chevy Volt (Gen 1), model year / trim: `______` |
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

## Drive-mode button press (TX) -- `0x1F4`  (address confirmed; injection NOT proven)

- **Same arbitration ID as the status message** (`0x1F4`). There is no
  separate Gen-2-style `0x1E1` button frame on this bus.
- Steady frame from the body module: `00 <mode> 00 00 00 00` @ ~40 Hz.
- A physical tap, observed over 5 timestamped presses:
  - byte 5 -> `0x80` while the button is held (~0.3-1 s), else `0x00`
  - byte 4 ramps `0x80 -> 0x40 -> 0x20` the longer it is held
  - byte 1 stays = the *current* mode throughout the press
  - the latched mode (byte 1) advances one step ~2-3 s **after** release
- All 5 taps advanced exactly one step: HOLD->NORMAL->SPORT->MOUNTAIN->HOLD->NORMAL.
- Injection findings (Phase C.5, in progress on-vehicle 2026-08-29):
  - Our `0x1F4` frames DO reach the cluster -- they wake the drive-mode screen.
  - The press is **duration-gated**: ~0.3 s of byte5=`0x80` only wakes the
    screen; ~1.2 s (with the byte4 ramp) counted as ~3 presses (NORMAL->HOLD
    in one shot). The byte4 release ramp **auto-repeats** when injected.
  - TX is hard on the MCP2515 here (RX stays clean): the controller walks
    ERROR-ACTIVE -> ERROR-PASSIVE -> BUS-OFF across a few shots. Suspect a
    marginal CAN-H/L solder joint.
- Approach (`voltdmf/canio.py`), aligned with the Gen 2 prior art
  (`vix597/chevy-volt-trip-mode`) -- **burst-and-release**:
  - Mirror byte 1 from the live bus. Send a short burst of
    `PRESS_BURST_FRAMES` byte 5 = `0x80` frames at `PRESS_FRAME_INTERVAL_S`
    (default 45 @ 100 Hz = 0.45 s), then **stop transmitting** -- no release
    frame, no ramp. The body module's own ~40 Hz idle `0x1F4` is the
    "button up".
  - Callers leave `RELEASE_GAP_S` (0.75 s, the reference's
    `BUTTON_PRESS_COOLDOWN`) of silence before the next press. That gap
    separates one counted press from the next *and* lets the transmit-error
    counter recover.
  - `tools/inject_test.py` is closed-loop: it reads `0x1F4` back after every
    press and stops when the dash reaches the goal -- no blind counting.
- **Open:** sweep `--burst-ms` / `--rate-hz` for **exactly one step per
  press** around the full cycle, `can0` staying ERROR-ACTIVE, zero error
  frames / new DTCs. Then bake the winning values into `canio.py`.

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

# Confirmed CAN signals (Gen 1 Chevy Volt, this vehicle)

**Status: NOTHING CONFIRMED YET.** This file is the Phase C deliverable
(DESIGN.md). Fill it in from the `tools/` output, then update
`voltdmf/signals.py` / `voltdmf/canio.py` and flip the `confirmed` flags.

Signal IDs and scaling can vary by model year and market. Record enough to
reproduce the decode; do not put vehicle-identifying details (VIN, plate) in
a checked-in file.

| Field | Value |
|---|---|
| Vehicle | 20__ Chevy Volt (Gen 1), model year / trim: `______` |
| Bus | OBD-II pins 6/14, HS-CAN, `______` kbit/s |
| Date tested | `______` |
| Tool used | SavvyCAN / candump / `tools/*` |

## SOC -- `0x206`

- Byte layout: `______` (offset, width, endianness)
- Raw -> value: `______` (scale / offset)
- Calibration points (raw value : dash % : dash kWh):
  - `______`
- => `voltdmf/signals.py`: `SOC_KWH_PER_COUNT = ___`, `GEN1_PACK_USABLE_KWH = ___`

## Drive-mode button press (TX) -- `0x___`

- Address: `0x___`  (is it `0x1E1`? bit 39? — from `mode_diff.py`)
- Payload: `__ __ __ __ __ __ __`
- Frames per logical press: `___`  (Gen 2 reference used 50)
- Screen-wake first press needed? `yes / no`
- => `voltdmf/canio.py`: `MODE_BUTTON_ADDR_*`, `MODE_BUTTON_PAYLOAD_*`, `SEND_CLUSTER_SIZE`

## Current-mode status -- `0x___`

- Address / byte / values per mode (from `cycle_modes.py`):
  - NORMAL: `______`
  - SPORT: `______`
  - MOUNTAIN: `______`
  - HOLD: `______`
- => implement `voltdmf/signals.decode_drive_mode()` and wire it as the
  `current_mode_source` in `voltdmf/daemon.py` (replacing `PressCountingModeTracker`)

## Shift / PRNDL -- `0x135` / `0x1F5`

- Address / byte / values: `______`
- => implement `voltdmf/signals.decode_shift()`; then set
  `SafetyGate(allow_unknown_shift=False)` in `voltdmf/daemon.py`

## Ignition behaviour (from `ignition_check.py`)

- Bus goes quiet with car off? `yes / no`
- Mode resets to NORMAL on ignition, or remembered? `______`
- => sets `ASSUMED_START_MODE` validity in `voltdmf/daemon.py`

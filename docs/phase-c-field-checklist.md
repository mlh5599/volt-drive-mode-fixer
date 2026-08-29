# On-Vehicle Field Checklist

Covers DESIGN.md **Phase A** (CAN bring-up) → **Phase C** (signal discovery) →
**Phase C.5** (injection gate).

> **Car in Park with the parking brake set for every step. Never run discovery
> or injection while the vehicle can move.** Physical abort = unplug the OBD
> cable.

---

## 0 · Pack before you go

- [ ] Pi boots headless, SSH reachable on the Wi-Fi the car parks in range of
- [ ] Repo on the Pi at latest `main`; virtualenv built; `pytest` green
- [ ] `can-utils` installed (`which candump cansend`)
- [ ] PiCAN2 seated firmly; **on-board 120 Ω terminator jumper OFF** (the
      vehicle bus is already terminated — the Pi is only a tap)
- [ ] OBD-II-to-DB9 cable; confirm it wires OBD pins 6/14 (HS-CAN)
- [ ] Power: accessory-socket USB **plus** a charged battery pack as backup
      (a Pi 3B browns out below ~2 A)
- [ ] Laptop with SSH; phone for photos/video of the instrument cluster
- [ ] Somewhere to write `raw hex ↔ dash reading ↔ timestamp` triples
- [ ] USB stick or an `scp` target to pull `candump` logs off the Pi
- [ ] OBD-II scan tool (ELM327 or similar) for the DTC check in Phase C.5
- [ ] Know where the drive-mode button is and how the dash shows the current mode

---

## 1 · Phase A — bring up `can0`

Ignition in RUN; engine off is fine.

- [ ] Append to `/boot/firmware/config.txt`, then `sudo reboot`:
  ```
  dtparam=spi=on
  dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
  ```
- [ ] `dmesg | grep -i mcp251` shows the controller; `ip link show can0` exists
- [ ] `sudo ip link set can0 up type can bitrate 500000`
- [ ] Ignition to RUN
- [ ] `candump can0` streams frames
- [ ] `ip -details -statistics link show can0` → state **ERROR-ACTIVE**, error
      counters not climbing
- [ ] **If BUS-OFF / error-frame flood:** `sudo ip link set can0 down`, retry
      bitrate `250000`, then `125000`, then `33333`. Record the value that gives
      ERROR-ACTIVE with live frames — it flows into `host/` and the deploy
      role's `*_can_bitrate`.
- [ ] Baseline capture, ~60 s, touching nothing: `tools/logcan.sh ~/captures`
- [ ] Daemon RX smoke test (transmits nothing):
  ```
  python -m voltdmf --config config.example.yaml --dry-run --channel can0 --log-level DEBUG
  ```
  → logs decoded frames + "would send N presses…"; a second `candump` shows
  **zero** frames originating from us.

---

## 2 · Phase C — signal discovery

Stationary, car in "ready"/on, Park, parking brake set.

### 2a · Drive-mode button + current-mode status signal

- [ ] `tools/mode_diff.py` — on each prompt, press the drive-mode button
      **once**; let it diff. Run 4–5 rounds.
  - [ ] Does `0x1E1` byte 4 bit `0x80` toggle on press? (confirms or kills the
        Gen-2 reference candidate)
  - [ ] List every other ID/byte that changes **in step with** each press
- [ ] `tools/cycle_modes.py` — guided walk NORMAL → SPORT → MOUNTAIN → HOLD.
      Record the candidate status byte's value **in each mode**; confirm the
      4-step cycle order.
- [ ] Note: does the **first** press only wake the cluster screen (so reaching
      a mode needs one extra press)?
- [ ] Photograph/video the cluster during the walk so values can be re-checked
      against timestamps

### 2b · SOC scaling on `0x206`

Needs a real discharge — usually a drive.

- [ ] Start `tools/watch_soc.py` logging
- [ ] Record `(raw 0x206, dash SOC %, dash EV-range or kWh)` at roughly
      90 / 70 / 50 / 40 / 30 / 25 / 15 % — extra points around the intended
      **threshold** and **reset** percentages
- [ ] Derive scale/offset → `SOC_KWH_PER_COUNT`, `GEN1_PACK_USABLE_KWH`

### 2c · Ignition behaviour

- [ ] `tools/ignition_check.py` across: on → set drive mode to something
      non-default → off → wait 2 min → on
  - [ ] Does drive mode reset to NORMAL, or is it remembered?
  - [ ] Does the bus go fully quiet with the car off, and how long after?
  - [ ] Seconds from "on" to first frames

### 2d · Shift / PRNDL (`0x135` or `0x1F5`)

- [ ] With `candump can0,135:7FF` (then `1F5:7FF`) running, move P → R → N → D →
      L and record the byte/value per position

### Phase C deliverable (back at a keyboard)

- [ ] Fill `docs/signals-confirmed.md` completely (IDs, byte layout, scaling,
      model year / trim, date — **no VIN or plate**)
- [ ] `voltdmf/signals.py`: real addresses in `SIGNAL_IDS`, flip
      `confirmed=True` for what's proven, set the SOC constants, implement
      `decode_drive_mode()` and `decode_shift()`
- [ ] `voltdmf/canio.py`: set `MODE_BUTTON_ADDR_*`, `MODE_BUTTON_PAYLOAD_*`,
      `SEND_CLUSTER_SIZE`
- [ ] Update `tests/test_signals.py::test_nothing_is_confirmed_yet` (it *should*
      now fail — rewrite it to assert the specific confirmed set) and add decode
      tests from the captured hex
- [ ] `voltdmf/daemon.py`: wire the real `decode_drive_mode` as
      `current_mode_source`; set `SafetyGate(allow_unknown_shift=False)`
- [ ] `pytest` green; commit on a branch

---

## 3 · Phase C.5 — injection gate

Stationary, Park, parking brake set; engine off or on jack stands; ventilate if
the engine may start.

- [ ] Terminal 2: `candump can0 | grep -iE 'err'` running (watch for error frames)
- [ ] `tools/inject_test.py --yes-stationary` — sends **one** logical press
- [ ] Dash mode advances **exactly one step** in NORMAL → SPORT → MOUNTAIN → HOLD
- [ ] Repeat for a full cycle (4 presses → back to start); each = one step
- [ ] No new warning lights, chimes, or drivetrain messages; no `candump` error
      frames
- [ ] OBD-II scan for stored DTCs; clear only ones you caused and understand
- [ ] **GATE:** do not run the daemon on a moving car unless every press =
      exactly one step and zero new DTCs

---

## 4 · Then, and only then — enable the deploy

- [ ] Pin the deploy role's code version to the confirmed-signals commit
- [ ] Enable the service but keep dry-run on for the first on-road drive; watch
      `journalctl -u voltdmf -f`
- [ ] Turn dry-run off once a dry-run drive shows correct trigger decisions
- [ ] Tune `threshold_percent` / `reset_percent` over several drives until
      reduced-propulsion mode never occurs
- [ ] Overlay File System + `/boot` write-protect + swap-off **last**, after
      it's proven

---

## Abort criteria (any step)

Unexpected warning light, drivetrain message, or a DTC you can't explain →
`sudo ip link set can0 down`, unplug the OBD cable, and diagnose before
continuing.

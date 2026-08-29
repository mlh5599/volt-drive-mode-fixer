# On-Vehicle Field Checklist

Covers DESIGN.md **Phase A** (CAN bring-up) → **Phase C** (signal discovery) →
**Phase C.5** (injection gate).

> **Car in Park with the parking brake set for every step. Never run discovery
> or injection while the vehicle can move.** Physical abort = unplug the OBD
> cable.

> **Progress:** see `docs/field-session-log.md`. As of session 1 (2026-08-29):
> Phase A **PASS**; Phase C current-mode **status** signal **CONFIRMED**
> (`0x1F4` byte 1); Phase C.5 **in progress** (injection reaches the cluster,
> not yet one-step-per-press); SOC / shift / ignition still need a drive.

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

## 1 · Phase A — bring up `can0`  ✅ PASS (2026-08-29)

Result: clean at **500 kbit/s**, `ERROR-ACTIVE`, zero error counters, no
bitrate fallback needed. Ignition in RUN; engine off is fine.

- [x] Append to `/boot/firmware/config.txt`, then `sudo reboot`:
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
- [x] Baseline capture, ~60 s, touching nothing: `tools/logcan.sh ~/captures`
- [ ] Daemon RX smoke test (transmits nothing):
  ```
  python -m voltdmf --config config.example.yaml --dry-run --channel can0 --log-level DEBUG
  ```
  → logs decoded frames + "would send N presses…"; a second `candump` shows
  **zero** frames originating from us.

---

## 2 · Phase C — signal discovery

Stationary, car in "ready"/on, Park, parking brake set.

### 2a · Drive-mode button + current-mode status signal  ✅ status CONFIRMED (2026-08-29)

Result: **`0x1F4`** carries both. Byte 1 = latched mode (NORMAL `0x00`,
SPORT `0x80`, MOUNTAIN `0x20`, HOLD `0x08`). Byte 5 = `0x80` button-down;
byte 4 = post-release decay ramp. `0x1E1` does **not** exist on this bus.
The `mode_diff.py` / `cycle_modes.py` diff-on-a-running-car approach was too
noisy — what worked was a timestamped dwell-per-mode capture + offline
per-byte scan. Full detail in `docs/signals-confirmed.md`.

- [x] Identify the drive-mode message and the current-mode byte
- [x] Confirm the 4-step cycle order and each mode's byte value
- [x] Note: the **first** press only wakes the cluster screen (relevant to
      injection too — see Phase C.5)
- [x] Video of the cluster during the walk, checked against timestamps

### 2b · SOC scaling on `0x206`  ⚠️ blocked — needs a drive

`0x206` **never appears** on this bus (Gen-2 ID, wrong for Gen 1). Needs a
real discharge drive to find the actual ID + scaling.

- [ ] Start `tools/watch_soc.py` logging
- [ ] Record `(raw 0x206, dash SOC %, dash EV-range or kWh)` at roughly
      90 / 70 / 50 / 40 / 30 / 25 / 15 % — extra points around the intended
      **threshold** and **reset** percentages
- [ ] Derive scale/offset → `SOC_KWH_PER_COUNT`, `GEN1_PACK_USABLE_KWH`

### 2c · Ignition behaviour  ⚠️ not started — needs a drive/ignition cycle

- [ ] `tools/ignition_check.py` across: on → set drive mode to something
      non-default → off → wait 2 min → on
  - [ ] Does drive mode reset to NORMAL, or is it remembered?
  - [ ] Does the bus go fully quiet with the car off, and how long after?
  - [ ] Seconds from "on" to first frames

### 2d · Shift / PRNDL (`0x135` or `0x1F5`)  ⚠️ blocked — needs the shifter moved

In Park, `0x135` bytes 0–6 are constant (`00 00 1c 76 8a 0c 1a`), byte 7 is a
free counter; `0x1F5` static. Nothing to decode without moving through
P-R-N-D-L.

- [ ] With `candump can0,135:7FF` (then `1F5:7FF`) running, move P → R → N → D →
      L and record the byte/value per position

### Phase C deliverable (back at a keyboard)

- [x] Fill `docs/signals-confirmed.md` for the **status** signal (IDs, byte
      layout, date — no VIN/plate). SOC / shift / ignition sections still
      marked NOT CONFIRMED pending a drive.
- [x] `voltdmf/signals.py`: `0x1F4` in `SIGNAL_IDS`, `drive_mode_status`
      `confirmed=True`, `decode_drive_mode()` implemented. SOC constants and
      `decode_shift()` still stubbed.
- [x] `voltdmf/canio.py`: `MODE_BUTTON_ADDR = 0x1F4`, press waveform
      (`PRESS_*` constants), `SEND_CLUSTER_SIZE`
- [x] Rewrote `test_nothing_is_confirmed_yet` → `test_confirmed_signal_set`;
      added `decode_drive_mode` tests from captured hex
- [ ] `voltdmf/daemon.py`: wire the real `decode_drive_mode` as
      `current_mode_source`; add `0x1F4` to `is_signal_frame` + RX listener;
      set `SafetyGate(allow_unknown_shift=False)` once shift is known
- [x] `pytest` green (59 passed); committed on a branch

---

## 3 · Phase C.5 — injection gate  🔄 IN PROGRESS (2026-08-29)

Stationary, Park, parking brake set, **car in full READY**; engine off or on
jack stands; ventilate if the engine may start.

Session 1 status: injected `0x1F4` frames reach the cluster (screen wakes) and
one shot moved NORMAL→HOLD. Not yet one-step-per-press; `can0` degrades to
BUS-OFF under sustained TX. Findings + next-session recipe:
**`docs/field-session-log.md`**.

- [x] Terminal 2: `candump can0 | grep -iE 'err'` running (watch for error frames)
- [x] `tools/inject_test.py --yes-stationary` — frames reach the cluster
- [ ] Dash mode advances **exactly one step** per shot (tune `--down-ms` /
      `--rate-hz`; start `--no-ramp --down-ms 450`)
- [ ] Repeat for a full cycle (4 shots → back to start); each = one step
- [ ] `can0` stays `ERROR-ACTIVE` throughout; no new warning lights, chimes,
      drivetrain messages, or `candump` error frames
- [ ] OBD-II scan for stored DTCs; clear only ones you caused and understand
- [ ] **GATE:** do not run the daemon on a moving car unless every press =
      exactly one step and zero new DTCs
- [ ] Bake the winning `--down-ms` / `--rate-hz` into `voltdmf/canio.py`;
      flip `drive_mode_button.confirmed = True`

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

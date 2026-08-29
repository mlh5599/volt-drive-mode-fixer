# On-Vehicle Field Checklist

Covers DESIGN.md **Phase A** (CAN bring-up) → **Phase C** (signal discovery) →
**Phase C.5** (injection gate).

> **Car in Park with the parking brake set for every step. Never run discovery
> or injection while the vehicle can move.** Physical abort = unplug the OBD
> cable.

> **Progress:** see `docs/field-session-log.md`. As of session 2 (2026-08-29):
> Phase A **PASS**; Phase C current-mode **status** signal **CONFIRMED**
> (`0x1F4` byte 1); the button INPUT is **`0x1E1` byte 4 bit 7** (an earlier
> call this session that `0x1E1` isn't the button only checked bytes 0–3).
> "Tracking echo" injection reproduced a full owner-confirmed drive-mode
> **menu walk** with `can0` ERROR-ACTIVE and zero error frames. Phase C.5 is
> **not a clean automated PASS yet**: on a parked car `0x1F4` byte 1
> lags/reverts, and multi-press walk timing is not run-to-run consistent —
> needs a physical-walk capture to tune against, then a drive to verify.
> SOC / shift / ignition still need a drive.

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

Result: **status** is `0x1F4` byte 1 (NORMAL `0x00`, SPORT `0x80`, MOUNTAIN
`0x20`, HOLD `0x08`). The button **input** is a separate ID, **`0x1E1`
byte 4 bit 7** ("ASCMSteeringButton"; a real press = ~14 frames with bit 7
set, counter still advancing) — `0x1E1` *is* on this bus in full READY; it
was briefly mis-ruled-out by diffing only bytes 0–3. The
`mode_diff.py` / `cycle_modes.py` diff-on-a-running-car approach was too
noisy — what worked was a timestamped dwell-per-mode capture + offline
per-byte scan, then a full-frame diff of a physical press. Full detail in
`docs/signals-confirmed.md`.

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
- [x] `voltdmf/canio.py`: `MODE_BUTTON_ADDR = 0x1E1`, tracking-echo press
      (`PRESS_TRACK_FRAMES`, `_next_button_frame()`), `SEND_CLUSTER_SIZE`
- [x] Rewrote `test_nothing_is_confirmed_yet` → `test_confirmed_signal_set`;
      added `decode_drive_mode` tests from captured hex
- [ ] `voltdmf/daemon.py`: wire the real `decode_drive_mode` as
      `current_mode_source`; add `0x1F4` to `is_signal_frame` + RX listener;
      set `SafetyGate(allow_unknown_shift=False)` once shift is known
- [x] `pytest` green (59 passed); committed on a branch

---

## 3 · Phase C.5 — injection gate  🟡 IN PROGRESS — walk reproduced, not yet a clean PASS (2026-08-29, session 2)

Stationary, Park, parking brake set, **car in full READY**; engine off or on
jack stands; ventilate if the engine may start.

Session 2 status: the "reflow the board" call earlier this session was
**wrong** (owner: soldering is fine). The failures were software — wrong ID
(transmitting on `0x1F4` collided same-ID with the module → ERROR-PASSIVE)
and a static `0x1E1` frame the cluster ignores. Fixed by (a) targeting
**`0x1E1` byte 4 bit 7** and (b) a **tracking-echo** press: reply to each
live `0x1E1` in its ~24 ms gap with bit 7 set, ~16×, replicating the
14-frame physical press with a valid advancing counter. A full injected
`NORMAL→SPORT→MOUNTAIN→HOLD→NORMAL` **menu walk** was owner-confirmed on the
dash, `can0` ERROR-ACTIVE, zero error frames. Remaining: walk-timing
consistency (~2 s menu window vs ~400 ms/press + walk-gap) and a drive to
confirm the mode actually commits/holds (parked `0x1F4` byte 1 lags ~7–9 s
and reverts to NORMAL). Full detail + next-session recipe:
**`docs/field-session-log.md`**.

- [x] Terminal 2: `candump can0,1E1:7FF | grep -iE 'err'` running (watch for error frames)
- [x] `tools/inject_test.py --yes-stationary` — frames reach the cluster (screen wakes)
- [x] Determined the press-count mechanism: button edges, not hold duration
- [x] `0x1E1` byte 4 bit 7 confirmed as the button (full-frame diff of a physical press)
- [x] Tracking-echo injection: `can0` stays ERROR-ACTIVE, zero error frames, all frames on the wire
- [x] Full injected `--cycle` walk reproduced and **owner-confirmed on the dash**
- [ ] Capture a known-good **physical** 4-press walk to measure the menu-open
      window / inter-press interval / byte-1 latch delay
- [ ] Tune `--walk-gap` (and `--frames`) so `inject_test.py --yes-stationary
      --cycle` lands every step run-to-run; owner confirms each on the dash
- [ ] `can0` stays `ERROR-ACTIVE` throughout; no new warning lights, chimes,
      drivetrain messages, or `candump` error frames
- [ ] First dry-run drive: modes commit/hold; check `journalctl -u voltdmf`
      against the dash. OBD-II scan for stored DTCs; clear only ones you
      caused and understand
- [ ] **GATE:** do not run the daemon on a moving car unless the walk reaches
      every mode reliably and there are zero new DTCs
- [ ] Bake the winning `PRESS_TRACK_FRAMES` / `RELEASE_GAP_S` / walk-gap into
      `voltdmf/canio.py` + `voltdmf/modecycle.py` (which still assumes
      "N presses = N steps from current mode"); flip
      `drive_mode_button.confirmed = True`

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

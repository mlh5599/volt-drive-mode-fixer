# On-Vehicle Field Checklist

Covers DESIGN.md **Phase A** (CAN bring-up) → **Phase C** (signal discovery) →
**Phase C.5** (injection gate), plus a stretch-goal capture (§2e — force 12 A
Level 1 charging).

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
>
> **Update (session 6, 2026-08-30):** shift **CONFIRMED** on the road
> (session 4, `0x1F5` byte 3); the closed-loop menu walk got an owner-confirmed
> on-road PASS (session 4). Remaining: the **SOC discharge drive** (bench prep
> A1–A4 done — see field-session-log Session 6), `ignition_check.py`, and the
> stationary injection sweep.
>
> **Update (session 7, 2026-08-30):** the **panel-button path** for the SOC
> drive is **verified in-car** — SW1+SW2 held 5 s launches
> `voltdmf-soclog.service`, and in the logger A/B move the gauge and
> hold-both-3s stops clean (took three GPIO hand-off fixes: `5b951da` /
> `e69cb44` / `55fe176`; deployed pin `55fe176`). The SOC discharge drive can
> now be run start-to-finish from the driver's seat with no laptop. Only the
> drive itself + `mine_capture.py` analysis remain for SOC.
>
> **Update (session 8, 2026-08-30):** the **full-drain SOC drive is done**
> (10/10 → 0/10, all bar-drops hand-marked). Timer-rejection analysis
> narrowed SOC to **three broadcast candidates** (`0x3E3` b0/b1/b6, `0x228`
> b2, `0x186` b6) and excluded four elapsed-time counters — but **no
> known-SOC anchor was logged**, so nothing is calibrated yet
> (`docs/analysis/session8-soc-candidates.md`). `soc_log.py` gained
> per-line candidate raw stamps + an opt-in `--diag-soc` (`22 005B`) poll.
> §2b below now describes the **Session-9 anchor drive** — that plus the
> stationary injection sweep and `ignition_check.py` are what's left.

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

### 2b · SOC — find the ID + scaling  ⚠️ ID narrowed to 3 candidates (Session 8); still needs a scaling anchor

`0x206` **never appears** on this bus (Gen-2 ID, wrong for Gen 1) — re-confirmed
0 frames in the Session-8 213 MB full-drain capture. This dash shows **no SOC %**,
only EV-range miles + a 10-increment battery gauge.

**Session 8 (2026-08-30) — full-drain drive done.** `captures/candump-2026-08-30_131932.log`,
gauge 10/10 → 0/10, all ten drops hand-marked. Timer-rejection analysis
(per-bar delta vs per-bar duration) narrowed SOC to three energy-linked
broadcast candidates — **`0x3E3` b0/b1/b6** (strongest), **`0x228` b2**
(cleanest, coarse), **`0x186` b6** (fine, noisy) — and excluded four
elapsed-time counters (`0x4CB`/`0x3DD`/`0x137`/`0x4E9`). **No scaling anchor
was logged** (no reading at a known SOC), so `signals.py` is untouched. GM
Volt RE wiki carries battery charge % only as diagnostic PID `22 005B`
(`X·100/255`). Detail: `docs/field-session-log.md` Session 8 + the visual
write-up (normalized overlay, per-bar table, timer-rejection scorecard,
native-scale shapes) in [`analysis/session8-soc-candidates.md`](analysis/session8-soc-candidates.md)
— charts regenerable from the capture via `tools/soc_report.py`.

- [x] Bench prep A1–A4 (2026-08-30); panel-launch path verified in-car
      (session 7, deployed pin `55fe176`).
- [x] The full-drain discharge drive (Session 8). Capture clean, no GPIO
      trouble.
- [x] `soc_log.py` speed logging fixed + accel added (Session 8) — reads a
      `candump -L can0,3E9:7FF` pipe, `0x3E9` b0–1 BE ÷ 64 → km/h → mph, plus
      a derived `a~` slope. Marks now carry `spd~44mph  a~-0.3`.

**Session 9 — the scaling-anchor drive** (owner's plan):

- [ ] Start at a **full charge**. Start the capture (**hold SW1+SW2 ~5 s**,
      or `systemctl start voltdmf-soclog.service` / `soc_log.py --yes
      --minutes 90 --buttons --lcd --diag-soc`). Engine on, **sit in Park
      ~2 min** — this is the missing anchor: the raw value of each candidate
      at the "100 % display" state.
      - Every mark line now carries `cand[3E3.0=… 3E3.1=… 3E3.6=… 228.2=…
        186.6=…]` (always on, passive), so the three broadcast candidates are
        logged inline with no extra step.
      - `--diag-soc` is **part of this run**, not optional: it polls UDS
        `22 005B` every ~10 s and stamps `soc22=NN.N%(0xRAW@7EC)` — the
        continuous known-SOC ground truth that lets the capture solve the
        raw→% fit directly. It is the one active-TX path (default session,
        mode-22 only, so broadcasts are not suppressed); auto-detects the ECU
        (0x7E4 then 0x7E0) and self-disables if nothing answers.
      - The deployed `voltdmf-soclog.service` and the ansible `voltdmf` role
        still launch the **passive** form. Before the drive, either add
        `--diag-soc` to that unit's `ExecStart` (and bump `voltdmf_version`
        to the new SHA), or run `soc_log.py … --diag-soc` by hand.
- [ ] Drive as **constant** as possible (steady speed / throttle) so
      d(candidate)/d(SOC) is a clean slope. The new `spd~` / `a~` in each
      mark is there to correlate against load.
- [ ] Tap **A** on every gauge-bar drop (B if it climbs back), as before.
- [ ] At the **2/10-bar mark**, trigger **HOLD** and drive **~10 min** steady.
      Charge-sustaining: a genuine SOC field stops falling (or rises a little)
      here — both a candidate confirmation and the second anchor for the slope.
      (2 bars is also the chosen SOC-HOLD floor — see `DESIGN.md`.)
- [ ] Hold both buttons 3 s (parked) to stop.
- [ ] `mine_capture.py <capture> --series 3E3:0:1 / 228:2:1 / 186:6:1 --every 20`;
      read each candidate's raw value at the 2-min idle anchor and at the
      HOLD entry. Cross-check against the inline `cand[…]` / `soc22=…` stamps
      in the marks log.
- [ ] Set `SOC_KWH_PER_COUNT` / `GEN1_PACK_USABLE_KWH` (or a direct percent
      decode) in `voltdmf/signals.py` from the two anchors; flip
      `soc.confirmed = True`; wire `decode_soc` into `_DecodeListener` /
      `is_signal_frame`; set `hold_threshold_percent` / `hold_reset_percent`
      from the raw value at the 2-bar / 3-bar crossings; record
      ID/offset/scale in `signals-confirmed.md`.

### 2c · Ignition behavior  ⚠️ not started — needs a drive/ignition cycle

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

### 2e · Charge-current setpoint — force 12 A Level 1  💤 deferred (owner deprioritized 2026-08-30) — kept for a later dedicated capture

Why: the center-stack 8 A → 12 A charge-rate setting reverts to 8 A every time
the car leaves Park. The owner only ever charges on a known-good home circuit,
so holding 12 A is a safe convenience win — `DESIGN.md` → "Stretch goal —
force 12 A Level 1 charging". **This session only captures the setpoint frame;
no injection.** Costs ~2 min and does not touch the gauge-marking workflow.

Prelude — **in Park, before pulling out**, with `soc_log.py` already running
(its `candump -l` grabs the whole bus):

- [ ] Note wall-clock time, toggle **8 → 12 A** in the menu, hold ~20 s
- [ ] Time, toggle **12 → 8 A**, hold ~20 s
- [ ] Time, toggle **8 → 12 A**, hold ~20 s
- [ ] Time, toggle **12 → 8 A**, hold ~20 s
- [ ] Shift to **Drive** and pull out — 5th transition, the forced revert to
      8 A; `0x1F5` byte 3 leaving `0x01` (Park) timestamps it for free
- [ ] *(optional, only if convenient at a stop — do not let it compete with
      tapping A on gauge drops)* toggle once or twice more mid-drive, call out
      the time

Analysis (back at a keyboard):

- [ ] `mine_capture.py <capture> --shift-window <prelude-start> <prelude-end>`
      — lists IDs that took discrete states in the window and prints every
      transition
- [ ] Cross those against the `0x1F5` byte 3 Park-exit timestamp: the setpoint
      frame is the one that changes at your toggles **and** snaps to the 8 A
      value exactly at Park-exit
- [ ] Expect a byte flipping `0x08`↔`0x0C` (amps) or `0x28`↔`0x3C` (40↔60 in
      0.2 A units); note ID / offset / encoding and whether a rolling counter
      or checksum rides along
- [ ] Record in `docs/signals-confirmed.md` → "Charge current setpoint"
      (stays NOT CONFIRMED until an injection test); leave injection +
      Park-exit re-assert for a later dedicated session

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

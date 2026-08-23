# Volt Drive Mode Fixer — Design

## Problem

On an aging Gen 1 Chevy Volt (2011–2015), fully depleting the EV battery causes
the pack voltage to sag under load, which trips "reduced propulsion" mode: the
gas engine revs to near max RPM to generate power directly, and the car
becomes sluggish. The owner currently avoids this by manually switching to
Mountain Mode before the battery runs low. This project automates that: a
device on the car's CAN bus watches the EV battery state of charge (SOC) and
automatically triggers a mode switch before the pack gets low enough to cause
the problem.

## Vehicle context

The Volt (both Gen 1 and Gen 2, model years 2011–2019) uses GM's **Global A**
CAN architecture. Unlike newer GM "Global B" vehicles, there is no
authenticating central gateway blocking the OBD-II port — the standard
driver-side OBD-II connector gives direct read/write access to the relevant
CAN buses. The Volt also has a second, auxiliary OBD-style connector
(passenger side) exposing the HV/battery-management bus, but the standard
port is sufficient for this project.

Gen 1 and Gen 2 differ in how drive mode is selected:
- **Gen 2** has a single "Drive Mode" button that *cycles* Normal → Sport →
  Mountain/Hold.
- **Gen 1** (this car) has a **discrete physical Mountain Mode button** —
  not a cycling button. The CAN message it sends has not been publicly
  documented (see "Open items" below); Gen 2's cycling-button message
  (`0x1E1`, described below) does not directly apply.

## Prior art (reuse this, don't rebuild it)

[`vix597/chevy-volt-trip-mode`](https://github.com/vix597/chevy-volt-trip-mode)
(blog writeup: [seanlaplante.com](https://seanlaplante.com/2021/11/13/hacking-my-chevy-volt-to-auto-switch-driving-modes-for-efficiency/))
is a 2017 Volt (Gen 2) project that does almost exactly this kind of
automation — it auto-switches modes based on speed rather than SOC, using a
comma.ai panda + Raspberry Pi. It's built directly on
[`commaai/opendbc`](https://github.com/commaai/opendbc)'s
`gm_global_a_powertrain.dbc`, which decodes the Volt/Bolt-era GM Global A
bus. Reuse this DBC file and the general panda + Python approach rather than
reverse-engineering the whole bus from scratch.

Its known gap, worth fixing rather than copying: it runs the panda in
`SAFETY_ALLOUTPUT` mode with no message whitelist, so the device is
technically capable of transmitting anything on the bus. See "Safety model"
below.

## Known CAN signals

| Signal | ID | Notes |
|---|---|---|
| EV battery SOC | `0x206` | Bytes 1–2, ~0.25 kWh/count granularity per OVMS notes. **Needs calibration** against your car's dash %/kWh readings — treat the raw value as relative until you've confirmed the scaling for your pack. |
| Drive mode button press (Gen 2 cycling button) | `0x1E1`, bit 39 | `DriveModeButton` signal in `gm_global_a_powertrain.dbc`. **Does not apply directly to Gen 1's discrete Mountain Mode button** — different physical control, likely a different message. |
| Vehicle speed | `0x3E9` | 16-bit big-endian, ÷100 for mph. Not needed for the SOC-triggered design but useful for bench testing/logging. |
| Shift/PRNDL position | `0x135` / `0x1F5` | Useful as a safety precondition (e.g., don't inject while not in Drive). |
| EV range remaining | — | **Not documented anywhere found.** Use SOC (`0x206`) as the trigger signal instead — matches the original ask ("range or battery %") and is the metric that's actually accessible. |
| Reduced-propulsion / limp-mode indicator | — | **Not documented.** Not needed for this design since the goal is to act *before* this state, using the SOC threshold instead. |
| Current drive mode (status, not button-press) | — | **Not documented.** Would let the device avoid overriding a mode the driver deliberately chose. See "Open items." |

## Hardware design

**Recommended BOM:**

| Part | Role | Approx. price |
|---|---|---|
| comma.ai **panda** (red) | CAN read/write interface, OBD-II connector | $99 (+shipping if international) |
| Small always-on SBC (e.g. Raspberry Pi Zero 2 W) | Runs the monitor/injector daemon | ~$15–20 |
| USB cable (panda ↔ SBC) | Data link | — |
| 12V→5V USB car power adapter, or tap from a switched 12V source | Power for the SBC | ~$10 |

This is a smaller footprint than the reference project (which added a
touchscreen for a manual UI) since this design's whole point is to run
unattended — no display needed. Mount the SBC + panda somewhere accessible
(e.g. under the dash near the OBD port) so it's easy to unplug entirely if
something needs to be debugged or disabled — that's your physical kill
switch, and it's free.

Power the SBC from a switched (ignition-on) 12V source if possible, so the
device is only live while the car is on, rather than drawing parasitic
current at all times.

## Software architecture

Reuse rather than rebuild:
- **`opendbc`**'s `gm_global_a_powertrain.dbc` for decoding/encoding CAN
  frames (SOC, speed, shift position, and — once discovered — the Gen 1 mode
  button signal).
- **`pandacan`** (comma's Python library) or generic **`python-can`** for
  talking to the panda from the SBC.
- **`cabana`** (comma's CAN viewer) or **SavvyCAN** for the manual
  reverse-engineering phase (see below) — no need to write custom logging
  tools.

The custom code needed is small and specific to this project:

1. **SOC monitor** — reads `0x206`, converts to a usable SOC value, applies
   a debounce/smoothing filter (raw CAN values can be noisy frame-to-frame).
2. **Threshold + hysteresis logic** — triggers a mode-switch action once SOC
   crosses a configurable low threshold, but only **once per drive cycle**
   (latch), and resets the latch once SOC rises back above a higher
   threshold (e.g. after charging) — this avoids repeatedly firing the
   button-press message if SOC hovers near the threshold.
3. **Injector** — sends the (to-be-discovered) Gen 1 Mountain Mode CAN
   frame, through the safety wrapper below.
4. **Safety wrapper** (see next section) — the only code path allowed to
   transmit.

Don't write this until step 1 in "Phased plan" below has confirmed the
actual Gen 1 button-press signal — a monitor/injector built against a
guessed CAN ID is worse than not having one.

## Safety model

CAN has no authentication, and the reference project's `SAFETY_ALLOUTPUT`
approach means the panda can transmit *anything*, not just the one message
this project needs. Tighten that:

- **Single narrow send function.** All transmission goes through one
  function that hard-codes the exact CAN ID + payload for the mode-switch
  message (and only that). No general-purpose "send arbitrary frame" path
  in the daemon.
- **Rate limiting.** Cap sends to, at most, the number of frames a real
  button press would generate — never a sustained/looping transmission.
- **Preconditions before injecting**: vehicle in Drive, speed within a
  sane range, ignition on — reduces the chance of triggering the switch in
  a state where it wouldn't make sense anyway.
- **Fail passive.** If the daemon crashes or hangs, it should simply stop
  transmitting — never get stuck mid-loop retrying a send. Run it under a
  process supervisor (e.g. `systemd` with restart-on-failure) but make sure
  a fresh process start doesn't immediately re-fire a stale latch.
- **Physical kill switch**: unplugging the OBD connector fully removes the
  device from the bus — keep it physically easy to reach for exactly this
  reason during early testing.
- **Bench test before live testing.** Validate the discovered mode-switch
  message with the car safely stationary (ignition on, engine off, or on
  jack stands) before relying on it while driving.

## Phased plan

1. **Signal discovery (your car, stationary).** Connect panda + laptop
   (skip the SBC for now), log CAN traffic with `cabana` or SavvyCAN.
   Confirm `0x206` tracks SOC sensibly on this car. Then, with the car
   safely stationary, press the physical Mountain Mode button and diff the
   CAN traffic before/after to identify the message it sends. Also check
   whether there's a distinguishable "current mode" status message you can
   read back (would let the daemon avoid fighting a manually-chosen mode).
2. **Bench-safe injection test.** Using the discovered message, replay it
   from the laptop (still stationary) and confirm it actually switches the
   mode, and that nothing else on the bus reacts badly (watch for new DTCs,
   warning lights).
3. **Daemon development.** Write the SOC monitor + threshold/hysteresis +
   safety-wrapped injector described above, using the confirmed CAN IDs.
   Test it still tethered to a laptop before deploying to the SBC.
4. **Deployment.** Move to the SBC, mount it and the panda in the car,
   power from a switched 12V source, and test across several real drive
   cycles, tuning the SOC threshold based on how early you need the switch
   to happen to reliably avoid reduced-propulsion mode.

## Open items (need your car to resolve)

- The exact CAN ID/payload for Gen 1's physical Mountain Mode button —
  not documented publicly; requires step 1 above.
- The SOC (`0x206`) raw-value-to-percentage scaling for your specific
  pack/model year — calibrate against the dash reading.
- Whether a "current drive mode" status signal exists and is distinguishable
  on the bus, to avoid overriding a manually-selected mode.

## Sources

- [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode)
- [seanlaplante.com writeup](https://seanlaplante.com/2021/11/13/hacking-my-chevy-volt-to-auto-switch-driving-modes-for-efficiency/)
- [commaai/opendbc](https://github.com/commaai/opendbc)
- [comma.ai panda](https://comma.ai/shop/panda)
- [OVMS voltampera_canbusnotes.txt](https://github.com/openvehicles/Open-Vehicle-Monitoring-System/blob/master/vehicle/Car%20Module/VoltAmpera/voltampera_canbusnotes.txt)
- [GM Volt Reverse Engineering Wiki](https://vehicle-reverse-engineering.fandom.com/wiki/GM_Volt)
- [GM-Volt.com OBD2 FAQ](https://www.gm-volt.com/threads/obd2-obdii-obd-ii-device-faq.110641/)
- [Understanding the openpilot Safety Model](https://blog.comma.ai/understanding-the-openpilot-safety-model/)

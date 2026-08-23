# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that automatically switches drive
mode to avoid fully depleting the EV battery, which otherwise triggers
reduced-propulsion mode. Two configurable trigger strategies, meant to run
together:

- **On-start → Mountain Mode**, every drive cycle.
- **SOC threshold → Hold Mode**, once the battery drops to a configurable
  percentage.
- *(Roadmap)* **Trip Mode** — switch to Hold above a speed threshold to
  bank EV range for later, based on
  [vix597/chevy-volt-trip-mode](https://github.com/vix597/chevy-volt-trip-mode).

See [DESIGN.md](DESIGN.md) for the full hardware/software design, config
schema, phased plan, and safety model.

Mode selection on this car is a single button that cycles
Normal → Sport → Mountain → Hold, so reaching a target mode means computing
how many presses to send from the current mode — see DESIGN.md for why that
makes a current-mode status signal a hard requirement.

**Status:** design phase. Next step is signal discovery on the actual
vehicle (Phase 1 in DESIGN.md) — confirming whether the button-press CAN
message matches the Gen 2 reference project's, and finding a current-mode
status signal.

**Hardware:** comma.ai panda (red) + [OBD2C connector](https://konik.ai/shop/obd2c-connector/)
(adapts panda's OBD-C port to the vehicle's standard OBD-II port) + small SBC
(e.g. Raspberry Pi Zero 2 W).

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), `cabana`/SavvyCAN for logging, `pandacan`/
`python-can` for the daemon.

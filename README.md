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

**Status:** design phase. Next step is signal discovery on the actual
vehicle (Phase 1 in DESIGN.md) — the CAN messages for Gen 1's Mountain Mode
button and Hold Mode engagement aren't publicly documented and need to be
captured directly.

**Hardware:** comma.ai panda (red) + small SBC (e.g. Raspberry Pi Zero 2 W).

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), `cabana`/SavvyCAN for logging, `pandacan`/
`python-can` for the daemon.

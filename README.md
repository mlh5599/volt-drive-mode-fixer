# Volt Drive Mode Fixer

A CAN-bus device for a Gen 1 Chevy Volt that watches EV battery state of
charge and automatically switches to Mountain Mode before the pack gets low
enough to trigger reduced-propulsion mode.

See [DESIGN.md](DESIGN.md) for the full hardware/software design, phased
plan, and safety model.

**Status:** design phase. Next step is signal discovery on the actual
vehicle (Phase 1 in DESIGN.md) — the CAN message for Gen 1's Mountain Mode
button isn't publicly documented and needs to be captured directly.

**Hardware:** comma.ai panda (red) + small SBC (e.g. Raspberry Pi Zero 2 W).

**Reused software:** [`opendbc`](https://github.com/commaai/opendbc)
(`gm_global_a_powertrain.dbc`), `cabana`/SavvyCAN for logging, `pandacan`/
`python-can` for the daemon.

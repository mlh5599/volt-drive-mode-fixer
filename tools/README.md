# Signal-discovery tools (DESIGN.md Phase C)

Run these on the Pi (or a laptop with the PiCAN2) with the car **stationary**,
ignition on, engine off or on jack stands. They are throwaway investigation
aids, not part of the `voltdmf` package.

Prereqs: `can0` up at the right bitrate (see `../host/`), `can-utils` and
`python-can` installed.

| Script | DESIGN.md step | What it does |
|---|---|---|
| `logcan.sh` | Phase C | Timestamped `candump -l` capture to a file. |
| `watch_soc.py` | Phase C | Live raw + candidate decodings of `0x206`; correlate against the dash to calibrate `decode_soc`. |
| `mode_diff.py` | Phase C | Diffs bus traffic before/after one mode-button press; checks the `0x1E1` bit-39 candidate and flags any other frame that moved. |
| `cycle_modes.py` | Phase C | Walks all four modes, tabulating candidate status frames per mode -> finds the current-mode status signal. |
| `ignition_check.py` | Phase C | Whether the bus goes quiet with the car off, and whether a chosen status frame resets to Normal across an ignition cycle. |
| `inject_test.py` | Phase C.5 | **Transmits.** Replays the discovered button press, stationary only, one press at a time with confirmation. |

Deliverable of Phase C: fill the confirmed values into `voltdmf/signals.py` and
`voltdmf/canio.py`, flip the `confirmed` flags, and write
`../docs/signals-confirmed.md`.

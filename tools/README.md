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
| `inject_test.py` | Phase C.5 | **Transmits.** Lower-level walk/timing sweep (`--cycle`, `--repeat`, `--frames`) with its own copy of the walk loop. Use for tuning frames/gap. |
| `set_mode.py` | Phase C.5 | **Transmits.** One walk to `--target <mode>` through the real `ModeCycleController.switch_to()` + `canio` TX path. Closed loop: taps, reads the live menu cursor (`0x1F4` byte 4), stops when it's on the target, then polls byte 1 for the commit and prints the timeline. `--dry-run` needs no CAN hardware (open-loop `switch_to()` against a fake presser). Use to exercise the shipping menu-walk code. |
| `drive_log.py` | Phase C.5 | **Transmits.** Unattended: start it parked, drive normally. Walks each mode in `--sequence` through the same closed-loop path as `set_mode.py`, then samples `0x1F4` byte 1 (+ tentative speed off `0x3E9`) for `--hold` s after each commit, logging every change to `~/vdmf-drivelog-<ts>.log`. Answers "do MOUNTAIN/HOLD hold while moving?" without live SSH. `--dry-run` prints the plan and per-mode tap counts. |

Deliverable of Phase C: fill the confirmed values into `voltdmf/signals.py` and
`voltdmf/canio.py`, flip the `confirmed` flags, and write
`../docs/signals-confirmed.md`.

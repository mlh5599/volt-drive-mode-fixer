# Pi host configuration (DESIGN.md Phase A + Phase D)

Files here are applied to the Raspberry Pi OS Lite (Bookworm) install on the
Pi 3B. They are checked in so the box is reproducible.

> **Automating this?** The steps below map cleanly onto a
> configuration-management role (Ansible, Salt, etc.). A reasonable shape:
> manage the MCP2515 overlay in `config.txt`; replace the
> `systemd-networkd` file with a `Type=oneshot` `can0` bring-up unit so it
> does not depend on which network manager is active; install the package
> into a virtualenv (e.g. `/opt/voltdmf/venv`); template
> `/etc/voltdmf/config.yaml`; and run `voltdmf.service` as an unprivileged
> user with `AmbientCapabilities=CAP_NET_RAW` instead of as root. Gate the
> whole thing behind a per-host enable flag, keep `--dry-run` on until the
> bench injection test (DESIGN.md Phase C.5) passes, and **never let the
> automation reboot the device** — if applying the overlay needs a reboot,
> stop and let a human do it while the vehicle is parked.

## Phase A -- bring up `can0`

1. **Enable SPI + the MCP2515 overlay.** Append `config.txt.snippet` to
   `/boot/firmware/config.txt` (Bookworm path -- *not* `/boot/config.txt`),
   then reboot. PiCAN2 = 16 MHz crystal, INT on GPIO25.
2. **PiCAN2 terminator jumper OFF** -- the vehicle bus is already terminated.
3. **Packages:**
   ```
   sudo apt install -y can-utils python3-can python3-yaml
   ```
4. **Auto-bring-up at boot** at 500 kbit/s (GM Global A HS powertrain bus):
   ```
   sudo cp systemd-networkd/80-can0.network /etc/systemd/network/
   sudo systemctl enable --now systemd-networkd
   ```
   Manual equivalent for one session:
   ```
   sudo ip link set can0 up type can bitrate 500000
   ```
5. **Verify** (ignition on):
   ```
   candump can0                 # should stream frames
   ip -details link show can0   # state ERROR-ACTIVE
   ```
   `BUS-OFF` / error frames => wrong bitrate; try 250000 / 125000 / 33333.

## Phase D -- run the daemon

```
sudo install -d /etc/voltdmf
sudo cp ../config.example.yaml /etc/voltdmf/config.yaml   # then edit
sudo pip install --break-system-packages ..               # or install the wheel
sudo cp ../systemd/voltdmf.service /etc/systemd/system/
sudo systemctl enable --now voltdmf
journalctl -u voltdmf -f
```

**Overlay File System (do this LAST, after the daemon is validated):**
`sudo raspi-config` -> Performance Options -> Overlay File System = enabled;
accept the `/boot` write-protect prompt; then
`sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile`.
Keep `/etc/voltdmf/config.yaml` on a writable mount -- see DESIGN.md
"Hardware design / Power".

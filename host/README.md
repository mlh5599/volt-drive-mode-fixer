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
> whole thing behind a per-host enable flag, and **never let the automation
> reboot the device** — if applying the overlay needs a reboot, stop and let
> a human do it while the vehicle is parked. The daemon boots armed but
> passive (`default_position: hold-soc`); put `--start-disarmed` in `ExecStart`
> for a host that should come up fully stopped.

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
sudo groupadd --system voltdmf                            # control-socket group
sudo usermod -aG voltdmf "$USER"                          # log out/in to pick it up
sudo cp ../systemd/voltdmf.service ../systemd/voltdmf.socket /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voltdmf.socket voltdmf.service
journalctl -u voltdmf -f
```

If `voltdmf.service` is managed elsewhere (e.g. the Ansible `roles/voltdmf`
unit on `voltpi`, which runs as `User=voltdmf` + `AmbientCapabilities=CAP_NET_RAW`),
do **not** replace it. Install only the socket plus a drop-in:

```
sudo cp ../systemd/voltdmf.socket /etc/systemd/system/
sudo cp -r ../systemd/voltdmf.service.d /etc/systemd/system/   # Requires=/After=voltdmf.socket
sudo usermod -aG voltdmf "$USER"
sudo ln -sf /opt/voltdmf/venv/bin/voltdmf-ctl /usr/local/bin/voltdmf-ctl
sudo systemctl daemon-reload
sudo systemctl enable --now voltdmf.socket
sudo systemctl restart voltdmf.service
```

The socket file ends up `0660 voltdmf:voltdmf` (owner follows the service user),
so operators reach it through the `voltdmf` group exactly as in the root layout.

### Runtime control (`voltdmf-ctl`)

The daemon runs permanently as root; steer it from your normal account via the
control socket at `/run/voltdmf/control.sock` (mode `0660 root:voltdmf` — you
must be in the `voltdmf` group, see above). No `sudo`:

```
voltdmf-ctl status                 # daemon + vehicle snapshot
voltdmf-ctl disarm                 # mid-drive stop: stop transmitting, keep reading/evaluating
voltdmf-ctl arm                    # resume transmission
voltdmf-ctl setpoint next          # one detent forward: hold-soc -> hold-now -> mountain -> off -> hold-soc
voltdmf-ctl setpoint mountain      # or jump straight to a detent (hold-soc | hold-now | mountain | off)
voltdmf-ctl set-mode hold          # request one switch now, out of band (safety gate still applies)
voltdmf-ctl reload                 # re-read /etc/voltdmf/config.yaml
```

The daemon **boots armed** but passive: the shipped `config.yaml` sets
`default_position: hold-soc`, the first detent of the four-position selector,
so it enforces nothing until the SOC-HOLD floor engages or the driver taps SW1
round to `hold-now` or `mountain` — a mid-drive `systemctl restart` on a
healthy pack leaves the car where it is. The last detent, `off`, stands the
device down completely (the SOC floor included), and is the only way to
release a floor that has latched for the key cycle without restarting. There is no `--dry-run`. `voltdmf-ctl disarm` is the mid-drive
stop, `voltdmf-ctl arm` (or `systemctl restart voltdmf`) resumes. Start the
service with `--start-disarmed` for a host that should come up stopped. `voltdmf.socket` is socket-activated: `voltdmf-ctl`
works even if the service is momentarily down (the command queues briefly).

For bench work without the units, run the daemon with an explicit path:
`python -m voltdmf --config … --control-socket /tmp/voltdmf.sock` and point the
client at it with `--socket /tmp/voltdmf.sock` or `$VOLTDMF_CONTROL_SOCKET`.

**Overlay File System (do this LAST, after the daemon is validated):**
`sudo raspi-config` -> Performance Options -> Overlay File System = enabled;
accept the `/boot` write-protect prompt; then
`sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile`.
Keep `/etc/voltdmf/config.yaml` on a writable mount -- see DESIGN.md
"Hardware design / Power".

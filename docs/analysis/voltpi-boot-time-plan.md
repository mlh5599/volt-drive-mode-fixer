# voltpi boot-time reduction plan

**Why this matters:** the Pi is powered from the car's switched accessory
socket, so it cold-boots on every car start. Battery-protection coverage
does not begin until `voltdmf.service` is active. Today that is **~19–25 s**
after power-on.

## Measured baseline (2026-09-02)

`systemd-analyze`: **5.712 s (kernel) + 20.931 s (userspace) = 26.644 s**;
`multi-user.target` at 19.228 s; `voltdmf.service` active at **@19.224 s**.

Critical chain to `voltdmf.service`:

```
systemd-fsck@boot-firmware   @4.624s  +1.302s
 -> cloud-init-main          @6.049s  +3.194s
 -> cloud-init-local         @9.247s  +1.039s
 -> cloud-init-network       @10.291s
 -> sysinit.target           @10.550s
 -> voltdmf.socket           @10.570s
 -> basic.target             @10.590s
 -> NetworkManager.service   @10.949s +4.421s
 -> NetworkManager-wait-online @15.378s +3.814s
 -> network-online.target    @19.198s
 -> voltdmf.service          @19.224s
```

Everything after `basic.target` @10.6 s is wasted for this workload: the
daemon needs `can0` and its local control socket, nothing on the network.

`systemd-analyze blame` top offenders: NetworkManager 4.421s,
NetworkManager-wait-online 3.814s, cloud-init-main 3.194s, tailscaled 2.240s,
dev-mmcblk0p2.device 1.700s, fsck@boot-firmware 1.302s, cloud-init-local
1.039s, cloud-final 940ms, cloud-config 716ms, rpi-resize-swap-file 514ms,
voltdmf-can0-up 329ms.

Other findings:
- Full Raspberry Pi OS image (not Lite): `vc4-kms-v3d`, `camera_auto_detect`,
  `display_auto_detect`, audio all loaded on a headless box.
- `auto_initramfs=1` builds/loads a ~16 MB initramfs a plain ext4 PARTUUID
  root with `rootwait` does not need.
- cloud-init fully enabled (5 units) though the Pi is 100 % ansible-provisioned
  (static hostname, NM profiles from `wifi_failover`, users + SSH keys
  ansible-managed).
- rootfs is plain `rw` ext4 — **no overlayroot**. The SD card takes writes
  every boot; a power-yank mid-write risks fs corruption -> fsck -> slow or
  failed boot.
- `wpa_supplicant` and NetworkManager both enabled (redundant on Bookworm/NM).
- BT hardware already off (`dtoverlay=disable-bt`); `bluetooth`/`hciuart`
  inactive but still `enabled`.
- Enabled cruft: avahi-daemon, udisks2, e2scrub_reap, keyboard-setup,
  console-setup, rpi-eeprom-update, systemd-pstore, plus apt-daily /
  apt-daily-upgrade / man-db / system-upgrade-check / binary-version-check
  timers.
- No RTC on the Pi 3B: wall clock is wrong until timesyncd completes (needs
  network). The daemon runs entirely on `time.monotonic()`, so dropping the
  network wait does not affect its logic — only journal timestamps drift
  until sync.

## Tiered plan

### Tier 1 — daemon stops waiting on the network  ·  ≈ −8–9 s  ·  ansible, reversible

`roles/voltdmf/templates/voltdmf.service.j2`: drop `network-online.target`
from `After=`, delete `Wants=network-online.target`. Keep `Requires=/After=
voltdmf-can0-up.service voltdmf.socket`. Audit `voltdmf-btn.service` for the
same pattern.

Safe: SOC poll is CAN-only; config is a local file; converge is a separate
manual action; Tailscale / SSH / node_exporter come up independently.

Result: `voltdmf` active right after `basic.target` + `can0-up` (~10–11 s
now, earlier once Tier 2 shortens the pre-sysinit chain).

### Tier 2 — disable cloud-init  ·  ≈ −4–5 s  ·  shortens the pre-sysinit chain

`touch /etc/cloud/cloud-init.disabled` (fast, reversible) or
`apt purge cloud-init` (permanent). Manage the flag file from the role.
Nothing on this host needs it. Removes `cloud-init-main` (3.2 s) +
`cloud-init-local` (1.0 s) from the chain and unblocks `sysinit.target`.

### Tier 3 — `NetworkManager-wait-online` off the boot path  ·  ≈ −3.8 s

`systemctl disable NetworkManager-wait-online.service`. Once Tier 1 lands,
nothing should block boot on the network. Keep NetworkManager itself (the
`wifi_failover` role needs it).

**Tiers 1–3 together: `voltdmf` active in ~6–8 s instead of ~19–25 s.**
All reversible, all ansible-managed, no reboot-bricking risk.

### Tier 4 — kernel / firmware phase  ·  ≈ −2–4 s  ·  config.txt / cmdline.txt, reboot-risk

- `auto_initramfs=0` — plain ext4 + `rootwait` needs no initramfs.
- Drop the graphics stack on this headless unit: remove
  `dtoverlay=vc4-kms-v3d`, set `max_framebuffers=0`, `camera_auto_detect=0`,
  `display_auto_detect=0`, `dtparam=audio=off`, `gpu_mem=16`.
- `disable_splash=1`, `boot_delay=0`; add `quiet logo.nologo` to cmdline.
- `/boot/firmware` fstab passno -> 0 (stop fsck'ing the FAT partition every
  boot — that is the 1.3 s).
- **Test-reboot after each change.** Keep a known-good copy of both files and
  have the SD reader on hand — a bad `config.txt`/`cmdline.txt` can stop boot.
- First check which homelab-ansible role (if any) manages `config.txt` /
  `cmdline.txt` before editing them there. The `voltdmf` role owns only the
  `mcp2515-can0` overlay line.

### Tier 5 — mask unused services  ·  ≈ −1–3 s + less 4-core contention  ·  ansible

Mask: `avahi-daemon`(+socket), `udisks2`, `ModemManager`, `wpa_supplicant`,
`e2scrub_reap` + `e2scrub_all.timer`, `keyboard-setup`, `console-setup`,
`rpi-eeprom-update`, `systemd-pstore`, `bluetooth`, and the background-work
timers (`apt-daily*`, `man-db`, `system-upgrade-check`,
`binary-version-check`) — a car Pi should not apt in the background.

Keep: ssh, NetworkManager, tailscaled, node_exporter, cron, timesyncd,
voltdmf*. Put this in the role as a declarative list so it survives a
reimage.

### Tier 6 — structural, next reimage  ·  optional

- **Raspberry Pi OS Lite** — no desktop / X / pipewire / graphics; leaner
  base. The box is fully headless and ansible-provisioned; nothing needs the
  full image.
- **`overlayroot=tmpfs` (read-only root)** — every boot is clean, no fsck,
  no SD wear, power-yank-proof. The daemon already assumes no persisted
  state, so it fits. Speeds boot *and* removes the corruption risk of an SD
  card on a switched accessory socket.

## Target

| Stage        | voltdmf active after power-on |
|--------------|-------------------------------|
| Today        | ~19–25 s                      |
| Tier 1–3     | ~6–8 s                        |
| + Tier 4–5   | ~4–6 s, total boot < ~10 s    |
| + Tier 6     | ~4–6 s, corruption-proof      |

## Measuring

- `systemd-analyze critical-chain voltdmf.service` before/after each tier.
- The metric that matters: power-on -> first `0x1F4` decode -> first
  reconcile. Worth adding a daemon log line
  `protection live (first mode decode at +Xs)` so it shows in
  `journalctl -b -u voltdmf`.
- Reboot-test after every `config.txt` / `cmdline.txt` change.

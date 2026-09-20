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
- `wpa_supplicant` and NetworkManager both enabled — may be redundant on
  Bookworm/NM, but confirm NM does not drive the unit before touching it
  (see "Connectivity guarantee").
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

## Connectivity guarantee (headless box)

This Pi has no screen or keyboard — the only way in is SSH over Wi-Fi
(`voltpi.haguehome.lan`) or Tailscale (`voltpi`). Every change below must
leave Wi-Fi **coming up on its own every boot**. The distinction that makes
this safe:

- The headless requirement is "Wi-Fi *comes up*", **not** "boot *waits* for
  Wi-Fi". `NetworkManager` brings the link up either way. Tiers 1 and 3 only
  remove the *barrier units* that make other services block until it is up —
  they do not touch NM or the radio. SSH, Tailscale, and node_exporter all
  tolerate the network arriving a few seconds later (they listen on a
  wildcard / retry).
- Tiers 2–3 actually get Wi-Fi up **sooner**: cloud-init off and the
  pre-`sysinit` chain shortened means `NetworkManager.service` starts
  earlier, so time-to-SSH improves.

Hard rules for every tier:

1. **`NetworkManager.service` stays enabled and unmasked.** It is the thing
   that connects Wi-Fi. Not on any mask list. Do not add `After=` deps to it.
2. **Keep `cfg80211.ieee80211_regdom=US` on `cmdline.txt`** through all
   Tier 4 edits — it is the Wi-Fi regulatory domain; without it the radio can
   come up soft-blocked or with no usable channels.
3. **Keep both `wifi_failover` NM profiles** (home IOT priority 100, phone
   hotspot priority 50). The phone hotspot is the field rescue path if the
   home SSID is unreachable.
4. **Do not mask `wpa_supplicant`.** Checked 2026-09-03: `wpa_supplicant.service`
   is `enabled` **and `active`** — NM on this box drives the standalone unit
   rather than a D-Bus-spawned supplicant, so masking it would drop Wi-Fi.
   The saving is marginal; it is off the table.
5. **Only mask `avahi-daemon` if `voltpi.local` is genuinely unused** —
   Unbound (`voltpi.haguehome.lan`) and Tailscale MagicDNS (`voltpi`) both
   work without it, but confirm before removing mDNS.
6. After **every** reboot that follows a Tier 2 / 4 change, confirm SSH-in
   works before moving on. Have the SD reader on hand for Tier 4.

Verify-before-disable checklist for Tier 2 (cloud-init):

- `ls /etc/netplan/` and `ls /etc/NetworkManager/system-connections/` — the
  Wi-Fi profiles must be `wifi_failover`-written keyfiles, not
  cloud-init-rendered netplan that would stop regenerating.
  **Checked 2026-09-03: `/etc/netplan/` is empty; `nmcli dev` shows `wlan0`
  connected via the `home-iot` keyfile. No netplan to strand — Tier 2 is
  network-safe.**
- `cloud-init query --format '{{ ds }}'` / check `/etc/cloud/cloud.cfg.d/`
  for a `*networking*` or `99-installer*` drop-in.
- Disable via `/etc/cloud/cloud-init.disabled` (leaves already-rendered
  config in place); do **not** `apt purge` until a reboot proves the network
  survives.
- `systemd-networkd-wait-online.service` — **checked 2026-09-03: already
  `disabled`.** Only `NetworkManager-wait-online.service` (`enabled`) is left
  on the boot path for Tier 3 to take.

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

### Tier 1.5 — decouple voltdmf's units from sysinit.target  ·  ≈ −4.4 s  ·  ansible, reversible  ·  **done 2026-09-19**

`voltdmf.service`, `voltdmf.socket`, and `voltdmf-can0-up.service` all still
used the *default* `DefaultDependencies=yes`, which silently adds an implicit
`After=sysinit.target`/`After=basic.target` — that's what kept them behind
`cloud-init-network` → `cloud-init-local` → `cloud-init-main` even after Tier 1
removed `network-online.target`. Added `DefaultDependencies=no` to all three
units' `[Unit]` sections, plus a manually re-added `Conflicts=shutdown.target`
/ `Before=shutdown.target` pair on each (per `systemd.special(7)`, since
`DefaultDependencies=no` also strips the automatic orderly-shutdown ordering,
and this daemon holds an open SocketCAN raw socket + a serial LCD line).
Explicitly did **not** add `After=local-fs.target` — root `/` is mounted by
the kernel before PID 1 runs regardless, and adding it would silently
re-couple voltdmf to the `/boot/firmware` fsck on exactly the boots (post
dirty-shutdown, e.g. the ignition-off power blip) where that fsck runs
longest. cloud-init, NetworkManager, wifi_failover, and tailscaled are
untouched and keep running on their normal schedule — this only changes what
voltdmf's own units wait on.

Verified via reboot: `voltdmf.service` active at **@6.569s** (was @11.013s),
chain now ends cleanly at `sys-subsystem-net-devices-can0.device`, no
cloud-init in the path. `voltdmf-ctl status`, WiFi (`wlan0`/`home-iot`), and
Tailscale all confirmed still converging normally and independently.

### Tier 2 — disable cloud-init  ·  ≈ −4–5 s  ·  shortens the pre-sysinit chain  ·  **done 2026-09-19**

`touch /etc/cloud/cloud-init.disabled` (fast, reversible) or
`apt purge cloud-init` (permanent). Manage the flag file from the role.
Nothing on this host needs it. Removes `cloud-init-main` (3.2 s) +
`cloud-init-local` (1.0 s) from the chain and unblocks `sysinit.target`.

Implemented as declarative `roles/voltdmf` tasks gated by
`voltdmf_disable_cloud_init` (host_vars, voltpi only) — flipping the var back
to `false` and reconverging removes the flag file and re-enables cloud-init,
no manual cleanup. Pre-change state + a no-network/no-SSH SD-card rollback
procedure recorded at `docs/analysis/voltpi-tier2-3-backup.md`.

### Tier 3 — network `wait-online` barriers off the boot path  ·  ≈ −3.8 s  ·  **done 2026-09-19**

`systemctl disable NetworkManager-wait-online.service` (and
`systemd-networkd-wait-online.service` if enabled). These are **barrier
units only** — they hold `network-online.target` until a link is up. NM
still associates Wi-Fi in the background exactly as before; the link is not
guaranteed present at a fixed point in boot, which is fine because nothing
on this box needs to *block* on it. **Keep `NetworkManager.service` itself
enabled** — it is what brings Wi-Fi up.

Implemented alongside Tier 2 as declarative `roles/voltdmf` tasks gated by
`voltdmf_disable_wait_online` (host_vars, voltpi only), same
undo-by-reconverge pattern. `systemd-networkd-wait-online.service` was
already disabled (confirmed 2026-09-03) — nothing to do there.

Verified via reboot: userspace boot **19.937s → 13.911s**, `multi-user.target`
**17.842s → 13.910s** (measured immediately before vs. after this change,
both already on top of Tier 1.5). `voltdmf.service` itself unchanged at
@6.499s, as expected — Tier 1.5 already took it off this chain; Tiers 2/3
shorten the rest of boot instead. All 5 cloud-init units confirmed `enabled`
+ `inactive` (the flag suppressed their run, not just re-labeled it),
`NetworkManager-wait-online.service` confirmed `disabled` + `inactive`.
`wlan0` connected via `home-iot`, Tailscale connected, `voltdmf-ctl status`
answered normally, no new journal errors.

**Tiers 1–3 together: `voltdmf` active @6.5s, full userspace boot ~13.9s —
down from the ~19.2s / ~26.6s baseline.** All reversible, all
ansible-managed, no reboot-bricking risk.

### Tier 4 — kernel / firmware phase  ·  ≈ −2–4 s  ·  config.txt / cmdline.txt, reboot-risk  ·  **done 2026-09-19**

- `auto_initramfs=0` — plain ext4 + `rootwait` needs no initramfs.
- Dropped the graphics stack on this headless unit: removed
  `dtoverlay=vc4-kms-v3d`, set `max_framebuffers=0`, `camera_auto_detect=0`,
  `display_auto_detect=0`, `dtparam=audio=off`, `gpu_mem=16`.
- `disable_splash=1`, `boot_delay=0`; appended `quiet logo.nologo` to
  cmdline.
- `/boot/firmware` fstab passno -> 0 (stop fsck'ing the FAT partition every
  boot).

`geerlingguy.raspberry_pi` isn't in `playbooks/voltpi.yml`'s role list, so
nothing else touches these files on this host — `roles/voltdmf` owns them
outright via paired do/undo tasks (`voltdmf_tune_boot_config`, host_vars,
voltpi only) that edit/restore the exact original values already in the
file rather than templating a whole-file overwrite, so a stray SD-card-
specific value (the root `PARTUUID`) is never hardcoded anywhere in the
role. `cmdline.txt` in particular is handled as a token add/strip against
its own live content (slurp + compute + copy), not a static template, for
the same reason. Pre-change state + an SD-card disaster-rollback procedure
(FAT-partition mount for `config.txt`/`cmdline.txt`, rootfs mount for
`fstab`) recorded at `docs/analysis/voltpi-tier4-backup.md`.

Verified via reboot: kernel time **4.912s → 3.211s** (no more initramfs load
or FAT-partition fsck), userspace **13.559s → 12.515s**, `voltdmf.service`
**@6.248s → @5.939s**. Zero `systemctl --failed` units; `can0` up; WiFi
(`wlan0`/`home-iot`), Tailscale, and `voltdmf-ctl status` all confirmed
healthy post-reboot. Journal shows the expected, harmless fallout of
dropping the graphics stack on a headless box with no framebuffer consumer
(`bcm2708_fb`/`vc_sm_cma_vchi_init`/MMAL VCHI probe failures) — not a
regression, nothing else references those drivers.

### Tier 5 — mask unused services  ·  ≈ −1–3 s + less 4-core contention  ·  ansible  ·  **done 2026-09-19**

Masked: `udisks2`, `e2scrub_reap` + `e2scrub_all.timer`, `keyboard-setup`,
`console-setup`, `rpi-eeprom-update`, `systemd-pstore`, `bluetooth`, and the
background-work timers (`apt-daily*`, `man-db`, `system-upgrade-check`) — a
car Pi should not apt in the background. `ModemManager`/`hciuart` from the
original list don't exist on this trixie image, so they were dropped rather
than masked. `binary-version-check.timer` was deliberately **excluded**:
`roles/node_exporter` (which runs on every voltpi converge regardless of
this tier) re-templates it fresh every time, so masking it would just get
silently undone on the next `make voltpi` — fighting another role's
ownership instead of a real held-masked state.

Conditional item resolved: `avahi-daemon`(+socket) masked — confirmed with
the user that `voltpi.local` is genuinely unused; Unbound DNS
(`voltpi.haguehome.lan`) and Tailscale MagicDNS (`voltpi`) are the only names
anything resolves this host by. **Not `wpa_supplicant`** — confirmed
load-bearing on this box (2026-09-03).

**Never masked:** `NetworkManager`, `ssh`, `tailscaled`. Kept: `node_exporter`,
`cron`, `systemd-timesyncd`, `voltdmf*`. The mask list lives in
`roles/voltdmf/defaults/main.yml` (`voltdmf_masked_services`) as declarative
state gated by `voltdmf_mask_unused_services` / `voltdmf_mask_avahi`
(host_vars, voltpi only) — flip either back to `false` and reconverge to
unmask everything, no manual host cleanup. A generic pre-mask task removes
any stale real unit-file override at `/etc/systemd/system/<name>` before
masking (a no-op for genuine stock units, which only ever have a `.wants/`
symlink there) — needed because `system-upgrade-check.timer` turned out to
be a leftover real file from `roles/system_upgrade`, a role that is
permanently excluded from voltpi's playbook (`mobile_hosts`) and so will
never regenerate it.

Verified via reboot: userspace boot **13.911s → 13.559s**, `multi-user.target`
**13.910s → 13.542s**, `voltdmf.service` **@6.499s → @6.248s**. Smaller win
than the ≈1–3s estimate — most of these units were background/on-demand
rather than sitting on the critical chain, so the gain is mostly less
4-core contention during the busy first ~6s, not a shortened dependency
path. All 14 masked units + avahi-daemon confirmed `masked`/`inactive`
post-reboot (the four masked timers showed a one-boot `active=failed`
between the live mask and the reboot — an expected artifact of masking a
unit mid-run, not a recurring issue; clean `inactive` after the reboot).
NetworkManager, tailscaled, ssh, cron, timesyncd, voltdmf + can0-up + btn all
active; `wlan0` connected via `home-iot`; Tailscale connected; `voltdmf-ctl
status` answered normally.

**Tiers 1–5 together: `voltdmf.service` active @5.939s, full userspace boot
12.515s, total boot (kernel + userspace) 15.727s — down from the ~19.2s /
~26.6s baseline.** All reversible, all ansible-managed except Tier 4's
higher (but SD-card-recoverable) risk. Only Tier 6 (structural, next
reimage) remains, and it's optional.

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
| Original baseline (2026-09-02) | ~19.2 s / ~26.6 s total |
| Tier 1–5 (measured, 2026-09-19) | **@5.939s, ~12.5s userspace, ~15.7s total** |
| + Tier 6     | same active time, corruption-proof |

## Measuring

- `systemd-analyze critical-chain voltdmf.service` before/after each tier.
- The metric that matters: power-on -> first `0x1F4` decode -> first
  reconcile. Worth adding a daemon log line
  `protection live (first mode decode at +Xs)` so it shows in
  `journalctl -b -u voltdmf`.
- Reboot-test after every `config.txt` / `cmdline.txt` change.

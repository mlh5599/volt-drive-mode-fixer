# voltpi boot-time Tier 4 — pre-change state + rollback

Backup/rollback record for `docs/analysis/voltpi-boot-time-plan.md` Tier 4
(kernel/firmware boot phase trims), applied 2026-09-19 via `roles/voltdmf`
(`voltdmf_tune_boot_config` in voltpi's host_vars, homelab-ansible repo).

Higher risk than Tiers 2/3/5: `config.txt`/`cmdline.txt` control the kernel
and firmware boot itself, not a userspace service — a bad line here can stop
boot before SSH/Tailscale ever come up, so the normal (SSH) rollback path
may not be available. This doc exists so recovery is possible even with
voltpi completely unreachable over the network, by pulling its SD card.

## Pre-change state (captured 2026-09-19, before Tier 4 applied)

`/boot/firmware/config.txt` (relevant lines; full file has PARTUUID/board
sections `[cm4]`/`[cm5]`/`[pi5]`/`[all]` around this, untouched):

```
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
dtparam=audio=on
```

No `gpu_mem=`, `disable_splash=`, or `boot_delay=` lines present beforehand.

`/boot/firmware/cmdline.txt` (single line, in full — note the PARTUUID is
specific to this SD card, do not reuse it elsewhere):

```
console=tty1 root=PARTUUID=cfd275b2-02 rootfstype=ext4 fsck.repair=yes rootwait cfg80211.ieee80211_regdom=US
```

`/etc/fstab` (in full):

```
proc            /proc           proc    defaults          0       0
PARTUUID=cfd275b2-01  /boot/firmware  vfat    defaults          0       2
PARTUUID=cfd275b2-02  /               ext4    defaults,noatime  0       1
```

`systemd-analyze` baseline immediately before Tier 4 (i.e. with Tiers 1–3/5
already applied):

```
Startup finished in 4.912s (kernel) + 13.559s (userspace) = 18.472s
multi-user.target reached after 13.542s in userspace.
```

## What Tier 4 changes

- `config.txt`: `auto_initramfs` 1→0, `camera_auto_detect` 1→0,
  `display_auto_detect` 1→0, `dtparam=audio` on→off, `max_framebuffers` 2→0,
  removes `dtoverlay=vc4-kms-v3d`, adds `gpu_mem=16`, `disable_splash=1`,
  `boot_delay=0`.
- `cmdline.txt`: appends ` quiet logo.nologo` to the existing line (root
  PARTUUID, `fsck.repair=yes`, `rootwait`, `cfg80211.ieee80211_regdom=US` all
  preserved untouched).
- `/etc/fstab`: `/boot/firmware` passno `2`→`0` (stop fsck'ing the FAT
  partition every boot).

All applied by `roles/voltdmf`'s Phase 9 tasks as paired do/undo edits
against the file's own existing content (not a templated overwrite), so a
normal rollback is just flipping `voltdmf_tune_boot_config` back to `false`
and reconverging.

## Normal rollback (SSH reachable)

```
# In homelab-ansible, inventories/production/host_vars/voltpi.haguehome.lan/vars.yml:
#   voltdmf_tune_boot_config: false
make voltpi   # or: ansible-playbook playbooks/voltpi.yml --tags voltdmf
sudo reboot   # on voltpi
```

Or by hand on the box, no ansible needed — restore the exact pre-change
lines above with `sudo nano /boot/firmware/config.txt` /
`/boot/firmware/cmdline.txt` / `/etc/fstab`, then `sudo reboot`.

## Disaster rollback (SD card, no network access to voltpi)

Pull the SD card, mount it on another Linux machine (`lsblk` to find the
partition, `sudo mount /dev/sdX2 /mnt` — `sdX1` is the FAT `bootfs`/
`/boot/firmware`, `sdX2` is the ext4 `rootfs`).

**Undo `config.txt`** — mount the FAT partition (`sdX1`) instead, e.g.
`sudo mount /dev/sdX1 /mnt/boot`, then edit `/mnt/boot/config.txt` to restore
the six changed lines and remove the three added lines listed above.

**Undo `cmdline.txt`** — on the same FAT mount, restore
`/mnt/boot/cmdline.txt` to exactly:

```
console=tty1 root=PARTUUID=cfd275b2-02 rootfstype=ext4 fsck.repair=yes rootwait cfg80211.ieee80211_regdom=US
```

**Undo `/etc/fstab`** — on the ext4 `rootfs` mount (`sdX2`), edit
`<mnt>/etc/fstab`'s `/boot/firmware` line, change the trailing `0` back to
`2`:

```
PARTUUID=cfd275b2-01  /boot/firmware  vfat    defaults          0       2
```

Then unmount both, reinsert the card into voltpi, and power on.

If `auto_initramfs=0` turns out to actually need an initramfs on this image
(unexpected — the kernel is loaded via a plain ext4 root with `rootwait`
already, no LUKS/LVM/overlay in the boot path) and the Pi won't boot at all,
restoring `config.txt`'s `auto_initramfs=1` line via the FAT-partition mount
above is the fix — the firmware re-generates/reloads the initramfs from the
existing `/boot/firmware/initramfs8` file it never deleted, nothing to
regenerate.

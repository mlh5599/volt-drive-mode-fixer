# voltpi boot-time Tiers 2/3 — pre-change state + rollback

Backup/rollback record for `docs/analysis/voltpi-boot-time-plan.md` Tier 2
(disable cloud-init) and Tier 3 (disable `NetworkManager-wait-online.service`),
applied 2026-09-19 via `roles/voltdmf` (`voltdmf_disable_cloud_init` /
`voltdmf_disable_wait_online` in voltpi's host_vars, homelab-ansible repo).

Both changes are additive/reversible by design (see the plan doc), so there is
no existing file content to restore — only state to put back. This doc exists
so that's recoverable even if voltpi is unreachable over SSH/WiFi/Tailscale,
by pulling its SD card and mounting it on another machine.

## Pre-change state (captured 2026-09-19, before either tier applied)

```
$ systemctl is-enabled cloud-init-local.service cloud-init-network.service \
    cloud-init-main.service cloud-config.service cloud-final.service
enabled
enabled
enabled
enabled
enabled

$ ls -la /etc/cloud/cloud-init.disabled
ls: cannot access '/etc/cloud/cloud-init.disabled': No such file or directory

$ systemctl is-enabled NetworkManager-wait-online.service systemd-networkd-wait-online.service
enabled
disabled

$ ls -la /etc/systemd/system/network-online.target.wants/
NetworkManager-wait-online.service -> /usr/lib/systemd/system/NetworkManager-wait-online.service

$ systemd-analyze
Startup finished in 4.842s (kernel) + 19.937s (userspace) = 24.780s
multi-user.target reached after 17.842s in userspace.
```

`systemd-networkd-wait-online.service` was already disabled beforehand (noted
in the plan doc, 2026-09-03) — Tier 3 only touches
`NetworkManager-wait-online.service`.

## Normal rollback (SSH reachable)

Preferred path — declarative, matches how the change was applied:

1. In homelab-ansible, set both vars back to `false` in
   `inventories/production/host_vars/voltpi.haguehome.lan/vars.yml`:
   `voltdmf_disable_cloud_init: false`, `voltdmf_disable_wait_online: false`.
2. `make voltpi` (or `ansible-playbook playbooks/voltpi.yml --tags voltdmf`).
   The role's Phase 7 tasks remove `/etc/cloud/cloud-init.disabled` and
   re-enable `NetworkManager-wait-online.service` on their own.
3. Reboot voltpi (parked) and confirm SSH + `systemctl is-enabled` on the five
   cloud-init units + `NetworkManager-wait-online.service` all read `enabled`
   again.

Or by hand on the box, no ansible needed:

```
sudo rm -f /etc/cloud/cloud-init.disabled
sudo systemctl enable NetworkManager-wait-online.service
sudo reboot
```

## Disaster rollback (SD card, no network access to voltpi)

If a change leaves voltpi unreachable, pull the SD card and mount it on this
machine (or any Linux box) in a USB SD reader. The Pi's Raspberry Pi OS layout
is two partitions: a small FAT32 `bootfs` (`/boot/firmware`) and the ext4
`rootfs` (`/`). Only `rootfs` matters here. Linux mounts ext4 natively — no
special tooling needed, plug the reader in and either let auto-mount pick it
up or `sudo mount /dev/sdX2 /mnt` (confirm the partition number with
`lsblk` first; adjust `sdX` to whatever the reader enumerates as).

With `rootfs` mounted at `<mnt>`:

**Undo Tier 2 (cloud-init):**

```
sudo rm -f <mnt>/etc/cloud/cloud-init.disabled
```

That's the entire change — the file's mere existence is what disables
cloud-init; deleting it restores the original (enabled) behavior. Nothing
else was touched.

**Undo Tier 3 (NetworkManager-wait-online):**

```
sudo ln -s /usr/lib/systemd/system/NetworkManager-wait-online.service \
  <mnt>/etc/systemd/system/network-online.target.wants/NetworkManager-wait-online.service
```

This recreates the exact symlink captured above — `systemctl disable` only
ever removed this one symlink, it did not touch the unit file itself
(`/usr/lib/systemd/system/NetworkManager-wait-online.service`, package-owned,
untouched by either tier).

Then unmount, reinsert the card into voltpi, and power on.

Tier 1.5 (`DefaultDependencies=no` on voltdmf's own units, same session,
already reboot-verified) is unrelated to this doc and needs no rollback here
— it doesn't touch cloud-init or network-online.target at all.

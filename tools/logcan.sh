#!/usr/bin/env bash
# Timestamped CAN capture for Phase C signal discovery.
#
# Usage:  ./logcan.sh [outdir] [iface]
#
# `candump -l` writes its own replayable log file (candump-<timestamp>.log,
# usable with canplayer) into the current directory, so this just cd's there
# and runs it. Ctrl-C to stop.
#
# Keep captures off the Pi's SD card once the overlay FS is enabled -- pass a
# USB mount as outdir, or scp the file off afterwards.
#
# Handy filtered live view while this runs, in another shell:
#   candump <iface>,206:7FF,1E1:7FF,3E9:7FF
set -euo pipefail

OUTDIR="${1:-.}"
IFACE="${2:-can0}"

mkdir -p "$OUTDIR"
cd "$OUTDIR"
echo "Logging $IFACE into $(pwd)/candump-*.log   (Ctrl-C to stop)"
exec candump -l "$IFACE"

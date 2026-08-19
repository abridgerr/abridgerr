#!/bin/bash
# linuxserver.io-style startup: apply PUID/PGID/TZ from the environment,
# then drop from root to that user before running the real command. Runs
# as root only up to the `exec gosu` line -- needed to change ownership/ids
# at all, but nothing app-level ever runs as root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
TZ="${TZ:-Etc/UTC}"

if [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
else
    echo "[entrypoint] Unknown TZ '$TZ' -- leaving timezone unchanged."
fi

# -o (non-unique) since PGID/PUID may coincide with an existing system
# id -- harmless for a single-purpose service container, and mirrors what
# linuxserver.io's own images do here.
groupmod -o -g "$PGID" abc
usermod -o -u "$PUID" abc

chown abc:abc /config

# GPU access: matches linuxserver.io's own convention here ("we will
# automatically ensure the abc user inside of the container has the
# proper permissions to access this device") -- for Intel/AMD (/dev/dri)
# AND, defensively, NVIDIA too. /dev/dri's render node is group-owned, but
# that group's GID is host-specific (confirmed 991 for "render" on one
# real host here, NOT the well-known video=44 that card0 uses) -- so it
# can't be baked into the image at build time. Instead, match whatever
# GID actually owns each device node right now, reusing an existing
# container group of that GID if one already exists (e.g. video=44) or
# creating one on the fly otherwise, and add abc to it. Without this,
# QSV/VAAPI fail with a permission error at runtime even though the
# device itself is passed through fine, since abc is no longer root.
# The /dev/nvidia* entries are DEFENSIVE, not the primary mechanism:
# unlike /dev/dri, the NVIDIA Container Toolkit's own device nodes are
# normally already world-read/write (crw-rw-rw-) when it injects them, so
# this loop is usually a no-op for them -- but costs nothing to run the
# same match anyway, in case a particular host/toolkit version locks them
# to a group instead. (No device here means no NVIDIA GPU was passed
# through -- see docker-compose.example.yml -- so the loop just skips
# those entries via the existing-file check below, same as it always has
# for /dev/dri on an Intel/AMD-only host.)
for dev in /dev/dri/renderD128 /dev/dri/card0 \
           /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset; do
    [ -e "$dev" ] || continue
    dev_gid=$(stat -c '%g' "$dev")
    dev_group=$(getent group "$dev_gid" | cut -d: -f1)
    if [ -z "$dev_group" ]; then
        dev_group="hostgrp_$dev_gid"
        groupadd -g "$dev_gid" "$dev_group"
    fi
    usermod -aG "$dev_group" abc
done

echo "
-------------------------------------
GID/UID
-------------------------------------
User uid:    $(id -u abc)
User gid:    $(id -g abc)
-------------------------------------
"

exec gosu abc "$@"

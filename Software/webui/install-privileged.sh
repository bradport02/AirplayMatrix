#!/bin/bash
# install-privileged.sh -- install (or update) the web UI's root helper.
#
# airplaymatrix-privileged.py is the single root-owned script the sudoers
# rule lets the unprivileged web UI run (see airplaymatrix-webui.sudoers).
# Because it is the thing that holds root, it deliberately lives outside the
# repo checkout the web UI itself runs from -- a user who can write the
# checkout must not thereby be able to rewrite what runs as root. So
# updating it is a manual, deliberate step rather than something a code sync
# picks up:
#
#   ssh -t airplaymatrix-zero 'sudo bash ~/Documents/AirplayMatrix-main/Software/webui/install-privileged.sh'
#
# Mode 0700 root:root, matching how it was originally installed: only root
# may read or execute it directly, and the web UI reaches it solely through
# the narrow sudoers rule.
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)/airplaymatrix-privileged.py"
DEST=/usr/local/bin/airplaymatrix-privileged.py

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

if [[ ! -f "$SRC" ]]; then
  echo "source not found: $SRC" >&2
  exit 1
fi

# Syntax-check before overwriting: this script is the only path the web UI
# has to root, so shipping a version that won't even parse would take out
# every privileged action at once (Wi-Fi, hostname, reboot, the lot).
if ! python3 -m py_compile "$SRC"; then
  echo "refusing to install: $SRC does not compile" >&2
  exit 1
fi

echo "installing $SRC -> $DEST"
install -o root -g root -m 0700 "$SRC" "$DEST"

echo "restarting the web UI so it picks up any new routes/templates"
systemctl restart airplaymatrix-webui

echo "done. Available privileged commands:"
"$DEST" --help 2>&1 | sed -n '/{/,$p' | head -3

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
# Installs every root-owned script this project uses, not just the helper --
# an update that fixes the TV-power hook is no use if applying it still needs
# a separate SSH session. The helper stays 0700 root:root as it was
# originally installed: only root may read or execute it directly, and the
# web UI reaches it solely through the narrow sudoers rule.
set -eu

SOFTWARE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Every root-owned script, not just the helper. An update that fixes the
# TV-power hook is no use if applying it still needs a separate SSH session.
# "source|destination|mode".
SCRIPTS=(
  "$SOFTWARE_DIR/webui/airplaymatrix-privileged.py|/usr/local/bin/airplaymatrix-privileged.py|0700"
  "$SOFTWARE_DIR/cec/airplay-tv-power.sh|/usr/local/bin/airplay-tv-power.sh|0755"
  "$SOFTWARE_DIR/cec/airplay-cec-remote.py|/usr/local/bin/airplay-cec-remote.py|0755"
)

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

for entry in "${SCRIPTS[@]}"; do
  IFS="|" read -r src dest mode <<< "$entry"
  if [[ ! -f "$src" ]]; then
    echo "source not found: $src" >&2
    exit 1
  fi
  # Syntax-check before overwriting. These are the only paths this project
  # has to root, so shipping one that will not even parse would take out
  # every privileged action at once -- Wi-Fi, hostname, reboot, the lot.
  if [[ "$src" == *.py ]]; then
    python3 -m py_compile "$src" || { echo "refusing to install: $src does not compile" >&2; exit 1; }
  else
    bash -n "$src" || { echo "refusing to install: $src has a syntax error" >&2; exit 1; }
  fi
  echo "installing $src -> $dest"
  install -o root -g root -m "$mode" "$src" "$dest"
done


echo "restarting the web UI so it picks up any new routes/templates"
systemctl restart airplaymatrix-webui

echo "done. Available privileged commands:"
/usr/local/bin/airplaymatrix-privileged.py --help 2>&1 | sed -n '/{/,$p' | head -3

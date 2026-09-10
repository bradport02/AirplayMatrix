#!/bin/bash
# shairport-build-switch.sh -- keep both shairport-sync builds on disk and
# swap between them in seconds.
#
# Why this exists: rolling back the development-branch build once overwrote
# it with the stable one, and getting it back cost a ~20 minute rebuild on
# this single-core Zero (twice, thanks to a git-clone network blip). Both
# binaries are ~1MB. Keeping both means switching is a copy and a restart.
#
# The two builds, and why you'd want either:
#
#   dev     5.6-dev (upstream `development` branch). The ONLY build where
#           AirPlay 2 remote control works, so the TV remote's play/pause
#           and track skip reach the phone. Needs get_plist_metadata = "no"
#           in /etc/shairport-sync.conf (see fix-plist-metadata.sh) or the
#           display sits on "Connection received" with no track metadata.
#
#   stable  5.2.3 (upstream master). No AirPlay 2 remote control at all --
#           upstream simply hasn't implemented it outside the dev branch.
#           Here as the known-good fallback.
#
# Usage (run directly on the Zero):
#   sudo bash ~/shairport-build-switch.sh            # show what's on disk
#   sudo bash ~/shairport-build-switch.sh dev
#   sudo bash ~/shairport-build-switch.sh stable
#   sudo bash ~/shairport-build-switch.sh save-current dev   # adopt live binary
#
# Beneath all of this the untouched apt package remains the last-resort
# fallback: sudo systemctl revert shairport-sync && sudo systemctl restart
# shairport-sync.
set -euo pipefail

BIN=/usr/local/bin/shairport-sync
DEV="${BIN}.dev"
STABLE="${BIN}.stable"
SERVICE=shairport-sync.service

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

version_of() {
  [[ -x "$1" ]] || { echo "(absent)"; return; }
  "$1" --version 2>&1 | head -1
}

show_status() {
  echo "live   : $(version_of "$BIN")"
  echo "dev    : $(version_of "$DEV")"
  echo "stable : $(version_of "$STABLE")"
  echo
  echo "service: $(systemctl is-active "$SERVICE")"
}

install_build() {
  local src="$1" label="$2"
  [[ -x "$src" ]] || { echo "no $label build saved at $src" >&2; exit 1; }

  local want live
  want="$(version_of "$src")"
  live="$(version_of "$BIN")"
  if [[ "$want" == "$live" ]]; then
    echo "already running the $label build: $live"
    exit 0
  fi

  # Never lose whatever is currently live -- if it isn't already saved under
  # one of the two names, keep it before overwriting.
  if [[ "$live" != "$(version_of "$DEV")" && "$live" != "$(version_of "$STABLE")" ]]; then
    local keep="${BIN}.unsaved-$(date +%Y%m%d%H%M%S)"
    cp -a "$BIN" "$keep"
    echo "live binary wasn't saved under dev/stable -- kept a copy at $keep"
  fi

  echo "switching to the $label build"
  systemctl stop "$SERVICE"
  cp -a "$src" "$BIN"
  systemctl start "$SERVICE"
  sleep 2

  if ! systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE failed to start on the $label build" >&2
    echo "check: journalctl -u $SERVICE -n 50" >&2
    exit 1
  fi
  echo "now running: $(version_of "$BIN")"
}

case "${1-}" in
  dev)    install_build "$DEV" dev ;;
  stable) install_build "$STABLE" stable ;;
  save-current)
    slot="${2-}"
    case "$slot" in
      dev)    cp -a "$BIN" "$DEV"; echo "saved live binary as dev: $(version_of "$DEV")" ;;
      stable) cp -a "$BIN" "$STABLE"; echo "saved live binary as stable: $(version_of "$STABLE")" ;;
      *) echo "usage: $0 save-current dev|stable" >&2; exit 1 ;;
    esac
    ;;
  ""|status) show_status ;;
  *) echo "usage: $0 [dev|stable|status|save-current dev|stable]" >&2; exit 1 ;;
esac

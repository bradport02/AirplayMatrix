#!/bin/bash
# capture-remote-keys.sh -- record exactly what this TV's remote puts on the
# CEC bus, so the mapping in airplay-cec-remote.py can be based on observed
# traffic rather than on what the CEC spec says a TV ought to send.
#
# Why this exists: airplay-cec-remote.py currently maps only the D-pad
# (select/left/right), because an earlier capture found this TV sending
# nothing else. But that capture watched USER_CONTROL_PRESSED only, and a
# CEC 1.4 TV has two *other* ways to drive a playback device:
#
#   <Play>         (opcode 0x41) -- play forward/reverse, fast-forward,
#                                   rewind, at several speeds
#   <Deck Control> (opcode 0x42) -- skip forward/wind, skip reverse/rewind,
#                                   stop, eject
#
# Those are not user-control keypresses and would have been invisible to
# both the earlier capture and the running service. This script logs the
# whole bus, so whichever mechanism the TV uses shows up.
#
# It also runs a second pass with the Deck Control feature advertised. Right
# now `cec-ctl -d /dev/cec0 -S` reports "Device Features: None" for us,
# because airplay-tv-power.sh claims the address with a plain
# `cec-ctl --playback --osd-name ...`. A TV has no reason to send deck
# commands to a device that never said it could handle them, so the second
# pass re-registers with --feat-deck-control and asks for the same buttons
# again. If pass B shows traffic that pass A didn't, that flag is the fix.
#
# Nothing here is permanent: the logical address is re-claimed exactly the
# way airplay-tv-power.sh does it on the way out, and the remote service is
# restarted. Run as root (it stops/starts a system service):
#
#   ssh -t airplaymatrix-zero 'sudo bash ~/capture-remote-keys.sh'
#
# Then read ~/cec-capture.log (world-readable, so it can be pulled over a
# plain non-root SSH session).
set -u

CEC_DEV=/dev/cec0
SERVICE=airplay-cec-remote.service
OUT=/home/airplaymatrix/cec-capture.log
SECONDS_PER_PASS=${SECONDS_PER_PASS:-45}

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

osd_name() {
  # Match whatever name the power script is currently claiming, so this
  # capture doesn't leave the device advertising something different.
  cec-ctl -d "$CEC_DEV" -S 2>/dev/null |
    sed -n "s/^[[:space:]]*OSD Name[[:space:]]*: '\(.*\)'/\1/p" | head -1
}

NAME="$(osd_name)"
NAME="${NAME:-AirplayMatrix}"

banner() {
  echo
  echo "======================================================================"
  echo "$*"
  echo "======================================================================"
}

capture() {
  local label="$1"
  banner "$label"
  echo ">>> Now press these on the TV remote, pausing ~2s between each:"
  echo "      play/pause,  next/skip forward,  previous/skip back,"
  echo "      rewind,  fast-forward,  stop"
  echo ">>> Recording for ${SECONDS_PER_PASS}s..."
  {
    echo
    echo "### $label -- $(date -Is)"
  } >> "$OUT"
  # --monitor-all catches messages that aren't addressed to us as well as
  # those that are; a TV that decides we're not a deck may be broadcasting
  # or addressing something else, and that's exactly what we want to see.
  timeout "$SECONDS_PER_PASS" cec-ctl -d "$CEC_DEV" --monitor-all >> "$OUT" 2>&1
  echo ">>> done."
}

echo "logging to $OUT"
: > "$OUT"

echo "stopping $SERVICE for the duration of the capture"
systemctl stop "$SERVICE"

capture "PASS A -- current configuration (Device Features: none)"

echo
echo "re-registering with the Deck Control feature advertised..."
cec-ctl -d "$CEC_DEV" --playback --feat-deck-control --osd-name "$NAME" >/dev/null 2>&1
cec-ctl -d "$CEC_DEV" -S 2>&1 | sed -n '/Device Features/,+3p' | tee -a "$OUT"

capture "PASS B -- with --feat-deck-control advertised"

echo
echo "restoring the original registration and restarting $SERVICE"
cec-ctl -d "$CEC_DEV" --playback --osd-name "$NAME" >/dev/null 2>&1
systemctl start "$SERVICE"

chmod 644 "$OUT"
echo
echo "capture complete -> $OUT"
echo "summary of what arrived:"
grep -oE "USER_CONTROL_PRESSED|DECK_CONTROL|^PLAY |ui-cmd: [a-z ]+|deck-control-mode: [a-z ]+|play-mode: [a-z -]+" "$OUT" |
  sort | uniq -c | sort -rn | head -20

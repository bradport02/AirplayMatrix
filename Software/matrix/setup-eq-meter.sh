#!/bin/bash
# setup-eq-meter.sh -- one-time root provisioning for the LED matrix's live
# "EQ meter" display mode (Software/display_settings.py's eq_meter_enabled,
# toggled from the web UI's /matrix page). Run once per device, as root:
#
#   ssh -t airplaymatrix-zero 'sudo bash ~/setup-eq-meter.sh'
#
# What this does, and why: Software/matrix/eq_meter.py needs a copy of the
# actual AirPlay audio to compute band levels from, without disturbing the
# real playback path shairport-sync already uses. That's a kernel ALSA
# loopback (`snd-aloop`) plus a `route`+`multi` PCM that fans the exact same
# stream out to both the real hardware output AND the loopback's playback
# side -- so the audio actually heard on the TV goes through the *same*
# hardware device as before, just wrapped in one extra (lossless, no
# resampling unless the hardware itself needs it) plugin layer. eq_meter.py
# only ever reads the *other* side of the loopback; it never touches
# playback, so a bug in it can make the meter wrong or blank but can't
# affect what's actually heard.
#
# Idempotent: safe to re-run (e.g. after this project's setup.sh, or to
# pick up a config.txt HDMI-audio-device rename) -- it overwrites its own
# marked block rather than appending to it, and only backs up
# shairport-sync.conf once (a second run sees the .orig already there and
# leaves it alone).
#
# Rollback: `sudo bash ~/setup-eq-meter.sh --revert` restores
# shairport-sync.conf's original alsa.output_device and restarts the
# service. It leaves snd-aloop loaded and asound.conf's stanza in place
# (harmless without anything asking for the eqtap device) rather than
# tearing them down, since the ALSA plugin config being present costs
# nothing when unused.
set -eu

SHAIRPORT_CONF=/etc/shairport-sync.conf
ASOUND_CONF=/etc/asound.conf
MODULES_LOAD_FILE=/etc/modules-load.d/airplaymatrix-eq-meter.conf
REAL_DEVICE_FILE=/etc/airplaymatrix-eq-meter-real-device
MARKER_BEGIN="# --- airplaymatrix EQ meter (managed block, see Software/matrix/setup-eq-meter.sh) ---"
MARKER_END="# --- end airplaymatrix EQ meter block ---"

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (sudo)" >&2
  exit 1
fi

log() { echo "[eq-meter-setup] $*"; }

# The real hardware ALSA device shairport-sync currently plays to, read
# straight out of its own config rather than hardcoded -- so this keeps
# working if a USB DAC ever replaces the Pi's HDMI audio (see
# shairport-sync.conf's own comment on that placeholder). Falls back to the
# vc4-hdmi device this project's setup.sh actually configures by default.
current_output_device() {
  sed -n 's/^[[:space:]]*output_device[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$SHAIRPORT_CONF" 2>/dev/null | head -1
}

revert() {
  if [[ -f "${SHAIRPORT_CONF}.orig" ]]; then
    log "restoring ${SHAIRPORT_CONF} from ${SHAIRPORT_CONF}.orig"
    cp "${SHAIRPORT_CONF}.orig" "$SHAIRPORT_CONF"
  else
    log "no ${SHAIRPORT_CONF}.orig backup found -- was setup-eq-meter.sh ever run? nothing to revert"
    exit 1
  fi
  systemctl restart shairport-sync
  log "reverted. snd-aloop and ${ASOUND_CONF}'s eqtap stanza are left in place (harmless, unused)."
  exit 0
}

if [[ "${1:-}" == "--revert" ]]; then
  revert
fi

real_device=$(current_output_device)
if [[ -z "$real_device" ]]; then
  echo "could not read alsa.output_device out of $SHAIRPORT_CONF -- refusing to guess" >&2
  exit 1
fi
if [[ "$real_device" == "eqtap" ]]; then
  # Already pointed at our own virtual device (a re-run after the first
  # setup) -- the real device name isn't recoverable from shairport-
  # sync.conf any more, so it was saved to REAL_DEVICE_FILE the first time
  # this script ran, specifically so a re-run (e.g. after this project's
  # setup.sh, or to pick up a config.txt HDMI-audio-device rename) doesn't
  # need to guess or wrap eqtap in another layer of itself.
  if [[ ! -f "$REAL_DEVICE_FILE" ]]; then
    echo "eqtap is already shairport-sync's output_device but $REAL_DEVICE_FILE (recorded on first run) is missing -- run --revert first, then re-run this script" >&2
    exit 1
  fi
  real_device=$(cat "$REAL_DEVICE_FILE")
fi
log "real hardware output device: $real_device"
echo "$real_device" > "$REAL_DEVICE_FILE"

log "loading snd-aloop and persisting it across reboots"
modprobe snd-aloop
cat > "$MODULES_LOAD_FILE" <<EOF
# Installed by Software/matrix/setup-eq-meter.sh -- provides the loopback
# device the EQ meter reads audio from. See that script for the full
# picture.
snd-aloop
EOF

log "writing ${ASOUND_CONF}'s eqtap stanza"
touch "$ASOUND_CONF"
# Strip any previous run's block before appending a fresh one, so re-running
# after e.g. the real output device changed doesn't leave a stale duplicate
# stanza behind.
if grep -qF "$MARKER_BEGIN" "$ASOUND_CONF" 2>/dev/null; then
  sed -i "/^${MARKER_BEGIN//\//\\/}\$/,/^${MARKER_END//\//\\/}\$/d" "$ASOUND_CONF"
fi
cat >> "$ASOUND_CONF" <<EOF
$MARKER_BEGIN
# Fans shairport-sync's stereo output out to two places at once: the real
# hardware device it was already using (slave "a", used exactly as-is --
# whatever rate/format negotiation it already did continues to happen the
# same way, "default" included) and one side of the snd-aloop loopback
# (slave "b"). "multi" alone would need a 4-channel *input* stream to
# actually feed both slaves (2 channels per slave) -- shairport-sync only
# ever produces 2 -- so "eqtap" on top is a "route" plugin that duplicates
# the incoming 2 channels onto both halves of that 4-channel stream.
pcm.airplaymatrix_eqtap_multi {
    type multi
    slaves.a.pcm "$real_device"
    slaves.a.channels 2
    slaves.b.pcm "plughw:Loopback,0,0"
    slaves.b.channels 2
    bindings.0.slave a
    bindings.0.channel 0
    bindings.1.slave a
    bindings.1.channel 1
    bindings.2.slave b
    bindings.2.channel 0
    bindings.3.slave b
    bindings.3.channel 1
}
pcm.airplaymatrix_eqtap_route {
    type route
    slave.pcm "airplaymatrix_eqtap_multi"
    slave.channels 4
    ttable.0.0 1
    ttable.1.1 1
    ttable.0.2 1
    ttable.1.3 1
}
pcm.eqtap {
    type plug
    slave.pcm "airplaymatrix_eqtap_route"
}
$MARKER_END
EOF

if [[ ! -f "${SHAIRPORT_CONF}.orig" ]]; then
  log "backing up ${SHAIRPORT_CONF} -> ${SHAIRPORT_CONF}.orig (first run only)"
  cp "$SHAIRPORT_CONF" "${SHAIRPORT_CONF}.orig"
fi

if [[ "$(current_output_device)" != "eqtap" ]]; then
  log "pointing shairport-sync's alsa.output_device at eqtap"
  sed -i 's/^\([[:space:]]*output_device[[:space:]]*=[[:space:]]*\)"[^"]*"/\1"eqtap"/' "$SHAIRPORT_CONF"
fi

log "restarting shairport-sync"
systemctl restart shairport-sync

log "done. Verify with: arecord -D hw:Loopback,1,0 -f S16_LE -r 44100 -c 2 -d 3 /tmp/eq-test.wav"
log "(while something is actually playing over AirPlay) -- a non-silent file means the tap works."
log "Then enable the EQ meter from the web UI's /matrix page."

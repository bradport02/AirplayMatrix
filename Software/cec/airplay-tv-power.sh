#!/bin/bash
# airplay-tv-power.sh — HDMI-CEC TV power control, driven by shairport-sync
# session-state hooks (see /etc/shairport-sync.conf, sessioncontrol block):
#   run_this_before_entering_active_state -> "airplay-tv-power.sh on"
#   run_this_after_exiting_active_state   -> "airplay-tv-power.sh off"
#
# "entering active state" fires when an AirPlay device connects/starts a
# session. "exiting active state" fires sessioncontrol.active_state_timeout
# seconds (set to 300 = 5 min in the conf) after playback stops, so the
# 5-minute idle timeout before power-off is handled natively by
# shairport-sync -- no timer/debounce logic needed here. Note sessioncontrol
# only has room for one command per event, which is why the OSD name is
# claimed inside these cases rather than from a hook of its own.
set -u

CEC_DEV=/dev/cec0   # HDMI0 -- the only connected port on this Pi
LOG_TAG=airplay-tv-power
SHAIRPORT_CONF=/etc/shairport-sync.conf

log() { logger -t "$LOG_TAG" "$*"; }

# Whether shairport-sync is playing *right now*, straight from its own MPRIS
# interface. The standby path below checks this before pulling the trigger.
#
# Why it has to: "exiting active state" is fired by a timer shairport-sync
# started when playback stopped, and the timer is not cancelled by playback
# resuming -- so pausing for longer than active_state_timeout and then
# hitting play again lands the TV in standby while the music is audibly
# playing. That was reported live: a long pause, resume, and the TV went
# off underneath the resumed track. This turns the hook into "go to standby
# unless something is actually playing", which is what it always meant.
is_playing() {
  local status
  # --reply-timeout is not optional here. This runs from shairport-sync's
  # own exit hook, which fires while the service is stopping -- so the D-Bus
  # name it is querying is its own, and on its way out. Without a timeout
  # dbus-send waits its 25s default, systemd waits for the hook, and
  # "systemctl stop" appears to hang for half a minute. Failing fast reads
  # as "not playing", which is the right answer when it is shutting down.
  status=$(dbus-send --system --reply-timeout=2000 --print-reply=literal \
             --dest=org.mpris.MediaPlayer2.ShairportSync \
             /org/mpris/MediaPlayer2 \
             org.freedesktop.DBus.Properties.Get \
             string:org.mpris.MediaPlayer2.Player string:PlaybackStatus 2>/dev/null \
           | tr -d '[:space:]')
  [[ "$status" == *Playing* ]]
}

cec() { cec-ctl -d "$CEC_DEV" "$@" >/dev/null 2>&1; }

# Volume is deliberately NOT set here any more. It used to push one over
# D-Bus on every session, then briefly set the ALSA mixer instead; neither
# does what the setting promises. The AirPlay volume can only be moved by
# offering it to the sender during session setup, so it is now
# shairport-sync's own `default_airplay_volume`, written into
# /etc/shairport-sync.conf by the web UI via
# webui/airplaymatrix-privileged.py's set-connect-volume. That command is
# the single writer on purpose: two things attenuating from one percentage
# would mean 60% applied twice.

# The TV's HDMI-input label (what many TVs show instead of "HDMI 1", the
# same way a Chromecast/Apple TV/games console's name usually appears there)
# is CEC's <Set OSD Name>, configured at logical-address-claim time via
# cec-ctl's --osd-name -- the kernel's CEC framework then answers any
# <Give OSD Name> query on our behalf for as long as the address stays
# claimed, no persistent listener process needed (confirmed live: other
# real devices already on the bus, e.g. an AV receiver and a games console,
# show up the same way via `cec-ctl -d /dev/cec0 -S`).
#
# Sourced fresh from shairport-sync.conf on every claim rather than cached,
# so renaming the device from the web UI takes effect on the next session
# without needing a separate hook there. <Set OSD Name>'s payload is a hard
# 14-byte protocol limit (see the HDMI-CEC spec) -- longer names get
# silently truncated by cec-ctl itself, so this truncates deterministically
# first instead: pick a name in the web UI you're fine seeing cut to its
# first 14 characters.
osd_name() {
  local name
  name=$(sed -n 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$SHAIRPORT_CONF" 2>/dev/null | head -1)
  printf '%s' "${name:-AirPlay}" | cut -c1-14
}

case "${1:-}" in
  on)
    log "AirPlay session active -> waking TV"
    cec --playback --osd-name "$(osd_name)"   # claim a logical address (idempotent)
    my_phys_addr=$(cec-ctl -d "$CEC_DEV" -x 2>/dev/null | tail -n1)
    cec --to 0 --image-view-on
    cec --to 0 --active-source phys-addr="$my_phys_addr"
    ;;
  wake)
    # Fired by the display app the moment a device connects (its metadata
    # stream carries a "conn"/"snam" item roughly half a second before
    # playback begins) -- shairport-sync itself has no connection-level
    # hook, only play/active-state ones, so this cannot come from the
    # conf's sessioncontrol block. Safe to run repeatedly; waking an
    # already-on TV is a no-op.
    log "AirPlay device connected -> waking TV"
    cec --playback --osd-name "$(osd_name)"
    my_phys_addr=$(cec-ctl -d "$CEC_DEV" -x 2>/dev/null | tail -n1)
    cec --to 0 --image-view-on
    cec --to 0 --active-source phys-addr="$my_phys_addr"
    ;;
  off)
    if is_playing; then
      log "idle timeout fired but playback has resumed -> leaving TV on"
      exit 0
    fi
    log "AirPlay session ended (5 min idle) -> standby TV"
    cec --playback --osd-name "$(osd_name)"
    cec --to 0 --standby
    ;;
  *)
    echo "usage: $0 {on|wake|off}" >&2
    exit 1
    ;;
esac

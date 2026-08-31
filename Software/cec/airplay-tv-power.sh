#!/bin/bash
# airplay-tv-power.sh — HDMI-CEC power control + default connect volume,
# driven by shairport-sync session-state hooks (see /etc/shairport-sync.conf,
# sessioncontrol block):
#   run_this_before_entering_active_state -> "airplay-tv-power.sh on"
#   run_this_after_exiting_active_state   -> "airplay-tv-power.sh off"
#
# "entering active state" fires when an AirPlay device connects/starts a
# session. "exiting active state" fires sessioncontrol.active_state_timeout
# seconds (set to 300 = 5 min in the conf) after playback stops, so the
# 5-minute idle timeout before power-off is handled natively by
# shairport-sync -- no timer/debounce logic needed here. Volume-setting
# lives in the "on" case here rather than its own hook for the same reason
# the OSD name doesn't: sessioncontrol only has room for one command per
# event.
set -u

CEC_DEV=/dev/cec0   # HDMI0 -- the only connected port on this Pi
LOG_TAG=airplay-tv-power
SHAIRPORT_CONF=/etc/shairport-sync.conf

# Web UI-editable settings (Software/display_settings.py, "AirPlay connect
# volume" card): connect_volume_percent, in the same JSON file the kiosk
# apps read for their own settings. Hardcoded absolute path, not
# Path.home()-relative like display_settings.py itself gets away with --
# this script runs as the `shairport-sync` user (see sessioncontrol in
# shairport-sync.conf), not `airplaymatrix`, so that would resolve to the
# wrong home directory entirely. No Python dependency either: this is a
# system-user shell script, and the JSON is simple enough for grep/sed.
DISPLAY_SETTINGS=/home/airplaymatrix/.config/airplaymatrix-display/config.json
CONNECT_VOLUME_DEFAULT=75

log() { logger -t "$LOG_TAG" "$*"; }

cec() { cec-ctl -d "$CEC_DEV" "$@" >/dev/null 2>&1; }

connect_volume_percent() {
  local pct
  pct=$(sed -n 's/.*"connect_volume_percent"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' \
          "$DISPLAY_SETTINGS" 2>/dev/null | head -1)
  [[ "$pct" =~ ^[0-9]+$ ]] || pct=$CONNECT_VOLUME_DEFAULT
  (( pct < 0 )) && pct=0
  (( pct > 100 )) && pct=100
  printf '%s' "$pct"
}

# shairport-sync's D-Bus Volume property is AirPlay's native scale: -30.0dB
# (quietest) to 0.0dB (loudest), linear -- see org.gnome.ShairportSync's
# introspection. The percent-to-dB mapping here is the same one AirPlay
# clients themselves use for their volume slider, so 75% here reads the
# same as if the phone's slider were dragged to 75%. The D-Bus policy
# (/etc/dbus-1/system.d/shairport-sync-dbus.conf) explicitly allows anyone
# to invoke methods/set properties on the service, so this needs no special
# permissions beyond what this script already runs as.
set_connect_volume() {
  local pct="$1" db
  db=$(awk -v p="$pct" 'BEGIN { printf "%.2f", -30.0 + (p / 100.0) * 30.0 }')
  if dbus-send --system --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
       org.freedesktop.DBus.Properties.Set \
       string:org.gnome.ShairportSync string:Volume "variant:double:$db" >/dev/null 2>&1; then
    log "set connect volume to ${pct}% (${db}dB)"
  else
    log "failed to set connect volume via D-Bus"
  fi
}

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
    set_connect_volume "$(connect_volume_percent)"
    ;;
  off)
    log "AirPlay session ended (5 min idle) -> standby TV"
    cec --playback --osd-name "$(osd_name)"
    cec --to 0 --standby
    ;;
  *)
    echo "usage: $0 {on|off}" >&2
    exit 1
    ;;
esac

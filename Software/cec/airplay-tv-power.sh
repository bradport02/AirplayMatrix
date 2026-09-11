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
#
# Reading this at all requires /home/airplaymatrix to be traversable by
# other users (o+x). Recent Raspberry Pi OS creates home directories 0700,
# which silently broke this: the file itself is world-readable, but a
# system user cannot reach it through a 0700 parent, so every session
# quietly fell back to CONNECT_VOLUME_DEFAULT and the web UI's setting did
# nothing. The symptom to recognise is the log below reporting a volume
# the web UI was never set to. setup.sh applies the o+x; if this script is
# installed by hand, do it there too.
DISPLAY_SETTINGS=/home/airplaymatrix/.config/airplaymatrix-display/config.json
CONNECT_VOLUME_DEFAULT=75

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

connect_volume_percent() {
  local pct
  pct=$(sed -n 's/.*"connect_volume_percent"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' \
          "$DISPLAY_SETTINGS" 2>/dev/null | head -1)
  [[ "$pct" =~ ^[0-9]+$ ]] || pct=$CONNECT_VOLUME_DEFAULT
  (( pct < 0 )) && pct=0
  (( pct > 100 )) && pct=100
  printf '%s' "$pct"
}

# AirPlay's native volume scale is -30.0dB (quietest) to 0.0dB (loudest),
# linear, and the percent-to-dB mapping here is the same one AirPlay clients
# use for their own slider -- so 75% here reads the same as dragging the
# phone's slider to 75%. The D-Bus policy
# (/etc/dbus-1/system.d/shairport-sync-dbus.conf) lets anyone set properties
# on the service, so no special permissions are needed beyond what this
# script already runs as.
#
# Which property, though, changed under shairport-sync 5.x, and silently.
# There are now two, on two different interfaces:
#
#   org.gnome.ShairportSync              Volume         local output volume,
#                                                       sitting with
#                                                       LoudnessEnabled and
#                                                       ConvolutionGain
#   org.gnome.ShairportSync.RemoteControl AirplayVolume the AirPlay-scale
#                                                       volume shared with
#                                                       the sender
#
# This used to set the former, which on 5.x attenuates locally instead of
# telling the phone anything. RemoteControl is the right one -- and note it
# is the same interface the TV remote's transport controls go through, so
# like them it only works on the development build (see
# cec/upgrade-shairport-dev.sh). The legacy property is kept as a fallback
# so switching back to the stable build (shairport-build-switch.sh stable)
# doesn't silently stop setting any volume at all.
#
# Everything here is verified by reading the value back, because dbus-send
# CANNOT be trusted to report this. Setting a property that does not exist
# on the named interface still exits 0 -- confirmed live -- which is exactly
# how the wrong property went unnoticed: the old code logged "set connect
# volume" every single time while doing nothing at all.
#
# The read-back is compared with a 1dB tolerance rather than for equality:
# AirPlay quantises what it is given (a requested -5.00 reads back as
# -5.25), so an exact match never happens.
set_connect_volume() {
  local pct="$1" db iface prop
  db=$(awk -v p="$pct" 'BEGIN { printf "%.2f", -30.0 + (p / 100.0) * 30.0 }')

  # Choose the target by *capability*, never by trying one and falling
  # through on a bad result. The two properties control different things --
  # one talks to the phone, the other attenuates locally -- so a fallback
  # triggered by a value mismatch could set both and attenuate twice,
  # leaving the music quieter than either setting asked for. Whether
  # AirplayVolume can be read at all is the honest test of which build is
  # running; whether the write then sticks is a separate question, reported
  # below but never used to pick a different target.
  if _read_volume org.gnome.ShairportSync.RemoteControl AirplayVolume >/dev/null; then
    iface=org.gnome.ShairportSync.RemoteControl
    prop=AirplayVolume
  else
    iface=org.gnome.ShairportSync
    prop=Volume
    log "RemoteControl.AirplayVolume unavailable -- using the legacy Volume property"
  fi

  dbus-send --system --reply-timeout=2000 --dest=org.gnome.ShairportSync \
    /org/gnome/ShairportSync org.freedesktop.DBus.Properties.Set \
    "string:$iface" "string:$prop" "variant:double:$db" >/dev/null 2>&1

  # Read back and compare with a 1dB tolerance rather than for equality:
  # AirPlay quantises what it is given (-5.00 comes back as -5.25).
  local got
  got=$(_read_volume "$iface" "$prop")
  if [[ "$got" =~ ^-?[0-9.]+$ ]] \
     && awk -v a="$got" -v b="$db" 'BEGIN { exit !((a - b < 1.0) && (b - a < 1.0)) }'; then
    log "set connect volume to ${pct}% (${db}dB) via ${prop}"
  else
    # Worth logging loudly rather than swallowing. AirPlay volume is a
    # property of a *session*: with none established, every route tested
    # (the property, RemoteControl.SetAirplayVolume, and
    # AdvancedRemoteControl.SetVolume) accepts the call, returns success
    # and changes nothing. If this shows up on every connection, the hook
    # is firing before the session is ready to be told anything.
    log "connect volume ${pct}% (${db}dB) did not stick via ${prop} (read back '${got:-nothing}')"
  fi
}

# Read one D-Bus double property. Fails if the property is absent on that
# interface, which is what distinguishes the development build (where
# RemoteControl carries the AirPlay volume) from the stable one.
#
# The read is the only trustworthy half of this exchange: dbus-send exits 0
# when *setting* a property that does not exist at all -- confirmed live --
# which is exactly how the wrong property went unnoticed for so long. The
# old code logged "set connect volume" every time while doing nothing.
_read_volume() {
  local got
  got=$(dbus-send --system --reply-timeout=2000 --print-reply=literal \
          --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
          org.freedesktop.DBus.Properties.Get \
          "string:$1" "string:$2" 2>/dev/null | awk '{ print $NF }')
  [[ "$got" =~ ^-?[0-9.]+$ ]] || return 1
  printf '%s' "$got"
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
  wake)
    # Fired by the display app the moment a device connects (its metadata
    # stream carries a "conn"/"snam" item roughly half a second before
    # playback begins) -- shairport-sync itself has no connection-level
    # hook, only play/active-state ones, so this cannot come from the
    # conf's sessioncontrol block. Deliberately does NOT set the volume:
    # that belongs to a session actually starting, not to a device merely
    # selecting this receiver. Safe to run repeatedly; waking an already-on
    # TV is a no-op.
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

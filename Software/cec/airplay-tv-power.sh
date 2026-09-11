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

# Sets this device's own output volume, via the ALSA mixer.
#
# It used to push a volume over shairport-sync's D-Bus interface instead,
# on the reasoning that the AirPlay volume is the one the phone shares. On
# shairport-sync 5.x (the development build this runs, see
# cec/upgrade-shairport-dev.sh) that simply does not work, and it fails
# silently in both directions. Measured live, during actual playback, every
# route accepts the call, returns success, and changes nothing:
#
#   RemoteControl.AirplayVolume       property write -> value unchanged
#   RemoteControl.SetAirplayVolume    method call    -> value unchanged
#   AdvancedRemoteControl.SetVolume   method call    -> value unchanged
#   org.gnome.ShairportSync.Volume    not writable at all; reads back a
#                                     fixed value whatever you set
#
# RemoteControl.Available reads false even mid-song, which is the clue:
# AirPlay volume belongs to the sender, and this build has no way to make
# it move. (dbus-send exits 0 when setting a property that does not exist
# on the named interface, which is how the old code managed to log
# "set connect volume" on every single session while doing nothing at all.)
#
# So this sets the volume it *can* set: the ALSA playback control the audio
# actually leaves through. That is a deliberately different promise from
# the old one. It sets this device's output level, not the phone's slider,
# and the phone's slider still works on top of it -- which is the right way
# round for what the setting is for, namely not being deafened or inaudible
# when a phone connects at whatever level it last remembered.
#
# The percent-to-dB mapping is kept at AirPlay's own -30.0dB..0.0dB rather
# than stretched over the control's full range, so a setting made before
# this change still means the same loudness. It matters here: this control
# bottoms out at -51dB, and mapping 0-100% onto that would make everything
# below about a third inaudible rather than merely quiet.
#
# Card and control are named explicitly because "the default device" is not
# a thing the mixer can be asked about: output_device in
# /etc/shairport-sync.conf is "eqtap", an ALSA plugin chain that ends at
# card 0 (see /etc/asound.conf). Attach a USB DAC and both that setting and
# these two need changing together.
MIXER_CARD=0
MIXER_CONTROL=PCM

set_connect_volume() {
  local pct="$1" db got
  db=$(awk -v p="$pct" 'BEGIN { printf "%.2f", -30.0 + (p / 100.0) * 30.0 }')

  # The `--` is load-bearing: every volume below 100% is a negative dB
  # value, and without it amixer parses that leading minus as an option and
  # refuses the whole command. It fails for 0-99% and succeeds for exactly
  # 100%, which is as misleading as a bug gets.
  if ! amixer -c "$MIXER_CARD" -- sset "$MIXER_CONTROL" "${db}dB" >/dev/null 2>&1; then
    log "failed to set connect volume: no '$MIXER_CONTROL' control on card $MIXER_CARD"
    return
  fi

  # Read back rather than trusting the exit status, for the same reason the
  # D-Bus version had to: a mixer write that lands somewhere unexpected is
  # worth knowing about, and this whole feature spent months reporting
  # success while doing nothing. Tolerance is 1dB -- the control quantises
  # to its own step size (255 steps over 51dB, so about 0.2dB), and asking
  # for -7.50 lands on the nearest step rather than exactly.
  got=$(amixer -c "$MIXER_CARD" sget "$MIXER_CONTROL" 2>/dev/null \
        | grep -om1 '\[-\?[0-9.]*dB\]' | tr -d '[]dB')
  if [[ "$got" =~ ^-?[0-9.]+$ ]] \
     && awk -v a="$got" -v b="$db" 'BEGIN { exit !((a - b < 1.0) && (b - a < 1.0)) }'; then
    log "set connect volume to ${pct}% (${db}dB)"
  else
    log "connect volume ${pct}% (${db}dB) did not stick (mixer reads '${got:-nothing}')"
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

#!/bin/bash
# airplay-tv-power.sh — HDMI-CEC power control driven by shairport-sync
# session-state hooks (see /etc/shairport-sync.conf, sessioncontrol block):
#   run_this_before_entering_active_state -> "airplay-tv-power.sh on"
#   run_this_after_exiting_active_state   -> "airplay-tv-power.sh off"
#
# "entering active state" fires when an AirPlay device connects/starts a
# session. "exiting active state" fires sessioncontrol.active_state_timeout
# seconds (set to 300 = 5 min in the conf) after playback stops, so the
# 5-minute idle timeout before power-off is handled natively by
# shairport-sync -- no timer/debounce logic needed here.
set -u

CEC_DEV=/dev/cec0   # HDMI0 -- the only connected port on this Pi
LOG_TAG=airplay-tv-power

log() { logger -t "$LOG_TAG" "$*"; }

cec() { cec-ctl -d "$CEC_DEV" "$@" >/dev/null 2>&1; }

case "${1:-}" in
  on)
    log "AirPlay session active -> waking TV"
    cec --playback                    # claim a logical address (idempotent)
    my_phys_addr=$(cec-ctl -d "$CEC_DEV" -x 2>/dev/null | tail -n1)
    cec --to 0 --image-view-on
    cec --to 0 --active-source phys-addr="$my_phys_addr"
    ;;
  off)
    log "AirPlay session ended (5 min idle) -> standby TV"
    cec --playback
    cec --to 0 --standby
    ;;
  *)
    echo "usage: $0 {on|off}" >&2
    exit 1
    ;;
esac

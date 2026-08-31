#!/usr/bin/env bash
#
# setup.sh -- one-shot fresh install for a new AirplayMatrix device.
#
#   git clone https://github.com/bradport02/AirplayMatrix.git
#   cd AirplayMatrix
#   ./setup.sh
#
# Installs and starts everything: AirPlay receive (shairport-sync),
# HDMI-CEC TV power/remote passthrough, the settings web UI, and either the
# Qt kiosk display app + its autostart routine (on a desktop image) or the
# headless matrix_daemon.py (on a Lite image / anything without a desktop
# session) -- see docs/install-full-pi.md and docs/install-pi-zero-wh.md for
# what this automates and why each step exists; this script is those docs
# turned into commands, not a separate design.
#
# SAFE TO RE-RUN. Every step here is idempotent: services already installed
# are stopped and reconfigured rather than duplicated, an existing kiosk-app
# venv is rebuilt from scratch rather than trusted, and switching this
# device between the kiosk display and the headless daemon tears down
# whichever one you're switching *away* from (see the "teardown" section
# below) -- that's the "remove any preexisting installs" half of the brief.
# Nothing here touches Wi-Fi credentials, the web UI's admin password, or the
# lyrics/song-details toggles -- those live outside anything this script
# deletes, so they survive a re-run untouched.
#
# What this does NOT do: flash the OS, set the hostname/Wi-Fi/SSH (Raspberry
# Pi Imager's advanced options already did that before you ever got here),
# or pick an ALSA output device for you (`aplay -l` still needs a human to
# read it) -- see the printed reminder near the end.

set -euo pipefail

# ---------------------------------------------------------------------------
# constants / logging
# ---------------------------------------------------------------------------

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_DIR="$REPO_DIR/Software"
TARGET_USER="airplaymatrix"
TARGET_UID="1000"

# Every shipped .service file, airplaymatrix-privileged.py's restart-display
# command, and this script all hardcode this one path -- see
# install-full-pi.md's intro. Changing TARGET_USER above without also
# hand-editing those files will not do what you want.
TARGET_HOME="/home/${TARGET_USER}"

c_info=$'\033[1;36m'; c_warn=$'\033[1;33m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$c_info" "$c_off" "$*"; }
warn() { printf '%s!!%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
step() { printf '\n%s== %s ==%s\n' "$c_info" "$*" "$c_off"; }

# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------

DISPLAY_MODE=auto   # auto | kiosk | headless
QT_MAJOR=auto        # auto | 5 | 6  (only consulted when DISPLAY_MODE=kiosk)
DO_LAUNCH=1
DO_POLISH=1          # only consulted when DISPLAY_MODE=kiosk && QT_MAJOR=5
AIRPLAY_MODE=2       # 2 | classic -- see "AirPlay 2" section below
REBUILD_AIRPLAY2=0

usage() {
  cat <<'EOF'
Usage: ./setup.sh [options]

  --kiosk          Force the Qt kiosk display app (auto-detected by default:
                    on if this image has a desktop/labwc session, off on a
                    Lite image).
  --headless       Force the headless matrix_daemon.py instead of the kiosk
                    app, regardless of what's detected.
  --qt5            Force the Qt5/PySide2 build of the kiosk app (app_qt5/).
  --qt6            Force the Qt6/PySide6 build of the kiosk app (app/).
                    Auto-detected from `uname -m` by default: aarch64 -> Qt6
                    (has official PySide6 wheels), anything else -> Qt5.
  --no-polish      Skip the Zero WH boot-time/idle-screen polish step (Qt5
                    kiosk builds only -- see docs/install-pi-zero-wh.md's
                    "Boot time and idle-screen polish" section for exactly
                    what this disables: cloud-init, Bluetooth, disk
                    auto-mount, RPi Connect screen sharing, the taskbar/
                    desktop-icon autostart, the boot splash, and the mouse
                    cursor). Pass this if you want any of those left alone,
                    e.g. you're actively using RPi Connect screen sharing on
                    this device.
  --classic-airplay  Skip building AirPlay 2 support (nqptp + shairport-sync
                    from source, on by default -- see "AirPlay 2" below);
                    apt's classic-AirPlay-1-only shairport-sync is what's
                    live instead. Faster install, no source build. Pass this
                    if you don't care about another app's audio being able
                    to interrupt AirPlay, or want the simpler/better-tested
                    path.
  --rebuild-airplay2  Force a fresh AirPlay 2 build even if
                    /usr/local/bin/shairport-sync already reports AirPlay2
                    support (default: skip the ~20min rebuild when it does).
                    Use this to pick up upstream shairport-sync/nqptp fixes.
  --no-launch      Do everything except launch the app / reboot hint at the
                    end -- useful if you're scripting this further.
  -h, --help       This.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kiosk) DISPLAY_MODE=kiosk ;;
    --headless) DISPLAY_MODE=headless ;;
    --qt5) QT_MAJOR=5 ;;
    --qt6) QT_MAJOR=6 ;;
    --no-polish) DO_POLISH=0 ;;
    --classic-airplay) AIRPLAY_MODE=classic ;;
    --rebuild-airplay2) REBUILD_AIRPLAY2=1 ;;
    --no-launch) DO_LAUNCH=0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

step "Preflight"

[[ "$(id -un)" == "$TARGET_USER" ]] || die \
  "run this as the '$TARGET_USER' user (every shipped systemd unit and the" \
  $'\n  '"web UI's privileged-action script hardcode that username and uid" \
  $'\n  '"1000 -- see install-full-pi.md's intro). Currently running as '$(id -un)'."

command -v apt-get >/dev/null 2>&1 || die "this targets Debian/Raspberry Pi OS (no apt-get found)"

[[ -f "$SOFTWARE_DIR/shairport-sync.conf.example" ]] || die \
  "Software/ not found next to this script -- run it from inside a clone of the repo, not a copy of just this file."

# A real incident, not a hypothetical: this repo ended up cloned into two
# different places on the same Zero WH -- one from `git clone` per this
# file's own header, one from an ad hoc GitHub zip download elsewhere.
# Re-running this script from a *different* clone than last time silently
# repoints the kiosk app's autostart at the new one, while the old clone
# sits there orphaned and never updated again -- no error, no obvious sign
# anything's wrong, just a kiosk app that quietly stops receiving fixes.
# That's exactly how a blank-screen-after-reboot bug traced back days later
# to stale QML in a clone nobody remembered existed. If an autostart script
# already points somewhere else, say so now rather than silently orphaning it.
EXISTING_RUN_SCRIPT="$TARGET_HOME/.local/bin/airplaymatrix-run.sh"
if [[ -f "$EXISTING_RUN_SCRIPT" ]]; then
  EXISTING_CD="$(sed -n 's/^cd \(.*\)$/\1/p' "$EXISTING_RUN_SCRIPT" | head -1)"
  if [[ -n "$EXISTING_CD" && "$EXISTING_CD" != "$SOFTWARE_DIR" ]]; then
    warn "Existing autostart script points at a DIFFERENT clone:"
    warn "  ${EXISTING_CD%/Software}"
    warn "This run will repoint it at $REPO_DIR instead. Once you've"
    warn "confirmed that works, delete the old one so it can't go stale"
    warn "and get run by accident: rm -rf \"${EXISTING_CD%/Software}\""
  fi
fi

log "Checking sudo access (you'll be prompted for your password once)..."
sudo -v || die "sudo access is required"
# Installs below take a few minutes; keep the ticket alive for the whole
# run rather than prompting again mid-way, and stop nagging once this
# script's own process tree is gone.
( while true; do sudo -n true; sleep 60; kill -0 "$$" 2>/dev/null || exit; done ) &
SUDO_KEEPALIVE_PID=$!
trap '{ kill "$SUDO_KEEPALIVE_PID"; } 2>/dev/null || true' EXIT

# ---- resolve DISPLAY_MODE ----
if [[ "$DISPLAY_MODE" == auto ]]; then
  if command -v labwc >/dev/null 2>&1; then
    DISPLAY_MODE=kiosk
  else
    DISPLAY_MODE=headless
    log "No desktop session (labwc) found -- defaulting to the headless matrix daemon. Pass --kiosk to override."
  fi
fi

# ---- resolve QT_MAJOR (kiosk only) ----
if [[ "$DISPLAY_MODE" == kiosk && "$QT_MAJOR" == auto ]]; then
  case "$(uname -m)" in
    aarch64) QT_MAJOR=6 ;;
    *) QT_MAJOR=5 ;;
  esac
fi

if [[ "$DISPLAY_MODE" == kiosk ]]; then
  log "Plan: kiosk display app, Qt${QT_MAJOR} build ($(uname -m))"
else
  log "Plan: headless matrix daemon (no display)"
fi
if [[ "$AIRPLAY_MODE" == classic ]]; then
  log "Plan: classic AirPlay 1 only (--classic-airplay)"
else
  log "Plan: AirPlay 2 (nqptp + shairport-sync built from source -- pass --classic-airplay to skip)"
fi

# Recorded before anything below touches it: the web UI's admin password is
# only ever shown in plaintext once, at the moment its config is first
# created (app.py stores just a hash after that) -- so whether one's coming
# below depends on whether this file exists *right now*, not on anything we
# can infer later from what got installed.
WEBUI_CONFIG="$TARGET_HOME/.config/airplaymatrix-webui/config.json"
[[ -f "$WEBUI_CONFIG" ]] && WEBUI_FRESH_INSTALL=0 || WEBUI_FRESH_INSTALL=1

# ---------------------------------------------------------------------------
# teardown -- stop/disable whatever's already here before reinstalling.
# Safe on a genuinely fresh device too: every "systemctl stop/disable" below
# is a no-op (ignored via `|| true`) if the unit was never installed.
# ---------------------------------------------------------------------------

step "Checking for and removing any previous install"

ALL_SERVICES=(shairport-sync airplay-cec-remote airplaymatrix-webui airplaymatrix-matrix)
for unit in "${ALL_SERVICES[@]}"; do
  if systemctl list-unit-files "${unit}.service" &>/dev/null; then
    log "Found existing ${unit}.service -- stopping it for a clean reinstall"
    sudo systemctl stop "$unit" 2>/dev/null || true
    sudo systemctl disable "$unit" 2>/dev/null || true
  fi
done

if pgrep -f "python3 -m app" >/dev/null 2>&1; then
  log "Kiosk app is currently running -- stopping it"
  pkill -TERM -f "python3 -m app" || true
  sleep 1
fi

if [[ -d "$SOFTWARE_DIR/webui/.venv" ]]; then
  log "Removing existing web UI venv (rebuilt fresh below, not trusted as-is)"
  rm -rf "$SOFTWARE_DIR/webui/.venv"
fi
if [[ -d "$SOFTWARE_DIR/.venv" ]]; then
  # Not created by this script (the Qt6 branch below uses apt packages, the
  # route actually running on the reference unit) -- only cleaned up here in
  # case this device was previously set up by hand via install-full-pi.md's
  # alternate pip/venv route, so it doesn't linger as a second, unused
  # PySide6 install.
  log "Removing existing kiosk-app venv (superseded by the apt-package route below)"
  rm -rf "$SOFTWARE_DIR/.venv"
fi

# Whichever display mode this run is *not* setting up, tear its artifacts
# down -- this is what makes switching a device between kiosk and headless
# a clean swap instead of leaving the old one half-installed and fighting
# the new one over the ESP32's one USB serial port.
if [[ "$DISPLAY_MODE" == kiosk ]]; then
  if systemctl list-unit-files airplaymatrix-matrix.service &>/dev/null; then
    log "Removing the headless matrix daemon's unit file (superseded by the kiosk app)"
    sudo rm -f /etc/systemd/system/airplaymatrix-matrix.service
  fi
else
  AUTOSTART_FILE="$TARGET_HOME/.config/labwc/autostart"
  if [[ -f "$AUTOSTART_FILE" ]] && grep -q "airplaymatrix-run.sh" "$AUTOSTART_FILE"; then
    log "Removing the kiosk app's autostart line (superseded by the headless daemon)"
    sed -i '/airplaymatrix-run\.sh/d' "$AUTOSTART_FILE"
  fi
  rm -f "$TARGET_HOME/.local/bin/airplaymatrix-run.sh"
fi

# AirPlay 2 (nqptp + shairport-sync, built from source)
#
# Neither Debian nor Raspberry Pi OS ship an AirPlay-2-capable shairport-sync
# or the nqptp companion clock-sync daemon it needs -- confirmed via
# `apt-cache show shairport-sync` pulling in none of AirPlay 2's crypto libs
# (libsodium, libplist) or nqptp itself, and the apt package's own `-V`
# output never showing "AirPlay2". Without it, iOS treats this receiver's
# stream the same as any other app's incidental audio: opening another app
# (e.g. Instagram) while AirPlaying can interrupt/replace it. Real AirPlay 2
# destinations (a HomePod, an AirPlay-2-certified TV) don't have that
# problem -- iOS treats them as an app-owned route instead, and other apps'
# sound just plays through the phone's own speaker.
#
# Installs to /usr/local/bin, NOT /usr/bin -- deliberately never touches or
# replaces the apt package installed above, so it stays the instant,
# always-available fallback: `sudo systemctl revert shairport-sync && sudo
# systemctl restart shairport-sync` completely undoes the override below.
AIRPLAY2_BUILD_DEPS=(
  build-essential git autoconf automake libtool
  libpopt-dev libconfig-dev libasound2-dev libavahi-client-dev
  libssl-dev libsoxr-dev libplist-dev libsodium-dev uuid-dev libgcrypt-dev
  xxd libplist-utils libavutil-dev libavcodec-dev libavformat-dev
  libmosquitto-dev libdbus-1-dev libglib2.0-dev
  systemd-dev  # Debian 13/trixie split pkg-config's systemd.pc out of systemd itself
)

install_airplay2() {
  if [[ "$REBUILD_AIRPLAY2" -eq 0 ]] && [[ -x /usr/local/bin/shairport-sync ]] \
     && /usr/local/bin/shairport-sync --version 2>&1 | grep -q AirPlay2; then
    log "AirPlay 2 build already present (/usr/local/bin/shairport-sync) -- skipping the ~20min rebuild."
    log "Pass --rebuild-airplay2 to force a fresh one (e.g. to pick up upstream fixes)."
    return
  fi

  step "1b: AirPlay 2 (nqptp + shairport-sync, built from source -- slow, be patient)"

  # Building on a memory-tight, single-core device (Pi Zero WH class) risks
  # the compiler getting OOM-killed mid-build, or the kiosk app it's
  # competing with for RAM getting killed instead -- neither is a real risk
  # on a Pi 4/5, so only pay this cost where it's actually needed.
  local total_mem_kb added_build_swap=0
  total_mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  if [[ "$total_mem_kb" -lt 1048576 ]]; then  # < 1GB
    log "Low-RAM device detected ($((total_mem_kb / 1024))MB) -- adding a temporary 1G build swapfile"
    if [[ ! -f /swapfile-build ]]; then
      sudo fallocate -l 1G /swapfile-build
      sudo chmod 600 /swapfile-build
      sudo mkswap /swapfile-build >/dev/null
      sudo swapon -p 10 /swapfile-build
    fi
    added_build_swap=1
  fi

  local kiosk_was_running=0
  if pgrep -f 'app_qt5\.main' >/dev/null 2>&1 || pgrep -f '(^|/)app\.main' >/dev/null 2>&1; then
    log "Stopping the kiosk app for the duration of the build (frees RAM; restarted after)"
    pkill -f 'app_qt5\.main' 2>/dev/null || true
    pkill -f '(^|/)app\.main' 2>/dev/null || true
    kiosk_was_running=1
  fi

  sudo apt-get install -y --no-install-recommends "${AIRPLAY2_BUILD_DEPS[@]}"

  local build_root
  build_root="$(mktemp -d)"

  log "Building nqptp..."
  git clone --quiet https://github.com/mikebrady/nqptp.git "$build_root/nqptp"
  ( cd "$build_root/nqptp" && autoreconf -fi && ./configure --with-systemd-startup && make )
  sudo make -C "$build_root/nqptp" install
  sudo systemctl enable --now nqptp

  log "Building shairport-sync (this is the slow part)..."
  git clone --quiet https://github.com/mikebrady/shairport-sync.git "$build_root/shairport-sync"
  (
    cd "$build_root/shairport-sync"
    autoreconf -fi
    # Same feature set as the apt package (dbus/mpris for the CEC remote
    # control, metadata for the desk-display apps, mqtt/soxr/convolution
    # for parity) plus --with-airplay-2. Flag names here are current as of
    # shairport-sync 5.2.3 -- the 4.x names (--with-dbus, --with-mpris,
    # --with-mqtt, --with-pa) were silently accepted-then-ignored rather
    # than erroring, which is how this was first discovered wrong.
    ./configure --sysconfdir=/etc \
      --with-alsa --with-soxr --with-avahi --with-ssl=openssl \
      --with-systemd-startup --with-airplay-2 \
      --with-metadata --with-dbus-interface --with-mpris-interface --with-mqtt-client \
      --with-stdout --with-pipe --with-dummy --with-convolution
    make
  )
  sudo make -C "$build_root/shairport-sync" install

  rm -rf "$build_root"

  if [[ "$added_build_swap" -eq 1 ]]; then
    sudo swapoff /swapfile-build || true
    sudo rm -f /swapfile-build
  fi

  log "Wiring in via a systemd override -- apt's /usr/bin/shairport-sync stays untouched as an"
  log "instant fallback: sudo systemctl revert shairport-sync && sudo systemctl restart shairport-sync"
  sudo mkdir -p /etc/systemd/system/shairport-sync.service.d
  sudo tee /etc/systemd/system/shairport-sync.service.d/override.conf >/dev/null <<'AIRPLAY2_OVERRIDE_EOF'
[Service]
ExecStart=
ExecStart=/usr/local/bin/shairport-sync $DAEMON_ARGS
AIRPLAY2_OVERRIDE_EOF
  sudo systemctl daemon-reload
  sudo systemctl restart shairport-sync

  if [[ "$kiosk_was_running" -eq 1 && "$DISPLAY_MODE" == kiosk && -S "/run/user/${TARGET_UID}/wayland-0" ]]; then
    log "Restarting the kiosk app"
    env DISPLAY=:0 XAUTHORITY="$TARGET_HOME/.Xauthority" \
      XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" WAYLAND_DISPLAY=wayland-0 \
      "$TARGET_HOME/.local/bin/airplaymatrix-run.sh" \
      >/tmp/airplaymatrix-run.log 2>&1 &
    disown
  elif [[ "$kiosk_was_running" -eq 1 ]]; then
    warn "Couldn't confirm an active desktop session to restart the kiosk app into --"
    warn "log back into the desktop session or reboot to bring it back."
  fi

  log "shairport-sync is now: $(/usr/local/bin/shairport-sync --version 2>&1)"
}

# ---------------------------------------------------------------------------
# 1. AirPlay receiver
# ---------------------------------------------------------------------------

step "1: AirPlay receiver (shairport-sync)"

sudo apt-get update -qq
sudo apt-get install -y shairport-sync avahi-daemon

if [[ -f /etc/shairport-sync.conf ]]; then
  backup="/etc/shairport-sync.conf.bak-$(date +%Y%m%d%H%M%S)"
  log "Existing /etc/shairport-sync.conf found -- backing it up to $backup before replacing it"
  sudo cp /etc/shairport-sync.conf "$backup"
fi
sudo cp "$SOFTWARE_DIR/shairport-sync.conf.example" /etc/shairport-sync.conf
sudo systemctl enable --now shairport-sync

log "Reminder: confirm the ALSA output device with 'aplay -l' -- the example"
log "conf ships with output_device = \"default\" (HDMI audio); edit"
log "/etc/shairport-sync.conf and restart shairport-sync if you're using a"
log "USB DAC or I2S HAT instead."

if [[ "$AIRPLAY_MODE" == classic ]]; then
  log "Skipping AirPlay 2 (--classic-airplay): apt's shairport-sync (classic AirPlay 1 only) stays live."
else
  install_airplay2
fi

# ---------------------------------------------------------------------------
# 2. HDMI-CEC
# ---------------------------------------------------------------------------

step "2: HDMI-CEC (TV power + remote passthrough)"

sudo apt-get install -y v4l-utils
sudo setcap cap_net_admin+ep "$(command -v cec-ctl)"
sudo usermod -aG video shairport-sync

sudo install -m 0755 "$SOFTWARE_DIR/cec/airplay-tv-power.sh" /usr/local/bin/airplay-tv-power.sh
sudo install -m 0755 "$SOFTWARE_DIR/cec/airplay-cec-remote.py" /usr/local/bin/airplay-cec-remote.py
sudo cp "$SOFTWARE_DIR/cec/airplay-cec-remote.service" /etc/systemd/system/
if [[ "$DISPLAY_MODE" == kiosk ]]; then
  sudo install -m 0755 "$SOFTWARE_DIR/cec/airplaymatrix-quit.sh" /usr/local/bin/airplaymatrix-quit.sh
fi

sudo systemctl daemon-reload
sudo systemctl enable --now airplay-cec-remote.service

# ---------------------------------------------------------------------------
# 3. Settings web UI
# ---------------------------------------------------------------------------

step "3: Settings web UI"

sudo apt-get install -y python3-venv
python3 -m venv "$SOFTWARE_DIR/webui/.venv"
"$SOFTWARE_DIR/webui/.venv/bin/pip" install --quiet --upgrade pip
"$SOFTWARE_DIR/webui/.venv/bin/pip" install --quiet -r "$SOFTWARE_DIR/webui/requirements.txt"

sudo install -o root -g root -m 0700 \
  "$SOFTWARE_DIR/webui/airplaymatrix-privileged.py" /usr/local/bin/airplaymatrix-privileged.py
sudo visudo -cf "$SOFTWARE_DIR/webui/airplaymatrix-webui.sudoers" \
  || die "airplaymatrix-webui.sudoers failed visudo syntax check -- refusing to install it"
sudo install -o root -g root -m 0440 \
  "$SOFTWARE_DIR/webui/airplaymatrix-webui.sudoers" /etc/sudoers.d/airplaymatrix-webui

sudo cp "$SOFTWARE_DIR/webui/airplaymatrix-webui.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airplaymatrix-webui.service

WEBUI_PASSWORD_LINE=""
if [[ "$WEBUI_FRESH_INSTALL" -eq 1 ]]; then
  # app.py prints this exactly once, right as the service starts, then only
  # ever stores a hash -- this is the one and only chance to capture it.
  # Poll rather than a single grep: `enable --now` returns as soon as
  # systemd has *forked* the process, not once Flask has actually logged
  # anything, so reading the journal immediately after is a real race on a
  # loaded system.
  log "Waiting for the web UI to generate its admin password..."
  for _ in $(seq 1 10); do
    WEBUI_PASSWORD_LINE="$(sudo journalctl -u airplaymatrix-webui -n 50 --no-pager 2>/dev/null | grep "generated initial" || true)"
    [[ -n "$WEBUI_PASSWORD_LINE" ]] && break
    sleep 1
  done
  if [[ -n "$WEBUI_PASSWORD_LINE" ]]; then
    log "$WEBUI_PASSWORD_LINE"
    log "(also repeated in the summary at the end -- change it from the Account page after logging in)"
  else
    warn "Could not read the generated password from the journal after 10s."
    warn "Retrieve it with: sudo journalctl -u airplaymatrix-webui | grep 'generated initial'"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Display: kiosk app or headless daemon
# ---------------------------------------------------------------------------

step "4: Display (${DISPLAY_MODE})"

if [[ "$DISPLAY_MODE" == headless ]]; then
  sudo apt-get install -y python3-serial python3-pil
  sudo usermod -aG dialout shairport-sync   # serial access to the ESP32

  sudo cp "$SOFTWARE_DIR/matrix/airplaymatrix-matrix.service" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now airplaymatrix-matrix.service

elif [[ "$QT_MAJOR" == 6 ]]; then
  # apt-package route: what the actual Pi 5 reference unit runs (system
  # python3, no venv) -- install-full-pi.md documents a pip/venv route too
  # (PySide6 does ship real aarch64 wheels), but this is the one proven on
  # real hardware, so it's what this script uses rather than an untested
  # second path.
  sudo apt-get install -y \
    python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtqml \
    python3-pyside6.qtquick python3-pyside6.qtquickcontrols2 \
    python3-serial python3-pil
  KIOSK_PYTHON="python3"
  KIOSK_MODULE="app.main"
  KIOSK_QT_ENV='export QT_QPA_PLATFORM=wayland'

else
  # No PySide2 wheel for this architecture -- apt packages + the system
  # interpreter is the only route (see docs/install-pi-zero-wh.md).
  sudo apt-get install -y \
    python3-pyside2.qtcore python3-pyside2.qtgui python3-pyside2.qtqml python3-pyside2.qtquick \
    qml-module-qtquick2 qml-module-qtquick-layouts qml-module-qtquick-controls2 qml-module-qtquick-window2 \
    qml-module-qtgraphicaleffects \
    python3-serial python3-pil
  KIOSK_PYTHON="python3"
  KIOSK_MODULE="app_qt5.main"
  KIOSK_QT_ENV='# no QT_QPA_PLATFORM override -- Qt5 build runs under the default XCB backend'
fi

# ---------------------------------------------------------------------------
# 5. Autostart (kiosk only)
# ---------------------------------------------------------------------------

if [[ "$DISPLAY_MODE" == kiosk ]]; then
  step "5: Kiosk app autostart"

  mkdir -p "$TARGET_HOME/.local/bin"
  cat > "$TARGET_HOME/.local/bin/airplaymatrix-run.sh" <<EOF
#!/bin/bash
set -e
cd $SOFTWARE_DIR
$KIOSK_QT_ENV
exec $KIOSK_PYTHON -m $KIOSK_MODULE
EOF
  chmod 0755 "$TARGET_HOME/.local/bin/airplaymatrix-run.sh"

  AUTOSTART_FILE="$TARGET_HOME/.config/labwc/autostart"
  AUTOSTART_LINE="$TARGET_HOME/.local/bin/airplaymatrix-run.sh &"
  mkdir -p "$(dirname "$AUTOSTART_FILE")"
  touch "$AUTOSTART_FILE"
  if grep -qxF "$AUTOSTART_LINE" "$AUTOSTART_FILE" 2>/dev/null; then
    log "Autostart line already present in $AUTOSTART_FILE"
  else
    {
      echo ''
      echo '# AirPlay Desk Display -- boots straight into the app. Not lwrespawn'"'"'d on'
      echo '# purpose: Shift+X (see rc.xml) closes it and that'"'"'s meant to stick so the'
      echo '# desktop underneath is usable, not bounce straight back.'
      echo "$AUTOSTART_LINE"
    } >> "$AUTOSTART_FILE"
    log "Added autostart line to $AUTOSTART_FILE"
  fi

  # rc.xml's Shift+X quit keybind. Idempotent via the marker comment below;
  # inserted via a small Python snippet rather than sed, since rc.xml is
  # real XML and "insert this text right before the first </keyboard>" is
  # easy to get subtly wrong with a line-oriented tool. python3 is always
  # present on Raspberry Pi OS, so this doesn't add a dependency.
  RC_FILE="$TARGET_HOME/.config/labwc/rc.xml"
  mkdir -p "$(dirname "$RC_FILE")"
  if [[ ! -f "$RC_FILE" ]]; then
    # No per-user override yet -- start from labwc's own shipped default so
    # the <keyboard> section we're inserting into actually exists.
    if [[ -f /etc/xdg/labwc/rc.xml ]]; then
      cp /etc/xdg/labwc/rc.xml "$RC_FILE"
    else
      die "no ~/.config/labwc/rc.xml and no /etc/xdg/labwc/rc.xml to seed it from -- add the Shift+X keybind manually, see Software/labwc/rc.xml.snippet.txt"
    fi
  fi

  MARKER="AirplayMatrix Desk Display quit keybind"
  python3 - "$RC_FILE" "$MARKER" <<'PYEOF'
import re
import sys

path, marker = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()

if marker in text:
    print(f"Shift+X keybind already present in {path}")
    sys.exit(0)

keybind = f"""    <!-- {marker} -->
    <keybind key="S-x">
      <action name="Execute">
        <command>/usr/local/bin/airplaymatrix-quit.sh</command>
      </action>
    </keybind>
  </keyboard>"""

new_text, n = re.subn(r"[ \t]*</keyboard>", keybind, text, count=1)
if n == 0:
    print(f"WARNING: no </keyboard> found in {path} -- add the keybind manually, "
          f"see Software/labwc/rc.xml.snippet.txt", file=sys.stderr)
    sys.exit(1)

open(path, "w", encoding="utf-8").write(new_text)
print(f"Added Shift+X quit keybind to {path}")
PYEOF
fi

# ---------------------------------------------------------------------------
# 6. Zero WH boot-time / idle-screen polish (kiosk, Qt5 only)
# ---------------------------------------------------------------------------
#
# Everything below was worked out and verified live on real Zero WH
# hardware, not theorised -- see docs/install-pi-zero-wh.md's "Boot time and
# idle-screen polish" section for the measurements and reasoning behind each
# piece. Scoped to the Qt5 kiosk path specifically: none of this has been
# tried on the Pi 5/Qt6 build, which doesn't share the Zero's single-core/
# 512MB constraints that motivate it. Idempotent like everything else here --
# safe to re-run, and every edit either checks first or is naturally a no-op
# the second time (a sed pattern that only matches an as-yet-uncommented
# line, a "not already present" grep before appending, etc.).

if [[ "$DISPLAY_MODE" == kiosk && "$QT_MAJOR" == 5 && "$DO_POLISH" -eq 1 ]]; then
  step "6: Zero WH boot-time / idle-screen polish"

  # -- cloud-init: first-boot customisation (Raspberry Pi Imager's
  # hostname/user/Wi-Fi/SSH seed) has already run by the time this script
  # is -- disabling it here is the documented, official mechanism
  # (ds-identify's is_disabled() checks this file before anything else),
  # and it alone was ~40s of every single boot on a Zero WH doing nothing.
  log "Disabling cloud-init (first-boot customisation already applied; ~40s/boot saved)"
  sudo touch /etc/cloud/cloud-init.disabled

  # -- netplan: ships some packaged config world-readable (0644) when
  # netplan's own security check wants 0600 on anything it manages. Harmless
  # content-wise here, but the permission mismatch was observed driving a
  # ~55s NetworkManager reload storm at boot (four separate `systemctl
  # reload NetworkManager.service` cycles, each ~9-15s on this CPU).
  log "Tightening netplan config file permissions to 0600"
  for f in /lib/netplan/*.yaml /etc/netplan/*.yaml; do
    if [[ -f "$f" ]]; then
      sudo chmod 600 "$f"
    fi
  done

  # -- background services this kiosk never needs: Bluetooth (no BT
  # hardware in use), disk auto-mount (udisks2 -- no removable media),
  # RPi Connect's screen sharing (wayvnc, not its remote-shell half) and the
  # generic system wayvnc/wayvnc-control pair, and two Pi 5-only hardware
  # probes that no-op on a Zero WH's BCM2835 but still cost boot time here.
  log "Disabling unused background services (Bluetooth, disk auto-mount, screen sharing, Pi 5-only probes)"
  ZERO_WH_UNNEEDED_SERVICES=(bluetooth udisks2 rp1-test glamor-test wayvnc wayvnc-control)
  for unit in "${ZERO_WH_UNNEEDED_SERVICES[@]}"; do
    if systemctl list-unit-files "${unit}.service" &>/dev/null; then
      sudo systemctl disable --now "${unit}.service" 2>/dev/null || true
    fi
  done
  # rpi-connect-wayvnc is a --user unit and rpi-connect itself is a per-user
  # CLI, not root-level -- both need an active user session/bus to reach,
  # which may not exist yet on a fresh headless SSH run before the first
  # desktop login. Best-effort: applies immediately if a session is up,
  # otherwise simply doesn't regress anything (RPi Connect's remote *shell*
  # is untouched either way -- only screen sharing is being turned off).
  systemctl --user disable --now rpi-connect-wayvnc.service &>/dev/null || true
  command -v rpi-connect >/dev/null 2>&1 && rpi-connect vnc off &>/dev/null || true

  # -- taskbar + desktop icons: invisible anyway underneath a full-screen
  # kiosk window, pure RAM cost. The sed only matches the as-yet-uncommented
  # line, so this is naturally idempotent on a re-run.
  log "Disabling the desktop taskbar/icon autostart (invisible behind the full-screen kiosk anyway)"
  LABWC_AUTOSTART_SYS=/etc/xdg/labwc/autostart
  if [[ -f "$LABWC_AUTOSTART_SYS" ]]; then
    sudo sed -i \
      -e "s|^/usr/bin/lwrespawn /usr/bin/pcmanfm-pi \&|# (disabled by AirplayMatrix setup.sh -- RAM headroom on the Zero WH) /usr/bin/lwrespawn /usr/bin/pcmanfm-pi \&|" \
      -e "s|^/usr/bin/lwrespawn /usr/bin/wf-panel-pi \&|# (disabled by AirplayMatrix setup.sh -- RAM headroom on the Zero WH) /usr/bin/lwrespawn /usr/bin/wf-panel-pi \&|" \
      "$LABWC_AUTOSTART_SYS"
  fi

  # -- boot splash: replace the Raspberry Pi firmware rainbow splash and
  # Plymouth's graphical theme with a blank console + a plain static
  # message (Software/boot/airplaymatrix-boot-message.sh), rather than a
  # custom Plymouth theme -- that would need an initramfs rebuild, real risk
  # on a device with no physical console to recover it with if that goes
  # wrong. This is cmdline.txt/config.txt only, always reversible by editing
  # those two files back (backups are saved alongside them below).
  log "Installing the boot-time placeholder message (tty1) and disabling the graphical splash"
  sudo install -m 0755 "$SOFTWARE_DIR/boot/airplaymatrix-boot-message.sh" /usr/local/bin/airplaymatrix-boot-message.sh
  sudo install -m 0644 "$SOFTWARE_DIR/boot/airplaymatrix-boot-message.service" /etc/systemd/system/airplaymatrix-boot-message.service
  sudo systemctl daemon-reload
  sudo systemctl enable airplaymatrix-boot-message.service

  BOOT_DIR=/boot/firmware
  [[ -d "$BOOT_DIR" ]] || BOOT_DIR=/boot

  CONFIG_FILE="$BOOT_DIR/config.txt"
  if [[ -f "$CONFIG_FILE" ]] && ! grep -qx "disable_splash=1" "$CONFIG_FILE"; then
    sudo cp "$CONFIG_FILE" "$CONFIG_FILE.bak-$(date +%Y%m%d%H%M%S)"
    if grep -q "^\[all\]" "$CONFIG_FILE"; then
      sudo sed -i "/^\[all\]/a disable_splash=1" "$CONFIG_FILE"
    else
      printf '\n[all]\ndisable_splash=1\n' | sudo tee -a "$CONFIG_FILE" >/dev/null
    fi
  fi

  CMDLINE_FILE="$BOOT_DIR/cmdline.txt"
  if [[ -f "$CMDLINE_FILE" ]]; then
    # `read` returns non-zero at EOF with no trailing newline -- true for
    # cmdline.txt on every image observed -- despite populating the array
    # correctly; the `|| true` only swallows that specific harmless case.
    read -r -a _cmdline_tokens < "$CMDLINE_FILE" || true
    _cmdline_new=()
    for t in "${_cmdline_tokens[@]}"; do
      # splash and plymouth.ignore-serial-consoles are what we're deliberately
      # replacing (see the .sh's comment for why); everything else the image
      # shipped with (console=, root=, rootfstype=, fsck.repair=, cfg80211
      # regdom, ...) is left exactly as-is, in whatever order it was in.
      case "$t" in
        splash|plymouth.ignore-serial-consoles) continue ;;
        *) _cmdline_new+=("$t") ;;
      esac
    done
    for want in quiet loglevel=1 systemd.show_status=false plymouth.enable=0 logo.nologo vt.global_cursor_default=0; do
      _present=0
      for t in "${_cmdline_new[@]}"; do
        if [[ "$t" == "$want" ]]; then
          _present=1
          break
        fi
      done
      if [[ "$_present" -eq 0 ]]; then
        _cmdline_new+=("$want")
      fi
    done
    _cmdline_joined="${_cmdline_new[*]}"
    if [[ "$_cmdline_joined" != "$(cat "$CMDLINE_FILE")" ]]; then
      sudo cp "$CMDLINE_FILE" "$CMDLINE_FILE.bak-$(date +%Y%m%d%H%M%S)"
      printf '%s' "$_cmdline_joined" | sudo tee "$CMDLINE_FILE" >/dev/null
    fi
  fi

  # -- mouse cursor: labwc's own default arrow, visible during the gap
  # between the compositor starting and the Qt app's own
  # setOverrideCursor(BlankCursor) call (see app_qt5/main.py) taking effect.
  # A hand-built, fully-transparent Xcursor theme closes that gap too --
  # see the generator script's docstring for why not xcursorgen (not
  # packaged for this device's 32-bit armhf archive).
  log "Installing a blank cursor theme (hides the pointer during the app-loading gap)"
  python3 "$SOFTWARE_DIR/boot/make_blank_cursor_theme.py"
  ENV_FILE="$TARGET_HOME/.config/labwc/environment"
  mkdir -p "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  if ! grep -qxF "XCURSOR_THEME=Blank" "$ENV_FILE"; then
    echo "XCURSOR_THEME=Blank" >> "$ENV_FILE"
  fi
  if ! grep -qxF "XCURSOR_SIZE=24" "$ENV_FILE"; then
    echo "XCURSOR_SIZE=24" >> "$ENV_FILE"
  fi

  # -- persistent journal logging: volatile (/run) by default, which meant
  # every reboot -- including the ones used to *recover* from a problem --
  # destroyed the very evidence needed to diagnose it. Cheap to keep.
  log "Enabling persistent journal logging (survives a reboot -- needed to debug anything that recurs)"
  sudo mkdir -p /var/log/journal
  sudo systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
  if ! grep -q "^Storage=persistent" /etc/systemd/journald.conf 2>/dev/null; then
    sudo sed -i "s/^\[Journal\]\$/[Journal]\nStorage=persistent/" /etc/systemd/journald.conf
    sudo systemctl restart systemd-journald
  fi

  log "Zero WH polish applied -- a reboot is needed for the boot-splash/cursor changes to take effect."
fi

# ---------------------------------------------------------------------------
# wrap-up
# ---------------------------------------------------------------------------

step "Done"

systemctl is-active --quiet shairport-sync && log "shairport-sync: active" || warn "shairport-sync: NOT active"
systemctl is-active --quiet airplay-cec-remote && log "airplay-cec-remote: active" || warn "airplay-cec-remote: NOT active"
systemctl is-active --quiet airplaymatrix-webui && log "airplaymatrix-webui: active" || warn "airplaymatrix-webui: NOT active"
if [[ "$DISPLAY_MODE" == headless ]]; then
  systemctl is-active --quiet airplaymatrix-matrix && log "airplaymatrix-matrix: active" || warn "airplaymatrix-matrix: NOT active"
fi

ip_addr="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo
log "Web UI: http://${ip_addr:-this device}:8080/"
if [[ -n "$WEBUI_PASSWORD_LINE" ]]; then
  log "$WEBUI_PASSWORD_LINE"
  log "(change it from the Account page after logging in -- this is the only time it's shown)"
elif [[ "$WEBUI_FRESH_INSTALL" -eq 1 ]]; then
  warn "Admin password was generated but couldn't be read back -- retrieve it with:"
  warn "  sudo journalctl -u airplaymatrix-webui | grep 'generated initial'"
else
  log "Admin password already set from a previous run (config preserved) -- use your existing one."
fi

if [[ "$DISPLAY_MODE" == kiosk ]]; then
  if [[ "$DO_LAUNCH" -eq 1 ]]; then
    if [[ -S "/run/user/${TARGET_UID}/wayland-0" ]]; then
      log "Launching the kiosk app now..."
      env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" WAYLAND_DISPLAY=wayland-0 \
        "$TARGET_HOME/.local/bin/airplaymatrix-run.sh" \
        >/tmp/airplaymatrix-run.log 2>&1 &
      disown
      log "Launched (pid $!). If it doesn't appear, check /tmp/airplaymatrix-run.log"
    else
      log "No active desktop session detected yet (first boot, or you're over SSH"
      log "before ever logging into the desktop) -- can't launch a GUI app with no"
      log "compositor running. It'll come up automatically on next boot; reboot now"
      log "with: sudo reboot"
    fi
  fi
  log "Shift+X (on the device itself) closes the kiosk app back to the desktop;"
  log "the web UI's 'Restart display app' button relaunches it without a reboot."
else
  log "Headless: the matrix panel is the only output, check it's receiving frames with:"
  log "  sudo journalctl -u airplaymatrix-matrix -f"
fi

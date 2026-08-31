# Full desktop build (reference)

> Most of what's below is automated by `setup.sh` at the repo root --
> `git clone`, `cd AirplayMatrix`, `./setup.sh` -- which detects an
> aarch64/desktop image and installs everything on this page, including the
> kiosk app's autostart routine, then launches it. Read on for what it's
> automating, manual step-by-step instructions if you'd rather not run a
> script, or troubleshooting.

This is what the running reference unit (a Raspberry Pi 5) actually has
installed: AirPlay receive, HDMI-CEC TV control, LED matrix output via the
"Desk Display" Qt/QML kiosk app, and the settings web UI. Use this doc as-is
for a Pi 4/5-class board with a screen attached; for a Pi Zero WH see
[install-pi-zero-wh.md](install-pi-zero-wh.md) instead -- the hardware is
different enough that a lot of this doesn't transfer directly.

Throughout, the username is assumed to be `airplaymatrix` (uid 1000) and the
repo is cloned to `~/Documents/AirplayMatrix-main`. Both are baked into the
shipped `.service` files and `airplaymatrix-privileged.py`'s
`restart-display` command (`XDG_RUNTIME_DIR=/run/user/1000`,
`runuser -u airplaymatrix`); either match them or hand-edit those paths.

## 1. Flash the OS

Raspberry Pi Imager → **Raspberry Pi OS (64-bit), with desktop**. In the
Imager's advanced options (gear icon / Ctrl+Shift+X):
- hostname: `airplaymatrix` (or your choice -- the web UI can change it later)
- username: `airplaymatrix`
- enable SSH
- Wi-Fi credentials, if not using Ethernet for first boot

Boot it, then:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## 2. AirPlay receiver

```bash
sudo apt install -y shairport-sync avahi-daemon
```

Copy the example config and adjust the audio output device:

```bash
sudo cp Software/shairport-sync.conf.example /etc/shairport-sync.conf
aplay -l   # find your output -- HDMI (vc4-hdmi-0) or a USB DAC's card name
sudo nano /etc/shairport-sync.conf   # set alsa.output_device accordingly
sudo systemctl enable --now shairport-sync
```

The example config already wires up the CEC session hooks from step 3 below
(`sessioncontrol.run_this_before/after_entering_active_state`) and the
metadata pipe the desk-display app and matrix daemon both read from.

## 2b. AirPlay 2 (optional but recommended)

`setup.sh` does this by default (pass `--classic-airplay` to skip it). Doing
it by hand:

Classic AirPlay 1 (what the apt package above is) means iOS treats this
receiver the same as any other audio output for *any* app -- opening
Instagram while AirPlaying music can interrupt/replace the stream. AirPlay
2 destinations (a HomePod, an AirPlay-2-certified TV) don't have that
problem: iOS treats them as an app-owned route, and other apps' incidental
sound just plays through the phone's own speaker instead. Neither Debian
nor Raspberry Pi OS ship an AirPlay-2-capable shairport-sync or the
`nqptp` companion clock-sync daemon it needs, so both are built from
source here -- installed to `/usr/local/bin`, not `/usr/bin`, so the apt
package above stays untouched as an instant fallback
(`sudo systemctl revert shairport-sync && sudo systemctl restart shairport-sync`
completely undoes this).

```bash
sudo apt install -y --no-install-recommends \
  build-essential git autoconf automake libtool \
  libpopt-dev libconfig-dev libasound2-dev libavahi-client-dev \
  libssl-dev libsoxr-dev libplist-dev libsodium-dev uuid-dev libgcrypt-dev \
  xxd libplist-utils libavutil-dev libavcodec-dev libavformat-dev \
  libmosquitto-dev libdbus-1-dev libglib2.0-dev \
  systemd-dev   # Debian 13/trixie split pkg-config's systemd.pc out of systemd itself

git clone https://github.com/mikebrady/nqptp.git /tmp/nqptp
cd /tmp/nqptp && autoreconf -fi && ./configure --with-systemd-startup && make
sudo make install
sudo systemctl enable --now nqptp
cd - && rm -rf /tmp/nqptp

git clone https://github.com/mikebrady/shairport-sync.git /tmp/shairport-sync
cd /tmp/shairport-sync && autoreconf -fi
# Same feature set as the apt package (dbus/mpris for the CEC remote
# control, metadata for the desk-display app, mqtt/soxr/convolution for
# parity) plus --with-airplay-2. Flag names current as of shairport-sync
# 5.2.3 -- the 4.x names (--with-dbus, --with-mpris, --with-mqtt, --with-pa)
# are silently accepted then ignored rather than erroring, not a config-time
# error you'd notice.
./configure --sysconfdir=/etc \
  --with-alsa --with-soxr --with-avahi --with-ssl=openssl \
  --with-systemd-startup --with-airplay-2 \
  --with-metadata --with-dbus-interface --with-mpris-interface --with-mqtt-client \
  --with-stdout --with-pipe --with-dummy --with-convolution
make
sudo make install
cd - && rm -rf /tmp/shairport-sync
```

Wire the new binary in via a systemd override rather than editing the apt
package's unit file:

```bash
sudo mkdir -p /etc/systemd/system/shairport-sync.service.d
sudo tee /etc/systemd/system/shairport-sync.service.d/override.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/local/bin/shairport-sync $DAEMON_ARGS
EOF
sudo systemctl daemon-reload
sudo systemctl restart shairport-sync
shairport-sync -V   # confirm "AirPlay2" appears in the version string
```

Building this natively is slow but not memory-heavy on a Pi 4/5 -- it's a
real concern on a Pi Zero WH's single ARM1176 core with 512MB RAM, where
`setup.sh` adds a temporary swapfile and stops the kiosk app for the
duration of the build; see install-pi-zero-wh.md.

## 3. HDMI-CEC (TV power + remote passthrough)

```bash
sudo apt install -y v4l-utils   # provides cec-ctl
```

Find your TV's CEC device (usually `/dev/cec0` for the first HDMI port) and
confirm you can talk to it:

```bash
cec-ctl -d /dev/cec0 --playback
cec-ctl -d /dev/cec0 --to 0 --image-view-on
```

Install the scripts and give `cec-ctl` the capability it needs to run in
monitor mode without root:

```bash
sudo cp Software/cec/airplay-tv-power.sh /usr/local/bin/
sudo cp Software/cec/airplay-cec-remote.py /usr/local/bin/
sudo cp Software/cec/airplaymatrix-quit.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/airplay-tv-power.sh /usr/local/bin/airplay-cec-remote.py /usr/local/bin/airplaymatrix-quit.sh

sudo setcap cap_net_admin+ep "$(command -v cec-ctl)"
sudo usermod -aG video shairport-sync   # for /dev/cec0 access

sudo cp Software/cec/airplay-cec-remote.service /etc/systemd/system/
sudo systemctl enable --now airplay-cec-remote.service
```

`airplay-tv-power.sh` is invoked by shairport-sync itself (see the
`sessioncontrol` block in the conf) so it needs no separate service. If your
TV is on a different HDMI input than `/dev/cec0`, edit `CEC_DEV` at the top
of that script.

## 4. Settings web UI

```bash
cd Software/webui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo install -o root -g root -m 0700 airplaymatrix-privileged.py /usr/local/bin/airplaymatrix-privileged.py
sudo visudo -cf airplaymatrix-webui.sudoers   # validate syntax
sudo install -o root -g root -m 0440 airplaymatrix-webui.sudoers /etc/sudoers.d/airplaymatrix-webui

sudo cp airplaymatrix-webui.service /etc/systemd/system/
sudo systemctl enable --now airplaymatrix-webui.service
```

The admin password is generated on first run and printed once to the
journal:

```bash
sudo journalctl -u airplaymatrix-webui -n 30 | grep "generated initial"
```

Log in at `http://<hostname-or-ip>:8080/` and change it from the Account
page. `airplaymatrix-webui.sudoers` grants that service NOPASSWD root access
to exactly one script; see that script's docstring before changing anything
here.

## 5. Desk-display kiosk app

Install PySide6 and the other Python deps into a venv (PyPI ships PySide6
wheels for aarch64, so this is a plain pip install on 64-bit Raspberry Pi
OS -- no build-from-source needed):

```bash
cd Software
python3 -m venv .venv
.venv/bin/pip install PySide6 pyserial pillow
```

(Or use `sudo apt install python3-pyside6.qtcore python3-pyside6.qtquick
python3-pyside6.qtquickcontrols2 python3-pyside6.qtqml python3-serial
python3-pil` and skip the venv, running with the system interpreter instead
-- either works, just be consistent about which interpreter
`airplaymatrix-run.sh` below invokes. This is what the reference unit
actually runs, and what `setup.sh` at the repo root uses -- the venv route
above is equally valid, just less exercised on real hardware.)

Autostart it from the desktop session:

```bash
mkdir -p ~/.local/bin
install -m 0755 <(cat <<'EOF'
#!/bin/bash
set -e
cd ~/Documents/AirplayMatrix-main/Software
export QT_QPA_PLATFORM=wayland
exec python3 -m app.main
EOF
) ~/.local/bin/airplaymatrix-run.sh
```

(If you used the venv above instead of system packages, point `exec` at
`.venv/bin/python3` instead of the bare `python3`.)

Append to `~/.config/labwc/autostart` (see
`Software/labwc/autostart.example`):

```
/home/airplaymatrix/.local/bin/airplaymatrix-run.sh &
```

And add the Shift+X "quit the kiosk app" keybind to
`~/.config/labwc/rc.xml` (see `Software/labwc/rc.xml.snippet.txt`) inside
the existing `<keyboard>` section:

```xml
<keybind key="S-x">
  <action name="Execute">
    <command>/usr/local/bin/airplaymatrix-quit.sh</command>
  </action>
</keybind>
```

Reboot. The kiosk app should come up full-screen; Shift+X closes it back to
the desktop, and the web UI's "Restart display app" button relaunches it
without a full reboot.

## 6. The ESP32/HUB75 matrix

Flash the companion ESP32 firmware (separate PlatformIO project, not in
this repo) and connect it to the Pi over USB. Both the kiosk app and
`matrix_daemon.py` auto-detect it by USB VID:PID (`matrix/link.py`,
`MATRIX_VID`/`MATRIX_PID` -- update these if you're using a different
USB-UART bridge chip) and reconnect automatically if it's unplugged.

## Verifying

- `systemctl status shairport-sync nqptp airplay-cec-remote airplaymatrix-webui`
- `shairport-sync -V` should show `AirPlay2` (step 2b) -- if it doesn't,
  `systemctl show shairport-sync -p ExecStart` to confirm the override in
  step 2b actually took (should point at `/usr/local/bin/shairport-sync`,
  not `/usr/bin/shairport-sync`).
- AirPlay from a phone/laptop to the device's name (default "AirplayMatrix")
  should wake the TV, start playback, and show artwork on the kiosk screen
  and the LED matrix. With AirPlay 2 (step 2b), opening another app on the
  phone (e.g. Instagram) shouldn't interrupt the stream.
- Stopping playback for 5 minutes (`active_state_timeout` in the conf)
  should put the TV in standby.
- AirPlay should connect at the web UI's "AirPlay connect volume" setting
  (75% by default) regardless of what a source app last remembered, and
  the phone's own volume slider should keep working normally after that.

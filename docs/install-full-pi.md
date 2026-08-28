# Full desktop build (reference)

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
python3-pyside6.qtquickcontrols2 python3-pyside6.qtqml python3-pyserial
python3-pil` and skip the venv, running with the system interpreter instead
-- either works, just be consistent about which interpreter
`airplaymatrix-run.sh` below invokes.)

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

- `systemctl status shairport-sync airplay-cec-remote airplaymatrix-webui`
- AirPlay from a phone/laptop to the device's name (default "AirplayMatrix")
  should wake the TV, start playback, and show artwork on the kiosk screen
  and the LED matrix.
- Stopping playback for 5 minutes (`active_state_timeout` in the conf)
  should put the TV in standby.

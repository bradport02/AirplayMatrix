# Pi Zero WH build

The reference unit is a Pi 5. A Pi Zero WH (the original Zero W with
pre-soldered headers -- **not** the Zero 2 W) is a very different machine,
and that difference matters for this project specifically:

| | Pi 5 (reference) | Pi Zero WH |
|---|---|---|
| CPU | quad-core Cortex-A76 @ 2.4GHz | single-core ARM1176 (ARMv6) @ 1GHz |
| RAM | 4-8GB | 512MB |
| OS | 64-bit only | **32-bit only** -- ARMv6 cannot run an aarch64 kernel |
| USB | 2x USB3 + 2x USB2 | one micro-USB **OTG data** port, full stop |
| Audio out | none built in | none built in |
| HDMI | full-size, 2 ports | mini-HDMI, 1 port |

Two things fall out of this before you buy anything or flash a card:

**PySide6/Qt6 has no official armv6 build.** The Qt Company only publishes
PySide6 wheels on PyPI for x86_64 and aarch64 -- there's no `pip install
PySide6` on a 32-bit Zero. The only route to the Desk Display kiosk app on
this hardware is Raspberry Pi OS's own apt-packaged
`python3-pyside6.*`, and I can't confirm from here whether Raspberry Pi OS
still builds those for the ARMv6 baseline the Zero WH needs (as opposed to
ARMv7+, which the Zero 2 W and everything newer uses). **Check this before
doing anything else**, on the actual device:

```bash
apt-cache search pyside6
apt-cache policy python3-pyside6.qtquick
```

If those come back empty, the kiosk app cannot run here at all and you
should skip straight to [Plan B: headless](#plan-b-headless-no-kiosk-screen)
below. If they do exist, you still have a real risk to weigh: a single
1GHz core has to run shairport-sync's real-time audio path, HDMI-CEC
handling, *and* a Qt Quick scene graph simultaneously. Audio glitches under
that contention are plausible and I have not been able to test this on
actual Zero WH hardware -- if you hit them, Plan B is a straight swap, no
Pi-side hardware changes needed.

## Hardware for this build

- Pi Zero WH
- microSD card
- ESP32 board (as in the full build) -- it's the *only* thing on the
  Zero's one USB data port, so **don't** plan on a USB audio dongle here
  unless you also add a powered USB hub
- **Audio + CEC, recommended: a passive mini-HDMI-to-HDMI cable straight
  into the TV.** This gets you CEC and audio through the TV's own speakers
  with zero extra hardware, and it's the same `vc4-hdmi` ALSA path the
  reference unit's config already uses (`shairport-sync.conf.example`'s
  `output_device = "default"` needs no changes). If you want dedicated
  audio output instead of the TV's speakers, prefer an **I2S DAC HAT**
  (uses GPIO, doesn't touch the USB port) over a USB DAC.
- Separate 5V micro-USB power supply into the Zero's `PWR IN` port (the
  data port is spoken for by the ESP32)

## 1. Flash the OS

Raspberry Pi Imager → **Raspberry Pi OS Lite (32-bit)** if you're doing
Plan B, or **Raspberry Pi OS (32-bit), with desktop** if you're attempting
the kiosk app. Same advanced-options setup as the full build: hostname
`airplaymatrix`, username `airplaymatrix`, SSH enabled, Wi-Fi credentials.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## 2. AirPlay receiver

Same as the full build:

```bash
sudo apt install -y shairport-sync avahi-daemon
sudo cp Software/shairport-sync.conf.example /etc/shairport-sync.conf
aplay -l   # confirm the HDMI audio device name matches "default"/vc4hdmi0
sudo systemctl enable --now shairport-sync
```

## 3. HDMI-CEC

Identical to the full build -- the Zero only has one HDMI port, so
`/dev/cec0` should be right without checking further:

```bash
sudo apt install -y v4l-utils
sudo setcap cap_net_admin+ep "$(command -v cec-ctl)"
sudo usermod -aG video shairport-sync

sudo cp Software/cec/airplay-tv-power.sh /usr/local/bin/
sudo cp Software/cec/airplay-cec-remote.py /usr/local/bin/
sudo chmod +x /usr/local/bin/airplay-tv-power.sh /usr/local/bin/airplay-cec-remote.py
sudo cp Software/cec/airplay-cec-remote.service /etc/systemd/system/
sudo systemctl enable --now airplay-cec-remote.service
```

(Skip `airplaymatrix-quit.sh` here if you're going headless -- it's only
meaningful with the kiosk app running.)

## 4. Settings web UI

Identical to the full build (step 4 there) -- Flask is light enough that
this isn't a concern on the Zero. Follow it as written.

## 5. Display: kiosk app or headless daemon

### Plan A: the kiosk app

If `apt-cache search pyside6` above actually found packages, follow
[install-full-pi.md, step 5](install-full-pi.md#5-desk-display-kiosk-app)
using the apt-package route (not the pip/venv one -- there's no PySide6
wheel to pip-install here), substituting `python3-pyside6.qtcore` etc. for
whatever the search above turned up. Then watch for audio glitches during
playback (`journalctl -u shairport-sync -f` while something plays) and CPU
saturation (`top` -- a sustained single core near 100% while the UI is
idle is a bad sign). If it's not holding up, move to Plan B; nothing
you've installed so far conflicts with it.

### Plan B: headless (no kiosk screen)

No Qt, no desktop environment needed -- this runs fine on **Raspberry Pi OS
Lite (32-bit)**.

```bash
sudo apt install -y python3-pyserial python3-pil
sudo usermod -aG dialout shairport-sync   # serial access to the ESP32

sudo cp Software/matrix/airplaymatrix-matrix.service /etc/systemd/system/
sudo systemctl enable --now airplaymatrix-matrix.service
```

`matrix_daemon.py` (`Software/matrix/matrix_daemon.py`) reuses the exact
same `metadata.py`/`encoder.py`/`matrix/link.py` code the kiosk app's
`MatrixController` does -- same artwork pipeline, same USB
auto-detect/reconnect, same blank-on-session-end behaviour -- just without
the QObject/QML plumbing that exists solely to feed a screen you don't have
in this build. There's no lyrics fetching, no now-playing display: the
matrix panel is the only output.

Check it's running and pushing frames:

```bash
sudo systemctl status airplaymatrix-matrix
sudo journalctl -u airplaymatrix-matrix -f
```

## Verifying

- `systemctl status shairport-sync airplay-cec-remote airplaymatrix-webui`
  (+ `airplaymatrix-matrix` if you went headless, or check the kiosk app is
  on-screen if not)
- AirPlay to the device should wake the TV over CEC and start audio.
- Album art should appear on the LED matrix within a second or two of a
  track starting.
- Leave it idle for 5 minutes after stopping playback -- the TV should go
  to standby (`active_state_timeout` in the conf).

## If you outgrow the Zero WH

The Wi-Fi/CEC/matrix pipeline software is identical between builds -- if
the Zero's CPU genuinely can't keep the kiosk app smooth even after
tuning, or you decide you want it after starting headless, the only real
option is more CPU: a Zero 2 W (quad-core Cortex-A53, supports 64-bit,
official PySide6 wheels available) is the natural step up while keeping
the same small form factor, or move to the full build's Pi 4/5 target.

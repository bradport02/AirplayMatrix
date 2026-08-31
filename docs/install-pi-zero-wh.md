# Pi Zero WH build

> Most of what's below is automated by `setup.sh` at the repo root --
> `git clone`, `cd AirplayMatrix`, `./setup.sh` -- which auto-detects a
> 32-bit/non-aarch64 image and picks the Qt5 kiosk build (Plan A) or the
> headless daemon (Plan B) for you based on whether a desktop session is
> present, no flags needed. Read on for the hardware risk this build
> carries, the ARMv6 caveats that are worth understanding before you buy
> anything, manual step-by-step instructions if you'd rather not run a
> script, and troubleshooting.

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

**PySide6/Qt6 is a no-go on this hardware** -- confirmed. The Qt Company
only publishes PySide6 wheels on PyPI for x86_64 and aarch64, so there's no
`pip install PySide6` on a 32-bit Zero, and Raspberry Pi OS's own apt
archive doesn't carry a usable `python3-pyside6.*` for the Zero's ARMv6
baseline either. That rules out running `app/` (the Pi 5's Qt6 build)
here at all, in any form.

**Plan A here is a second build of the kiosk app on Qt5/PySide2**
(`app_qt5/`), not the Qt6 one repackaged -- Qt5 is a genuinely different
major version, not a recompile, so it's real second QML/Python tree,
ported by hand from `app/`. It looks and behaves the same (same background,
album art, and duration), with two differences:

- Two things that were always-on in `app/` are now toggles in the settings
  web UI, saved to `~/.config/airplaymatrix-display/config.json`
  (`Software/display_settings.py`) and picked up by the running app within
  a couple of seconds, no restart needed:
  - **Song details** (title/artist/album) -- **on** by default.
  - **Lyrics** -- **off** by default. This is the single most expensive
    thing the app does (per-frame scrolling text layout on top of a
    network lookup per track), and the reason both toggles exist at all:
    flip it on from the web UI and watch `top`/`journalctl -u
    shairport-sync -f` to see whether this specific ARM1176 can actually
    carry it, without needing SSH or a reboot to try.
  - Album art, scrub bar/duration, and LED matrix output behave identically
    to `app/` and aren't optional.
- Beyond that pair of toggles, this build also trims a few things `app/`
  always does, on the assumption that a single ARM1176 core has no GPU
  headroom to spare on decoration -- none of these are configurable, they're
  just gone from `app_qt5/`:
  - **Idle/waiting screen**: flat black with plain static white text
    ("Waiting for AirPlay" / "Waiting for connection", with a small
    "Discoverable: <name>" line above showing the name set in the web UI),
    not `app/`'s pulsing radar-ring animation -- this is the screen the app
    sits on the most, so it's the one most worth costing the GPU nothing at
    all. See `app_qt5/qml/NowPlayingView.qml`.
  - **Ambient colour wash**: `app/`'s four soft colour blobs (each its own
    FastBlur pass) fading in over the blurred artwork while playing are
    dropped entirely, not just the Qt6 saturation/brightness grade on top
    of them -- four extra continuous GPU blur passes stacked on a single
    ARM1176 core is the kind of cost this build can't spend. The blurred,
    darkened artwork backdrop itself is kept (`QtGraphicalEffects`' FastBlur
    stands in for `QtQuick.Effects`'s Qt6-only `MultiEffect` there and for
    the album-art shadow). See `app_qt5/qml/Background.qml`'s comment for
    the detail.

As with the Qt6 attempt this replaces, whether a single 1GHz ARM1176 core
can run shairport-sync's real-time audio path, HDMI-CEC handling, *and* a
Qt Quick scene graph at the same time -- Qt5's scene graph is lighter than
Qt6's, but still real GPU/CPU work -- is a real open question I have not
been able to test on actual Zero WH hardware. Watch for audio glitches
(`journalctl -u shairport-sync -f` while something plays) and sustained
near-100% CPU (`top`) while trying it, **starting with lyrics off** (the
default) before testing whether turning them on tips it over. If it doesn't
hold up even with lyrics off, [Plan B: headless](#plan-b-headless-no-kiosk-screen)
below is a straight swap, no Pi-side hardware changes needed.

**Check the apt packages exist before doing anything else**, on the actual
device -- confirmed present in Debian trixie's archive (which Raspberry Pi
OS Bookworm/Trixie mirrors closely for non-RPi-specific packages like Qt
bindings) as of this doc being written, but that was checked on 64-bit
hardware, not the Zero's 32-bit ARMv6 target, so still verify here rather
than trust that blindly:

```bash
apt-cache policy python3-pyside2.qtcore python3-pyside2.qtquick python3-pyside2.qtqml python3-pyside2.qtgui
apt-cache policy qml-module-qtgraphicaleffects qml-module-qtquick-layouts qml-module-qtquick-controls2 qml-module-qtquick-window2 qml-module-qtquick2
```

If any of those come back with no candidate, the kiosk app cannot run here
at all and you should skip straight to Plan B.

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

## 2b. AirPlay 2 (optional but recommended)

Also same as the full build (install-full-pi.md's step 2b) -- `setup.sh`
does this by default, pass `--classic-airplay` to skip it. The one
Zero-WH-specific thing worth knowing before running it by hand: this
single ARM1176 core with 512MB RAM is genuinely tight for a native build,
so add a temporary swapfile and free up RAM first (`setup.sh` does both of
these automatically):

```bash
sudo fallocate -l 1G /swapfile-build && sudo chmod 600 /swapfile-build
sudo mkswap /swapfile-build && sudo swapon -p 10 /swapfile-build
# then follow install-full-pi.md's step 2b, and finally:
sudo swapoff /swapfile-build && sudo rm -f /swapfile-build
```

If the kiosk app is already running (a re-run, not a fresh install), stop
it first (`pkill -f app_qt5.main`) so the build isn't competing with it for
RAM, then relaunch it afterward. labwc's own autostart (step 5 below) isn't
respawned, so relaunching by hand over SSH needs these set explicitly --
none of them are inherited from an SSH session the way they would be from
a shell already running inside the desktop session:

```bash
DISPLAY=:0 XAUTHORITY=~/.Xauthority WAYLAND_DISPLAY=wayland-0 \
  XDG_RUNTIME_DIR=/run/user/1000 ~/.local/bin/airplaymatrix-run.sh &
```

Without these, Qt5's XCB platform plugin fails to start at all (`could not
connect to display`) rather than falling back to something visible.

Building itself took about 20 minutes on real Zero WH hardware -- CPU-bound
on the single core, not memory-bound once the swapfile above is in place.

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

If you're attempting Plan A (the kiosk app) below, also install the Shift+X
"quit the kiosk app" script -- skip this if you already know you're going
headless (Plan B), it has nothing to bind to there:

```bash
sudo cp Software/cec/airplaymatrix-quit.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/airplaymatrix-quit.sh
```

## 4. Settings web UI

Identical to the full build (step 4 there) -- Flask is light enough that
this isn't a concern on the Zero. Follow it as written.

## 5. Display: kiosk app or headless daemon

### Plan A: the kiosk app (Qt5/PySide2 build)

Raspberry Pi OS **(32-bit), with desktop** (not Lite) for this plan. There's
no PySide2 wheel for armv6 to `pip install` any more than there was for
PySide6, so this is apt packages + the system interpreter, no venv (be
consistent about that below -- `airplaymatrix-run.sh` has to invoke whatever
interpreter actually has these packages):

```bash
sudo apt install -y \
  python3-pyside2.qtcore python3-pyside2.qtgui python3-pyside2.qtqml python3-pyside2.qtquick \
  qml-module-qtquick2 qml-module-qtquick-layouts qml-module-qtquick-controls2 qml-module-qtquick-window2 \
  qml-module-qtgraphicaleffects \
  python3-serial python3-pil
```

(No `dialout` group step here, matching the full build -- Raspberry Pi
Imager's default user already gets `dialout` among its standard
supplementary groups, which is what actually gives `app_qt5`, running as
that desktop user, ESP32 serial access. Only the headless daemon in Plan B
needs one added explicitly, because it deliberately runs as the
`shairport-sync` service user instead.)

Autostart, same shape as the full build's step 5 but pointing at the Qt5
package (`app_qt5`, not `app`) and no `QT_QPA_PLATFORM` override -- Qt5's
Wayland QPA plugin isn't in the apt set installed above, and the default
(XCB, under the desktop session's own X11/Xwayland) is what's actually
tested here:

```bash
mkdir -p ~/.local/bin
install -m 0755 <(cat <<'EOF'
#!/bin/bash
set -e
cd ~/Documents/AirplayMatrix-main/Software
exec python3 -m app_qt5.main
EOF
) ~/.local/bin/airplaymatrix-run.sh
```

Append to `~/.config/labwc/autostart` and add the Shift+X quit keybind to
`~/.config/labwc/rc.xml` -- both identical to
[install-full-pi.md, step 5](install-full-pi.md#5-desk-display-kiosk-app),
substituting nothing (`airplaymatrix-run.sh`, `airplaymatrix-quit.sh`, and
the web UI's "Restart display app" button all work unmodified across both
builds; see those files' comments if you're curious why).

Reboot. The kiosk app should come up full-screen, with song details on and
lyrics off (the defaults) -- toggle either from the web UI dashboard
(`http://airplaymatrix.local:8080/`, the "Desk display" card) without
restarting anything, and watch for audio glitches during playback
(`journalctl -u shairport-sync -f`) and CPU saturation (`top` -- a
sustained single core near 100% is a bad sign) as you do. **Test with
lyrics off first, then turn them on**, since that's the toggle this whole
build exists to let you test safely. If it's not holding up even with
lyrics off, move to Plan B; nothing you've installed so far conflicts with
it (stop/disable the kiosk app's autostart line in
`~/.config/labwc/autostart` first, so the two don't fight over the ESP32's
serial port).

### Plan B: headless (no kiosk screen)

No Qt, no desktop environment needed -- this runs fine on **Raspberry Pi OS
Lite (32-bit)**.

```bash
sudo apt install -y python3-serial python3-pil
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

- `systemctl status shairport-sync nqptp airplay-cec-remote airplaymatrix-webui`
  (+ `airplaymatrix-matrix` if you went headless, or check the kiosk app is
  on-screen if not)
- `shairport-sync -V` should show `AirPlay2` (step 2b) -- opening another
  app on the phone (e.g. Instagram) while AirPlaying shouldn't interrupt
  the stream.
- AirPlay to the device should wake the TV over CEC, start audio, and
  connect at the web UI's "AirPlay connect volume" setting (75% by
  default) regardless of what a source app last remembered -- the phone's
  own volume slider should keep working normally after that.
- Album art should appear on the LED matrix within a second or two of a
  track starting.
- Leave it idle for 5 minutes after stopping playback -- the TV should go
  to standby (`active_state_timeout` in the conf).
- If you went with Plan A, the web UI dashboard's "Desk display" card
  should show song details on / lyrics off (the defaults), and toggling
  either should change what's on screen within a couple of seconds with no
  restart. If lyrics land early or late (step 2b's AirPlay 2 buffering vs.
  shairport-sync's progress metadata), dial in the "Lyrics offset" field
  there too.

## Boot time and idle-screen polish (Plan A only)

`setup.sh` applies all of this automatically on the Qt5 kiosk path (pass
`--no-polish` to skip it -- see `./setup.sh --help`). It's written up here
because every number below was measured live on real Zero WH hardware, not
theorised, and because a couple of these are worth understanding before you
just trust a script to edit `cmdline.txt` for you.

**Boot time.** A stock Raspberry Pi OS Desktop image on this hardware took
**3min 45s** from power-on to the kiosk app appearing. Two things accounted
for nearly all of it:

- **cloud-init** (`cloud-init-local`/`-main`/`-network`/`-config`/`-final`,
  ~43s combined) -- this is what Raspberry Pi Imager's first-boot
  customisation (hostname/user/Wi-Fi/SSH) runs on top of. It has nothing
  left to do on every boot after the first, but re-detects that from
  scratch each time regardless. `sudo touch /etc/cloud/cloud-init.disabled`
  is the officially documented way to skip it (checked by `ds-identify`
  before any real detection work) -- safe on a single fixed-purpose device
  that will never be re-seeded with new cloud-init user-data.
- **A NetworkManager/netplan reload storm** (~55s) -- four separate
  `systemctl reload NetworkManager.service` cycles, ~15s apart, each
  costing ~9s on this CPU. Traced to `/lib/netplan/00-network-manager-all.yaml`
  shipping world-readable (0644) when netplan's own security check wants
  0600 on anything it manages -- `sudo chmod 600` on the netplan-owned
  YAML files fixed it. (A *second*, larger NetworkManager cost -- the
  reload cycles' actual reconciliation work, ~87s independent of the
  permission issue -- was investigated but deliberately left alone: fixing
  it further would mean changing how NetworkManager/netplan manage the one
  interface this device is reachable through, and getting that wrong with
  no physical console means a full re-flash to recover. Not worth it for a
  device you only reach over Wi-Fi.)

Together those two got boot down to **2min 13s**. A further ~20s came from
disabling a handful of background services this kiosk never needs --
Bluetooth, disk auto-mount (`udisks2`), RPi Connect's screen sharing
(`wayvnc`, not its remote-shell half, which is left alone), the generic
system `wayvnc`/`wayvnc-control` pair, and two Pi 5-only hardware-probe
services (`rp1-test`, `glamor-test`) that no-op on this SoC but still cost
boot time here. The same services were also worth ~80MB of RAM back at
runtime -- see the next section.

**RAM.** This build runs on a **512MB** device, of which roughly 426MB is
actually usable after the GPU memory split. A stock Desktop image's full
session -- taskbar, desktop icons, RPi Connect, PolicyKit auth agent, gvfs
auto-mount, xdg-desktop-portal -- was measured using enough of that to push
the box into **active swap** during playback (up to 140MB swapped, on an SD
card), which is almost certainly what a stuttering/dropping AirPlay session
on this hardware actually is: real-time audio and networking both stalling
on synchronous SD-card I/O, not a CPU shortage. Disabling the background
services above, plus the taskbar/desktop-icon autostart lines in
`/etc/xdg/labwc/autostart` (invisible underneath the full-screen kiosk
window anyway), took swap usage from ~140MB down to ~70MB under the same
load. If you still see audio glitches or failed AirPlay reconnects after
this, check `free -h` and `vmstat 1` for swap activity before assuming it's
a lyrics/CPU problem -- and check `journalctl` (now persistent across
reboots, see below) for what shairport-sync/avahi were doing at the time.

**Boot splash and idle cursor.** The Raspberry Pi rainbow splash and
Plymouth's graphical theme are both disabled --
`Software/boot/airplaymatrix-boot-message.sh`, running as a plain systemd
service on `tty1`, shows a static "AirPlay Matrix is booting..." message
with a cycling `...` instead. This is deliberately *not* a custom Plymouth
theme: that would need the theme baked into the initramfs to show anything
before the root filesystem is even mounted, and rebuilding an initramfs on
a device with no physical console to recover it with if that goes wrong is
a real risk this project isn't taking for a cosmetic feature. The
trade-off worth knowing: Plymouth's graphical splash was also *hiding*
some kernel driver spam (mostly `dwc_otg`/`vc4-drm` messages, harmless but
noisy) and systemd's own `Starting X...`/`Finished X...` status lines --
disabling Plymouth without separately silencing those leaks them straight
to the console. `loglevel=1` and `systemd.show_status=false` on the kernel
command line handle that independently of Plymouth. One thing deliberately
left alone: a brief `fsck`/`rootfs: clean...` line, since that's the
filesystem integrity check writing directly to console (not through the
kernel log, so `loglevel` can't touch it) -- silencing that would mean
silencing a real safety check, not just cosmetics.

Separately, the mouse pointer that briefly appears while the kiosk app
loads is labwc's own default cursor, shown in the gap between the
compositor starting and the app's own `setOverrideCursor(BlankCursor)`
call (`app_qt5/main.py`) taking effect. `Software/boot/make_blank_cursor_theme.py`
hand-builds a minimal, fully-transparent Xcursor theme (no `xcursorgen`
dependency -- not packaged for this device's 32-bit armhf archive) and
points labwc at it via `XCURSOR_THEME`/`XCURSOR_SIZE` in
`~/.config/labwc/environment`, closing that gap too.

**Debuggability.** Journal storage is volatile (`/run`) by default on this
image, which means every reboot -- including the ones used to *recover*
from a problem -- destroys the evidence needed to diagnose it.
`Storage=persistent` in `/etc/systemd/journald.conf` (plus creating
`/var/log/journal`) fixes that for a negligible amount of SD card space.

## If you outgrow the Zero WH

The Wi-Fi/CEC/matrix pipeline software is identical between builds -- if
the Zero's CPU genuinely can't keep the kiosk app smooth even after
tuning, or you decide you want it after starting headless, the only real
option is more CPU: a Zero 2 W (quad-core Cortex-A53, supports 64-bit,
official PySide6 wheels available) is the natural step up while keeping
the same small form factor, or move to the full build's Pi 4/5 target.

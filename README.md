# AirplayMatrix

A Raspberry Pi that shows up as an AirPlay speaker, plays the audio out over
HDMI/analog, and pushes the currently-playing album art to a 64x64 HUB75 LED
matrix (driven by a companion ESP32 board over USB serial). It also turns
the TV on/off over HDMI-CEC when a session starts/stops, forwards TV-remote
button presses into AirPlay transport controls, advertises the AirPlay
device name as its CEC OSD name (shown as the HDMI input's label by
receivers/TVs that query it -- support varies by brand/firmware), and
exposes a small password-protected web UI for day-to-day settings.

## Quick start

Flash the OS first (Raspberry Pi Imager's advanced options set the
hostname/username/SSH/Wi-Fi at flash time -- username **must** be
`airplaymatrix`, see either install doc's step 1 if you haven't done this
yet), boot the Pi, then as that user:

```bash
git clone https://github.com/bradport02/AirplayMatrix.git
cd AirplayMatrix
./setup.sh
```

That's the whole install. It:

1. **Detects the hardware** -- a desktop/labwc session present means the Qt
   kiosk display, nothing means the headless matrix daemon; on the kiosk
   path, `uname -m` picks the Qt6 build (aarch64) or the Qt5 build
   (everything else, e.g. Pi Zero WH), which also gets a round of boot-time/
   idle-screen polish (cloud-init, background services, boot splash, mouse
   cursor -- see install-pi-zero-wh.md). Override with `--kiosk` /
   `--headless` / `--qt5` / `--qt6` / `--no-polish` if you want something
   other than the default for this hardware; `./setup.sh --help` lists all
   of them.
2. **Checks for and removes any previous install** -- stops/disables
   whatever's already running before reinstalling, and if you're switching a
   device between the kiosk display and headless (or back), cleanly tears
   down whichever one you're switching *away* from so the two never end up
   fighting over the ESP32's one USB serial port. **Safe to re-run** at any
   time -- nothing here touches Wi-Fi credentials, the web UI's admin
   password, or the lyrics/song-details toggles.
3. **Installs and starts everything**: AirPlay receive, HDMI-CEC, the
   settings web UI, and the display (kiosk app + its autostart routine, or
   the headless daemon).
4. **Prints the web UI's login URL and, on a first install, its
   admin password** (shown once, ever -- change it from the Account page
   after logging in).
5. **Opens the app** -- launches the kiosk display immediately if a desktop
   session is already up, or tells you to reboot if this is a fresh boot
   with no session to launch into yet (headless has nothing to open; check
   it's pushing frames with `sudo journalctl -u airplaymatrix-matrix -f`
   instead).

See the two docs below for what each step is automating and why -- this
script is those docs turned into commands, not a separate design.

## Build targets

Two hardware targets are documented, both handled automatically by
`setup.sh` above -- read these for hardware-specific caveats, manual/
step-by-step instructions, or troubleshooting, not as a prerequisite to
running the script:

| Build | Hardware | What you get |
|---|---|---|
| [Full desktop build](docs/install-full-pi.md) | Pi 4/5-class board, screen attached | Everything below, plus the on-screen "Desk Display" kiosk app (album art, lyrics, now-playing) |
| [Pi Zero WH build](docs/install-pi-zero-wh.md) | Pi Zero WH (original, ARM11) | Everything below, plus a lighter Qt5/PySide2 kiosk app (`app_qt5/`) with lyrics/song-details as web UI toggles -- see that doc for the hardware risk and a headless fallback |

"Everything below" = AirPlay receive, HDMI-CEC TV control + remote
passthrough, LED matrix output, and the settings web UI.

## How it fits together

```
                     ┌────────────────────────────┐
   AirPlay client ──▶│  shairport-sync (systemd)  │──▶ audio out (HDMI/USB DAC)
                     │  + metadata FIFO           │
                     └─────────────┬──────────────┘
                                   │ /tmp/shairport-sync-metadata
                    ┌──────────────┼───────────────────────┐
                    ▼                                       ▼
        ┌───────────────────────┐             ┌─────────────────────────┐
        │ Desk Display (Qt/QML) │  --or--     │ matrix_daemon.py         │
        │ app/ (Qt6, Pi 5)      │             │ (headless build)         │
        │ app_qt5/ (Qt5, Zero)  │             │                          │
        └───────────┬───────────┘             └────────────┬────────────┘
                    │ encode 64x64 JPEG, base64 over USB serial          │
                    └──────────────────────┬───────────────────────────┘
                                            ▼
                                  ESP32 + HUB75 LED matrix

  HDMI-CEC: shairport-sync session hooks → airplay-cec-remote/airplay-tv-power
            (TV power on/off, remote D-pad → AirPlay play/pause/skip)

  Settings: airplaymatrix-webui (Flask, :8080) → sudo → airplaymatrix-privileged.py
            (device name, Wi-Fi, TV timeout, hostname, restarts, reboot)
```

The desk-display app (whichever Qt build), and `matrix_daemon.py`, are
independent consumers of the same shairport-sync metadata pipe and the same
`encoder.py`/`matrix/link.py` code -- only one of them needs to run, and
they're never run together on the same box. `app/` and `app_qt5/` are
likewise never both installed on the same box -- one Pi runs one Qt major
version's build, picked by which hardware it is (see the two install docs).

## Repository layout

```
setup.sh                    fresh-install/re-run script -- see Quick start above

Software/
  receiver/               (conceptual only -- shairport-sync itself is an apt package, not vendored here)
  shairport-sync.conf.example   AirPlay name, alsa output, metadata pipe, CEC session hooks
  metadata.py              shairport-sync metadata pipe parser + track state
  encoder.py                artwork downscale/JPEG-encode for the 64x64 panel
  lrclib.py                 lyrics lookup (used by the desk-display app only)
  receiver.py                cross-platform receiver process supervisor (Pi: no-op; Windows: WSL2)
  sps_bridge.py               re-serves the metadata pipe over TCP (Windows/WSL only)
  display_settings.py           shared show_lyrics/show_details toggle state (JSON under ~/.config), read by app_qt5 and written by the web UI
  airplay_name.py                reads the AirPlay device name out of shairport-sync.conf, shared by both kiosk apps' "Discoverable: <name>" idle-screen line
  matrix/
    link.py                    serial protocol to the ESP32/HUB75 firmware
    matrix_daemon.py            headless metadata → matrix bridge (no Qt), for constrained hardware
    airplaymatrix-matrix.service
  cec/
    airplay-tv-power.sh          TV power on/standby via cec-ctl, called from shairport-sync's sessioncontrol hooks
    airplay-cec-remote.py          TV remote passthrough → shairport-sync D-Bus RemoteControl
    airplay-cec-remote.service
    airplaymatrix-quit.sh           closes the desk-display kiosk app (bound to Shift+X)
  webui/
    app.py, templates/, static/    Flask settings UI (device name, Wi-Fi, TV timeout, hostname, reboot)
    airplaymatrix-privileged.py     the one root-owned script the web UI is allowed to invoke via sudo
    airplaymatrix-webui.service, airplaymatrix-webui.sudoers
  app/
    main.py, app_controller.py, *_controller.py, qml/   the Qt6/PySide6 "Desk Display" kiosk app (Pi 5 / full build)
  app_qt5/
    main.py, app_controller.py, *_controller.py, qml/   the Qt5/PySide2 port of the same app (Pi Zero WH build) --
      settings_controller.py exposes display_settings.py's toggles to QML; lyrics/song-details visibility and
      LyricsController's fetching both gate on them
  labwc/
    autostart.example, rc.xml.snippet.txt   desktop autostart + Shift+X keybind for the kiosk app
  boot/
    airplaymatrix-boot-message.sh, .service    Zero WH kiosk only: blank-screen boot placeholder (tty1),
      replacing the Raspberry Pi splash + Plymouth's graphical theme -- see install-pi-zero-wh.md
    make_blank_cursor_theme.py    hand-built, fully-transparent Xcursor theme -- hides labwc's default
      pointer during the app-loading gap, no xcursorgen dependency

docs/
  install-full-pi.md       full desktop build (reference: this is what the running Pi 5 unit uses)
  install-pi-zero-wh.md    Pi Zero WH build, hardware caveats, headless fallback
```

## Hardware

- Raspberry Pi (see the two install docs for which model per build)
- ESP32 dev board running the HUB75 matrix firmware (separate PlatformIO
  project, `AirplayMatrix/src/main.cpp`, `esp32dev` env; not part of this
  repo), connected to the Pi over USB
- A 64x64 HUB75 LED panel, wired to the ESP32
- Audio out: a USB DAC or HDMI audio extractor -- neither Pi 5 nor Pi Zero
  WH has an analog 3.5mm jack (see the install docs for what's actually
  configured today)
- TV with HDMI-CEC support, for the TV power/remote features

## Status / known limitations

- The web UI is plain HTTP on the LAN only (no TLS) -- an accepted tradeoff
  for a home admin panel, not an oversight.
- Audio output is still on the HDMI-audio placeholder (`output_device =
  "default"` in shairport-sync.conf) on the reference unit; swap in a real
  DAC's ALSA device name once one is attached.
- The web UI's `/matrix` page (power/brightness/colour/mode) is UI-only --
  the ESP32 firmware is currently a passive image-frame sink with no command
  channel, so none of those controls are wired to the panel yet.
- `app_qt5/` (the Pi Zero WH kiosk build) has been run and tuned on real
  Zero WH hardware -- lyrics/song-details toggles, boot time (3min 45s down
  to ~2min 13s), idle RAM/swap pressure, and the boot splash/cursor are all
  covered in docs/install-pi-zero-wh.md, including the one thing that was
  deliberately *not* chased further (a NetworkManager/netplan reload cost)
  and why.

# AirplayMatrix

A Raspberry Pi that shows up as an AirPlay speaker, plays the audio out over
HDMI/analog, and pushes the currently-playing album art to a 64x64 HUB75 LED
matrix (driven by a companion ESP32 board over USB serial). It also turns
the TV on/off over HDMI-CEC when a session starts/stops, forwards TV-remote
button presses into AirPlay transport controls, and exposes a small
password-protected web UI for day-to-day settings.

Two build targets are documented here:

| Build | Hardware | What you get |
|---|---|---|
| [Full desktop build](docs/install-full-pi.md) | Pi 4/5-class board, screen attached | Everything below, plus the on-screen "Desk Display" kiosk app (album art, lyrics, now-playing) |
| [Pi Zero WH build](docs/install-pi-zero-wh.md) | Pi Zero WH (original, ARM11) | Everything below; the desk-display kiosk app is a stretch on this hardware -- see that doc for the risk and a headless fallback |

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
        │ app/  (full build)    │             │ (headless build)         │
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

Both the desk-display app and `matrix_daemon.py` are two independent
consumers of the same shairport-sync metadata pipe and the same
`encoder.py`/`matrix/link.py` code -- only one of them needs to run, and
they're never run together on the same box.

## Repository layout

```
Software/
  receiver/               (conceptual only -- shairport-sync itself is an apt package, not vendored here)
  shairport-sync.conf.example   AirPlay name, alsa output, metadata pipe, CEC session hooks
  metadata.py              shairport-sync metadata pipe parser + track state
  encoder.py                artwork downscale/JPEG-encode for the 64x64 panel
  lrclib.py                 lyrics lookup (used by the desk-display app only)
  receiver.py                cross-platform receiver process supervisor (Pi: no-op; Windows: WSL2)
  sps_bridge.py               re-serves the metadata pipe over TCP (Windows/WSL only)
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
    main.py, app_controller.py, *_controller.py, qml/   the Qt/QML "Desk Display" kiosk app (full build only)
  labwc/
    autostart.example, rc.xml.snippet.txt   desktop autostart + Shift+X keybind for the kiosk app

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

#!/usr/bin/env python3
"""
airplay-cec-remote.py — translate TV-remote button presses (received over
HDMI-CEC, via Remote Control Passthrough while this Pi is the active source)
into AirPlay playback commands, sent to shairport-sync's own D-Bus
RemoteControl interface (org.gnome.ShairportSync.RemoteControl), which in
turn relays them to the connected AirPlay client over DACP.

Mapping (this TV's remote sends plain D-pad codes, not dedicated media
keys -- confirmed by capturing real button presses with `cec-ctl --monitor`):
    OK / select (ui-cmd 0x00)  -> PlayPause
    right       (ui-cmd 0x04)  -> Next
    left        (ui-cmd 0x03)  -> Previous

Runs `cec-ctl --monitor` as a long-lived subprocess and parses its text
output rather than using the raw CEC ioctls directly, to stay consistent
with the rest of this project's use of cec-ctl for CEC access. Requires
CAP_NET_ADMIN on /usr/bin/cec-ctl (monitor mode needs it even for a device
we already own) -- see setcap in the systemd unit's provisioning notes.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys

LOG = logging.getLogger("airplay-cec-remote")

CEC_DEV = "/dev/cec0"

# Only react to messages addressed to us (Playback Device 1, i.e. "to 4").
# The sender is normally the TV, but any device on the bus routing us
# passthrough presses should be honoured.
PRESSED_RE = re.compile(r"^Received from .* to Playback Device 1 \(\d+ to 4\): USER_CONTROL_PRESSED")
RELEASED_RE = re.compile(r"^Received from .* to Playback Device 1 \(\d+ to 4\): USER_CONTROL_RELEASED")
UI_CMD_RE = re.compile(r"^\s*ui-cmd:\s*(\S+)")

UI_CMD_TO_DBUS_METHOD = {
    "select": "PlayPause",
    "right": "Next",
    "left": "Previous",
}

DBUS_DEST = "org.gnome.ShairportSync"
DBUS_PATH = "/org/gnome/ShairportSync"
DBUS_IFACE = "org.gnome.ShairportSync.RemoteControl"


def call_remote_control(method: str) -> None:
    LOG.info("invoking %s", method)
    try:
        subprocess.run(
            [
                "dbus-send", "--system", "--type=method_call",
                f"--dest={DBUS_DEST}", DBUS_PATH, f"{DBUS_IFACE}.{method}",
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        LOG.warning("%s failed: %s", method, exc.stderr.decode(errors="replace").strip())
    except subprocess.TimeoutExpired:
        LOG.warning("%s timed out", method)


def run() -> None:
    cmd = ["cec-ctl", "-d", CEC_DEV, "--monitor"]
    LOG.info("starting: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    # State machine: remember whether the button currently held down has
    # already fired its action, so a repeated PRESSED while a button stays
    # held (auto-repeat) doesn't fire Next/Previous/PlayPause over and over.
    # Reset on RELEASED.
    fired_for_current_press = False

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if RELEASED_RE.match(line):
            fired_for_current_press = False
            continue
        if PRESSED_RE.match(line):
            fired_for_current_press = False  # a new press header; ui-cmd follows
            continue
        m = UI_CMD_RE.match(line)
        if m and not fired_for_current_press:
            ui_cmd = m.group(1)
            method = UI_CMD_TO_DBUS_METHOD.get(ui_cmd)
            if method:
                call_remote_control(method)
                fired_for_current_press = True

    LOG.error("cec-ctl monitor process exited (code %s)", proc.wait())
    sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run()

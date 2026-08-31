"""
Shared helper for reading the AirPlay device name shairport-sync advertises.

Three places already need this exact "name = "..."" line out of
/etc/shairport-sync.conf: the web UI's settings page
(webui/app.py:read_shairport_settings), the HDMI-CEC OSD-name script
(cec/airplay-tv-power.sh:osd_name), and now both kiosk apps' idle screen
("Discoverable: <name>", see app/app_controller.py and
app_qt5/app_controller.py). This is the one the Qt apps import, read fresh
each poll rather than cached, so renaming the device from the web UI shows
up on the idle screen without restarting the kiosk app -- the same live-
reload the song-details/lyrics toggles already get (display_settings.py).

Read unprivileged: /etc/shairport-sync.conf is world-readable -- only
*writing* it (the web UI's device-name field) needs root, via the
privileged helper script.
"""

from __future__ import annotations

import re
from pathlib import Path

CONF_PATH = Path("/etc/shairport-sync.conf")
DEFAULT_NAME = "AirPlay"

_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"')


def read() -> str:
    """Current AirPlay device name, or DEFAULT_NAME if unset/unreadable."""
    try:
        text = CONF_PATH.read_text()
    except OSError:
        return DEFAULT_NAME
    m = _NAME_RE.search(text)
    name = m.group(1) if m else ""
    return name or DEFAULT_NAME


def mtime() -> float:
    """CONF_PATH's mtime, or -1.0 if it doesn't exist / isn't readable.

    Lets callers cheaply poll for a rename (a stat) without re-reading and
    re-parsing the file on every tick -- same trick as
    app_qt5/settings_controller.py uses for display_settings.json.
    """
    try:
        return CONF_PATH.stat().st_mtime
    except OSError:
        return -1.0

"""
Shared on-disk toggle state for the desk-display kiosk app, editable from
the web UI.

This exists for the Pi Zero WH's Qt5 build (app_qt5/) specifically: lyrics
fetching/rendering and the title/artist/album text block are each an
independent on/off switch here, off and on by default respectively, so the
web UI can be used to test whether the Zero's single ARM11 core can carry
live lyrics without needing SSH access or a reboot to try. The Pi 5's Qt6
app (app/) always shows both and doesn't consult those two toggles.

`lyrics_offset_seconds` is the exception -- both builds read it. It exists
because a receiver built with AirPlay 2 support (see docs/install-pi-zero-
wh.md's AirPlay-2 section) buffers audio well beyond classic AirPlay 1's
handful of hundred milliseconds, but shairport-sync's `prgr` metadata --
which TrackController.position() dead-reckons from -- reports the source's
stream position, not the receiver's buffered output position. The two drift
apart by however much the receiver is currently buffering, which lyrics
timing makes obvious in a way a plain progress bar doesn't. This constant
compensates: how many seconds *later* (positive) or *earlier* (negative)
the lyric line should switch relative to prgr's raw position. There's no
way to derive the right value from the protocol -- it's the receiver's
actual buffer depth, which isn't exposed -- so it's a knob to dial in by
ear/eye from the web UI, not something computed.

Both the kiosk app and the web UI run as the same unprivileged
`airplaymatrix` user, so this is a plain JSON file under ~/.config -- no
sudo/privileged-script plumbing needed, unlike the device-name/Wi-Fi/etc.
settings in webui/app.py which touch root-owned files.

The app polls this file's mtime (see app_qt5/settings_controller.py) rather
than reacting to a push, so a toggle flipped in the web UI takes effect
live, without restarting the kiosk app.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypedDict

LOG = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "airplaymatrix-display" / "config.json"

DEFAULTS = {
    "show_lyrics": False,  # off by default -- Zero WH performance is unproven
    "show_details": True,  # title/artist/album text block
    "lyrics_offset_seconds": 0.0,  # AirPlay-2 buffering compensation, see above
}

# Generous enough to cover any receiver's real buffer depth (AirPlay 2's is
# typically a couple of seconds) with room to spare, tight enough that a
# fat-fingered value in the web UI can't make lyrics silently useless for a
# whole track.
LYRICS_OFFSET_LIMIT_SECONDS = 10.0


class DisplaySettings(TypedDict):
    show_lyrics: bool
    show_details: bool
    lyrics_offset_seconds: float


def load() -> DisplaySettings:
    """Read the current settings, falling back to DEFAULTS for anything
    missing or if the file doesn't exist / is corrupt. Never raises."""
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return dict(DEFAULTS)  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("could not read %s (%s), using defaults", CONFIG_PATH, exc)
        return dict(DEFAULTS)  # type: ignore[return-value]
    try:
        offset = float(data.get("lyrics_offset_seconds", DEFAULTS["lyrics_offset_seconds"]))
    except (TypeError, ValueError):
        offset = DEFAULTS["lyrics_offset_seconds"]
    offset = max(-LYRICS_OFFSET_LIMIT_SECONDS, min(LYRICS_OFFSET_LIMIT_SECONDS, offset))
    return {
        "show_lyrics": bool(data.get("show_lyrics", DEFAULTS["show_lyrics"])),
        "show_details": bool(data.get("show_details", DEFAULTS["show_details"])),
        "lyrics_offset_seconds": offset,
    }


def save(settings: DisplaySettings) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a concurrent read (the app's poll timer) never
    # observes a half-written file.
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


def set_one(key: str, value: bool) -> DisplaySettings:
    if key not in DEFAULTS:
        raise ValueError(f"unknown display setting: {key!r}")
    settings = load()
    settings[key] = value  # type: ignore[literal-required]
    save(settings)
    return settings


def set_lyrics_offset(seconds: float) -> DisplaySettings:
    """Separate from set_one() -- this one's a clamped float, not a toggle."""
    seconds = max(-LYRICS_OFFSET_LIMIT_SECONDS, min(LYRICS_OFFSET_LIMIT_SECONDS, seconds))
    settings = load()
    settings["lyrics_offset_seconds"] = seconds
    save(settings)
    return settings

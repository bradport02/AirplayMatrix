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
ear/eye from the web UI, not something computed. Every change to it is
appended to OFFSET_HISTORY_PATH below, to tell a one-off dial-in apart from
actual drift.

`connect_volume_percent` is a second exception, and isn't read by either
kiosk app at all -- it's cec/airplay-tv-power.sh, run by shairport-sync as
the `shairport-sync` user on every new AirPlay connection, that reads it
(via CONFIG_PATH's literal string, not this module -- that script has no
Python/this package's sys.path, and Path.home() would resolve to the
*shairport-sync* user's home if it somehow did import this, not
airplaymatrix's). It lives here anyway rather than its own file because
the web UI already has a working read/write/live-reload story for this
exact JSON file; no reason to invent a second one for one integer.

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
from datetime import datetime
from pathlib import Path
from typing import TypedDict

LOG = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "airplaymatrix-display" / "config.json"

# Every actual change to lyrics_offset_seconds gets a line here, so whether
# it's stable or drifting over time can be checked later without having to
# reconstruct it from memory. A plain flat-file append rather than relying on
# journald: one of these Pis has already lost its persistent journal once to
# an unclean-shutdown ext4 corruption, and neither the web UI's route nor the
# kiosk app's poll loop otherwise records *when* or *by how much* this value
# has changed.
OFFSET_HISTORY_PATH = CONFIG_PATH.parent / "lyrics_offset_history.log"

# The only accepted values for transition_mode, shared by the web UI's
# validation and load()'s fallback below.
TRANSITION_MODES = ("fade", "crossfade")

# The only log levels the web UI offers. Deliberately not the full logging
# module set: anything above INFO would hide the session/track lines this
# project's own troubleshooting depends on.
LOG_LEVELS = ("INFO", "DEBUG")

# Bounds for crossfade_seconds. The floor is the app's own standard
# animation length -- going below it wouldn't be a crossfade so much as a
# cut, and the fade mode already covers "get on with it". The ceiling is
# generous on purpose: a very slow dissolve is a legitimate look on a
# display that sits on a shelf.
CROSSFADE_MIN_SECONDS = 0.4
CROSSFADE_MAX_SECONDS = 5.0


def _clamp_crossfade(value: object) -> float:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULTS["crossfade_seconds"]  # type: ignore[return-value]
    return max(CROSSFADE_MIN_SECONDS, min(CROSSFADE_MAX_SECONDS, seconds))

DEFAULTS = {
    "show_lyrics": False,  # off by default -- Zero WH performance is unproven
    "show_details": True,  # title/artist/album text block
    "lyrics_offset_seconds": 0.0,  # AirPlay-2 buffering compensation, see above
    "connect_volume_percent": 75,  # AirPlay volume set on every new connection, see above
    # Off by default, same reasoning as show_lyrics: an untested extra CPU/
    # audio-capture load on the Zero WH's single core shouldn't turn on by
    # itself. Read by MatrixController (app/ and app_qt5/) to decide whether
    # the 64x64 panel shows album artwork (default) or Software/matrix/
    # eq_meter.py's live band-level bars instead -- see that module's
    # docstring for where the audio actually comes from.
    "eq_meter_enabled": False,
    # The round scrub handle riding the end of the progress bar. Off by
    # default: the bar itself already shows position, and the dot is the
    # part of it that has to be redrawn at a new x every time position
    # updates. Kept as a toggle rather than deleted outright because it
    # is purely a look preference, not a correctness one.
    "progress_dot_enabled": False,
    # How the display moves from one track to the next. "fade" takes the
    # outgoing track out to the background and brings the new one up once
    # its artwork has decoded; "crossfade" dissolves the old artwork and
    # text straight into the new, never passing through an empty screen.
    # "fade" stays the default: it is the cheaper of the two on the Zero WH
    # (only ever one artwork decoded and composited at a time), and it is
    # the behaviour this build shipped with.
    # Silence the receiver at the start of a session until the metadata and
    # lyrics have landed, then restart the track so the two begin together.
    # See app_qt5/sync_controller.py. Off by default: it deliberately
    # delays the first few seconds of the first song, which is a trade
    # worth making only if you want it.
    "sync_on_connect": False,
    # Kiosk app log verbosity, settable from the web UI's Diagnostics page.
    # DEBUG turns on per-metadata-item and per-lyric-line tracing, which is
    # what makes a misbehaving track diagnosable without an SSH session --
    # and is far too noisy to leave on. Applied at app start, so it needs a
    # "Restart display app" to take effect.
    "log_level": "INFO",
    "transition_mode": "fade",
    # Length of each stage of a crossfade, in seconds. The default matches
    # Theme.durationSlow (0.4s), which is what every other animation in the
    # UI uses, so leaving it alone keeps transitions consistent with the
    # rest of the app. A track change runs two stages back to back (text
    # out, then artwork dissolve + text back in), so the whole transition
    # takes roughly twice this.
    "crossfade_seconds": 0.4,
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
    connect_volume_percent: int
    eq_meter_enabled: bool
    progress_dot_enabled: bool
    sync_on_connect: bool
    log_level: str
    transition_mode: str
    crossfade_seconds: float


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
    try:
        connect_volume = int(data.get("connect_volume_percent", DEFAULTS["connect_volume_percent"]))
    except (TypeError, ValueError):
        connect_volume = DEFAULTS["connect_volume_percent"]
    connect_volume = max(0, min(100, connect_volume))
    return {
        "show_lyrics": bool(data.get("show_lyrics", DEFAULTS["show_lyrics"])),
        "show_details": bool(data.get("show_details", DEFAULTS["show_details"])),
        "lyrics_offset_seconds": offset,
        "connect_volume_percent": connect_volume,
        "eq_meter_enabled": bool(data.get("eq_meter_enabled", DEFAULTS["eq_meter_enabled"])),
        "progress_dot_enabled": bool(
            data.get("progress_dot_enabled", DEFAULTS["progress_dot_enabled"])
        ),
        # Anything unrecognised falls back to the default rather than being
        # passed through to QML, which would otherwise have to defend
        # against it -- this file is hand-editable.
        "sync_on_connect": bool(data.get("sync_on_connect", DEFAULTS["sync_on_connect"])),
        "log_level": (
            data.get("log_level")
            if data.get("log_level") in LOG_LEVELS
            else DEFAULTS["log_level"]
        ),
        "crossfade_seconds": _clamp_crossfade(data.get("crossfade_seconds")),
        "transition_mode": (
            data.get("transition_mode")
            if data.get("transition_mode") in TRANSITION_MODES
            else DEFAULTS["transition_mode"]
        ),
    }


def save(settings: DisplaySettings) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a concurrent read (the app's poll timer) never
    # observes a half-written file.
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


def set_crossfade_seconds(seconds: float) -> DisplaySettings:
    """Separate from set_one() -- a clamped float, not a toggle."""
    settings = load()
    settings["crossfade_seconds"] = _clamp_crossfade(seconds)
    save(settings)
    return settings


def set_log_level(level: str) -> DisplaySettings:
    if level not in LOG_LEVELS:
        raise ValueError(f"unknown log level: {level!r}")
    settings = load()
    settings["log_level"] = level
    save(settings)
    return settings


def set_transition_mode(mode: str) -> DisplaySettings:
    """Separate from set_one() -- this one's an enum, not a toggle."""
    if mode not in TRANSITION_MODES:
        raise ValueError(f"unknown transition mode: {mode!r}")
    settings = load()
    settings["transition_mode"] = mode
    save(settings)
    return settings


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
    previous = settings["lyrics_offset_seconds"]
    settings["lyrics_offset_seconds"] = seconds
    save(settings)
    if seconds != previous:
        _log_offset_change(previous, seconds)
    return settings


def _log_offset_change(previous: float, new: float) -> None:
    """Best-effort: a failure to write the history line shouldn't undo the
    setting change above, which has already been saved by the time this
    runs."""
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  {previous:+.2f} -> {new:+.2f}\n"
    try:
        with OFFSET_HISTORY_PATH.open("a") as f:
            f.write(line)
    except OSError as exc:
        LOG.warning("could not append to %s (%s)", OFFSET_HISTORY_PATH, exc)


def set_connect_volume_percent(percent: int) -> DisplaySettings:
    """Separate from set_one() -- this one's a clamped int, not a toggle."""
    percent = max(0, min(100, percent))
    settings = load()
    settings["connect_volume_percent"] = percent
    save(settings)
    return settings

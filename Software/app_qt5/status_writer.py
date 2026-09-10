"""
Publishes what's on the kiosk screen to a file the web UI can read.

The web UI cannot ask shairport-sync what's playing: the metadata FIFO has a
single reader and the kiosk app is it. Rather than add a second transport,
the app writes what it is already displaying to a small JSON file whenever
that changes, and the web UI reads it. See Software/runtime_state.py for the
location and the write-then-rename that keeps a reader from seeing half a
file.

Written on change only -- track publishes, session and playback transitions
-- never on a timer. Playback position is deliberately *not* included: it
changes four times a second, and writing the file that often to an SD card
for a "now playing" panel nobody is watching most of the time would be a
poor trade. The web UI shows what the track is, not a live scrub bar.

The artwork thumbnail reuses encoder.encode(), which already produces the
64x64 baseline JPEG the LED matrix is fed. It is about a kilobyte base64'd,
costs nothing extra to make, and is the right size for a web thumbnail --
so no second image path exists to go wrong.
"""

from __future__ import annotations

import logging
import time

from PySide2.QtCore import QObject

import runtime_state
from encoder import encode

from .track_controller import TrackController

LOG = logging.getLogger(__name__)


class StatusWriter(QObject):
    def __init__(self, track: TrackController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._track = track
        self._artwork_revision: int | None = None
        self._thumbnail = ""

        for signal in (
            track.titleChanged,
            track.artistChanged,
            track.albumChanged,
            track.artworkChanged,
            track.playingChanged,
            track.sessionActiveChanged,
            track.contentReadyChanged,
            track.clientNameChanged,
            track.clientConnectedChanged,
        ):
            signal.connect(self._write)

        self._write()

    def _write(self) -> None:
        try:
            runtime_state.write_json(
                runtime_state.NOW_PLAYING_PATH,
                {
                    "updated_at": time.time(),
                    "session_active": self._track.sessionActive,
                    "client_connected": self._track.clientConnected,
                    "client_name": self._track.clientName,
                    "content_ready": self._track.contentReady,
                    "playing": self._track.playing,
                    "title": self._track.title,
                    "artist": self._track.artist,
                    "album": self._track.album,
                    "duration": self._track.duration,
                    "artwork_b64": self._current_thumbnail(),
                },
            )
        except OSError as exc:
            # Never let a status file take the display down with it.
            LOG.debug("could not write now-playing state: %s", exc)

    def _current_thumbnail(self) -> str:
        """64x64 JPEG of the current artwork, base64'd, cached by revision.

        Without the cache this would re-encode on every signal above --
        several times per track change, for an image that only changes when
        the artwork does.
        """
        data = self._track.artwork_bytes()
        if not data:
            self._artwork_revision = None
            self._thumbnail = ""
            return ""
        revision = len(data)  # cheap proxy; artwork bytes change wholesale
        if revision != self._artwork_revision:
            encoded = encode(data)
            self._thumbnail = encoded.b64 if encoded is not None else ""
            self._artwork_revision = revision
        return self._thumbnail

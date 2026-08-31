"""
Live-reloading lyrics-offset setting, exposed to QML.

Unlike app_qt5/settings_controller.py (which this mirrors), this build has
no lyrics/song-details toggles to expose -- app/ always shows both, see
display_settings.py's docstring. lyrics_offset_seconds is the one field
both builds read, so this controller exists just for that: a receiver
built with AirPlay 2 support buffers audio well beyond classic AirPlay 1,
and that buffering isn't reflected in shairport-sync's `prgr` metadata that
TrackController.position() dead-reckons from, so lyrics land early by
however much the receiver is currently buffering. This is the compensation
knob, set from the web UI.

A plain mtime poll rather than QFileSystemWatcher: the web UI writes via
write-to-tmp-then-rename (see display_settings.save), and
QFileSystemWatcher on Linux frequently stops tracking a path across a
rename-replace of the file it points at -- the watch would silently go
dead after the first edit. Polling every POLL_MS sidesteps that entirely.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QObject, QTimer, Signal

import display_settings

LOG = logging.getLogger(__name__)

POLL_MS = 2000


class SettingsController(QObject):
    lyricsOffsetSecondsChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = display_settings.load()
        self._mtime = self._current_mtime()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @staticmethod
    def _current_mtime() -> float:
        try:
            return display_settings.CONFIG_PATH.stat().st_mtime
        except OSError:
            return -1.0

    def _poll(self) -> None:
        mtime = self._current_mtime()
        if mtime == self._mtime:
            return
        self._mtime = mtime
        new = display_settings.load()
        old, self._settings = self._settings, new
        if old["lyrics_offset_seconds"] != new["lyrics_offset_seconds"]:
            LOG.info("lyrics_offset_seconds -> %s", new["lyrics_offset_seconds"])
            self.lyricsOffsetSecondsChanged.emit()

    def _get_lyrics_offset_seconds(self) -> float:
        return self._settings["lyrics_offset_seconds"]

    lyricsOffsetSeconds = Property(float, _get_lyrics_offset_seconds, notify=lyricsOffsetSecondsChanged)

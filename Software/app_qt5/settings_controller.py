"""
Live-reloading display toggles, exposed to QML.

Wraps display_settings.py (Software/display_settings.py, shared with the web
UI). A plain mtime poll rather than QFileSystemWatcher: the web UI writes
via write-to-tmp-then-rename (see display_settings.save), and
QFileSystemWatcher on Linux frequently stops tracking a path across a
rename-replace of the file it points at -- the watch would silently go dead
after the first edit. Polling every POLL_MS sidesteps that entirely, and at
this interval it's not meaningful CPU cost next to the rest of the app.
"""

from __future__ import annotations

import logging

from PySide2.QtCore import Property, QObject, QTimer, Signal

import display_settings

LOG = logging.getLogger(__name__)

POLL_MS = 2000


class SettingsController(QObject):
    showLyricsChanged = Signal()
    showDetailsChanged = Signal()

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
        if old["show_lyrics"] != new["show_lyrics"]:
            LOG.info("show_lyrics -> %s", new["show_lyrics"])
            self.showLyricsChanged.emit()
        if old["show_details"] != new["show_details"]:
            LOG.info("show_details -> %s", new["show_details"])
            self.showDetailsChanged.emit()

    def _get_show_lyrics(self) -> bool:
        return self._settings["show_lyrics"]

    def _get_show_details(self) -> bool:
        return self._settings["show_details"]

    showLyrics = Property(bool, _get_show_lyrics, notify=showLyricsChanged)
    showDetails = Property(bool, _get_show_details, notify=showDetailsChanged)

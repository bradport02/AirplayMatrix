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
    lyricsOffsetSecondsChanged = Signal()
    eqMeterEnabledChanged = Signal()
    progressDotEnabledChanged = Signal()
    transitionModeChanged = Signal()
    crossfadeSecondsChanged = Signal()
    syncOnConnectChanged = Signal()

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
        if old["lyrics_offset_seconds"] != new["lyrics_offset_seconds"]:
            LOG.info("lyrics_offset_seconds -> %s", new["lyrics_offset_seconds"])
            self.lyricsOffsetSecondsChanged.emit()
        if old["eq_meter_enabled"] != new["eq_meter_enabled"]:
            LOG.info("eq_meter_enabled -> %s", new["eq_meter_enabled"])
            self.eqMeterEnabledChanged.emit()
        if old["progress_dot_enabled"] != new["progress_dot_enabled"]:
            LOG.info("progress_dot_enabled -> %s", new["progress_dot_enabled"])
            self.progressDotEnabledChanged.emit()
        if old["transition_mode"] != new["transition_mode"]:
            LOG.info("transition_mode -> %s", new["transition_mode"])
            self.transitionModeChanged.emit()
        if old["sync_on_connect"] != new["sync_on_connect"]:
            LOG.info("sync_on_connect -> %s", new["sync_on_connect"])
            self.syncOnConnectChanged.emit()
        if old["crossfade_seconds"] != new["crossfade_seconds"]:
            LOG.info("crossfade_seconds -> %s", new["crossfade_seconds"])
            self.crossfadeSecondsChanged.emit()

    def _get_show_lyrics(self) -> bool:
        return self._settings["show_lyrics"]

    def _get_show_details(self) -> bool:
        return self._settings["show_details"]

    def _get_lyrics_offset_seconds(self) -> float:
        return self._settings["lyrics_offset_seconds"]

    def _get_eq_meter_enabled(self) -> bool:
        return self._settings["eq_meter_enabled"]

    def _get_progress_dot_enabled(self) -> bool:
        return self._settings["progress_dot_enabled"]

    def _get_transition_mode(self) -> str:
        return self._settings["transition_mode"]

    def _get_sync_on_connect(self) -> bool:
        return self._settings["sync_on_connect"]

    def _get_crossfade_ms(self) -> int:
        # Handed to QML in milliseconds, which is what every QML animation
        # duration is expressed in -- saves each call site converting.
        return int(round(self._settings["crossfade_seconds"] * 1000))

    showLyrics = Property(bool, _get_show_lyrics, notify=showLyricsChanged)
    showDetails = Property(bool, _get_show_details, notify=showDetailsChanged)
    lyricsOffsetSeconds = Property(float, _get_lyrics_offset_seconds, notify=lyricsOffsetSecondsChanged)
    # Not read by app_qt5/qml either -- see app/settings_controller.py's
    # matching property, which this mirrors. Only MatrixController consumes
    # it (Software/matrix/eq_meter.py has the actual capture/render logic).
    eqMeterEnabled = Property(bool, _get_eq_meter_enabled, notify=eqMeterEnabledChanged)
    # Read by PlaybackBar.qml. Unlike eqMeterEnabled above, this one
    # genuinely is a QML-side property -- it just shows/hides an element.
    progressDotEnabled = Property(
        bool, _get_progress_dot_enabled, notify=progressDotEnabledChanged
    )
    # "fade" or "crossfade" -- read by NowPlayingView/AlbumArt/Background to
    # pick how a track change is animated. See display_settings.py.
    transitionMode = Property(str, _get_transition_mode, notify=transitionModeChanged)
    crossfadeMs = Property(int, _get_crossfade_ms, notify=crossfadeSecondsChanged)
    # Read by SyncController, not by QML.
    syncOnConnect = Property(bool, _get_sync_on_connect, notify=syncOnConnectChanged)

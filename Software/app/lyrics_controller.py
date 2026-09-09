"""
Time-synced lyrics exposed to QML.

Wraps LyricsFetcher (lrclib.py). Field changes on TrackController arrive one
at a time as separate metadata items (title, then artist, then album, then
duration, in no guaranteed order), so requests are debounced rather than
fired on every field change -- otherwise a request could fire before
`duration` has arrived, and since LyricsFetcher dedupes purely on the
(artist, title, album) key, a later duration-only update would never
retrigger it, silently losing the duration disambiguation between radio
edits, remasters and live versions.

Lyric-line lookup is pull-based (`lineAt`/`nextLineAt`, called from QML
alongside `TrackController.currentPosition()`) rather than driven by a
second internal poll timer. Positions are shifted by
`settings.lyricsOffsetSeconds` before lookup -- see
Software/display_settings.py's docstring for why that exists (an AirPlay
2-capable receiver's output buffering vs. shairport-sync's `prgr` metadata
reporting stream position, not audible position).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from lrclib import LyricsFetcher, SyncedLyrics

from .settings_controller import SettingsController
from .track_controller import TrackController

LOG = logging.getLogger(__name__)

DEBOUNCE_MS = 300


class LyricsController(QObject):
    resultReady = Signal(object, object)  # (key, SyncedLyrics | None) -- relay onto GUI thread
    linesChanged = Signal()

    def __init__(
        self,
        track: TrackController,
        settings: SettingsController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._track = track
        self._settings = settings
        self._lyrics: Optional[SyncedLyrics] = None
        self._lock = threading.Lock()

        self._fetcher = LyricsFetcher(self._on_result)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fetch_now)

        self.resultReady.connect(self._apply_result)

        track.titleChanged.connect(self._on_identity_changed)
        track.artistChanged.connect(self._on_identity_changed)
        track.albumChanged.connect(self._on_identity_changed)
        track.durationChanged.connect(self._schedule_fetch)

    def _on_identity_changed(self) -> None:
        # Clear immediately rather than waiting for the debounced re-fetch
        # below to resolve: title/artist/album update one at a time as
        # separate metadata items arrive, and TrackController's position
        # has already reset for the new track by the time any of them
        # changes. Without this, the previous track's (still-valid)
        # SyncedLyrics keeps getting matched against the new position for
        # as long as the fetch takes, showing a stale/wrong line instead of
        # nothing. Duration alone changing isn't wired here -- it can
        # refine mid-track (prgr's estimate vs a later astm) without the
        # song actually changing, so clearing on it too would flash away
        # perfectly valid, already-displaying lyrics.
        with self._lock:
            had_lyrics = self._lyrics is not None
            self._lyrics = None
        if had_lyrics:
            self.linesChanged.emit()
        self._schedule_fetch()

    def _schedule_fetch(self) -> None:
        self._debounce.start()  # restarts the countdown if already pending

    def _fetch_now(self) -> None:
        artist, title, album = self._track.artist, self._track.title, self._track.album
        if not artist or not title:
            with self._lock:
                self._lyrics = None
            self.linesChanged.emit()
            return
        self._fetcher.request((artist, title, album), self._track.duration)

    def _on_result(self, key: tuple[str, str, str], lyrics: Optional[SyncedLyrics]) -> None:
        # Called off-thread by LyricsFetcher's worker; emit is safe from any
        # thread, delivery to _apply_result is auto-queued onto the GUI thread.
        self.resultReady.emit(key, lyrics)

    def _apply_result(self, key: tuple[str, str, str], lyrics: Optional[SyncedLyrics]) -> None:
        LOG.debug("lyrics %s for %r", "found" if lyrics else "not found", key)
        with self._lock:
            self._lyrics = lyrics
        self.linesChanged.emit()

    def _get_has_lyrics(self) -> bool:
        with self._lock:
            return bool(self._lyrics)

    hasLyrics = Property(bool, _get_has_lyrics, notify=linesChanged)

    def _adjusted(self, position: float) -> float:
        # Positive lyrics_offset_seconds means "the receiver's buffering
        # delays audible playback behind prgr's reported position" -- so the
        # lyric line should switch later, which means looking up an
        # *earlier* point on the (unshifted) synced-lyrics timeline.
        return position - self._settings.lyricsOffsetSeconds

    # Assumed length (seconds) of the currently active line when it's the
    # last line LRCLIB gave us -- there's no next timestamp to measure a real
    # span against, so the word-wipe below just has to pick something rather
    # than divide by zero or refuse to animate the last line at all.
    _FALLBACK_LINE_SPAN = 4.0

    @Slot(float, result=float)
    def currentLineProgress(self, position: float) -> float:
        """Fraction (0..1) of the way through the active line's time window,
        for LyricsPanel.qml's word-by-word "karaoke" fill.

        LRCLIB only carries line-level timestamps -- unlike Apple Music's own
        catalog, which uses word/syllable-level TTML data for the real thing
        (see the karaoke-style research behind this feature). There is no
        per-word timing to read here, so this hands QML a single linear
        progress value across the whole line, and LyricsPanel.qml's
        wordThresholds() divides that up between words by character count.
        It's an estimate, not a transcript of when each word was actually
        sung, but it tracks the line's real start/end times, so it can't
        drift the way a fixed per-word duration would on a fast-sung line.
        """
        with self._lock:
            lyrics = self._lyrics
        if not lyrics:
            return 0.0
        pos = self._adjusted(position)
        idx = lyrics.index_at(pos)
        if idx < 0:
            return 0.0
        start = lyrics.lines[idx].time
        end = lyrics.lines[idx + 1].time if idx + 1 < len(lyrics.lines) else start + self._FALLBACK_LINE_SPAN
        span = end - start
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (pos - start) / span))

    @Slot(float, result=str)
    def lineAt(self, position: float) -> str:
        with self._lock:
            lyrics = self._lyrics
        if not lyrics:
            return ""
        line = lyrics.line_at(self._adjusted(position))
        return line.text if line else ""

    @Slot(float, result=str)
    def nextLineAt(self, position: float) -> str:
        with self._lock:
            lyrics = self._lyrics
        if not lyrics:
            return ""
        idx = lyrics.index_at(self._adjusted(position)) + 1
        return lyrics.lines[idx].text if 0 <= idx < len(lyrics.lines) else ""

    @Slot(float, result=str)
    def previousLineAt(self, position: float) -> str:
        with self._lock:
            lyrics = self._lyrics
        if not lyrics:
            return ""
        idx = lyrics.index_at(self._adjusted(position)) - 1
        return lyrics.lines[idx].text if 0 <= idx < len(lyrics.lines) else ""

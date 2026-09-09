"""
Time-synced lyrics exposed to QML.

PySide2 port of app/lyrics_controller.py -- see that file's docstring for
the debounce/pull-based-lookup rationale, which is unchanged here. The one
real difference from the Qt6 version: this is the feature the Pi Zero WH
port exists to make optional (see Software/display_settings.py). When
`settings.showLyrics` is off, `_fetch_now` returns before ever calling
LyricsFetcher.request -- no lrclib.org network request, no synced-lyrics
parsing, and (via QML gating on the same property in NowPlayingView.qml) no
per-tick lineAt()/nextLineAt() calls or scrolling animation either. That's
the whole point of the toggle: a clean, complete off, not just a hidden
panel still doing the work behind it.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from PySide2.QtCore import Property, QObject, QTimer, Signal, Slot

from lrclib import LyricLine, LyricsFetcher, SyncedLyrics

from .settings_controller import SettingsController
from .track_controller import TrackController

LOG = logging.getLogger(__name__)

DEBOUNCE_MS = 300

# End-of-song credits, appended to the synced-lyrics timeline so they scroll
# through the existing panel exactly like lyric lines -- no separate UI, no
# separate timer, and the lyrics offset applies to them for free.
#
# The credit text comes from the AirPlay stream's DAAP "composer" field
# (metadata.py's "ascp"), which Apple fills with songwriting credits.
# LRCLIB has no credits of any kind -- its API returns only the lyrics plus
# title/artist/album/duration -- so this is the only source available
# without introducing a second lookup against something like MusicBrainz.
CREDITS_MIN_GAP_SECONDS = 1.2  # after the final sung lyric, before the block
CREDITS_LEAD_SECONDS = 20.0  # how close to the end of the track to aim for
MAX_CREDIT_NAMES = 6  # a long writing team shouldn't crowd out the song

# Apple joins names with commas and a trailing ampersand: "A, B & C".
_CREDIT_SPLIT_RE = re.compile(r"\s*(?:,|&|;| and )\s*")


class LyricsController(QObject):
    resultReady = Signal(object, object)  # (key, SyncedLyrics | None) -- relay onto GUI thread
    linesChanged = Signal()
    searchedChanged = Signal()

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
        # False means "we haven't got an answer for the current track yet",
        # which is a different thing from "we looked and there aren't any" --
        # hasLyrics alone can't tell those apart, and the panel was showing
        # its "No lyrics found" message during the gap before the lookup came
        # back. Only a real result (found or genuinely absent) sets this.
        self._searched = False
        # Timeline position (in lyrics time, so before the offset is
        # applied) at which the credits block starts, or None when this
        # track has no credits. Lets the panel style them differently
        # without having to know which individual lines are credits.
        self._credits_start: Optional[float] = None
        # Index of the line lineAt() last handed to the panel. Only used to
        # log transitions, so a whole song produces a few dozen lines rather
        # than four a second -- enough to see whether lyrics are being
        # selected at all, and whether they track the music.
        self._last_line_index = -2
        self._lock = threading.Lock()

        self._fetcher = LyricsFetcher(self._on_result)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fetch_now)

        self.resultReady.connect(self._apply_result)

        # liveIdentityChanged, not titleChanged/artistChanged/albumChanged:
        # those now fire only once the new track is published to the display
        # (behind the track-change fade, see TrackController._publish), and a
        # lyrics lookup is a network round trip that wants the extra head
        # start. durationChanged is folded into the same signal for the same
        # reason.
        track.liveIdentityChanged.connect(self._on_identity_changed)
        # Duration only re-runs the lookup (a better duration can pick a
        # better LRCLIB match); it must NOT clear what's on screen, which is
        # what _on_identity_changed does. See TrackController's comment.
        track.liveDurationChanged.connect(self._schedule_fetch)
        # Off->on: pick up lyrics for whatever's already playing right away
        # rather than waiting for the next metadata item (which may be
        # minutes away, or never, if nothing about the track changes again).
        # On->off: no fetch needed, _fetch_now's own guard below is what
        # actually stops the work; nothing to do here.
        settings.showLyricsChanged.connect(self._on_toggle_changed)

    def _on_toggle_changed(self) -> None:
        if self._settings.showLyrics:
            # Same reasoning as _on_identity_changed: the panel is about to
            # be shown again with a fetch still in flight, so don't let it
            # inherit a stale "searched" from before it was switched off.
            self._set_searched(False)
            self._schedule_fetch()

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
            self._credits_start = None
        if had_lyrics:
            self.linesChanged.emit()
        # New track identity -- whatever we knew about the last one says
        # nothing about this one, so go back to "no answer yet" until the
        # fetch below actually resolves.
        self._set_searched(False)
        self._schedule_fetch()

    def _set_searched(self, value: bool) -> None:
        with self._lock:
            if self._searched == value:
                return
            self._searched = value
        self.searchedChanged.emit()

    def _schedule_fetch(self) -> None:
        self._debounce.start()  # restarts the countdown if already pending

    def _fetch_now(self) -> None:
        if not self._settings.showLyrics:
            return
        artist, title, album = self._track.live_identity()
        if not artist or not title:
            with self._lock:
                self._lyrics = None
                self._credits_start = None
            self.linesChanged.emit()
            return
        self._fetcher.request((artist, title, album), self._track.live_duration())

    def _on_result(self, key: tuple[str, str, str], lyrics: Optional[SyncedLyrics]) -> None:
        # Called off-thread by LyricsFetcher's worker; emit is safe from any
        # thread, delivery to _apply_result is auto-queued onto the GUI thread.
        self.resultReady.emit(key, lyrics)

    def _apply_result(self, key: tuple[str, str, str], lyrics: Optional[SyncedLyrics]) -> None:
        if lyrics and lyrics.lines:
            LOG.debug(
                "lyrics found for %r -- %d lines, %.1fs to %.1fs (track %.1fs)",
                key, len(lyrics.lines), lyrics.lines[0].time, lyrics.lines[-1].time,
                self._track.live_duration(),
            )
        else:
            LOG.debug("lyrics not found for %r", key)
        self._last_line_index = -2
        credits_start = None
        if lyrics:
            lyrics, credits_start = self._with_credits(lyrics)
        with self._lock:
            self._lyrics = lyrics
            self._credits_start = credits_start
        self.linesChanged.emit()
        # A result arrived either way -- lyrics or a definitive "none" -- so
        # the panel is now entitled to say so.
        self._set_searched(True)

    def _with_credits(self, lyrics: SyncedLyrics) -> tuple[SyncedLyrics, Optional[float]]:
        """Return `lyrics` plus end-of-song credit lines, and the time the
        credits start (None if none were added).

        Returns the original object unchanged whenever credits don't apply,
        so a track with no composer data simply behaves as it always did.
        """
        composer = self._track.live_composer().strip()
        if not composer or not lyrics.lines:
            # Silent until now, which made "no credits on screen" impossible
            # to tell apart from a bug. The sender simply doesn't always
            # populate the composer field -- it varies by source app and
            # even by track.
            LOG.debug("credits skipped: sender sent no composer for this track")
            return lyrics, None

        names = [n for n in _CREDIT_SPLIT_RE.split(composer) if n.strip()]
        if not names:
            return lyrics, None
        names = names[:MAX_CREDIT_NAMES]

        # The last line with actual words in it, not simply the last line:
        # LRC files conventionally end with a bare timestamp and no text
        # (e.g. "[07:39.80]") marking where the final lyric stops singing.
        sung = [ln for ln in lyrics.lines if ln.text.strip()]
        if not sung:
            return lyrics, None
        last_line_time = sung[-1].time

        # One timed line, and one *visual* line: names comma-separated after
        # the label rather than stacked. Credits aren't sung, so scrolling
        # them through the window a name at a time read as lyrics, and the
        # earlier entries had to start before the song's real last lyric to
        # fit -- which cut those lyrics off. As a single line it sits
        # strictly *after* the last lyric and simply stays there.
        #
        # No role suffix: the only credit the sender gives us is the DAAP
        # composer field, so every name would carry the same label, which
        # adds length without adding information.
        text = "Credits: " + ", ".join(n.strip() for n in names)

        duration = self._track.live_duration()
        earliest = last_line_time + CREDITS_MIN_GAP_SECONDS
        if duration > 0:
            # Prefer the end of the track, but never before the lyrics have
            # finished -- a song with a long instrumental outro shouldn't
            # show its credits over the last verse.
            start = max(earliest, duration - CREDITS_LEAD_SECONDS)
            if start >= duration:
                LOG.debug(
                    "credits skipped: lyrics run to the end (duration %.1fs, last lyric %.1fs)",
                    duration, last_line_time,
                )
                return lyrics, None
        else:
            start = earliest

        lines = list(lyrics.lines)
        lines.append(LyricLine(start, text))
        LOG.debug(
            "credits: %d name(s) from %r at %.1fs (last lyric %.1fs, track %.1fs)",
            len(names), composer, start, last_line_time, duration,
        )
        return SyncedLyrics(lines, offset=lyrics.offset), start

    def _get_has_lyrics(self) -> bool:
        with self._lock:
            return bool(self._lyrics)

    def _get_searched(self) -> bool:
        with self._lock:
            return self._searched

    hasLyrics = Property(bool, _get_has_lyrics, notify=linesChanged)
    # Gates LyricsPanel.qml's "No lyrics found" message so it appears only
    # once a lookup has actually come back empty, not during the fetch.
    searched = Property(bool, _get_searched, notify=searchedChanged)

    def _adjusted(self, position: float) -> float:
        # Positive lyrics_offset_seconds means "the receiver's buffering
        # delays audible playback behind prgr's reported position" -- so the
        # lyric line should switch later, which means looking up an
        # *earlier* point on the (unshifted) synced-lyrics timeline. See
        # display_settings.py's docstring.
        return position - self._settings.lyricsOffsetSeconds

    @Slot(float, result=bool)
    def creditsActive(self, position: float) -> bool:
        """Whether playback has reached the end-of-song credits.

        The panel styles the whole three-line window differently while this
        is true -- credits are a block to be read at a glance, not lyrics
        being sung one line at a time.
        """
        with self._lock:
            start = self._credits_start
        return start is not None and self._adjusted(position) >= start

    @Slot(float, result=str)
    def lineAt(self, position: float) -> str:
        with self._lock:
            lyrics = self._lyrics
        if not lyrics:
            if self._last_line_index != -2:
                self._last_line_index = -2
                LOG.debug("lineAt: no lyrics loaded")
            return ""
        adjusted = self._adjusted(position)
        index = lyrics.index_at(adjusted)
        if index != self._last_line_index:
            self._last_line_index = index
            if index < 0:
                LOG.debug(
                    "lineAt: before first line (pos %.1fs, adjusted %.1fs, first line at %.1fs)",
                    position, adjusted, lyrics.lines[0].time if lyrics.lines else -1.0,
                )
            else:
                LOG.debug(
                    "lineAt: line %d/%d at %.1fs (pos %.1fs, adjusted %.1fs) %r",
                    index + 1, len(lyrics.lines), lyrics.lines[index].time,
                    position, adjusted, lyrics.lines[index].text[:40],
                )
        line = lyrics.lines[index] if index >= 0 else None
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

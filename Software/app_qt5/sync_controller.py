"""
Mute-and-restart on first connection, so lyrics start in step.

The problem it solves: AirPlay hands over the audio stream before it hands
over the metadata. By the time the title arrives, LRCLIB has been queried and
synced lyrics have come back, the song is already several seconds in -- and
because those seconds elapsed before we knew anything, there is no way to
work out where in the track we actually are beyond shairport-sync's own
"prgr" reports, which are themselves relative to a stream that started
before we were looking. Lyrics can end up meaningfully out of step for the
whole of the first track of a session.

The trick here, when display_settings.sync_on_connect is on: silence the
receiver as soon as a session opens, wait for the metadata (and the lyrics)
to land, then ask the sender to seek back to the start of the track and
unmute. The listener hears the song begin from the top, a moment late, with
the lyrics already loaded and correctly anchored.

Deliberately limited to the *first* track of a session. Later track changes
already have all the metadata machinery warmed up and are not worth
interrupting -- and silently restarting a track someone is enjoying would be
a far worse bug than a slightly late lyric.

Three things about this are worth knowing before changing it:

  * Volume is moved through shairport-sync's own D-Bus Volume property, in
    dB, exactly as Software/cec/airplay-tv-power.sh does for the connect
    volume -- so muting and restoring speak the same language as the rest
    of the project rather than reaching for an ALSA mixer that this Pi's
    HDMI output may not even expose.
  * Muting waits a moment after the session opens, because shairport-sync's
    own `run_this_before_entering_active_state` hook sets the connect volume
    at that instant. Muting first would simply be overwritten.
  * There is an unconditional deadline. Every path that mutes also arms a
    timer that restores the volume no matter what else happens, because the
    worst possible outcome here is a receiver that is silently muted and
    gives the user no clue why.
"""

from __future__ import annotations

import logging
import subprocess

from PySide2.QtCore import QObject, QTimer

import display_settings

from .lyrics_controller import LyricsController
from .settings_controller import SettingsController
from .track_controller import TrackController

LOG = logging.getLogger(__name__)

# Let shairport-sync's own connect-volume hook land before muting over it.
MUTE_DELAY_MS = 400

# How long to wait for metadata/lyrics before giving up and just playing the
# song. Generous, because the whole point is to wait -- but bounded, because
# a track whose lyrics never resolve must not stay silent.
READY_TIMEOUT_MS = 10_000

# Gap between asking the sender to seek back and restoring the volume, so
# the seek has taken effect before sound returns and the listener doesn't
# hear a slice of the wrong part of the song.
UNMUTE_DELAY_MS = 700

# Absolute backstop: whatever else happens, the volume is restored this long
# after muting.
DEADLINE_MS = 20_000

# Seek offset in microseconds. MPRIS Seek is relative and clamps at the
# start of the track, so any value larger than a plausible song works as
# "go back to the beginning".
SEEK_TO_START_US = -600_000_000

DBUS_DEST = "org.gnome.ShairportSync"
DBUS_PATH = "/org/gnome/ShairportSync"
MPRIS_DEST = "org.mpris.MediaPlayer2.ShairportSync"
MPRIS_PATH = "/org/mpris/MediaPlayer2"

# shairport-sync's AirPlay volume range, matching airplay-tv-power.sh.
VOLUME_MIN_DB = -30.0


class SyncController(QObject):
    def __init__(
        self,
        track: TrackController,
        lyrics: LyricsController,
        settings: SettingsController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._track = track
        self._lyrics = lyrics
        self._settings = settings

        self._muted = False
        self._done_for_session = False

        self._mute_timer = self._one_shot(MUTE_DELAY_MS, self._mute)
        self._ready_timer = self._one_shot(READY_TIMEOUT_MS, self._on_ready_timeout)
        self._unmute_timer = self._one_shot(UNMUTE_DELAY_MS, self._restore_volume)
        self._deadline_timer = self._one_shot(DEADLINE_MS, self._on_deadline)

        track.sessionActiveChanged.connect(self._on_session_changed)
        track.contentReadyChanged.connect(self._check_ready)
        lyrics.searchedChanged.connect(self._check_ready)

    def _one_shot(self, interval_ms: int, slot) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval_ms)
        timer.timeout.connect(slot)
        return timer

    # -- session lifecycle --

    def _on_session_changed(self) -> None:
        if self._track.sessionActive:
            self._on_session_started()
        else:
            self._on_session_ended()

    def _on_session_started(self) -> None:
        if self._done_for_session or not self._settings.syncOnConnect:
            return
        self._done_for_session = True  # first track of this session only
        LOG.info("sync-on-connect: muting until this track's metadata lands")
        self._mute_timer.start()
        self._deadline_timer.start()

    def _on_session_ended(self) -> None:
        # Restore before forgetting, or a session that ends mid-wait leaves
        # the receiver muted for the next one.
        if self._muted:
            LOG.info("sync-on-connect: session ended while muted -- restoring volume")
            self._restore_volume()
        for timer in (self._mute_timer, self._ready_timer, self._unmute_timer, self._deadline_timer):
            timer.stop()
        self._done_for_session = False

    # -- the sequence --

    def _mute(self) -> None:
        if not self._track.sessionActive:
            return  # session went away during the delay
        self._muted = True
        self._set_volume_db(-144.0)  # shairport-sync's mute sentinel
        self._ready_timer.start()
        self._check_ready()

    def _check_ready(self) -> None:
        """Metadata (and lyrics, if they're being shown) have arrived."""
        if not self._muted or not self._ready_timer.isActive():
            return
        if not self._track.contentReady:
            return
        if self._settings.showLyrics and not self._lyrics.searched:
            return  # a lyrics lookup is still outstanding
        self._ready_timer.stop()
        LOG.info("sync-on-connect: metadata ready -- restarting the track")
        self._seek_to_start()
        self._unmute_timer.start()

    def _on_ready_timeout(self) -> None:
        LOG.info("sync-on-connect: metadata didn't arrive in time -- unmuting as-is")
        # No seek: without metadata there is nothing to have got in step
        # with, so restarting the track would be a pointless interruption.
        self._restore_volume()

    def _on_deadline(self) -> None:
        if self._muted:
            LOG.warning("sync-on-connect: deadline reached while muted -- restoring volume")
            self._restore_volume()

    def _restore_volume(self) -> None:
        self._muted = False
        self._ready_timer.stop()
        self._deadline_timer.stop()
        percent = display_settings.load()["connect_volume_percent"]
        self._set_volume_db(VOLUME_MIN_DB + (percent / 100.0) * abs(VOLUME_MIN_DB))

    # -- D-Bus --

    def _set_volume_db(self, db: float) -> None:
        self._run([
            "dbus-send", "--system", f"--dest={DBUS_DEST}", DBUS_PATH,
            "org.freedesktop.DBus.Properties.Set",
            f"string:{DBUS_DEST}", "string:Volume", f"variant:double:{db:.2f}",
        ])

    def _seek_to_start(self) -> None:
        self._run([
            "dbus-send", "--system", f"--dest={MPRIS_DEST}", MPRIS_PATH,
            "org.mpris.MediaPlayer2.Player.Seek", f"int64:{SEEK_TO_START_US}",
        ])

    @staticmethod
    def _run(command: list[str]) -> None:
        # Fire-and-forget: ordering here is enforced by this class's own
        # timers, not by waiting on dbus-send, and the GUI thread must not
        # block on D-Bus.
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            LOG.warning("sync-on-connect: %s failed: %s", command[0], exc)

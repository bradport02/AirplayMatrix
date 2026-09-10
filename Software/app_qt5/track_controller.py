"""
Track/session state exposed to QML.

Direct PySide2 port of app/track_controller.py for the Pi Zero WH's Qt5
build -- see that file's docstring for the full rationale (sessionActive vs.
playing, bridgeConnected polling). The only difference from the Qt6 version
is the import line; metadata.py itself has no Qt dependency at all, so
nothing else here needed to change.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from PySide2.QtCore import Property, QObject, QTimer, Signal, Slot

from encoder import legible_text_is_dark, quadrant_colors
from metadata import MetadataItem, MetadataSource, TrackTracker

LOG = logging.getLogger(__name__)

CONNECTION_POLL_MS = 250

# A track change is not one atomic event: shairport-sync delivers title,
# artist and album as separate metadata items, and the PICT artwork later
# still -- often a good deal later, since it's the big one. So the fade is
# driven off what it's actually waiting for (the new artwork) rather than
# off a fixed quiet period.
#
# The earlier version here restarted a single ~650ms "no more changes"
# countdown on every arriving item, artwork included, which produced two
# fades per track: the countdown expired before the artwork showed up, so
# the panel faded back in still displaying the *previous* track's cover,
# then the artwork landed and was treated as a fresh change, fading out and
# in all over again.
#
# Now: identity opens the transition, the new artwork closes it. This is
# just the short beat between the new artwork being decoded and fading back
# in, so the image is actually ready on screen rather than popping in
# mid-fade.
TRACK_SETTLE_MS = 200

# Fallback for a track whose artwork never arrives at all (not every source
# sends one). Without this the panel would stay faded out for the whole
# song waiting for something that isn't coming. Observed real-world gap
# between the title arriving and the cover following it is ~1.5s, so this
# needs decent headroom above that -- and if it does expire early, late
# artwork is still published when it turns up (see _on_artwork_touched),
# it just appears without the fade.
TRACK_CHANGE_MAX_MS = 3000

# Safety net on the QML fade-out handshake. NowPlayingView reports when the
# panel reaches zero opacity, but it can only report a *change* -- and the
# panel is legitimately already at zero in several situations (the artwork
# for the outgoing track never finished decoding, or a previous publish left
# it hidden). In those cases the report never comes, and before this the
# transition simply hung there, leaving the screen blank until the next
# track. Treating the callback as "whichever happens first" instead of a
# requirement means a missing report costs a slightly less well-timed fade,
# never a stuck display. Comfortably longer than Theme.durationSlow (400ms).
FADE_OUT_GRACE_MS = 700

# shairport-sync's own mute sentinel (see metadata.py's _parse_volume);
# reused here so "no volume reported yet" and "muted" aren't ambiguous.
NO_VOLUME = -144.0


def _sniff_mime(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"  # AirPlay artwork is overwhelmingly JPEG; safe default


@dataclass
class _Transition:
    """One track change in flight, and nothing else.

    Two things must happen before the incoming track can replace the one on
    screen, and they complete in *either* order:

        settled    the incoming track's data is complete (artwork included)
        faded_out  the panel has confirmed it is off screen

    "fade" mode starts fading the moment a change is noticed, so faded_out
    normally arrives first; "crossfade" holds the outgoing track until the
    data is complete, so settled does. Neither order is special -- the swap
    happens when both are true, whenever that is.

    The point of putting them on an object that exists *only* during a
    change is that there is no such thing as a stale value: no transition,
    no flags. Held as separate long-lived booleans, they repeatedly kept
    values from the previous change and let a fade be reported before one
    had begun, or a swap happen onto a panel that was still visible.
    """

    settled: bool = False
    faded_out: bool = False

    # Whether this change stays on the album already on screen, and so needs
    # only the song title and the lyrics to move -- see
    # TrackController._same_album_as_displayed. It lives here with the other
    # two for the same reason they do: it is a property of one change and
    # has no meaning outside one. Held as a long-lived boolean it would
    # survive into the next change, and "the previous change was a
    # same-album one" is not a question anything wants answered.
    same_album: bool = False

    @property
    def ready(self) -> bool:
        return self.settled and self.faded_out


class TrackController(QObject):
    titleChanged = Signal()
    artistChanged = Signal()
    albumChanged = Signal()
    durationChanged = Signal()
    playingChanged = Signal()
    volumeChanged = Signal()
    artworkChanged = Signal()
    cornersChanged = Signal()
    textIsDarkChanged = Signal()
    sessionActiveChanged = Signal()
    bridgeConnectedChanged = Signal()
    trackChangingChanged = Signal()
    readyToTransitionChanged = Signal()
    sameAlbumTransitionChanged = Signal()
    contentReadyChanged = Signal()
    # Fires when the *live* track identity changes, ahead of anything being
    # published to the display. LyricsController listens to this rather than
    # to titleChanged/artistChanged/albumChanged, which are now deferred
    # until the fade -- a lyrics lookup is a network round trip and should
    # start the moment the new track is known, not a second later once it's
    # on screen.
    liveIdentityChanged = Signal()
    # Duration deliberately gets its own signal rather than riding on
    # liveIdentityChanged: it refines mid-track (prgr's estimate, then a
    # later astm) without the song having changed, and the lyrics controller
    # *clears* its lyrics on an identity change. Folding the two together
    # meant every duration refinement -- and every metadata re-send, such as
    # editing the play queue -- threw away lyrics that were displaying
    # perfectly well and re-fetched them.
    liveDurationChanged = Signal()
    pendingArtworkChanged = Signal()
    clientNameChanged = Signal()
    clientConnectedChanged = Signal()
    _sessionEnded = Signal()
    _sessionStarted = Signal()
    # Internal relays only. _emit_changes runs on the metadata thread, and a
    # QTimer may only be started/stopped from the thread that owns it, so
    # the transition timers are driven through these instead -- Qt queues
    # delivery onto the GUI thread, which is where this object lives. Kept
    # as two separate signals because the two events mean opposite things
    # to the transition: identity opens it, artwork closes it.
    _identityTouched = Signal()
    _artworkTouched = Signal()

    def __init__(
        self, source_factory: Callable[[], MetadataSource], parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        # The source is built on the background thread, not here: both
        # PipeSource and TcpSource block in their constructor until the
        # initial connection succeeds (they call _reconnect() synchronously),
        # so constructing one on the GUI thread would hang app startup for
        # as long as the receiver is unreachable.
        self._source: MetadataSource | None = None
        self._tracker = TrackTracker()
        self._lock = threading.Lock()
        self._session_active = False
        self._bridge_connected = False

        # Artwork derivations for whatever the *tracker* currently holds --
        # i.e. the incoming track, which is not necessarily the one on
        # screen. Moved into _displayed only by _publish().
        self._artwork_source = ""
        self._corners = ("", "", "", "")  # top-left, top-right, bottom-left, bottom-right
        self._text_is_dark = False  # whether now-playing text should use dark-on-light ink

        # What QML is actually showing. The identity Q_PROPERTYs below read
        # from here, NOT from the live tracker, and it is only ever
        # refreshed by _publish() -- which runs while the panel is faded to
        # invisible. That is the whole mechanism: metadata arrives field by
        # field over a few hundred milliseconds, and if the bindings tracked
        # it live the title would visibly swap to the new song while the old
        # song's artwork was still fading out. Freezing the snapshot means
        # the outgoing track stays completely intact until it is off screen,
        # then everything changes at once behind the fade.
        self._displayed = {
            "title": "",
            "artist": "",
            "album": "",
            "duration": 0.0,
            "artwork_source": "",
            "corners": ("", "", "", ""),
            "text_is_dark": False,
        }
        # Whether _displayed holds a real, settled track yet. Until it does,
        # NowPlayingView keeps showing the connection/waiting screen rather
        # than a half-populated now-playing panel.
        self._content_ready = False
        # The two conditions _publish() waits on, tracked separately because
        # they complete independently: the new track's data being complete,
        # and the outgoing panel having finished fading out.
        # The change in flight, or None when the display is settled. See
        # _Transition -- every "is a track change happening?" question in
        # this class is this one identity check.
        self._transition: Optional[_Transition] = None

        # artwork_revision of whatever is currently *on display*. The
        # transition waits for the tracker to hold something different from
        # this -- deliberately not "an artwork item arrived after the title
        # did". Skipping a track and letting one end naturally deliver the
        # same pieces in different orders, and when the artwork came first
        # the old after-the-fact check never matched, so the fade sat there
        # until the 2.5s fallback fired instead of following the music.
        # Comparing against what's displayed doesn't care about ordering.
        self._published_artwork_rev = -1

        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(TRACK_SETTLE_MS)
        self._settle_timer.timeout.connect(self._on_track_settled)

        self._fade_grace_timer = QTimer(self)
        self._fade_grace_timer.setSingleShot(True)
        self._fade_grace_timer.setInterval(FADE_OUT_GRACE_MS)
        self._fade_grace_timer.timeout.connect(self._on_fade_grace_expired)

        self._max_wait_timer = QTimer(self)
        self._max_wait_timer.setSingleShot(True)
        self._max_wait_timer.setInterval(TRACK_CHANGE_MAX_MS)
        self._max_wait_timer.timeout.connect(self._on_track_settled)

        self._identityTouched.connect(self._on_identity_touched)
        self._artworkTouched.connect(self._on_artwork_touched)
        self._sessionEnded.connect(self._on_session_ended)
        self._sessionStarted.connect(self._on_session_started)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(CONNECTION_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_connection)
        self._poll_timer.start()

        self._thread = threading.Thread(target=self._run, args=(source_factory,), daemon=True)
        self._thread.start()

    # -- background thread --

    def _run(self, source_factory: Callable[[], MetadataSource]) -> None:
        source = source_factory()
        self._source = source  # publishes to the GUI-thread poll; see _poll_connection
        self._probe_existing_session()
        for item in source.items():
            self._handle_item(item)

    def _probe_existing_session(self) -> None:
        """Notice a session that was already under way before we started.

        The metadata pipe is a stream of *events*, not state: "pbeg" fires
        once when playback begins and is never repeated, and nothing else
        arrives between track changes. So an app that attaches mid-song --
        after a restart, a crash, or simply being started late -- sees no
        evidence of the session at all and sits with sessionActive false.
        That stops the QML poll timer dead (running: sessionActive), which
        means no progress bar and no lyrics for the rest of the track,
        however well they fetched.

        shairport-sync's MPRIS interface does expose state rather than
        events, so one question at startup settles it. Runs on this
        background thread, not the GUI thread, because it shells out.
        """
        try:
            result = subprocess.run(
                [
                    "dbus-send", "--system", "--print-reply=literal",
                    "--dest=org.mpris.MediaPlayer2.ShairportSync",
                    "/org/mpris/MediaPlayer2",
                    "org.freedesktop.DBus.Properties.Get",
                    "string:org.mpris.MediaPlayer2.Player", "string:PlaybackStatus",
                ],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.debug("could not probe for an existing session: %s", exc)
            return
        if "Playing" not in result.stdout:
            return
        with self._lock:
            if self._session_active:
                return
            self._session_active = True
        LOG.info("AirPlay session already in progress at startup")
        self.sessionActiveChanged.emit()

    def _handle_item(self, item: MetadataItem) -> None:
        with self._lock:
            changed = self._tracker.apply(item)

            session_changed = False
            session_ended = False
            # "pbeg" is the proper session-start event, but it fires exactly
            # once, at the moment playback begins. Anything that attaches to
            # the stream later -- this app restarting mid-song, most
            # obviously -- never sees it and would sit there believing no
            # session exists, with the poll timer (running: sessionActive)
            # stopped: no progress bar, and no lyrics however well they
            # fetched. The other three are all unambiguous evidence that a
            # session is underway right now, so any of them will do to
            # notice one already in progress.
            if (
                item.type == "ssnc"
                and item.code in ("pbeg", "prgr", "pres", "prsm")
                and not self._session_active
            ):
                self._session_active = True
                session_changed = True
            elif item.type == "ssnc" and item.code == "pend" and self._session_active:
                self._session_active = False
                session_changed = True
                session_ended = True
                # pend itself carries no artwork-clear -- that's normally
                # only a zero-length PICT mid-session -- so without this
                # the last track's artwork keeps showing (background,
                # matrix panel) indefinitely after the session has actually
                # ended.
                if self._clear_artwork_locked():
                    changed.add("artwork")
                    changed.add("artwork_revision")

            if "artwork" in changed or "artwork_revision" in changed:
                self._rebuild_artwork_source()

        self._emit_changes(changed, session_changed)
        if session_ended:
            self._sessionEnded.emit()
        elif session_changed and self._session_active:
            self._sessionStarted.emit()

    def _clear_artwork_locked(self) -> bool:
        """Caller holds self._lock. Returns True if artwork actually changed."""
        if self._tracker.state.artwork is None:
            return False
        self._tracker.state.artwork = None
        self._tracker.state.artwork_revision += 1
        return True

    def _rebuild_artwork_source(self) -> None:
        # Caller holds self._lock.
        data = self._tracker.state.artwork
        if not data:
            self._artwork_source = ""
            self._corners = ("", "", "", "")
            self._text_is_dark = False
            return
        mime = _sniff_mime(data)
        b64 = base64.b64encode(data).decode("ascii")
        self._artwork_source = f"data:{mime};base64,{b64}"

        # Feeds Background.qml's ambient colour wash. Best-effort: a decode
        # failure here shouldn't take down artwork display, which just
        # succeeded above using the same bytes.
        colors = quadrant_colors(data)
        self._corners = (
            (colors.top_left, colors.top_right, colors.bottom_left, colors.bottom_right)
            if colors is not None
            else ("", "", "", "")
        )
        # Whether title/artist/lyrics text should switch to dark ink for
        # this artwork -- see encoder.legible_text_is_dark's docstring.
        self._text_is_dark = legible_text_is_dark(colors) if colors is not None else False

    def _emit_changes(self, changed: set[str], session_changed: bool) -> None:
        # Qt auto-queues signal delivery to the GUI thread, so emitting here
        # from the background thread is safe -- the connected slots just run
        # later, on the thread that owns this QObject.
        #
        # Note what is NOT emitted here any more: title/artist/album/
        # duration/artwork/corners/textIsDark. Those describe *which track*
        # and are published as one atomic batch by _publish(), behind the
        # fade. Only genuinely live transport state goes out immediately.
        if "playing" in changed:
            self.playingChanged.emit()
        if "volume" in changed:
            self.volumeChanged.emit()
        if session_changed:
            LOG.info("AirPlay session %s", "started" if self._session_active else "ended")
            self.sessionActiveChanged.emit()

        if "client_name" in changed:
            self.clientNameChanged.emit()
        if "client_connected" in changed:
            self.clientConnectedChanged.emit()
        if changed & {"artwork", "artwork_revision"}:
            self.pendingArtworkChanged.emit()
        if changed & {"title", "artist", "album"}:
            self.liveIdentityChanged.emit()
        if "duration" in changed:
            self.liveDurationChanged.emit()

        # Deliberately not position/playing/volume: those change constantly
        # mid-track and would keep the panel faded out forever.
        if changed & {"title", "artist", "album"}:
            self._identityTouched.emit()
        if changed & {"artwork", "artwork_revision"}:
            self._artworkTouched.emit()

    # -- GUI-thread transition window --

    def _on_identity_touched(self) -> None:
        # Only the *first* identity item opens the transition. The others
        # (artist, album) arrive right behind it and must not reopen or
        # extend anything -- what the panel is waiting on from here is the
        # artwork, and that's what _on_artwork_touched resolves. They do get
        # a say in exactly one thing: the same-album verdict below was taken
        # before they landed, so it is re-taken as they land.
        if self._transition is not None:
            self._recheck_same_album()
            return
        LOG.debug("transition: opened (content_ready=%s)", self._content_ready)
        # A fade-out is always assumed owed; either QML's report or the
        # grace timer satisfies it. See FADE_OUT_GRACE_MS for why that
        # can't be inferred from _content_ready.
        self._transition = _Transition(same_album=self._same_album_as_displayed())
        self._settle_timer.stop()
        self._fade_grace_timer.stop()
        if self._artwork_is_new():
            # The new cover is already here -- it arrived with, or ahead of,
            # the title. Nothing left to wait for beyond the short settle.
            self._settle_timer.start()
        else:
            self._max_wait_timer.start()
        # Same-album first, then trackChanging -- deliberately, and the
        # order matters. Main.qml's showTrack reads both, and QML
        # re-evaluates it after *each* emission with the other property
        # still sitting at the value it was last notified of. Announcing the
        # change first would therefore show it one intermediate state of "a
        # change is in flight and it is not a same-album one", which is
        # precisely the combination that takes the whole panel and the
        # backdrop off screen. Nothing renders between two synchronous
        # emits, so it would most likely never be seen -- but the panel
        # staying put is the entire feature, and it costs nothing to make it
        # true of every intermediate state rather than just the final one.
        self.sameAlbumTransitionChanged.emit()
        self.trackChangingChanged.emit()
        self.readyToTransitionChanged.emit()

    def _same_album_as_displayed(self) -> bool:
        """Whether the incoming track is simply the next one off the album
        that is already on screen.

        The key is artist AND album, never album alone. Album titles collide
        constantly -- "Greatest Hits", "Live", "Singles", and the empty
        string every sender that doesn't populate `asal` leaves behind -- so
        matching on the album by itself would declare two unrelated records
        the same album and cut one cover straight into the other with no
        transition at all. Pairing it with the artist is also exactly the
        guarantee the display needs rather than a merely-safer key: the
        details line under the title renders artist and album together, and
        this whole feature's promise is that that line does not move. If
        either half of it differs, something on screen has to acknowledge
        that, so it has to be an ordinary full transition.

        The price of that strictness is compilations: a "Various Artists"
        record changes artist every track, so it gets the full transition
        even though its cover never changes. That is the right way round to
        be wrong -- the artist text really is changing there.

        Empty fields never match, in either direction. An unknown album is
        not evidence of sameness, and treating two blanks as equal would
        turn "this sender sends no album metadata" into "every track is off
        the same album", i.e. artwork that never transitions again for the
        rest of the session. Same for the artist.

        And there has to be something on screen worth keeping. Before the
        first publish of a session _displayed is still empty, so there is no
        established album for the incoming track to be the same as.
        """
        if not self._content_ready:
            return False
        with self._lock:
            # Both sides read under one lock: _publish() swaps _displayed
            # wholesale, and comparing against half of the old snapshot and
            # half of the new one would be meaningless.
            artist = self._tracker.state.artist
            album = self._tracker.state.album
            displayed_artist = self._displayed["artist"]
            displayed_album = self._displayed["album"]
        if not artist or not album:
            return False
        return artist == displayed_artist and album == displayed_album

    def _recheck_same_album(self) -> None:
        """Withdraw a same-album verdict the rest of the metadata has since
        contradicted.

        The verdict has to be taken the instant the transition opens --
        "fade" mode starts moving the panel right there, so there is nothing
        to wait with -- but at that instant it is only provisional. The
        tracker has no per-track boundary: it holds the *previous* track's
        artist and album until an item actually replaces them (see
        TrackTracker.apply), so a transition opened by the title arriving on
        its own compares the incoming track against fields that still
        describe the outgoing one, and every change looks like a same-album
        change until asar/asal catch up.

        Nothing can slip past this. _emit_changes fires _identityTouched on
        any change to title, artist OR album, so every value this verdict is
        built from is re-examined the moment it moves -- there is no path by
        which the album can change during a transition without arriving
        here. What is *not* guaranteed is that it changes before the swap:
        a sender that revises the album a beat after the artwork has landed
        misses this window entirely, but that lands as an ordinary mid-track
        metadata refinement, which opens a transition of its own and
        corrects the display in full.

        In practice they catch up in the same burst, microseconds later on
        the metadata thread and usually within the same GUI-thread event
        loop turn, so this runs before a single frame has been rendered and
        the provisional verdict is never seen at all. When a sender splits
        that burst across separate pipe reads it can cost one frame of a
        fade that then reverses -- on the order of 2% of opacity, at the
        very start of the ease. That is the deliberate trade: the
        alternative is delaying the fade on *every* track change by a
        settling window, to cover a case that resolves itself in a frame.

        One-way on purpose. A verdict can only be downgraded, never
        upgraded. By the time this could promote a change back to
        same-album the panel may already be visibly on its way out, and
        snapping it back to full opacity mid-fade is a far worse artefact
        than the conservative transition it would be "correcting".
        Downgrading is safe because nothing has moved yet.
        """
        transition = self._transition
        if transition is None or not transition.same_album:
            return
        if self._same_album_as_displayed():
            return
        LOG.debug("transition: same-album verdict withdrawn, transitioning in full")
        transition.same_album = False
        self.sameAlbumTransitionChanged.emit()

    def _artwork_is_new(self) -> bool:
        """Whether the tracker holds real, not-yet-displayed artwork.

        Requires actual bytes, not just a revision bump. Senders emit a
        zero-length PICT to clear the outgoing cover roughly a second
        before delivering the incoming one, and that clear bumps the
        revision too -- taking it as "the new artwork is here" published
        the track with no artwork at all, and the real cover then arrived
        after the transition had already closed.
        """
        with self._lock:
            state = self._tracker.state
            return state.artwork is not None and (
                state.artwork_revision != self._published_artwork_rev
            )

    def _on_artwork_touched(self) -> None:
        if not self._artwork_is_new():
            return
        if self._transition is None:
            # Artwork for a track that has already been published -- the
            # sender was slower than TRACK_CHANGE_MAX_MS, or it updated the
            # cover mid-track. Publishing it straight away means it appears
            # (without a fade, since the panel is already up) rather than
            # being dropped on the floor, which is what used to happen to
            # any artwork that missed its transition.
            #
            # Gated on _content_ready, and that matters: on a fresh connect
            # the artwork routinely arrives *before* the title, and without
            # this guard that first PICT published a snapshot with an empty
            # title and flipped _content_ready on -- which put the progress
            # bar on screen next to a blank panel. There is no track to
            # update until a real one has been published.
            if self._session_active and self._content_ready:
                LOG.debug("late artwork -- publishing without a transition")
                self._publish()
            return
        self._max_wait_timer.stop()
        self._settle_timer.start()

    def _on_track_settled(self) -> None:
        """The incoming track's data is as complete as it's going to get."""
        if self._transition is None:
            return  # a timer outliving its transition
        self._settle_timer.stop()
        self._max_wait_timer.stop()
        LOG.debug("transition: data settled (faded_out=%s)", self._transition.faded_out)
        self._transition.settled = True
        # Only now can the visible fade begin in crossfade mode (the panel
        # holds the outgoing track until the data is complete), so this is
        # where the backstop starts counting. Starting it back when the
        # change was first noticed meant it expired during the ~1.5s wait
        # for artwork and declared the fade finished before it had begun --
        # which published the new track straight onto a fully visible
        # panel, and only then let it fade. Skipped when QML has already
        # reported (fade mode fades immediately, so it usually has).
        self.readyToTransitionChanged.emit()
        if not self._transition.faded_out:
            self._fade_grace_timer.start()
        self._advance()

    def _on_session_started(self) -> None:
        """Put the current track back on screen when a session resumes.

        Sessions do not only start when someone presses play: the metadata
        FIFO EOFs briefly whenever shairport-sync reopens it, which reads as
        the session ending and starting again a second later. _on_session_ended
        clears content_ready (so the *next* connection opens on the waiting
        screen), and normally a transition would republish -- but only an
        identity *change* opens one, and after a blip like this the track is
        the same one that was already playing. Nothing changed, so nothing
        republished, and the display sat on "Loading track..." indefinitely
        with a perfectly good track underneath it.

        Publishing directly is right here: there is nothing to transition
        between, the same track simply needs to be shown again.
        """
        if self._content_ready:
            return
        with self._lock:
            has_track = bool(self._tracker.state.title)
        if not has_track:
            return  # genuinely nothing to show yet; wait for metadata
        LOG.debug("session resumed with a track already known -- republishing")
        self._end_transition()
        self._publish()

    def _on_session_ended(self) -> None:
        """Back to a clean slate, so the next connection starts from the
        waiting screen and fades in fresh rather than flashing up the last
        session's track."""
        self._end_transition()
        if self._content_ready:
            self._content_ready = False
            self.contentReadyChanged.emit()

    @Slot()
    def fadeOutComplete(self) -> None:
        """Called from NowPlayingView.qml the moment the panel reaches zero
        opacity. Swapping the displayed track any earlier than this is
        exactly the bug this whole mechanism exists to avoid."""
        if self._transition is None:
            return  # nothing in flight; QML guards this too, belt and braces
        LOG.debug("transition: fade-out reported by QML (settled=%s)", self._transition.settled)
        self._fade_grace_timer.stop()
        self._transition.faded_out = True
        self._advance()

    def _on_fade_grace_expired(self) -> None:
        """QML didn't report a fade-out in time -- most likely because the
        panel was already invisible and its opacity never changed."""
        if self._transition is None:
            return
        LOG.debug("transition: fade-out grace expired (settled=%s)", self._transition.settled)
        self._transition.faded_out = True
        self._advance()

    def _advance(self) -> None:
        """Publish as soon as both halves of the transition are done."""
        if self._transition is not None and self._transition.ready:
            self._publish()

    def _end_transition(self) -> None:
        """Drop any change in flight and silence its timers.

        Safe to call when nothing is in flight, which is why every path that
        finishes or abandons a transition can just call it.
        """
        self._settle_timer.stop()
        self._max_wait_timer.stop()
        self._fade_grace_timer.stop()
        if self._transition is None:
            return
        self._transition = None
        # trackChanging first, same-album second -- the mirror image of the
        # order _on_identity_touched emits them in, and for the mirror
        # reason. Retiring the same-album verdict while showTrack still has
        # trackChanging cached as true would hand it that same "changing,
        # and not a same-album change" combination on the way *out* of a
        # transition the panel sat through untouched, which would fade it
        # out just as the new track appears. Announcing the change is over
        # first leaves no such state to observe.
        self.trackChangingChanged.emit()
        self.sameAlbumTransitionChanged.emit()
        self.readyToTransitionChanged.emit()

    def _publish(self) -> None:
        """Move the incoming track into _displayed and let the panel back in.

        Everything changes in one go while nothing is visible, so the panel
        never shows a mix of two tracks.
        """
        with self._lock:
            state = self._tracker.state
            new = {
                "title": state.title,
                "artist": state.artist,
                "album": state.album,
                "duration": state.duration,
                "artwork_source": self._artwork_source,
                "corners": self._corners,
                "text_is_dark": self._text_is_dark,
            }
            old, self._displayed = self._displayed, new
            self._published_artwork_rev = state.artwork_revision

        if old["title"] != new["title"]:
            self.titleChanged.emit()
        if old["artist"] != new["artist"]:
            self.artistChanged.emit()
        if old["album"] != new["album"]:
            self.albumChanged.emit()
        if old["duration"] != new["duration"]:
            self.durationChanged.emit()
        if old["artwork_source"] != new["artwork_source"]:
            self.artworkChanged.emit()
        if old["corners"] != new["corners"]:
            self.cornersChanged.emit()
        if old["text_is_dark"] != new["text_is_dark"]:
            self.textIsDarkChanged.emit()

        LOG.debug("transition: published %r", new["title"])
        if not self._content_ready:
            self._content_ready = True
            self.contentReadyChanged.emit()

        # Last, so QML applies the new content before it starts fading it in.
        self._end_transition()

    def _get_client_name(self) -> str:
        with self._lock:
            return self._tracker.state.client_name

    # Deliberately live rather than part of the published snapshot: the
    # connection screen shows this *before* there is any track to publish.
    clientName = Property(str, _get_client_name, notify=clientNameChanged)

    def _get_pending_artwork_source(self) -> str:
        with self._lock:
            return self._artwork_source

    # The *incoming* track's artwork, as soon as its bytes have been decoded
    # to a data URL -- which is roughly a second and a half before the
    # snapshot it belongs to is published. Lets QML decode the new cover
    # into an offscreen layer during that wait, so the transition doesn't
    # have to stop and decode when it starts. Not what the panel displays;
    # that's artworkSource, which stays on the published snapshot.
    pendingArtworkSource = Property(
        str, _get_pending_artwork_source, notify=pendingArtworkChanged
    )

    def _get_client_connected(self) -> bool:
        with self._lock:
            return self._tracker.state.client_connected

    # A device is connected but may not have started streaming yet. Also
    # live, for the same reason clientName is.
    clientConnected = Property(bool, _get_client_connected, notify=clientConnectedChanged)

    def _get_track_changing(self) -> bool:
        return self._transition is not None

    def _get_ready_to_transition(self) -> bool:
        return self._transition is not None and self._transition.settled

    # True from the moment a new track's metadata starts arriving until
    # TRACK_SETTLE_MS after the last of it lands. NowPlayingView.qml fades
    # the now-playing panel out while it's set, so the switch reads as one
    # deliberate transition rather than fields visibly popping in one by one.
    trackChanging = Property(bool, _get_track_changing, notify=trackChangingChanged)
    # A change is in flight AND the incoming track's data is complete -- so
    # the visible transition can run start to finish without stopping to
    # wait for anything. Crossfade mode holds the outgoing track fully on
    # screen until this turns true: the artwork arrives about a second and a
    # half after the title, and fading out on the title alone left the
    # display sitting empty for that whole gap.
    readyToTransition = Property(
        bool, _get_ready_to_transition, notify=readyToTransitionChanged
    )

    def _get_same_album_transition(self) -> bool:
        return self._transition is not None and self._transition.same_album

    # A change is in flight AND it stays on the album already on screen, so
    # the cover, the blurred backdrop and the artist/album line are all
    # correct for the incoming track before it even arrives. QML narrows the
    # transition down to the song title and the lyrics while this is set and
    # leaves everything else completely alone -- see NowPlayingView.qml.
    #
    # It goes false again with the transition itself rather than latching,
    # which is what keeps a *mid-track* artwork update (see
    # _on_artwork_touched's late-publish path) behaving exactly as it always
    # has: there is no transition in flight then, so this is false, and the
    # cover changing under a panel that thought it had nothing to do is not
    # a case QML has to reason about at all.
    sameAlbumTransition = Property(
        bool, _get_same_album_transition, notify=sameAlbumTransitionChanged
    )

    def _get_content_ready(self) -> bool:
        return self._content_ready

    # False until the first track of a session has been published. Keeps
    # NowPlayingView on the connection/waiting screen rather than showing a
    # now-playing panel that is still filling itself in, and gives that
    # screen something to cross-fade *out of* when the track arrives.
    contentReady = Property(bool, _get_content_ready, notify=contentReadyChanged)

    def live_identity(self) -> tuple[str, str, str]:
        """(artist, title, album) as the metadata stream has them right now,
        ahead of any fade. For LyricsController, which must start its lookup
        immediately -- not for anything that draws."""
        with self._lock:
            state = self._tracker.state
            return (state.artist, state.title, state.album)

    def live_composer(self) -> str:
        """Songwriting credits as delivered ("A, B & C"), or "" if the
        sender didn't provide any. For LyricsController's end-of-song
        credits; nothing draws this directly."""
        with self._lock:
            return self._tracker.state.composer

    def live_duration(self) -> float:
        with self._lock:
            return self._tracker.state.duration

    # -- GUI-thread connection poll --

    def _poll_connection(self) -> None:
        source = self._source
        connected = source.connected if source is not None else False
        session_changed = False
        artwork_cleared = False
        with self._lock:
            if connected == self._bridge_connected:
                return
            self._bridge_connected = connected
            if not connected and self._session_active:
                # No live transport, so whatever session we last saw is no
                # longer observable -- don't leave the UI showing "playing".
                self._session_active = False
                session_changed = True
                artwork_cleared = self._clear_artwork_locked()
                if artwork_cleared:
                    self._rebuild_artwork_source()
        LOG.info("bridge %s", "connected" if connected else "disconnected")
        self.bridgeConnectedChanged.emit()
        if session_changed:
            LOG.info("AirPlay session %s", "started" if self._session_active else "ended")
            self.sessionActiveChanged.emit()
        if artwork_cleared:
            self.artworkChanged.emit()
            self.cornersChanged.emit()
            self.textIsDarkChanged.emit()

    # -- Q_PROPERTY surface --

    # These read the published snapshot, not the live tracker -- see
    # _displayed in __init__ and _publish().

    def _get_title(self) -> str:
        with self._lock:
            return self._displayed["title"]

    def _get_artist(self) -> str:
        with self._lock:
            return self._displayed["artist"]

    def _get_album(self) -> str:
        with self._lock:
            return self._displayed["album"]

    def _get_duration(self) -> float:
        with self._lock:
            return self._displayed["duration"]

    def _get_playing(self) -> bool:
        with self._lock:
            return self._tracker.state.playing

    def _get_volume(self) -> float:
        with self._lock:
            volume = self._tracker.state.volume
        return volume if volume is not None else NO_VOLUME

    def _get_artwork_source(self) -> str:
        with self._lock:
            return self._displayed["artwork_source"]

    def _get_corner_top_left(self) -> str:
        with self._lock:
            return self._displayed["corners"][0]

    def _get_corner_top_right(self) -> str:
        with self._lock:
            return self._displayed["corners"][1]

    def _get_corner_bottom_left(self) -> str:
        with self._lock:
            return self._displayed["corners"][2]

    def _get_corner_bottom_right(self) -> str:
        with self._lock:
            return self._displayed["corners"][3]

    def _get_text_is_dark(self) -> bool:
        with self._lock:
            return self._displayed["text_is_dark"]

    def artwork_bytes(self) -> bytes | None:
        """Raw artwork as delivered (JPEG or PNG), for non-QML consumers
        (MatrixController) that need the original bytes rather than the
        already-base64'd QML display string."""
        with self._lock:
            return self._tracker.state.artwork

    def _get_session_active(self) -> bool:
        with self._lock:
            return self._session_active

    def _get_bridge_connected(self) -> bool:
        with self._lock:
            return self._bridge_connected

    title = Property(str, _get_title, notify=titleChanged)
    artist = Property(str, _get_artist, notify=artistChanged)
    album = Property(str, _get_album, notify=albumChanged)
    duration = Property(float, _get_duration, notify=durationChanged)
    playing = Property(bool, _get_playing, notify=playingChanged)
    volume = Property(float, _get_volume, notify=volumeChanged)
    artworkSource = Property(str, _get_artwork_source, notify=artworkChanged)
    cornerTopLeft = Property(str, _get_corner_top_left, notify=cornersChanged)
    cornerTopRight = Property(str, _get_corner_top_right, notify=cornersChanged)
    cornerBottomLeft = Property(str, _get_corner_bottom_left, notify=cornersChanged)
    cornerBottomRight = Property(str, _get_corner_bottom_right, notify=cornersChanged)
    textIsDark = Property(bool, _get_text_is_dark, notify=textIsDarkChanged)
    sessionActive = Property(bool, _get_session_active, notify=sessionActiveChanged)
    bridgeConnected = Property(bool, _get_bridge_connected, notify=bridgeConnectedChanged)

    @Slot(result=float)
    def currentPosition(self) -> float:
        with self._lock:
            return self._tracker.state.position()

"""
Track/session state exposed to QML.

Owns the shairport-sync metadata item stream on a background thread (same
async style as lrclib.py's LyricsFetcher: a plain daemon thread, not a
second concurrency idiom). TrackTracker does the actual field-folding;
this class only adds the QObject/signal surface QML needs plus two things
TrackTracker doesn't track on its own:

  * sessionActive -- whether an AirPlay session is open (pbeg..pend),
    independent of `playing`, which conflates pause (pfls) with session end
    (pend). Without this, "paused" and "no device connected" would look
    identical in the UI.

  * bridgeConnected -- whether the TCP link to sps_bridge is actually up.
    This is polled on a GUI-thread timer rather than derived from item
    arrival: while the bridge is down, no items are flowing at all, so a
    purely item-driven signal would never observe the "disconnected" state
    during an outage.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Callable

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from encoder import quadrant_colors
from metadata import MetadataItem, MetadataSource, TrackTracker

LOG = logging.getLogger(__name__)

CONNECTION_POLL_MS = 250

# shairport-sync's own mute sentinel (see metadata.py's _parse_volume);
# reused here so "no volume reported yet" and "muted" aren't ambiguous.
NO_VOLUME = -144.0


def _sniff_mime(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"  # AirPlay artwork is overwhelmingly JPEG; safe default


class TrackController(QObject):
    titleChanged = Signal()
    artistChanged = Signal()
    albumChanged = Signal()
    durationChanged = Signal()
    playingChanged = Signal()
    volumeChanged = Signal()
    artworkChanged = Signal()
    cornersChanged = Signal()
    sessionActiveChanged = Signal()
    bridgeConnectedChanged = Signal()

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
        self._artwork_source = ""
        self._corners = ("", "", "", "")  # top-left, top-right, bottom-left, bottom-right

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
        for item in source.items():
            self._handle_item(item)

    def _handle_item(self, item: MetadataItem) -> None:
        with self._lock:
            changed = self._tracker.apply(item)

            session_changed = False
            if item.type == "ssnc" and item.code == "pbeg" and not self._session_active:
                self._session_active = True
                session_changed = True
            elif item.type == "ssnc" and item.code == "pend" and self._session_active:
                self._session_active = False
                session_changed = True
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

    def _emit_changes(self, changed: set[str], session_changed: bool) -> None:
        # Qt auto-queues signal delivery to the GUI thread, so emitting here
        # from the background thread is safe -- the connected slots just run
        # later, on the thread that owns this QObject.
        if "title" in changed:
            self.titleChanged.emit()
        if "artist" in changed:
            self.artistChanged.emit()
        if "album" in changed:
            self.albumChanged.emit()
        if "duration" in changed:
            self.durationChanged.emit()
        if "playing" in changed:
            self.playingChanged.emit()
        if "volume" in changed:
            self.volumeChanged.emit()
        if "artwork" in changed or "artwork_revision" in changed:
            self.artworkChanged.emit()
            self.cornersChanged.emit()
        if session_changed:
            LOG.info("AirPlay session %s", "started" if self._session_active else "ended")
            self.sessionActiveChanged.emit()

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

    # -- Q_PROPERTY surface --

    def _get_title(self) -> str:
        with self._lock:
            return self._tracker.state.title

    def _get_artist(self) -> str:
        with self._lock:
            return self._tracker.state.artist

    def _get_album(self) -> str:
        with self._lock:
            return self._tracker.state.album

    def _get_duration(self) -> float:
        with self._lock:
            return self._tracker.state.duration

    def _get_playing(self) -> bool:
        with self._lock:
            return self._tracker.state.playing

    def _get_volume(self) -> float:
        with self._lock:
            volume = self._tracker.state.volume
        return volume if volume is not None else NO_VOLUME

    def _get_artwork_source(self) -> str:
        with self._lock:
            return self._artwork_source

    def _get_corner_top_left(self) -> str:
        with self._lock:
            return self._corners[0]

    def _get_corner_top_right(self) -> str:
        with self._lock:
            return self._corners[1]

    def _get_corner_bottom_left(self) -> str:
        with self._lock:
            return self._corners[2]

    def _get_corner_bottom_right(self) -> str:
        with self._lock:
            return self._corners[3]

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
    sessionActive = Property(bool, _get_session_active, notify=sessionActiveChanged)
    bridgeConnected = Property(bool, _get_bridge_connected, notify=bridgeConnectedChanged)

    @Slot(result=float)
    def currentPosition(self) -> float:
        with self._lock:
            return self._tracker.state.position()

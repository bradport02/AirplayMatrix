"""
LED matrix link state exposed to QML.

Direct PySide2 port of app/matrix_controller.py -- see that file's docstring
for the full rationale (single-slot "latest wins" mailbox, worker-thread
encode, USB auto-detect/reconnect, and how EQ-meter frames share the same
mailbox and self-arbitrate against display_settings.py's eq_meter_enabled
alongside artwork pushes). Only the import lines differ; matrix/link.py,
matrix/eq_meter.py and encoder.py have no Qt dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple, Optional, Union

from PySide2.QtCore import Property, QObject, Signal, Slot

from encoder import EncodedArtwork, blank, encode, encode_image

from .settings_controller import SettingsController
from .track_controller import TrackController

# pyserial, Pillow and matrix/link are all pulled in on the worker thread
# below rather than here. Between them they cost well over a second on the
# Zero WH's ARM1176, and none of it is needed until a frame is actually
# encoded or a panel actually found -- both of which happen on that thread,
# after the display is already up. Paying for them at import time just
# lengthened the black screen at boot for hardware that may not even be
# attached. Annotations in this file are strings (PEP 563), so they never
# need the real modules.
LOG = logging.getLogger(__name__)

RECONNECT_INTERVAL = 3.0  # seconds before the *first* retry after a drop
# Ceiling for the exponential backoff below. A matrix that's plugged in and
# working never gets here (the backoff resets the moment a connect
# succeeds); this is for the case where there simply isn't one attached --
# a real and ongoing configuration on the Zero WH, which spends most of its
# life as a lyrics/now-playing screen. Rescanning the USB bus every 3s
# forever to discover the same nothing is background work this single-core
# device shouldn't be doing, and 30s is still well inside "notices within a
# few seconds of me actually plugging it in" territory for a panel that has
# to be physically connected by hand.
MAX_RECONNECT_INTERVAL = 30.0
IDLE_POLL_INTERVAL = 1.0  # seconds the worker blocks waiting for new artwork


class _Frame(NamedTuple):
    """What's sitting in the mailbox: either raw artwork bytes still needing
    encoder.encode() (decode/downscale/crop first), a PIL Image that only
    needs encoder.encode_image() (already MATRIX_SIZE-square -- an EQ-meter
    bar frame), or the blank sentinel (kind="blank", no data)."""

    kind: str  # "artwork" | "eq" | "blank"
    data: Union[bytes, Image.Image, None]


class _LatestSlot:
    """A one-item mailbox: put() always overwrites, get() blocks up to
    `timeout` and returns the most recent value (or None on timeout)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._value: Optional[_Frame] = None
        self._has_value = False

    def put(self, value: _Frame) -> None:
        with self._cond:
            self._value = value
            self._has_value = True
            self._cond.notify()

    def get(self, timeout: float) -> Optional[_Frame]:
        with self._cond:
            if not self._has_value:
                self._cond.wait(timeout=timeout)
            if not self._has_value:
                return None
            value = self._value
            self._has_value = False
            self._value = None
            return value


class MatrixController(QObject):
    statusChanged = Signal()
    portNameChanged = Signal()
    errorChanged = Signal()

    def __init__(
        self,
        track: TrackController,
        settings: SettingsController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._status = "disconnected"  # disconnected | connecting | connected | error
        self._port_name = ""  # last port auto-detected; re-resolved on every connect attempt
        self._error = ""

        self._track = track
        self._settings = settings
        self._pending = _LatestSlot()
        self._blank_sent = threading.Event()

        track.artworkChanged.connect(lambda: self._on_track_artwork(track))
        # eq_meter_enabled flipping off should hand the panel back to
        # artwork right away, not leave it sitting on the last EQ frame
        # until the next metadata item happens to arrive (which may be
        # minutes away, or never, if nothing about the current track
        # changes again). Flipping on needs no equivalent push here --
        # EqController starts its own timer on the same signal and the
        # first frame follows within one tick.
        settings.eqMeterEnabledChanged.connect(self._on_eq_toggle)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _on_track_artwork(self, track: TrackController) -> None:
        if self._settings.eqMeterEnabled:
            return  # EQ meter owns the panel right now; see module docstring
        # None is the "push a blank frame" sentinel (see _run) -- artwork
        # clearing (explicit zero-length PICT, session end, bridge
        # disconnect) fires this same signal with artwork_bytes() now None,
        # and the panel needs to actually blank rather than keep showing
        # whatever it last received.
        data = track.artwork_bytes()
        self._pending.put(_Frame("artwork", data) if data else _Frame("blank", None))

    def _on_eq_toggle(self) -> None:
        if not self._settings.eqMeterEnabled:
            self._on_track_artwork(self._track)

    @Slot(object)
    def push_eq_frame(self, img: Image.Image) -> None:
        """Called by EqController once per tick with a freshly rendered
        bar-graph frame. Self-arbitrates against eq_meter_enabled the same
        way _on_track_artwork does, rather than trusting the caller not to
        call this after the setting's already flipped back off -- a frame
        already in flight from EqController's own thread/timer when the
        toggle changes shouldn't win a race against the artwork refresh
        _on_eq_toggle just queued."""
        if not self._settings.eqMeterEnabled:
            return
        self._pending.put(_Frame("eq", img))

    # -- background thread --

    def _run(self) -> None:
        import serial
        from matrix.link import MatrixLink, PayloadTooLarge, find_matrix_port

        link: Optional[MatrixLink] = None
        retry_delay = RECONNECT_INTERVAL
        while True:
            if link is None:
                link = self._try_connect()
                if link is None:
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RECONNECT_INTERVAL)
                    continue
                retry_delay = RECONNECT_INTERVAL

            frame = self._pending.get(timeout=IDLE_POLL_INTERVAL)
            if frame is None:
                if find_matrix_port() is None:
                    self._set_status("disconnected", error="no matching USB device found")
                    link = self._close(link)
                continue

            artwork: Optional[EncodedArtwork]
            if frame.kind == "blank":
                artwork = blank()
            elif frame.kind == "eq":
                artwork = encode_image(frame.data)
            else:
                artwork = encode(frame.data)
            if artwork is None:
                LOG.warning("%s frame failed to encode for matrix push", frame.kind)
                continue

            try:
                link.send_artwork(artwork)
                if frame.kind == "blank":
                    self._blank_sent.set()
            except (PayloadTooLarge, serial.SerialException) as exc:
                LOG.warning("matrix push failed: %s", exc)
                self._set_status("error", error=str(exc))
                link = self._close(link)

    def _try_connect(self) -> Optional[MatrixLink]:
        # Same deferral as _run, which is this method's only caller -- both
        # already resolved by the time it runs, and Python caches modules so
        # this is a dict lookup, not a second load.
        import serial
        from matrix.link import MatrixLink, find_matrix_port

        port = find_matrix_port()
        if not port:
            self._set_status("disconnected")
            return None
        with self._lock:
            port_changed = port != self._port_name
            self._port_name = port
        if port_changed:
            self.portNameChanged.emit()
        self._set_status("connecting")
        try:
            link = MatrixLink(port)
        except serial.SerialException as exc:
            self._set_status("error", error=str(exc))
            return None
        self._set_status("connected")
        return link

    @staticmethod
    def _close(link: Optional[MatrixLink]) -> None:
        if link is not None:
            link.close()
        return None

    def _set_status(self, status: str, error: str = "") -> None:
        with self._lock:
            status_changed = status != self._status
            error_changed = error != self._error
            self._status = status
            self._error = error
        if status_changed:
            LOG.info("matrix status: %s%s", status, f" ({error})" if error else "")
            self.statusChanged.emit()
        if error_changed:
            self.errorChanged.emit()

    # -- Q_PROPERTY surface --

    def _get_status(self) -> str:
        with self._lock:
            return self._status

    def _get_port_name(self) -> str:
        with self._lock:
            return self._port_name

    def _get_error(self) -> str:
        with self._lock:
            return self._error

    status = Property(str, _get_status, notify=statusChanged)
    portName = Property(str, _get_port_name, notify=portNameChanged)
    error = Property(str, _get_error, notify=errorChanged)

    @Slot()
    def clear(self) -> None:
        """Push a blank frame and block briefly for it to actually be sent.

        Used on app shutdown, from the GUI thread: without waiting, the
        process can exit (killing this daemon thread) before the background
        worker ever wakes up and writes it out, leaving the panel frozen on
        the last artwork forever. The wait is bounded -- if the matrix is
        disconnected or unresponsive, shutdown must not hang on it.
        """
        self._blank_sent.clear()
        self._pending.put(_Frame("blank", None))
        self._blank_sent.wait(timeout=1.0)

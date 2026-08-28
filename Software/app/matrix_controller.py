"""
LED matrix link state exposed to QML.

Owns the MatrixLink lifecycle on a background thread: connect, watch for the
selected port disappearing (device unplugged), reconnect. Artwork pushes go
through a single-slot "latest wins" mailbox rather than a FIFO queue -- a
burst of track changes (skip, skip, skip) should only ever push the most
recent artwork, not a backlog of stale frames the panel would flash through
after the fact.

The 64x64 downscale/re-encode (encoder.encode) happens in the worker thread,
not in the GUI-thread signal handler that receives new artwork, since it's
real CPU work (image decode/resize/JPEG re-encode).

The port is auto-detected by USB VID:PID (see matrix.link.find_matrix_port)
and re-detected on every connection attempt, rather than picked once and
persisted -- that's what lets the board be unplugged/replugged (which can
renumber /dev/ttyUSBn) and still be found on the next retry with no user
action.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import serial
from PySide6.QtCore import Property, QObject, Signal, Slot

from encoder import blank, encode
from matrix.link import MatrixLink, PayloadTooLarge, find_matrix_port

from .track_controller import TrackController

LOG = logging.getLogger(__name__)

RECONNECT_INTERVAL = 3.0  # seconds between connection attempts while offline
IDLE_POLL_INTERVAL = 1.0  # seconds the worker blocks waiting for new artwork


class _LatestSlot:
    """A one-item mailbox: put() always overwrites, get() blocks up to
    `timeout` and returns the most recent value (or None on timeout)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._value: Optional[bytes] = None
        self._has_value = False

    def put(self, value: bytes) -> None:
        with self._cond:
            self._value = value
            self._has_value = True
            self._cond.notify()

    def get(self, timeout: float) -> Optional[bytes]:
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

    def __init__(self, track: TrackController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._status = "disconnected"  # disconnected | connecting | connected | error
        self._port_name = ""  # last port auto-detected; re-resolved on every connect attempt
        self._error = ""

        self._pending = _LatestSlot()
        self._blank_sent = threading.Event()

        track.artworkChanged.connect(lambda: self._on_track_artwork(track))

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _on_track_artwork(self, track: TrackController) -> None:
        # b"" is the "push a blank frame" sentinel (see _run) -- artwork
        # clearing (explicit zero-length PICT, session end, bridge
        # disconnect) fires this same signal with artwork_bytes() now None,
        # and the panel needs to actually blank rather than keep showing
        # whatever it last received.
        data = track.artwork_bytes()
        self._pending.put(data if data else b"")

    # -- background thread --

    def _run(self) -> None:
        link: Optional[MatrixLink] = None
        while True:
            if link is None:
                link = self._try_connect()
                if link is None:
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            raw = self._pending.get(timeout=IDLE_POLL_INTERVAL)
            if raw is None:
                if find_matrix_port() is None:
                    self._set_status("disconnected", error="no matching USB device found")
                    link = self._close(link)
                continue

            is_blank = raw == b""
            if is_blank:
                artwork = blank()
            else:
                artwork = encode(raw)
                if artwork is None:
                    LOG.warning("artwork failed to encode for matrix push")
                    continue

            try:
                link.send_artwork(artwork)
                if is_blank:
                    self._blank_sent.set()
            except (PayloadTooLarge, serial.SerialException) as exc:
                LOG.warning("matrix push failed: %s", exc)
                self._set_status("error", error=str(exc))
                link = self._close(link)

    def _try_connect(self) -> Optional[MatrixLink]:
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
        self._pending.put(b"")
        self._blank_sent.wait(timeout=1.0)

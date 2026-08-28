#!/usr/bin/env python3
"""
matrix_daemon.py -- headless bridge from shairport-sync's metadata pipe to
the ESP32/HUB75 matrix, with no Qt/QML dependency.

This exists for hardware too weak to run the PySide6 "Desk Display" kiosk
app (app/main.py) -- notably the Raspberry Pi Zero W/WH, whose single-core
ARM11 has no realistic path to a smooth Qt Quick scene graph and for which
PySide6/Qt6 has no official armv6 build in the first place. Everything this
daemon does is a straight-line reuse of the same modules the GUI app uses
(metadata.py, encoder.py, matrix/link.py) minus the QObject/Signal plumbing
that exists only to feed QML -- there is no second implementation of the
matrix logic to keep in sync.

Behaviour, matching app/matrix_controller.py:
  * Reads the shairport-sync metadata FIFO (PipeSource) and folds it into a
    TrackTracker.
  * On every artwork change, encodes it to a 64x64 JPEG and pushes it to the
    ESP32 over serial. A "latest wins" single-slot mailbox means a burst of
    track changes only ever pushes the most recent artwork, never a backlog.
  * On session end (or artwork explicitly cleared), pushes a blank frame.
  * Auto-detects the ESP32's serial port by USB VID:PID and reconnects if
    it's unplugged/replugged.
  * Pushes one final blank frame on SIGTERM/SIGINT so the panel doesn't
    freeze on the last artwork when the service is stopped.

Run directly for testing:
    python3 -m matrix.matrix_daemon --pipe /tmp/shairport-sync-metadata

In production this is installed as the airplaymatrix-matrix.service unit
(see matrix/airplaymatrix-matrix.service in this directory).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import serial

_PARENT = str(Path(__file__).resolve().parent.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from encoder import blank, encode  # noqa: E402
from matrix.link import MatrixLink, PayloadTooLarge, find_matrix_port  # noqa: E402
from metadata import MetadataItem, PipeSource, TrackTracker  # noqa: E402

LOG = logging.getLogger("matrix_daemon")

RECONNECT_INTERVAL = 3.0  # seconds between matrix connection attempts while offline
IDLE_POLL_INTERVAL = 1.0  # seconds the matrix worker blocks waiting for new artwork
SHUTDOWN_BLANK_TIMEOUT = 1.0  # seconds to wait for a final blank frame on exit


class _LatestSlot:
    """A one-item mailbox: put() always overwrites, get() blocks up to
    `timeout` and returns the most recent value (or None on timeout).

    Same idea as app/matrix_controller.py's _LatestSlot, reimplemented here
    without the QObject dependency that file otherwise has no reason to
    carry.
    """

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


class MatrixDaemon:
    def __init__(self, pipe_path: str) -> None:
        self._pipe_path = pipe_path
        self._pending = _LatestSlot()
        self._blank_sent = threading.Event()
        self._stop = threading.Event()
        self._tracker = TrackTracker()
        self._session_active = False

    # -- metadata thread --

    def _metadata_loop(self) -> None:
        source = PipeSource(self._pipe_path)
        for item in source.items():
            if self._stop.is_set():
                return
            self._handle_item(item)

    def _handle_item(self, item: MetadataItem) -> None:
        changed = self._tracker.apply(item)

        if item.type == "ssnc" and item.code == "pbeg":
            self._session_active = True
        elif item.type == "ssnc" and item.code == "pend":
            self._session_active = False
            # pend carries no artwork-clear of its own (see
            # TrackController._handle_item) -- without this the last
            # track's art stays on the panel forever after the session ends.
            if self._tracker.state.artwork is not None:
                self._tracker.state.artwork = None
                changed.add("artwork")

        if "artwork" in changed:
            data = self._tracker.state.artwork
            self._pending.put(data if data else b"")

    # -- matrix (ESP32) thread --

    def _matrix_loop(self) -> None:
        link: Optional[MatrixLink] = None
        while not self._stop.is_set():
            if link is None:
                link = self._try_connect()
                if link is None:
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            raw = self._pending.get(timeout=IDLE_POLL_INTERVAL)
            if raw is None:
                if find_matrix_port() is None:
                    LOG.info("matrix disconnected: no matching USB device found")
                    link = self._close(link)
                continue

            is_blank = raw == b""
            artwork = blank() if is_blank else encode(raw)
            if artwork is None:
                LOG.warning("artwork failed to encode for matrix push")
                continue

            try:
                link.send_artwork(artwork)
                if is_blank:
                    self._blank_sent.set()
            except (PayloadTooLarge, serial.SerialException) as exc:
                LOG.warning("matrix push failed: %s", exc)
                link = self._close(link)

        # Final best-effort blank frame on shutdown.
        if link is not None:
            try:
                link.send_artwork(blank())
            except (PayloadTooLarge, serial.SerialException):
                pass
            self._close(link)

    @staticmethod
    def _try_connect() -> Optional[MatrixLink]:
        port = find_matrix_port()
        if not port:
            return None
        LOG.info("connecting to matrix on %s", port)
        try:
            link = MatrixLink(port)
        except serial.SerialException as exc:
            LOG.warning("matrix connect failed: %s", exc)
            return None
        LOG.info("matrix connected")
        return link

    @staticmethod
    def _close(link: Optional[MatrixLink]) -> None:
        if link is not None:
            link.close()
        return None

    # -- lifecycle --

    def run(self) -> None:
        metadata_thread = threading.Thread(target=self._metadata_loop, daemon=True)
        matrix_thread = threading.Thread(target=self._matrix_loop, daemon=True)
        metadata_thread.start()
        matrix_thread.start()

        def _handle_signal(signum: int, _frame: object) -> None:
            LOG.info("received signal %d, shutting down", signum)
            self._blank_sent.clear()
            self._pending.put(b"")
            self._blank_sent.wait(timeout=SHUTDOWN_BLANK_TIMEOUT)
            self._stop.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        while not self._stop.is_set():
            time.sleep(0.5)
        matrix_thread.join(timeout=SHUTDOWN_BLANK_TIMEOUT + 1.0)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipe", default="/tmp/shairport-sync-metadata")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    MatrixDaemon(args.pipe).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

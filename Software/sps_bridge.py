#!/usr/bin/env python3
"""
shairport-sync metadata bridge.

Runs inside WSL (or on the Pi, where it is optional) and re-serves the raw
metadata pipe as a TCP stream so that a client on another host — or on the
Windows side of a WSL boundary — can consume it.

The pipe is opened O_RDONLY and blocks until shairport-sync opens the write
end. When shairport-sync exits it closes the pipe, producing EOF; the reader
loop reopens rather than terminating, so the bridge survives receiver restarts.

The item stream is forwarded byte-for-byte. No parsing happens here: the
parser lives on the client so that pipe-mode and TCP-mode sources are fed by
identical code.

Usage:
    python3 sps_bridge.py --pipe /tmp/shairport-sync-metadata --port 5555
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import selectors
import socket
import threading
import time
from typing import Set

LOG = logging.getLogger("sps_bridge")

# Artwork items are large; anything smaller than a few kB causes excessive
# syscall churn on PICT delivery.
READ_CHUNK = 65536

# Bound the per-client queue so a stalled consumer cannot pin unbounded memory
# holding artwork blobs. On overflow the client is dropped and must reconnect.
CLIENT_BUFFER_LIMIT = 4 * 1024 * 1024


class ClientSet:
    """Thread-safe fan-out to connected TCP consumers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: Set[socket.socket] = set()

    def add(self, sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._clients.add(sock)
        LOG.info("client connected (%d total)", len(self._clients))

    def discard(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            targets = list(self._clients)
        for sock in targets:
            try:
                sock.sendall(data)
            except OSError as exc:
                LOG.info("dropping client: %s", exc)
                self.discard(sock)


def accept_loop(listener: socket.socket, clients: ClientSet) -> None:
    while True:
        try:
            sock, _addr = listener.accept()
        except OSError as exc:
            LOG.error("accept failed: %s", exc)
            time.sleep(0.5)
            continue
        clients.add(sock)


def open_pipe(path: str) -> int:
    """Open the FIFO for reading, waiting for it to exist if necessary."""
    while True:
        try:
            return os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            LOG.warning("metadata pipe %s not present; waiting", path)
            time.sleep(2.0)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise


def pipe_loop(path: str, clients: ClientSet) -> None:
    sel = selectors.DefaultSelector()
    while True:
        fd = open_pipe(path)
        LOG.info("metadata pipe opened")
        sel.register(fd, selectors.EVENT_READ)
        try:
            while True:
                sel.select()
                data = os.read(fd, READ_CHUNK)
                if not data:
                    # Writer closed. shairport-sync has stopped or restarted.
                    LOG.info("metadata pipe EOF; reopening")
                    break
                clients.broadcast(data)
        finally:
            sel.unregister(fd)
            os.close(fd)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipe", default="/tmp/shairport-sync-metadata")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(8)
    LOG.info("bridge listening on %s:%d", args.host, args.port)

    clients = ClientSet()
    threading.Thread(
        target=accept_loop, args=(listener, clients), daemon=True
    ).start()
    pipe_loop(args.pipe, clients)


if __name__ == "__main__":
    main()

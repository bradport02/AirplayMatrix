"""
shairport-sync metadata pipe parsing and track state tracking.

The pipe carries a stream of pseudo-XML items, not a single document:

    <item><type>73736e63</type><code>70726772</code><length>19</length>
    <data encoding="base64">
    MTIzNC81Njc4LzkwMTI=</data></item>

`type` and `code` are the hex encoding of a four-byte ASCII identifier.
Two types are relevant:

    core (0x636f7265)  DAAP/DMAP track metadata forwarded from the sender
    ssnc (0x73736e63)  shairport-sync's own session and artwork events

Zero-length items omit the <data> element entirely.

This module deliberately parses at the byte level rather than with an XML
parser: the stream has no root element, PICT payloads reach several hundred
kilobytes, and items arrive incrementally, so an incremental scan for the
closing tag is both simpler and faster than feeding a SAX parser.
"""

from __future__ import annotations

import abc
import base64
import binascii
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

LOG = logging.getLogger(__name__)

# AirPlay progress values are expressed in RTP frames at a fixed 44.1 kHz
# clock regardless of the source material's sample rate.
RTP_FRAME_RATE = 44100.0

_ITEM_RE = re.compile(
    rb"<item>"
    rb"<type>([0-9a-fA-F]{8})</type>"
    rb"<code>([0-9a-fA-F]{8})</code>"
    rb"<length>(\d+)</length>"
    rb"(?:\s*<data encoding=\"base64\">\s*(.*?)</data>)?"
    rb"</item>",
    re.DOTALL,
)

# Cap the reassembly buffer so a malformed or truncated stream cannot grow it
# without bound. Sized to comfortably exceed the largest realistic PICT item.
MAX_BUFFER = 8 * 1024 * 1024


@dataclass(frozen=True)
class MetadataItem:
    """A single decoded item from the metadata stream."""

    type: str  # four-character type, e.g. "ssnc"
    code: str  # four-character code, e.g. "prgr"
    data: bytes

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _fourcc(hex_bytes: bytes) -> str:
    try:
        return binascii.unhexlify(hex_bytes).decode("ascii", errors="replace")
    except binascii.Error:
        return "????"


class ItemParser:
    """Incremental parser. Feed arbitrary byte chunks, get complete items out."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Iterator[MetadataItem]:
        self._buf.extend(chunk)

        if len(self._buf) > MAX_BUFFER:
            # Retain only the tail; a partial item is unrecoverable anyway and
            # the next <item> boundary will resynchronise the stream.
            LOG.warning("metadata buffer overflow, resynchronising")
            del self._buf[: len(self._buf) - MAX_BUFFER // 2]

        consumed = 0
        for match in _ITEM_RE.finditer(self._buf):
            consumed = match.end()
            payload = match.group(4)
            data = b""
            if payload:
                try:
                    data = base64.b64decode(payload, validate=False)
                except binascii.Error:
                    LOG.warning("undecodable base64 payload, item dropped")
                    continue
            yield MetadataItem(
                type=_fourcc(match.group(1)),
                code=_fourcc(match.group(2)),
                data=data,
            )

        if consumed:
            del self._buf[:consumed]


class MetadataSource(abc.ABC):
    """Byte-stream source feeding the parser. Subclasses supply transport only."""

    def __init__(self) -> None:
        self._parser = ItemParser()
        self.connected = False

    @abc.abstractmethod
    def _read(self) -> bytes:
        """Return the next chunk, or b'' on end of stream."""

    @abc.abstractmethod
    def _reconnect(self) -> None:
        """Re-establish the transport after end of stream."""

    def items(self) -> Iterator[MetadataItem]:
        # The constructor's own _reconnect() has already succeeded by the
        # time items() is ever called, so `connected` starts true here --
        # it tracks the transport being established, not whether data has
        # arrived yet. An idle-but-connected source (bridge up, nothing
        # streaming) must not read as disconnected.
        self.connected = True
        while True:
            try:
                chunk = self._read()
            except OSError as exc:
                LOG.warning("source read failed: %s", exc)
                chunk = b""
            if not chunk:
                self.connected = False
                self._parser = ItemParser()  # discard any partial item
                time.sleep(1.0)
                self._reconnect()
                self.connected = True
                continue
            yield from self._parser.feed(chunk)


class PipeSource(MetadataSource):
    """Reads the FIFO directly. Used on the Pi, where the app is co-hosted."""

    def __init__(self, path: str = "/tmp/shairport-sync-metadata") -> None:
        super().__init__()
        self._path = path
        self._fd: Optional[int] = None
        self._reconnect()

    def _reconnect(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        while self._fd is None:
            try:
                self._fd = os.open(self._path, os.O_RDONLY)
            except OSError as exc:
                LOG.warning("cannot open %s: %s", self._path, exc)
                time.sleep(2.0)

    def _read(self) -> bytes:
        assert self._fd is not None
        return os.read(self._fd, 65536)


class TcpSource(MetadataSource):
    """Consumes sps_bridge.py. Used on Windows against shairport-sync in WSL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5555) -> None:
        super().__init__()
        self._addr = (host, port)
        self._sock: Optional[socket.socket] = None
        self._reconnect()

    def _reconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        while self._sock is None:
            try:
                self._sock = socket.create_connection(self._addr, timeout=5.0)
                self._sock.settimeout(None)
                LOG.info("connected to metadata bridge at %s:%d", *self._addr)
            except OSError as exc:
                LOG.warning("bridge connect failed: %s", exc)
                time.sleep(2.0)

    def _read(self) -> bytes:
        assert self._sock is not None
        return self._sock.recv(65536)


@dataclass
class TrackState:
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    duration: float = 0.0  # seconds
    artwork: Optional[bytes] = None  # raw JPEG or PNG as delivered
    artwork_revision: int = 0  # increments on every new PICT
    playing: bool = False
    volume: Optional[float] = None  # AirPlay volume, -30.0..0.0 dB, or None

    _anchor_wall: float = field(default=0.0, repr=False)
    _anchor_pos: float = field(default=0.0, repr=False)

    def position(self) -> float:
        """Dead-reckoned playback position in seconds.

        shairport-sync emits `prgr` only at track start, seek and resume, so a
        continuous position must be extrapolated locally and re-anchored each
        time a new progress item arrives. Never poll for this — there is no
        query interface.
        """
        if not self.playing:
            return self._anchor_pos
        elapsed = time.monotonic() - self._anchor_wall
        pos = self._anchor_pos + elapsed
        return min(pos, self.duration) if self.duration else pos

    def anchor(self, position: float) -> None:
        self._anchor_pos = max(0.0, position)
        self._anchor_wall = time.monotonic()

    def identity(self) -> tuple[str, str, str]:
        """Key used to decide whether the track has actually changed."""
        return (self.artist, self.title, self.album)


class TrackTracker:
    """Folds the item stream into a TrackState.

    `apply` returns a set of changed field names so the UI layer can emit
    targeted property-change notifications rather than refreshing wholesale.
    """

    def __init__(self) -> None:
        self.state = TrackState()

    def apply(self, item: MetadataItem) -> set[str]:
        changed: set[str] = set()
        st = self.state

        if item.type == "core":
            mapping = {
                "minm": "title",
                "asar": "artist",
                "asal": "album",
                "asgn": "genre",
            }
            attr = mapping.get(item.code)
            if attr:
                value = item.text()
                if getattr(st, attr) != value:
                    setattr(st, attr, value)
                    changed.add(attr)
            elif item.code == "astm":
                # DAAP song time, milliseconds, big-endian u32.
                if len(item.data) == 4:
                    duration = int.from_bytes(item.data, "big") / 1000.0
                    if abs(duration - st.duration) > 0.5:
                        st.duration = duration
                        changed.add("duration")

        elif item.type == "ssnc":
            if item.code == "PICT":
                # A zero-length PICT means the sender has no artwork for this
                # track; treat it as an explicit clear, not as a no-op.
                st.artwork = item.data or None
                st.artwork_revision += 1
                changed.update({"artwork", "artwork_revision"})

            elif item.code == "prgr":
                parsed = self._parse_progress(item.text())
                if parsed is not None:
                    position, duration = parsed
                    st.anchor(position)
                    st.playing = True
                    changed.add("position")
                    # Prefer astm when present; prgr end-of-stream can lag on
                    # variable-bitrate sources and drift by a second or two.
                    if st.duration <= 0.0 and duration > 0.0:
                        st.duration = duration
                        changed.add("duration")

            elif item.code in ("pbeg", "prsm"):
                if not st.playing:
                    st.playing = True
                    st.anchor(st.position())
                    changed.add("playing")

            elif item.code in ("pend", "pfls"):
                # pfls is a flush, which is what pause looks like on the wire.
                if st.playing:
                    st.anchor(st.position())
                    st.playing = False
                    changed.add("playing")
                if item.code == "pend":
                    st.anchor(0.0)

            elif item.code == "pvol":
                parsed = self._parse_volume(item.text())
                if parsed is not None and parsed != st.volume:
                    st.volume = parsed
                    changed.add("volume")

        return changed

    @staticmethod
    def _parse_progress(text: str) -> Optional[tuple[float, float]]:
        parts = text.strip().split("/")
        if len(parts) != 3:
            return None
        try:
            start, current, end = (int(p) for p in parts)
        except ValueError:
            return None
        position = (current - start) / RTP_FRAME_RATE
        duration = (end - start) / RTP_FRAME_RATE
        return max(0.0, position), max(0.0, duration)

    @staticmethod
    def _parse_volume(text: str) -> Optional[float]:
        # Format: "airplay_volume,volume,lowest_volume,highest_volume".
        # -144.0 is the mute sentinel.
        try:
            return float(text.split(",")[0])
        except (ValueError, IndexError):
            return None

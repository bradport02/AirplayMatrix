"""
Synced lyrics retrieval from LRCLIB.

AirPlay carries no lyric data of any kind, so lyrics must be resolved out of
band from the track identity. LRCLIB is keyed on artist, title, album and
duration; the duration match is what disambiguates radio edits, remasters and
live versions, so pass it whenever `astm` or `prgr` has supplied one.

The service is community-contributed, so expect misses on obscure and live
material. `get` is an exact lookup and returns 404 freely; `search` is the
fallback and is fuzzier, hence the local duration filter applied to its
results.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger(__name__)

API_ROOT = "https://lrclib.net/api"

# LRCLIB asks clients to identify themselves. Replace the URL with the
# project's own before any wider distribution.
USER_AGENT = "airplay-desk-display/0.1 (https://github.com/yourname/airplay-desk)"

# A match is rejected if its duration differs from the AirPlay-reported
# duration by more than this. Two seconds absorbs encoder padding and the
# rounding in prgr without admitting a different arrangement of the track.
DURATION_TOLERANCE = 2.0

_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.MULTILINE)


@dataclass(frozen=True)
class LyricLine:
    time: float  # seconds from track start
    text: str


class SyncedLyrics:
    """Parsed LRC with an O(log n) lookup by playback position."""

    def __init__(self, lines: list[LyricLine], offset: float = 0.0) -> None:
        self.lines = sorted(lines, key=lambda ln: ln.time)
        self.offset = offset
        self._times = [ln.time for ln in self.lines]

    def __bool__(self) -> bool:
        return bool(self.lines)

    def index_at(self, position: float) -> int:
        """Index of the active line, or -1 before the first timestamp."""
        return bisect_right(self._times, position + self.offset) - 1

    def line_at(self, position: float) -> Optional[LyricLine]:
        idx = self.index_at(position)
        return self.lines[idx] if idx >= 0 else None


def parse_lrc(text: str) -> SyncedLyrics:
    """Parse LRC into timestamped lines.

    Handles the compressed form where one lyric carries several timestamps
    (used for repeated choruses), and honours the [offset:] tag, whose sign
    convention is that a positive value means the lyrics should appear
    earlier — hence the negation below.
    """
    offset = 0.0
    match = _OFFSET_RE.search(text)
    if match:
        offset = int(match.group(1)) / 1000.0

    lines: list[LyricLine] = []
    for raw in text.splitlines():
        stamps = list(_TIMESTAMP_RE.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes = int(stamp.group(1))
            seconds = int(stamp.group(2))
            frac_raw = stamp.group(3) or "0"
            # Two-digit fractions are centiseconds, three-digit milliseconds.
            frac = int(frac_raw) / (100.0 if len(frac_raw) <= 2 else 1000.0)
            lines.append(LyricLine(minutes * 60 + seconds + frac, content))

    return SyncedLyrics(lines, offset)


def _request(path: str, params: dict[str, str]) -> Optional[list | dict]:
    url = f"{API_ROOT}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            import json

            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            LOG.warning("lrclib %s returned %d", path, exc.code)
        return None
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        LOG.warning("lrclib %s failed: %s", path, exc)
        return None


def fetch(
    artist: str,
    title: str,
    album: str = "",
    duration: float = 0.0,
) -> Optional[SyncedLyrics]:
    """Resolve synced lyrics, exact lookup first then a filtered search."""
    if not artist or not title:
        return None

    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration > 0:
        params["duration"] = str(int(round(duration)))

    record = _request("get", params)
    if isinstance(record, dict) and record.get("syncedLyrics"):
        return parse_lrc(record["syncedLyrics"])

    results = _request("search", {"artist_name": artist, "track_name": title})
    if not isinstance(results, list):
        return None

    for candidate in results:
        if not candidate.get("syncedLyrics"):
            continue
        if duration > 0:
            candidate_duration = candidate.get("duration") or 0
            if abs(candidate_duration - duration) > DURATION_TOLERANCE:
                continue
        return parse_lrc(candidate["syncedLyrics"])

    return None


class LyricsFetcher:
    """Off-thread wrapper. Late results for superseded tracks are discarded."""

    def __init__(self, on_result) -> None:
        self._on_result = on_result  # callable(key, SyncedLyrics | None)
        self._lock = threading.Lock()
        self._current_key: Optional[tuple[str, str, str]] = None

    def request(self, key: tuple[str, str, str], duration: float) -> None:
        with self._lock:
            if key == self._current_key:
                return
            self._current_key = key

        artist, title, album = key

        def worker() -> None:
            result = fetch(artist, title, album, duration)
            with self._lock:
                if key != self._current_key:
                    return  # track changed while the request was in flight
            self._on_result(key, result)

        threading.Thread(target=worker, daemon=True).start()

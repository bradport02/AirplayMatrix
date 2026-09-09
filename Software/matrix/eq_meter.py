#!/usr/bin/env python3
"""
Live audio levels for the LED matrix's "EQ meter" display mode
(Software/display_settings.py's eq_meter_enabled toggle).

Where the audio comes from: shairport-sync's own ALSA output plays to a
`multi` ALSA device (see docs/install-pi-zero-wh.md's EQ meter section, and
the asound.conf stanza the eq-meter-setup script installs) that fans the
exact same PCM out to two places at once -- the real hardware output the TV
actually hears, and one side of a `snd-aloop` kernel loopback. This module
only ever reads the *other* side of that loopback (via a plain `arecord`
subprocess, piped into Python) -- it never touches the real audio path at
all, so a bug here can make the meter wrong or blank but can't affect what's
actually played, and shairport-sync needs no code changes or restarts to
support it.

Level computation is a small FFT (numpy.fft.rfft, no scipy needed) over the
last ~1024 samples, grouped into a handful of log-spaced frequency bands
(bass through treble aren't evenly spaced on a linear Hz axis, and a linear
grouping would dump almost the entire visible spectrum's energy into the
first band or two) -- not a full spectrum analyser, just enough bins to
paint a classic bar-graph "EQ meter" look on a 64-wide panel. Deliberately
not a filter-bank/biquad design (the more usual choice on hardware this
weak): the Zero WH's single ARM11 core already has numpy available as a
system package, and one small FFT a tick (this module's __main__ block can
be used to sanity-check the rate live) is cheap next to the JPEG-encode and
serial-write already happening for every frame regardless of source.

This module has no Qt dependency, same as encoder.py and matrix/link.py --
the Qt-aware glue (a QTimer calling bands()/render_bars() and pushing the
result at MatrixController) lives in app/eq_controller.py and
app_qt5/eq_controller.py.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)

LOOPBACK_DEVICE = "hw:Loopback,1,0"  # capture side; playback side is Loopback,0,0
SAMPLE_RATE = 44100  # matches shairport-sync's own ALSA output format
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # S16_LE
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE

WINDOW_FRAMES = 1024  # ~23ms at 44.1kHz -- enough frequency resolution down
                       # into the bass without the FFT itself costing much
RING_FRAMES = WINDOW_FRAMES * 2  # slack so a read never blocks on a partial window

# Log-spaced band edges (Hz), tuned for a 16-bar display: dense in the bass/
# midrange where most music's visible movement lives, sparser in the
# treble where a single wide band looks no different from three narrow ones
# on a 64-pixel panel anyway.
BAND_EDGES_HZ = [
    40, 62, 100, 160, 250, 400, 630, 1000, 1600, 2500,
    4000, 6300, 10000, 14000, 18000, 20000, 22000,
]
N_BANDS = len(BAND_EDGES_HZ) - 1

ATTACK = 0.6   # how fast a band jumps up towards a louder reading
DECAY = 0.15   # how fast it falls back towards a quieter one -- slower than
               # attack so bars fall like a real VU meter, not flicker like
               # raw FFT noise would

RESPAWN_COOLDOWN = 2.0  # seconds between arecord respawn attempts if it dies

# Precomputed once at import time, not per tick: the Hann window, the FFT's
# frequency axis, and (since both only ever depend on WINDOW_FRAMES/
# SAMPLE_RATE/BAND_EDGES_HZ, all fixed) each band's boolean mask into that
# axis. None of this changes between calls, and recomputing the masks alone
# would otherwise be N_BANDS numpy comparisons against a 513-element array
# every single tick for no reason.
_HANN_WINDOW = np.hanning(WINDOW_FRAMES).astype(np.float32)
_FREQS = np.fft.rfftfreq(WINDOW_FRAMES, d=1.0 / SAMPLE_RATE)
_BAND_MASKS = [
    (_FREQS >= lo) & (_FREQS < hi) for lo, hi in zip(BAND_EDGES_HZ[:-1], BAND_EDGES_HZ[1:])
]

# What a Hann-windowed rfft's magnitude reads for a full-scale (0dBFS) single
# sine tone landing cleanly in one bin: a Hann window's coherent gain is
# ~0.5, and a single-sided (rfft) spectrum halves that again, so the peak
# bin's magnitude comes out to roughly amplitude * N / 4. Used only to turn
# raw FFT magnitude (which spans several orders of magnitude between
# silence and a loud drum hit, and depends on WINDOW_FRAMES) into a dBFS-
# like number _update_levels can sanely clamp to a fixed -60..0 range --
# without this reference the same -60..0 mapping would either clip
# everything to 1.0 or show nothing at all, depending on window size,
# rather than tracking actual loudness.
_FULL_SCALE_REF = 32768.0 * WINDOW_FRAMES / 4.0


class EqCapture:
    """Owns the arecord subprocess + rolling PCM buffer + band smoothing.

    Started/stopped by the app-level EqController in lockstep with
    display_settings.py's eq_meter_enabled toggle -- there's no reason to
    hold the loopback device open, or run arecord at all, while artwork
    mode is showing.
    """

    def __init__(self, device: str = LOOPBACK_DEVICE) -> None:
        self._device = device
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._levels = [0.0] * N_BANDS
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_spawn_attempt = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._spawn()
        self._reader_thread = threading.Thread(target=self._run, daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_proc()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    def _spawn(self) -> None:
        self._last_spawn_attempt = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                [
                    "arecord", "-D", self._device, "-f", "S16_LE",
                    "-r", str(SAMPLE_RATE), "-c", str(CHANNELS), "-t", "raw", "-q", "-",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            LOG.warning("could not start arecord for EQ capture: %s", exc)
            self._proc = None

    def _kill_proc(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def _run(self) -> None:
        chunk_frames = WINDOW_FRAMES // 4  # a few reads per window, for responsiveness
        chunk_bytes = chunk_frames * FRAME_BYTES
        while not self._stop.is_set():
            if self._proc is None or self._proc.stdout is None:
                if time.monotonic() - self._last_spawn_attempt >= RESPAWN_COOLDOWN:
                    self._spawn()
                else:
                    time.sleep(0.2)
                continue
            try:
                chunk = self._proc.stdout.read(chunk_bytes)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                # arecord died, or the loopback isn't there at all (snd-aloop
                # not loaded, or the EQ setup script never ran on this
                # device) -- decay to silence rather than freezing on
                # whatever the last real reading was, and try respawning.
                self._kill_proc()
                with self._lock:
                    self._levels = [lv * (1 - DECAY) for lv in self._levels]
                time.sleep(0.2)
                continue
            with self._lock:
                self._buffer.extend(chunk)
                max_bytes = RING_FRAMES * FRAME_BYTES
                if len(self._buffer) > max_bytes:
                    del self._buffer[: len(self._buffer) - max_bytes]
                self._update_levels()

    def _update_levels(self) -> None:
        """Caller already holds self._lock."""
        window_bytes = WINDOW_FRAMES * FRAME_BYTES
        if len(self._buffer) < window_bytes:
            return
        raw = bytes(self._buffer[-window_bytes:])
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        mono = samples.reshape(-1, CHANNELS).mean(axis=1) * _HANN_WINDOW
        spectrum = np.abs(np.fft.rfft(mono))

        for i, mask in enumerate(_BAND_MASKS):
            magnitude = float(spectrum[mask].mean()) if mask.any() else 0.0
            # dBFS-like value relative to _FULL_SCALE_REF, then -60..0dBFS
            # mapped to 0..1: log-compressed because a 16-bit FFT bin's
            # magnitude spans many orders of magnitude between "silence" and
            # "full-scale drum hit", and relative to a fixed reference so
            # the mapping means the same thing regardless of window size or
            # how loud a "loud" band happens to be in absolute FFT units.
            db = 20.0 * np.log10(magnitude / _FULL_SCALE_REF + 1e-9)
            target = max(0.0, min(1.0, (db + 60.0) / 60.0))
            rate = ATTACK if target > self._levels[i] else DECAY
            self._levels[i] += (target - self._levels[i]) * rate

    def bands(self) -> List[float]:
        with self._lock:
            return list(self._levels)


# -- rendering ----------------------------------------------------------------

MATRIX_SIZE = 64

# Classic VU-meter gradient: green low, amber mid, red only right at the top
# of a bar -- the same idea as a hardware LED bar-graph driver in "VU" mode
# (e.g. an LM3915), not a single flat colour per bar.
_GRADIENT_STOPS: List[Tuple[float, Tuple[int, int, int]]] = [
    (0.0, (40, 220, 90)),
    (0.7, (240, 200, 40)),
    (0.9, (235, 60, 50)),
]


def _gradient_color(row_fraction: float) -> Tuple[int, int, int]:
    for (pos, color), (next_pos, next_color) in zip(_GRADIENT_STOPS, _GRADIENT_STOPS[1:]):
        if row_fraction <= next_pos:
            t = (row_fraction - pos) / (next_pos - pos) if next_pos > pos else 0.0
            return tuple(int(a + (b - a) * t) for a, b in zip(color, next_color))
    return _GRADIENT_STOPS[-1][1]


def render_bars(levels: List[float], size: int = MATRIX_SIZE) -> Image.Image:
    """Render band levels (0..1 each) as a vertical bar graph, evenly spaced
    across a `size`x`size` black frame -- one bar per band, gap between bars
    proportional to bar width so it still reads cleanly whatever N_BANDS is."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    px = img.load()
    n = len(levels)
    if n == 0:
        return img
    bar_width = size / n
    gap = max(1, int(bar_width * 0.15))
    for i, level in enumerate(levels):
        level = max(0.0, min(1.0, level))
        bar_height = int(round(level * size))
        if bar_height <= 0:
            continue
        x0 = int(round(i * bar_width))
        x1 = max(x0 + 1, int(round((i + 1) * bar_width)) - gap)
        for y in range(size - bar_height, size):
            color = _gradient_color((size - y) / size)
            for x in range(x0, min(x1, size)):
                px[x, y] = color
    return img


def _main() -> int:
    """Manual on-device check: `python3 -m matrix.eq_meter` from Software/,
    with the EQ setup script's asound.conf + snd-aloop already in place and
    something actually playing over AirPlay. Prints each band as a row of
    #s so the meter can be sanity-checked over SSH with no display or
    matrix hardware needed."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    capture = EqCapture()
    capture.start()
    try:
        while True:
            time.sleep(0.15)
            bars = capture.bands()
            row = " ".join(f"{b:0.2f}" for b in bars)
            print(row)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

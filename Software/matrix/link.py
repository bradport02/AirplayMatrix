#!/usr/bin/env python3
"""
Serial driver for the ESP32 HUB75 matrix link.

Talks to the deployed firmware (`AirplayMatrix/src/main.cpp`, PlatformIO env
esp32dev) over its default `Serial` object -- UART0, exposed as a normal COM
port through the board's USB-UART bridge, at 2,000,000 baud. This is not what
CLAUDE.md's constraints section describes (UART2 @ 921600); that text is
stale against the sketch as committed and should be corrected separately.
The firmware is authoritative here and is not being changed.

The wire protocol, as implemented by the sketch:

  * No request/ack layer. The firmware is a passive sink: whatever base64
    text arrives gets decoded a character at a time and rendered once a
    frame boundary is seen. There is no framing beyond that -- no start
    marker, no length prefix, no command opcodes.
  * A frame is opened by the first valid base64 character and closed by a
    CR or LF, or by 300 ms of silence (the firmware's own idle-flush), so a
    dropped trailing newline is not fatal.
  * The decoded-byte ceiling is 24576 (MAX_PAYLOAD in the sketch). Exceeding
    it sets an overflow flag and the frame is discarded without drawing.
  * There is no checksum or CRC. A corrupted-but-still-valid base64 byte
    silently corrupts the frame; nothing on the wire will report that.
  * The firmware writes free-text diagnostic lines back over the same port
    ("OK drawn at 1/N", "ERROR: ...", etc). Reading them back is
    best-effort logging, not a real acknowledgement -- treat their absence
    as "unknown", not "failed".
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import serial
from serial.tools import list_ports as _list_ports

_PARENT = str(Path(__file__).resolve().parent.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from encoder import RESAMPLE_MODES, EncodedArtwork, encode  # noqa: E402

LOG = logging.getLogger(__name__)

BAUD = 2_000_000
MAX_PAYLOAD = 24576  # firmware MAX_PAYLOAD: decoded-byte ceiling, not base64 length
BOOT_READY_TIMEOUT = 3.0  # most USB-UART bridges reset the chip on port open
RESPONSE_TIMEOUT = 0.5  # window to collect the firmware's status line(s)

# VID:PID of the CH340 USB-UART bridge on the deployed ESP32 board, read off
# the actual hardware with `python3 -m serial.tools.list_ports -v`. It's a
# generic bridge chip (also used by plenty of other boards/dongles), so this
# is "the board we ship", not a guarantee of uniqueness -- see
# find_matrix_port().
MATRIX_VID = 0x1A86
MATRIX_PID = 0x7523


class PayloadTooLarge(ValueError):
    pass


def list_ports() -> List[str]:
    return [p.device for p in _list_ports.comports()]


def find_matrix_port() -> Optional[str]:
    """Auto-detect the matrix's serial port by USB VID:PID.

    Returns the first match, or None if nothing matching is plugged in. If
    more than one turns up (e.g. a second board, or an unrelated device that
    happens to share the same generic bridge chip), that's logged so it's
    not a silent surprise, and the first one (by whatever order the OS
    enumerates them in) is used.
    """
    matches = [
        p.device
        for p in _list_ports.comports()
        if p.vid == MATRIX_VID and p.pid == MATRIX_PID
    ]
    if len(matches) > 1:
        LOG.warning(
            "multiple devices match the matrix's VID:PID (%s); using %s",
            ", ".join(matches), matches[0],
        )
    return matches[0] if matches else None


class MatrixLink:
    """Serial connection to the ESP32 HUB75 driver."""

    def __init__(
        self,
        port: str,
        baud: int = BAUD,
        timeout: float = 1.0,
        await_boot: bool = True,
    ) -> None:
        self._serial = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        if await_boot:
            self._await_ready()

    def close(self) -> None:
        self._serial.close()

    def __enter__(self) -> "MatrixLink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _await_ready(self, timeout: float = BOOT_READY_TIMEOUT) -> bool:
        """Wait for the boot banner after opening the port.

        Opening the port typically toggles DTR/RTS on the USB-UART bridge and
        resets the chip, so writing immediately can race the reboot. This is
        best-effort: some boards don't wire auto-reset, so absence of "Ready"
        is not treated as an error.
        """
        deadline = time.monotonic() + timeout
        self._serial.timeout = 0.2
        while time.monotonic() < deadline:
            line = self._serial.readline().decode("ascii", errors="replace").strip()
            if line == "Ready":
                return True
        LOG.debug("no boot banner seen within %.1fs; proceeding anyway", timeout)
        return False

    def send_artwork(
        self, artwork: EncodedArtwork, wait_for_response: float = RESPONSE_TIMEOUT
    ) -> List[str]:
        if len(artwork.jpeg) > MAX_PAYLOAD:
            raise PayloadTooLarge(
                f"{len(artwork.jpeg)} decoded bytes exceeds the firmware's "
                f"{MAX_PAYLOAD}-byte MAX_PAYLOAD"
            )
        return self._send(artwork.b64.encode("ascii"), wait_for_response)

    def _send(self, b64_payload: bytes, wait_for_response: float) -> List[str]:
        self._serial.reset_input_buffer()
        self._serial.write(b64_payload + b"\n")
        self._serial.flush()
        return self._read_response(wait_for_response)

    def _read_response(self, timeout: float) -> List[str]:
        """Collect diagnostic lines the firmware logs after rendering a frame.

        Best-effort only -- see module docstring. Stops early on the line
        that always terminates a frame's log output ("OK ..." / "ERROR ...").
        """
        lines: List[str] = []
        deadline = time.monotonic() + timeout
        self._serial.timeout = 0.1
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").rstrip("\r\n")
            if line:
                lines.append(line)
            if line.startswith("OK") or line.startswith("ERROR"):
                break
        return lines


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Push artwork to the HUB75 matrix over serial.")
    ap.add_argument("image", nargs="?", type=Path, help="JPEG or PNG file to push")
    ap.add_argument("--port", help="COM port, e.g. COM5")
    ap.add_argument("--list-ports", action="store_true", help="list available serial ports and exit")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--resample", choices=sorted(RESAMPLE_MODES), default="lanczos")
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.list_ports:
        ports = list_ports()
        if not ports:
            print("no serial ports found")
        for p in ports:
            print(p)
        return 0

    if not args.image or not args.port:
        parser.error("image path and --port are required unless --list-ports is given")

    data = args.image.read_bytes()
    artwork = encode(data, quality=args.quality, resample=args.resample)
    if artwork is None:
        LOG.error("failed to decode/encode %s", args.image)
        return 1

    LOG.info(
        "encoded %s: %d byte JPEG @ quality %d, %d base64 chars",
        args.image,
        len(artwork.jpeg),
        artwork.quality,
        len(artwork.b64),
    )

    try:
        with MatrixLink(args.port, baud=args.baud) as link:
            lines = link.send_artwork(artwork)
    except PayloadTooLarge as exc:
        LOG.error("%s", exc)
        return 1
    except serial.SerialException as exc:
        LOG.error("serial error: %s", exc)
        return 1

    if lines:
        for line in lines:
            LOG.info("device: %s", line)
    else:
        LOG.warning("no response from device within timeout (frame may still have rendered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

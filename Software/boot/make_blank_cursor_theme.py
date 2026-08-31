#!/usr/bin/env python3
"""Builds a minimal, fully-transparent Xcursor theme named "Blank" under
~/.local/share/icons/Blank -- used to hide the compositor's default arrow
cursor on the AirplayMatrix Zero WH kiosk during the gap between labwc
starting and the Qt app's own setOverrideCursor(BlankCursor) call taking
effect (see app_qt5/main.py's comment on why the app hides its own cursor
-- this covers the moment before that code has even run yet).

Writes one hand-built Xcursor binary file (X.Org's well-documented,
stable format -- a small table of contents followed by ARGB image chunks)
containing a single 24x24 fully-transparent (alpha=0) image, then copies
it under every cursor name a compositor/client is likely to request for
the plain idle pointer. No xcursorgen dependency (not packaged for this
device's 32-bit armhf archive) -- just Python's stdlib struct module.
"""
from __future__ import annotations

import os
import struct

THEME_NAME = "Blank"
DEST = os.path.expanduser(f"~/.local/share/icons/{THEME_NAME}")
CURSORS_DIR = os.path.join(DEST, "cursors")

# Names a wlroots compositor (labwc) or an XWayland Qt client is likely to
# request for the plain, non-hovering idle pointer. All map to the same
# blank image -- there's nothing to distinguish since every one of them
# should just render as nothing.
CURSOR_NAMES = [
    "default",
    "left_ptr",
    "top_left_arrow",
    "arrow",
    "pointer",
]

SIZE = 24  # matches XCURSOR_SIZE set in labwc/environment


def build_blank_xcursor(size: int) -> bytes:
    """One static, fully-transparent size x size ARGB32 image, per the
    Xcursor file format (magic, header, one TOC entry, one image chunk)."""
    image_type = 0xFFFD0002
    header_size = 16
    version = 0x00010000
    ntoc = 1

    file_header = struct.pack("<4sIII", b"Xcur", header_size, version, ntoc)
    # TOC entry: type, subtype (nominal size), position of chunk in file
    toc_entry = struct.pack("<III", image_type, size, header_size + 12)

    chunk_header_size = 36
    chunk_version = 1
    xhot = yhot = 0
    delay = 1  # static image; must be a valid (nonzero) delay per the spec
    pixels = b"\x00\x00\x00\x00" * (size * size)  # ARGB, alpha=0 throughout

    image_chunk = struct.pack(
        "<IIIIIIIII",
        chunk_header_size,
        image_type,
        size,
        chunk_version,
        size,
        size,
        xhot,
        yhot,
        delay,
    ) + pixels

    return file_header + toc_entry + image_chunk


def main() -> None:
    os.makedirs(CURSORS_DIR, exist_ok=True)
    data = build_blank_xcursor(SIZE)
    for name in CURSOR_NAMES:
        with open(os.path.join(CURSORS_DIR, name), "wb") as f:
            f.write(data)

    with open(os.path.join(DEST, "index.theme"), "w") as f:
        f.write(
            "[Icon Theme]\n"
            "Name=Blank\n"
            "Comment=Fully transparent cursor theme for the AirplayMatrix "
            "kiosk (hides the idle pointer before the app itself takes "
            "over the cursor)\n"
        )

    print(f"wrote {len(CURSOR_NAMES)} cursor(s) + index.theme under {DEST}")


if __name__ == "__main__":
    main()

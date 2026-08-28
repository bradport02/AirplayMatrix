"""
Album artwork conditioning for the 64x64 HUB75 matrix.

Pipeline: decode whatever the sender delivered (JPEG or PNG, typically
600x600 or larger), downscale to 64x64, re-encode as JPEG, base64 the result.

Two encoder choices matter and neither is the library default:

  * Chroma subsampling is disabled (4:4:4). At 64x64 the luma plane is already
    tiny, so 4:2:0 saves a few hundred bytes at most while visibly smearing
    saturated edges — exactly the failure mode that a 64x64 emissive panel
    exaggerates, since each pixel is a discrete 3 mm RGB LED with no optical
    blending between neighbours.

  * BOX resampling is offered alongside LANCZOS. LANCZOS is sharper on
    photographic covers but its negative lobes overshoot at hard edges, and on
    an LED matrix that overshoot reads as a bright halo rather than the subtle
    ringing you would see on an LCD. Flat-colour and typographic covers are
    usually cleaner with BOX.

The base64 output is plain RFC 4648, which is exactly what base64-image.de
produces; the data URI prefix that site prepends is cosmetic and optional
here. There is no proprietary compression involved on that site or this one.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Optional

from PIL import Image

LOG = logging.getLogger(__name__)

MATRIX_SIZE = 64

RESAMPLE_MODES = {
    "lanczos": Image.LANCZOS,
    "box": Image.BOX,
    "bicubic": Image.BICUBIC,
}


@dataclass(frozen=True)
class EncodedArtwork:
    jpeg: bytes
    b64: str
    quality: int

    @property
    def data_uri(self) -> str:
        return f"data:image/jpeg;base64,{self.b64}"

    def wire_time(self, baud: int = 2_000_000, framing_bits: int = 10) -> float:
        """Approximate UART transfer time in seconds, excluding protocol overhead."""
        return len(self.b64) * framing_bits / baud


def downscale(data: bytes, resample: str = "lanczos") -> Image.Image:
    """Decode and reduce artwork to MATRIX_SIZE square, RGB, no alpha.

    Non-square covers are centre-cropped rather than letterboxed; the matrix
    has no bezel, so letterbox bars would look like part of the artwork.
    Alpha is flattened onto black, matching an unlit panel pixel.
    """
    img = Image.open(io.BytesIO(data))
    img.load()

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (0, 0, 0))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")

    width, height = img.size
    if width != height:
        edge = min(width, height)
        left = (width - edge) // 2
        top = (height - edge) // 2
        img = img.crop((left, top, left + edge, top + edge))

    filt = RESAMPLE_MODES.get(resample, Image.LANCZOS)

    # Pillow's reduce() does an exact integer-factor box average first, which
    # both speeds up the large-ratio case and suppresses the aliasing that a
    # single-pass LANCZOS shows when decimating 600px to 64px in one step.
    factor = min(width, height) // (MATRIX_SIZE * 2)
    if factor >= 2:
        img = img.reduce(factor)

    return img.resize((MATRIX_SIZE, MATRIX_SIZE), filt)


def encode(
    data: bytes,
    quality: int = 85,
    resample: str = "lanczos",
    max_bytes: Optional[int] = None,
) -> Optional[EncodedArtwork]:
    """Downscale, JPEG-encode and base64 the artwork.

    If `max_bytes` is given it bounds the *base64* length, since that is what
    actually occupies the serial link. Quality is walked down in steps rather
    than binary-searched: the search space is small and a monotonic walk keeps
    the chosen quality as high as possible for a given budget.
    """
    try:
        img = downscale(data, resample)
    except (OSError, ValueError) as exc:
        LOG.warning("artwork decode failed: %s", exc)
        return None

    for attempt_quality in _quality_ladder(quality):
        buf = io.BytesIO()
        img.save(
            buf,
            format="JPEG",
            quality=attempt_quality,
            optimize=True,
            subsampling=0,  # 4:4:4, see module docstring
            progressive=False,  # baseline only; TJpgDec cannot decode progressive
        )
        jpeg = buf.getvalue()
        b64 = base64.b64encode(jpeg).decode("ascii")
        if max_bytes is None or len(b64) <= max_bytes:
            return EncodedArtwork(jpeg=jpeg, b64=b64, quality=attempt_quality)

    LOG.warning("artwork could not be fitted into %s base64 bytes", max_bytes)
    return None


def blank(quality: int = 85) -> EncodedArtwork:
    """An all-black MATRIX_SIZE frame.

    Used to clear the panel (no track, no artwork, session ended, app
    shutting down) rather than leaving the last artwork frozen on-screen.
    Same baseline/4:4:4 JPEG settings as encode() for TJpgDec compatibility.
    """
    img = Image.new("RGB", (MATRIX_SIZE, MATRIX_SIZE), (0, 0, 0))
    buf = io.BytesIO()
    img.save(
        buf,
        format="JPEG",
        quality=quality,
        optimize=True,
        subsampling=0,
        progressive=False,
    )
    jpeg = buf.getvalue()
    b64 = base64.b64encode(jpeg).decode("ascii")
    return EncodedArtwork(jpeg=jpeg, b64=b64, quality=quality)


def _quality_ladder(start: int) -> list[int]:
    ladder = [start]
    value = start
    while value > 30:
        value -= 10
        ladder.append(value)
    return ladder


@dataclass(frozen=True)
class QuadrantColors:
    """Average colour of each quadrant of the *original* artwork (no square
    crop -- unlike downscale(), corner position here should mean corner
    position in the artwork as delivered, not after the matrix pipeline's
    own cropping). Feeds the ambient background gradient in the UI, not the
    64x64 matrix panel."""

    top_left: str
    top_right: str
    bottom_left: str
    bottom_right: str


def quadrant_colors(data: bytes) -> Optional[QuadrantColors]:
    """Reduce artwork straight to a 2x2 image with box averaging -- each of
    the four resulting pixels is exactly the mean colour of that quadrant,
    which is cheaper and simpler than sampling/averaging pixels by hand."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
    except (OSError, ValueError) as exc:
        LOG.warning("artwork decode failed for quadrant colours: %s", exc)
        return None

    quad = img.resize((2, 2), Image.BOX)
    px = quad.load()

    def _hex(rgb: tuple[int, int, int]) -> str:
        return "#%02x%02x%02x" % rgb

    return QuadrantColors(
        top_left=_hex(px[0, 0]),
        top_right=_hex(px[1, 0]),
        bottom_left=_hex(px[0, 1]),
        bottom_right=_hex(px[1, 1]),
    )

"""Core image-to-SNSTK conversion logic.

This module is intentionally free of I/O side-effects so it can be
used from both the CLI and the web application without modification
(Single Responsibility / Dependency-Inversion principles).
"""

from __future__ import annotations

import math
import random
import struct
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Supernote colour codes
# ---------------------------------------------------------------------------

COLORCODE_BLACK: int = 0x61
COLORCODE_BACKGROUND: int = 0x62

# Anti-aliasing levels (0x0F = near black / high opacity → 0xEF = near transparent)
AA_LEVELS: list[int] = [
    0x0F, 0x1F, 0x2F, 0x3F, 0x4F, 0x5F, 0x6F, 0x7F,
    0x8F, 0x9F, 0xAF, 0xBF, 0xCF, 0xDF, 0xEF,
]

# ---------------------------------------------------------------------------
# Stroke pens
# ---------------------------------------------------------------------------
# A placed sticker is drawn from its trail strokes, not from its bitmap, so
# the strokes alone must carry the image's tone.  The firmware draws strokes
# in the same pen colours as the plugin SDK (sn-plugin-lib Element.penColor).
PEN_COLOR_BLACK: int = 0x00
PEN_COLOR_DARK_GRAY: int = 0x9D
PEN_COLOR_LIGHT_GRAY: int = 0xC9

# Thinnest pen weight the firmware accepts.  Each 1 px scanline stroke is
# drawn ~2.3 px wide at this weight, so neighbouring rows overlap.
STROKE_PEN_WEIGHT: int = 200

# Tones a placed sticker can show, darkest first:
#   (rendered grey, stroke pen colour or None for paper, bitmap colour code)
# The greys are what an A6X2 Nomad actually puts on screen for each pen
# colour (measured from a screencap).  The bitmap codes are the AA levels
# whose ink amount matches those greys, so the picker thumbnail shows the
# same four tones the strokes will draw.
TONE_PALETTE: tuple[tuple[int, int | None, int], ...] = (
    (0,   PEN_COLOR_BLACK,      COLORCODE_BLACK),
    (157, PEN_COLOR_DARK_GRAY,  0x9F),
    (201, PEN_COLOR_LIGHT_GRAY, 0xBF),
    (255, None,                 COLORCODE_BACKGROUND),
)
TONE_PAPER: int = len(TONE_PALETTE) - 1

# ---------------------------------------------------------------------------
# Known devices
# ---------------------------------------------------------------------------

# ``emr`` is the pen digitizer's (width, height) in its own units — the same
# two values stored in each stroke's device-info block.  It covers the screen
# at 8.45 digitizer units per pixel.
DEVICES: dict[str, dict] = {
    "N5":  {"name": "A5X2 Manta / A6X2 Nomad", "screen": (1920, 2560), "emr": (16224, 21632)},
    "A5X": {"name": "A5X",                      "screen": (1404, 1872), "emr": (11864, 15819)},
    "A6X": {"name": "A6X",                      "screen": (1404, 1872), "emr": (11864, 15819)},
}

DEFAULT_STICKER_SIZE: int = 180

# Transparent breathing room added on every side, in pixels.  ``0`` means the
# sticker's longest edge is exactly ``size`` px with no padding at all.
DEFAULT_MARGIN: int = 0

# Supported image extensions (anything Pillow can open)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}
)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _is_bw_opaque_image(img: Image.Image, bw_threshold: float = 0.90) -> bool:
    """Detect an opaque high-contrast B&W image with embedded white details.

    Returns ``True`` when **both** conditions are met:

    1. The image is high-contrast black-and-white — at least
       *bw_threshold* (default 90 %) of non-transparent pixels are
       near-black (luminance < 30) or near-white (luminance > 225).
    2. The image is predominantly opaque — fewer than 5 % of pixels
       are transparent.

    Condition 2 ensures we only change the pipeline for images that
    have **opaque white areas** surrounded by black (like a JPG or
    full-background PNG).  Images with transparent backgrounds (like
    an icon on a clear canvas) already render correctly with the
    standard dithering + scanline-fill pipeline and are left
    untouched to avoid unnecessary output changes.
    """
    arr = np.array(img.convert("RGBA"))
    alpha = arr[:, :, 3]
    total_pixels = alpha.size

    # Condition 2 — mostly opaque (< 5 % transparent)
    transparent_fraction = float(np.sum(alpha < 10)) / total_pixels
    if transparent_fraction > 0.05:
        return False

    # Condition 1 — high-contrast B&W
    opaque_mask = alpha > 0
    if not opaque_mask.any():
        return False
    lum = (
        0.299 * arr[:, :, 0].astype(np.float64)
        + 0.587 * arr[:, :, 1].astype(np.float64)
        + 0.114 * arr[:, :, 2].astype(np.float64)
    )
    opaque_lum = lum[opaque_mask]
    bw_count = np.sum((opaque_lum < 30) | (opaque_lum > 225))
    return float(bw_count) / len(opaque_lum) >= bw_threshold


def alpha_to_colorcode(alpha: int) -> int:
    """Convert an alpha value (0=transparent, 255=opaque) to a Supernote colour code."""
    if alpha < 9:
        return COLORCODE_BACKGROUND
    if alpha > 246:
        return COLORCODE_BLACK
    index = 14 - round((alpha / 255) * 14)
    return AA_LEVELS[index]


# ---------------------------------------------------------------------------
# Image → pixel array
# ---------------------------------------------------------------------------

def _round_half_up(value: float) -> int:
    """Round half away from zero, matching JavaScript's ``Math.round``.

    Python's built-in :func:`round` uses banker's rounding, which would make
    ``docs/app.js`` and this module disagree by one pixel whenever a scaled
    dimension lands exactly on ``.5`` (e.g. a 8x5 source at ``size=4``).
    Values here are always non-negative.
    """
    return int(math.floor(value + 0.5))


def clamp_margin(size: int, margin: int) -> int:
    """Clamp *margin* to the largest value that keeps the sticker within *size*.

    A margin of half the requested size or more would squeeze the artwork to
    nothing while the canvas kept growing as ``content + 2*margin``, so the
    finished sticker ended up *larger* than *size* with a 1 px dot inside it
    (e.g. ``size=32, margin=64`` produced a 129x129 sticker).  The largest
    safe margin leaves at least 1 px of content: ``(size - 1) // 2``.
    """
    return max(0, min(margin, (size - 1) // 2))


def fit_dimensions(
    content_w: int,
    content_h: int,
    size: int,
    margin: int = DEFAULT_MARGIN,
    upscale: bool = True,
) -> tuple[int, int]:
    """Scale ``content_w`` x ``content_h`` so its longest edge fills *size*.

    The aspect ratio is always preserved.  ``margin`` is transparent padding
    added later on every side, so the *content* is fitted into a
    ``(size - 2*margin)`` box and the finished sticker measures exactly
    *size* px on its longest edge.

    Unlike the previous implementation this scales **up** as well as down:
    a 64x64 source with ``size=180`` really does produce a 180 px sticker.
    Pass ``upscale=False`` to keep smaller sources at their native scale.

    Returns:
        ``(new_w, new_h)`` — the scaled content dimensions.

    Raises:
        ValueError: If *size* is less than 1.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    margin = clamp_margin(size, margin)
    box = max(1, size - 2 * margin)
    longest = max(content_w, content_h)
    if longest <= 0:
        return 1, 1
    scale = box / longest
    if not upscale:
        scale = min(scale, 1.0)
    return (
        max(1, _round_half_up(content_w * scale)),
        max(1, _round_half_up(content_h * scale)),
    )


def image_to_pixels(
    source: str | Path | BinaryIO,
    size: int = DEFAULT_STICKER_SIZE,
    trim: bool = True,
    margin: int = DEFAULT_MARGIN,
    pad_square: bool = False,
    upscale: bool = True,
) -> tuple[list[int], int, int, Image.Image, bool]:
    """Load an image and return ``(pixels, width, height, pil_image, is_bw)``.

    *source* may be a file path or any file-like object (e.g. a
    ``BytesIO`` from a web upload).  All Pillow-supported formats are
    accepted.  The returned *pil_image* is the resized RGBA image used
    for high-quality trail dithering.  *is_bw* indicates whether the
    source was detected as a high-contrast black-and-white image.

    When *trim* is ``True`` (the default), transparent borders are
    cropped away before resizing so the visible content fills as much
    of the sticker area as possible.

    Sizing contract:

    * The finished sticker measures exactly *size* px on its longest
      edge — sources smaller than *size* are scaled **up**, not left at
      their native scale (pass ``upscale=False`` to opt out).
    * The source aspect ratio is preserved.  A 600x200 card becomes a
      180x60 sticker, **not** a 180x180 one with dead space above and
      below.
    * *margin* adds transparent breathing room on every side and is
      included in *size*.  It defaults to ``0``, so what you ask for is
      what the device gets.  It is clamped to ``(size - 1) // 2`` so the
      sticker can never grow past *size*.
    * *pad_square* restores the old behaviour of centring the artwork
      on a square ``size`` x ``size`` canvas.

    Args:
        source: Path or file-like object for the source image.
        size:   Length of the finished sticker's longest edge, in pixels.
        trim:   Crop transparent borders before scaling.
        margin: Transparent padding on each side, in pixels.
        pad_square: Centre the artwork on a square canvas instead of
                using the artwork's own aspect ratio.
        upscale: Allow sources smaller than *size* to be scaled up.

    Raises:
        ValueError: If *size* is less than 1.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    img = Image.open(source).convert("RGBA")

    # Detect opaque B&W images early (before resizing) for pipeline
    # optimisation.  Only images without transparency need the special
    # handling — transparent-background images render fine with the
    # standard pipeline.
    is_bw = _is_bw_opaque_image(img)

    if trim:
        bbox = img.getbbox()  # bounding box of non-transparent pixels
        if bbox is not None:
            img = img.crop(bbox)
        # If bbox is None the image is fully transparent — keep as-is.

    # Scale the (trimmed) artwork so its longest edge fills the requested
    # size.  Scaling is driven by the *content* dimensions alone — the
    # source image's own pixel dimensions no longer cap the result, which
    # is what previously made `size` a no-op for small sources.
    margin = clamp_margin(size, margin)
    trimmed_w, trimmed_h = img.size
    new_w, new_h = fit_dimensions(trimmed_w, trimmed_h, size, margin, upscale)

    # B&W images: use NEAREST resampling to preserve crisp edges.
    # LANCZOS creates anti-aliased gray pixels at black/white boundaries
    # which, combined with trail stroke bleeding on the e-ink display,
    # causes fine white detail lines to be swallowed by surrounding black.
    resample = Image.NEAREST if is_bw else Image.LANCZOS
    img = img.resize((new_w, new_h), resample)

    # Build the final canvas.  By default it hugs the artwork (plus any
    # requested margin) so the sticker the device imports has no hidden
    # transparent padding inflating its bounding box.  ``pad_square``
    # restores the legacy square canvas for callers that relied on it.
    if pad_square:
        canvas_w = canvas_h = size
    else:
        canvas_w = new_w + 2 * margin
        canvas_h = new_h + 2 * margin

    if (new_w, new_h) != (canvas_w, canvas_h):
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        canvas.paste(img, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
        img = canvas

    w, h = img.size

    if not is_bw:
        # The bitmap shows the same four tones the strokes will draw, so the
        # picker thumbnail matches the sticker once it is placed in a note.
        tones = quantize_tones(img)
        pixels = [TONE_PALETTE[t][2] for t in tones.ravel()]
        return pixels, w, h, img, is_bw

    pixels: list[int] = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = img.getpixel((x, y))
            if a == 0:
                pixels.append(COLORCODE_BACKGROUND)
            else:
                gray = int(0.299 * r + 0.587 * g + 0.114 * b)
                ink_alpha = int((255 - gray) * (a / 255))
                pixels.append(alpha_to_colorcode(ink_alpha))

    return pixels, w, h, img, is_bw


# ---------------------------------------------------------------------------
# RLE encoder
# ---------------------------------------------------------------------------

def encode_rle(pixels: list[int]) -> bytes:
    """Encode pixel data using Supernote's RattaRLE compression."""
    result = bytearray()
    i = 0
    while i < len(pixels):
        color = pixels[i]
        run = 1
        while i + run < len(pixels) and pixels[i + run] == color:
            run += 1
        i += run

        while run > 0:
            if run >= 0x4000:
                result.append(color)
                result.append(0xFF)
                run -= 0x4000
            elif run > 128:
                high_part = ((run - 1) >> 7) - 1
                if high_part < 0:
                    high_part = 0
                shift = (high_part + 1) << 7
                second_byte = run - 1 - shift
                while second_byte > 255 and high_part < 127:
                    high_part += 1
                    shift = (high_part + 1) << 7
                    second_byte = run - 1 - shift
                while second_byte < 0 and high_part > 0:
                    high_part -= 1
                    shift = (high_part + 1) << 7
                    second_byte = run - 1 - shift
                if 0 <= second_byte <= 255:
                    result.append(color)
                    result.append(high_part | 0x80)
                    result.append(color)
                    result.append(second_byte)
                    actual = 1 + second_byte + ((high_part + 1) << 7)
                    run -= actual
                else:
                    result.append(color)
                    result.append(127)
                    run -= 128
            else:
                result.append(color)
                result.append(run - 1)
                run = 0

    return bytes(result)


# ---------------------------------------------------------------------------
# Custom IEEE 754 encoding (Supernote contour coordinates)
# ---------------------------------------------------------------------------

def _decimal_to_custom_ieee754(value: float) -> bytes:
    """Encode a float as Supernote's custom IEEE 754 format.

    This is standard little-endian IEEE 754 single-precision with the
    first two bytes swapped:  ``std[1], std[0], std[2], std[3]``.

    Verified against the ``decimal_to_custom_ieee754`` function in the
    PySN/snex reference implementation.
    """
    if value == 0.0:
        return b'\x00\x00\x00\x00'
    std = struct.pack('<f', value)
    return bytes([std[1], std[0], std[2], std[3]])


# ---------------------------------------------------------------------------
# Stroke record binary constants
# ---------------------------------------------------------------------------
# These byte sequences define the fixed parts of a Supernote pen stroke
# record.  They were verified against both the PySN/snex reference
# implementation (pen_strokes_dict_to_bytes) and a working sticker from
# christmas2025.snstk.
#
# A stroke record body (everything after the 20-byte stroke header) has
# this layout:
#
#   body[ 0:  8]  Record marker (8 bytes)
#   body[ 8: 28]  Page/type + padding + constants (20 bytes)
#   body[28: 80]  Tool name "others" null-padded (52 bytes)
#   body[80:104]  Bounding box: min_x, min_y, avg_x, avg_y, max_x, max_y (6×i32)
#   body[104:116] Device info (12 bytes, device-specific)
#   body[116:168] Annotation "superNoteNote" null-padded (52 bytes)
#   body[168:192] Flags (24 bytes)
#   body[192+]    Vector points, pressure, unique, one arrays, then
#                 post-array metadata, contours, and r_bytes.
# ---------------------------------------------------------------------------

# Record marker (body[0:8])
_MARKER = bytes.fromhex('20000000ffffffff')

# Page/type + padding + constants (body[8:28])
# Byte 0 must be 0x03 — verified in both Christmas Dog and Stocking strokes.
_PAGE_CONST = bytes.fromhex(
    '03000000'   # page = 3 (required by firmware)
    '00000000'   # padding
    '00000000'   # padding
    '88130000'   # constant = 5000
    '00000000'   # padding
)

# Tool name "others" null-padded to 52 bytes (body[28:80])
_TOOL_NAME = (b'others' + b'\x00' * 46)

# Device info (12 bytes each) — body[104:116]
# First u32 must be 0x1a (26) — verified in both Christmas Dog and Stocking.
# PySN/snex uses 0x02 for notebook strokes, but sticker strokes require 0x1a.
_DEVICE_INFO_N5 = bytes.fromhex('1a00000080540000603f0000')
_DEVICE_INFO_OTHER = bytes.fromhex('1a000000cb3d0000582e0000')

# Annotation "superNoteNote" null-padded to 52 bytes (body[116:168])
_ANNOTATION = (b'superNoteNote' + b'\x00' * 39)

# Flags (24 bytes) — body[168:192]
_FLAGS = bytes.fromhex(
    '01000000000000000000000000000000'
    '0000000000000000'
)

# 54 fixed bytes between stroke_nb and contours_count
# Last byte must be 0x01 — verified in Christmas Dog working strokes.
_POST_STROKE_NB = bytes.fromhex(
    '00000000000000000000000000000000'
    '01000000010000000000000000000000'
    '01000000010000000000000000000000'
    '000000000001'
)

# r_bytes template (94 bytes) — tail of every stroke record.
# Extracted verbatim from a working Christmas Dog sticker stroke.
# Contains FF block, double constant, screen dimensions, "none" strings,
# and pen metadata.  Screen width at offset 37, height at offset 41.
_R_BYTES_TEMPLATE = bytes.fromhex(
    "ffffffffffffffffffffffffffffffffffffffff"   # 20 × 0xFF
    "4dac33dcb771d43f"                           # double constant
    "002f0000000000000080070000000a00"           # 14 bytes (screen_w at +37)
    "00000000000004000000"                       # 10 bytes (screen_h at +41)
    "6e6f6e65"                                   # "none"
    "040000006e6f6e65"                           # 4 + "none"
    "00000000"                                   # 4 zeros
    "0300000002000000"                           # 8 bytes
    "00000000000000000000000000000000"           # 16 zeros
)


def _build_r_bytes(screen_w: int, screen_h: int) -> bytes:
    """Build the r_bytes tail of a stroke record with correct screen dims."""
    r = bytearray(_R_BYTES_TEMPLATE)
    struct.pack_into('<I', r, 37, screen_w)
    struct.pack_into('<I', r, 41, screen_h)
    return bytes(r)


# ---------------------------------------------------------------------------
# Single stroke builder
# ---------------------------------------------------------------------------

def _interpolate_contour(
    points: list[tuple[float, float]], spacing: float = 2.0,
) -> list[tuple[float, float]]:
    """Densely interpolate points along a closed polygon at *spacing* px intervals.

    The Supernote firmware expects the vector-points array to contain a dense
    pen-trajectory (typically 50–2000+ points), *not* just the polygon
    vertices.  This function walks along each edge of *points* (closing the
    polygon back to the first vertex) and emits a new sample every *spacing*
    pixels.

    Returns:
        A list of ``(x, y)`` floats with many more entries than the input.
    """
    import math

    dense: list[tuple[float, float]] = []
    n = len(points)
    if n < 2:
        return list(points)

    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-6:
            dense.append((x0, y0))
            continue
        steps = max(1, int(seg_len / spacing))
        for s in range(steps):
            t = s / steps
            dense.append((x0 + dx * t, y0 + dy * t))

    # Ensure we have a reasonable minimum — retry with finer spacing once
    if len(dense) < 10 and spacing > 0.5:
        return _interpolate_contour(points, spacing=max(0.5, spacing / 2))

    return dense


def _build_stroke(
    contour_points: list[tuple[float, float]],
    stroke_nb: int,
    device: str,
    screen_w: int,
    screen_h: int,
    _x_offset: float = 0.0,
    _y_offset: float = 0.0,
    pen_color: int = PEN_COLOR_BLACK,
) -> bytes:
    """Build a single stroke record from contour points.

    Each contour_points entry is ``(x, y)`` in sticker pixel coordinates.
    The stroke format follows the PySN/snex ``pen_strokes_dict_to_bytes``
    reference implementation exactly.

    The **vector-points** section is populated with densely interpolated
    samples along the contour (mimicking a real pen trajectory), while the
    **contours** section stores the original simplified polygon vertices.

    Contour/bbox stay in sticker pixel space (matching the bitmap) while
    vector points are in pen-digitizer space, whose x axis runs opposite to
    the screen's (see the vector-points section below).

    Args:
        contour_points: List of (x, y) float coordinates in pixel space.
        stroke_nb: Stroke sequence number (1-based).
        device: Device code key from :data:`DEVICES`.
        screen_w: Screen width for the target device.
        screen_h: Screen height for the target device.
        _x_offset: Extra horizontal shift of the drawn stroke, in pixels.
        _y_offset: Extra vertical shift of the drawn stroke, in pixels.
        pen_color: Stroke pen colour, one of the ``PEN_COLOR_*`` constants.

    Returns:
        Complete stroke_data bytes (stroke header + record body).
    """
    _p = struct.Struct('<I').pack   # unsigned 32-bit LE
    _ps = struct.Struct('<i').pack  # signed 32-bit LE

    # Dense vector points for the pen trajectory
    vector_pts = _interpolate_contour(contour_points, spacing=2.0)
    n_vec = len(vector_pts)

    # Simplified contour points for the contour section
    n_contour = len(contour_points)

    # ---- Coordinate spaces ----
    # The Supernote firmware uses TWO coordinate systems in each stroke:
    #   bbox / contour  → sticker pixel coordinates (0..width/height)
    #   vector points   → pen digitizer coordinates
    # Fitted over all 4,613 strokes of the official christmas2025.snstk:
    #   digi_x = emr_width - px * scale,   digi_y = py * scale
    # with scale = emr_width / screen_width (8.45 on every known device).
    # The firmware draws the sticker from the vector points but places the
    # selection box from bbox/contour, so the two must describe the same
    # pixels or the art drifts away from its lasso box.
    emr_w = DEVICES.get(device, DEVICES["N5"])["emr"][0]
    scale = emr_w / screen_w

    # Bounding box in PIXEL space (NOT digitizer space).
    # The firmware uses these values for sticker placement/hit-testing.
    px_xs = [p[0] for p in contour_points]
    px_ys = [p[1] for p in contour_points]
    min_x = int(min(px_xs))
    max_x = int(max(px_xs))
    min_y = int(min(px_ys))
    max_y = int(max(px_ys))
    avg_x = (min_x + max_x) // 2
    avg_y = (min_y + max_y) // 2

    buf = bytearray()

    # ---- Stroke header (20 bytes) ----
    buf += struct.pack('B', 10)           # pen_type = 10 (standard)
    buf += b'\x00\x00\x00'
    buf += struct.pack('B', pen_color)    # pen_color (PEN_COLOR_*)
    buf += b'\x00\x00\x00'
    buf += struct.pack('<H', STROKE_PEN_WEIGHT)
    buf += bytes.fromhex('00000A00000000000000')   # 10 fixed bytes

    # ---- Record body ----
    # Marker (8 bytes)
    buf += _MARKER
    # Page + padding + constants (20 bytes)
    buf += _PAGE_CONST
    # Tool name (52 bytes)
    buf += _TOOL_NAME

    # Bounding box (6 × i32 = 24 bytes) — sticker pixel space
    buf += _ps(min_x)
    buf += _ps(min_y)
    buf += _ps(avg_x)
    buf += _ps(avg_y)
    buf += _ps(max_x)
    buf += _ps(max_y)

    # Device info (12 bytes)
    buf += _DEVICE_INFO_N5 if device in ('N5',) else _DEVICE_INFO_OTHER
    # Annotation (52 bytes)
    buf += _ANNOTATION
    # Flags (24 bytes)
    buf += _FLAGS

    # ---- Vector points (y, x as i32 pairs) — digitizer coordinates ----
    buf += _p(n_vec)
    for x, y in vector_pts:
        digi_x = _round_half_up(emr_w - (x + _x_offset) * scale)
        digi_y = _round_half_up((y + _y_offset) * scale)
        buf += _ps(digi_y)   # y stored first
        buf += _ps(digi_x)   # x stored second

    # ---- Pressure (u16 per point) ----
    buf += _p(n_vec)
    for _ in range(n_vec):
        buf += struct.pack('<H', 1000)    # default pressure

    # ---- Unique (u32 per point, all same value) ----
    buf += _p(n_vec)
    for _ in range(n_vec):
        buf += _p(1)

    # ---- One (u8 per point, all 1) ----
    buf += _p(n_vec)
    buf += b'\x01' * n_vec

    # ---- 16 bytes (12 zeros + 0x61000000) ----
    # Byte 12 must be 0x61 — verified in both Christmas Dog and Stocking.
    buf += b'\x00' * 12 + bytes.fromhex('61000000')

    # ---- Stroke number (u32) ----
    buf += _p(stroke_nb)

    # ---- 54 fixed bytes ----
    buf += _POST_STROKE_NB

    # ---- Contours section ----
    # 1 contour containing the simplified polygon vertices
    buf += _p(1)                          # contours_count = 1

    # Point count + custom IEEE 754 encoded (x, y) pairs
    buf += _p(n_contour)
    for x, y in contour_points:
        buf += _decimal_to_custom_ieee754(float(x))
        buf += _decimal_to_custom_ieee754(float(y))

    # Second contours_count (footer repeat)
    buf += _p(1)

    # ---- r_bytes (118 bytes with screen dims) ----
    buf += _build_r_bytes(screen_w, screen_h)

    return bytes(buf)


# ---------------------------------------------------------------------------
# Trails builder (four-tone error diffusion + scanline fills)
# ---------------------------------------------------------------------------


def _rgba_image_to_grayscale(img: Image.Image) -> np.ndarray:
    """Convert a PIL RGBA image directly to grayscale (0=black, 255=white).

    Preserves full 256-level precision — much better for dithering than
    going through the lossy 17-level Supernote colour codes.
    """
    img_rgba = img.convert("RGBA")
    w, h = img_rgba.size
    gray = np.full((h, w), 255.0, dtype=np.float64)
    for y in range(h):
        for x in range(w):
            r, g, b, a = img_rgba.getpixel((x, y))
            if a == 0:
                gray[y, x] = 255.0
            else:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                # Blend with white background based on alpha
                gray[y, x] = lum * (a / 255) + 255 * (1 - a / 255)
    return gray


def _pixels_to_grayscale(
    pixels: list[int], width: int, height: int,
) -> np.ndarray:
    """Convert Supernote colour codes to a grayscale image (0=black, 255=white).

    Fallback for when the original PIL image isn't available.  The four
    :data:`TONE_PALETTE` codes map back to their exact greys, so a bitmap
    built by :func:`quantize_tones` round-trips to the same tones.
    """
    code_to_gray: dict[int, int] = {COLORCODE_BLACK: 0, COLORCODE_BACKGROUND: 255}
    for idx, code in enumerate(AA_LEVELS):
        code_to_gray[code] = int((idx + 1) / (len(AA_LEVELS) + 1) * 255)
    for tone, _pen, code in TONE_PALETTE:
        code_to_gray[code] = tone

    gray = np.full((height, width), 255, dtype=np.float64)
    for i, code in enumerate(pixels):
        gray[i // width, i % width] = code_to_gray.get(code, 255)
    return gray


def _quantize_gray(gray: np.ndarray, opaque: np.ndarray) -> np.ndarray:
    """Floyd-Steinberg error diffusion onto the :data:`TONE_PALETTE` greys.

    Args:
        gray:   float grayscale image (0=black, 255=white).
        opaque: bool mask of pixels that may receive ink.  Transparent
                pixels are always paper and neither take nor pass on
                diffusion error, so ink never spills past the artwork.

    Returns:
        ``uint8`` array of :data:`TONE_PALETTE` indices, same shape.
    """
    h, w = gray.shape
    img = gray.astype(np.float64)   # astype copies; error is diffused in place
    tones = [t[0] for t in TONE_PALETTE]
    # Midpoints between neighbouring tones: a value below cuts[i] rounds to
    # tone i or darker.
    cuts = [(a + b) / 2 for a, b in zip(tones, tones[1:])]
    out = np.full((h, w), TONE_PAPER, dtype=np.uint8)

    for y in range(h):
        for x in range(w):
            if not opaque[y, x]:
                continue
            old_val = img[y, x]
            idx = 0
            while idx < len(cuts) and old_val >= cuts[idx]:
                idx += 1
            out[y, x] = idx
            err = old_val - tones[idx]

            if x + 1 < w and opaque[y, x + 1]:
                img[y, x + 1] += err * 7.0 / 16.0
            if y + 1 < h:
                if x - 1 >= 0 and opaque[y + 1, x - 1]:
                    img[y + 1, x - 1] += err * 3.0 / 16.0
                if opaque[y + 1, x]:
                    img[y + 1, x] += err * 5.0 / 16.0
                if x + 1 < w and opaque[y + 1, x + 1]:
                    img[y + 1, x + 1] += err * 1.0 / 16.0

    return out


def quantize_tones(img: Image.Image) -> np.ndarray:
    """Reduce an RGBA image to the four tones a placed sticker can show.

    Returns a ``(height, width)`` ``uint8`` array of :data:`TONE_PALETTE`
    indices.  Both the bitmap and the trail strokes are built from this
    one result, so the picker thumbnail and the placed sticker agree.
    """
    gray = _rgba_image_to_grayscale(img)
    opaque = np.array(img.convert("RGBA"))[:, :, 3] > 0
    return _quantize_gray(gray, opaque)


def _erode_cross(mask: np.ndarray) -> np.ndarray:
    """Erode a binary mask with a 3x3 cross-shaped structuring element.

    Equivalent to ``cv2.erode(mask, MORPH_CROSS 3x3)`` but implemented with
    NumPy so the package needs no OpenCV dependency.  Each pixel becomes the
    minimum of itself and its four cardinal neighbours; out-of-bounds
    neighbours are ignored, matching OpenCV's default border handling for
    erosion (outside pixels treated as the maximum value).

    Args:
        mask: 2-D array where the non-zero value marks foreground.

    Returns:
        The eroded mask, same shape and dtype.
    """
    out = mask.copy()
    # ``np.minimum`` is associative, so accumulating the shifted minima in
    # place yields the true 5-point minimum.
    out[1:, :] = np.minimum(out[1:, :], mask[:-1, :])
    out[:-1, :] = np.minimum(out[:-1, :], mask[1:, :])
    out[:, 1:] = np.minimum(out[:, 1:], mask[:, :-1])
    out[:, :-1] = np.minimum(out[:, :-1], mask[:, 1:])
    return out


def _row_runs(row: np.ndarray) -> list[tuple[int, int, int]]:
    """Return ``(x_start, x_end, tone)`` for each run of one inked tone.

    *row* holds :data:`TONE_PALETTE` indices; paper pixels start no run.
    """
    runs: list[tuple[int, int, int]] = []
    width = len(row)
    x = 0
    while x < width:
        tone = int(row[x])
        if tone == TONE_PAPER:
            x += 1
            continue
        x_start = x
        while x < width and row[x] == tone:
            x += 1
        runs.append((x_start, x - 1, tone))
    return runs


def build_trails(
    pixels: list[int],
    width: int,
    height: int,
    device: str = "N5",
    pil_image: Image.Image | None = None,
    x_offset: float | None = None,
    y_offset: float = 0.0,
    is_bw: bool = False,
) -> bytes:
    """Build the trails section as scanline strokes in four tones.

    A placed sticker is drawn from these strokes, not from its bitmap.
    The image is reduced to black, dark grey, light grey and paper with
    :func:`quantize_tones`, and every horizontal run of one tone becomes a
    stroke in the matching pen colour.  Grey pens keep mid-tones grey even
    though neighbouring ~2.3 px-wide strokes overlap; with black dots alone
    that overlap turned every mid-tone solid black.

    Strokes are emitted row by row, top to bottom.  Each row then paints
    over the overlap from the row above, so every tone spreads by the same
    amount.  Emitting all black strokes last instead lets black spread over
    its neighbours in both directions and visibly darkens the sticker
    (measured on an A6X2 Nomad: black coverage rose from 42 % to ~60 %).

    When *pil_image* is provided, tones come from the full 256-level RGBA
    data; otherwise they are recovered from the bitmap colour codes.

    When *is_bw* is ``True``, the image is treated as high-contrast
    black-and-white line art: a luminance threshold and a light erosion
    produce black strokes only, so pen width cannot swallow fine white
    detail on the e-ink display.

    Args:
        pixels: Supernote colour codes (row-major, length = *width* × *height*).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.
        pil_image: Optional PIL RGBA image for full-precision tones.
        is_bw: Whether the source image is high-contrast B&W line art.

    Returns:
        Raw bytes for the trails block (**excluding** the leading uint32
        length prefix — the caller wraps it).
    """
    _pack_u32 = struct.Struct("<I").pack
    screen_w, screen_h = DEVICES.get(device, DEVICES["N5"])["screen"]

    # TONE_PALETTE index per pixel; paper pixels get no stroke.
    if is_bw:
        # B&W line art: use a simple luminance threshold instead of
        # dithering.  Dithering adds noise dots at edges that, combined
        # with pen stroke thickness on the e-ink display, cause fine
        # white details to be swallowed by surrounding black.
        if pil_image is not None:
            gray = _rgba_image_to_grayscale(pil_image)
        else:
            gray = _pixels_to_grayscale(pixels, width, height)
        # Erode the black mask before generating scanline fills.  Each pen
        # stroke bleeds on the e-ink display; erosion shrinks the black
        # regions inward so the bleed expands back toward the original
        # boundary without overflowing into the white details.  A
        # cross-shaped 3×3 kernel is the lightest erosion that protects
        # detail without fragmenting thin features at sticker scale.
        black = _erode_cross((gray < 128).astype(np.uint8) * 255) == 255
        tones = np.where(black, 0, TONE_PAPER)
    else:
        if pil_image is not None:
            tones = quantize_tones(pil_image)
        else:
            gray = _pixels_to_grayscale(pixels, width, height)
            opaque = (np.array(pixels) != COLORCODE_BACKGROUND).reshape(height, width)
            tones = _quantize_gray(gray, opaque)

    # Optional extra shift of the drawn strokes, in pixels.  Not needed for
    # alignment: the digitizer mapping in _build_stroke already places
    # every stroke on the pixels its bbox describes.
    if x_offset is None:
        x_offset = 0.0

    all_strokes = bytearray()
    stroke_nb = 1004

    for y in range(height):
        for x_start, x_end, tone in _row_runs(tones[y]):
            # Create rectangle points for this run in ORIGINAL pixel space.
            # Contour/bbox must match the bitmap positions so the selection
            # box aligns with the visible content.  X-mirroring (needed
            # because the firmware flips the rendered vector strokes) is
            # applied later, in _build_stroke's digitizer transform only.
            run_pts = [
                (float(x_start), float(y)),
                (float(x_end), float(y)),
                (float(x_end), float(y + 1)),
                (float(x_start), float(y + 1)),
            ]

            stroke_data = _build_stroke(
                run_pts, stroke_nb, device, screen_w, screen_h,
                _x_offset=x_offset, _y_offset=y_offset,
                pen_color=TONE_PALETTE[tone][1],
            )
            all_strokes += _pack_u32(len(stroke_data))
            all_strokes += stroke_data
            stroke_nb += 1

    num_strokes = stroke_nb - 1004
    if num_strokes == 0:
        # Fallback if image is entirely transparent
        fallback_pts = [
            (0.0, 0.0), (float(width - 1), 0.0),
            (float(width - 1), float(height - 1)), (0.0, float(height - 1)),
        ]
        stroke_data = _build_stroke(
            fallback_pts, 1004, device, screen_w, screen_h, _x_offset=x_offset, _y_offset=y_offset,
        )
        all_strokes = bytearray(_pack_u32(len(stroke_data))) + bytearray(stroke_data)
        num_strokes = 1

    buf = bytearray()
    buf += _pack_u32(num_strokes)
    buf += all_strokes

    return bytes(buf)


# ---------------------------------------------------------------------------
# .sticker file builder
# ---------------------------------------------------------------------------

def _generate_file_id() -> str:
    """Generate a unique file ID in Supernote's format.

    The ID is 33 characters: ``F`` + 14-digit timestamp + 3-digit
    milliseconds + 15-character alphanumeric suffix, matching the
    format used by official Supernote sticker tools.
    """
    timestamp = time.strftime("%Y%m%d%H%M%S")
    ms = f"{int(time.time() * 1000) % 1000:03d}"
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    rng = random.Random(uuid.uuid4().int)
    suffix = "".join(rng.choice(alphabet) for _ in range(15))
    return f"F{timestamp}{ms}{suffix}"


def build_sticker(
    pixels: list[int],
    width: int,
    height: int,
    device: str = "N5",
    pil_image: Image.Image | None = None,
    x_offset: float | None = None,
    y_offset: float = 0.0,
    is_bw: bool = False,
) -> bytes:
    """Assemble a complete ``.sticker`` binary from pixel data.

    Args:
        pixels: Supernote colour codes (one per pixel, row-major).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.
        pil_image: Optional PIL RGBA image for high-quality trail dithering.
        is_bw: Whether the source image is high-contrast B&W line art.

    Returns:
        Raw bytes suitable for inclusion in an SNSTK ZIP archive.
    """
    file_id = _generate_file_id()

    # Section 1 – header
    magic = b"stck"
    version = b"SN_FILE_VER_20230015"
    header_meta = (
        f"<FILE_TYPE:STICKER>"
        f"<APPLY_EQUIPMENT:{device}>"
        f"<FILE_PARSE_TYPE:0>"
        f"<RATTA_ETMD:0>"
        f"<FILE_ID:{file_id}>"
        f"<ANTIALIASING_CONVERT:2>"
    ).encode("ascii")
    header = magic + version + struct.pack("<I", len(header_meta)) + header_meta
    bitmap_offset = len(header)

    # Section 2 – bitmap (RLE-encoded)
    rle_data = encode_rle(pixels)
    bitmap_block = struct.pack("<I", len(rle_data)) + rle_data

    # Section 3 – trails (required for sticker insertion)
    trails_offset = bitmap_offset + len(bitmap_block)
    trails_data = build_trails(
        pixels, width, height, device, pil_image=pil_image,
        x_offset=x_offset, y_offset=y_offset, is_bw=is_bw,
    )
    trails_block = struct.pack("<I", len(trails_data)) + trails_data

    # Section 4 – sticker rect
    rect_offset = trails_offset + len(trails_block)
    rect_str = f"0,0,{width},{height}".encode("ascii")
    rect_block = struct.pack("<I", len(rect_str)) + rect_str

    # Section 5 – footer
    footer_offset = rect_offset + len(rect_block)
    footer_meta = (
        f"<FILE_FEATURE:24>"
        f"<STICKERBITMAP:{bitmap_offset}>"
        f"<STICKERRECT:{rect_offset}>"
        f"<STICKERROTATION:1000>"
        f"<STICKERTRAILS:{trails_offset}>"
    ).encode("ascii")
    footer_block = (
        struct.pack("<I", len(footer_meta))
        + footer_meta
        + b"tail"
        + struct.pack("<I", footer_offset)
    )

    return header + bitmap_block + trails_block + rect_block + footer_block


# ---------------------------------------------------------------------------
# ZIP metadata patch
# ---------------------------------------------------------------------------
# The Supernote firmware's sticker-pack importer is strict about ZIP entry
# metadata.  Working packs (e.g. christmas2025.snstk) require:
#   flag_bits        = 0x800       (UTF-8 filename flag)
#   version_made_by  = 0x0333      (Unix host 0x03 + zip version 51)
#   version_needed   = 20          (2.0 — required for DEFLATE)
#   external_attr    = 0x81800000  (Unix mode S_IFREG | 0600)
# The Unix host byte (high byte of version_made_by) is critical: the
# external_attr above holds Unix file-mode bits, and the firmware only
# interprets them — and thus recognises the entry as a sticker — when the
# host byte declares Unix.  Writing version_made_by=51 (host byte 0x00 =
# FAT/DOS) leaves the mode bits uninterpretable, and the importer silently
# rejects every entry, so all stickers show blank in the picker.
#
# Python's zipfile module forcibly resets flag_bits=0 in writestr(), so we
# patch the real ZIP headers after generation.

# version_made_by: Unix host (0x03) in the high byte, ZIP spec version 51
# in the low byte → 0x0333.  Matches official Supernote packs exactly.
_ZIP_VERSION_MADE_BY = (0x03 << 8) | 51
_ZIP_VERSION_NEEDED = 20  # 2.0 — minimum for DEFLATE-compressed entries

_ZIP_LOCAL_FILE_HEADER_SIGNATURE = 0x04034B50
_ZIP_CENTRAL_DIRECTORY_SIGNATURE = 0x02014B50
_ZIP_EOCD_SIGNATURE = 0x06054B50

def _find_eocd_offset(data: bytes) -> int:
    """Return the End of Central Directory offset for a ZIP archive."""
    max_comment_len = 0xFFFF
    search_start = max(0, len(data) - (22 + max_comment_len))
    offset = data.rfind(b"PK\x05\x06", search_start)
    if offset == -1:
        raise ValueError("ZIP EOCD record not found")
    return offset


def _patch_zip_flags(data: bytes) -> bytes:
    """Patch ZIP metadata required by Supernote on actual ZIP headers only."""
    buf = bytearray(data)
    eocd_offset = _find_eocd_offset(buf)
    eocd = struct.unpack_from("<IHHHHIIH", buf, eocd_offset)
    if eocd[0] != _ZIP_EOCD_SIGNATURE:
        raise ValueError("Invalid ZIP EOCD signature")

    central_directory_size = eocd[5]
    central_directory_offset = eocd[6]
    central_directory_end = central_directory_offset + central_directory_size

    cursor = central_directory_offset
    while cursor < central_directory_end:
        signature = struct.unpack_from("<I", buf, cursor)[0]
        if signature != _ZIP_CENTRAL_DIRECTORY_SIGNATURE:
            raise ValueError(f"Invalid central directory signature at offset {cursor}")

        # version_made_by (host byte + version) — MUST keep the Unix host
        # byte or the firmware rejects the entry (see module note above).
        struct.pack_into("<H", buf, cursor + 4, _ZIP_VERSION_MADE_BY)
        struct.pack_into("<H", buf, cursor + 6, _ZIP_VERSION_NEEDED)  # version_needed
        flags = struct.unpack_from("<H", buf, cursor + 8)[0]
        struct.pack_into("<H", buf, cursor + 8, flags | 0x800)
        struct.pack_into("<I", buf, cursor + 38, 0x81800000)

        filename_len, extra_len, comment_len = struct.unpack_from("<HHH", buf, cursor + 28)
        local_header_offset = struct.unpack_from("<I", buf, cursor + 42)[0]

        local_signature = struct.unpack_from("<I", buf, local_header_offset)[0]
        if local_signature != _ZIP_LOCAL_FILE_HEADER_SIGNATURE:
            raise ValueError(
                f"Invalid local file header signature at offset {local_header_offset}"
            )
        struct.pack_into("<H", buf, local_header_offset + 4, _ZIP_VERSION_NEEDED)
        flags = struct.unpack_from("<H", buf, local_header_offset + 6)[0]
        struct.pack_into("<H", buf, local_header_offset + 6, flags | 0x800)

        cursor += 46 + filename_len + extra_len + comment_len

    if cursor != central_directory_end:
        raise ValueError("Central directory parsing did not end on the expected boundary")
    return bytes(buf)


# ---------------------------------------------------------------------------
# High-level SNSTK pack builder
# ---------------------------------------------------------------------------

def build_snstk(
    images: list[tuple[str, str | Path | BinaryIO]],
    size: int = DEFAULT_STICKER_SIZE,
    device: str = "N5",
    trim: bool = True,
    x_offset: float | None = None,
    y_offset: float = 0.0,
    margin: int = DEFAULT_MARGIN,
    pad_square: bool = False,
    upscale: bool = True,
) -> bytes:
    """Build an SNSTK sticker pack and return its raw bytes.

    Args:
        images: A list of ``(name, source)`` pairs, where *name* is the
                desired sticker name (used as the entry name inside the
                ZIP) and *source* is anything accepted by
                :func:`image_to_pixels`.
        size:   Length of each sticker's longest edge, in pixels.  Sources
                smaller than this are scaled up so the requested size is
                always honoured.
        device: Target device code.
        trim:   Crop transparent borders before resizing (default ``True``).
        margin: Transparent padding on each side, in pixels (default ``0``).
        pad_square: Centre artwork on a square canvas instead of keeping
                its own aspect ratio (legacy behaviour, default ``False``).
        upscale: Allow sources smaller than *size* to be scaled up
                (default ``True``).

    Returns:
        Raw bytes of the ``.snstk`` archive.

    Raises:
        ValueError: If *images* is empty.
    """
    if not images:
        raise ValueError("At least one image is required.")

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, source in images:
            pixels, w, h, pil_img, is_bw = image_to_pixels(
                source, size, trim=trim, margin=margin,
                pad_square=pad_square, upscale=upscale,
            )
            sticker_data = build_sticker(
                pixels, w, h, device, pil_image=pil_img,
                x_offset=x_offset, y_offset=y_offset, is_bw=is_bw,
            )
            entry_name = f"{name}.sticker"

            info = zipfile.ZipInfo(entry_name)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Declare Unix host + version 51 so external_attr's Unix mode
            # bits are valid.  create_system defaults to FAT (0) on Windows,
            # which would zero the host byte — set it explicitly.  (The
            # _patch_zip_flags pass re-asserts this too, defensively.)
            info.create_system = 3  # Unix
            info.create_version = 51
            info.external_attr = 0x81800000
            zf.writestr(info, sticker_data)

    # Python's zipfile.writestr() forcibly resets flag_bits to 0.
    # The Supernote firmware requires flag_bits=0x800 (UTF-8 filename
    # flag) — packs without it are silently rejected by the device.
    # Post-process the ZIP bytes to set the correct flags.
    return _patch_zip_flags(buf.getvalue())

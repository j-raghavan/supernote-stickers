"""Core image-to-SNSTK conversion logic.

This module is intentionally free of I/O side-effects so it can be
used from both the CLI and the web application without modification
(Single Responsibility / Dependency-Inversion principles).
"""

from __future__ import annotations

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
# Known devices
# ---------------------------------------------------------------------------

DEVICES: dict[str, dict] = {
    "N5":  {"name": "A5X2 Manta / A6X2 Nomad", "screen": (1920, 2560)},
    "A5X": {"name": "A5X",                      "screen": (1404, 1872)},
    "A6X": {"name": "A6X",                      "screen": (1404, 1872)},
}

DEFAULT_STICKER_SIZE: int = 180

# Supported image extensions (anything Pillow can open)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"}
)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

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

def image_to_pixels(
    source: str | Path | BinaryIO,
    size: int = DEFAULT_STICKER_SIZE,
    trim: bool = True,
) -> tuple[list[int], int, int, Image.Image]:
    """Load an image and return ``(pixels, width, height, pil_image)``.

    *source* may be a file path or any file-like object (e.g. a
    ``BytesIO`` from a web upload).  All Pillow-supported formats are
    accepted.  The returned *pil_image* is the resized RGBA image used
    for high-quality trail dithering.

    When *trim* is ``True`` (the default), transparent borders are
    cropped away before resizing so the visible content fills as much
    of the sticker area as possible.
    """
    img = Image.open(source).convert("RGBA")
    orig_max_dim = max(img.size)

    if trim:
        bbox = img.getbbox()  # bounding box of non-transparent pixels
        if bbox is not None:
            img = img.crop(bbox)
        # If bbox is None the image is fully transparent — keep as-is.

    # Scale the trimmed image back to the original canvas size so the
    # sticker matches the user's intended dimensions.  For images that
    # were originally larger than *size*, cap at (size − 10) so there
    # is a small margin and the content doesn't touch the edges.
    if orig_max_dim > size:
        target = size - 10
    else:
        target = orig_max_dim

    trimmed_w, trimmed_h = img.size
    scale = min(target / trimmed_w, target / trimmed_h)
    new_w = max(1, round(trimmed_w * scale))
    new_h = max(1, round(trimmed_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Centre on a size×size canvas so the bitmap and trail layers are
    # consistently positioned, matching the reference coordinate system
    # that the fixed digitiser offsets (15200, 200) were calibrated against.
    if new_w != size or new_h != size:
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        offset_x = (size - new_w) // 2
        offset_y = (size - new_h) // 2
        canvas.paste(img, (offset_x, offset_y))
        img = canvas

    w, h = img.size

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

    return pixels, w, h, img


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
) -> bytes:
    """Build a single stroke record from contour points.

    Each contour_points entry is ``(x, y)`` in sticker pixel coordinates.
    The stroke format follows the PySN/snex ``pen_strokes_dict_to_bytes``
    reference implementation exactly.

    The **vector-points** section is populated with densely interpolated
    samples along the contour (mimicking a real pen trajectory), while the
    **contours** section stores the original simplified polygon vertices.

    Args:
        contour_points: List of (x, y) float coordinates from OpenCV.
        stroke_nb: Stroke sequence number (1-based).
        device: Device code key from :data:`DEVICES`.
        screen_w: Screen width for the target device.
        screen_h: Screen height for the target device.

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

    # ---- Coordinate scaling ----
    # The Supernote firmware expects:
    #   bbox          → sticker pixel coordinates (0..width/height)
    #   vector points → pen digitizer coordinates (much larger range)
    # Working stickers (Christmas Dog) show a ~8x scale between the two.
    # Device info encodes digitizer dims (21632 × 16224 for N5).
    _VEC_SCALE = 8.0
    _VEC_OFFSET_X = 15200
    _VEC_OFFSET_Y = 200

    # Compute bounding box in the SAME digitizer coordinate space as vectors
    # so the firmware sees consistent coordinates for both.
    px_xs = [p[0] for p in contour_points]
    px_ys = [p[1] for p in contour_points]
    min_x = int(min(px_xs) * _VEC_SCALE + _VEC_OFFSET_X)
    max_x = int(max(px_xs) * _VEC_SCALE + _VEC_OFFSET_X)
    min_y = int(min(px_ys) * _VEC_SCALE + _VEC_OFFSET_Y)
    max_y = int(max(px_ys) * _VEC_SCALE + _VEC_OFFSET_Y)
    avg_x = (min_x + max_x) // 2
    avg_y = (min_y + max_y) // 2

    buf = bytearray()

    # ---- Stroke header (20 bytes) ----
    buf += struct.pack('B', 10)           # pen_type = 10 (standard)
    buf += b'\x00\x00\x00'
    buf += struct.pack('B', 0)            # pen_color = 0 (black)
    buf += b'\x00\x00\x00'
    buf += struct.pack('<H', 220)         # pen_weight = 220
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
        digi_x = int(x * _VEC_SCALE + _VEC_OFFSET_X)
        digi_y = int(y * _VEC_SCALE + _VEC_OFFSET_Y)
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
# Trails builder (Floyd-Steinberg dithering + OpenCV contours)
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

    Fallback for when the original PIL image isn't available.
    """
    code_to_gray: dict[int, int] = {COLORCODE_BLACK: 0, COLORCODE_BACKGROUND: 255}
    for idx, code in enumerate(AA_LEVELS):
        code_to_gray[code] = int((idx + 1) / (len(AA_LEVELS) + 1) * 255)

    gray = np.full((height, width), 255, dtype=np.float64)
    for i, code in enumerate(pixels):
        gray[i // width, i % width] = code_to_gray.get(code, 255)
    return gray


def _enhance_contrast(gray: np.ndarray) -> np.ndarray:
    """Stretch contrast and apply gamma correction for better dithering.

    1. Contrast stretch: remap [min, max] of non-white pixels to [0, 255].
    2. Gamma correction (γ=0.6): darken midtones so that light skin tones
       and subtle features produce enough black dots after dithering.
    """
    # Find the value range of non-white pixels (actual content)
    content_mask = gray < 250
    if not content_mask.any():
        return gray
    lo = float(gray[content_mask].min())
    hi = float(gray[content_mask].max())
    if hi - lo < 1:
        return gray

    # Contrast stretch
    out = gray.copy()
    out[content_mask] = (gray[content_mask] - lo) / (hi - lo) * 255.0
    out = np.clip(out, 0, 255)

    # Gamma correction (< 1 darkens midtones, 0.4 = aggressive)
    out[content_mask] = 255.0 * (out[content_mask] / 255.0) ** 0.4

    return out


def _floyd_steinberg_dither(gray: np.ndarray) -> np.ndarray:
    """Apply Floyd-Steinberg error-diffusion dithering.

    Takes a float64 grayscale image (0=black, 255=white) and returns a
    uint8 binary image (0 or 255) that, when viewed at a distance,
    approximates the original tonal gradation.
    """
    h, w = gray.shape
    img = _enhance_contrast(gray)

    for y in range(h):
        for x in range(w):
            old_val = img[y, x]
            new_val = 0.0 if old_val < 128 else 255.0
            img[y, x] = new_val
            err = old_val - new_val

            if x + 1 < w:
                img[y, x + 1] += err * 7.0 / 16.0
            if y + 1 < h:
                if x - 1 >= 0:
                    img[y + 1, x - 1] += err * 3.0 / 16.0
                img[y + 1, x] += err * 5.0 / 16.0
                if x + 1 < w:
                    img[y + 1, x + 1] += err * 1.0 / 16.0

    return (img < 128).astype(np.uint8) * 255


def build_trails(
    pixels: list[int],
    width: int,
    height: int,
    device: str = "N5",
    pil_image: Image.Image | None = None,
) -> bytes:
    """Build the trails section using scanline fills on dithered bitmap.

    Converts the grayscale pixel data to a black-and-white halftone using
    Floyd-Steinberg error-diffusion dithering, then creates strokes by
    finding solid horizontal runs of black pixels (scanline fill approach).
    Each row of the dithered image produces one or more strokes for its
    black pixel runs, creating a proper newspaper-style halftone pattern.

    When *pil_image* is provided, dithering works directly from the full
    256-level RGBA data instead of the lossy 17-level colour codes.

    Args:
        pixels: Supernote colour codes (row-major, length = *width* × *height*).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.
        pil_image: Optional PIL RGBA image for high-quality dithering.

    Returns:
        Raw bytes for the trails block (**excluding** the leading uint32
        length prefix — the caller wraps it).
    """
    _pack_u32 = struct.Struct("<I").pack
    screen_w, screen_h = DEVICES.get(device, DEVICES["N5"])["screen"]

    # Dither from full RGBA data when available (much higher quality)
    if pil_image is not None:
        gray = _rgba_image_to_grayscale(pil_image)
    else:
        gray = _pixels_to_grayscale(pixels, width, height)
    dithered = _floyd_steinberg_dither(gray)

    # Generate scanline fill strokes from dithered mask
    # dithered is uint8: 0 for black, 255 for white
    all_strokes = bytearray()
    stroke_nb = 1004

    for y in range(height):
        row = dithered[y]
        x = 0
        while x < width:
            if row[x] == 255:  # white pixel, skip
                x += 1
                continue
            x_start = x
            while x < width and row[x] == 0:  # black pixel
                x += 1
            x_end = x - 1
            if x_end - x_start < 0:
                continue

            # Create rectangle points for this run (pixel space)
            # Must have non-zero height so firmware renders a filled polygon
            # Mirror X axis — the firmware renders trails with X inverted
            run_pts = [
                (float(width - 1 - x_start), float(y)),
                (float(width - 1 - x_end), float(y)),
                (float(width - 1 - x_end), float(y + 1)),
                (float(width - 1 - x_start), float(y + 1)),
            ]

            stroke_data = _build_stroke(run_pts, stroke_nb, device, screen_w, screen_h)
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
            fallback_pts, 1004, device, screen_w, screen_h,
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
) -> bytes:
    """Assemble a complete ``.sticker`` binary from pixel data.

    Args:
        pixels: Supernote colour codes (one per pixel, row-major).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.
        pil_image: Optional PIL RGBA image for high-quality trail dithering.

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
    trails_data = build_trails(pixels, width, height, device, pil_image=pil_image)
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
#   flag_bits      = 0x800  (UTF-8 filename flag)
#   create_version = 51
#   external_attr  = 0x81800000
# Python's zipfile module forcibly resets flag_bits=0 in writestr(), so we
# patch the real ZIP headers after generation.

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

        struct.pack_into("<H", buf, cursor + 4, 51)  # create_version
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
) -> bytes:
    """Build an SNSTK sticker pack and return its raw bytes.

    Args:
        images: A list of ``(name, source)`` pairs, where *name* is the
                desired sticker name (used as the entry name inside the
                ZIP) and *source* is anything accepted by
                :func:`image_to_pixels`.
        size:   Maximum sticker dimension in pixels.
        device: Target device code.
        trim:   Crop transparent borders before resizing (default ``True``).

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
            pixels, w, h, pil_img = image_to_pixels(source, size, trim=trim)
            sticker_data = build_sticker(pixels, w, h, device, pil_image=pil_img)
            entry_name = f"{name}.sticker"

            info = zipfile.ZipInfo(entry_name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_version = 51
            info.external_attr = 0x81800000
            zf.writestr(info, sticker_data)

    # Python's zipfile.writestr() forcibly resets flag_bits to 0.
    # The Supernote firmware requires flag_bits=0x800 (UTF-8 filename
    # flag) — packs without it are silently rejected by the device.
    # Post-process the ZIP bytes to set the correct flags.
    return _patch_zip_flags(buf.getvalue())

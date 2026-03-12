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
) -> tuple[list[int], int, int]:
    """Load an image and return ``(pixels, width, height)``.

    *source* may be a file path or any file-like object (e.g. a
    ``BytesIO`` from a web upload).  All Pillow-supported formats are
    accepted.
    """
    img = Image.open(source).convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
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

    return pixels, w, h


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
# Trails builder
# ---------------------------------------------------------------------------

def build_trails(pixels: list[int], width: int, height: int, device: str = "N5") -> bytes:
    """Build the trails section required by the Supernote firmware.

    The Supernote device uses the trails section to render stickers when
    they are inserted into notes.  Without valid trails data the device
    displays "Inserting..." indefinitely.

    This implementation uses a binary template extracted from a known-working
    sticker (christmas2025.snstk).  The template contains a minimal valid
    record with 4 coordinate pairs that the firmware can parse successfully.
    Only the screen dimensions are adjusted per target device.

    The trails data does not affect the visual appearance of the sticker
    (the bitmap handles that) -- it just needs to be structurally valid
    so the firmware doesn't hang.

    Args:
        pixels: Supernote colour codes (row-major, length = *width* x *height*).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.

    Returns:
        Raw bytes for the trails block (**excluding** the leading uint32
        length prefix -- the caller wraps it).
    """
    _pack_u32 = struct.Struct("<I").pack

    screen_w, screen_h = DEVICES.get(device, DEVICES["N5"])["screen"]

    # ------------------------------------------------------------------
    # Minimal valid record template (536 bytes)
    # ------------------------------------------------------------------
    # Extracted from record 11 of christmas2025.snstk (Christmas Dog).
    # Structure (all little-endian):
    #   Bytes   0- 27: Record marker  (0x20, 0xFFFFFFFF, type=3, pad, 5000, pad)
    #   Bytes  28- 79: Tool name      "others" null-padded to 52 bytes
    #   Bytes  80-115: Bounding box   9 x uint32
    #   Bytes 116-167: Annotation     "superNoteNote" null-padded to 52 bytes
    #   Bytes 168-191: Post-annot     uint32(1) + 20 zero bytes
    #   Bytes 192-195: Coord count    uint32(4)
    #   Bytes 196-227: Coordinates    4 x (uint32 x, uint32 y) pairs
    #   Bytes 228-231: Timing count   uint32(4)
    #   Bytes 232-239: Timing data    4 x uint16 values
    #   Bytes 240-243: Pressure count uint32(4)
    #   Bytes 244-259: Pressure data  4 x (uint16, uint16) pairs
    #   Bytes 260-263: Pen-type count uint32(4)
    #   Bytes 264-267: Pen-type data  4 x uint8(1)
    #   Bytes 268-413: Per-stroke metadata + float32 coords + sub-stroke data
    #   Bytes 414-535: Footer (FF block, double, screen dims, "none" strings)
    #
    # Screen width  is at byte offset 455 (uint32 LE)
    # Screen height is at byte offset 459 (uint32 LE)
    # ------------------------------------------------------------------
    _RECORD_TEMPLATE_HEX = (
        "20000000ffffffff03000000000000000000000088130000000000006f7468657273000000000000"
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "810000009b00000026060000f0000000830000009d0000001a00000080540000603f000073757065"
        "724e6f74654e6f746500000000000000000000000000000000000000000000000000000000000000"
        "00000000000000000100000000000000000000000000000000000000000000000400000025050000"
        "083b000025050000083b0000270500000a3b0000290500000b3b000004000000c000f8004901f400"
        "04000000540b9001540b9001540bf401f00a58020400000001010101000000000000000000000000"
        "61000000ec0300000000000000000000000000000000000001000000010000000000000000000000"
        "0100000001000000000000000000000000000000000101000000080000007a4403438a571b43a06d"
        "024388241b437463014306e41b431a310143d8b91c43a2ac01434c791d438483024348ac1d43b28d"
        "0343caec1c4310c0034302171c4301000000ffffffffffffffffffffffffffffffffffffffff4dac"
        "33dcb771d43f002f0000000000000080070000000a00000000000000040000006e6f6e6504000000"
        "6e6f6e6500000000030000000200000000000000000000000000000000000000931400000a000000"
        "00000000dc0000000a00000000000000"
    )
    record = bytearray(bytes.fromhex(_RECORD_TEMPLATE_HEX))

    # Patch screen dimensions for the target device
    struct.pack_into("<I", record, 455, screen_w)
    struct.pack_into("<I", record, 459, screen_h)

    # ------------------------------------------------------------------
    # Global header (28 bytes)
    # ------------------------------------------------------------------
    buf = bytearray()
    buf += _pack_u32(1)       # stroke count (1 record)
    buf += _pack_u32(4)       # total coordinate count in record
    buf += _pack_u32(10)      # constant (observed in all working stickers)
    buf += _pack_u32(0)       # reserved
    buf += _pack_u32(4)       # secondary value
    buf += _pack_u32(10)      # constant
    buf += _pack_u32(0)       # reserved

    buf += record

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
    # 15-char mixed-case alphanumeric suffix to match official format
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    rng = random.Random(uuid.uuid4().int)
    suffix = "".join(rng.choice(alphabet) for _ in range(15))
    return f"F{timestamp}{ms}{suffix}"


def build_sticker(
    pixels: list[int],
    width: int,
    height: int,
    device: str = "N5",
) -> bytes:
    """Assemble a complete ``.sticker`` binary from pixel data.

    Args:
        pixels: Supernote colour codes (one per pixel, row-major).
        width:  Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code key from :data:`DEVICES`.

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
    trails_data = build_trails(pixels, width, height, device)
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
# High-level SNSTK pack builder
# ---------------------------------------------------------------------------

def build_snstk(
    images: list[tuple[str, str | Path | BinaryIO]],
    size: int = DEFAULT_STICKER_SIZE,
    device: str = "N5",
) -> bytes:
    """Build an SNSTK sticker pack and return its raw bytes.

    Args:
        images: A list of ``(name, source)`` pairs, where *name* is the
                desired sticker name (used as the entry name inside the
                ZIP) and *source* is anything accepted by
                :func:`image_to_pixels`.
        size:   Maximum sticker dimension in pixels.
        device: Target device code.

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
            pixels, w, h = image_to_pixels(source, size)
            sticker_data = build_sticker(pixels, w, h, device)
            zf.writestr(f"{name}.sticker", sticker_data)

    return buf.getvalue()

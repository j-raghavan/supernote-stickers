#!/usr/bin/env python3
"""Convert PNG images to Supernote .snstk sticker pack format.

Usage:
    python3 png2snstk.py output.snstk image1.png image2.png ...
    python3 png2snstk.py output.snstk *.png
    python3 png2snstk.py output.snstk input_folder/

The sticker names are derived from the PNG filenames (without extension).

Requirements:
    pip install Pillow
"""

import argparse
import os
import queue
import struct
import sys
import time
import uuid
import zipfile
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow is required. Install with: pip install Pillow", file=sys.stderr)
    sys.exit(1)


# Supernote RLE color codes
COLORCODE_BLACK = 0x61
COLORCODE_BACKGROUND = 0x62

# Anti-aliasing grayscale levels (0x0F=near black, 0xEF=near transparent)
# Maps alpha 0-255 to the nearest color code
AA_LEVELS = [0x0F, 0x1F, 0x2F, 0x3F, 0x4F, 0x5F, 0x6F, 0x7F,
             0x8F, 0x9F, 0xAF, 0xBF, 0xCF, 0xDF, 0xEF]

# Known device codes and their screen resolutions (width x height)
# Sticker pixel size is independent of device resolution, but the device
# code is stored in the header metadata as APPLY_EQUIPMENT.
DEVICES = {
    "N5":  {"name": "A5X2 Manta / A6X2 Nomad", "screen": (1920, 2560)},
    "A5X": {"name": "A5X",                      "screen": (1404, 1872)},
    "A6X": {"name": "A6X",                      "screen": (1404, 1872)},
}

DEFAULT_STICKER_SIZE = 180


def alpha_to_colorcode(alpha: int) -> int:
    """Convert an alpha value (0=transparent, 255=opaque) to a Supernote color code."""
    if alpha < 9:
        return COLORCODE_BACKGROUND
    if alpha > 246:
        return COLORCODE_BLACK
    # Map alpha to one of the 15 anti-aliasing levels
    # alpha 255 = black (0x61), alpha 0 = background (0x62)
    # AA levels: 0x0F (near black, high alpha) to 0xEF (near transparent, low alpha)
    index = 14 - round((alpha / 255) * 14)
    return AA_LEVELS[index]


def png_to_pixels(image_path: str, size: int = DEFAULT_STICKER_SIZE) -> tuple[list[int], int, int]:
    """Load a PNG and convert to a list of Supernote color codes.

    Returns (pixels, width, height).
    """
    img = Image.open(image_path).convert("RGBA")

    # Resize to fit within size x size, preserving aspect ratio
    img.thumbnail((size, size), Image.LANCZOS)
    w, h = img.size

    pixels = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = img.getpixel((x, y))
            if a == 0:
                pixels.append(COLORCODE_BACKGROUND)
            else:
                # Convert to grayscale luminance, then combine with alpha
                # For stickers, we treat darker pixels as more "ink"
                gray = int(0.299 * r + 0.587 * g + 0.114 * b)
                # Invert: black ink = high alpha, white = transparent
                ink_alpha = int((255 - gray) * (a / 255))
                pixels.append(alpha_to_colorcode(ink_alpha))

    return pixels, w, h


def encode_rle(pixels: list[int]) -> bytes:
    """Encode pixels using Supernote's RattaRLE compression."""
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


def generate_file_id() -> str:
    """Generate a unique file ID in Supernote's format."""
    timestamp = time.strftime("%Y%m%d%H%M%S")
    # Add milliseconds and random suffix
    ms = f"{int(time.time() * 1000) % 1000:03d}"
    import random as _rng
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    r = _rng.Random(uuid.uuid4().int)
    suffix = "".join(r.choice(alphabet) for _ in range(15))
    return f"F{timestamp}{ms}{suffix}"


def _build_trails(pixels: list[int], width: int, height: int, device: str = "N5") -> bytes:
    """Build the trails section required by the Supernote firmware.

    Uses a binary template extracted from a known-working sticker
    (christmas2025.snstk record 11).  Only screen dimensions are
    adjusted for the target device.
    """
    _pack_u32 = struct.Struct("<I").pack

    screen_w, screen_h = DEVICES.get(device, DEVICES["N5"])["screen"]

    # Minimal valid record template (536 bytes) from christmas2025.snstk
    # Screen width at byte offset 455, screen height at byte offset 459.
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

    # Patch screen dimensions
    struct.pack_into("<I", record, 455, screen_w)
    struct.pack_into("<I", record, 459, screen_h)

    # Global header (28 bytes)
    buf = bytearray()
    buf += _pack_u32(1)       # stroke count
    buf += _pack_u32(4)       # total coords
    buf += _pack_u32(10)      # constant
    buf += _pack_u32(0)       # reserved
    buf += _pack_u32(4)       # secondary value
    buf += _pack_u32(10)      # constant
    buf += _pack_u32(0)       # reserved

    buf += record
    return bytes(buf)


def build_sticker(pixels: list[int], width: int, height: int, device: str = "N5") -> bytes:
    """Build a complete .sticker file from pixel data.

    Args:
        pixels: List of Supernote color codes.
        width: Sticker width in pixels.
        height: Sticker height in pixels.
        device: Device code (N5=Manta/Nomad, A5X, A6X).
    """
    # --- Section 1: File header ---
    magic = b"stck"
    version = b"SN_FILE_VER_20230015"
    file_id = generate_file_id()
    header_meta = (
        f"<FILE_TYPE:STICKER>"
        f"<APPLY_EQUIPMENT:{device}>"
        f"<FILE_PARSE_TYPE:0>"
        f"<RATTA_ETMD:0>"
        f"<FILE_ID:{file_id}>"
        f"<ANTIALIASING_CONVERT:2>"
    ).encode("ascii")
    header_meta_len = struct.pack("<I", len(header_meta))

    header = magic + version + header_meta_len + header_meta
    bitmap_offset = len(header)

    # --- Section 2: Bitmap (RLE-encoded) ---
    rle_data = encode_rle(pixels)
    bitmap_block = struct.pack("<I", len(rle_data)) + rle_data

    # --- Section 3: Trails (required for sticker insertion) ---
    trails_offset = bitmap_offset + len(bitmap_block)
    trails_data = _build_trails(pixels, width, height, device)
    trails_block = struct.pack("<I", len(trails_data)) + trails_data

    # --- Section 4: Sticker rect ---
    rect_offset = trails_offset + len(trails_block)
    rect_str = f"0,0,{width},{height}".encode("ascii")
    rect_block = struct.pack("<I", len(rect_str)) + rect_str

    # --- Section 5: Footer ---
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


def create_snstk(output_path: str, png_paths: list[str], size: int = DEFAULT_STICKER_SIZE,
                 device: str = "N5") -> None:
    """Create a .snstk sticker pack from PNG files.

    Args:
        output_path: Path for the output .snstk file.
        png_paths: List of PNG file paths.
        size: Maximum sticker dimension (default 180).
        device: Target device type.
    """
    if not png_paths:
        print("Error: No PNG files provided.", file=sys.stderr)
        sys.exit(1)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for png_path in png_paths:
            name = Path(png_path).stem
            print(f"  Converting: {name}")

            pixels, w, h = png_to_pixels(png_path, size)
            sticker_data = build_sticker(pixels, w, h, device)

            zf.writestr(f"{name}.sticker", sticker_data)

    print(f"\nCreated {output_path} with {len(png_paths)} sticker(s)")
    print(f"Copy to your Supernote's EXPORT folder and import from Settings > Stickers")


def main():
    parser = argparse.ArgumentParser(
        description="Convert PNG images to Supernote .snstk sticker pack format"
    )
    parser.add_argument("output", help="Output .snstk file path")
    parser.add_argument("inputs", nargs="+", help="PNG files or directories containing PNGs")
    parser.add_argument(
        "-s", "--size", type=int, default=DEFAULT_STICKER_SIZE,
        help=f"Maximum sticker dimension in pixels (default: {DEFAULT_STICKER_SIZE})"
    )
    device_help = "Target device code (default: N5). Known codes: " + ", ".join(
        f"{code}={info['name']}" for code, info in DEVICES.items()
    )
    parser.add_argument(
        "-d", "--device", default="N5",
        help=device_help,
    )

    args = parser.parse_args()

    # Collect all PNG paths
    png_paths = []
    for input_path in args.inputs:
        p = Path(input_path)
        if p.is_dir():
            png_paths.extend(sorted(str(f) for f in p.glob("*.png")))
        elif p.is_file() and p.suffix.lower() == ".png":
            png_paths.append(str(p))
        else:
            print(f"Warning: Skipping {input_path} (not a PNG file or directory)", file=sys.stderr)

    if not png_paths:
        print("Error: No PNG files found in the provided inputs.", file=sys.stderr)
        sys.exit(1)

    # Ensure output has .snstk extension
    output = args.output
    if not output.endswith(".snstk"):
        output += ".snstk"

    print(f"Creating sticker pack: {output}")
    print(f"Sticker size: {args.size}x{args.size} max")
    print(f"Target device: {args.device}")
    print(f"Input files: {len(png_paths)}\n")

    create_snstk(output, png_paths, args.size, args.device)


if __name__ == "__main__":
    main()

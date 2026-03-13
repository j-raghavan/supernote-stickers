#!/usr/bin/env python3
"""Convert PNG images to Supernote .snstk sticker pack format.

Usage:
    python3 png2snstk.py output.snstk image1.png image2.png ...
    python3 png2snstk.py output.snstk *.png
    python3 png2snstk.py output.snstk input_folder/

The sticker names are derived from the PNG filenames (without extension).

Requirements:
    pip install Pillow opencv-python-headless
"""

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow is required. Install with: pip install Pillow", file=sys.stderr)
    sys.exit(1)

try:
    import cv2  # noqa: F401
except ImportError:
    print("Error: OpenCV is required. Install with: pip install opencv-python-headless",
          file=sys.stderr)
    sys.exit(1)

# Import the core conversion logic from the package
# If running standalone, add src/ to path
try:
    from supernote_stickers.converter import (
        DEVICES,
        DEFAULT_STICKER_SIZE,
        build_snstk,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from supernote_stickers.converter import (
        DEVICES,
        DEFAULT_STICKER_SIZE,
        build_snstk,
    )


def create_snstk(
    output_path: str,
    png_paths: list[str],
    size: int = DEFAULT_STICKER_SIZE,
    device: str = "N5",
) -> None:
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

    images = []
    for png_path in png_paths:
        name = Path(png_path).stem
        print(f"  Converting: {name}")
        images.append((name, png_path))

    Path(output_path).write_bytes(build_snstk(images, size=size, device=device))
    print(f"\nCreated {output_path} with {len(png_paths)} sticker(s)")
    print("Copy to your Supernote's EXPORT folder and import from Settings > Stickers")


def main():
    parser = argparse.ArgumentParser(
        description="Convert PNG images to Supernote .snstk sticker pack format"
    )
    parser.add_argument("output", help="Output .snstk file path")
    parser.add_argument("inputs", nargs="+", help="PNG files or directories containing PNGs")
    parser.add_argument(
        "-s", "--size", type=int, default=DEFAULT_STICKER_SIZE,
        help=f"Maximum sticker dimension in pixels (default: {DEFAULT_STICKER_SIZE})",
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
            print(f"Warning: Skipping {input_path} (not a PNG file or directory)",
                  file=sys.stderr)

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

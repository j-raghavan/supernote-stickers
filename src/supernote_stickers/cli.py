"""Command-line interface for the Supernote sticker converter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from supernote_stickers.converter import (
    DEFAULT_MARGIN,
    DEFAULT_STICKER_SIZE,
    DEVICES,
    SUPPORTED_EXTENSIONS,
    build_snstk,
    clamp_margin,
)


def _collect_images(inputs: list[str]) -> list[tuple[str, Path]]:
    """Expand file and directory arguments into ``(name, path)`` pairs."""
    result: list[tuple[str, Path]] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for ext in SUPPORTED_EXTENSIONS:
                result.extend(
                    (f.stem, f) for f in sorted(p.glob(f"*{ext}"))
                )
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            result.append((p.stem, p))
        else:
            print(f"Warning: skipping {raw!r} (unsupported file or not found)", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``png2snstk`` command."""
    parser = argparse.ArgumentParser(
        description="Convert images to a Supernote .snstk sticker pack",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("output", help="Output .snstk file path")
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Image files or directories to include",
    )
    parser.add_argument(
        "-s", "--size",
        type=int,
        default=DEFAULT_STICKER_SIZE,
        help=(
            "Length of the sticker's longest edge in pixels. Smaller source "
            "images are scaled up so this size is always honoured."
        ),
    )
    device_choices = list(DEVICES.keys())
    parser.add_argument(
        "-d", "--device",
        default="N5",
        choices=device_choices,
        help=(
            "Target device – "
            + ", ".join(f"{k}={v['name']}" for k, v in DEVICES.items())
        ),
    )
    parser.add_argument(
        "--no-trim",
        action="store_true",
        default=False,
        help="Disable automatic trimming of transparent borders",
    )
    parser.add_argument(
        "-m", "--margin",
        type=int,
        default=DEFAULT_MARGIN,
        help=(
            "Transparent padding on each side in pixels (included in --size). "
            "Default 0 — the artwork fills the sticker exactly."
        ),
    )
    parser.add_argument(
        "--pad-square",
        action="store_true",
        default=False,
        help=(
            "Centre the artwork on a square canvas instead of keeping its own "
            "aspect ratio (legacy behaviour; adds transparent padding)"
        ),
    )
    parser.add_argument(
        "--no-upscale",
        action="store_true",
        default=False,
        help="Never scale a source image up; leave smaller images at native size",
    )

    args = parser.parse_args(argv)

    images = _collect_images(args.inputs)
    if not images:
        print("Error: no supported image files found.", file=sys.stderr)
        return 1

    if args.size < 1:
        print(f"Error: --size must be >= 1, got {args.size}.", file=sys.stderr)
        return 1

    # A margin of half the size or more would leave no room for artwork, so it
    # is clamped rather than silently producing a sticker larger than --size.
    margin = clamp_margin(args.size, args.margin)
    if margin != args.margin:
        print(
            f"Warning: --margin {args.margin} is too large for --size {args.size}; "
            f"using {margin} (the largest that fits).",
            file=sys.stderr,
        )

    output = Path(args.output)
    if output.suffix.lower() != ".snstk":
        output = output.with_suffix(".snstk")

    print(f"Creating sticker pack: {output}")
    if args.pad_square:
        print(f"Sticker size: {args.size}×{args.size} (square canvas)")
    elif args.no_upscale:
        print(f"Sticker size: up to {args.size}px longest edge (aspect preserved, no upscaling)")
    else:
        print(f"Sticker size: {args.size}px longest edge (aspect preserved)")
    if margin:
        print(f"Margin: {margin}px per side")
    print(f"Target device: {args.device} ({DEVICES[args.device]['name']})")
    print(f"Images: {len(images)}")

    data = build_snstk(
        images,
        size=args.size,
        device=args.device,
        trim=not args.no_trim,
        margin=margin,
        pad_square=args.pad_square,
        upscale=not args.no_upscale,
    )
    output.write_bytes(data)

    print(f"\nDone – {output} ({len(data):,} bytes, {len(images)} sticker(s))")
    print("Copy to your Supernote's EXPORT folder and import from Settings › Stickers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

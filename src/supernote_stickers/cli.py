"""Command-line interface for the Supernote sticker converter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from supernote_stickers.converter import (
    DEFAULT_STICKER_SIZE,
    DEVICES,
    SUPPORTED_EXTENSIONS,
    build_snstk,
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
        help="Maximum sticker dimension in pixels",
    )
    device_choices = list(DEVICES.keys())
    parser.add_argument(
        "-d", "--device",
        default="N6",
        choices=device_choices,
        help=(
            "Target device – "
            + ", ".join(f"{k}={v['name']}" for k, v in DEVICES.items())
        ),
    )

    args = parser.parse_args(argv)

    images = _collect_images(args.inputs)
    if not images:
        print("Error: no supported image files found.", file=sys.stderr)
        return 1

    output = Path(args.output)
    if output.suffix.lower() != ".snstk":
        output = output.with_suffix(".snstk")

    print(f"Creating sticker pack: {output}")
    print(f"Sticker size: {args.size}×{args.size} max")
    print(f"Target device: {args.device} ({DEVICES[args.device]['name']})")
    print(f"Images: {len(images)}")

    data = build_snstk(images, size=args.size, device=args.device)
    output.write_bytes(data)

    print(f"\nDone – {output} ({len(data):,} bytes, {len(images)} sticker(s))")
    print("Copy to your Supernote's EXPORT folder and import from Settings › Stickers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

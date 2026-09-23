"""Flask web application for the Supernote sticker converter."""

from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from supernote_stickers.converter import (
    DEFAULT_MARGIN,
    DEFAULT_STICKER_SIZE,
    DEVICES,
    SUPPORTED_EXTENSIONS,
    build_snstk,
)

app = Flask(__name__, template_folder="../templates")

# Largest sticker edge accepted from the web endpoint.  Matches the `max`
# attribute on the size input; enforced here because that attribute is
# client-side only.
MAX_STICKER_SIZE = 512

# Maximum upload size: 16 MB total
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    """Serve the upload UI."""
    return render_template(
        "index.html",
        devices=DEVICES,
        default_size=DEFAULT_STICKER_SIZE,
        supported_extensions=sorted(SUPPORTED_EXTENSIONS),
    )


@app.get("/health")
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok"})


def _form_int(name: str, default: int) -> int:
    """Read an integer form field, treating a missing/empty value as *default*.

    Raises:
        ValueError: If the field is present but not a valid integer.  The
            caller turns this into a 400 so the endpoint's JSON error
            contract holds for every bad input.
    """
    raw = request.form.get(name, "")
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name!r} must be an integer, got {raw!r}") from None


@app.post("/convert")
def convert():
    """Accept uploaded images and return an SNSTK archive.

    Form fields:
        files[]  – one or more image files
        size       – longest sticker edge in px (optional, default 180)
        device     – device code (optional, default "N5")
        trim       – crop transparent borders (optional, default "true")
        margin     – transparent padding per side in px (optional, default 0)
        pad_square – centre on a square canvas (optional, default "false")
        upscale    – scale small sources up to *size* (optional, default "true")
    """
    uploaded = request.files.getlist("files[]")
    if not uploaded:
        return jsonify({"error": "No files uploaded."}), 400

    # Parse numeric fields defensively: these are untrusted input on a public
    # endpoint, and a bad value must produce a 400 with the same JSON shape as
    # every other input error — not an uncaught ValueError rendered as HTML.
    try:
        size = _form_int("size", DEFAULT_STICKER_SIZE)
        margin = _form_int("margin", DEFAULT_MARGIN)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Bound the size server-side.  The HTML `max` attribute is advisory only,
    # and conversion cost grows with the square of the canvas, so an unbounded
    # size lets one small upload burn arbitrary CPU and memory.
    if not 1 <= size <= MAX_STICKER_SIZE:
        return jsonify(
            {"error": f"'size' must be between 1 and {MAX_STICKER_SIZE}, got {size}"}
        ), 400
    if margin < 0:
        return jsonify({"error": f"'margin' must be >= 0, got {margin}"}), 400

    device = request.form.get("device", "N5")
    trim = request.form.get("trim", "true").lower() not in ("false", "0", "no")
    pad_square = request.form.get("pad_square", "false").lower() in ("true", "1", "yes")
    upscale = request.form.get("upscale", "true").lower() not in ("false", "0", "no")

    if device not in DEVICES:
        return jsonify({"error": f"Unknown device code: {device!r}"}), 400

    images: list[tuple[str, BytesIO]] = []
    for f in uploaded:
        filename = Path(f.filename or "sticker")
        if filename.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return jsonify(
                {"error": f"Unsupported file type: {filename.suffix!r}"}
            ), 400
        buf = BytesIO(f.read())
        images.append((filename.stem, buf))

    try:
        snstk_bytes = build_snstk(
            images,
            size=size,
            device=device,
            trim=trim,
            margin=margin,
            pad_square=pad_square,
            upscale=upscale,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return send_file(
        BytesIO(snstk_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name="stickers.snstk",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """Start the development server (``snstk-web`` command)."""
    # Flask always listens on 5000 internally for reliability
    internal_port = 5000
    
    # Get external port for logging purposes (if different from internal)
    external_port = os.environ.get("PORT", internal_port)
    if int(external_port) != internal_port:
        print(f"\n⚠️  WARNING: This is a development server. Do not use it in production deployment.\n"
              f"   Container listening on: {internal_port}\n"
              f"   Access from host on: {external_port}\n", file=sys.stderr)
    
    app.run(host="0.0.0.0", port=internal_port, debug=False)


if __name__ == "__main__":
    run()

"""Flask web application for the Supernote sticker converter."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from supernote_stickers.converter import (
    DEFAULT_STICKER_SIZE,
    DEVICES,
    SUPPORTED_EXTENSIONS,
    build_snstk,
)

app = Flask(__name__, template_folder="../templates")

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


@app.post("/convert")
def convert():
    """Accept uploaded images and return an SNSTK archive.

    Form fields:
        files[]  – one or more image files
        size     – max sticker dimension (optional, default 180)
        device   – device code (optional, default "N6")
    """
    uploaded = request.files.getlist("files[]")
    if not uploaded:
        return jsonify({"error": "No files uploaded."}), 400

    size = int(request.form.get("size", DEFAULT_STICKER_SIZE))
    device = request.form.get("device", "N6")

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
        snstk_bytes = build_snstk(images, size=size, device=device)
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    run()

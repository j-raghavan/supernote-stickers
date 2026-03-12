"""Tests for the Flask web application."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from supernote_stickers.web.app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    with flask_app.test_client() as c:
        yield c


def _png_bytes(width: int = 20, height: int = 20, color=(0, 0, 0, 255)) -> bytes:
    img = Image.new("RGBA", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Index route
# ---------------------------------------------------------------------------

def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Supernote" in resp.data
    assert b"snstk" in resp.data.lower()


# ---------------------------------------------------------------------------
# Convert route – success paths
# ---------------------------------------------------------------------------

def test_convert_single_png(client):
    data = {"files[]": (BytesIO(_png_bytes()), "test.png")}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"


def test_convert_multiple_images(client):
    data = {
        "files[]": [
            (BytesIO(_png_bytes(color=(255, 0, 0, 255))), "a.png"),
            (BytesIO(_png_bytes(color=(0, 255, 0, 255))), "b.png"),
        ]
    }
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"


def test_convert_with_custom_size(client):
    data = {
        "files[]": (BytesIO(_png_bytes(100, 100)), "big.png"),
        "size":    "32",
    }
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200


def test_convert_with_device_a5x(client):
    data = {
        "files[]": (BytesIO(_png_bytes()), "s.png"),
        "device":  "A5X",
    }
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Convert route – error paths
# ---------------------------------------------------------------------------

def test_convert_no_files_returns_400(client):
    resp = client.post("/convert", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_convert_unsupported_file_type(client):
    data = {"files[]": (BytesIO(b"not an image"), "file.exe")}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_convert_unknown_device_returns_400(client):
    data = {
        "files[]": (BytesIO(_png_bytes()), "s.png"),
        "device":  "Z99",
    }
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400

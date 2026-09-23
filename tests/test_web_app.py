"""Tests for the Flask web application."""

from __future__ import annotations

import re
import zipfile
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
# Convert route – new sizing options
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,value",
    [
        ("margin", "10"),
        ("margin", "0"),
        ("pad_square", "true"),
        ("pad_square", "false"),
        ("upscale", "false"),
        ("upscale", "true"),
    ],
)
def test_convert_accepts_sizing_options(client, field, value):
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), field: value}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"


def test_convert_clamps_oversized_margin_instead_of_growing_sticker(client):
    """A margin larger than half the size must not inflate the sticker.

    `size=32, margin=64` previously produced a 129x129 sticker; the endpoint
    must now clamp and still succeed.
    """
    data = {
        "files[]": (BytesIO(_png_bytes(100, 100)), "s.png"),
        "size": "32",
        "margin": "64",
    }
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    with zipfile.ZipFile(BytesIO(resp.data)) as zf:
        sticker = zf.read(zf.namelist()[0])
    rect = re.search(rb"0,0,(\d+),(\d+)", sticker)
    w, h = int(rect.group(1)), int(rect.group(2))
    assert max(w, h) <= 32, f"clamped margin still produced {w}x{h}"


def test_convert_rejects_oversized_size(client):
    """`size` is bounded server-side: the HTML `max` attribute is advisory."""
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), "size": "100000"}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"


def test_convert_huge_margin_does_not_blow_up(client):
    """Regression: margin=30000 used to allocate gigabytes before the clamp."""
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), "margin": "30000"}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert len(resp.data) < 100_000


# ---------------------------------------------------------------------------
# Convert route – error paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["size", "margin"])
@pytest.mark.parametrize("bad", ["abc", "1e3", "1.5", "0x10", "12abc"])
def test_convert_non_integer_returns_400_json(client, field, bad):
    """Bad numbers must return the endpoint's JSON 400, not an HTML 500."""
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), field: bad}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"
    assert "error" in resp.get_json()


@pytest.mark.parametrize("field", ["size", "margin"])
@pytest.mark.parametrize("blank", ["", "  "])
def test_convert_empty_numeric_field_falls_back_to_default(client, field, blank):
    """The UI can post an empty number input; that means "use the default"."""
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), field: blank}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200


@pytest.mark.parametrize("size", ["0", "-5"])
def test_convert_non_positive_size_returns_400(client, size):
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), "size": size}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"


def test_convert_negative_margin_returns_400(client):
    data = {"files[]": (BytesIO(_png_bytes()), "s.png"), "margin": "-5"}
    resp = client.post("/convert", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"


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

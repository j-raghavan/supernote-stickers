"""Tests for the core converter module."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from supernote_stickers.converter import (
    AA_LEVELS,
    COLORCODE_BACKGROUND,
    COLORCODE_BLACK,
    DEFAULT_STICKER_SIZE,
    DEVICES,
    SUPPORTED_EXTENSIONS,
    alpha_to_colorcode,
    build_snstk,
    build_sticker,
    encode_rle,
    image_to_pixels,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rgba_image(width: int, height: int, color: tuple) -> BytesIO:
    """Return an in-memory PNG RGBA image filled with *color*."""
    img = Image.new("RGBA", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# alpha_to_colorcode
# ---------------------------------------------------------------------------

class TestAlphaToColorcode:
    def test_fully_transparent(self):
        assert alpha_to_colorcode(0) == COLORCODE_BACKGROUND

    def test_near_transparent(self):
        assert alpha_to_colorcode(8) == COLORCODE_BACKGROUND

    def test_near_opaque(self):
        assert alpha_to_colorcode(247) == COLORCODE_BLACK

    def test_fully_opaque(self):
        assert alpha_to_colorcode(255) == COLORCODE_BLACK

    def test_mid_range_returns_aa_level(self):
        code = alpha_to_colorcode(128)
        assert code in AA_LEVELS

    def test_all_outputs_are_valid(self):
        valid = {COLORCODE_BLACK, COLORCODE_BACKGROUND} | set(AA_LEVELS)
        for a in range(256):
            assert alpha_to_colorcode(a) in valid


# ---------------------------------------------------------------------------
# encode_rle
# ---------------------------------------------------------------------------

class TestEncodeRle:
    def test_empty(self):
        assert encode_rle([]) == b""

    def test_single_pixel(self):
        data = encode_rle([COLORCODE_BLACK])
        assert len(data) == 2
        assert data[0] == COLORCODE_BLACK
        assert data[1] == 0  # run-1 = 0

    def test_run_of_same_color(self):
        pixels = [COLORCODE_BACKGROUND] * 10
        data = encode_rle(pixels)
        # Should be compressed to 2 bytes: [color, run-1]
        assert len(data) == 2
        assert data[0] == COLORCODE_BACKGROUND
        assert data[1] == 9  # 10 - 1

    def test_two_different_colors(self):
        pixels = [COLORCODE_BLACK, COLORCODE_BACKGROUND]
        data = encode_rle(pixels)
        assert len(data) == 4

    def test_output_is_bytes(self):
        assert isinstance(encode_rle([COLORCODE_BLACK]), bytes)


# ---------------------------------------------------------------------------
# image_to_pixels
# ---------------------------------------------------------------------------

class TestImageToPixels:
    def test_opaque_black_image(self):
        buf = _make_rgba_image(10, 10, (0, 0, 0, 255))
        pixels, w, h, _img = image_to_pixels(buf, size=10)
        assert w == 10
        assert h == 10
        assert len(pixels) == 100
        # Black + fully opaque should map to COLORCODE_BLACK
        assert all(p == COLORCODE_BLACK for p in pixels)

    def test_transparent_image(self):
        buf = _make_rgba_image(5, 5, (0, 0, 0, 0))
        pixels, w, h, _img = image_to_pixels(buf, size=10)
        assert all(p == COLORCODE_BACKGROUND for p in pixels)

    def test_resize_respects_max_dimension(self):
        buf = _make_rgba_image(200, 100, (0, 0, 0, 255))
        pixels, w, h, _img = image_to_pixels(buf, size=50)
        assert max(w, h) <= 50

    def test_accepts_file_path(self, tmp_path: Path):
        img = Image.new("RGBA", (20, 20), (0, 0, 0, 255))
        p = tmp_path / "test.png"
        img.save(p)
        pixels, w, h, _img = image_to_pixels(p)
        assert len(pixels) == w * h

    def test_accepts_jpeg(self):
        img = Image.new("RGB", (20, 20), (128, 128, 128))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        pixels, w, h, _img = image_to_pixels(buf)
        assert len(pixels) == w * h


# ---------------------------------------------------------------------------
# build_sticker
# ---------------------------------------------------------------------------

class TestBuildSticker:
    def _make_sticker(self, device="N5"):
        pixels = [COLORCODE_BLACK] * 100
        return build_sticker(pixels, 10, 10, device)

    def test_starts_with_magic(self):
        data = self._make_sticker()
        assert data[:4] == b"stck"

    def test_contains_version(self):
        data = self._make_sticker()
        assert b"SN_FILE_VER_20230015" in data

    def test_contains_tail_marker(self):
        data = self._make_sticker()
        assert b"tail" in data

    def test_device_code_in_header(self):
        for device in DEVICES:
            data = build_sticker([COLORCODE_BLACK] * 4, 2, 2, device)
            assert device.encode() in data

    def test_returns_bytes(self):
        assert isinstance(self._make_sticker(), bytes)


# ---------------------------------------------------------------------------
# build_snstk
# ---------------------------------------------------------------------------

class TestBuildSnstk:
    def test_empty_raises(self):
        with pytest.raises(ValueError):
            build_snstk([])

    def test_single_image_produces_valid_zip(self):
        buf = _make_rgba_image(20, 20, (0, 0, 0, 255))
        result = build_snstk([("test_sticker", buf)])
        with zipfile.ZipFile(BytesIO(result)) as zf:
            assert "test_sticker.sticker" in zf.namelist()

    def test_multiple_images(self):
        images = [
            ("star",  _make_rgba_image(20, 20, (255, 0, 0, 255))),
            ("heart", _make_rgba_image(20, 20, (0, 255, 0, 255))),
        ]
        result = build_snstk(images)
        with zipfile.ZipFile(BytesIO(result)) as zf:
            names = zf.namelist()
        assert "star.sticker" in names
        assert "heart.sticker" in names

    def test_each_sticker_starts_with_magic(self):
        buf = _make_rgba_image(20, 20, (0, 0, 0, 128))
        result = build_snstk([("magic_test", buf)])
        with zipfile.ZipFile(BytesIO(result)) as zf:
            data = zf.read("magic_test.sticker")
        assert data[:4] == b"stck"

    def test_custom_size_respected(self):
        buf = _make_rgba_image(200, 200, (0, 0, 0, 255))
        result = build_snstk([("s", buf)], size=32)
        with zipfile.ZipFile(BytesIO(result)) as zf:
            sticker_data = zf.read("s.sticker")
        # The rect string "0,0,32,32" (or smaller) should be in the data
        assert b"0,0," in sticker_data

    def test_zip_entry_metadata_matches_supernote_requirements(self):
        buf = _make_rgba_image(20, 20, (0, 0, 0, 255))
        result = build_snstk([("ascii_name", buf)])
        with zipfile.ZipFile(BytesIO(result)) as zf:
            info = zf.getinfo("ascii_name.sticker")
        assert info.flag_bits == 0x800
        assert info.create_version == 51
        assert info.external_attr == 0x81800000


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_size(self):
        assert DEFAULT_STICKER_SIZE == 180

    def test_supported_extensions_includes_png(self):
        assert ".png" in SUPPORTED_EXTENSIONS

    def test_supported_extensions_includes_jpg(self):
        assert ".jpg" in SUPPORTED_EXTENSIONS

    def test_devices_has_n5(self):
        assert "N5" in DEVICES

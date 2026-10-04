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
    DEFAULT_MARGIN,
    DEFAULT_STICKER_SIZE,
    DEVICES,
    PEN_COLOR_BLACK,
    PEN_COLOR_DARK_GRAY,
    PEN_COLOR_LIGHT_GRAY,
    STROKE_PEN_WEIGHT,
    SUPPORTED_EXTENSIONS,
    TONE_PALETTE,
    TONE_PAPER,
    alpha_to_colorcode,
    build_snstk,
    build_sticker,
    build_trails,
    clamp_margin,
    encode_rle,
    fit_dimensions,
    image_to_pixels,
    quantize_tones,
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
        pixels, w, h, _img, _bw = image_to_pixels(buf, size=10)
        assert w == 10
        assert h == 10
        assert len(pixels) == 100
        # Black + fully opaque should map to COLORCODE_BLACK
        assert all(p == COLORCODE_BLACK for p in pixels)

    def test_transparent_image(self):
        buf = _make_rgba_image(5, 5, (0, 0, 0, 0))
        pixels, w, h, _img, _bw = image_to_pixels(buf, size=10)
        assert all(p == COLORCODE_BACKGROUND for p in pixels)

    def test_resize_respects_max_dimension(self):
        # Exact, not `<= 50`: the old capping behaviour produced 40x20, which
        # also satisfied `<=` and so could regress undetected.
        buf = _make_rgba_image(200, 100, (0, 0, 0, 255))
        pixels, w, h, _img, _bw = image_to_pixels(buf, size=50)
        assert (w, h) == (50, 25)

    def test_accepts_file_path(self, tmp_path: Path):
        img = Image.new("RGBA", (20, 20), (0, 0, 0, 255))
        p = tmp_path / "test.png"
        img.save(p)
        pixels, w, h, _img, _bw = image_to_pixels(p)
        assert len(pixels) == w * h

    def test_accepts_jpeg(self):
        img = Image.new("RGB", (20, 20), (128, 128, 128))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        pixels, w, h, _img, _bw = image_to_pixels(buf)
        assert len(pixels) == w * h


# ---------------------------------------------------------------------------
# Sticker sizing contract
# ---------------------------------------------------------------------------

class TestSizingContract:
    """The longest edge must equal the requested size, aspect preserved.

    Regression cover for the reported bug where a requested size was not
    honoured: small sources were never scaled up, and every sticker was
    letterboxed onto a square canvas so non-square artwork arrived on the
    device surrounded by transparent padding.
    """

    def test_longest_edge_matches_requested_size(self):
        buf = _make_rgba_image(600, 200, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180)
        assert max(w, h) == 180

    def test_aspect_ratio_preserved_not_letterboxed(self):
        # A 3:1 card must stay 3:1, not become a padded square.
        buf = _make_rgba_image(600, 200, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180)
        assert (w, h) == (180, 60)

    def test_tall_image_keeps_its_aspect(self):
        buf = _make_rgba_image(200, 600, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180)
        assert (w, h) == (60, 180)

    def test_small_source_is_scaled_up(self):
        # Previously the source's own dimensions capped the result, making
        # the size control a no-op for anything smaller than `size`.
        buf = _make_rgba_image(64, 64, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180)
        assert (w, h) == (180, 180)

    def test_upscale_can_be_disabled(self):
        buf = _make_rgba_image(64, 64, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180, upscale=False)
        assert (w, h) == (64, 64)

    def test_no_hidden_padding_by_default(self):
        buf = _make_rgba_image(300, 300, (0, 0, 0, 255))
        _px, w, h, img, _bw = image_to_pixels(buf, size=180)
        # Opaque content must reach every edge — no transparent border.
        assert img.getbbox() == (0, 0, w, h)

    def test_margin_is_included_in_requested_size(self):
        buf = _make_rgba_image(300, 300, (0, 0, 0, 255))
        _px, w, h, img, _bw = image_to_pixels(buf, size=180, margin=10)
        assert (w, h) == (180, 180)
        # 10 px of transparency on each side, artwork 160x160 in the middle.
        assert img.getbbox() == (10, 10, 170, 170)

    def test_pad_square_restores_legacy_canvas(self):
        buf = _make_rgba_image(600, 200, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180, pad_square=True)
        assert (w, h) == (180, 180)

    def test_size_is_honoured_for_every_aspect(self):
        for src_w, src_h in [(600, 200), (200, 600), (50, 50), (1000, 1000), (7, 300)]:
            buf = _make_rgba_image(src_w, src_h, (0, 0, 0, 255))
            _px, w, h, _img, _bw = image_to_pixels(buf, size=200)
            assert max(w, h) == 200, f"{src_w}x{src_h} produced {w}x{h}"


class TestFitDimensions:
    def test_longest_edge_fills_the_box(self):
        assert fit_dimensions(600, 200, 180) == (180, 60)

    def test_scales_up(self):
        assert fit_dimensions(45, 15, 180) == (180, 60)

    def test_margin_shrinks_the_content_box(self):
        assert fit_dimensions(100, 100, 180, margin=10) == (160, 160)

    def test_upscale_false_caps_at_native_scale(self):
        assert fit_dimensions(45, 15, 180, upscale=False) == (45, 15)

    def test_never_returns_zero(self):
        assert fit_dimensions(2000, 3, 32) == (32, 1)

    def test_rounds_half_up_like_javascript(self):
        # Python's banker's rounding would give (4, 2) here; docs/app.js
        # uses Math.round, so both implementations must agree on (4, 3).
        assert fit_dimensions(8, 5, 4) == (4, 3)



# ---------------------------------------------------------------------------
# build_sticker
# ---------------------------------------------------------------------------

class TestMarginClamping:
    """A margin must never push the sticker past the requested size.

    Regression cover: `size=32, margin=64` used to floor the content box at
    1 px while the canvas kept growing as `content + 2*margin`, yielding a
    129x129 sticker containing a single-pixel dot.
    """

    @pytest.mark.parametrize(
        "size,margin",
        [(180, 89), (180, 90), (180, 200), (180, 500), (32, 64), (1, 0), (2, 5), (512, 9999)],
    )
    def test_sticker_never_exceeds_requested_size(self, size, margin):
        buf = _make_rgba_image(600, 200, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=size, margin=margin)
        assert max(w, h) <= size, f"size={size} margin={margin} gave {w}x{h}"

    def test_clamp_margin_leaves_at_least_one_content_pixel(self):
        for size in range(1, 100):
            margin = clamp_margin(size, 10_000)
            assert size - 2 * margin >= 1

    def test_clamp_margin_passes_through_valid_values(self):
        assert clamp_margin(180, 0) == 0
        assert clamp_margin(180, 10) == 10
        assert clamp_margin(180, 89) == 89

    def test_clamp_margin_caps_at_half(self):
        assert clamp_margin(180, 90) == 89
        assert clamp_margin(32, 64) == 15
        assert clamp_margin(1, 5) == 0

    def test_negative_margin_is_treated_as_zero(self):
        buf = _make_rgba_image(300, 300, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=180, margin=-5)
        assert (w, h) == (180, 180)

    def test_pad_square_with_margin_stays_square_and_within_size(self):
        buf = _make_rgba_image(600, 200, (0, 0, 0, 255))
        for margin in (0, 10, 50, 89, 200):
            _px, w, h, img, _bw = image_to_pixels(
                buf, size=180, margin=margin, pad_square=True
            )
            assert (w, h) == (180, 180), f"margin={margin}"
            buf.seek(0)


class TestSizeValidation:
    """`size` below 1 must be rejected, not silently degenerate.

    Previously `size=0` emitted a 0x0 sticker and `size=-5` leaked a raw
    Pillow `ValueError` out of `Image.new`.
    """

    @pytest.mark.parametrize("size", [0, -1, -5])
    @pytest.mark.parametrize("pad_square", [True, False])
    def test_rejects_non_positive_size(self, size, pad_square):
        buf = _make_rgba_image(10, 10, (0, 0, 0, 255))
        with pytest.raises(ValueError, match="size must be >= 1"):
            image_to_pixels(buf, size=size, pad_square=pad_square)

    def test_fit_dimensions_rejects_non_positive_size(self):
        with pytest.raises(ValueError, match="size must be >= 1"):
            fit_dimensions(100, 100, 0)

    def test_size_one_is_allowed(self):
        buf = _make_rgba_image(10, 10, (0, 0, 0, 255))
        _px, w, h, _img, _bw = image_to_pixels(buf, size=1)
        assert (w, h) == (1, 1)


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
# Four-tone strokes
# ---------------------------------------------------------------------------

def _parse_strokes(trails: bytes) -> list[tuple[int, int, int, int, int]]:
    """Return ``(pen_color, pen_weight, x_start, x_end, y)`` per stroke."""
    import struct

    count = struct.unpack_from("<I", trails, 0)[0]
    pos, strokes = 4, []
    for _ in range(count):
        length = struct.unpack_from("<I", trails, pos)[0]
        data = trails[pos + 4:pos + 4 + length]
        weight = struct.unpack_from("<H", data, 8)[0]
        min_x, min_y, _ax, _ay, max_x, _my = struct.unpack_from("<6i", data, 100)
        strokes.append((data[4], weight, min_x, max_x, min_y))
        pos += 4 + length
    return strokes


def _gray_ramp(width: int = 64, height: int = 16) -> Image.Image:
    """Opaque horizontal ramp from black to white (not B&W line art)."""
    row = [round(255 * x / (width - 1)) for x in range(width)]
    img = Image.new("RGBA", (width, height))
    img.putdata([(v, v, v, 255) for _y in range(height) for v in row])
    return img


class TestQuantizeTones:
    @pytest.mark.parametrize("index", range(len(TONE_PALETTE)))
    def test_flat_palette_tone_maps_to_itself(self, index):
        tone = TONE_PALETTE[index][0]
        img = Image.new("RGBA", (8, 8), (tone, tone, tone, 255))
        assert (quantize_tones(img) == index).all()

    def test_mid_grey_mixes_neighbouring_tones(self):
        # 180 sits between dark grey (157) and light grey (201): error
        # diffusion must mix exactly those two, never black or paper.
        img = Image.new("RGBA", (16, 16), (180, 180, 180, 255))
        assert set(quantize_tones(img).ravel()) == {1, 2}

    def test_average_tone_tracks_source(self):
        tones = [t[0] for t in TONE_PALETTE]
        for value in (40, 120, 180, 230):
            img = Image.new("RGBA", (32, 32), (value, value, value, 255))
            rendered = sum(tones[i] for i in quantize_tones(img).ravel()) / (32 * 32)
            assert abs(rendered - value) < 6

    def test_transparent_pixels_never_get_ink(self):
        img = Image.new("RGBA", (16, 16), (128, 128, 128, 255))
        for x in range(16):
            for y in range(8):
                img.putpixel((x, y), (0, 0, 0, 0))
        tones = quantize_tones(img)
        assert (tones[:8] == TONE_PAPER).all()
        assert (tones[8:] != TONE_PAPER).any()


class TestFourToneStrokes:
    def test_strokes_use_native_pens_at_minimum_weight(self):
        img = _gray_ramp()
        strokes = _parse_strokes(build_trails([], 64, 16, pil_image=img))
        assert {s[0] for s in strokes} == {
            PEN_COLOR_BLACK, PEN_COLOR_DARK_GRAY, PEN_COLOR_LIGHT_GRAY,
        }
        assert {s[1] for s in strokes} == {STROKE_PEN_WEIGHT}
        assert STROKE_PEN_WEIGHT >= 200

    def test_strokes_are_emitted_row_by_row(self):
        # Grouping strokes by colour (e.g. all black last) lets one tone
        # spread over its neighbours on the device and darkens the sticker;
        # row-major order spreads every tone evenly.
        img = _gray_ramp()
        strokes = _parse_strokes(build_trails([], 64, 16, pil_image=img))
        positions = [(y, x0) for _pen, _w, x0, _x1, y in strokes]
        assert positions == sorted(positions)
        first_row = [s[0] for s in strokes if s[4] == 0]
        assert len(set(first_row)) == 3   # tones interleave within a row

    def test_bitmap_and_strokes_show_the_same_tones(self):
        buf = BytesIO()
        _gray_ramp(48, 12).save(buf, format="PNG")
        buf.seek(0)
        pixels, w, h, img, is_bw = image_to_pixels(buf, size=48)
        assert not is_bw
        code_to_index = {code: i for i, (_t, _p, code) in enumerate(TONE_PALETTE)}
        assert set(pixels) <= set(code_to_index)

        pen_to_index = {pen: i for i, (_t, pen, _c) in enumerate(TONE_PALETTE) if pen is not None}
        drawn = [TONE_PAPER] * (w * h)
        for pen, _weight, x0, x1, y in _parse_strokes(build_trails(pixels, w, h, pil_image=img)):
            for x in range(x0, x1 + 1):
                drawn[y * w + x] = pen_to_index[pen]
        assert drawn == [code_to_index[c] for c in pixels]

    def test_bitmap_codes_round_trip_without_source_image(self):
        buf = BytesIO()
        _gray_ramp(48, 12).save(buf, format="PNG")
        buf.seek(0)
        pixels, w, h, img, _bw = image_to_pixels(buf, size=48)
        assert build_trails(pixels, w, h) == build_trails(pixels, w, h, pil_image=img)

    @pytest.mark.parametrize("device", sorted(DEVICES))
    def test_pen_points_land_inside_their_bbox_on_non_square_sticker(self, device):
        # The firmware draws vector points but places the lasso box from the
        # bbox; a width-dependent offset once drew 109 px-wide art ~45 px to
        # the right of its selection box.
        import struct

        img = _gray_ramp(109, 30)
        trails = build_trails([], 109, 30, device=device, pil_image=img)
        emr_w = DEVICES[device]["emr"][0]
        scale = emr_w / DEVICES[device]["screen"][0]
        pos = 4
        for _ in range(struct.unpack_from("<I", trails, 0)[0]):
            length = struct.unpack_from("<I", trails, pos)[0]
            data = trails[pos + 4:pos + 4 + length]
            min_x, min_y, _ax, _ay, max_x, max_y = struct.unpack_from("<6i", data, 100)
            n = struct.unpack_from("<I", data, 212)[0]
            pts = struct.unpack_from(f"<{2 * n}i", data, 216)
            xs = [(emr_w - dx) / scale for dx in pts[1::2]]
            ys = [dy / scale for dy in pts[0::2]]
            assert min_x - 0.5 <= min(xs) and max(xs) <= max_x + 0.5
            assert min_y - 0.5 <= min(ys) and max(ys) <= max_y + 0.5
            pos += 4 + length

    def test_line_art_keeps_black_strokes_only(self):
        img = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
        for x in range(20):
            img.putpixel((x, 10), (0, 0, 0, 255))
            img.putpixel((x, 11), (0, 0, 0, 255))
            img.putpixel((x, 12), (0, 0, 0, 255))
        strokes = _parse_strokes(build_trails([], 20, 20, pil_image=img, is_bw=True))
        assert {s[0] for s in strokes} == {PEN_COLOR_BLACK}
        assert {s[1] for s in strokes} == {STROKE_PEN_WEIGHT}


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
        # Host byte of version_made_by MUST be Unix (3): external_attr below
        # holds Unix mode bits, and the firmware only recognises the sticker
        # when the host declares Unix.  A FAT host (0) makes every sticker
        # show blank in the picker.
        assert info.create_system == 3
        assert info.external_attr == 0x81800000

    def test_zip_raw_headers_declare_unix_host_and_deflate_version(self):
        """The firmware reads the raw ZIP headers, not zipfile's parsed view.

        Regression guard: version_made_by must be 0x0333 (Unix host 0x03 +
        version 51) and version_needed must be 20 (DEFLATE) in *every*
        central-directory and local-file header.
        """
        import struct

        buf = _make_rgba_image(20, 20, (0, 0, 0, 255))
        data = build_snstk([("a", buf), ("b", buf)])

        eocd = data.rfind(b"PK\x05\x06")
        cd_off = struct.unpack_from("<II", data, eocd + 12)[1]

        # Central-directory headers
        cur = cd_off
        n = 0
        while cur < len(data) and struct.unpack_from("<I", data, cur)[0] == 0x02014B50:
            version_made_by, version_needed = struct.unpack_from("<HH", data, cur + 4)
            assert version_made_by == 0x0333, hex(version_made_by)
            assert version_needed == 20
            fnl, efl, cml = struct.unpack_from("<HHH", data, cur + 28)
            cur += 46 + fnl + efl + cml
            n += 1
        assert n == 2

        # Local-file headers
        i = 0
        locals_seen = 0
        while (i := data.find(b"PK\x03\x04", i)) >= 0:
            version_needed = struct.unpack_from("<H", data, i + 4)[0]
            assert version_needed == 20
            locals_seen += 1
            i += 4
        assert locals_seen == 2


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_size(self):
        assert DEFAULT_STICKER_SIZE == 180

    def test_default_margin_is_zero(self):
        # The default must stay 0 so `size` means exactly what it says.
        assert DEFAULT_MARGIN == 0

    def test_supported_extensions_includes_png(self):
        assert ".png" in SUPPORTED_EXTENSIONS

    def test_supported_extensions_includes_jpg(self):
        assert ".jpg" in SUPPORTED_EXTENSIONS

    def test_devices_has_n5(self):
        assert "N5" in DEVICES

/**
 * Supernote Sticker Converter – browser-side implementation.
 *
 * Mirrors the Python logic in src/supernote_stickers/converter.py so the
 * GitHub Pages site works with no backend whatsoever.
 *
 * Uses OpenCV.js (WASM) for contour detection to generate proper stroke
 * data that the Supernote firmware can render.
 */

'use strict';

// ---------------------------------------------------------------------------
// Constants (mirrors converter.py)
// ---------------------------------------------------------------------------

const COLORCODE_BLACK      = 0x61;
const COLORCODE_BACKGROUND = 0x62;

const AA_LEVELS = [
  0x0F, 0x1F, 0x2F, 0x3F, 0x4F, 0x5F, 0x6F, 0x7F,
  0x8F, 0x9F, 0xAF, 0xBF, 0xCF, 0xDF, 0xEF,
];

const DEFAULT_STICKER_SIZE = 180;

// ---------------------------------------------------------------------------
// Floyd-Steinberg dithering — works directly from RGBA imageData
// ---------------------------------------------------------------------------

/**
 * Convert raw RGBA imageData to grayscale Float64Array (0=black, 255=white).
 * Transparent pixels (a=0) become 255 (white/background).
 * This skips the lossy Supernote colour-code intermediate step so all
 * 256 grayscale levels are preserved for high-quality dithering.
 */
function rgbaToGrayscale(imageData, width, height) {
  const gray = new Float64Array(width * height);
  for (let i = 0; i < width * height; i++) {
    const r = imageData[i * 4];
    const g = imageData[i * 4 + 1];
    const b = imageData[i * 4 + 2];
    const a = imageData[i * 4 + 3];
    if (a === 0) {
      gray[i] = 255;
    } else {
      // Luminosity grayscale, then factor in alpha
      const lum = 0.299 * r + 0.587 * g + 0.114 * b;
      // Blend with white background based on alpha
      gray[i] = lum * (a / 255) + 255 * (1 - a / 255);
    }
  }
  return gray;
}

/**
 * Stretch contrast and apply gamma correction for better dithering.
 * Uses γ=0.4 to darken midtones aggressively so light skin tones and
 * subtle features produce enough black dots.
 */
function enhanceContrast(gray) {
  const out = Float64Array.from(gray);
  // Find min/max of non-white pixels (actual content)
  let lo = 255, hi = 0;
  for (let i = 0; i < out.length; i++) {
    if (out[i] < 250) {
      if (out[i] < lo) lo = out[i];
      if (out[i] > hi) hi = out[i];
    }
  }
  if (hi - lo < 1) return out;

  // Contrast stretch + gamma correction (γ=0.4 darkens midtones)
  for (let i = 0; i < out.length; i++) {
    if (out[i] < 250) {
      let v = (out[i] - lo) / (hi - lo) * 255;
      v = Math.max(0, Math.min(255, v));
      out[i] = 255 * Math.pow(v / 255, 0.4);
    }
  }
  return out;
}

/**
 * Apply Floyd-Steinberg error-diffusion dithering.
 * Returns Uint8Array mask (0 or 255) where 255 = black pixel.
 */
function floydSteinbergDither(gray) {
  const width = Math.round(Math.sqrt(gray.length));  // not used, see params version
  const img = enhanceContrast(gray);
  return _ditherCore(img);
}

function floydSteinbergDitherFromRGBA(imageData, width, height) {
  const gray = rgbaToGrayscale(imageData, width, height);
  const img = enhanceContrast(gray);
  return _ditherCore(img, width, height);
}

function _ditherCore(img, width, height) {
  if (!width) { width = Math.round(Math.sqrt(img.length)); height = width; }

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = y * width + x;
      const oldVal = img[idx];
      const newVal = oldVal < 128 ? 0 : 255;
      img[idx] = newVal;
      const err = oldVal - newVal;

      if (x + 1 < width)
        img[idx + 1] += err * 7 / 16;
      if (y + 1 < height) {
        if (x - 1 >= 0)
          img[(y + 1) * width + (x - 1)] += err * 3 / 16;
        img[(y + 1) * width + x] += err * 5 / 16;
        if (x + 1 < width)
          img[(y + 1) * width + (x + 1)] += err * 1 / 16;
      }
    }
  }

  // Convert to mask: black pixels (< 128) become 255 in mask
  const mask = new Uint8Array(width * height);
  for (let i = 0; i < img.length; i++) {
    mask[i] = img[i] < 128 ? 255 : 0;
  }
  return mask;
}

// ---------------------------------------------------------------------------
// ColourMapper – converts RGBA pixel to a Supernote colour code
// ---------------------------------------------------------------------------

const ColourMapper = {
  alphaToColorcode(alpha) {
    if (alpha < 9)   return COLORCODE_BACKGROUND;
    if (alpha > 246) return COLORCODE_BLACK;
    const index = 14 - Math.round((alpha / 255) * 14);
    return AA_LEVELS[index];
  },

  rgbaToColorcode(r, g, b, a) {
    if (a === 0) return COLORCODE_BACKGROUND;
    const gray     = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
    const inkAlpha = Math.round((255 - gray) * (a / 255));
    return this.alphaToColorcode(inkAlpha);
  },
};

// ---------------------------------------------------------------------------
// ImageProcessor – loads an image file and extracts pixel data
// ---------------------------------------------------------------------------

const ImageProcessor = {
  /**
   * Find the bounding box of non-transparent pixels in an RGBA ImageData.
   * Returns {sx, sy, sw, sh} or null if the image is fully transparent.
   */
  _trimBounds(data, width, height) {
    let minX = width, minY = height, maxX = -1, maxY = -1;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const a = data[(y * width + x) * 4 + 3];
        if (a > 0) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    if (maxX < 0) return null; // fully transparent
    return { sx: minX, sy: minY, sw: maxX - minX + 1, sh: maxY - minY + 1 };
  },

  async fileToPixels(file, size = DEFAULT_STICKER_SIZE, trim = true) {
    // Avoid premultiplied-alpha data loss so transparent pixels stay intact
    const bitmap = await createImageBitmap(file, { premultiplyAlpha: 'none' });
    let { width: origW, height: origH } = bitmap;
    console.log(`[fileToPixels] original image: ${origW}x${origH}, size=${size}, trim=${trim}`);

    // Draw full image to a temp canvas so we can inspect pixels for trimming
    const tmpCanvas = new OffscreenCanvas(origW, origH);
    const tmpCtx    = tmpCanvas.getContext('2d', { willReadFrequently: true });
    tmpCtx.drawImage(bitmap, 0, 0);
    const fullData = tmpCtx.getImageData(0, 0, origW, origH);

    // Determine source region (trimmed or full)
    let sx = 0, sy = 0, sw = origW, sh = origH;
    if (trim) {
      const bounds = this._trimBounds(fullData.data, origW, origH);
      console.log(`[fileToPixels] trim bounds:`, bounds);
      if (bounds) {
        sx = bounds.sx;  sy = bounds.sy;
        sw = bounds.sw;  sh = bounds.sh;
      }
      // If fully transparent, keep original dimensions
    }
    console.log(`[fileToPixels] source region: sx=${sx}, sy=${sy}, sw=${sw}, sh=${sh}`);

    // Scale the trimmed image back to the original canvas size so the
    // sticker matches the user's intended dimensions.  For images that
    // were originally larger than size, cap at (size - 10) for margin.
    const origMaxDim = Math.max(origW, origH);
    const target = origMaxDim > size ? size - 10 : origMaxDim;
    const scale = Math.min(target / sw, target / sh);
    const w     = Math.max(1, Math.round(sw * scale));
    const h     = Math.max(1, Math.round(sh * scale));
    console.log(`[fileToPixels] origMaxDim=${origMaxDim}, target=${target}, scale=${scale.toFixed(4)}, scaled=${w}x${h}`);

    // Draw the (possibly trimmed) region scaled to fit within size×size
    const canvas  = new OffscreenCanvas(w, h);
    const ctx     = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, w, h);

    // Centre on a size×size canvas so bitmap and trails are consistently
    // positioned, matching the reference coordinate system the fixed
    // digitiser offsets were calibrated against.
    let finalW = w, finalH = h;
    let finalCanvas = canvas;
    if (w !== size || h !== size) {
      finalCanvas = new OffscreenCanvas(size, size);
      const fCtx = finalCanvas.getContext('2d', { willReadFrequently: true });
      const ox = Math.floor((size - w) / 2);
      const oy = Math.floor((size - h) / 2);
      console.log(`[fileToPixels] centering: ox=${ox}, oy=${oy}, finalCanvas=${size}x${size}`);
      fCtx.drawImage(canvas, ox, oy);
      finalW = size;
      finalH = size;
    } else {
      console.log(`[fileToPixels] no centering needed, already ${w}x${h}`);
    }

    console.log(`[fileToPixels] final output: ${finalW}x${finalH}`);
    const { data } = finalCanvas.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, finalW, finalH);
    const pixels   = new Uint8Array(finalW * finalH);

    for (let i = 0; i < finalW * finalH; i++) {
      const r = data[i * 4];
      const g = data[i * 4 + 1];
      const b = data[i * 4 + 2];
      const a = data[i * 4 + 3];
      pixels[i] = ColourMapper.rgbaToColorcode(r, g, b, a);
    }

    bitmap.close();
    return { pixels, width: finalW, height: finalH, imageData: data };
  },
};

// ---------------------------------------------------------------------------
// RLEEncoder – Supernote RattaRLE compression
// ---------------------------------------------------------------------------

const RLEEncoder = {
  encode(pixels) {
    const result = [];
    let i = 0;

    while (i < pixels.length) {
      const color = pixels[i];
      let run = 1;
      while (i + run < pixels.length && pixels[i + run] === color) run++;
      i += run;

      while (run > 0) {
        if (run >= 0x4000) {
          result.push(color, 0xFF);
          run -= 0x4000;
        } else if (run > 128) {
          let highPart  = ((run - 1) >> 7) - 1;
          if (highPart < 0) highPart = 0;
          let shift      = (highPart + 1) << 7;
          let secondByte = run - 1 - shift;

          while (secondByte > 255 && highPart < 127) {
            highPart++;
            shift      = (highPart + 1) << 7;
            secondByte = run - 1 - shift;
          }
          while (secondByte < 0 && highPart > 0) {
            highPart--;
            shift      = (highPart + 1) << 7;
            secondByte = run - 1 - shift;
          }

          if (secondByte >= 0 && secondByte <= 255) {
            result.push(color, highPart | 0x80, color, secondByte);
            const actual = 1 + secondByte + ((highPart + 1) << 7);
            run -= actual;
          } else {
            result.push(color, 127);
            run -= 128;
          }
        } else {
          result.push(color, run - 1);
          run = 0;
        }
      }
    }

    return new Uint8Array(result);
  },
};

// ---------------------------------------------------------------------------
// Custom IEEE 754 encoding (Supernote contour coordinates)
// ---------------------------------------------------------------------------

/**
 * Encode a float as Supernote's custom IEEE 754 format.
 * Standard LE IEEE 754 single-precision with first two bytes swapped.
 */
function decimalToCustomIEEE754(value) {
  if (value === 0) return [0, 0, 0, 0];
  const buf = new ArrayBuffer(4);
  new DataView(buf).setFloat32(0, value, true);  // LE float
  const std = new Uint8Array(buf);
  return [std[1], std[0], std[2], std[3]];       // swap bytes 0,1
}

// ---------------------------------------------------------------------------
// Binary helpers
// ---------------------------------------------------------------------------

function packU32LE(arr, val) {
  arr.push(val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >>> 24) & 0xFF);
}

function packI32LE(arr, val) {
  packU32LE(arr, val | 0);  // force signed interpretation
}

function packU16LE(arr, val) {
  arr.push(val & 0xFF, (val >> 8) & 0xFF);
}

function hexToBytes(hex) {
  const bytes = [];
  for (let i = 0; i < hex.length; i += 2) {
    bytes.push(parseInt(hex.substr(i, 2), 16));
  }
  return bytes;
}

// ---------------------------------------------------------------------------
// Stroke record binary constants (mirrors converter.py)
// ---------------------------------------------------------------------------

const DEVICES = {
  N5:  { screen: [1920, 2560] },
  A5X: { screen: [1404, 1872] },
  A6X: { screen: [1404, 1872] },
};


// Record marker (8 bytes)
const _MARKER = hexToBytes('20000000ffffffff');

// Page + padding + constants (20 bytes) — page=3 required by firmware
const _PAGE_CONST = hexToBytes(
  '03000000' +   // page = 3 (required by firmware)
  '00000000' +   // padding
  '00000000' +   // padding
  '88130000' +   // constant = 5000
  '00000000'     // padding
);

// Tool name "others" null-padded to 52 bytes
const _TOOL_NAME = (() => {
  const name = [0x6F, 0x74, 0x68, 0x65, 0x72, 0x73];  // "others"
  return name.concat(new Array(46).fill(0));
})();

// Device info (12 bytes each) — first byte 0x1a for stickers
const _DEVICE_INFO_N5    = hexToBytes('1a00000080540000603f0000');
const _DEVICE_INFO_OTHER = hexToBytes('1a000000cb3d0000582e0000');

// Annotation "superNoteNote" null-padded to 52 bytes
const _ANNOTATION = (() => {
  const txt = [0x73,0x75,0x70,0x65,0x72,0x4E,0x6F,0x74,0x65,0x4E,0x6F,0x74,0x65];
  return txt.concat(new Array(39).fill(0));
})();

// Flags (24 bytes)
const _FLAGS = hexToBytes(
  '01000000000000000000000000000000' +
  '0000000000000000'
);

// 54 fixed bytes between stroke_nb and contours_count (last byte = 0x01)
const _POST_STROKE_NB = hexToBytes(
  '00000000000000000000000000000000' +
  '01000000010000000000000000000000' +
  '01000000010000000000000000000000' +
  '000000000001'
);

// r_bytes template (94 bytes) — extracted from working Christmas Dog stroke.
// Screen width at offset 37, height at offset 41.
const _R_BYTES_TEMPLATE = hexToBytes(
  'ffffffffffffffffffffffffffffffffffffffff' +
  '4dac33dcb771d43f' +
  '002f0000000000000080070000000a00' +
  '00000000000004000000' +
  '6e6f6e65' +
  '040000006e6f6e65' +
  '00000000' +
  '0300000002000000' +
  '00000000000000000000000000000000'
);

function buildRBytes(screenW, screenH) {
  const r = _R_BYTES_TEMPLATE.slice();
  // Patch screen dims at offsets 37 and 41
  const dv = new DataView(new Uint8Array(r).buffer);
  dv.setUint32(37, screenW, true);
  dv.setUint32(41, screenH, true);
  return Array.from(new Uint8Array(dv.buffer));
}


// ---------------------------------------------------------------------------
// TrailsBuilder – OpenCV WASM contour-based stroke generation
// ---------------------------------------------------------------------------

const TrailsBuilder = {
  DEVICES,

  /**
   * Densely interpolate points along a closed polygon at ~spacing px intervals.
   * The Supernote firmware expects vector-points to be a dense pen trajectory.
   * @param {Array<[number,number]>} points  Polygon vertices
   * @param {number} spacing  Pixel spacing between samples
   * @returns {Array<[number,number]>}  Dense point list
   */
  _interpolateContour(points, spacing = 2.0) {
    const dense = [];
    const n = points.length;
    if (n < 2) return points.slice();

    for (let i = 0; i < n; i++) {
      const [x0, y0] = points[i];
      const [x1, y1] = points[(i + 1) % n];
      const dx = x1 - x0, dy = y1 - y0;
      const segLen = Math.hypot(dx, dy);
      if (segLen < 1e-6) { dense.push([x0, y0]); continue; }
      const steps = Math.max(1, Math.floor(segLen / spacing));
      for (let s = 0; s < steps; s++) {
        const t = s / steps;
        dense.push([x0 + dx * t, y0 + dy * t]);
      }
    }

    // Retry with finer spacing if too few points
    if (dense.length < 10 && spacing > 0.5) {
      return this._interpolateContour(points, Math.max(0.5, spacing / 2));
    }
    return dense;
  },


  /**
   * Build a single stroke from contour points.
   *
   * @param {Array<[number,number]>} contourPts  (x, y) float pairs
   * @param {number} strokeNb  1-based stroke sequence number
   * @param {string} device    Device code
   * @param {number} screenW   Screen width
   * @param {number} screenH   Screen height
   * @returns {Uint8Array}  Stroke data bytes
   */
  _buildStroke(contourPts, strokeNb, device, screenW, screenH) {
    // Dense vector points for pen trajectory (firmware needs many points)
    const vectorPts = this._interpolateContour(contourPts, 2.0);
    const nVec = vectorPts.length;
    // Simplified contour points for shape outline
    const nContour = contourPts.length;

    // Coordinate scaling: bbox in sticker pixel space, vector in digitizer space
    const VEC_SCALE = 8.0;
    const VEC_OFFSET_X = 15200;
    const VEC_OFFSET_Y = 200;

    // Bounding box in the SAME digitizer coordinate space as vectors
    let minPxX = Infinity, minPxY = Infinity, maxPxX = -Infinity, maxPxY = -Infinity;
    for (const [x, y] of contourPts) {
      if (x < minPxX) minPxX = x;
      if (y < minPxY) minPxY = y;
      if (x > maxPxX) maxPxX = x;
      if (y > maxPxY) maxPxY = y;
    }
    const minX = Math.floor(minPxX * VEC_SCALE + VEC_OFFSET_X);
    const minY = Math.floor(minPxY * VEC_SCALE + VEC_OFFSET_Y);
    const maxX = Math.floor(maxPxX * VEC_SCALE + VEC_OFFSET_X);
    const maxY = Math.floor(maxPxY * VEC_SCALE + VEC_OFFSET_Y);
    const avgX = (minX + maxX) >> 1;
    const avgY = (minY + maxY) >> 1;

    const buf = [];

    // ---- Stroke header (20 bytes) ----
    buf.push(10, 0, 0, 0);       // pen_type=10 + padding
    buf.push(0, 0, 0, 0);        // pen_color=0 + padding
    packU16LE(buf, 220);          // pen_weight=220
    buf.push(...hexToBytes('00000A00000000000000'));   // 10 fixed bytes

    // ---- Record body ----
    buf.push(..._MARKER);
    buf.push(..._PAGE_CONST);
    buf.push(..._TOOL_NAME);

    // Bounding box (6 × i32)
    packI32LE(buf, minX);
    packI32LE(buf, minY);
    packI32LE(buf, avgX);
    packI32LE(buf, avgY);
    packI32LE(buf, maxX);
    packI32LE(buf, maxY);

    // Device info (12 bytes)
    buf.push(...(device === 'N5' ? _DEVICE_INFO_N5 : _DEVICE_INFO_OTHER));
    // Annotation (52 bytes)
    buf.push(..._ANNOTATION);
    // Flags (24 bytes)
    buf.push(..._FLAGS);

    // ---- Vector points (y, x as i32 pairs) — digitizer coordinates ----
    packU32LE(buf, nVec);
    for (const [x, y] of vectorPts) {
      const digiX = Math.round(x * VEC_SCALE + VEC_OFFSET_X);
      const digiY = Math.round(y * VEC_SCALE + VEC_OFFSET_Y);
      packI32LE(buf, digiY);   // y stored first
      packI32LE(buf, digiX);   // x stored second
    }

    // ---- Pressure (u16 per point) ----
    packU32LE(buf, nVec);
    for (let i = 0; i < nVec; i++) packU16LE(buf, 1000);

    // ---- Unique (u32 per point) ----
    packU32LE(buf, nVec);
    for (let i = 0; i < nVec; i++) packU32LE(buf, 1);

    // ---- One (u8 per point) ----
    packU32LE(buf, nVec);
    for (let i = 0; i < nVec; i++) buf.push(1);

    // ---- 16 bytes (12 zeros + 0x61000000) ----
    for (let i = 0; i < 12; i++) buf.push(0);
    buf.push(0x61, 0x00, 0x00, 0x00);

    // ---- Stroke number ----
    packU32LE(buf, strokeNb);

    // ---- 54 fixed bytes ----
    buf.push(..._POST_STROKE_NB);

    // ---- Contours section (simplified polygon vertices) ----
    packU32LE(buf, 1);           // contours_count = 1
    packU32LE(buf, nContour);    // point count for this contour
    for (const [x, y] of contourPts) {
      buf.push(...decimalToCustomIEEE754(x));
      buf.push(...decimalToCustomIEEE754(y));
    }
    packU32LE(buf, 1);           // second contours_count

    // ---- r_bytes ----
    buf.push(...buildRBytes(screenW, screenH));

    return new Uint8Array(buf);
  },

  /**
   * Build trails using Floyd-Steinberg dithering + scanline fills.
   *
   * @param {Uint8Array} pixels   Row-major Supernote colour codes (for bitmap)
   * @param {number}     width    Sticker width
   * @param {number}     height   Sticker height
   * @param {string}     device   Device code
   * @param {Uint8ClampedArray} imageData  Raw RGBA pixel data for dithering
   * @returns {Uint8Array}
   */
  build(pixels, width, height, device = 'N5', imageData = null) {
    const [screenW, screenH] = (this.DEVICES[device] || this.DEVICES.N5).screen;

    // Dither directly from RGBA data (full 256-level grayscale precision)
    let ditheredMask;
    if (imageData) {
      ditheredMask = floydSteinbergDitherFromRGBA(imageData, width, height);
    } else {
      const gray = new Float64Array(width * height);
      const codeToGray = new Map([[COLORCODE_BLACK, 0], [COLORCODE_BACKGROUND, 255]]);
      for (let i = 0; i < AA_LEVELS.length; i++)
        codeToGray.set(AA_LEVELS[i], Math.round((i + 1) / (AA_LEVELS.length + 1) * 255));
      for (let i = 0; i < pixels.length; i++)
        gray[i] = codeToGray.get(pixels[i]) ?? 255;
      ditheredMask = _ditherCore(enhanceContrast(gray), width, height);
    }

    // Scanline fill strokes from dithered image
    const strokeChunks = [];
    let strokeNb = 1004;
    let numStrokes = 0;
    let totalBytes = 0;

    const wrapStroke = (dataArr) => {
      const chunk = new Uint8Array(4 + dataArr.length);
      new DataView(chunk.buffer).setUint32(0, dataArr.length, true);
      chunk.set(dataArr, 4);
      strokeChunks.push(chunk);
      totalBytes += chunk.length;
      strokeNb++;
      numStrokes++;
    };

    for (let y = 0; y < height; y++) {
      let x = 0;
      while (x < width) {
        if (ditheredMask[y * width + x] === 0) { x++; continue; }  // white, skip
        const xStart = x;
        while (x < width && ditheredMask[y * width + x] !== 0) x++;  // black run
        const xEnd = x - 1;
        if (xEnd < xStart) continue;
        // Proper rectangle with 1-pixel height (non-degenerate polygon)
        // Mirror X axis — the firmware renders trails with X inverted
        const runPts = [
          [width - 1 - xStart, y],
          [width - 1 - xEnd,   y],
          [width - 1 - xEnd,   y + 1],
          [width - 1 - xStart, y + 1],
        ];
        wrapStroke(this._buildStroke(runPts, strokeNb, device, screenW, screenH));
      }
    }

    // Fallback if no strokes at all
    if (numStrokes === 0) {
      const fallbackPts = [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]];
      wrapStroke(this._buildStroke(fallbackPts, 1004, device, screenW, screenH));
    }

    // TOTALPATH: assemble strokes_count + all stroke chunks
    const out = new Uint8Array(4 + totalBytes);
    const dv = new DataView(out.buffer);
    dv.setUint32(0, numStrokes, true);
    let off = 4;
    for (const chunk of strokeChunks) {
      out.set(chunk, off);
      off += chunk.length;
    }
    return out;
  },
};

// ---------------------------------------------------------------------------
// StickerBuilder – assembles a single .sticker binary
// ---------------------------------------------------------------------------

const StickerBuilder = {
  _generateFileId() {
    const now  = new Date();
    const pad  = (n, len = 2) => String(n).padStart(len, '0');
    const ts   = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
               + `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const ms   = pad(now.getMilliseconds(), 3);
    const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
    const arr  = crypto.getRandomValues(new Uint8Array(15));
    const rand = Array.from(arr, b => alphabet[b % alphabet.length]).join('');
    return `F${ts}${ms}${rand}`;
  },

  _str(s) { return new TextEncoder().encode(s); },

  _writeU32(arr, val) {
    arr.push(val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >>> 24) & 0xFF);
  },

  async build(pixels, width, height, device = 'N5', imageData = null) {
    const fileId = this._generateFileId();

    // --- Section 1 – header ---
    const magic       = [0x73, 0x74, 0x63, 0x6B]; // 'stck'
    const version     = this._str('SN_FILE_VER_20230015');
    const headerMeta  = this._str(
      `<FILE_TYPE:STICKER>`
      + `<APPLY_EQUIPMENT:${device}>`
      + `<FILE_PARSE_TYPE:0>`
      + `<RATTA_ETMD:0>`
      + `<FILE_ID:${fileId}>`
      + `<ANTIALIASING_CONVERT:2>`,
    );
    const header = [
      ...magic,
      ...version,
      ...((() => { const a = []; this._writeU32(a, headerMeta.length); return a; })()),
      ...headerMeta,
    ];
    const bitmapOffset = header.length;

    // --- Section 2 – bitmap ---
    const rle  = RLEEncoder.encode(pixels);
    const bitmapBlock = new Uint8Array(4 + rle.length);
    new DataView(bitmapBlock.buffer).setUint32(0, rle.length, true);
    bitmapBlock.set(rle instanceof Uint8Array ? rle : new Uint8Array(rle), 4);

    // --- Section 3 – trails (Floyd-Steinberg dithered) ---
    const trailsOffset = bitmapOffset + bitmapBlock.length;
    const trailsData   = TrailsBuilder.build(pixels, width, height, device, imageData);
    const trailsBlock  = new Uint8Array(4 + trailsData.length);
    new DataView(trailsBlock.buffer).setUint32(0, trailsData.length, true);
    trailsBlock.set(trailsData, 4);

    // --- Section 4 – rect ---
    const rectOffset = trailsOffset + trailsBlock.length;
    const rectStr    = this._str(`0,0,${width},${height}`);
    const rectBlock  = new Uint8Array(4 + rectStr.length);
    new DataView(rectBlock.buffer).setUint32(0, rectStr.length, true);
    rectBlock.set(new Uint8Array(rectStr), 4);

    // --- Section 5 – footer ---
    const footerOffset = rectOffset + rectBlock.length;
    const footerMeta   = this._str(
      `<FILE_FEATURE:24>`
      + `<STICKERBITMAP:${bitmapOffset}>`
      + `<STICKERRECT:${rectOffset}>`
      + `<STICKERROTATION:1000>`
      + `<STICKERTRAILS:${trailsOffset}>`,
    );
    const footerBlock = new Uint8Array(4 + footerMeta.length + 4 + 4);
    const fbDv = new DataView(footerBlock.buffer);
    fbDv.setUint32(0, footerMeta.length, true);
    footerBlock.set(new Uint8Array(footerMeta), 4);
    const tailOff = 4 + footerMeta.length;
    footerBlock.set(new Uint8Array([0x74, 0x61, 0x69, 0x6C]), tailOff); // 'tail'
    fbDv.setUint32(tailOff + 4, footerOffset, true);

    // Concatenate all sections
    const headerArr = new Uint8Array(header);
    const total = headerArr.length + bitmapBlock.length + trailsBlock.length + rectBlock.length + footerBlock.length;
    const result = new Uint8Array(total);
    let off = 0;
    result.set(headerArr, off); off += headerArr.length;
    result.set(bitmapBlock, off); off += bitmapBlock.length;
    result.set(trailsBlock, off); off += trailsBlock.length;
    result.set(rectBlock, off); off += rectBlock.length;
    result.set(footerBlock, off);
    return result;
  },
};

// ---------------------------------------------------------------------------
// ZIP metadata patcher
// ---------------------------------------------------------------------------

const ZIP_LOCAL_FILE_HEADER_SIGNATURE = 0x04034b50;
const ZIP_CENTRAL_DIRECTORY_SIGNATURE = 0x02014b50;
const ZIP_EOCD_SIGNATURE = 0x06054b50;

function findEocdOffset(dv) {
  const minEocdSize = 22;
  const maxCommentLength = 0xffff;
  const start = Math.max(0, dv.byteLength - (minEocdSize + maxCommentLength));

  for (let i = dv.byteLength - minEocdSize; i >= start; i--) {
    if (dv.getUint32(i, true) === ZIP_EOCD_SIGNATURE) {
      return i;
    }
  }

  throw new Error('ZIP EOCD record not found');
}

function patchZipMetadata(buffer) {
  const dv = new DataView(buffer);
  const eocdOffset = findEocdOffset(dv);
  const centralDirectorySize = dv.getUint32(eocdOffset + 12, true);
  const centralDirectoryOffset = dv.getUint32(eocdOffset + 16, true);
  const centralDirectoryEnd = centralDirectoryOffset + centralDirectorySize;

  if (centralDirectoryEnd > dv.byteLength) {
    throw new Error('Central directory extends beyond ZIP buffer');
  }

  for (let cursor = centralDirectoryOffset; cursor < centralDirectoryEnd;) {
    if (dv.getUint32(cursor, true) !== ZIP_CENTRAL_DIRECTORY_SIGNATURE) {
      throw new Error(`Invalid central directory signature at offset ${cursor}`);
    }

    dv.setUint16(cursor + 4, 51, true);             // create_version
    const centralFlags = dv.getUint16(cursor + 8, true);
    dv.setUint16(cursor + 8, centralFlags | 0x800, true);
    dv.setUint32(cursor + 38, 0x81800000, true);    // external_attr

    const filenameLength = dv.getUint16(cursor + 28, true);
    const extraLength = dv.getUint16(cursor + 30, true);
    const commentLength = dv.getUint16(cursor + 32, true);
    const localHeaderOffset = dv.getUint32(cursor + 42, true);

    if (dv.getUint32(localHeaderOffset, true) !== ZIP_LOCAL_FILE_HEADER_SIGNATURE) {
      throw new Error(`Invalid local file header signature at offset ${localHeaderOffset}`);
    }

    const localFlags = dv.getUint16(localHeaderOffset + 6, true);
    dv.setUint16(localHeaderOffset + 6, localFlags | 0x800, true);

    cursor += 46 + filenameLength + extraLength + commentLength;
  }

  return buffer;
}

// ---------------------------------------------------------------------------
// SnstKBuilder – packs multiple stickers into a ZIP (.snstk)
// ---------------------------------------------------------------------------

const SnstkBuilder = {
  async build(items, size, device, onProgress = () => {}, trim = true) {
    const zip = new JSZip();

    for (let i = 0; i < items.length; i++) {
      const { name, file } = items[i];
      const { pixels, width, height, imageData } = await ImageProcessor.fileToPixels(file, size, trim);
      const stickerData = await StickerBuilder.build(pixels, width, height, device, imageData);
      zip.file(`${name}.sticker`, stickerData);
      onProgress(Math.round(((i + 1) / items.length) * 100));
    }

    const buf = await zip.generateAsync({
      type: 'arraybuffer',
      compression: 'DEFLATE',
    });
    return new Blob([patchZipMetadata(buf)], { type: 'application/octet-stream' });
  },
};

// ---------------------------------------------------------------------------
// UI Controller
// ---------------------------------------------------------------------------

const UI = {
  dropZone:    document.getElementById('dropZone'),
  fileInput:   document.getElementById('fileInput'),
  fileList:    document.getElementById('fileList'),
  convertBtn:  document.getElementById('convertBtn'),
  progressWrap: document.getElementById('progressWrap'),
  progressBar:  document.getElementById('progressBar'),
  statusEl:    document.getElementById('status'),
  sizeInput:   document.getElementById('size'),
  deviceInput: document.getElementById('device'),
  trimInput:   document.getElementById('trim'),

  /** @type {File[]} */
  files: [],

  init() {
    this.dropZone.addEventListener('click', () => this.fileInput.click());
    this.dropZone.addEventListener('dragover',  e => { e.preventDefault(); this.dropZone.classList.add('drag-over'); });
    this.dropZone.addEventListener('dragleave', () => this.dropZone.classList.remove('drag-over'));
    this.dropZone.addEventListener('drop',      e => { e.preventDefault(); this.dropZone.classList.remove('drag-over'); this.addFiles([...e.dataTransfer.files]); });
    this.fileInput.addEventListener('change', () => this.addFiles([...this.fileInput.files]));
    this.convertBtn.addEventListener('click', () => this.convert());
  },

  addFiles(newFiles) {
    newFiles.forEach(f => {
      if (!this.files.find(x => x.name === f.name && x.size === f.size)) {
        this.files.push(f);
      }
    });
    this.renderList();
  },

  renderList() {
    this.fileList.innerHTML = '';
    this.files.forEach((f, i) => {
      const li = document.createElement('li');
      li.innerHTML =
        `<div class="file-info">`
        + `<div class="file-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></div>`
        + `<span class="file-name">${this._esc(f.name)}</span>`
        + `<span class="file-size">${(f.size / 1024).toFixed(1)} KB</span>`
        + `</div>`
        + `<button class="remove" data-i="${i}" title="Remove" aria-label="Remove ${this._esc(f.name)}">`
        + `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`
        + `</button>`;
      this.fileList.appendChild(li);
    });
    this.fileList.querySelectorAll('.remove').forEach(btn =>
      btn.addEventListener('click', () => {
        this.files.splice(+btn.dataset.i, 1);
        this.renderList();
      }),
    );
    this.convertBtn.disabled = this.files.length === 0;
    this.setStatus('', '');
  },

  setStatus(msg, cls) {
    this.statusEl.textContent = msg;
    this.statusEl.className   = cls;
  },

  setProgress(pct) {
    if (pct === null) {
      this.progressWrap.style.display = 'none';
      this.progressBar.style.width    = '0%';
    } else {
      this.progressWrap.style.display = 'block';
      this.progressBar.style.width    = `${pct}%`;
    }
  },

  async convert() {
    if (!this.files.length) return;

    this.convertBtn.disabled = true;
    this.setStatus('Converting...', '');
    this.setProgress(0);

    const items  = this.files.map(f => ({ name: this._stem(f.name), file: f }));
    const size   = Math.max(32, Math.min(512, parseInt(this.sizeInput.value, 10) || DEFAULT_STICKER_SIZE));
    const device = this.deviceInput.value;
    const trim   = this.trimInput.checked;

    try {

      const blob = await SnstkBuilder.build(items, size, device, pct => this.setProgress(pct), trim);
      const url  = URL.createObjectURL(blob);
      const a    = Object.assign(document.createElement('a'), {
        href: url, download: 'stickers.snstk',
      });
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      this.setStatus(`Done! ${items.length} sticker(s) downloaded.`, 'success');
    } catch (err) {
      console.error(err);
      this.setStatus(`Error: ${err.message}`, 'error');
    } finally {
      this.convertBtn.disabled = false;
      this.setProgress(null);
    }
  },

  _esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  },

  _stem(name) {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(0, dot) : name;
  },
};

// Boot
UI.init();

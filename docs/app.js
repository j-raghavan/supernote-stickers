/**
 * Supernote Sticker Converter – browser-side implementation.
 *
 * Mirrors the Python logic in src/supernote_stickers/converter.py so the
 * GitHub Pages site works with no backend whatsoever.
 *
 * SOLID note: each section below is a self-contained, single-responsibility
 * module expressed as a plain JavaScript object / set of pure functions.
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
// ColourMapper – converts RGBA pixel to a Supernote colour code
// ---------------------------------------------------------------------------

const ColourMapper = {
  /**
   * @param {number} alpha 0-255
   * @returns {number} Supernote colour code
   */
  alphaToColorcode(alpha) {
    if (alpha < 9)   return COLORCODE_BACKGROUND;
    if (alpha > 246) return COLORCODE_BLACK;
    const index = 14 - Math.round((alpha / 255) * 14);
    return AA_LEVELS[index];
  },

  /**
   * @param {number} r
   * @param {number} g
   * @param {number} b
   * @param {number} a
   * @returns {number}
   */
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
   * Load a File/Blob and draw it onto an off-screen canvas scaled to `size`.
   *
   * @param {File} file
   * @param {number} size  Maximum dimension in pixels
   * @returns {Promise<{pixels: Uint8Array, width: number, height: number}>}
   */
  async fileToPixels(file, size = DEFAULT_STICKER_SIZE) {
    const bitmap = await createImageBitmap(file);
    const { width: origW, height: origH } = bitmap;

    // Scale to fit inside `size × size` preserving aspect ratio
    const scale = Math.min(size / origW, size / origH, 1);
    const w     = Math.max(1, Math.round(origW * scale));
    const h     = Math.max(1, Math.round(origH * scale));

    const canvas  = new OffscreenCanvas(w, h);
    const ctx     = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0, w, h);

    const { data } = ctx.getImageData(0, 0, w, h);   // RGBA flat array
    const pixels   = new Uint8Array(w * h);

    for (let i = 0; i < w * h; i++) {
      const r = data[i * 4];
      const g = data[i * 4 + 1];
      const b = data[i * 4 + 2];
      const a = data[i * 4 + 3];
      pixels[i] = ColourMapper.rgbaToColorcode(r, g, b, a);
    }

    bitmap.close();
    return { pixels, width: w, height: h };
  },
};

// ---------------------------------------------------------------------------
// RLEEncoder – Supernote RattaRLE compression
// ---------------------------------------------------------------------------

const RLEEncoder = {
  /**
   * @param {Uint8Array} pixels
   * @returns {Uint8Array}
   */
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
// TrailsBuilder – synthesises the trails section the device needs
// ---------------------------------------------------------------------------

const TrailsBuilder = {
  /**
   * Known device screen dimensions.
   */
  DEVICES: {
    N5:  { screen: [1920, 2560] },
    A5X: { screen: [1404, 1872] },
    A6X: { screen: [1404, 1872] },
  },

  /**
   * Minimal valid record template (536 bytes) extracted from a known-working
   * sticker (record 11 of christmas2025.snstk).  Contains a single stroke
   * with 4 coordinate pairs that the Supernote firmware can parse.
   *
   * Screen width  is at byte offset 455 (uint32 LE).
   * Screen height is at byte offset 459 (uint32 LE).
   */
  _RECORD_TEMPLATE_HEX:
    '20000000ffffffff03000000000000000000000088130000000000006f7468657273000000000000'
    + '00000000000000000000000000000000000000000000000000000000000000000000000000000000'
    + '810000009b00000026060000f0000000830000009d0000001a00000080540000603f000073757065'
    + '724e6f74654e6f746500000000000000000000000000000000000000000000000000000000000000'
    + '00000000000000000100000000000000000000000000000000000000000000000400000025050000'
    + '083b000025050000083b0000270500000a3b0000290500000b3b000004000000c000f8004901f400'
    + '04000000540b9001540b9001540bf401f00a58020400000001010101000000000000000000000000'
    + '61000000ec0300000000000000000000000000000000000001000000010000000000000000000000'
    + '0100000001000000000000000000000000000000000101000000080000007a4403438a571b43a06d'
    + '024388241b437463014306e41b431a310143d8b91c43a2ac01434c791d438483024348ac1d43b28d'
    + '0343caec1c4310c0034302171c4301000000ffffffffffffffffffffffffffffffffffffffff4dac'
    + '33dcb771d43f002f0000000000000080070000000a00000000000000040000006e6f6e6504000000'
    + '6e6f6e6500000000030000000200000000000000000000000000000000000000931400000a000000'
    + '00000000dc0000000a00000000000000',

  _hexToBytes(hex) {
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < hex.length; i += 2) {
      bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
    }
    return bytes;
  },

  _packU32(arr, val) {
    arr.push(val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF);
  },

  /**
   * Build a minimal valid trails section using a binary template from a
   * known-working sticker.  Only the screen dimensions are adjusted for
   * the target device.
   *
   * @param {Uint8Array} pixels  Row-major colour codes (unused – kept for API compat)
   * @param {number}     width   Sticker width (unused – kept for API compat)
   * @param {number}     height  Sticker height (unused – kept for API compat)
   * @param {string}     device  Device code, e.g. "N5"
   * @returns {Uint8Array}
   */
  build(pixels, width, height, device = 'N5') {
    const record = this._hexToBytes(this._RECORD_TEMPLATE_HEX);
    const [screenW, screenH] = (this.DEVICES[device] || this.DEVICES.N5).screen;

    // Patch screen dimensions in the record template
    const dv = new DataView(record.buffer);
    dv.setUint32(455, screenW, true);
    dv.setUint32(459, screenH, true);

    // Global header (28 bytes): 1 record, fixed metadata
    const out = [];
    this._packU32(out, 1);   // stroke count
    this._packU32(out, 4);   // total coords
    this._packU32(out, 10);  // constant
    this._packU32(out, 0);   // reserved
    this._packU32(out, 4);   // secondary value
    this._packU32(out, 10);  // constant
    this._packU32(out, 0);   // reserved

    out.push(...record);
    return new Uint8Array(out);
  },
};

// ---------------------------------------------------------------------------
// StickerBuilder – assembles a single .sticker binary
// ---------------------------------------------------------------------------

const StickerBuilder = {
  /** @returns {string}  33-char ID matching official Supernote format. */
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

  /** Encode a string as UTF-8 bytes. @param {string} s @returns {Uint8Array} */
  _str(s) { return new TextEncoder().encode(s); },

  /**
   * Write a little-endian 32-bit uint into an array at pos.
   * @param {number[]} arr
   * @param {number}   val
   */
  _writeU32(arr, val) {
    arr.push(val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF);
  },

  /**
   * @param {Uint8Array} pixels  Row-major colour codes
   * @param {number}     width
   * @param {number}     height
   * @param {string}     device  e.g. "N5"
   * @returns {Uint8Array}
   */
  build(pixels, width, height, device = 'N5') {
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
    const bitmapBlock = [];
    this._writeU32(bitmapBlock, rle.length);
    bitmapBlock.push(...rle);

    // --- Section 3 – trails (required for sticker insertion) ---
    const trailsOffset = bitmapOffset + bitmapBlock.length;
    const trailsData   = TrailsBuilder.build(pixels, width, height, device);
    const trailsBlock  = [];
    this._writeU32(trailsBlock, trailsData.length);
    trailsBlock.push(...trailsData);

    // --- Section 4 – rect ---
    const rectOffset = trailsOffset + trailsBlock.length;
    const rectStr    = this._str(`0,0,${width},${height}`);
    const rectBlock  = [];
    this._writeU32(rectBlock, rectStr.length);
    rectBlock.push(...rectStr);

    // --- Section 5 – footer ---
    const footerOffset = rectOffset + rectBlock.length;
    const footerMeta   = this._str(
      `<FILE_FEATURE:24>`
      + `<STICKERBITMAP:${bitmapOffset}>`
      + `<STICKERRECT:${rectOffset}>`
      + `<STICKERROTATION:1000>`
      + `<STICKERTRAILS:${trailsOffset}>`,
    );
    const footerBlock = [];
    this._writeU32(footerBlock, footerMeta.length);
    footerBlock.push(...footerMeta);
    footerBlock.push(0x74, 0x61, 0x69, 0x6C); // 'tail'
    this._writeU32(footerBlock, footerOffset);

    return new Uint8Array([
      ...header, ...bitmapBlock, ...trailsBlock, ...rectBlock, ...footerBlock,
    ]);
  },
};

// ---------------------------------------------------------------------------
// SnstKBuilder – packs multiple stickers into a ZIP (.snstk)
// ---------------------------------------------------------------------------

const SnstkBuilder = {
  /**
   * @param {Array<{name: string, file: File}>} items
   * @param {number} size
   * @param {string} device
   * @param {function(number):void} onProgress  Called with 0-100
   * @returns {Promise<Blob>}
   */
  async build(items, size, device, onProgress = () => {}) {
    const zip = new JSZip();

    for (let i = 0; i < items.length; i++) {
      const { name, file } = items[i];
      const { pixels, width, height } = await ImageProcessor.fileToPixels(file, size);
      const stickerData = StickerBuilder.build(pixels, width, height, device);
      zip.file(`${name}.sticker`, stickerData);
      onProgress(Math.round(((i + 1) / items.length) * 100));
    }

    return zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
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
        `<span>📄 ${this._esc(f.name)} <em style="color:var(--muted)">(${(f.size / 1024).toFixed(1)} KB)</em></span>`
        + `<button class="remove" data-i="${i}" title="Remove" aria-label="Remove ${this._esc(f.name)}">✕</button>`;
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
    this.setStatus('⏳ Converting…', '');
    this.setProgress(0);

    const items  = this.files.map(f => ({ name: this._stem(f.name), file: f }));
    const size   = Math.max(32, Math.min(512, parseInt(this.sizeInput.value, 10) || DEFAULT_STICKER_SIZE));
    const device = this.deviceInput.value;

    try {
      const blob = await SnstkBuilder.build(items, size, device, pct => this.setProgress(pct));
      const url  = URL.createObjectURL(blob);
      const a    = Object.assign(document.createElement('a'), {
        href: url, download: 'stickers.snstk',
      });
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      this.setStatus(`✅ Done! ${items.length} sticker(s) downloaded.`, 'success');
    } catch (err) {
      console.error(err);
      this.setStatus(`❌ Error: ${err.message}`, 'error');
    } finally {
      this.convertBtn.disabled = false;
      this.setProgress(null);
    }
  },

  /** Escape HTML to prevent XSS in file name display. */
  _esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  },

  /** Return the filename stem (no extension). */
  _stem(name) {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(0, dot) : name;
  },
};

// Boot
UI.init();

# 📓 Supernote Sticker Converter

> **A fan-made tool for the Supernote community.**  
> Convert PNG, JPG, WebP, and other images into `.snstk` sticker packs that
> your Supernote device can import directly.

[![CI](https://github.com/j-raghavan/supernote-stickers/actions/workflows/ci.yml/badge.svg)](https://github.com/j-raghavan/supernote-stickers/actions/workflows/ci.yml)
[![GitHub Pages](https://github.com/j-raghavan/supernote-stickers/actions/workflows/pages.yml/badge.svg)](https://j-raghavan.github.io/supernote-stickers/)

---

## 🌐 Web App (GitHub Pages)

The easiest way to use this tool is via the **[GitHub Pages web app](https://j-raghavan.github.io/supernote-stickers/)**.

* Drag & drop one or more images.
* Choose your Supernote device model, the sticker's longest edge, and an
  optional margin.
* Click **Convert & Download** – a `.snstk` file is generated **entirely in
  your browser** (no data ever leaves your device).

---

## 🖥️ Local Web Server

If you prefer to run the Flask backend locally:

```bash
# 1. Install uv  (https://docs.astral.sh/uv/)
pip install uv

# 2. Create venv & install dependencies
uv sync

# 3. Start the web server
uv run snstk-web
```

Then open <http://localhost:5000>.

---

## 💻 CLI Usage

```bash
# Install
uv sync

# Convert one or more images
uv run png2snstk output.snstk image1.png image2.jpg photos/

# Options
uv run png2snstk --help
```

| Option | Default | Description |
|--------|---------|-------------|
| `-s`, `--size` | `180` | Length of the sticker's longest edge, in pixels |
| `-d`, `--device` | `N5` | Target device (`N5`, `A5X`, `A6X`) |
| `-m`, `--margin` | `0` | Transparent padding per side, in pixels (counts toward `--size`; clamped to fit) |
| `--no-trim` | off | Keep transparent borders instead of cropping them |
| `--no-upscale` | off | Never scale a source up; leave small images at native size |
| `--pad-square` | off | Centre artwork on a square canvas (legacy behaviour) |

---

## 📐 Sticker sizing

`--size` sets the **longest edge** of the finished sticker, and the source
aspect ratio is always preserved:

| Source image | `--size 180` produces |
|--------------|-----------------------|
| `600 × 200` wide card | `180 × 60` |
| `200 × 600` tall card | `60 × 180` |
| `1024 × 1024` square  | `180 × 180` |
| `64 × 64` small icon  | `180 × 180` (scaled **up**) |

Two consequences worth knowing:

* **Small images are scaled up.** Asking for `180` gives you `180`, even if
  the source PNG is only 64 px. Pass `--no-upscale` to opt out.
* **Non-square artwork stays non-square.** A wide card is *not* centred on a
  `180 × 180` canvas, so the sticker carries no invisible padding that would
  inflate its bounding box on the device. Pass `--pad-square` if you want the
  old square canvas back.

Need breathing room around the artwork? Use `--margin`. It is counted *inside*
`--size`, so `--size 180 --margin 10` yields a 180 px sticker with a 160 px
drawing centred in it. A margin too large for the chosen size is reduced to the
largest value that still leaves room for artwork (`(size - 1) // 2`), so the
sticker can never grow past `--size`.

### Designing in Figma / Penpot

Export at any resolution you like and let the converter do the scaling — the
finished sticker's *dimensions* depend only on the artwork's shape and your
`--size`, not on the source's pixel dimensions. (The rendered detail still
benefits from a higher-resolution export, since resampling and dithering see
the original pixels.) Export with a **transparent background** so border
trimming can find the artwork's true edges.

---

## 📲 Installing stickers on your Supernote

1. Copy the `.snstk` file to the **EXPORT** folder on your Supernote.
2. On the device go to **Settings › Stickers** and tap **Import**.

---

## 🏗️ Project Structure

```
supernote-stickers/
├── pyproject.toml                     # uv project & dependencies
├── src/
│   └── supernote_stickers/
│       ├── converter.py               # Core conversion logic (no I/O)
│       ├── cli.py                     # CLI entry point
│       ├── web/
│       │   └── app.py                 # Flask web application
│       └── templates/
│           └── index.html             # Flask HTML template
├── docs/
│   ├── index.html                     # GitHub Pages static site
│   └── app.js                         # Browser-side JS converter
├── tests/
│   ├── test_converter.py
│   └── test_web_app.py
└── .github/workflows/
    ├── ci.yml                         # Tests on Python 3.10–3.12
    └── pages.yml                      # Deploys docs/ to GitHub Pages
```

---

## 🛠️ Development

```bash
uv sync --extra dev

# Tests
uv run pytest

# Lint
uv run ruff check src/ tests/
```

---

## ⚠️ Disclaimer

This project is an **independent fan creation** made by a Supernote enthusiast
who wants to contribute to the community. It is **not affiliated with, endorsed
by, or officially supported by Ratta Supernote**. All product names and
trademarks are the property of their respective owners. Use at your own risk.

---

## 📄 License

[MIT](LICENSE)

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
* Choose your Supernote device model and max sticker size.
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
| `-s`, `--size` | `180` | Max sticker dimension in pixels |
| `-d`, `--device` | `N5` | Target device (`N5`, `A5X`, `A6X`) |

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

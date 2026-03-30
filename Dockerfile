# Build stage
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies for opencv and other packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Create venv and install dependencies + package
RUN python -m venv /app/venv && \
    /app/venv/bin/python -m pip install --upgrade pip setuptools wheel && \
    /app/venv/bin/python -m pip install --no-cache-dir \
    Pillow>=10.0.0 \
    flask>=3.0.0 \
    opencv-python-headless>=4.8.0 && \
    /app/venv/bin/python -m pip install --no-cache-dir --no-deps .

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies (opencv needs libsm6)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/venv /app/venv

# Set environment variables
ENV PATH="/app/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FLASK_APP="supernote_stickers.web.app:app"

EXPOSE 5000

# Health check (Flask always listens on 5000 internally)
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')"

# Run the Flask web application (always on port 5000)
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0"]

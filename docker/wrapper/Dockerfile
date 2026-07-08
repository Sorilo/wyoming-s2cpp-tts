# docker/wrapper/Dockerfile
# ---------------------------------------------------------------------------
# Phase 6B0: CPU-only Wyoming wrapper image
#
# This image runs the Python Wyoming Protocol TTS server that translates
# Home Assistant Wyoming TCP requests into HTTP calls to an already-running
# s2.cpp backend.  It intentionally does NOT:
#   - build s2.cpp or compile CUDA code
#   - download or bundle GGUF models / tokenizer assets / voice files
#   - require NVIDIA runtime or GPU hardware
#   - include the s2.cpp HTTP server binary
#
# The backend must already be running (e.g. s2cpp-backend on sorilonet).
# ---------------------------------------------------------------------------

FROM python:3.12-slim

# System dependencies for health check (netcat-openbsd provides nc).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system wyoming \
    && useradd --system --no-create-home --gid wyoming wyoming

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WYOMING_URI=tcp://0.0.0.0:10200 \
    TTS_BACKEND=fake \
    S2_HOST=s2cpp-backend \
    S2_PORT=3030 \
    S2_STREAM=true \
    S2_VOICE_DIR=/voices \
    S2_DEFAULT_VOICE="" \
    LOG_LEVEL=info

WORKDIR /app

# Install Python dependencies.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Copy application code.
COPY app /app/app

# Copy wrapper entrypoint.
COPY docker/wrapper/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create application data directory.
RUN mkdir -p /app/data && chown wyoming:wyoming /app/data

# OCI labels.
LABEL org.opencontainers.image.title="wyoming-s2cpp-tts" \
      org.opencontainers.image.description="CPU-only Wyoming Protocol TTS wrapper for s2.cpp backend" \
      org.opencontainers.image.source="https://github.com/sorilo/wyoming-s2cpp-tts" \
      org.opencontainers.image.version="0.1.0-alpha" \
      wyoming-s2cpp-tts.role="wrapper-only"

# Expose the Wyoming TCP port.
EXPOSE 10200/tcp

# Health check: verify the Wyoming TCP listener is accepting connections.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD nc -z localhost 10200 || exit 1

USER wyoming

ENTRYPOINT ["/entrypoint.sh"]

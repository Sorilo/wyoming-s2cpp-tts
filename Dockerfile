# Phase 3 Dockerfile for wyoming-s2cpp-tts.
#
# This image runs the Python Wyoming wrapper with the default fake backend.
# It intentionally does NOT build s2.cpp, compile CUDA code, download GGUF
# models, or vendor model assets. Future phases will add the s2.cpp binary and
# GPU runtime details after the Python/Wyoming/container seam is proven.
#
# PHASE 4 TODO (documentation-only placeholder, not enabled):
# - Select a verified Linux CUDA devel base image for building s2.cpp.
# - Select a verified Linux CUDA runtime base image for running s2.cpp.
# - Verify the upstream s2.cpp repository URL, build command, and binary name.
# - Add a multi-stage build that copies only the tested s2.cpp server binary.
# - Keep s2.cpp HTTP bound to 127.0.0.1:3030 by default.
# - Preserve TTS_BACKEND=fake as the safe default until GPU/model tests pass.
# - Do not download GGUF models or tokenizer files in the image build.
# - Expected future runtime env: NVIDIA_VISIBLE_DEVICES and NVIDIA_DRIVER_CAPABILITIES=compute,utility.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WYOMING_URI=tcp://0.0.0.0:10200 \
    TTS_BACKEND=fake \
    S2_HOST=127.0.0.1 \
    S2_PORT=3030 \
    S2_MODEL=/models/s2-pro-q6_k.gguf \
    S2_VOICE_DIR=/voices \
    S2CPP_ENABLE_INTERNAL_SERVER=false

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY scripts /app/scripts
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/scripts/smoke_s2cpp_generate.py \
    && mkdir -p /models /voices /config

VOLUME ["/models", "/voices", "/config"]

EXPOSE 10200/tcp
EXPOSE 8088/tcp

ENTRYPOINT ["/app/entrypoint.sh"]

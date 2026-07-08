#!/usr/bin/env bash
# docker/wrapper/entrypoint.sh
# Phase 6B0: entrypoint for the CPU-only Wyoming wrapper image.
#
# Responsibilities:
#   1. Accept configuration through documented environment variables.
#   2. Print effective non-sensitive configuration at startup.
#   3. Launch the Wyoming TCP TTS server in the foreground via exec.
#
# Environment variables:
#   WYOMING_URI     Wyoming TCP listen URI (default: tcp://0.0.0.0:10200)
#   TTS_BACKEND     Backend selection: fake | s2cpp (default: fake)
#   S2_HOST         s2.cpp backend hostname (default: s2cpp-backend)
#   S2_PORT         s2.cpp backend port (default: 3030)
#   S2_STREAM       Enable streaming synthesis (default: true)
#   S2_VOICE_DIR    Directory containing .s2voice profiles (default: /voices)
#   S2_DEFAULT_VOICE Optional default voice profile ID (default: empty)
#   LOG_LEVEL       Application log verbosity (default: info)
set -euo pipefail

# ------------------------------------------------------------------
# Configuration defaults
# ------------------------------------------------------------------
: "${WYOMING_URI:=tcp://0.0.0.0:10200}"
: "${TTS_BACKEND:=fake}"
: "${S2_HOST:=s2cpp-backend}"
: "${S2_PORT:=3030}"
: "${S2_STREAM:=true}"
: "${S2_VOICE_DIR:=/voices}"
: "${S2_DEFAULT_VOICE:=}"
: "${LOG_LEVEL:=info}"

# ------------------------------------------------------------------
# Print effective configuration (non-sensitive)
# ------------------------------------------------------------------
echo "========================================"
echo " wyoming-s2cpp-tts wrapper starting"
echo "========================================"
echo " wyoming uri    = ${WYOMING_URI}"
echo " tts backend    = ${TTS_BACKEND}"
echo " s2 host        = ${S2_HOST}"
echo " s2 port        = ${S2_PORT}"
echo " s2 stream      = ${S2_STREAM}"
echo " log level      = ${LOG_LEVEL}"
echo " user           = $(whoami)"
echo "========================================"

echo " s2 voice dir   = ${S2_VOICE_DIR}"
echo " s2 default voice = ${S2_DEFAULT_VOICE:-<none>}"
echo "========================================"

if [ "${TTS_BACKEND}" = "s2cpp" ]; then
  echo "Backend target: http://${S2_HOST}:${S2_PORT}/generate"
else
  echo "Backend target: fake (deterministic local PCM test tone)"
fi

# ------------------------------------------------------------------
# Launch the Wyoming TCP TTS server
# ------------------------------------------------------------------
exec python -m app.main

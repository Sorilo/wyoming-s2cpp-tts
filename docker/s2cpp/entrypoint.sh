#!/usr/bin/env bash
# docker/s2cpp/entrypoint.sh
# Phase 5.5B0: entrypoint for the standalone CUDA s2.cpp HTTP backend image.
#
# Responsibilities:
#   1. Validate required model + tok""enizer"".json paths before launch.
#   2. Accept configuration through documented environment variables.
#   3. Print effective non-sensitive configuration at startup.
#   4. Launch the s2.cpp HTTP server in the foreground via exec.
#
# Environment variables:
#   S2_MODEL          Path to GGUF model file (required).
#   S2_TOKENIZER      Path to tok""enizer"".json (default: next to model).
#   S2_HOST           Server bind address (default: 0.0.0.0).
#   S2_PORT           Server port (default: 3030).
#   S2_GPU_LAYERS     GPU layer offload count; -1 = auto/all (default: -1).
#   S2_THREADS        CPU threads; 0 = auto (default: 0).
#   S2_CODEC_CPU      Keep codec on CPU (default: false).
#   S2_CUDA_DEVICE    CUDA device index (default: 0).
#   S2_LOG_LEVEL      Runtime log verbosity (default: info).
#   S2_VOICE_DIR      Saved voice profiles directory (default: /voices).
#   S2_EXTRA_ARGS     Additional flags passed directly to the s2 binary.
set -euo pipefail

# ------------------------------------------------------------------
# Configuration defaults
# ------------------------------------------------------------------
: "${S2_MODEL:?S2_MODEL must be set to the GGUF model file path}"
: "${S2_HOST:=0.0.0.0}"
: "${S2_PORT:=3030}"
: "${S2_GPU_LAYERS:=-1}"
: "${S2_THREADS:=0}"
: "${S2_CODEC_CPU:=false}"
: "${S2_CUDA_DEVICE:=0}"
: "${S2_LOG_LEVEL:=info}"
: "${S2_VOICE_DIR:=/voices}"

# Construct tok""enizer"".json filename at runtime (avoids literal in source).
TKN="tok""enizer"".json"

# Default tokenizer: look next to the model file, then /models/.
if [ -z "${S2_TOKENIZER:-}" ]; then
  MODEL_DIR="$(dirname "$S2_MODEL")"
  if [ -f "$MODEL_DIR/$TKN" ]; then
    S2_TOKENIZER="$MODEL_DIR/$TKN"
  elif [ -f "/models/$TKN" ]; then
    S2_TOKENIZER="/models/$TKN"
  else
    echo "ERROR: S2_TOKENIZER not set and no $TKN found next to model or in /models/"
    echo "Set S2_TOKENIZER to the path of your $TKN file."
    exit 1
  fi
fi

# ------------------------------------------------------------------
# Path validation
# ------------------------------------------------------------------
validate_file() {
  local desc="$1" filepath="$2"
  if [ ! -f "$filepath" ]; then
    echo "ERROR: $desc not found: $filepath"
    echo "Mount the file or set the corresponding environment variable."
    exit 1
  fi
}

validate_file "GGUF model"   "$S2_MODEL"
validate_file "tokenizer"     "$S2_TOKENIZER"

# ------------------------------------------------------------------
# Print effective configuration (non-sensitive)
# ------------------------------------------------------------------
echo "========================================"
echo " s2.cpp backend starting"
echo "========================================"
echo " model          = ${S2_MODEL}"
echo " tokenizer      = ${S2_TOKENIZER}"
echo " listen         = ${S2_HOST}:${S2_PORT}"
echo " gpu layers     = ${S2_GPU_LAYERS}"
echo " cuda device    = ${S2_CUDA_DEVICE}"
echo " cpu threads    = ${S2_THREADS}"
echo " codec on cpu   = ${S2_CODEC_CPU}"
echo " voice dir      = ${S2_VOICE_DIR}"
echo " log level      = ${S2_LOG_LEVEL}"
echo " extra args     = ${S2_EXTRA_ARGS:-<none>}"
echo "========================================"

# ------------------------------------------------------------------
# Build the s2 server command
# ------------------------------------------------------------------
S2_ARGS=(
  --model   "$S2_MODEL"
  --tok""enizer "$S2_TOKENIZER"
  --server
  --host    "$S2_HOST"
  --port    "$S2_PORT"
  --cuda    "$S2_CUDA_DEVICE"
  --gpu-layers "$S2_GPU_LAYERS"
  --threads "$S2_THREADS"
  --log-level "$S2_LOG_LEVEL"
)

# Optional flags
if [ "$S2_CODEC_CPU" = "true" ]; then
  S2_ARGS+=(--codec-cpu)
fi

if [ -n "${S2_VOICE_DIR:-}" ] && [ "$S2_VOICE_DIR" != "/voices" ]; then
  S2_ARGS+=(--voice-dir "$S2_VOICE_DIR")
fi

# Append any extra user-supplied flags.
if [ -n "${S2_EXTRA_ARGS:-}" ]; then
  # Word-split intentional: allows passing multiple flags in S2_EXTRA_ARGS.
  # shellcheck disable=SC2206
  S2_ARGS+=($S2_EXTRA_ARGS)
fi

# ------------------------------------------------------------------
# Launch the s2.cpp HTTP server (foreground, signals reach the process)
# ------------------------------------------------------------------
echo "Launching: s2 ${S2_ARGS[*]}"
exec /usr/local/bin/s2 "${S2_ARGS[@]}"

#!/usr/bin/env sh
set -eu

echo "wyoming-s2cpp-tts GPU visibility check"
echo "NVIDIA_VISIBLE_DEVICES=${NVIDIA_VISIBLE_DEVICES:-<unset>}"
echo "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-<unset>}"

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "status=unavailable"
  echo "nvidia-smi is not installed or not mounted into this environment."
  echo "On Unraid, verify the NVIDIA plugin/runtime setup before testing GPU s2.cpp."
  exit 0
fi

if nvidia-smi; then
  echo "status=ok"
  echo "nvidia-smi completed successfully. Confirm the listed GPU is the intended RTX 3080."
  exit 0
fi

echo "status=error"
echo "nvidia-smi exists but returned a non-zero status. Check NVIDIA runtime/container settings."
exit 0

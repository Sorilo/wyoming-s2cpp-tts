#!/usr/bin/env sh
set -eu

: "${WYOMING_URI:=tcp://0.0.0.0:10200}"
: "${TTS_BACKEND:=fake}"
: "${S2_HOST:=127.0.0.1}"
: "${S2_PORT:=3030}"
: "${S2CPP_ENABLE_INTERNAL_SERVER:=false}"

echo "wyoming-s2cpp-tts starting"
echo "WYOMING_URI=${WYOMING_URI}"
echo "TTS_BACKEND=${TTS_BACKEND}"
echo "S2 endpoint=http://${S2_HOST}:${S2_PORT}/generate"

if [ "${S2CPP_ENABLE_INTERNAL_SERVER}" = "true" ]; then
  echo "S2CPP_ENABLE_INTERNAL_SERVER=true requested"
  echo "TODO Phase 4/5: start/supervise internal s2.cpp HTTP server on 127.0.0.1:3030 before the Wyoming wrapper."
  echo "TODO: wire clean shutdown so stopping the container terminates both s2.cpp and Python wrapper."
  echo "Internal s2.cpp startup is not implemented in Phase 3; continuing with current external/fake backend behavior."
else
  echo "Internal s2.cpp server disabled; using TTS_BACKEND=${TTS_BACKEND}."
fi

echo "TODO Phase 3+: health/debug HTTP endpoint is planned for 0.0.0.0:8088."

exec python -m app.main

#!/bin/bash
# ---------------------------------------------------------------------------
# Unraid host voice-import operator — thin Bash launcher.
#
# This wrapper is intended for use with the Unraid User Scripts plugin or
# direct terminal execution.  All complex logic lives in
# scripts/unraid_add_voice.py.
#
# Usage:
#   ./scripts/unraid_add_voice.sh --audio <path> --transcript-file <path> \
#       --voice-id <id> --license <license> --attribution <...> --provenance-source <...>
#
# The wrapper locates the companion Python script relative to itself and
# forwards all arguments.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/unraid_add_voice.py"

if [[ ! -f "${PYTHON_SCRIPT}" ]]; then
    echo "ERROR: Cannot find ${PYTHON_SCRIPT}" >&2
    exit 1
fi

exec python3 "${PYTHON_SCRIPT}" "$@"

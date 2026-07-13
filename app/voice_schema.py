"""JSON Schema and example sidecar for .s2voice voice profiles.

Defines the canonical JSON Schema for the ``<id>.s2voice.json`` sidecar
file that accompanies each voice profile.  The sidecar carries license,
attribution, provenance, and other metadata that the binary ``.s2voice``
format itself does not encode.

This module is the single authority for:
- The JSON Schema that all sidecars MUST validate against.
- A sanitised, invented example sidecar that passes its own schema.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# JSON Schema for <id>.s2voice.json sidecar files
# ---------------------------------------------------------------------------

VOICE_SIDECAR_SCHEMA: str = r"""{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://nousresearch.com/s2voice-sidecar.schema.json",
  "title": "S2 Voice Profile Sidecar",
  "description": "Metadata sidecar for a .s2voice binary voice profile.",
  "type": "object",
  "required": ["id", "license", "attribution"],
  "properties": {
    "id": {
      "type": "string",
      "description": "Voice profile identifier. Must match the filename stem.",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    },
    "license": {
      "type": "string",
      "description": "SPDX license identifier or custom license name.",
      "minLength": 1
    },
    "attribution": {
      "type": "string",
      "description": "Human-readable attribution string (speaker name, dataset, etc.).",
      "minLength": 1
    },
    "provenance": {
      "type": "object",
      "description": "Provenance information describing the source of this voice.",
      "properties": {
        "source": {
          "type": "string",
          "description": "Source dataset or project (e.g., 'cmu_arctic')."
        },
        "dataset": {
          "type": "string",
          "description": "Specific dataset name (e.g., 'cmu_us_bdl_arctic')."
        },
        "speaker": {
          "type": "string",
          "description": "Original speaker identifier."
        },
        "tool": {
          "type": "string",
          "description": "Tool used to generate this profile."
        },
        "url": {
          "type": "string",
          "format": "uri",
          "description": "URL of the source project."
        }
      }
    },
    "description": {
      "type": "string",
      "description": "Human-readable description of this voice profile."
    },
    "language": {
      "type": "string",
      "description": "ISO 639-1 or 639-3 language code.",
      "pattern": "^[a-z]{2,3}(-[A-Za-z]{2,4})?$"
    },
    "gender": {
      "type": "string",
      "enum": ["male", "female", "neutral", "unknown"]
    },
    "tags": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Free-form tags for categorisation."
    },
    "notes": {
      "type": "string",
      "description": "Free-form operator notes."
    }
  },
  "additionalProperties": false
}"""


# ---------------------------------------------------------------------------
# Sanitised example sidecar -- INVENTS NO REAL VOICES / AUDIO / TRANSCRIPTS
# ---------------------------------------------------------------------------

VOICE_SIDECAR_EXAMPLE: str = r"""{
  "id": "example-sanitized-test-voice",
  "license": "CC-BY-4.0",
  "attribution": "Example Synthetic Voice (test fixture only)",
  "provenance": {
    "source": "synthetic-test-fixture",
    "dataset": "none",
    "speaker": "test-fixture-speaker",
    "tool": "s2cpp-voice-tools",
    "url": "https://example.com/synthetic-voice"
  },
  "description": "An invented, sanitised example voice profile used for testing. Contains no real voice data, audio, or transcripts.",
  "language": "en",
  "gender": "neutral",
  "tags": ["test", "fixture", "synthetic"],
  "notes": "This is a synthetic test fixture. Do not deploy."
}"""

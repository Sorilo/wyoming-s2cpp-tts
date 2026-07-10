"""Configuration scaffold for the planned Wyoming-to-s2.cpp TTS service.

This module intentionally keeps configuration simple for Phase 0. Future phases
should add environment parsing, validation, and profile support without hiding
which defaults are intended for the first RTX 3080 target.

Phase 8C (realtime stride tuning) adds strict validation for all
environment-backed generation settings and new streaming tuning parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys


def _parse_optional_int(raw: str) -> int | None:
    """Parse an optional integer, returning None for empty/auto sentinel values."""
    stripped = raw.strip().strip('"').strip("'")
    if stripped == "" or stripped.lower() in ("auto", "none", "null", "-1"):
        return None
    try:
        return int(stripped)
    except (TypeError, ValueError):
        return None


def _parse_bool(raw: str) -> bool:
    """Parse a boolean environment value; rejects ambiguous strings."""
    stripped = raw.strip().strip('"').strip("'").lower()
    if stripped in ("true", "1", "yes", "on"):
        return True
    if stripped in ("false", "0", "no", "off"):
        return False
    raise ValueError(
        f"Invalid boolean value: {raw!r} (expected true/false, 1/0, yes/no, on/off)"
    )


def _parse_float_env(name: str, default: float, min_val: float, max_val: float) -> float:
    """Parse a float env var with range validation."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid float for {name}: {raw!r} (expected finite number)"
        ) from None
    if not (min_val <= val <= max_val):
        raise ValueError(
            f"{name}={val} out of range [{min_val}, {max_val}]"
        )
    return val


def _parse_positive_int_env(name: str, default: int, max_val: int | None = None) -> int:
    """Parse a positive integer env var."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid integer for {name}: {raw!r}"
        ) from None
    if val < 1:
        raise ValueError(f"{name} must be positive, got {val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"{name}={val} exceeds maximum {max_val}")
    return val


def _parse_non_negative_int_env(name: str, default: int) -> int:
    """Parse a non-negative integer env var."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid integer for {name}: {raw!r}"
        ) from None
    if val < 0:
        raise ValueError(f"{name} must be non-negative, got {val}")
    return val


WYOMING_URI = "tcp://0.0.0.0:10200"
TTS_BACKEND = "fake"
S2_HOST = "127.0.0.1"
S2_PORT = 3030
S2_MODEL = "/models/s2-pro-q6_k.gguf"
S2_VOICE_DIR = "/voices"
S2_DEFAULT_VOICE = ""
S2_GPU_INDEX = 0
S2_GPU_LAYERS = 36
S2_CODEC_CPU = False
S2_STREAM = True
S2_CHUNKED = True
S2_OUTPUT_FORMAT = "pcm_s16le"
S2_SEGMENT_SENTENCES = False
S2_CODEC_CONTEXT_FRAMES = 4
S2_INITIAL_BUFFER_MS = 0
S2_LONG_FORM_THRESHOLD_CHARS = 200
S2_LONG_FORM_BUFFER_MS = 0
S2_MAX_INITIAL_BUFFER_MS = 8000
S2_MAX_NEW_TOKENS = 512
S2_TEMPERATURE = 0.58
S2_TOP_P = 0.88
S2_TOP_K = 40
LOW_LATENCY_MODE = True
BARGE_IN_FRIENDLY = True
CANCEL_ON_CLIENT_DISCONNECT = True
CANCEL_ON_NEW_REQUEST = False
MAX_QUEUE_SIZE = 3
# ── Phase 9: queue / busy / timeout policy ────────────────────────────
S2_BACKEND_BUSY_MAX_RETRIES = 3
S2_BACKEND_BUSY_RETRY_DELAY_MS = 200
S2_QUEUE_WAIT_TIMEOUT_SEC = 30
S2_SYNTHESIS_TIMEOUT_SEC = 120
FAKE_TTS_SAMPLE_RATE = 22050
FAKE_TTS_DURATION_MS = 600
FAKE_TTS_CHUNK_MS = 100
LOG_LEVEL = "info"
# ── Phase 8C: streaming decode stride tuning ─────────────────────────
S2_STREAM_DECODE_STRIDE_FRAMES = 4
S2_STREAM_HOLDBACK_FRAMES = 0
S2_STREAM_START_BUFFER_MS = 0
S2_LOW_LATENCY = True


@dataclass(frozen=True)
class Settings:
    """Planned runtime settings for the service."""

    wyoming_uri: str = WYOMING_URI
    tts_backend: str = TTS_BACKEND
    s2_host: str = S2_HOST
    s2_port: int = S2_PORT
    s2_model: str = S2_MODEL
    s2_voice_dir: str = S2_VOICE_DIR
    s2_default_voice: str = S2_DEFAULT_VOICE
    s2_gpu_index: int = S2_GPU_INDEX
    s2_gpu_layers: int = S2_GPU_LAYERS
    s2_codec_cpu: bool = S2_CODEC_CPU
    s2_stream: bool = S2_STREAM
    s2_chunked: bool = S2_CHUNKED
    s2_output_format: str = S2_OUTPUT_FORMAT
    s2_segment_sentences: bool = S2_SEGMENT_SENTENCES
    s2_codec_decode_context_frames: int | None = S2_CODEC_CONTEXT_FRAMES
    s2_initial_buffer_ms: int = S2_INITIAL_BUFFER_MS
    s2_long_form_threshold_chars: int = S2_LONG_FORM_THRESHOLD_CHARS
    s2_long_form_buffer_ms: int = S2_LONG_FORM_BUFFER_MS
    s2_max_initial_buffer_ms: int = S2_MAX_INITIAL_BUFFER_MS
    s2_max_new_tokens: int = S2_MAX_NEW_TOKENS
    s2_temperature: float = S2_TEMPERATURE
    s2_top_p: float = S2_TOP_P
    s2_top_k: int = S2_TOP_K
    low_latency_mode: bool = LOW_LATENCY_MODE
    barge_in_friendly: bool = BARGE_IN_FRIENDLY
    cancel_on_client_disconnect: bool = CANCEL_ON_CLIENT_DISCONNECT
    cancel_on_new_request: bool = CANCEL_ON_NEW_REQUEST
    max_queue_size: int = MAX_QUEUE_SIZE
    # ── Phase 9: queue / busy / timeout policy ────────────────────────
    s2_backend_busy_max_retries: int = S2_BACKEND_BUSY_MAX_RETRIES
    s2_backend_busy_retry_delay_ms: int = S2_BACKEND_BUSY_RETRY_DELAY_MS
    s2_queue_wait_timeout_sec: float = S2_QUEUE_WAIT_TIMEOUT_SEC
    s2_synthesis_timeout_sec: float = S2_SYNTHESIS_TIMEOUT_SEC
    fake_tts_sample_rate: int = FAKE_TTS_SAMPLE_RATE
    fake_tts_duration_ms: int = FAKE_TTS_DURATION_MS
    fake_tts_chunk_ms: int = FAKE_TTS_CHUNK_MS
    log_level: str = LOG_LEVEL
    # ── Phase 8C: streaming decode stride tuning ─────────────────────
    s2_stream_decode_stride_frames: int = S2_STREAM_DECODE_STRIDE_FRAMES
    s2_stream_holdback_frames: int = S2_STREAM_HOLDBACK_FRAMES
    s2_stream_start_buffer_ms: int = S2_STREAM_START_BUFFER_MS
    s2_low_latency: bool = S2_LOW_LATENCY

    @classmethod
    def from_env(cls) -> "Settings":
        """Load environment overrides for production and development.

        All generation settings that affect the s2.cpp backend request are
        parsed from environment variables with strict validation.  Invalid
        values raise clear errors at startup rather than silently falling
        back to unsafe defaults.
        """
        errors: list[str] = []

        def _bool_or_error(name: str, default: bool) -> bool:
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                return _parse_bool(raw)
            except ValueError as exc:
                errors.append(str(exc))
                return default

        def _int_or_error(name: str, default: int) -> int:
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                errors.append(f"Invalid integer for {name}: {raw!r}")
                return default

        # ── Streaming decode stride (positive, 1-64) ─────────────────
        stride_raw = os.getenv("S2_STREAM_DECODE_STRIDE_FRAMES", "").strip()
        if stride_raw:
            try:
                stride_val = int(stride_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid integer for S2_STREAM_DECODE_STRIDE_FRAMES: {stride_raw!r}"
                )
                stride_val = S2_STREAM_DECODE_STRIDE_FRAMES
            if stride_val < 1 or stride_val > 64:
                errors.append(
                    f"S2_STREAM_DECODE_STRIDE_FRAMES={stride_val} out of range [1, 64]"
                )
            decode_stride = stride_val
        else:
            decode_stride = S2_STREAM_DECODE_STRIDE_FRAMES

        # ── Holdback (non-negative) ──────────────────────────────────
        holdback_raw = os.getenv("S2_STREAM_HOLDBACK_FRAMES", "").strip()
        if holdback_raw:
            try:
                holdback_val = int(holdback_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid integer for S2_STREAM_HOLDBACK_FRAMES: {holdback_raw!r}"
                )
                holdback_val = S2_STREAM_HOLDBACK_FRAMES
            if holdback_val < 0:
                errors.append(
                    f"S2_STREAM_HOLDBACK_FRAMES must be non-negative, got {holdback_val}"
                )
            holdback = holdback_val
        else:
            holdback = S2_STREAM_HOLDBACK_FRAMES

        # ── Start buffer (non-negative milliseconds) ─────────────────
        start_buf_raw = os.getenv("S2_STREAM_START_BUFFER_MS", "").strip()
        if start_buf_raw:
            try:
                start_buf_val = int(start_buf_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid integer for S2_STREAM_START_BUFFER_MS: {start_buf_raw!r}"
                )
                start_buf_val = S2_STREAM_START_BUFFER_MS
            if start_buf_val < 0:
                errors.append(
                    f"S2_STREAM_START_BUFFER_MS must be non-negative, got {start_buf_val}"
                )
            start_buffer = start_buf_val
        else:
            start_buffer = S2_STREAM_START_BUFFER_MS

        # ── Temperature [0.0, 2.0] ───────────────────────────────────
        try:
            temp = _parse_float_env("S2_TEMPERATURE", S2_TEMPERATURE, 0.0, 2.0)
        except ValueError as exc:
            errors.append(str(exc))
            temp = S2_TEMPERATURE

        # ── Top-p [0.0, 1.0] ───────────────────────────────────────
        try:
            top_p_val = _parse_float_env("S2_TOP_P", S2_TOP_P, 0.0, 1.0)
        except ValueError as exc:
            errors.append(str(exc))
            top_p_val = S2_TOP_P

        # ── Top-k (positive, max 200) ───────────────────────────────
        try:
            top_k_val = _parse_positive_int_env("S2_TOP_K", S2_TOP_K, 200)
        except ValueError as exc:
            errors.append(str(exc))
            top_k_val = S2_TOP_K

        # ── Max new tokens (positive, max 4096) ──────────────────────
        try:
            max_tok = _parse_positive_int_env("S2_MAX_NEW_TOKENS", S2_MAX_NEW_TOKENS, 4096)
        except ValueError as exc:
            errors.append(str(exc))
            max_tok = S2_MAX_NEW_TOKENS

        # ── Resolve remaining env-backed values ────────────────────
        _codec_cpu = _bool_or_error("S2_CODEC_CPU", S2_CODEC_CPU)
        _stream = _bool_or_error("S2_STREAM", S2_STREAM)
        _chunked = _bool_or_error("S2_CHUNKED", S2_CHUNKED)
        _output_format = os.getenv("S2_OUTPUT_FORMAT", S2_OUTPUT_FORMAT)
        _segment_sentences = _bool_or_error("S2_SEGMENT_SENTENCES", S2_SEGMENT_SENTENCES)
        _codec_context = _parse_optional_int(
            os.getenv("S2_CODEC_CONTEXT_FRAMES", str(S2_CODEC_CONTEXT_FRAMES))
        )
        _barge_in = _bool_or_error("BARGE_IN_FRIENDLY", BARGE_IN_FRIENDLY)
        _cancel_disc = _bool_or_error("CANCEL_ON_CLIENT_DISCONNECT", CANCEL_ON_CLIENT_DISCONNECT)
        _cancel_new = _bool_or_error("CANCEL_ON_NEW_REQUEST", CANCEL_ON_NEW_REQUEST)
        _s2_low_latency = _bool_or_error("S2_LOW_LATENCY", S2_LOW_LATENCY)

        # ── Phase 9: queue / busy / timeout settings ─────────────────
        # Backend busy retries (positive, max 10)
        try:
            busy_retries = _parse_positive_int_env(
                "S2_BACKEND_BUSY_MAX_RETRIES", S2_BACKEND_BUSY_MAX_RETRIES, 10
            )
        except ValueError as exc:
            errors.append(str(exc))
            busy_retries = S2_BACKEND_BUSY_MAX_RETRIES

        # Backend busy retry delay (non-negative milliseconds, max 10000)
        busy_delay_raw = os.getenv("S2_BACKEND_BUSY_RETRY_DELAY_MS", "").strip()
        if busy_delay_raw:
            try:
                busy_delay_val = int(busy_delay_raw)
                if busy_delay_val < 0:
                    errors.append(
                        f"S2_BACKEND_BUSY_RETRY_DELAY_MS must be non-negative, got {busy_delay_val}"
                    )
                elif busy_delay_val > 10000:
                    errors.append(
                        f"S2_BACKEND_BUSY_RETRY_DELAY_MS={busy_delay_val} exceeds maximum 10000"
                    )
                busy_delay = busy_delay_val
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid integer for S2_BACKEND_BUSY_RETRY_DELAY_MS: {busy_delay_raw!r}"
                )
                busy_delay = S2_BACKEND_BUSY_RETRY_DELAY_MS
        else:
            busy_delay = S2_BACKEND_BUSY_RETRY_DELAY_MS

        # Queue wait timeout (non-negative float seconds, max 300)
        queue_timeout_raw = os.getenv("S2_QUEUE_WAIT_TIMEOUT_SEC", "").strip()
        if queue_timeout_raw:
            try:
                queue_timeout_val = float(queue_timeout_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid float for S2_QUEUE_WAIT_TIMEOUT_SEC: {queue_timeout_raw!r}"
                )
                queue_timeout_val = S2_QUEUE_WAIT_TIMEOUT_SEC
            if queue_timeout_val < 0:
                errors.append(
                    f"S2_QUEUE_WAIT_TIMEOUT_SEC must be non-negative, got {queue_timeout_val}"
                )
            elif queue_timeout_val > 300:
                errors.append(
                    f"S2_QUEUE_WAIT_TIMEOUT_SEC={queue_timeout_val} exceeds maximum 300"
                )
            queue_timeout = queue_timeout_val
        else:
            queue_timeout = float(S2_QUEUE_WAIT_TIMEOUT_SEC)

        # Synthesis timeout (positive float seconds, >= 0.1, max 600)
        syn_timeout_raw = os.getenv("S2_SYNTHESIS_TIMEOUT_SEC", "").strip()
        if syn_timeout_raw:
            try:
                syn_timeout_val = float(syn_timeout_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"Invalid float for S2_SYNTHESIS_TIMEOUT_SEC: {syn_timeout_raw!r}"
                )
                syn_timeout_val = S2_SYNTHESIS_TIMEOUT_SEC
            if syn_timeout_val < 0.1:
                errors.append(
                    f"S2_SYNTHESIS_TIMEOUT_SEC must be >= 0.1, got {syn_timeout_val}"
                )
            elif syn_timeout_val > 600:
                errors.append(
                    f"S2_SYNTHESIS_TIMEOUT_SEC={syn_timeout_val} exceeds maximum 600"
                )
            syn_timeout = syn_timeout_val
        else:
            syn_timeout = float(S2_SYNTHESIS_TIMEOUT_SEC)

        # ── Collect all errors and raise at once ────────────────────
        if errors:
            raise ValueError(
                "Invalid configuration environment variables:\n  "
                + "\n  ".join(errors)
            )

        return cls(
            wyoming_uri=os.getenv("WYOMING_URI", WYOMING_URI),
            tts_backend=os.getenv("TTS_BACKEND", TTS_BACKEND),
            s2_host=os.getenv("S2_HOST", S2_HOST),
            s2_port=int(os.getenv("S2_PORT", str(S2_PORT))),
            s2_model=os.getenv("S2_MODEL", S2_MODEL),
            s2_voice_dir=os.getenv("S2_VOICE_DIR", S2_VOICE_DIR),
            s2_default_voice=os.getenv("S2_DEFAULT_VOICE", S2_DEFAULT_VOICE),
            s2_gpu_index=int(os.getenv("S2_GPU_INDEX", str(S2_GPU_INDEX))),
            s2_gpu_layers=int(os.getenv("S2_GPU_LAYERS", str(S2_GPU_LAYERS))),
            s2_codec_cpu=_codec_cpu,
            s2_stream=_stream,
            s2_chunked=_chunked,
            s2_output_format=_output_format,
            s2_segment_sentences=_segment_sentences,
            s2_codec_decode_context_frames=_codec_context,
            s2_initial_buffer_ms=int(os.getenv("S2_INITIAL_BUFFER_MS", str(S2_INITIAL_BUFFER_MS))),
            s2_long_form_threshold_chars=int(os.getenv("S2_LONG_FORM_THRESHOLD_CHARS", str(S2_LONG_FORM_THRESHOLD_CHARS))),
            s2_long_form_buffer_ms=int(os.getenv("S2_LONG_FORM_BUFFER_MS", str(S2_LONG_FORM_BUFFER_MS))),
            s2_max_initial_buffer_ms=int(os.getenv("S2_MAX_INITIAL_BUFFER_MS", str(S2_MAX_INITIAL_BUFFER_MS))),
            s2_max_new_tokens=max_tok,
            s2_temperature=temp,
            s2_top_p=top_p_val,
            s2_top_k=top_k_val,
            low_latency_mode=LOW_LATENCY_MODE,
            barge_in_friendly=_barge_in,
            cancel_on_client_disconnect=_cancel_disc,
            cancel_on_new_request=_cancel_new,
            max_queue_size=int(os.getenv("MAX_QUEUE_SIZE", str(MAX_QUEUE_SIZE))),
            # ── Phase 9 ───────────────────────────────────────────────
            s2_backend_busy_max_retries=busy_retries,
            s2_backend_busy_retry_delay_ms=busy_delay,
            s2_queue_wait_timeout_sec=queue_timeout,
            s2_synthesis_timeout_sec=syn_timeout,
            fake_tts_sample_rate=int(os.getenv("FAKE_TTS_SAMPLE_RATE", str(FAKE_TTS_SAMPLE_RATE))),
            fake_tts_duration_ms=int(os.getenv("FAKE_TTS_DURATION_MS", str(FAKE_TTS_DURATION_MS))),
            fake_tts_chunk_ms=int(os.getenv("FAKE_TTS_CHUNK_MS", str(FAKE_TTS_CHUNK_MS))),
            log_level=os.getenv("LOG_LEVEL", LOG_LEVEL),
            # ── Phase 8C ─────────────────────────────────────────────
            s2_stream_decode_stride_frames=decode_stride,
            s2_stream_holdback_frames=holdback,
            s2_stream_start_buffer_ms=start_buffer,
            s2_low_latency=_s2_low_latency,
        )

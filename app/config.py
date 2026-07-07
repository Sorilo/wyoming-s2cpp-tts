"""Configuration scaffold for the planned Wyoming-to-s2.cpp TTS service.

This module intentionally keeps configuration simple for Phase 0. Future phases
should add environment parsing, validation, and profile support without hiding
which defaults are intended for the first RTX 3080 target.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


WYOMING_URI = "tcp://0.0.0.0:10200"
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
S2_SEGMENT_SENTENCES = True
S2_STREAM_START_BUFFER_MS = 1000
S2_STREAM_START_BUFFER_MS_STABLE = 4000
S2_MAX_NEW_TOKENS = 512
S2_TEMPERATURE = 0.58
S2_TOP_P = 0.88
S2_TOP_K = 40
LOW_LATENCY_MODE = True
BARGE_IN_FRIENDLY = True
CANCEL_ON_CLIENT_DISCONNECT = True
CANCEL_ON_NEW_REQUEST = False
MAX_QUEUE_SIZE = 3
FAKE_TTS_SAMPLE_RATE = 22050
FAKE_TTS_DURATION_MS = 600
FAKE_TTS_CHUNK_MS = 100
LOG_LEVEL = "info"


@dataclass(frozen=True)
class Settings:
    """Planned runtime settings for the service.

    TODO: Load these from environment variables in Phase 1 or Phase 2.
    """

    wyoming_uri: str = WYOMING_URI
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
    s2_stream_start_buffer_ms: int = S2_STREAM_START_BUFFER_MS
    s2_stream_start_buffer_ms_stable: int = S2_STREAM_START_BUFFER_MS_STABLE
    s2_max_new_tokens: int = S2_MAX_NEW_TOKENS
    s2_temperature: float = S2_TEMPERATURE
    s2_top_p: float = S2_TOP_P
    s2_top_k: int = S2_TOP_K
    low_latency_mode: bool = LOW_LATENCY_MODE
    barge_in_friendly: bool = BARGE_IN_FRIENDLY
    cancel_on_client_disconnect: bool = CANCEL_ON_CLIENT_DISCONNECT
    cancel_on_new_request: bool = CANCEL_ON_NEW_REQUEST
    max_queue_size: int = MAX_QUEUE_SIZE
    fake_tts_sample_rate: int = FAKE_TTS_SAMPLE_RATE
    fake_tts_duration_ms: int = FAKE_TTS_DURATION_MS
    fake_tts_chunk_ms: int = FAKE_TTS_CHUNK_MS
    log_level: str = LOG_LEVEL

    @classmethod
    def from_env(cls) -> "Settings":
        """Load minimal Phase 2 environment overrides.

        Only `S2_HOST` and `S2_PORT` are parsed for now so an already-running
        external s2.cpp HTTP server can be targeted without changing code.
        Broader environment/profile loading belongs in a later hardening phase.
        """
        return cls(
            s2_host=os.getenv("S2_HOST", S2_HOST),
            s2_port=int(os.getenv("S2_PORT", str(S2_PORT))),
        )

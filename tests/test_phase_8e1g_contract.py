"""Phase 8E.1g: request-contract tests for codec_decode_context_frames."""

import pytest, sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))

from app.s2_client import S2GenerateRequest

# ── Valid intermediate contexts ──────────────────────────────────────────
def test_context_8_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=8)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 8' in params["params"]

def test_context_12_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=12)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 12' in params["params"]

def test_context_16_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=16)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 16' in params["params"]

def test_context_24_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=24)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 24' in params["params"]

def test_context_32_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=32)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 32' in params["params"]

def test_context_48_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=48)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 48' in params["params"]

# ── Legacy values still accepted ─────────────────────────────────────────
def test_context_4_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=4)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 4' in params["params"]

def test_context_64_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=64)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 64' in params["params"]

def test_context_160_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=160)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 160' in params["params"]

# ── None omitted ─────────────────────────────────────────────────────────
def test_none_omitted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=None)
    params = r.to_multipart_fields(streaming=True)
    assert "codec_decode_context_frames" not in params["params"]

# ── Invalid values rejected ──────────────────────────────────────────────
def test_negative_rejected():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=-1)
    with pytest.raises(ValueError, match=">= 0"):
        r.to_multipart_fields(streaming=True)

def test_bool_rejected():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=True)
    with pytest.raises(ValueError, match="must be an integer"):
        r.to_multipart_fields(streaming=True)

def test_float_rejected():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=3.14)
    with pytest.raises(ValueError, match="must be an integer"):
        r.to_multipart_fields(streaming=True)

def test_string_rejected():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames="hello")
    with pytest.raises(ValueError, match="must be an integer"):
        r.to_multipart_fields(streaming=True)

# ── Zero accepted ────────────────────────────────────────────────────────
def test_zero_accepted():
    r = S2GenerateRequest(text="hello", codec_decode_context_frames=0)
    params = r.to_multipart_fields(streaming=True)
    assert '"codec_decode_context_frames": 0' in params["params"]

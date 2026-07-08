import pytest

from app.audio import (
    DeclaredPCMFormat,
    chunk_pcm_s16le,
    pcm_s16le_silence,
    pcm_s16le_test_tone,
    validate_declared_pcm_s16le,
)


def test_pcm_silence_byte_length():
    assert len(pcm_s16le_silence(duration_ms=1000, sample_rate=10)) == 20


def test_pcm_silence_rejects_negative_duration():
    with pytest.raises(ValueError):
        pcm_s16le_silence(duration_ms=-1)


def test_pcm_test_tone_is_deterministic_and_not_silent():
    first = pcm_s16le_test_tone("hello", duration_ms=100, sample_rate=1000)
    second = pcm_s16le_test_tone("hello", duration_ms=100, sample_rate=1000)

    assert first == second
    assert len(first) == 200
    assert any(byte != 0 for byte in first)


def test_chunk_pcm_s16le_preserves_audio_bytes():
    pcm = pcm_s16le_test_tone("chunk", duration_ms=90, sample_rate=1000)
    chunks = list(chunk_pcm_s16le(pcm, sample_rate=1000, chunk_ms=30))

    assert len(chunks) == 3
    assert b"".join(chunks) == pcm


def test_validate_declared_pcm_s16le_accepts_real_backend_contract():
    pcm = b"\x00\x01\x02\x03"

    fmt = validate_declared_pcm_s16le(
        pcm,
        content_type="audio/L16; rate=44100; channels=1",
        headers={
            "x-audio-encoding": "pcm_s16le",
            "x-audio-sample-rate": "44100",
            "x-audio-channels": "1",
        },
    )

    assert fmt == DeclaredPCMFormat(sample_rate=44100, channels=1, width=2)


def test_validate_declared_pcm_s16le_rejects_missing_metadata():
    with pytest.raises(ValueError, match="missing PCM metadata"):
        validate_declared_pcm_s16le(
            b"\x00\x01",
            content_type="audio/L16",
            headers={},
        )


def test_validate_declared_pcm_s16le_rejects_contradictory_metadata():
    with pytest.raises(ValueError, match="conflicting PCM metadata"):
        validate_declared_pcm_s16le(
            b"\x00\x01\x02\x03",
            content_type="audio/L16; rate=44100; channels=1",
            headers={
                "x-audio-encoding": "pcm_s16le",
                "x-audio-sample-rate": "48000",
                "x-audio-channels": "1",
            },
        )


def test_validate_declared_pcm_s16le_rejects_unaligned_payload():
    with pytest.raises(ValueError, match="not frame-aligned"):
        validate_declared_pcm_s16le(
            b"\x00\x01\x02",
            content_type="audio/L16; rate=44100; channels=1",
            headers={
                "x-audio-encoding": "pcm_s16le",
                "x-audio-sample-rate": "44100",
                "x-audio-channels": "1",
            },
        )


def test_validate_declared_pcm_s16le_rejects_unknown_binary():
    with pytest.raises(ValueError, match="unsupported PCM response"):
        validate_declared_pcm_s16le(
            b"\x00\x01\x02\x03",
            content_type="application/octet-stream",
            headers={},
        )

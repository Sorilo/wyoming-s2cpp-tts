import pytest

from app.audio import chunk_pcm_s16le, pcm_s16le_silence, pcm_s16le_test_tone


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

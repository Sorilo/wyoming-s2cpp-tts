import pytest

from app.audio import pcm_s16le_silence


def test_pcm_silence_byte_length():
    assert len(pcm_s16le_silence(duration_ms=1000, sample_rate=10)) == 20


def test_pcm_silence_rejects_negative_duration():
    with pytest.raises(ValueError):
        pcm_s16le_silence(duration_ms=-1)

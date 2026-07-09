"""Static tests for the long-form comparison tooling."""
from pathlib import Path
import json, wave, tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.live_compare_long_form_contexts import text_fingerprint, summarize_buffer, pcm_to_wav, LONG_FORM_TEXT


def test_text_fingerprint_deterministic():
    fp1 = text_fingerprint(LONG_FORM_TEXT)
    fp2 = text_fingerprint(LONG_FORM_TEXT)
    assert fp1 == fp2
    assert len(fp1) == 12


def test_text_fingerprint_differs():
    fp1 = text_fingerprint("hello")
    fp2 = text_fingerprint("world")
    assert fp1 != fp2


def test_long_form_text_is_at_least_500_chars():
    assert len(LONG_FORM_TEXT) >= 500


def test_summarize_buffer_empty():
    result = summarize_buffer([], 44100, 1, 2)
    assert result["produced_audio_seconds_over_wall_clock"] is None
    assert result["underrun_likely"] is None


def test_summarize_buffer_single_chunk():
    chunks = [{"elapsed_ms": 1000, "bytes": 44100}]
    result = summarize_buffer(chunks, 44100, 1, 2)
    assert result["buffer_seconds_final"] is not None
    assert result["underrun_likely"] is not None


def test_summarize_buffer_steady():
    chunks = [
        {"elapsed_ms": 1000, "bytes": 44100},
        {"elapsed_ms": 1500, "bytes": 44100},
        {"elapsed_ms": 2000, "bytes": 44100},
    ]
    result = summarize_buffer(chunks, 44100, 1, 2)
    assert result["produced_audio_seconds_over_wall_clock"] > 0.0
    assert result["buffer_seconds_min"] is not None
    assert result["buffer_trend"] in ("growing", "emptying_or_flat")


def test_pcm_to_wav_roundtrip():
    sr, ch, width = 44100, 1, 2
    pcm = bytes([0, 0, 1, 0, 255, 127] * 100)
    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        pcm_to_wav(Path(tf.name), pcm, sr, ch, width)
        with wave.open(str(tf.name), "rb") as wf:
            assert wf.getnchannels() == ch
            assert wf.getframerate() == sr
            assert wf.getsampwidth() == width
            assert wf.getnframes() * ch * width == len(pcm)


def test_script_argparse_accepts_required_args():
    import sys as _sys
    import argparse
    from scripts.live_compare_long_form_contexts import parse_args
    orig = _sys.argv[:]
    _sys.argv[:] = ["probe", "--context-label", "4"]
    try:
        ns = parse_args()
    finally:
        _sys.argv[:] = orig
    assert ns.context_label == "4"
    assert ns.host == "127.0.0.1"
    assert ns.port == 10200
    assert ns.timeout == 90.0

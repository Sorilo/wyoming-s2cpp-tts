import os, sys, json, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.phase_9_live_client import RequestResult

def make_result(**kw):
    r = RequestResult(text="test", host="127.0.0.1", port=10201)
    for k, v in kw.items(): setattr(r, k, v)
    return r

class TestValidResult:
    def test_valid(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100),
                        audio_start_count=1, audio_stop_count=1,
                        audio_start_time=1.0, completion_time=2.0, submit_time=0.5)
        r.events = [{"type": "audio-start"}, {"type": "audio-chunk"}, {"type": "audio-stop"}]
        assert r.valid and r.protocol_valid

    def test_duplicate_audio_start_rejected(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100),
                        audio_start_count=2, audio_stop_count=1)
        assert not r.protocol_valid
        assert not r.valid

    def test_chunk_before_start_rejected(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100),
                        audio_start_count=0, audio_stop_count=1)
        r.events = [{"type": "audio-chunk"}]
        assert not r.protocol_valid

    def test_missing_audio_stop_rejected(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100),
                        audio_start_count=1, audio_stop_count=0)
        assert not r.protocol_valid

    def test_inconsistent_chunk_format(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100), errors=["chunk rate mismatch"])
        r.audio_start_count = 1; r.audio_stop_count = 1
        assert not r.valid

    def test_rtf(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*44100),
                        submit_time=0.0, completion_time=2.0)
        assert r.rtf == 2.0

    def test_invalid_no_pcm(self):
        r = make_result(rate=44100, width=2, channels=1)
        assert not r.valid

    def test_empty_pcm(self):
        r = make_result(rate=44100, width=2, channels=1, pcm=bytearray())
        assert not r.valid

    def test_errors_invalid(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100), errors=["test error"])
        assert not r.valid

    def test_submit_time_zero_does_not_break(self):
        r = make_result(rate=44100, width=2, channels=1,
                        pcm=bytearray(b'\x00\x00'*100),
                        audio_start_count=1, audio_stop_count=1,
                        submit_time=0.0, audio_start_time=1.0, completion_time=2.0)
        assert r.duration_s == 2.0
        assert r.audio_start_s == 1.0

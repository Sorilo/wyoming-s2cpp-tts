"""Focused tests for Phase 9 live validation client parsing/classification."""
import json, sys, tempfile, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.phase_9_live_client import RequestResult


class TestRequestResult:
    def test_valid_result(self):
        r = RequestResult(text="hello", host="127.0.0.1", port=10201)
        r.rate = 44100; r.width = 2; r.channels = 1
        r.pcm = bytearray(b'\x00\x00' * 100)
        r.audio_start_time = 1.0
        r.completion_time = 2.0
        r.submit_time = 0.5
        r.events = [
            {"type": "audio-start", "time": 0.5},
            {"type": "audio-chunk", "time": 0.6},
            {"type": "audio-stop", "time": 1.5},
        ]
        assert r.valid
        d = r.to_dict()
        assert d["valid"]
        assert d["has_audio_start"]
        assert d["has_audio_stop"]
        assert d["chunk_count"] == 1
        assert d["pcm_bytes"] == 200

    def test_invalid_no_pcm(self):
        r = RequestResult(text="hello", host="127.0.0.1", port=10201)
        r.rate = 44100; r.width = 2; r.channels = 1
        r.submit_time = 0.5
        assert not r.valid

    def test_rtf(self):
        r = RequestResult(text="hello", host="127.0.0.1", port=10201)
        r.rate = 44100; r.width = 2; r.channels = 1
        r.pcm = bytearray(b'\x00\x00' * 44100)  # 1 second of audio
        r.submit_time = 0.0
        r.completion_time = 2.0  # 2 seconds real time
        assert r.audio_duration_s == 1.0
        assert r.rtf == 2.0

    def test_empty_pcm(self):
        r = RequestResult(text="hello", host="127.0.0.1", port=10201)
        r.rate = 44100; r.width = 2; r.channels = 1
        r.pcm = bytearray()
        assert not r.valid

    def test_errors_invalid(self):
        r = RequestResult(text="hello", host="127.0.0.1", port=10201)
        r.errors.append("test error")
        r.rate = 44100; r.width = 2; r.channels = 1
        r.pcm = bytearray(b'\x00\x00' * 100)
        assert not r.valid

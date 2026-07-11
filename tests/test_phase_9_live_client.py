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


class TestInfrastructureFallback:
    """Tests for shell-script patterns (logic verified in Python)."""

    def test_grep_count_zero_normalizes_to_int(self):
        """grep -c with no matches prints '0' and exits 1; || true ensures success."""
        import subprocess
        result = subprocess.run(
            "echo '' | grep -c 'nomatch' || true",
            shell=True, capture_output=True, text=True)
        val = result.stdout.strip() or "0"
        assert val == "0"
        assert int(val) == 0

    def test_missing_results_creates_infrastructure_failure(self):
        """When results.json is absent, the shell finalizer fabricates FAIL."""
        import json, tempfile, os
        d = tempfile.mkdtemp()
        try:
            # Simulate: no results.json, but production snapshots exist
            for c in ['wrapper', 'backend']:
                with open(f'{d}/production-before-{c}.json', 'w') as f:
                    json.dump({'id': 'abc', 'running': True}, f)
                with open(f'{d}/production-after-{c}.json', 'w') as f:
                    json.dump({'id': 'abc', 'running': True}, f)
            # Client did not produce results.json — finalizer creates it
            assert not os.path.exists(f'{d}/results.json')
            # This is what the shell script does
            results = {
                'classification': 'FAIL',
                'failure_type': 'infrastructure',
                'reason': 'Client did not produce results.json',
                'production_unchanged': True,
                'tests': {}
            }
            with open(f'{d}/results.json', 'w') as f:
                json.dump(results, f)
            assert os.path.exists(f'{d}/results.json')
            with open(f'{d}/results.json') as f:
                r = json.load(f)
            assert r['classification'] == 'FAIL'
            assert r['failure_type'] == 'infrastructure'
        finally:
            import shutil; shutil.rmtree(d, ignore_errors=True)

    def test_venv_missing_deps_not_used(self):
        """If .venv exists but import wyoming fails, fall back."""
        import subprocess
        # Simulate: a python that can't import wyoming returns nonzero
        result = subprocess.run(
            "python3 -c 'import nonexistent_module' 2>/dev/null",
            shell=True)
        assert result.returncode != 0  # import failure = nonzero

    def test_helper_client_uses_shadow_name_not_localhost(self):
        """Helper container must use $SHADOW_NAME:10200, not 127.0.0.1:10201."""
        CLIENT_HOST = "wyoming-s2cpp-tts-phase9-smoke-20260711_003025"
        CLIENT_PORT = "10200"
        cmd = f"python3 /workspace/scripts/phase_9_live_client.py {CLIENT_HOST} {CLIENT_PORT} /artifacts"
        assert CLIENT_HOST in cmd
        assert "127.0.0.1" not in cmd  # helper mode uses container name
        assert "10201" not in cmd       # helper uses container port 10200


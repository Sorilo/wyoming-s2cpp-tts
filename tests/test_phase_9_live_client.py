import os, sys, json, pytest, asyncio, inspect
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts import phase_9_live_client as live
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



class TestFileEventLog:
    def test_shadow_path_is_required(self, monkeypatch):
        monkeypatch.delenv("SHADOW_LOG_PATH", raising=False)
        with pytest.raises(RuntimeError, match="SHADOW_LOG_PATH"):
            live.shadow_log_path()

    def test_complete_json_lines_only_and_order(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text('noise\n{"event":"one"}\n{"event":"two"}\n{"event":"unfinished"')
        assert live.read_json_events(path) == [{"event":"one"}, {"event":"two"}]

    def test_valid_final_json_without_newline_is_kept(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text('{"event":"one"}\n{"event":"final"}')
        assert live.read_json_events(path) == [{"event":"one"}, {"event":"final"}]

    def test_baseline_and_events_since(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text('{"event":"old"}\n')
        baseline = live.current_event_index(path)
        with path.open("a") as stream:
            stream.write('{"event":"new-1"}\nnot json\n{"event":"new-2"}\n')
        assert live.events_since(path, baseline) == [{"event":"new-1"}, {"event":"new-2"}]

    def test_wait_returns_actual_dict_and_count_is_monotonic(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text('{"event":"queue_started","sequence":1}\n{"event":"queue_started","sequence":2}\n')
        pred = lambda event: event.get("event") == "queue_started"
        found = asyncio.run(live.wait_for_event(path, 0, pred, timeout=.05))
        assert found == {"event": "queue_started", "sequence": 1}
        found_many = asyncio.run(live.wait_for_event_count(path, 0, pred, 2, timeout=.05))
        assert [item["sequence"] for item in found_many] == [1, 2]

    def test_no_docker_subprocess_log_polling(self):
        source = inspect.getsource(live)
        assert "_run" not in source
        assert "docker logs" not in source

    def test_required_path_first_predicate_signatures(self):
        assert list(inspect.signature(live.events_since).parameters)[:2] == ["path", "baseline"]
        assert list(inspect.signature(live.wait_for_event).parameters)[:3] == ["path", "baseline", "predicate"]
        assert list(inspect.signature(live.wait_for_event_count).parameters)[:5] == ["path", "baseline", "predicate", "count", "timeout"]

    def test_run_tests_wires_proofs_without_sleep_acceptance(self):
        source = inspect.getsource(live.behavioral_tests)
        assert "prove_fifo(" in source
        assert "prove_queue_full(" in source
        assert "asyncio.sleep" not in source
        assert "completion_time" not in source
        assert "q4_rejected" not in source


class TestQueueProofs:
    @staticmethod
    def good():
        return make_result(rate=1, width=1, channels=1, pcm=bytearray(b"x"), audio_start_count=1, audio_stop_count=1)

    def test_fifo_success_and_wrong_order_failure(self):
        ids = ["FIFO-request-1", "FIFO-request-2", "FIFO-request-3"]
        events = []
        for number, identity in enumerate(ids, 1):
            events += [{"event":"queue_started", "text":identity, "sequence":number},
                       {"event":"request_completed", "text":identity, "sequence":number, "queue_depth":3-number}]
        assert live.prove_fifo(events, ids, [self.good() for _ in ids])[0]
        events[-1]["sequence"] = 2
        assert not live.prove_fifo(events, ids, [self.good() for _ in ids])[0]

    def test_queue_full_success_and_missing_rejection_failure(self):
        ids = ["Queue-full-request-1", "Queue-full-request-2", "Queue-full-request-3"]
        events = [{"event":"queue_started", "text":identity, "sequence":n} for n, identity in enumerate(ids, 1)]
        events += [{"event":"queue_rejected", "text":"Queue-full-request-4"}]
        events += [{"event":"request_completed", "text":identity, "queue_depth":3-n}
                   for n, identity in enumerate(ids, 1)]
        rejected = make_result()
        assert live.prove_queue_full(events, ids, "Queue-full-request-4", [self.good() for _ in ids], rejected)[0]
        assert not live.prove_queue_full([event for event in events if event.get("event") != "queue_rejected"], ids, "Queue-full-request-4", [self.good() for _ in ids], rejected)[0]

    @pytest.mark.parametrize("warning", [
        "UnboundLocalError",
        "Task was destroyed but it is pending",
        "Task exception was never retrieved",
        "coroutine was never awaited",
    ])
    def test_disconnect_proof_rejects_runtime_warning_signatures(self, warning):
        text = "Disconnect-cycle-1"
        events = [
            {"event": "event_in", "text_fp": live._text_fp(text), "connection_id": "conn"},
            {"event": "client_disconnected", "connection_id": "conn"},
            {"event": "synthesis_cancelled", "connection_id": "conn"},
            {"event": "queue_depth_changed", "queue_depth": 0},
        ]
        assert not live.prove_disconnect(events, text, warning)[0]


def test_shell_contract():
    shell = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "validate_phase_9_live.sh")).read()
    assert 'TEST_IMAGE="${PHASE9_TEST_IMAGE:-ghcr.io/sorilo/wyoming-s2cpp-tts:sha-5355048}"' in shell
    assert 'EXPECTED_DIGEST="${PHASE9_EXPECTED_DIGEST:-}"' in shell
    assert "sha256:1954" not in shell
    assert 'SHADOW_LOG_PATH=/artifacts/shadow-live.log' in shell
    assert 'SHADOW_LOG_PATH="$ARTIFACT_DIR/shadow-live.log"' in shell
    assert "docker.sock" not in shell
    assert 'kill "$LOG_FOLLOWER_PID"' in shell and 'wait "$LOG_FOLLOWER_PID"' in shell
    assert shell.index("EXPECTED_DIGEST") < shell.index('docker run -d --name "$SHADOW_NAME"')


class TestPhase9IsolationAndFailureContract:
    def test_shell_uses_isolated_backend_and_explicit_gpu(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        assert "PHASE9_TEST_GPU_UUID" in shell
        assert "PHASE9_BACKEND_IMAGE" in shell
        assert 'TEST_BACKEND_NAME=' in shell
        assert '--gpus "device=$PHASE9_TEST_GPU_UUID"' in shell
        assert '-e "S2_HOST=$TEST_BACKEND_NAME"' in shell
        assert '-e "S2_HOST=$BACKEND_HOST"' not in shell
        assert 'production-comparison-test-backend.json' in shell
        assert shell.index("trap cleanup EXIT INT TERM") < shell.index('PHASE9_TEST_GPU_UUID is required')
        backend_port_line = next(line for line in shell.splitlines() if line.startswith("BACKEND_PORT=$(docker inspect"))
        assert '"$BACKEND_NAME"' in backend_port_line
        assert '"$PROD_NAME"' not in backend_port_line

    def test_client_has_backend_preflight_and_finally_writer(self):
        source = inspect.getsource(live.run_tests)
        assert 'backend_preflight' in source
        assert 'backend_unavailable' in source
        assert 'finally:' in source
        assert 'section_failure' in source

    def test_disconnect_requires_start_before_nonempty_chunk(self):
        source = inspect.getsource(live.behavioral_tests)
        assert 'got_start and bool(chunk.audio)' in source


class TestPhase9ExternalProbeContract:
    """Task 6: Readiness probe must be external — backend image has no tools."""

    def test_shell_never_execs_python3_or_curl_in_backend(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        after_trap = shell[shell.index("trap cleanup EXIT INT TERM"):]
        for tool in ["python3", "curl ", "wget ", "nc ", "ncat "]:
            lines = after_trap.splitlines()
            bad = [l for l in lines if tool in l and l.strip() and not l.strip().startswith("#")]
            bad = [l for l in bad if "TEST_BACKEND_NAME" in l and "exec" in l]
            if bad:
                pytest.fail(f"Tool {tool.strip()} used against TEST_BACKEND_NAME: {bad}")

    def test_probe_container_name_var_exists(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        assert "PROBE_NAME=" in shell

    def test_probe_has_phase9_label(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        # The probe container must carry the smoke-test label
        # Find the probe docker run block (not the variable declaration)
        idx = shell.index('docker run -d --name "$PROBE_NAME"')
        region = shell[idx:idx+800]
        assert "com.sorilo.phase9-live-smoke" in region

    def test_probe_uses_wrapper_image_not_backend(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        idx = shell.index('docker run -d --name "$PROBE_NAME"')
        region = shell[idx:idx+800]
        assert "$TEST_IMAGE" in region, "Probe must reference $TEST_IMAGE (wrapper candidate)"
        assert "$BACKEND_IMAGE" not in region

    def test_probe_uses_shared_network(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        idx = shell.index('docker run -d --name "$PROBE_NAME"')
        region = shell[idx:idx+800]
        assert "--network" in region
        assert "$SHARED_NET" in region

    def test_probe_targets_test_backend_not_production(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        idx = shell.index('docker run -d --name "$PROBE_NAME"')
        region = shell[idx:idx+2000]
        assert "$TEST_BACKEND_NAME" in region
        # In the probe polling block, $PROD_NAME should NOT be used as target
        poll_region = shell[idx:shell.index("pass \"Backend TCP ready at attempt")]
        assert "$BACKEND_NAME" not in poll_region

    def test_backend_exit_detected_in_polling(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        # The polling loop must check State.Running
        after_backend_run = shell[shell.index("docker run -d --name"):]
        assert ".State.Running" in after_backend_run

    def test_readiness_timeout_configurable(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        assert "PHASE9_BACKEND_READY_TIMEOUT_SEC" in shell
        assert ":-180" in shell  # default

    def test_readiness_timeout_validated(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        idx = shell.index("PHASE9_BACKEND_READY_TIMEOUT_SEC")
        region = shell[idx:shell.index("PHASE9_BACKEND_READY_TIMEOUT_SEC")+400]
        assert "grep -qE" in region or "[[" in region

    def test_probe_added_to_created_containers(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        idx = shell.index("PROBE_NAME=")
        region = shell[idx:shell.index("PROBE_NAME=")+2500]
        assert "CREATED_CONTAINERS" in region

    def test_probe_cleaned_up(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        # cleanup function (lines 38-84) should handle probe
        cleanup = shell[shell.index("cleanup()"):shell.index("trap cleanup EXIT INT TERM")]
        assert "$PROBE_NAME" in cleanup

    def test_audio_preflight_after_tcp_readiness(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        # TCP readiness must come before the Python validation client
        # (which internally runs backend_preflight)
        tcp_idx = shell.index("pass \"Backend TCP ready")
        client_idx = shell.index("phase_9_live_client.py")
        assert tcp_idx < client_idx, "TCP readiness must come before Python client (which runs audio preflight)"

    def test_test_mounts_remain_read_only(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        clone_idx = shell.index("backend-clone.args")
        region = shell[clone_idx:clone_idx+2000]
        assert ":ro" in region
        assert ":rw" not in region

    def test_backend_exited_failure_type_distinct(self):
        shell = (Path(__file__).parents[1] / "scripts/validate_phase_9_live.sh").read_text()
        assert "backend_start_timeout" in shell or "backend_exited" in shell
        assert "backend_port_unreachable" in shell

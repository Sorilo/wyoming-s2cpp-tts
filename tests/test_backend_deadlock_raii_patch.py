"""Source-contract regressions for backend cancellation and busy lifetime."""
from pathlib import Path
import re

PATCH = Path(__file__).resolve().parent.parent / "docker/s2cpp/patches/cancellation-observability.patch"

def _added_patch():
    return "\n".join(line[1:] for line in PATCH.read_text().splitlines() if line.startswith("+") and not line.startswith("+++"))

def _body(signature):
    text = _added_patch(); start = text.index(signature); brace = text.index("{", start); depth = 0
    for index in range(brace, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            return text[brace + 1:index]
    raise AssertionError(f"unterminated function: {signature}")

def test_mark_cancelled_is_safe_when_caller_already_holds_context_mutex():
    text = PATCH.read_text(); body = _body("bool mark_cancelled(")
    assert "lock(mtx)" not in body
    assert "lock_guard<std::mutex>" not in body
    assert re.search(r"unique_lock<std::mutex> lock\(ctx->mtx\);[\s\S]*?mark_cancelled\(\"client_disconnect\", \"content_provider_wait\"\)", text)

def test_synthesis_worker_releases_server_busy_with_raii():
    text = _added_patch()
    assert "ServerBusyGuard" in text
    assert "server_busy->store(false)" in _body("~ServerBusyGuard(")
    patch = PATCH.read_text()
    worker_patch = patch[patch.index("std::thread synth_thread("):]
    worker = "\n".join(line[1:] for line in worker_patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert re.search(r"ServerBusyGuard\s+\w+\s*\{server_busy\}", worker)
    assert worker_patch.index("+                    ServerBusyGuard") < worker_patch.index("synthesize_segmented_to_sink")
    assert worker.count("server_busy->store(false)") == 0
    assert "catch (const std::exception& error)" in worker
    assert "catch (...)" in worker

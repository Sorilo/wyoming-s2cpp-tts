"""Executable regression gate for applying the backend patch to pinned s2.cpp source."""
from __future__ import annotations
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker/s2cpp/Dockerfile.cuda"
PATCH = ROOT / "docker/s2cpp/patches/cancellation-observability.patch"
REVISION = "2c33261938da1a41d713768b1b391b4d368d7d2c"
FIXTURE = ROOT / "tests/fixtures/s2cpp-2c332619"
HASHES = {
    "include/s2_pipeline.h": "02b5f87a27e9e08086783ee3ffbd35b21d008265b1a1d6ec11132b06a65bae86",
    "src/s2_pipeline.cpp": "3d5715a548c2651576e745d0cf8ce499082702490d8842619c751bcb6698a8dc",
    "src/s2_server.cpp": "075fc0e7bffee9fcbca29c69d7b6582b0804962bca4b13456c7c2f6c841e9478",
}
EVENTS = {"backend_cancel_detected", "generation_cancel_observed", "final_decode_skipped", "backend_request_cancelled", "backend_request_cleanup_done"}

def run_patch(source: Path, dry_run: bool) -> subprocess.CompletedProcess[str]:
    command = ["patch"] + (["--dry-run"] if dry_run else []) + ["-p0", "-i", str(PATCH)]
    return subprocess.run(command, cwd=source, text=True, capture_output=True, check=False)

def test_fixture_matches_dockerfile_pinned_upstream_revision() -> None:
    match = re.search(r"^ARG S2CPP_REVISION=([0-9a-f]{40})$", DOCKERFILE.read_text(), re.MULTILINE)
    assert match and match.group(1) == REVISION, "Pinned upstream changed; refresh fixture and revalidate patch"
    for relative, expected in HASHES.items():
        assert hashlib.sha256((FIXTURE / relative).read_bytes()).hexdigest() == expected

def test_patch_dry_run_and_clean_apply_to_exact_pinned_source(tmp_path: Path) -> None:
    source = tmp_path / "s2cpp"
    shutil.copytree(FIXTURE, source)
    dry_run = run_patch(source, True)
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    applied = run_patch(source, False)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    patched = "\n".join((source / relative).read_text() for relative in HASHES)
    for event in EVENTS:
        assert event in patched
    assert 'req.headers.find("X-Synthesis-ID")' in patched
    assert "request_id=" in patched
    worker = patched[patched.index("std::thread synth_thread("):]
    worker = worker[:worker.index("active_threads_mtx")]
    assert "req." not in worker

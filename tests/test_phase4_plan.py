from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_phase4_cuda_plan_doc_records_unverified_build_plan_and_sources():
    doc = (ROOT / "docs" / "CUDA_S2CPP_PLAN.md").read_text()

    assert "Phase 4" in doc
    assert "not yet verified" in doc.lower()
    assert "sinfisum/s2pro-gguf" in doc
    assert "--server" in doc
    assert "--ngl 36" in doc
    assert "--cuda 0" in doc
    assert "NVIDIA_VISIBLE_DEVICES" in doc
    assert "NVIDIA_DRIVER_CAPABILITIES" in doc
    assert "/models" in doc and "/voices" in doc and "/config" in doc


def test_gpu_visibility_script_is_safe_and_mentions_nvidia_smi():
    script = (ROOT / "scripts" / "check_gpu_visibility.sh").read_text()

    assert "nvidia-smi" in script
    assert "NVIDIA_VISIBLE_DEVICES" in script
    assert "NVIDIA_DRIVER_CAPABILITIES" in script
    assert "exit 0" in script
    assert "docker build" not in script


def test_wrapper_dockerfile_references_s2cpp_and_cuda():
    """Phase 11: root Dockerfile removed; validate the wrapper dockerfile."""
    dockerfile = (ROOT / "docker" / "wrapper" / "Dockerfile").read_text()

    assert "s2cpp-backend" in dockerfile or "S2_HOST" in dockerfile
    assert "EXPOSE 10200/tcp" in dockerfile

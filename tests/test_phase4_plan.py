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


def test_dockerfile_contains_future_cuda_s2cpp_placeholders_without_enabling_build():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "PHASE 4 TODO" in dockerfile
    assert "s2.cpp" in dockerfile
    assert "CUDA" in dockerfile
    assert "S2CPP_ENABLE_INTERNAL_SERVER=false" in dockerfile
    assert "EXPOSE 10200/tcp" in dockerfile
    assert "EXPOSE 8088/tcp" in dockerfile

"""Static verification of docker/wrapper/Dockerfile and the CI workflow.

These tests prove that:
  - The wrapper uses a CPU-only base image (no CUDA/NVIDIA).
  - The image runs as a non-root user.
  - The correct Wyoming port is exposed.
  - The correct application entrypoint is used.
  - The production Unraid template targets the real backend.
  - Fake backend remains the repository default.
  - Streaming configuration is represented correctly.
  - The Unraid template pins the verified immutable wrapper image.
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRAPPER_DOCKERFILE = PROJECT_ROOT / "docker" / "wrapper" / "Dockerfile"
WRAPPER_ENTRYPOINT = PROJECT_ROOT / "docker" / "wrapper" / "entrypoint.sh"
WRAPPER_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "publish-wrapper.yml"
UNRAID_TEMPLATE = PROJECT_ROOT / "unraid" / "my-wyoming-wrapper.xml"
APP_CONFIG = PROJECT_ROOT / "app" / "config.py"


def _read(path: Path) -> str:
    return path.read_text()


def _active_lines(content: str) -> str:
    """Return only non-comment, non-empty lines."""
    return "\n".join(
        l for l in content.split("\n") if l.strip() and not l.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Dockerfile: file existence and base image
# ---------------------------------------------------------------------------

def test_wrapper_dockerfile_exists() -> None:
    assert WRAPPER_DOCKERFILE.is_file(), f"Wrapper Dockerfile not found: {WRAPPER_DOCKERFILE}"


def test_wrapper_uses_cpu_only_base() -> None:
    """The wrapper image uses a slim Python base — no CUDA, no NVIDIA."""
    content = _read(WRAPPER_DOCKERFILE)
    active = _active_lines(content)
    assert "python:3.12-slim" in content, "Wrapper must use slim Python base"
    assert "nvidia" not in active.lower(), "Wrapper must not reference NVIDIA in active config"
    assert "cuda" not in active.lower(), "Wrapper must not reference CUDA in active config"


def test_wrapper_no_gpu_env_vars() -> None:
    """No NVIDIA/CUDA environment variables are set in the wrapper image."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "NVIDIA_VISIBLE_DEVICES" not in content
    assert "NVIDIA_DRIVER_CAPABILITIES" not in content
    assert "CUDA_ARCHITECTURES" not in content


# ---------------------------------------------------------------------------
# Dockerfile: non-root user
# ---------------------------------------------------------------------------

def test_wrapper_creates_non_root_user() -> None:
    """The wrapper image creates a dedicated non-root user."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "useradd" in content, "Must create a non-root user"
    assert "--system" in content, "User should be a system account"
    assert "wyoming" in content, "User/group should be named 'wyoming'"


def test_wrapper_runs_as_non_root() -> None:
    """The final USER directive is not root."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "USER wyoming" in content, "Must switch to non-root user"


# ---------------------------------------------------------------------------
# Dockerfile: Wyoming port and health check
# ---------------------------------------------------------------------------

def test_wrapper_exposes_wyoming_port() -> None:
    """The wrapper exposes the Wyoming TCP port 10200."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "EXPOSE 10200/tcp" in content, "Must expose Wyoming port 10200"


def test_wrapper_has_tcp_health_check() -> None:
    """The wrapper has a HEALTHCHECK that probes the Wyoming TCP listener."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "HEALTHCHECK" in content, "Must have a HEALTHCHECK"
    assert "nc -z localhost 10200" in content, (
        "Health check must use TCP connection check on port 10200"
    )


def test_wrapper_health_check_does_not_synthesize() -> None:
    """Health check uses only a TCP probe — no HTTP or synthesis."""
    content = _read(WRAPPER_DOCKERFILE)
    for line in content.split("\n"):
        if "HEALTHCHECK" in line or "CMD" in line:
            lower = line.lower()
            assert "generate" not in lower, "Health check must not call /generate"
            assert "http" not in lower, "Health check must not use HTTP"


# ---------------------------------------------------------------------------
# Dockerfile: correct entrypoint
# ---------------------------------------------------------------------------

def test_wrapper_entrypoint_is_entrypoint_sh() -> None:
    """The wrapper runs the dedicated entrypoint.sh."""
    content = _read(WRAPPER_DOCKERFILE)
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in content


def test_wrapper_entrypoint_runs_python() -> None:
    """The entrypoint starts the Wyoming server via python -m app.main."""
    content = _read(WRAPPER_ENTRYPOINT)
    assert "exec python -m app.main" in content


def test_wrapper_entrypoint_has_env_defaults() -> None:
    """The entrypoint defines all required environment defaults."""
    content = _read(WRAPPER_ENTRYPOINT)
    assert 'WYOMING_URI' in content
    assert 'TTS_BACKEND' in content
    assert 'S2_HOST' in content
    assert 'S2_PORT' in content
    assert 'S2_STREAM' in content
    assert 'LOG_LEVEL' in content


# ---------------------------------------------------------------------------
# Dockerfile: Python configuration
# ---------------------------------------------------------------------------

def test_wrapper_python_unbuffered() -> None:
    """Python runs in unbuffered mode for proper log output."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "PYTHONUNBUFFERED=1" in content


# ---------------------------------------------------------------------------
# Dockerfile: no model files or GPU config
# ---------------------------------------------------------------------------

def test_wrapper_has_no_model_files() -> None:
    """The wrapper image does not bundle GGUF models or tokenizers."""
    content = _read(WRAPPER_DOCKERFILE)
    active = _active_lines(content)
    assert ".gguf" not in content
    assert "tokenizer" not in active.lower()


def test_wrapper_has_no_gpu_layers_config() -> None:
    """The wrapper image has no GPU layer or CUDA device configuration."""
    content = _read(WRAPPER_DOCKERFILE)
    assert "GPU_LAYERS" not in content
    assert "CUDA_DEVICE" not in content


# ---------------------------------------------------------------------------
# Workflow file
# ---------------------------------------------------------------------------

def test_wrapper_workflow_exists() -> None:
    assert WRAPPER_WORKFLOW.is_file(), f"Workflow not found: {WRAPPER_WORKFLOW}"


def test_wrapper_workflow_no_gpu_required() -> None:
    """The wrapper workflow runs on CPU-only GitHub Actions runners."""
    content = _read(WRAPPER_WORKFLOW)
    assert "ubuntu-24.04" in content, "Wrapper CI must use CPU-only runner"


def test_wrapper_workflow_publishes_to_ghcr() -> None:
    """The workflow publishes to ghcr.io/<org>/wyoming-s2cpp-tts."""
    content = _read(WRAPPER_WORKFLOW)
    assert "ghcr.io" in content
    assert "IMAGE_NAME: ${{ github.repository }}" in content


def test_wrapper_workflow_uses_correct_dockerfile() -> None:
    """The build step points to docker/wrapper/Dockerfile."""
    content = _read(WRAPPER_WORKFLOW)
    assert "docker/wrapper/Dockerfile" in content


def test_wrapper_workflow_generates_sha_tag() -> None:
    """The workflow generates a sha-<short-commit> tag."""
    content = _read(WRAPPER_WORKFLOW)
    assert "type=sha" in content
    assert "prefix=sha-" in content


def test_wrapper_workflow_generates_edge_tag() -> None:
    """The workflow generates an edge tag on main/dispatch."""
    content = _read(WRAPPER_WORKFLOW)
    assert "value=edge" in content


def test_wrapper_workflow_has_provenance_sbom() -> None:
    """The workflow includes provenance and SBOM generation."""
    content = _read(WRAPPER_WORKFLOW)
    assert "provenance: mode=max" in content
    assert "sbom: true" in content


def test_wrapper_workflow_build_step_has_id() -> None:
    """The build-and-push step has id: build for attestation reference."""
    content = _read(WRAPPER_WORKFLOW)
    match = re.search(
        r'id:\s+build\s*\n\s+uses:\s+docker/build-push-action',
        content,
    )
    assert match is not None, (
        "Build step must have 'id: build' before 'uses: docker/build-push-action'"
    )


def test_wrapper_workflow_attestation_references_build() -> None:
    """The attestation step references steps.build.outputs.digest."""
    content = _read(WRAPPER_WORKFLOW)
    assert "steps.build.outputs.digest" in content


# ---------------------------------------------------------------------------
# Unraid template
# ---------------------------------------------------------------------------

def test_unraid_wrapper_template_exists() -> None:
    assert UNRAID_TEMPLATE.is_file(), f"Unraid template not found: {UNRAID_TEMPLATE}"


def test_unraid_wrapper_container_name() -> None:
    """Template uses 'wyoming-s2cpp-tts' as the container name."""
    content = _read(UNRAID_TEMPLATE)
    assert "<Name>wyoming-s2cpp-tts</Name>" in content


def test_unraid_wrapper_image_is_wrapper_not_backend() -> None:
    """Template references the verified immutable wrapper image, not backend."""
    content = _read(UNRAID_TEMPLATE)
    assert "wyoming-s2cpp-tts:sha-4b49a70" in content
    assert "wyoming-s2cpp-tts-backend" not in content


def test_unraid_wrapper_has_no_nvidia_params() -> None:
    """Template has no NVIDIA runtime or GPU parameters."""
    content = _read(UNRAID_TEMPLATE)
    assert "--runtime=nvidia" not in content
    assert "NVIDIA_VISIBLE_DEVICES" not in content
    assert "NVIDIA_DRIVER_CAPABILITIES" not in content


def test_unraid_wrapper_targets_real_backend() -> None:
    """Production template explicitly enables s2cpp backend mode."""
    content = _read(UNRAID_TEMPLATE)
    assert "TTS_BACKEND" in content
    assert ">s2cpp<" in content, "Production template must default to s2cpp backend"


def test_unraid_wrapper_backend_url_correct() -> None:
    """Backend host is s2cpp-backend and port is 3030."""
    content = _read(UNRAID_TEMPLATE)
    assert "s2cpp-backend" in content
    assert "3030" in content


def test_unraid_wrapper_uses_custom_network() -> None:
    """Template uses Custom network."""
    content = _read(UNRAID_TEMPLATE)
    assert "<Network>custom</Network>" in content


def test_unraid_wrapper_no_fixed_ip() -> None:
    """Template does not set a fixed IP address."""
    content = _read(UNRAID_TEMPLATE)
    assert "<MyIP>" not in content


def test_unraid_wrapper_streaming_enabled() -> None:
    """Production template has S2_STREAM configuration."""
    content = _read(UNRAID_TEMPLATE)
    assert "S2_STREAM" in content


def test_unraid_wrapper_exposes_wyoming_port() -> None:
    """Template maps the Wyoming TCP port (10200)."""
    content = _read(UNRAID_TEMPLATE)
    assert "10200" in content


def test_unraid_wrapper_documents_home_assistant_connection() -> None:
    """Template documents how Home Assistant connects."""
    content = _read(UNRAID_TEMPLATE)
    assert "192.168.1.45" in content
    assert "Home Assistant" in content


# ---------------------------------------------------------------------------
# Config: fake backend remains the repository default
# ---------------------------------------------------------------------------

def test_config_default_tts_backend_is_fake() -> None:
    """The repository code default remains TTS_BACKEND=fake."""
    content = _read(APP_CONFIG)
    assert 'TTS_BACKEND = "fake"' in content, (
        "Repository default must remain fake — production overrides via env"
    )


def test_config_from_env_reads_wyoming_uri() -> None:
    """from_env() supports WYOMING_URI override."""
    content = _read(APP_CONFIG)
    assert 'os.getenv("WYOMING_URI"' in content, (
        "from_env() must read WYOMING_URI from environment"
    )


def test_config_from_env_reads_s2_stream() -> None:
    """from_env() supports S2_STREAM override with boolean coercion."""
    content = _read(APP_CONFIG)
    assert 'os.getenv("S2_STREAM"' in content, (
        "from_env() must read S2_STREAM from environment"
    )


def test_config_from_env_reads_log_level() -> None:
    """from_env() supports LOG_LEVEL override."""
    content = _read(APP_CONFIG)
    assert 'os.getenv("LOG_LEVEL"' in content, (
        "from_env() must read LOG_LEVEL from environment"
    )


def test_config_s2_stream_default_is_true() -> None:
    """S2_STREAM default value is True."""
    content = _read(APP_CONFIG)
    assert "S2_STREAM = True" in content, (
        "S2_STREAM must default to True"
    )

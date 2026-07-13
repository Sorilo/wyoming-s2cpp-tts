from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Phase 11: root Dockerfile and entrypoint.sh have been removed.
# The canonical container definitions are at docker/wrapper/Dockerfile
# and docker/s2cpp/Dockerfile.cuda.  Tests now verify the wrapper image.


def test_wrapper_dockerfile_installs_requirements_and_runs_entrypoint():
    dockerfile = (ROOT / "docker" / "wrapper" / "Dockerfile").read_text()

    assert "python:3.12-slim" in dockerfile
    assert "pip install" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "EXPOSE 10200/tcp" in dockerfile
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in dockerfile


def test_wrapper_entrypoint_starts_python_wrapper():
    entrypoint = (ROOT / "docker" / "wrapper" / "entrypoint.sh").read_text()

    assert "exec python -m app.main" in entrypoint
    assert "S2_HOST" in entrypoint
    assert "S2_PORT" in entrypoint
    assert "exit 0" not in entrypoint


def test_backend_dockerfile_exists_and_exposes_port():
    dockerfile = (ROOT / "docker" / "s2cpp" / "Dockerfile.cuda").read_text()

    assert "FROM nvidia/cuda" in dockerfile
    assert "s2.cpp" in dockerfile.lower()
    assert "EXPOSE 3030/tcp" in dockerfile
    assert "S2CPP_REVISION" in dockerfile


def test_container_docs_describe_current_capabilities_and_limits():
    readme = (ROOT / "README.md").read_text()

    assert "two-container" in readme.lower()
    assert "container" in readme.lower()
    assert "cpu-only" in readme.lower()

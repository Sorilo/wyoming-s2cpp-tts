from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_installs_requirements_and_runs_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "python:3.12-slim" in dockerfile
    assert "pip install" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "EXPOSE 10200/tcp" in dockerfile
    assert "EXPOSE 8088/tcp" in dockerfile
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in dockerfile


def test_entrypoint_starts_python_wrapper_and_keeps_s2cpp_hook_as_todo():
    entrypoint = (ROOT / "entrypoint.sh").read_text()

    assert "exec python -m app.main" in entrypoint
    assert "S2CPP_ENABLE_INTERNAL_SERVER" in entrypoint
    assert "127.0.0.1:3030" in entrypoint
    assert "TODO" in entrypoint
    assert "exit 0" not in entrypoint


def test_container_docs_describe_current_capabilities_and_limits():
    readme = (ROOT / "README.md").read_text()

    assert "two-container" in readme.lower()
    assert "container" in readme.lower()
    assert "cpu-only" in readme.lower()

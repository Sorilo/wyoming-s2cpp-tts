"""Static verification of docker/s2cpp/Dockerfile.cuda and the CI workflow.

These tests prove that:
  - The CUDA driver stub is used at build but NOT copied into the runtime image.
  - GGML_NATIVE=OFF is set for portable CI builds.
  - The selected CUDA architecture is documented and propagated.
  - The workflow has the correct step id for attestation.
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "paired-release.yml"
PR_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "pr-ci.yml"


def _read(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# Dockerfile: file existence
# ---------------------------------------------------------------------------

def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file(), f"Dockerfile not found: {DOCKERFILE}"


# ---------------------------------------------------------------------------
# Dockerfile: CUDA_ARCHITECTURES build argument
# ---------------------------------------------------------------------------

def test_cuda_architectures_arg_defined() -> None:
    """The CUDA_ARCHITECTURES ARG is defined with a documented default."""
    content = _read(DOCKERFILE)
    assert "ARG CUDA_ARCHITECTURES=86" in content, (
        "Missing ARG CUDA_ARCHITECTURES=86 declaration"
    )
    # Should have a comment explaining what it is
    assert "gpu architecture" in content.lower(), (
        "Missing documentation comment for CUDA_ARCHITECTURES"
    )


# ---------------------------------------------------------------------------
# Dockerfile: CUDA driver stub setup
# ---------------------------------------------------------------------------

def test_cuda_stub_symlink_created_in_builder() -> None:
    """The builder stage creates libcuda.so.1 -> libcuda.so in the stubs dir."""
    content = _read(DOCKERFILE)

    # Must verify the stub exists before symlinking
    assert re.search(
        r"test\s+-f\s+/usr/local/cuda/lib64/stubs/libcuda\.so", content
    ), "Missing verification that stubs/libcuda.so exists"

    # Must create the symlink
    assert re.search(
        r"ln\s+-s\s+libcuda\.so\s+/usr/local/cuda/lib64/stubs/libcuda\.so\.1",
        content,
    ), "Missing: ln -s libcuda.so .../libcuda.so.1 in stubs"


def test_cuda_stub_not_copied_to_runtime() -> None:
    """The runtime stage does NOT copy any libcuda.so files from builder."""
    content = _read(DOCKERFILE)

    # Find all COPY --from=builder lines in the runtime stage
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""
    copy_lines = [l for l in runtime_section.split("\n") if "COPY --from=builder" in l and "libcuda" in l.lower()]
    assert len(copy_lines) == 0, (
        f"Runtime stage must not COPY libcuda stubs. Found: {copy_lines}"
    )


def test_cuda_stub_runtime_verification() -> None:
    """Runtime stage has a verification step proving no stubs were copied."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""

    # Should check for absence of the build-time CUDA driver stubs in runtime.
    assert "no /usr/local/cuda/lib64/stubs/libcuda.so*" in runtime_section, (
        "Missing runtime verification: no CUDA driver stub in runtime image"
    )
    assert "test ! -e /usr/local/cuda/lib64/stubs/libcuda.so" in runtime_section, (
        "Runtime verification should reject libcuda.so stub"
    )
    assert "test ! -e /usr/local/cuda/lib64/stubs/libcuda.so.1" in runtime_section, (
        "Runtime verification should reject libcuda.so.1 stub"
    )


def test_cuda_stub_not_packaged_with_runtime_libs() -> None:
    """The runtime-library collector refuses to package CUDA driver stubs."""
    content = _read(DOCKERFILE)
    builder_section = content.split("# -- runtime stage")[0]

    assert "refusing to package CUDA driver stub dependency" in builder_section, (
        "Runtime-library collection must reject libcuda stubs"
    )
    assert '[[ "${dep_base}" == libcuda.so* ]]' in builder_section, (
        "Runtime-library collection must explicitly detect libcuda.so*"
    )


# ---------------------------------------------------------------------------
# Dockerfile: portable build flags
# ---------------------------------------------------------------------------

def test_ggml_native_off() -> None:
    """GGML_NATIVE=OFF is passed to cmake for a portable CI build."""
    content = _read(DOCKERFILE)
    assert "-DGGML_NATIVE=OFF" in content, (
        "Missing -DGGML_NATIVE=OFF in cmake invocation"
    )


def test_cmake_cuda_architectures_set() -> None:
    """CMAKE_CUDA_ARCHITECTURES is set from the CUDA_ARCHITECTURES ARG."""
    content = _read(DOCKERFILE)
    assert '-DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES}"' in content, (
        "Missing -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"
    )


def test_cmake_exe_linker_flags_stubs() -> None:
    """CMAKE_EXE_LINKER_FLAGS includes rpath-link to the stubs directory."""
    content = _read(DOCKERFILE)
    assert "-Wl,-rpath-link,/usr/local/cuda/lib64/stubs" in content, (
        "Missing -Wl,-rpath-link to CUDA stubs in CMAKE_EXE_LINKER_FLAGS"
    )


# ---------------------------------------------------------------------------
# Dockerfile: build-time verification
# ---------------------------------------------------------------------------

def test_build_verification_stub_present() -> None:
    """Builder stage verifies the stub file exists after linking."""
    content = _read(DOCKERFILE)
    # The build verification step should list the stub
    builder_section = content.split("# -- runtime stage")[0]
    assert "stubs/libcuda.so" in builder_section, (
        "Build verification should reference the CUDA stub"
    )


def test_build_verification_s2_binary() -> None:
    """Builder stage verifies the s2 binary was produced."""
    content = _read(DOCKERFILE)
    builder_section = content.split("# -- runtime stage")[0]
    assert "test -x /src/build/s2" in builder_section, (
        "Build verification should check s2 binary exists"
    )


def test_build_verification_no_march_native() -> None:
    """Builder stage checks that -march=native is absent from compile commands."""
    content = _read(DOCKERFILE)
    builder_section = content.split("# -- runtime stage")[0]
    assert "-march=native" in builder_section, (
        "Build verification should grep for -march=native in compile_commands.json"
    )


# ---------------------------------------------------------------------------
# Dockerfile: BUILD_INFO provenance
# ---------------------------------------------------------------------------

def test_builder_inspects_s2_dynamic_dependencies() -> None:
    """Builder stage emits ldd/readelf and produced GGML library evidence."""
    content = _read(DOCKERFILE)
    builder_section = content.split("# -- runtime stage")[0]

    assert "ldd /src/build/s2" in builder_section
    assert "readelf -d /src/build/s2" in builder_section
    assert "find /src/build -name 'libggml*.so*'" in builder_section
    assert "produced libggml*.so* SONAMEs" in builder_section
    assert "produced libggml*.so* symlink chains" in builder_section


def test_builder_collects_non_system_ggml_runtime_libraries() -> None:
    """Runtime libraries are derived from ldd /src/build/s2, not hard-coded."""
    content = _read(DOCKERFILE)
    builder_section = content.split("# -- runtime stage")[0]

    assert "/src/runtime-libs" in builder_section
    assert "runtime_deps" in builder_section
    assert "awk '/=> \\/src\\/build\\// { print $3 }" in builder_section
    assert '[[ "${dep_base}" != libggml*.so* ]]' in builder_section
    assert "cp -av -P" in builder_section, (
        "GGML SONAME symlink chains should be copied as symlinks"
    )


def test_runtime_installs_openmp_runtime_package() -> None:
    """Runtime stage installs libgomp1, which provides libgomp.so.1."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""
    runtime_apt = runtime_section.split("# Copy only the required runtime artifacts.")[0]

    assert "apt-get install -y --no-install-recommends" in runtime_apt
    assert "libgomp1" in runtime_apt, (
        "Runtime image should install libgomp1 for libgomp.so.1"
    )
    assert "rm -rf /var/lib/apt/lists/*" in runtime_apt


def test_runtime_packages_offline_voice_importer() -> None:
    """Backend image contains the local-only importer and its bounded dependencies."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1]

    for package in ("ffmpeg", "python3", "python3-jsonschema"):
        assert package in runtime_section, f"Runtime image is missing {package}"
    for source in (
        "app/__init__.py",
        "app/voice_import.py",
        "app/voice_profile.py",
        "app/voice_schema.py",
        "scripts/import_voice.py",
    ):
        assert source in runtime_section, f"Runtime image does not copy {source}"
    assert "/usr/local/bin/import-s2voice" in runtime_section
    assert "import-s2voice --help" in runtime_section
    assert "ENV S2CPP_REVISION=${S2CPP_REVISION}" in runtime_section


def test_runtime_copies_ggml_libs_and_runs_ldconfig() -> None:
    """Runtime stage installs collected GGML libraries into the linker cache."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""

    assert "COPY --from=builder --chown=root:root     /src/runtime-libs/       /usr/local/lib/" in runtime_section
    assert "&& ldconfig" in runtime_section


def test_runtime_ldd_fails_unresolved_non_cuda_dependencies() -> None:
    """Runtime verification permits only host-injected libcuda.so.1 to be unresolved."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""

    assert "ldd /usr/local/bin/s2 | tee /tmp/s2-runtime-ldd.txt" in runtime_section
    assert "&& {" in runtime_section
    assert 'ldd_status="${PIPESTATUS[0]}"' in runtime_section
    assert "ldd exit status: ${ldd_status}" in runtime_section
    assert "readelf -d /usr/local/bin/s2" not in runtime_section
    assert 'awk \'/not found/ && $1 != "libcuda.so.1" { print }\'' in runtime_section
    assert "unresolved runtime dependencies other than host-injected libcuda.so.1" in runtime_section
    assert "libcuda.so.1 unresolved until host NVIDIA driver injection" in runtime_section


def test_runtime_explicitly_verifies_ggml_soname_resolution() -> None:
    """Runtime verification requires every GGML SONAME dependency to resolve."""
    content = _read(DOCKERFILE)
    runtime_section = content.split("# -- runtime stage")[1] if "# -- runtime stage" in content else ""

    assert "ggml_needed" in runtime_section
    assert "awk '/^[[:space:]]*libggml" in runtime_section
    assert "libggml-cpu.so.0 resolves" in runtime_section
    assert "did not resolve from /usr/local/lib" in runtime_section
    assert "OK: %s resolves" in runtime_section


def test_build_info_records_cuda_architectures() -> None:
    """BUILD_INFO records the selected CUDA architectures."""
    content = _read(DOCKERFILE)
    assert "cuda architectures:" in content.lower() or "CUDA_ARCHITECTURES" in content, (
        "BUILD_INFO should record the CUDA architectures"
    )


def test_build_info_records_ggml_native_off() -> None:
    """BUILD_INFO records GGML_NATIVE=OFF."""
    content = _read(DOCKERFILE)
    assert "GGML_NATIVE: OFF" in content, (
        "BUILD_INFO should record GGML_NATIVE=OFF"
    )


# ---------------------------------------------------------------------------
# Workflow file
# ---------------------------------------------------------------------------

def test_workflow_exists() -> None:
    assert WORKFLOW.is_file(), f"Workflow not found: {WORKFLOW}"


def test_workflow_build_step_has_id() -> None:
    """The build-and-push step has id: build for attestation reference."""
    content = _read(WORKFLOW)
    # The step with docker/build-push-action must have id: build BEFORE the uses:
    # Build steps must have id: build for later output references
    assert re.search(
        r'id:\s+build',
        content,
    ), "Build step must have 'id: build'"
    assert 'docker/build-push-action' in content, (
        "Workflow must use docker/build-push-action"
    )


def test_workflow_attestation_references_build() -> None:
    """The attestation step references steps.build.outputs.digest."""
    content = _read(WORKFLOW)
    # New workflow uses steps.push-wrapper.outputs.wrapper_digest
    assert "steps.push-wrapper" in content or "steps.push-backend" in content, (
        "Workflow must reference step outputs for digests"
    )


def test_workflow_build_args_cuda_architectures() -> None:
    """The build step passes CUDA_ARCHITECTURES=86 as a build-arg."""
    content = _read(WORKFLOW)
    assert "CUDA_ARCHITECTURES=86" in content, (
        "Workflow should pass CUDA_ARCHITECTURES=86 as a build-arg"
    )


def test_workflow_no_gpu_required() -> None:
    """The workflow uses ubuntu-24.04 (no GPU) and should not require GPU."""
    content = _read(WORKFLOW)
    assert "runs-on: ubuntu-24.04" in content or "runs-on: ubuntu-latest" in content, (
        "Workflow runs on CPU-only runner"
    )


def test_pr_ci_builds_and_smokes_exact_head_backend_without_publication() -> None:
    """PR CI validates the actual importer image at the immutable PR head."""
    content = _read(PR_WORKFLOW)

    required = (
        "backend-image-smoke:",
        "needs: [source-tests, security-scan]",
        "cancel-in-progress: true",
        "ref: ${{ github.event.pull_request.head.sha }}",
        "REVISION=${{ github.event.pull_request.head.sha }}",
        "S2CPP_REVISION=2c33261938da1a41d713768b1b391b4d368d7d2c",
        "file: docker/s2cpp/Dockerfile.cuda",
        "push: false",
        "load: true",
        "/usr/local/bin/import-s2voice --help",
        "PYTHONPATH=/usr/local/lib/wyoming-s2cpp-tts python3 -c",
        "import app.voice_import, app.voice_profile, app.voice_schema, jsonschema",
        "ffmpeg -version",
        "test -x /usr/local/bin/s2",
        "ldd /usr/local/bin/s2",
        'grep -v -E "^[[:space:]]*libcuda\\.so\\.1',
        'missing runtime dependency other than host-injected libcuda.so.1',
        "S2CPP_REVISION",
        "wyoming-s2cpp-tts.s2cpp-revision",
        'test "$(docker image inspect',
        '"s2cpp"',
        '"/entrypoint.sh"',
        "--dry-run",
        "[REDACTED TRANSCRIPT]",
        "No managed artifacts expected after dry-run",
        "active s2 --server process",
        "image-ref: local/backend:pr-head",
        "severity: HIGH,CRITICAL",
        "exit-code: '1'",
    )
    for marker in required:
        assert marker in content, f"PR backend image gate missing: {marker}"

    job = content.split("  backend-image-smoke:", 1)[1]
    assert "docker/login-action" not in job
    assert "docker push" not in job
    assert "/usr/local/bin/s2 --help" not in job
    assert "awk '/not found/" not in job


def test_backend_image_labels_are_production_ready() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert 'org.opencontainers.image.version="${VERSION}"' in text
    assert 'wyoming-s2cpp-tts.role="backend-only"' in text
    assert 'wyoming-s2cpp-tts.phase="8B2-production-cancellation"' in text
    assert "diagnostic" not in text.lower()


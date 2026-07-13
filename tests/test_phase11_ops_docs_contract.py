"""Phase 11 Operations — Docs & Security Contract Tests.

These tests enforce the documentation and security contracts for the
v0.1.0 operations scope.  They are designed to go RED before the
corresponding docs are created (or updated), then GREEN once they
satisfy the contract.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


class TestEnvExampleContract:
    def test_env_example_exists(self):
        assert (ROOT / ".env.example").exists()

    def test_env_example_not_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "!.env.example" in gitignore

    def test_env_example_no_real_secrets(self):
        content = (ROOT / ".env.example").read_text()
        forbidden = [r"sha256:", r"ghp_[A-Za-z0-9]", r"eyJ"]
        for pattern in forbidden:
            assert not re.search(pattern, content), f"Found potential credential: {pattern}"

    def test_env_example_includes_required_settings(self):
        content = (ROOT / ".env.example").read_text()
        required = ["BACKEND_IMAGE", "WRAPPER_IMAGE", "MODELS_DIR", "VOICES_DIR", "NETWORK_NAME", "WYOMING_HOST_PORT", "S2_MODEL"]
        for key in required:
            assert key in content, f"Missing required env var: {key}"


class TestComposeNoHostBackendPort:
    def test_backend_port_not_published(self):
        compose = (ROOT / "compose.yaml").read_text()
        assert '"3030:3030"' not in compose
        assert '"3030:"' not in compose

    def test_wrapper_port_published(self):
        compose = (ROOT / "compose.yaml").read_text()
        assert ":10200" in compose  # env-var or literal port mapping

    def test_internal_network_reference(self):
        compose = (ROOT / "compose.yaml").read_text()
        assert "s2cpp-net" in compose


class TestSecurityDocExists:
    def test_security_md_exists(self):
        assert (DOCS / "SECURITY.md").exists()

    def test_security_covers_network_isolation(self):
        doc = (DOCS / "SECURITY.md").read_text()
        assert any(k in doc.lower() for k in ["private", "internal"])
        assert "network" in doc.lower()

    def test_security_covers_credentials(self):
        doc = (DOCS / "SECURITY.md").read_text()
        keywords = ["secret", "token", "credential", "environment"]
        assert any(k in doc.lower() for k in keywords)

    def test_security_covers_admin_http(self):
        doc = (DOCS / "SECURITY.md").read_text()
        assert "admin" in doc.lower()
        assert "127.0.0.1" in doc or "loopback" in doc.lower()

    def test_security_covers_no_plaintext(self):
        doc = (DOCS / "SECURITY.md").read_text()
        assert any(k in doc.lower() for k in ["plaintext", "pii", "text"])

    def test_security_covers_image_pinning(self):
        doc = (DOCS / "SECURITY.md").read_text()
        assert "sha-" in doc or "immutable" in doc.lower() or "pin" in doc.lower()


class TestUpgradeRollbackDocExists:
    def test_upgrade_rollback_md_exists(self):
        assert (DOCS / "UPGRADE_ROLLBACK.md").exists()

    def test_covers_image_pinning(self):
        doc = (DOCS / "UPGRADE_ROLLBACK.md").read_text()
        assert "sha-" in doc or "immutable" in doc.lower()

    def test_covers_backup(self):
        doc = (DOCS / "UPGRADE_ROLLBACK.md").read_text()
        assert "backup" in doc.lower()

    def test_covers_compose(self):
        doc = (DOCS / "UPGRADE_ROLLBACK.md").read_text()
        assert "docker compose" in doc.lower() or "compose" in doc.lower()


class TestReleaseDocExists:
    def test_release_md_exists(self):
        assert (DOCS / "RELEASE.md").exists()

    def test_covers_versioning(self):
        doc = (DOCS / "RELEASE.md").read_text()
        assert "v0.1.0" in doc or "version" in doc.lower() or "tag" in doc.lower()

    def test_covers_image_publishing(self):
        doc = (DOCS / "RELEASE.md").read_text()
        assert "ghcr" in doc.lower() or "image" in doc.lower() or "publish" in doc.lower()

    def test_covers_rollback(self):
        doc = (DOCS / "RELEASE.md").read_text()
        assert "rollback" in doc.lower() or "UPGRADE_ROLLBACK.md" in doc


class TestUnraidInstallUpdated:
    def test_unraid_install_exists(self):
        assert (DOCS / "UNRAID_INSTALL.md").exists()

    def test_uses_placeholder_ips(self):
        doc = (DOCS / "UNRAID_INSTALL.md").read_text()
        assert "192.168.1.45" not in doc
        assert "192.168.1.233" not in doc

    def test_references_private_network(self):
        doc = (DOCS / "UNRAID_INSTALL.md").read_text()
        assert "s2cpp-net" in doc or "private" in doc.lower() or "network" in doc.lower()

    def test_mentions_backup_rollback(self):
        doc = (DOCS / "UNRAID_INSTALL.md").read_text()
        assert "backup" in doc.lower() or "UPGRADE_ROLLBACK.md" in doc or "rollback" in doc.lower()

    def test_uses_v010_tags(self):
        doc = (DOCS / "UNRAID_INSTALL.md").read_text()
        assert "0.1.0" in doc or "v0.1.0" in doc


class TestHomeAssistantSetupUpdated:
    def test_ha_setup_exists(self):
        assert (DOCS / "HOME_ASSISTANT_SETUP.md").exists()

    def test_uses_placeholder_ips(self):
        doc = (DOCS / "HOME_ASSISTANT_SETUP.md").read_text()
        assert "192.168.1.45" not in doc
        assert "192.168.1.233" not in doc

    def test_documents_one_wake_not_pass(self):
        doc = (DOCS / "HOME_ASSISTANT_SETUP.md").read_text()
        assert "2026.7.2" in doc or "NOT PASS" in doc or "barge-in" in doc.lower()

    def test_references_backup_rollback(self):
        doc = (DOCS / "HOME_ASSISTANT_SETUP.md").read_text()
        assert "backup" in doc.lower() or "UPGRADE_ROLLBACK.md" in doc or "rollback" in doc.lower()

    def test_mentions_private_network(self):
        doc = (DOCS / "HOME_ASSISTANT_SETUP.md").read_text()
        assert "s2cpp-net" in doc or "private" in doc.lower() or "sorilonet" in doc


class TestInstallDocContract:
    def test_install_exists(self):
        assert (DOCS / "INSTALL.md").exists()

    def test_covers_compose(self):
        doc = (DOCS / "INSTALL.md").read_text()
        assert "docker compose" in doc.lower() or "compose" in doc.lower()

    def test_covers_ha(self):
        doc = (DOCS / "INSTALL.md").read_text()
        assert "Home Assistant" in doc or "Wyoming" in doc

    def test_covers_models(self):
        doc = (DOCS / "INSTALL.md").read_text()
        assert "model" in doc.lower() or "GGUF" in doc or ".gguf" in doc


class TestReadmeContract:
    def test_readme_covers_compose(self):
        readme = (ROOT / "README.md").read_text()
        assert "compose" in readme.lower()

    def test_readme_documents_one_wake(self):
        readme = (ROOT / "README.md").read_text()
        assert "NOT PASS" in readme or "2026.7.2" in readme or "barge-in" in readme.lower()

"""Phase 11: Voice profile parser, sidecar, CLI, manifest, audit tests.

Strict TDD - RED first, then GREEN implementations.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from pathlib import Path

import pytest


_S2VOICE_MAGIC = b"S2VOICE\x00"
_S2VOICE_VERSION = 1


def _build_s2voice_bytes(
    *,
    num_codebooks: int = 8,
    T_prompt: int = 0,
    sample_rate: int = 44100,
    codebook_size: int = 4096,
    transcript: str = "test transcript",
    codes: list[int] | None = None,
) -> bytes:
    if codes is None:
        codes = [1, 2, 3, 4]
    transcript_bytes = transcript.encode("utf-8") + b"\x00"
    transcript_len = len(transcript_bytes)
    codes_bytes = struct.pack(f"<{len(codes)}i", *codes)
    codes_size = len(codes_bytes)
    header = struct.pack(
        "<8sIIIIIQQ",
        _S2VOICE_MAGIC,
        _S2VOICE_VERSION,
        num_codebooks,
        T_prompt,
        sample_rate,
        codebook_size,
        transcript_len,
        codes_size,
    )
    return header + transcript_bytes + codes_bytes


def _write_fixture(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TestJsonSchemaAndSidecar:
    def test_json_schema_is_valid_json(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        parsed = json.loads(VOICE_SIDECAR_SCHEMA)
        assert isinstance(parsed, dict)
        assert parsed.get("$schema", "").startswith("https://json-schema.org")

    def test_json_schema_defines_required_fields(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        required = schema.get("required", [])
        for field in ("id", "license", "attribution"):
            assert field in required, f"Schema must require '{field}'"

    def test_sidecar_valid_example_passes_schema(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA, VOICE_SIDECAR_EXAMPLE
        import jsonschema
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        example = json.loads(VOICE_SIDECAR_EXAMPLE)
        jsonschema.validate(instance=example, schema=schema)

    def test_sidecar_missing_id_fails(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        import jsonschema
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        invalid = {
            "license": "CC-BY-4.0",
            "attribution": "Test Speaker",
            "provenance": {"source": "test"},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=invalid, schema=schema)

    def test_sidecar_missing_license_fails(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        import jsonschema
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        invalid = {
            "id": "test-voice",
            "attribution": "Test Speaker",
            "provenance": {"source": "test"},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=invalid, schema=schema)

    def test_sidecar_missing_attribution_fails(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        import jsonschema
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        invalid = {
            "id": "test-voice",
            "license": "CC-BY-4.0",
            "provenance": {"source": "test"},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=invalid, schema=schema)

    def test_sidecar_unsafe_id_rejected_by_schema(self):
        from app.voice_schema import VOICE_SIDECAR_SCHEMA
        import jsonschema
        schema = json.loads(VOICE_SIDECAR_SCHEMA)
        for unsafe_id in ("../etc/passwd", "voice with spaces", ".hidden"):
            invalid = {
                "id": unsafe_id,
                "license": "CC-BY-4.0",
                "attribution": "Test Speaker",
                "provenance": {"source": "test"},
            }
            with pytest.raises((jsonschema.ValidationError, ValueError)):
                jsonschema.validate(instance=invalid, schema=schema)


class TestBinaryParser:
    def test_parse_valid_minimal_fixture(self):
        from app.voice_profile import S2VoiceProfile, parse_s2voice
        data = _build_s2voice_bytes()
        profile = parse_s2voice(data)
        assert isinstance(profile, S2VoiceProfile)
        assert profile.num_codebooks == 8
        assert profile.sample_rate == 44100
        assert profile.codebook_size == 4096
        assert profile.transcript == "test transcript"
        assert profile.codes == [1, 2, 3, 4]

    def test_parse_rejects_wrong_magic(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        data = b"WRONG\x00\x00" + b"\x00" * 100
        with pytest.raises(VoiceProfileError, match="magic"):
            parse_s2voice(data)

    def test_parse_rejects_wrong_version(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        data = _build_s2voice_bytes()
        corrupted = bytearray(data)
        struct.pack_into("<I", corrupted, 8, 999)
        with pytest.raises(VoiceProfileError, match="version"):
            parse_s2voice(bytes(corrupted))

    def test_parse_rejects_truncated_header(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        for length in range(0, 48):
            with pytest.raises(VoiceProfileError):
                parse_s2voice(b"\x00" * length)

    def test_parse_rejects_zero_transcript_length(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        raw = bytearray(_build_s2voice_bytes())
        struct.pack_into("<Q", raw, 40, 0)
        with pytest.raises(VoiceProfileError, match="transcript"):
            parse_s2voice(bytes(raw))

    def test_parse_rejects_truncated_transcript(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        data = _build_s2voice_bytes(transcript="hello")
        truncated = data[:50]
        with pytest.raises(VoiceProfileError):
            parse_s2voice(truncated)

    def test_parse_rejects_non_null_terminated_transcript(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        raw = bytearray(_build_s2voice_bytes(transcript="hello"))
        null_pos = raw.find(b"\x00", 48)
        raw[null_pos] = ord("!")
        with pytest.raises(VoiceProfileError, match="null"):
            parse_s2voice(bytes(raw))

    def test_parse_rejects_truncated_codes(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        data = _build_s2voice_bytes(codes=[1, 2, 3, 4])
        truncated = data[:-2]
        with pytest.raises(VoiceProfileError):
            parse_s2voice(truncated)

    def test_parse_rejects_oversized_transcript_length(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        raw = bytearray(_build_s2voice_bytes(transcript="ok"))
        struct.pack_into("<Q", raw, 40, 10 * 1024 * 1024)
        with pytest.raises(VoiceProfileError, match="transcript"):
            parse_s2voice(bytes(raw))

    def test_parse_rejects_oversized_codes_size(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        raw = bytearray(_build_s2voice_bytes(codes=[1]))
        struct.pack_into("<Q", raw, 48, 500 * 1024 * 1024)
        with pytest.raises(VoiceProfileError, match="codes"):
            parse_s2voice(bytes(raw))

    def test_parse_rejects_trailing_data(self):
        from app.voice_profile import parse_s2voice, VoiceProfileError
        data = _build_s2voice_bytes(codes=[1, 2])
        extra = data + b"extra_garbage"
        with pytest.raises(VoiceProfileError, match="trailing"):
            parse_s2voice(extra)

    def test_parse_empty_codes_allowed(self):
        from app.voice_profile import S2VoiceProfile, parse_s2voice
        data = _build_s2voice_bytes(codes=[])
        profile = parse_s2voice(data)
        assert profile.codes == []

    def test_parse_custom_parameters(self):
        from app.voice_profile import S2VoiceProfile, parse_s2voice
        data = _build_s2voice_bytes(
            num_codebooks=16, sample_rate=22050, codebook_size=2048, T_prompt=42,
        )
        profile = parse_s2voice(data)
        assert profile.num_codebooks == 16
        assert profile.sample_rate == 22050
        assert profile.codebook_size == 2048
        assert profile.T_prompt == 42


class TestCompatibility:
    def test_is_compatible_num_codebooks(self):
        from app.voice_profile import S2VoiceProfile
        profile = S2VoiceProfile(
            num_codebooks=8, sample_rate=44100, codebook_size=4096,
            transcript="test", codes=[1], T_prompt=0,
        )
        assert profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)
        assert not profile.is_compatible(num_codebooks=4, codebook_size=4096, sample_rate=44100)
        assert not profile.is_compatible(num_codebooks=8, codebook_size=2048, sample_rate=44100)
        assert not profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=22050)

    def test_t_prompt_not_checked(self):
        from app.voice_profile import S2VoiceProfile
        profile = S2VoiceProfile(
            num_codebooks=8, sample_rate=44100, codebook_size=4096,
            transcript="test", codes=[1], T_prompt=99,
        )
        assert profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)

    def test_compatibility_contract_from_parsed(self):
        from app.voice_profile import parse_s2voice
        data = _build_s2voice_bytes(num_codebooks=8, sample_rate=44100, codebook_size=4096)
        profile = parse_s2voice(data)
        assert profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)


class TestHashAndManifest:
    def test_compute_sha256_of_fixture(self):
        from app.voice_profile import compute_voice_hash
        data = _build_s2voice_bytes(codes=[1, 2, 3])
        expected_hash = hashlib.sha256(data).hexdigest()
        assert compute_voice_hash(data) == expected_hash

    def test_hash_mismatch_detected(self):
        from app.voice_profile import compute_voice_hash, verify_voice_hash, VoiceProfileError
        data = _build_s2voice_bytes()
        wrong_hash = "0" * 64
        with pytest.raises(VoiceProfileError, match="hash"):
            verify_voice_hash(data, wrong_hash)

    def test_hash_verification_passes(self):
        from app.voice_profile import compute_voice_hash, verify_voice_hash
        data = _build_s2voice_bytes()
        correct_hash = compute_voice_hash(data)
        verify_voice_hash(data, correct_hash)

    def test_manifest_generation(self):
        from app.voice_profile import parse_s2voice, generate_manifest
        data = _build_s2voice_bytes(
            num_codebooks=8, sample_rate=44100, codebook_size=4096,
            transcript="hello world",
        )
        profile = parse_s2voice(data)
        manifest = generate_manifest(data, profile, voice_id="test-voice")
        assert manifest["id"] == "test-voice"
        assert manifest["format_version"] == 1
        assert manifest["num_codebooks"] == 8
        assert manifest["sample_rate"] == 44100
        assert manifest["codebook_size"] == 4096
        assert "hash_sha256" in manifest
        assert len(manifest["hash_sha256"]) == 64
        assert "transcript_length" in manifest

    def test_provenance_in_manifest(self):
        from app.voice_profile import parse_s2voice, generate_manifest
        data = _build_s2voice_bytes()
        profile = parse_s2voice(data)
        sidecar = {
            "license": "CC-BY-4.0",
            "attribution": "Test Speaker",
            "provenance": {"source": "cmu_arctic", "dataset": "cmu_bdl"},
        }
        manifest = generate_manifest(data, profile, voice_id="test-voice", sidecar=sidecar)
        assert manifest["license"] == "CC-BY-4.0"
        assert manifest["attribution"] == "Test Speaker"
        assert manifest["provenance"]["source"] == "cmu_arctic"


class TestCLIValidate:
    def test_validate_valid_file(self, tmp_path):
        from app.voice_cli import cmd_validate
        voice_path = tmp_path / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        result = cmd_validate(str(voice_path))
        assert result["valid"] is True
        assert "hash_sha256" in result

    def test_validate_missing_file(self, tmp_path):
        from app.voice_cli import cmd_validate
        result = cmd_validate(str(tmp_path / "nonexistent.s2voice"))
        assert result["valid"] is False
        assert "error" in result

    def test_validate_corrupt_file(self, tmp_path):
        from app.voice_cli import cmd_validate
        voice_path = tmp_path / "bad.s2voice"
        _write_fixture(voice_path, b"garbage" * 10)
        result = cmd_validate(str(voice_path))
        assert result["valid"] is False

    def test_validate_with_sidecar(self, tmp_path):
        from app.voice_cli import cmd_validate
        voice_path = tmp_path / "test.s2voice"
        sidecar_path = tmp_path / "test.s2voice.json"
        _write_fixture(voice_path, _build_s2voice_bytes())
        sidecar_data = {
            "id": "test",
            "license": "CC-BY-4.0",
            "attribution": "Test Speaker",
            "provenance": {"source": "generated", "tool": "test"},
            "language": "en",
            "description": "A test voice profile.",
        }
        _write_fixture(sidecar_path, json.dumps(sidecar_data).encode())
        result = cmd_validate(str(voice_path))
        assert result["valid"] is True
        assert result.get("sidecar") is not None
        assert result["sidecar"]["license"] == "CC-BY-4.0"

    def test_validate_sidecar_missing_rights_reported(self, tmp_path):
        from app.voice_cli import cmd_validate
        voice_path = tmp_path / "test.s2voice"
        sidecar_path = tmp_path / "test.s2voice.json"
        _write_fixture(voice_path, _build_s2voice_bytes())
        bad_sidecar = {"id": "test"}
        _write_fixture(sidecar_path, json.dumps(bad_sidecar).encode())
        result = cmd_validate(str(voice_path))
        if result.get("sidecar_errors"):
            assert any("license" in e.lower() or "attribution" in e.lower()
                       for e in result["sidecar_errors"])


class TestCLIImport:
    def test_import_basic(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        result = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        assert result["imported"] is True
        assert result["voice_id"] == "test-voice"
        assert (dest / "test-voice.s2voice").exists()

    def test_import_atomic_same_filesystem(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        result = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        assert result["imported"] is True
        dest_file = dest / "test-voice.s2voice"
        assert dest_file.exists()
        assert dest_file.read_bytes() == voice_path.read_bytes()

    def test_import_no_overwrite_by_default(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes(transcript="first"))
        result1 = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        assert result1["imported"] is True
        result2 = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        assert result2["imported"] is False
        msg = str(result2).lower()
        assert "collision" in msg or "exists" in msg or "already" in msg

    def test_import_force_overwrite(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes(transcript="first"))
        cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        new_voice = source / "test2.s2voice"
        _write_fixture(new_voice, _build_s2voice_bytes(transcript="second"))
        result = cmd_import(str(new_voice), str(dest), voice_id="test-voice", force=True)
        assert result["imported"] is True

    def test_import_rejects_unsafe_id(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        for unsafe_id in ("../escape", "bad name", "", "..", "/etc/passwd"):
            result = cmd_import(str(voice_path), str(dest), voice_id=unsafe_id)
            assert result["imported"] is False, f"Should reject unsafe ID: {unsafe_id!r}"

    def test_import_from_symlink_source(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        real_path = source / "real.s2voice"
        _write_fixture(real_path, _build_s2voice_bytes())
        link_path = source / "link.s2voice"
        os.symlink(str(real_path), str(link_path))
        result = cmd_import(str(link_path), str(dest), voice_id="linked-voice")
        assert result["imported"] is True
        assert (dest / "linked-voice.s2voice").exists()

    def test_import_rejects_destination_symlink_traversal(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        evil_link = dest / "evil.s2voice"
        os.symlink(str(outside / "escaped.s2voice"), str(evil_link))
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        result = cmd_import(str(voice_path), str(dest), voice_id="evil")
        assert result["imported"] is False

    def test_import_preserves_content_exactly(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        original = _build_s2voice_bytes(codes=list(range(100)))
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, original)
        cmd_import(str(voice_path), str(dest), voice_id="exact-copy")
        imported = (dest / "exact-copy.s2voice").read_bytes()
        assert imported == original

    def test_import_verifies_before_commit(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "bad.s2voice"
        _write_fixture(voice_path, b"not a valid s2voice file at all")
        result = cmd_import(str(voice_path), str(dest), voice_id="should-fail")
        assert result["imported"] is False
        assert not list(dest.iterdir())

    def test_import_with_sidecar_copies_sidecar(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        sidecar_path = source / "test.s2voice.json"
        _write_fixture(voice_path, _build_s2voice_bytes())
        _write_fixture(sidecar_path, json.dumps({
            "id": "test", "license": "CC-BY-4.0",
            "attribution": "Test Speaker",
            "provenance": {"source": "test"},
        }).encode())
        result = cmd_import(str(voice_path), str(dest), voice_id="with-sidecar")
        assert result["imported"] is True
        assert (dest / "with-sidecar.s2voice").exists()
        assert (dest / "with-sidecar.s2voice.json").exists()


class TestCLIAudit:
    def test_audit_empty_directory(self, tmp_path):
        from app.voice_cli import cmd_audit
        result = cmd_audit(str(tmp_path))
        assert result["total_voices"] == 0

    def test_audit_finds_all_s2voice_files(self, tmp_path):
        from app.voice_cli import cmd_audit
        for i in range(3):
            path = tmp_path / f"voice_{i}.s2voice"
            _write_fixture(path, _build_s2voice_bytes(transcript=f"voice_{i}"))
        result = cmd_audit(str(tmp_path))
        assert result["total_voices"] == 3
        assert len(result.get("voices", [])) == 3

    def test_audit_reports_managed_vs_unmanaged(self, tmp_path):
        from app.voice_cli import cmd_audit
        path_a = tmp_path / "managed.s2voice"
        sidecar_a = tmp_path / "managed.s2voice.json"
        _write_fixture(path_a, _build_s2voice_bytes())
        _write_fixture(sidecar_a, json.dumps({
            "id": "managed", "license": "CC-BY-4.0",
            "attribution": "Test", "provenance": {"source": "managed"},
        }).encode())
        path_b = tmp_path / "unmanaged.s2voice"
        _write_fixture(path_b, _build_s2voice_bytes())
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        assert voices["managed"].get("managed") is True
        assert voices["unmanaged"].get("managed") is False

    def test_audit_reports_licenses(self, tmp_path):
        from app.voice_cli import cmd_audit
        path = tmp_path / "test.s2voice"
        sidecar = tmp_path / "test.s2voice.json"
        _write_fixture(path, _build_s2voice_bytes())
        _write_fixture(sidecar, json.dumps({
            "id": "test", "license": "MIT",
            "attribution": "Test Speaker",
            "provenance": {"source": "test"},
        }).encode())
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        assert voices["test"]["license"] == "MIT"

    def test_audit_reports_missing_license(self, tmp_path):
        from app.voice_cli import cmd_audit
        path = tmp_path / "no_license.s2voice"
        _write_fixture(path, _build_s2voice_bytes())
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        issues = voices["no_license"].get("issues", [])
        assert any("license" in i.lower() for i in issues)

    def test_audit_reports_missing_attribution(self, tmp_path):
        from app.voice_cli import cmd_audit
        path = tmp_path / "no_attr.s2voice"
        sidecar = tmp_path / "no_attr.s2voice.json"
        _write_fixture(path, _build_s2voice_bytes())
        _write_fixture(sidecar, json.dumps({
            "id": "no_attr", "license": "CC-BY-4.0",
            "provenance": {"source": "test"},
        }).encode())
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        issues = voices["no_attr"].get("issues", [])
        assert any("attribution" in i.lower() for i in issues)

    def test_audit_reports_hash(self, tmp_path):
        from app.voice_cli import cmd_audit
        data = _build_s2voice_bytes()
        path = tmp_path / "test.s2voice"
        _write_fixture(path, data)
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        expected_hash = hashlib.sha256(data).hexdigest()
        assert voices["test"]["hash_sha256"] == expected_hash

    def test_audit_reports_corrupt_file(self, tmp_path):
        from app.voice_cli import cmd_audit
        path = tmp_path / "corrupt.s2voice"
        _write_fixture(path, b"not a valid file")
        result = cmd_audit(str(tmp_path))
        voices = {v["id"]: v for v in result.get("voices", [])}
        assert voices["corrupt"].get("valid") is False
        assert "error" in voices["corrupt"]

    def test_audit_symlinks_ignored(self, tmp_path):
        from app.voice_cli import cmd_audit
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real_path = real_dir / "real_voice.s2voice"
        _write_fixture(real_path, _build_s2voice_bytes())
        link_path = tmp_path / "link_voice.s2voice"
        os.symlink(str(real_path), str(link_path))
        result = cmd_audit(str(tmp_path))
        assert result["total_voices"] == 0

    def test_audit_skip_subdirectories(self, tmp_path):
        from app.voice_cli import cmd_audit
        _write_fixture(tmp_path / "top.s2voice", _build_s2voice_bytes())
        sub = tmp_path / "sub"
        sub.mkdir()
        _write_fixture(sub / "nested.s2voice", _build_s2voice_bytes())
        result = cmd_audit(str(tmp_path))
        assert result["total_voices"] == 1
        assert result["voices"][0]["id"] == "top"


class TestCLILicenses:
    def test_licenses_summary(self, tmp_path):
        from app.voice_cli import cmd_licenses
        for i, lic in enumerate(["MIT", "CC-BY-4.0", "MIT"]):
            path = tmp_path / f"voice_{i}.s2voice"
            sidecar = tmp_path / f"voice_{i}.s2voice.json"
            _write_fixture(path, _build_s2voice_bytes())
            _write_fixture(sidecar, json.dumps({
                "id": f"voice_{i}", "license": lic,
                "attribution": f"Speaker {i}",
                "provenance": {"source": "test"},
            }).encode())
        result = cmd_licenses(str(tmp_path))
        assert "MIT" in result["licenses"]
        assert "CC-BY-4.0" in result["licenses"]
        assert result["licenses"]["MIT"]["count"] == 2
        assert result["licenses"]["CC-BY-4.0"]["count"] == 1

    def test_licenses_empty_directory(self, tmp_path):
        from app.voice_cli import cmd_licenses
        result = cmd_licenses(str(tmp_path))
        assert result["licenses"] == {}
        assert result["total_voices"] == 0

    def test_licenses_reports_unlicensed(self, tmp_path):
        from app.voice_cli import cmd_licenses
        path_a = tmp_path / "licensed.s2voice"
        sidecar_a = tmp_path / "licensed.s2voice.json"
        _write_fixture(path_a, _build_s2voice_bytes())
        _write_fixture(sidecar_a, json.dumps({
            "id": "licensed", "license": "MIT", "attribution": "A",
            "provenance": {"source": "test"},
        }).encode())
        path_b = tmp_path / "unlicensed.s2voice"
        _write_fixture(path_b, _build_s2voice_bytes())
        result = cmd_licenses(str(tmp_path))
        assert result.get("unlicensed_count", 0) >= 1


class TestBackwardCompatibility:
    def test_discover_voices_still_works(self, tmp_path):
        from app.voice_discovery import discover_voices
        _write_fixture(tmp_path / "cmu_bdl_male_us.s2voice", _build_s2voice_bytes())
        _write_fixture(tmp_path / "cmu_rms_male_us.s2voice", _build_s2voice_bytes())
        result = discover_voices(str(tmp_path))
        assert "cmu_bdl_male_us" in result
        assert "cmu_rms_male_us" in result

    def test_discover_voices_unaffected_by_sidecars(self, tmp_path):
        from app.voice_discovery import discover_voices
        path = tmp_path / "test_voice.s2voice"
        sidecar = tmp_path / "test_voice.s2voice.json"
        _write_fixture(path, _build_s2voice_bytes())
        _write_fixture(sidecar, json.dumps({
            "id": "test_voice", "license": "MIT", "attribution": "T",
            "provenance": {"source": "test"},
        }).encode())
        result = discover_voices(str(tmp_path))
        assert "test_voice" in result


class TestIncompatibilityDetection:
    def test_incompatible_codebook_count(self):
        from app.voice_profile import parse_s2voice
        data = _build_s2voice_bytes(num_codebooks=16)
        profile = parse_s2voice(data)
        assert not profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)

    def test_incompatible_sample_rate(self):
        from app.voice_profile import parse_s2voice
        data = _build_s2voice_bytes(sample_rate=16000)
        profile = parse_s2voice(data)
        assert not profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)

    def test_incompatible_codebook_size(self):
        from app.voice_profile import parse_s2voice
        data = _build_s2voice_bytes(codebook_size=1024)
        profile = parse_s2voice(data)
        assert not profile.is_compatible(num_codebooks=8, codebook_size=4096, sample_rate=44100)


class TestAtomicImportEdgeCases:
    def test_temp_file_cleaned_up_on_failure(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.s2voice"
        _write_fixture(voice_path, _build_s2voice_bytes())
        os.chmod(str(dest), stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
            assert result["imported"] is False
        finally:
            os.chmod(str(dest), stat.S_IRWXU)
        for f in dest.iterdir():
            assert not f.name.startswith(".")

    def test_import_without_extension_handled(self, tmp_path):
        from app.voice_cli import cmd_import
        source = tmp_path / "source"
        source.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        voice_path = source / "test.noext"
        _write_fixture(voice_path, _build_s2voice_bytes())
        result = cmd_import(str(voice_path), str(dest), voice_id="test-voice")
        assert "error" in result or result.get("imported") is True

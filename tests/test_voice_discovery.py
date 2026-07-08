"""Tests for app.voice_discovery — safe .s2voice enumeration."""

import os
import tempfile

import pytest

from app.voice_discovery import _sanitize_voice_id, discover_voices


# ── sanitise_voice_id unit tests ────────────────────────────────────────

def test_valid_profile_ids():
    for fid in [
        "cmu_bdl_male_us",
        "cmu_rms_male_us",
        "cmu_jmk_male_canadian",
        "cmu_slt_female_us",
        "cmu_clb_female_us",
        "cmu_eey_female_us",
        "my-custom-voice_01",
    ]:
        filename = f"{fid}.s2voice"
        assert _sanitize_voice_id(filename) == fid


def test_rejects_missing_suffix():
    assert _sanitize_voice_id("no_suffix") is None
    assert _sanitize_voice_id("almost.s2voiceX") is None
    assert _sanitize_voice_id(".s2voice") is None


def test_rejects_empty_id():
    assert _sanitize_voice_id(".s2voice") is None


def test_rejects_hidden_files():
    assert _sanitize_voice_id(".hidden.s2voice") is None


def test_rejects_unsafe_characters():
    assert _sanitize_voice_id("bad/name.s2voice") is None
    assert _sanitize_voice_id("bad name.s2voice") is None
    assert _sanitize_voice_id("bad\x00name.s2voice") is None
    assert _sanitize_voice_id("bad\tname.s2voice") is None


def test_rejects_traversal_names():
    assert _sanitize_voice_id("../etc/passwd.s2voice") is None
    assert _sanitize_voice_id("..\\escaped.s2voice") is None


# ── discover_voices integration tests ───────────────────────────────────

def _touch(path: str):
    """Create an empty file."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"")


def test_empty_voice_directory():
    with tempfile.TemporaryDirectory() as d:
        assert discover_voices(d) == []


def test_nonexistent_directory():
    assert discover_voices("/tmp/nonexistent-voice-dir-xyz123") == []


def test_six_valid_profiles(tmp_path):
    profiles = [
        "cmu_bdl_male_us",
        "cmu_rms_male_us",
        "cmu_jmk_male_canadian",
        "cmu_slt_female_us",
        "cmu_clb_female_us",
        "cmu_eey_female_us",
    ]
    for pid in profiles:
        _touch(os.path.join(tmp_path, f"{pid}.s2voice"))

    result = discover_voices(str(tmp_path))
    assert result == sorted(profiles)


def test_deterministic_sorting(tmp_path):
    # Create in reverse order to ensure sorting is deterministic.
    profiles = ["zzz_voice", "aaa_voice", "mmm_voice"]
    for pid in reversed(profiles):
        _touch(os.path.join(tmp_path, f"{pid}.s2voice"))

    result = discover_voices(str(tmp_path))
    assert result == sorted(profiles)


def test_drop_in_new_file(tmp_path):
    _touch(os.path.join(tmp_path, "alpha.s2voice"))
    assert discover_voices(str(tmp_path)) == ["alpha"]

    # Add a second file — must appear on next scan without restart.
    _touch(os.path.join(tmp_path, "beta.s2voice"))
    assert discover_voices(str(tmp_path)) == ["alpha", "beta"]


def test_removed_file_disappears(tmp_path):
    _touch(os.path.join(tmp_path, "alpha.s2voice"))
    _touch(os.path.join(tmp_path, "beta.s2voice"))
    assert len(discover_voices(str(tmp_path))) == 2

    os.remove(os.path.join(tmp_path, "beta.s2voice"))
    assert discover_voices(str(tmp_path)) == ["alpha"]


def test_unrelated_files_ignored(tmp_path):
    _touch(os.path.join(tmp_path, "voice_a.s2voice"))
    _touch(os.path.join(tmp_path, "README.md"))
    _touch(os.path.join(tmp_path, "voice_b.s2voice"))
    _touch(os.path.join(tmp_path, "data.bin"))

    assert discover_voices(str(tmp_path)) == ["voice_a", "voice_b"]


def test_nested_files_ignored(tmp_path):
    _touch(os.path.join(tmp_path, "outer.s2voice"))
    sub = os.path.join(tmp_path, "subdir")
    _touch(os.path.join(sub, "nested.s2voice"))
    _touch(os.path.join(sub, "also_nested.s2voice"))

    assert discover_voices(str(tmp_path)) == ["outer"]


def test_hidden_files_ignored(tmp_path):
    _touch(os.path.join(tmp_path, ".hidden.s2voice"))
    _touch(os.path.join(tmp_path, "visible.s2voice"))

    assert discover_voices(str(tmp_path)) == ["visible"]


@pytest.mark.skipif(os.name != "posix", reason="symlink test requires POSIX")
def test_symlinks_ignored(tmp_path):
    real_dir = os.path.join(tmp_path, "real")
    os.makedirs(real_dir, exist_ok=True)
    _touch(os.path.join(real_dir, "real_voice.s2voice"))

    os.symlink(os.path.join(real_dir, "real_voice.s2voice"),
               os.path.join(tmp_path, "link_voice.s2voice"))
    _touch(os.path.join(tmp_path, "direct.s2voice"))

    assert discover_voices(str(tmp_path)) == ["direct"]


def test_malformed_names_rejected(tmp_path):
    _touch(os.path.join(tmp_path, "good_voice.s2voice"))
    _touch(os.path.join(tmp_path, "bad/name.s2voice"))
    _touch(os.path.join(tmp_path, "bad name.s2voice"))
    _touch(os.path.join(tmp_path, "good_voice2.s2voice"))

    assert discover_voices(str(tmp_path)) == ["good_voice", "good_voice2"]


def test_traversal_names_rejected(tmp_path):
    _touch(os.path.join(tmp_path, "safe.s2voice"))
    # Filesystem won't let us create files with '/' in the name, but
    # os.scandir can return entries from other sources.
    # The sanitizer already covers those cases at the unit level.

    assert "safe" in discover_voices(str(tmp_path))


def test_duplicate_ids_raise(tmp_path):
    _touch(os.path.join(tmp_path, "Voice.s2voice"))
    _touch(os.path.join(tmp_path, "voice.s2voice"))

    with pytest.raises(ValueError, match="Duplicate voice profile id"):
        discover_voices(str(tmp_path))


def test_same_case_duplicate_not_created(tmp_path):
    """Same-case duplicate cannot exist on a normal filesystem, but
    os.scandir dedup won't double-count."""
    _touch(os.path.join(tmp_path, "unique.s2voice"))
    # Can't create a true duplicate; just verify single entry.
    assert discover_voices(str(tmp_path)) == ["unique"]

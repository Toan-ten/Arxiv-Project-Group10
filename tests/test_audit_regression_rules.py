"""Regression tests for AUD-002/AUD-003; pure offline, no live API or Drive."""
import hashlib
import json
from pathlib import Path

import pytest

from src.withdrawal_evidence import confirmed_withdrawal
from src.archive_validation import validate_archive_sidecars


def official(arxiv_id="2501.02724", version="v2"):
    return {
        "arxiv_id": arxiv_id,
        "versioned_id": f"{arxiv_id}{version}",
        "version_ids": [f"{arxiv_id}v1", f"{arxiv_id}v2"],
        "official_url": f"https://arxiv.org/abs/{arxiv_id}{version}",
        "is_withdrawn": True,
        "withdrawal_reason": "Withdrawn by the author due to insufficient proof",
    }


def test_positive_exact_official_version():
    assert confirmed_withdrawal(official(), "2501.02724", "v2") is True


@pytest.mark.parametrize("difference", [
    {"versioned_id": "2501.02724v1"},
    {"official_url": "https://arxiv.org/abs/2501.02724v1"},
    {"official_url": "https://arxiv.org.evil.test/abs/2501.02724v2"},
    {"official_url": "https://arxiv.org:bad/abs/2501.02724v2"},
    {"official_url": "https://arxiv.org/abs/2501.02724v2?redirect=1"},
    {"arxiv_id": "2501.02725"},
    {"version_ids": ["2501.02724v1"]},
])
def test_wrong_or_ambiguous_evidence(difference):
    assert not confirmed_withdrawal({**official(), **difference}, "2501.02724", "v2")


def test_paper_wide_withdrawn_keyword_is_not_version_proof():
    meta = {"title": "How to withdraw funds", "abstract": "Withdrawn by the author", "is_withdrawn": True}
    assert not confirmed_withdrawal(meta, "2501.02724", "v2")


def test_official_url_and_flag_without_explicit_statement_are_insufficient():
    meta = official()
    meta["withdrawal_reason"] = ""
    meta["abstract"] = "How to withdraw funds safely"
    assert not confirmed_withdrawal(meta, "2501.02724", "v2")


def test_explicit_official_statement_without_flag():
    meta = official()
    meta["is_withdrawn"] = False
    assert confirmed_withdrawal(meta, "2501.02724", "v2")


def _archive(tmp_path):
    archive = tmp_path / "cohort_0151_corpus.tar.gz"
    archive.write_bytes(b"test archive fixture bytes")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sha = archive.with_name(archive.name + ".sha256")
    sha.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    manifest = tmp_path / "cohort_0151_corpus.manifest.json"
    manifest.write_text(json.dumps({"cohort_number": 151, "archive_sha256": digest,
                                   "archive_filename": archive.name}), encoding="utf-8")
    return archive, sha, manifest


def test_valid_sidecars(tmp_path):
    archive, _, _ = _archive(tmp_path)
    assert validate_archive_sidecars(archive, 151)["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_original_checksum_corruption_must_block_even_if_old_backup_exists(tmp_path):
    archive, sha, _ = _archive(tmp_path)
    sha.write_text("0" * 64 + "  " + archive.name + "\n")
    with pytest.raises(ValueError, match="checksum"):
        validate_archive_sidecars(archive, 151)


def test_missing_manifest_fail_closed(tmp_path):
    archive, _, manifest = _archive(tmp_path)
    manifest.unlink()
    with pytest.raises(ValueError, match="Missing"):
        validate_archive_sidecars(archive, 151)


def test_manifest_hash_corruption_fail_closed(tmp_path):
    archive, _, manifest = _archive(tmp_path)
    meta = json.loads(manifest.read_text())
    meta["archive_sha256"] = "0" * 64
    manifest.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="manifest|archive_sha256"):
        validate_archive_sidecars(archive, 151)

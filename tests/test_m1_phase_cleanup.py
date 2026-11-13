"""Failure-path characterization for phase extraction and old checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from m1_refactor_fixture import baseline_module
from test_m1_phases import _metadata

from src import m1_phases, semantic_scholar


@pytest.mark.parametrize("implementation", ["baseline", "current"])
def test_metadata_exception_restores_environment_and_releases_writer_lock(tmp_path, implementation):
    phases = baseline_module() if implementation == "baseline" else m1_phases
    drive = tmp_path / "drive"
    with (
        patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "previous-key", "S2_API_KEY": "previous-alias"}),
        patch.object(semantic_scholar, "fetch_metadata", side_effect=RuntimeError("fixture failure")),
    ):
        with pytest.raises(RuntimeError, match="fixture failure"):
            phases.run_m1_s2_member(
                "24120260", drive_root=drive, local_root=tmp_path / "local",
                semantic_scholar_api_key="offline-fixture-key", cohort_limit=1,
            )
        assert os.environ["SEMANTIC_SCHOLAR_API_KEY"] == "previous-key"
        assert os.environ["S2_API_KEY"] == "previous-alias"
    assert not list((drive / "reports" / "phase_locks").glob("*.lock"))
    envelopes = list((drive / "reports" / "session_envelopes").glob("*.json"))
    assert len(envelopes) == 1
    assert json.loads(envelopes[0].read_text())["status"] == "finished"


def test_failed_atomic_replacement_preserves_old_evidence_and_checksum(tmp_path):
    baseline = baseline_module()
    drive = tmp_path / "drive"
    settings = dict(drive_root=drive, local_root=tmp_path / "local",
                    semantic_scholar_api_key="offline-fixture-key", cohort_limit=1)

    def limited(ids, **_kwargs):
        return [{"arxiv_id": identifier, "status": "rate_limited", "http_status": 429,
                 "attempts": 1, "paper": None, "error": "offline rate limit"}
                for identifier in ids]

    with patch.object(semantic_scholar, "fetch_metadata", side_effect=limited):
        result = baseline.run_m1_s2_member("24120260", **settings)
    checkpoint = Path(result["checkpoint_root"]) / "cohort_0151" / "checkpoint.json"
    saved = json.loads(checkpoint.read_text())
    relative = next(iter(saved["files"]))
    evidence = checkpoint.parent / "papers" / relative
    original = evidence.read_bytes()
    replace = os.replace

    def fail_evidence_replace(source, destination):
        if Path(destination) == evidence:
            raise OSError("fixture atomic replacement failed")
        return replace(source, destination)

    with (
        patch.object(semantic_scholar, "fetch_metadata", side_effect=lambda ids, **_kw: [
            _metadata(identifier, found=False) for identifier in ids]),
        patch.object(os, "replace", side_effect=fail_evidence_replace),
    ):
        with pytest.raises(OSError, match="fixture atomic replacement failed"):
            m1_phases.run_m1_s2_member("24120260", **settings)
    assert evidence.read_bytes() == original
    resumed = json.loads(checkpoint.read_text())
    assert resumed["files"][relative] == hashlib.sha256(original).hexdigest()
    assert resumed["papers"] == saved["papers"]
    assert not list(evidence.parent.glob("*.tmp"))
    assert not list((drive / "reports" / "phase_locks").glob("*.lock"))

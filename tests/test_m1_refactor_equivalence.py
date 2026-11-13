"""Differential regression tests against the immutable M1 baseline commit."""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from m1_refactor_fixture import baseline_module

from src import m1_phases, m1_workflow

S2_CASES = (
    "test_s2_before_arxiv_stops_on_429_then_resumes_without_arxiv_or_duplicate_ids",
    "test_bounded_auto_resume_after_references_429_uses_verified_checkpoint",
    "test_auto_resume_does_not_retry_http_403",
    "test_auto_resume_limit_stops_after_one_retry_and_keeps_checkpoint",
    "test_auto_resume_uses_retry_after_and_stops_when_session_budget_is_insufficient",
    "test_batch_length_mismatch_fails_before_any_metadata_evidence_write",
    "test_invalid_batch_is_split_and_each_id_is_checkpointed",
    "test_checkpoint_checksum_mismatch_fails_closed_and_preserves_file",
    "test_partial_reference_page_resumes_from_saved_continuation",
)
AUDIT_CASES = (
    "test_arxiv_phase_never_calls_s2_and_never_claims_m1",
    "test_arxiv_stops_without_discovery_when_required_kaggle_snapshot_is_unavailable",
    "test_audit_blocks_export_when_source_or_s2_evidence_is_missing",
    "test_audit_blocks_export_when_kaggle_discrepancy_checksum_is_invalid",
    "test_successful_audit_reconciles_s2_without_changing_source_bytes",
)
CASES = ["TestIndependentS2Phase." + name for name in S2_CASES]
CASES += ["TestArxivAndAuditGates." + name for name in AUDIT_CASES]


def test_existing_api_names_and_signatures_are_preserved():
    baseline = baseline_module()
    for name, value in vars(baseline).items():
        if inspect.isfunction(value) and value.__module__ == baseline.__name__:
            current = getattr(m1_phases, name)
            assert inspect.signature(current) == inspect.signature(value), name
    for name in (
        "_required_m1_secret", "build_member_selection", "run_m1_member",
        "run_m1_arxiv_member", "run_m1_s2_member", "run_m1_audit_member",
    ):
        assert getattr(m1_workflow, name) is getattr(m1_phases, name)
    for name in ("PHASE_SCHEMA_VERSION", "S2_DEFAULTS", "S2_METADATA_BATCH_SIZE"):
        assert getattr(m1_phases, name) == getattr(baseline, name)
    assert issubclass(m1_phases.M1PhaseError, RuntimeError)


@pytest.mark.parametrize("case", CASES, ids=lambda name: name.split(".")[-1])
def test_exact_artifacts_match_baseline_in_fresh_processes(tmp_path, case):
    helper = Path(__file__).with_name("m1_refactor_fixture.py")
    outputs = []
    implementations = ["baseline", "current"]
    if case.endswith((
        "test_s2_before_arxiv_stops_on_429_then_resumes_without_arxiv_or_duplicate_ids",
        "test_checkpoint_checksum_mismatch_fails_closed_and_preserves_file",
        "test_partial_reference_page_resumes_from_saved_continuation",
        "test_successful_audit_reconciles_s2_without_changing_source_bytes",
    )):
        implementations.append("mixed")
    for implementation in implementations:
        output = tmp_path / (implementation + ".json")
        subprocess.run(
            [sys.executable, str(helper), implementation, case,
             str(tmp_path / "workspace"), str(output)],
            cwd=helper.parents[1], check=True, capture_output=True, text=True,
            timeout=60,
        )
        outputs.append(json.loads(output.read_text(encoding="utf-8")))
    assert outputs[0]["artifacts"], "The characterization must compare real files"
    for output in outputs[1:]:
        assert outputs[0] == output

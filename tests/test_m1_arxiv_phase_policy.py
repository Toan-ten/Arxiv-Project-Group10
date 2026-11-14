"""Offline characterization of the source phase's bounded 429 resume loop."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from m1_refactor_fixture import baseline_module

from src import arxiv_client, batch, benchmark_runner, m1_phases, semantic_scholar
from src.fast_production import FastProductionRunner


@pytest.mark.parametrize("implementation", ["baseline", "current"])
@pytest.mark.parametrize(
    "status,durable,budget,limit,expected_calls,deferred",
    [
        (429, True, 3600, 1, 2, None),
        (429, False, 3600, 1, 1, "checkpoint_missing_or_invalid"),
        (429, True, 60, 1, 1, "session_budget"),
        (429, True, 3600, 0, 1, None),
        (403, True, 3600, 1, 1, None),
    ],
)
def test_arxiv_policy_preserves_stop_and_resume_boundaries(
    tmp_path, implementation, status, durable, budget, limit, expected_calls, deferred,
):
    phases = baseline_module() if implementation == "baseline" else m1_phases
    member = "24120260"
    selection = phases.build_member_selection(member, 1)
    identifier = selection["pairs"][0][1]
    drive, local = tmp_path / "drive" / member, tmp_path / "local" / member
    runner = FastProductionRunner(local_root=local, drive_root=drive,
                                  scratch_root=tmp_path / "scratch")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(b"offline Kaggle snapshot")
    snapshot_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    index = drive / "kaggle_indices" / "fixture.json"
    index.parent.mkdir()
    index.write_text(json.dumps({"snapshot_sha256": snapshot_sha}), encoding="utf-8")
    reason = {"type": "http_policy", "stage": "discovery", "http_status": status,
              "arxiv_id": identifier}
    calls = []

    def run_cohort(*args, **kwargs):
        calls.append((args, kwargs))
        stopped = len(calls) == 1
        return {"fast_runner": {"stopped": stopped,
                                "stop_reason": reason if stopped else None,
                                "batch_id": "fixture-batch"}}

    history = [{"stop_reason": reason, "papers": [{
        "mssv": member, "arxiv_id": identifier, "state": "processed",
        "result": {"discovery": {"action": "queried", "http_status": status}},
    }]}] if durable else []
    with ExitStack() as stack:
        factory = stack.enter_context(patch.object(phases, "FastProductionRunner", return_value=runner))
        stack.enter_context(patch.object(phases, "_mark_archive_arxiv_only"))
        stack.enter_context(patch.object(runner, "run_cohort", side_effect=run_cohort))
        stack.enter_context(patch.object(runner, "count_source_completed_papers",
                                         side_effect=lambda _n: 100 if len(calls) > 1 else 0))
        stack.enter_context(patch.object(runner.store, "read_manifest", return_value={
            "discovery_status": "rate_limited"}))
        stack.enter_context(patch.object(runner.store, "read_metadata", return_value={
            "provenance": {"arxiv": {"snapshot": {
                "mssv": member, "arxiv_id": identifier, "status": "rate_limited",
                "http_status": 429,
            }}}}))
        stack.enter_context(patch.object(benchmark_runner, "_load", return_value=({}, history)))
        stack.enter_context(patch("src.archive_validation.validate_archive_sidecars"))
        stack.enter_context(patch("src.kaggle_candidates.prepare_snapshot", return_value={
            "status": "ready", "snapshot_path": str(snapshot), "snapshot_sha256": snapshot_sha,
        }))
        stack.enter_context(patch("src.kaggle_candidates.reuse_or_build_index", return_value={
            "index_path": str(index), "reused": True, "index": {"candidates": {}},
        }))
        stack.enter_context(patch.object(batch, "check_arxiv_cooldown", side_effect=
                                         arxiv_client.ArxivCooldownError(
                                             "2999-01-01T00:00:00+00:00")))
        stack.enter_context(patch.object(phases.random, "uniform", return_value=300.0))
        # A recorded provider deadline must dominate the local random delay.
        stack.enter_context(patch.object(phases.time, "time", return_value=32472143550.0))
        sleep = stack.enter_context(patch.object(phases.time, "sleep"))
        s2 = stack.enter_context(patch.object(semantic_scholar, "fetch_metadata"))
        result = phases.run_m1_arxiv_member(
            member, drive_root=drive, local_root=local, cohort_limit=1,
            kaggle_token="offline-kaggle-token", auto_resume_429=True,
            auto_resume_429_max_per_cohort=limit, budget_seconds=budget,
        )
    factory.assert_called_once()
    s2.assert_not_called()
    assert len(calls) == expected_calls
    assert result["m1_complete"] is False
    assert not list((drive / "reports" / "phase_locks").glob("*.lock"))
    if expected_calls == 2:
        assert result["stopped"] is False
        assert result["auto_resume_429"]["resume_attempts"] == 1
        assert sleep.call_args.args == (450.0,)
        events = result["cohorts"][str(selection["cohorts"][0])]["auto_resume_429"]["events"]
        assert events[0]["outcome"] == "resumed"
    else:
        assert result["stopped"] is True
        assert result["stop_reason"] == reason
        sleep.assert_not_called()
        if deferred:
            events = result["cohorts"][str(selection["cohorts"][0])]["auto_resume_429"]["events"]
            assert events[-1]["deferred_reason"] == deferred

"""Kiểm thử ngoại tuyến cho bộ tổng hợp benchmark nhiều thành viên."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "group_benchmark_aggregator", ROOT / "scripts/aggregate_m1_group_benchmark.py"
)
aggregator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregator)


def _summary(member: str, *, run_scope: str = "member_full_assignment") -> dict:
    return {
        "mssv": member,
        "run_scope": run_scope,
        "cohorts": list(aggregator.member_cohort_range(member)),
        "expected_id_count": 5000,
        "session_ids": [f"session-{member}"],
        "campaign_id": f"campaign-{member}",
        "data_signature": "signature",
        "end_to_end_complete": True,
    }


def test_load_member_summaries_accepts_unified_full_assignment(tmp_path: Path) -> None:
    for member in aggregator.MEMBERS:
        folder = tmp_path / member / "reports"
        folder.mkdir(parents=True)
        (folder / "benchmark_observed_session.json").write_text(
            json.dumps(_summary(member)), encoding="utf-8"
        )

    summaries, errors = aggregator.load_member_summaries(tmp_path)

    assert set(summaries) == set(aggregator.MEMBERS)
    assert errors == []


def test_load_member_summaries_ignores_sample_runs(tmp_path: Path) -> None:
    member = aggregator.MEMBERS[0]
    folder = tmp_path / member / "reports"
    folder.mkdir(parents=True)
    (folder / "benchmark_observed_sample.json").write_text(
        json.dumps(_summary(member, run_scope="member_subset_same_pipeline")),
        encoding="utf-8",
    )

    summaries, errors = aggregator.load_member_summaries(tmp_path)

    assert member not in summaries
    assert errors == []


def test_load_session_envelopes_reads_member_reports(tmp_path: Path) -> None:
    session_id = "session-24120010"
    envelope_root = tmp_path / aggregator.MEMBERS[0] / "reports" / "session_envelopes"
    envelope_root.mkdir(parents=True)
    expected = {"session_id": session_id, "status": "finished"}
    (envelope_root / f"{session_id}.json").write_text(
        json.dumps(expected), encoding="utf-8"
    )

    rows, errors = aggregator.load_session_envelopes(tmp_path, {session_id})

    assert rows == [expected]
    assert errors == []


def test_load_session_envelopes_reports_duplicate_locations(tmp_path: Path) -> None:
    session_id = "session-duplicate"
    for member in aggregator.MEMBERS[:2]:
        envelope_root = tmp_path / member / "reports" / "session_envelopes"
        envelope_root.mkdir(parents=True)
        (envelope_root / f"{session_id}.json").write_text(
            json.dumps({"session_id": session_id, "status": "finished"}),
            encoding="utf-8",
        )

    rows, errors = aggregator.load_session_envelopes(tmp_path, {session_id})

    assert rows == [{}]
    assert errors == [f"duplicate_session_envelope:{session_id}"]


def test_group_paper_and_discovery_metrics_require_all_members(tmp_path, monkeypatch):
    summaries = {}
    audits = []
    for index, member in enumerate(aggregator.MEMBERS, start=1):
        value = _summary(member)
        value.update(discovery_arm="arxiv_only", selection_fingerprint="group", data_signature="data",
                     environment_fingerprint="env", starting_state_fingerprint="empty", measurement_protocol="v1",
                     runtime_commit="commit",
                     paper_timings={"status": "complete", "papers": [{"arxiv_id": member, "total_seconds": index}]},
                     discovery_timings={"status": "complete", "wall_seconds": index},
                     logical_size_metrics={"logical_size_measured_papers": 1,
                                           "average_logical_before_bytes_per_paper": 100 * index,
                                           "average_logical_after_bytes_per_paper": 80 * index,
                                           "logical_size_scope": "regular files before/after filtering"},
                     reference_metadata_success={"complete_records": 1,
                                                 "eligible_records": 2,
                                                 "success_rate_percent": 50.0,
                                                 "definition": "nonempty metadata fields"})
        summaries[member] = value
        audits.append({"stage": "audit", "cohort": 0, "configuration": {"mssv": member},
                       "execution_status": "returned", "result": {"is_reconciled": True}})
    monkeypatch.setattr(aggregator, "load_member_summaries", lambda _: (summaries, []))
    monkeypatch.setattr(aggregator, "load_observations", lambda *_: (audits, []))
    monkeypatch.setattr(aggregator, "load_session_envelopes", lambda *_: ([], []))
    monkeypatch.setattr(aggregator, "aggregate_group_observations", lambda *a, **k: {
        "status": "measured_intervals", "missing_stages": [], "expected_id_count": 20000,
        "end_to_end_wall_seconds": 20.0,
    })
    result = aggregator.aggregate(tmp_path)
    assert result["average_paper_seconds"] == 2.5
    assert result["discovery_wall_seconds"] == 10
    assert result["logical_size_metrics"]["average_logical_before_bytes_per_paper"] == 250
    assert result["logical_size_metrics"]["average_logical_after_bytes_per_paper"] == 200
    assert result["reference_metadata_success"]["success_rate_percent"] == 50.0
    summaries[aggregator.MEMBERS[0]]["paper_timings"]["status"] = "incomplete"
    result = aggregator.aggregate(tmp_path)
    assert result["average_paper_seconds"] is None
    assert not result["end_to_end_complete"]

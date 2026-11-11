"""Offline checks for scoped and durable execution evidence."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.session_evidence import (SessionBudget, load_campaign_state, observe_stage,
                                  fingerprint_complete_corpus, revision_history_counts,
                                  save_campaign_state)


def make_runner(tmp_path):
    runner = SimpleNamespace(corpus_dir=tmp_path / 'corpus', scratch_dir=tmp_path / 'scratch',
                             reports_dir=tmp_path / 'reports')
    runner.corpus_dir.mkdir()
    runner.scratch_dir.mkdir()
    return runner


def test_observation_persists_success_and_excludes_paper_payload(tmp_path):
    runner = make_runner(tmp_path)
    result = {'stopped': True, 'stop_reason': {'http_status': 429}, 'papers': ['private']}
    assert observe_stage(runner, 's2', 151, lambda: result, session_id='test') == result
    record = json.loads(next((runner.reports_dir / 'stage_observations').glob('*.json')).read_text())
    assert record['execution_status'] == 'returned'
    assert record['result']['stop_reason']['http_status'] == 429
    assert 'papers' not in record['result']
    assert record['wall_seconds'] >= 0
    assert record['resources']['ram']['sample_count'] >= 1
    assert record['completeness'].startswith('partial')


def test_observation_persists_exception_without_exception_message(tmp_path):
    runner = make_runner(tmp_path)
    def fail():
        raise ValueError('secret should not be persisted')
    with pytest.raises(ValueError):
        observe_stage(runner, 'source', 151, fail, session_id='test')
    data = next((runner.reports_dir / 'stage_observations').glob('*.json')).read_text()
    record = json.loads(data)
    assert record['execution_status'] == 'raised'
    assert record['error_type'] == 'ValueError'
    assert 'secret' not in data


def test_revision_history_reports_unverified_and_invalid():
    pairs = [('1', 'one'), ('1', 'two'), ('1', 'three')]
    store = SimpleNamespace(read_manifest=lambda m, a: {'discovery_status': 'found'},
                            read_metadata=lambda m, a: {'id': a})
    with patch('src.fast_production.cohort_pairs', return_value=pairs), \
         patch('src.arxiv_history._cached', side_effect=[True, False, ValueError()]), \
         patch('src.arxiv_history._single_version_cached', return_value=False):
        assert revision_history_counts(SimpleNamespace(store=store), 151) == {
            'verified': 1, 'missing_or_unverified': 1, 'invalid': 1}


def test_summarize_rejects_overlap_missing_and_duplicates():
    from src.session_evidence import summarize_observations
    row = dict(observation_id='one', started_at='2026-01-01T00:00:00+00:00',
               finished_at='2026-01-01T00:00:10+00:00', wall_seconds=8.0,
               execution_status='returned')
    second = dict(row, observation_id='two', started_at='2026-01-01T00:00:10+00:00',
                  finished_at='2026-01-01T00:00:20+00:00')
    assert summarize_observations([row, second])['measured_wall_seconds'] == 16
    assert not summarize_observations([row, second])['end_to_end_complete']
    assert summarize_observations([row, row])['measured_wall_seconds'] is None
    assert summarize_observations([row, dict(second, started_at='2026-01-01T00:00:09+00:00')])['status'] == 'incomplete'
    assert summarize_observations([dict(row, finished_at=None)])['status'] == 'incomplete'


def test_workflow_summary_refuses_missing_stages_and_compares_matching_scope():
    from src.session_evidence import summarize_workflow, compare_workflows
    resource={'ram': {'status':'collected','sample_count':2,'sum_sample_bytes':30,'sampled_peak_bytes':20},
              'disk': {'status':'collected','sampled_peak_bytes':50}}
    row={'observation_id':'a','stage':'source','cohort':151,
         'started_at':'2026-01-01T00:00:00+00:00','finished_at':'2026-01-01T00:00:02+00:00',
         'wall_seconds':2.0,'execution_status':'returned','resources':resource,'result':{}}
    incomplete=summarize_workflow([row],[('source',151),('s2',151)])
    assert incomplete['active_seconds'] is None and incomplete['missing_stages']==[['s2',151]]
    complete=summarize_workflow([row],[('source',151)])
    assert complete['active_seconds']==2 and complete['sampled_average_ram_bytes']==15
    assert complete['end_to_end_complete'] is False
    keys={'selection_fingerprint':'x','data_signature':'y','environment_fingerprint':'z',
          'starting_state_fingerprint':'w','measurement_protocol':'v1'}
    eligible_summary = {**complete, **keys, 'end_to_end_complete': True, 'end_to_end_wall_seconds': 8}
    assert compare_workflows(eligible_summary, eligible_summary)['delta_seconds'] == 0
    assert not compare_workflows(eligible_summary, {**eligible_summary, 'data_signature':'other'})['eligible']
    assert not compare_workflows({**complete, **keys}, {**complete, **keys})['eligible']


def test_session_budget_uses_soft_deadline_at_paper_boundaries():
    now = [100.0]
    budget = SessionBudget(10, clock=lambda: now[0])
    assert budget.remaining_seconds == 10
    assert not budget.should_stop()
    now[0] = 110.0
    assert budget.should_stop()
    assert budget.snapshot()["remaining_seconds"] == 0


def test_session_envelope_persists_total_wall_and_detects_abrupt_run(tmp_path):
    from src.session_evidence import finish_session_envelope, start_session_envelope
    path = start_session_envelope(tmp_path, "closed")
    result = finish_session_envelope(path, started_monotonic=10.0, clock=lambda: 13.5)
    assert result["status"] == "finished" and result["wall_seconds"] == 3.5
    interrupted = start_session_envelope(tmp_path, "interrupted")
    data = json.loads(interrupted.read_text())
    assert data["status"] == "running" and data["finished_at"] is None


def test_complete_corpus_signature_includes_verified_not_found_ids():
    from types import SimpleNamespace

    store = SimpleNamespace(
        read_manifest=lambda *_: {"discovery_status": "not_found", "status": "not_found", "versions": {}},
    )
    valid_not_found = SimpleNamespace(
        status="valid", is_m1_complete=False, has_integrity_failure=False,
    )
    with patch("src.storage_integrity.audit_paper_integrity", return_value=valid_not_found):
        signature = fingerprint_complete_corpus(store, [("24120260", "2501.02642")])
    assert isinstance(signature, str) and len(signature) == 64


def test_complete_corpus_signature_rejects_unresolved_and_invalid_ids():
    from types import SimpleNamespace

    store = SimpleNamespace(
        read_manifest=lambda *_: {"discovery_status": "found", "status": "partial", "versions": {}},
    )
    unresolved = SimpleNamespace(
        status="incomplete", is_m1_complete=False, has_integrity_failure=False,
    )
    invalid = SimpleNamespace(
        status="invalid", is_m1_complete=False, has_integrity_failure=True,
    )
    for audit in (unresolved, invalid):
        with patch("src.storage_integrity.audit_paper_integrity", return_value=audit):
            assert fingerprint_complete_corpus(store, [("24120260", "2501.02642")]) is None


def test_campaign_state_resume_preserves_cohorts_and_updates_atomically(tmp_path):
    path = tmp_path / "campaign.json"
    state = load_campaign_state(path, mssv="24120260", cohorts=[151, 152])
    save_campaign_state(path, state, cohort=151, update={"status": "source_partial", "source_complete": 73})
    resumed = load_campaign_state(path, mssv="24120260", cohorts=[151, 152])
    assert resumed["cohorts"]["151"]["status"] == "source_partial"
    assert resumed["cohorts"]["151"]["source_complete"] == 73
    assert resumed["cohorts"]["152"]["status"] == "not_started"
    save_campaign_state(path, resumed, cohort=152, update={"status": "source_complete"})
    final = json.loads(path.read_text())
    assert final["cohorts"]["151"]["source_complete"] == 73
    assert final["cohorts"]["152"]["status"] == "source_complete"


def test_group_observation_rollup_requires_stage_coverage_and_separates_session_gaps():
    from src.session_evidence import aggregate_group_observations
    resource = {"ram": {"status": "collected", "sample_count": 1,
                        "sum_sample_bytes": 10, "sampled_peak_bytes": 10},
                "disk": {"status": "collected", "sampled_peak_bytes": 20}}
    rows = []
    for index, stage in enumerate(("snapshot_prepare", "archive_backup", "restore", "source",
                                   "s2_restore", "s2", "handoff_audit", "audit")):
        second = index + (20 if index >= 4 else 0)
        rows.append({"observation_id": str(index), "session_id": "s1" if index < 4 else "s2",
                     "stage": stage, "cohort": 0 if stage in {"snapshot_prepare", "audit"} else 1,
                     "started_at": f"2026-01-01T00:00:{second:02d}+00:00",
                     "finished_at": f"2026-01-01T00:00:{second + 1:02d}+00:00",
                     "wall_seconds": 1.0, "execution_status": "returned", "resources": resource,
                     "result": {}})
    envelopes = [
        {"session_id": "s1", "started_at": "2026-01-01T00:00:00+00:00",
         "finished_at": "2026-01-01T00:00:04+00:00", "wall_seconds": 4.0, "status": "finished"},
        {"session_id": "s2", "started_at": "2026-01-01T00:00:24+00:00",
         "finished_at": "2026-01-01T00:00:28+00:00", "wall_seconds": 4.0, "status": "finished"},
    ]
    result = aggregate_group_observations(rows, members=["m"], cohorts_by_member={"m": [1]},
                                          session_envelopes=envelopes)
    assert result["status"] == "measured_intervals"
    assert result["known_active_seconds"] == 8
    assert result["session_gaps_seconds"] == 20
    assert result["end_to_end_wall_seconds"] == 8
    assert result["end_to_end_complete"] is False
    assert result["expected_id_count"] is None


def test_group_observation_rollup_rejects_unfinished_session_envelope():
    from src.session_evidence import aggregate_group_observations
    result = aggregate_group_observations([], members=[], cohorts_by_member={}, session_envelopes=[{}])
    assert result["status"] == "incomplete"
    assert result["end_to_end_wall_seconds"] is None

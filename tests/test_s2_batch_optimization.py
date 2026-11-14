"""Offline checks for M1 batch completeness, failure boundaries and pacing."""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from src import m1_phases, s2_batch, s2_integration, semantic_scholar as s2
from src.fast_production import FastProductionRunner
from src.m1_phase_modules import s2_phase
from test_semantic_scholar import Response, http_error, identifiers, paper


def batch_paper(identifier):
    result = paper(identifier)
    result.update(referenceCount=1, references=[{
        "paperId": "reference-1", "title": "Reference", "year": 2024,
        "authors": [{"name": "Author"}], "venue": "Fixture",
        "externalIds": {"ArXiv": "hep-th/9901001"}, "publicationDate": "2024-01-01",
    }])
    return result


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(now=0.0, calls=[], outcomes=[], starts=[])

    def send(request, timeout):
        state.calls.append(request)
        state.starts.append(state.now)
        outcome = state.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(json.loads(request.data)["ids"])
        return Response(outcome)

    monkeypatch.setattr(s2, "_opener", SimpleNamespace(open=send))
    monkeypatch.setattr(s2.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(s2.time, "sleep", lambda delay: setattr(state, "now", state.now + delay))
    monkeypatch.setattr(s2.random, "uniform", lambda *_: 0.0)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "offline-key")
    state.limiter = s2.RateLimiter(max_requests_per_window=None)
    return state


def fetch(state, ids, **kwargs):
    return s2_batch.fetch_enrichment(ids, limiter=state.limiter, **kwargs)


def test_one_batch_keeps_required_fields_and_explicit_collection_scope(transport):
    ids = identifiers(100)
    transport.outcomes.append([batch_paper(i) for i in ids])
    rows = fetch(transport, ids)
    assert len(transport.calls) == 1
    assert len(rows) == 100
    for identifier, row in zip(ids, rows):
        s2_integration.metadata_result(row["metadata"], identifier)
        s2_integration.references_evidence(row["references"], identifier)
        snapshot = row["references"]
        assert snapshot["coverage"]["reported_total"] == len(snapshot["items"]) == 1
        assert snapshot["provenance"]["relation_fields_collected"] is False
        assert snapshot["items"][0]["arxiv_id"] == "hep-th/9901001"
        assert snapshot["items"][0]["publication_date"] == "2024-01-01"
        assert "contexts" not in snapshot["items"][0]["raw"]


@pytest.mark.parametrize("change", [
    {"references": None}, {"referenceCount": None}, {"referenceCount": 2},
    {"references": "invalid"}, {"references": [{"authors": "invalid"}]},
])
def test_unproven_references_request_fallback_instead_of_claiming_complete(transport, change):
    identifier = identifiers(1)[0]
    data = batch_paper(identifier)
    data.update(change)
    transport.outcomes.append([data])
    row = fetch(transport, [identifier])[0]
    assert row["metadata"]["status"] == "found"
    assert row["references"] is None
    assert row["fallback_reason"]
    assert len(transport.calls) == 1


def test_missing_references_is_not_an_empty_reference_list(transport):
    identifier = identifiers(1)[0]
    data = batch_paper(identifier)
    del data["references"]
    data["referenceCount"] = 0
    transport.outcomes.append([data])
    assert fetch(transport, [identifier])[0]["references"] is None


def test_verified_empty_list_is_complete(transport):
    identifier = identifiers(1)[0]
    data = batch_paper(identifier)
    data.update(references=[], referenceCount=0)
    transport.outcomes.append([data])
    assert fetch(transport, [identifier])[0]["references"]["status"] == "complete"


def test_swapped_batch_identities_fail_before_any_row_is_accepted(transport):
    ids = identifiers(2)
    transport.outcomes.append([batch_paper(i) for i in reversed(ids)])
    rows = fetch(transport, ids)
    assert len(transport.calls) == 1
    assert all(r["metadata"]["status"] == "invalid_response" for r in rows)
    assert all(r["references"] is None for r in rows)


@pytest.mark.parametrize("code", [403, 429, 500])
def test_http_failures_do_not_trigger_recursive_splitting(transport, code):
    ids = identifiers(201)
    transport.outcomes.extend(http_error(code) for _ in range(3))
    rows = fetch(transport, ids, max_attempts=1)
    expected = 1 if code in {403, 429} else 3
    assert len(transport.calls) == expected
    if code in {403, 429}:
        assert all(r["metadata"]["attempts"] == 0 for r in rows[100:])


def test_retry_after_and_backoff_still_apply_to_batch(transport):
    ids = identifiers(2)
    transport.outcomes.extend([http_error(429, after=10), [batch_paper(i) for i in ids]])
    rows = fetch(transport, ids, max_attempts=2)
    assert all(r["references"] is not None for r in rows)
    assert len(transport.calls) == 2
    assert transport.starts[1] - transport.starts[0] >= 10


def test_size_failure_splits_but_403_in_left_half_stops_right_half(transport):
    ids = identifiers(2)
    transport.outcomes.extend([http_error(413), http_error(403)])
    rows = fetch(transport, ids, max_attempts=1)
    assert len(transport.calls) == 2
    assert rows[0]["metadata"]["http_status"] == 403
    assert rows[1]["metadata"]["attempts"] == 0


@pytest.mark.parametrize("size_error", [413, 400])
def test_only_recognized_size_errors_split(transport, size_error):
    ids = identifiers(2)
    error = http_error(size_error)
    if size_error == 400:
        error = HTTPError(s2.BATCH_URL, 400, "fixture", error.headers,
                          io.BytesIO(b'{"message":"Response would exceed maximum size"}'))
    transport.outcomes.extend([error, [batch_paper(ids[0])], [batch_paper(ids[1])]])
    assert all(r["references"] is not None for r in fetch(transport, ids, max_attempts=1))
    assert len(transport.calls) == 3


def test_authenticated_limiter_does_not_pause_at_request_101(transport):
    for _ in range(120):
        with transport.limiter.request_slot():
            pass
    assert transport.now == 119
    assert not transport.limiter._request_timestamps


def test_unauthenticated_rolling_window_is_preserved(transport):
    limiter = s2.RateLimiter()
    for _ in range(101):
        with limiter.request_slot():
            pass
    assert transport.now == 300


def test_one_session_limiter_is_reused_across_cohorts_and_scope_is_reset(tmp_path, monkeypatch):
    seen = []

    def process(*_args):
        seen.append(s2_phase._REQUEST_SCOPE.get()[0])
        return dict(status="complete", stopped=False, metadata_complete=100, references_complete=100)

    monkeypatch.setattr(s2_phase, "_process_s2_cohort", process)
    monkeypatch.setattr(s2_phase, "observe_stage", lambda runner, stage, cohort, fn, **kw: fn())
    result = s2_phase.run_m1_s2_member(
        "24120260", drive_root=tmp_path / "drive", local_root=tmp_path / "local",
        semantic_scholar_api_key="offline-key", cohort_limit=2, reference_strategy="batch_m1",
        runner_factory=lambda **kw: FastProductionRunner(**kw, scratch_root=tmp_path / "scratch"),
    )
    assert result["references_complete"] == 200
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[0].max_requests_per_window is None
    assert s2_phase._REQUEST_SCOPE.get() is None


def test_batch_phase_checkpoints_all_papers_and_second_run_uses_no_http(tmp_path, monkeypatch):
    ids = [i for member, i in s2_phase.cohort_pairs(151) if member == "24120260"]
    original = FastProductionRunner
    monkeypatch.setattr(m1_phases, "FastProductionRunner", lambda **kw: original(
        **kw, scratch_root=tmp_path / "scratch"))
    rows = [{"metadata": {"arxiv_id": i, "status": "found", "http_status": 200,
                           "attempts": 1, "paper": paper(i), "error": None},
             "references": s2_batch._snapshot(i, batch_paper(i))[0], "fallback_reason": None}
            for i in ids]
    batch = Mock(return_value=rows)
    monkeypatch.setattr(s2_batch, "fetch_enrichment", batch)
    paginated = Mock(side_effect=AssertionError("No per-paper requests expected"))
    monkeypatch.setattr(s2, "fetch_references", paginated)
    settings = dict(drive_root=tmp_path / "drive", local_root=tmp_path / "local",
                    semantic_scholar_api_key="offline-key", cohort_limit=1,
                    reference_strategy="batch_m1")
    first = m1_phases.run_m1_s2_member("24120260", **settings)
    assert first["metadata_complete"] == first["references_complete"] == 100
    assert batch.call_count == 1
    checkpoint = next((tmp_path / "drive").rglob("cohort_0151/checkpoint.json"))
    saved = json.loads(checkpoint.read_text())
    assert len(saved["files"]) == 300  # 200 API files and 100 input-linked quality records.
    assert sum(name.endswith("quality.json") for name in saved["files"]) == 100
    before = copy.deepcopy(saved["files"])
    second = m1_phases.run_m1_s2_member("24120260", **settings)
    assert second["references_complete"] == 100
    assert batch.call_count == 1
    assert json.loads(checkpoint.read_text())["files"] == before
    paginated.assert_not_called()


def test_unverified_batch_uses_paginated_fallback_for_only_the_affected_paper(tmp_path, monkeypatch):
    from test_m1_phases import _references

    ids = [i for member, i in s2_phase.cohort_pairs(151) if member == "24120260"]
    monkeypatch.setattr(m1_phases, "FastProductionRunner", lambda **kw: FastProductionRunner(
        **kw, scratch_root=tmp_path / "scratch"))
    rows = [{"metadata": {"arxiv_id": i, "status": "found", "http_status": 200,
                           "attempts": 1, "paper": paper(i), "error": None},
             "references": s2_batch._snapshot(i, batch_paper(i))[0], "fallback_reason": None}
            for i in ids]
    rows[0].update(references=None, fallback_reason="reference_count_unverified")
    monkeypatch.setattr(s2_batch, "fetch_enrichment", Mock(return_value=rows))
    paginated = Mock(side_effect=lambda i, **kw: _references(i, found=True))
    monkeypatch.setattr(s2, "fetch_references", paginated)
    result = m1_phases.run_m1_s2_member(
        "24120260", drive_root=tmp_path / "drive", local_root=tmp_path / "local",
        semantic_scholar_api_key="offline-key", cohort_limit=1, reference_strategy="batch_m1",
    )
    assert result["references_complete"] == 100
    assert paginated.call_count == 1
    assert paginated.call_args.args[0] == ids[0]


@pytest.mark.parametrize("code", [403, 429])
def test_batch_phase_saves_failure_and_stops_without_paginated_requests(tmp_path, monkeypatch, code):
    ids = [i for member, i in s2_phase.cohort_pairs(151) if member == "24120260"]
    monkeypatch.setattr(m1_phases, "FastProductionRunner", lambda **kw: FastProductionRunner(
        **kw, scratch_root=tmp_path / "scratch"))
    rows = [{"metadata": {"arxiv_id": i,
                           "status": "rate_limited" if code == 429 else "http_error",
                           "http_status": code, "attempts": 1, "paper": None,
                           "error": "http_" + str(code)},
             "references": None, "fallback_reason": None} for i in ids]
    batch = Mock(return_value=rows)
    monkeypatch.setattr(s2_batch, "fetch_enrichment", batch)
    paginated = Mock(side_effect=AssertionError("Stop on HTTP policy"))
    monkeypatch.setattr(s2, "fetch_references", paginated)
    result = m1_phases.run_m1_s2_member(
        "24120260", drive_root=tmp_path / "drive", local_root=tmp_path / "local",
        semantic_scholar_api_key="offline-key", cohort_limit=1, reference_strategy="batch_m1",
    )
    assert result["stopped"] is True
    assert result["stop_reason"]["http_status"] == code
    assert result["stop_reason"]["checkpoint_saved"] is True
    assert result["references_complete"] == 0
    assert batch.call_count == 1
    paginated.assert_not_called()


def test_old_partial_snapshot_keeps_relation_data_and_uses_paginated_resume(tmp_path, monkeypatch):
    from test_m1_phases import _metadata, _references

    first_id = "2501.02642"
    old = s2_batch._snapshot(first_id, batch_paper(first_id))[0]
    raw = old["items"][0]["raw"]
    raw.update(contexts=["Saved context"], intents=["Background"], isInfluential=False)
    old["items"][0] = s2._normalize_reference(raw)
    old.update(status="partial", error={"type": "rate_limited", "message": "http_429",
                                       "http_status": 429, "attempts": 1, "offset": 1})
    old["coverage"].update(complete=False, continuation={"offset": 1}, reported_total=2)
    monkeypatch.setattr(m1_phases, "FastProductionRunner", lambda **kw: FastProductionRunner(
        **kw, scratch_root=tmp_path / "scratch"))
    monkeypatch.setattr(s2, "fetch_metadata", lambda ids, **kw: [_metadata(i, found=True) for i in ids])
    initial = Mock(return_value=copy.deepcopy(old))
    monkeypatch.setattr(s2, "fetch_references", initial)
    settings = dict(drive_root=tmp_path / "drive", local_root=tmp_path / "local",
                    semantic_scholar_api_key="offline-key", cohort_limit=1)
    assert m1_phases.run_m1_s2_member("24120260", **settings)["stopped"]
    resumed = []

    def paginated(identifier, **kw):
        if identifier == first_id:
            resumed.append(kw["resume_from"])
            snapshot = copy.deepcopy(old)
            snapshot["items"] *= 2
            snapshot.update(status="complete", error=None)
            snapshot["coverage"].update(complete=True, continuation=None, retrieved_count=2)
            return snapshot
        return _references(identifier, found=True)

    def batch(ids, **kw):
        assert first_id not in ids
        return [{"metadata": _metadata(i, found=True),
                 "references": s2_batch._snapshot(i, batch_paper(i))[0],
                 "fallback_reason": None} for i in ids]

    monkeypatch.setattr(s2, "fetch_references", paginated)
    monkeypatch.setattr(s2_batch, "fetch_enrichment", batch)
    result = m1_phases.run_m1_s2_member("24120260", reference_strategy="batch_m1", **settings)
    assert result["references_complete"] == 100
    assert resumed == [old]
    assert resumed[0]["items"][0]["raw"]["contexts"] == ["Saved context"]

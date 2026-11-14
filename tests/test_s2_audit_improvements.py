"""Offline regression checks for audited provider gaps and interrupted cleanup."""
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import arxiv_client, reference_dates, s2_batch, s2_integration
from src.benchmark_metrics import ResourceSampler
from src.export import export_references_table
from src.s2_quality import assess_references
from src.session_evidence import observe_stage
from test_s2_batch_optimization import batch_paper, transport  # noqa: F401
from test_arxiv_client import paper_xml
from test_semantic_scholar import identifiers


def test_batch_fallback_persists_original_list_and_validates_reason(transport):
    identifier = identifiers(1)[0]
    original = batch_paper(identifier)
    original["referenceCount"] = 2
    transport.outcomes.append([original])
    row = s2_batch.fetch_enrichment([identifier], limiter=transport.limiter)[0]
    metadata = row["metadata"]
    assert metadata["batch_fallback"]["references"] == original["references"]
    s2_integration.metadata_result(metadata, identifier)
    bad = copy.deepcopy(metadata)
    bad["batch_fallback"]["reason"] = "references_missing_or_invalid"
    with pytest.raises(ValueError, match="batch_fallback_reason"):
        s2_integration.metadata_result(bad, identifier)


@pytest.mark.parametrize("reported,received", [(82, 81), (79, 78)])
def test_terminal_pagination_does_not_claim_count_agreement(reported, received):
    identifier = identifiers(1)[0]
    snapshot = s2_batch._snapshot(identifier, batch_paper(identifier))[0]
    snapshot["items"] *= received
    snapshot["provenance"].pop("retrieval_strategy")
    snapshot["coverage"].update(retrieved_count=received, reported_total=None)
    quality = assess_references({"paper": {"referenceCount": reported}}, snapshot)
    assert quality["pagination_complete"] is True
    assert quality["collection_complete"] is True
    assert quality["count_match"] is False
    assert quality["retrieved_count"] == received


def test_unknown_count_is_distinct_from_verified_empty_list():
    identifier = identifiers(1)[0]
    paper = batch_paper(identifier)
    paper.update(referenceCount=0, references=[])
    snapshot = s2_batch._snapshot(identifier, paper)[0]
    assert assess_references({"paper": paper}, snapshot)["count_match"] is True
    assert assess_references({"paper": {}}, snapshot)["count_match"] is None


def test_shutdown_does_not_wait_for_stuck_final_disk_or_accept_late_samples(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def read(_):
        entered.set()
        release.wait(3)
        return 42
    sampler = ResourceSampler(tmp_path, disk_reader=read)
    worker = threading.Thread(target=sampler.sample_disk, daemon=True)
    worker.start()
    assert entered.wait(1)
    sampler._threads.append((worker, False))
    try:
        sampler.stop(timeout=0.01)
        before = sampler.report()
        assert before["disk"]["final_corpus_status"] == "not_collected"
        release.set()
        worker.join(timeout=1)
        sampler.sample_disk(final=True)
        assert sampler.report() == before
    finally:
        release.set()


def test_observation_closes_even_when_cleanup_is_interrupted(tmp_path, monkeypatch):
    runner = SimpleNamespace(corpus_dir=tmp_path / "corpus", scratch_dir=tmp_path / "scratch",
                             reports_dir=tmp_path / "reports")
    def interrupt(self):
        self._stop.set()
        raise KeyboardInterrupt
    monkeypatch.setattr(ResourceSampler, "stop", interrupt)
    with pytest.raises(KeyboardInterrupt):
        observe_stage(runner, "s2", 151, lambda: {"status": "partial"}, session_id="test")
    record = json.loads(next((runner.reports_dir / "stage_observations").glob("*.json")).read_text())
    assert record["execution_status"] == "returned"
    assert record["result"]["status"] == "partial"
    assert record["cleanup_status"] == "interrupted" and record["finished_at"]


def date_store(ids, *, observed_date=None):
    refs = {"status": "complete", "coverage": {"complete": True}, "items": [
        {"arxiv_id": i, "publication_date": observed_date, "submission_date": None,
         "title": "Reference", "authors": ["A"], "s2_paper_id": "s2"} for i in ids]}
    return SimpleNamespace(
        read_references=lambda *_: copy.deepcopy(refs),
        read_metadata=Mock(side_effect=ValueError("not in local corpus")),
    )


def fake_batch(mssv, ids, **_):
    # Different published/updated dates, latest v3, and exact ID parsing.
    bodies = [paper_xml(identifier).decode() for identifier in ids]
    xml = '<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(
        '<entry>' + value.split('<entry>', 1)[1].split('</entry>')[0] + '</entry>'
        for value in bodies) + '</feed>'
    return {"status": "found", "http_status": 200, "raw_xml": xml}


def test_date_enrichment_deduplicates_edges_keeps_s2_and_reuses_cache(tmp_path, monkeypatch):
    store = date_store(["1810.04805", "1810.04805", "hep-th/9901001"])
    original = store.read_references()
    query = Mock(side_effect=fake_batch)
    monkeypatch.setattr(arxiv_client, "query_arxiv_batch", query)
    pairs = [("24120260", "2501.02707")]
    root = tmp_path / "dates"
    result = reference_dates.enrich_reference_dates(store, pairs, root)
    assert result["missing_date_rows_before"] == 3
    assert result["missing_date_unique_ids"] == result["resolved_unique_ids"] == 2
    assert set(query.call_args.args[1]) == {"1810.04805", "hep-th/9901001"}
    assert result["dates"]["1810.04805"] == "2025-02-12"  # published, never updated.
    second = reference_dates.enrich_reference_dates(store, pairs, root)
    assert second["cache_reused_ids"] == 2 and query.call_count == 1
    rows = export_references_table(store, pairs, reference_date_cache=root, strict=True)
    assert len(rows) == 2 and all(r["ref_submission_date"] == "2025-02-12" for r in rows)
    assert store.read_references() == original
    assert len(next(root.glob("*.response.json")).read_bytes()) > 0


def test_cache_tampering_fails_closed_before_requery(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv_client, "query_arxiv_batch", Mock(side_effect=fake_batch))
    store = date_store(["1810.04805"])
    root = tmp_path / "dates"
    reference_dates.enrich_reference_dates(store, [("24120260", "2501.02707")], root)
    path = next(root.glob("*.response.json"))
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match="checksum"):
        reference_dates.enrich_reference_dates(store, [("24120260", "2501.02707")], root)


def test_existing_s2_date_requires_no_arxiv_query(tmp_path, monkeypatch):
    query = Mock(side_effect=AssertionError("unexpected HTTP"))
    monkeypatch.setattr(arxiv_client, "query_arxiv_batch", query)
    result = reference_dates.enrich_reference_dates(date_store(["1810.04805"], observed_date="2018-10-11"),
                                                    [("24120260", "2501.02707")], tmp_path / "dates")
    assert result["missing_date_unique_ids"] == 0
    query.assert_not_called()


def test_unresolved_id_stays_null_and_temporary_failure_can_be_retried(tmp_path, monkeypatch):
    query = Mock(return_value={"status": "rate_limited", "http_status": 429})
    monkeypatch.setattr(arxiv_client, "query_arxiv_batch", query)
    store = date_store(["1810.04805"])
    root = tmp_path / "dates"
    pairs = [("24120260", "2501.02707")]
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = reference_dates.enrich_reference_dates(store, pairs, root, now=lambda: stamp)
    assert first["stopped"] and not first["dates"]
    reference_dates.enrich_reference_dates(store, pairs, root, now=lambda: stamp + timedelta(minutes=1))
    assert query.call_count == 1
    query.side_effect = fake_batch
    final = reference_dates.enrich_reference_dates(store, pairs, root, now=lambda: stamp + timedelta(minutes=6))
    assert final["resolved_unique_ids"] == 1 and query.call_count == 2


def test_date_enrichment_checks_session_budget_before_http(tmp_path, monkeypatch):
    query = Mock(side_effect=AssertionError("unexpected HTTP"))
    monkeypatch.setattr(arxiv_client, "query_arxiv_batch", query)
    result = reference_dates.enrich_reference_dates(date_store(["1810.04805"]),
                                                    [("24120260", "2501.02707")], tmp_path / "dates",
                                                    should_stop=lambda: True)
    assert result["stopped"] and result["unresolved_unique_ids"] == 1
    query.assert_not_called()


def test_legacy_76_paper_checkpoint_resumes_only_24_without_rewriting_evidence(tmp_path, monkeypatch):
    from src import m1_phases, semantic_scholar
    from src.fast_production import FastProductionRunner
    from src.m1_phase_modules.checkpoint import _persist_selection, _write_json
    from src.m1_phase_modules.common import build_member_selection
    from src.m1_phase_modules.s2_evidence import (
        _cohort_s2_paths, _new_s2_manifest, _save_s2_metadata, _save_s2_references,
    )

    drive = tmp_path / "drive"
    drive.mkdir()
    selection = build_member_selection("24120260", 1)
    root = _persist_selection(drive, selection)
    cohort_root, checkpoint = _cohort_s2_paths(root, 151)
    cohort_root.mkdir(parents=True)
    manifest = _new_s2_manifest(selection, 151)
    def row(identifier):
        parent = batch_paper(identifier)
        return {"metadata": {"arxiv_id": identifier, "status": "found", "http_status": 200,
                             "attempts": 1, "paper": {k: v for k, v in parent.items() if k != "references"},
                             "error": None},
                "references": s2_batch._snapshot(identifier, parent)[0], "fallback_reason": None}
    for identifier in manifest["ids"][:76]:
        value = row(identifier)
        if identifier == manifest["ids"][0]:
            value["metadata"]["paper"]["referenceCount"] = 2
        _save_s2_metadata(manifest, cohort_root, identifier, value["metadata"])
        _save_s2_references(manifest, cohort_root, identifier, value["references"])
    for name in list(manifest["files"]):
        if name.endswith("quality.json"):
            (cohort_root / "papers" / name).unlink()
            del manifest["files"][name]
    before = {name: (cohort_root / "papers" / name).read_bytes() for name in manifest["files"]}
    manifest.update(status="partial", metadata_complete=76, references_complete=76,
                    stopped=True, stop_reason={"type": "interrupted", "stage": "s2"})
    _write_json(checkpoint, manifest)
    fetch = Mock(side_effect=lambda ids, **_: [row(identifier) for identifier in ids])
    monkeypatch.setattr(s2_batch, "fetch_enrichment", fetch)
    monkeypatch.setattr(semantic_scholar, "fetch_references", Mock(side_effect=AssertionError("unexpected fallback")))
    monkeypatch.setattr(m1_phases, "FastProductionRunner", lambda **kw: FastProductionRunner(
        **kw, scratch_root=tmp_path / "scratch"))
    result = m1_phases.run_m1_s2_member(
        "24120260", drive_root=drive, local_root=tmp_path / "local", cohort_limit=1,
        semantic_scholar_api_key="offline-key", reference_strategy="batch_m1",
    )
    assert result["references_complete"] == 100
    assert result["reference_quality"]["count_mismatch_papers"] == 1
    assert fetch.call_count == 1 and fetch.call_args.args[0] == manifest["ids"][76:]
    assert all((cohort_root / "papers" / name).read_bytes() == raw for name, raw in before.items())
    saved = json.loads(checkpoint.read_text())
    quality_name = next(name for name in saved["files"] if name.endswith("quality.json"))
    quality_path = cohort_root / "papers" / quality_name
    bad = json.loads(quality_path.read_text())
    bad["input_sha256"]["metadata"] = "0" * 64
    saved["files"][quality_name] = _write_json(quality_path, bad)
    _write_json(checkpoint, saved)
    with pytest.raises(m1_phases.M1PhaseError, match="quality"):
        m1_phases.run_m1_s2_member(
            "24120260", drive_root=drive, local_root=tmp_path / "local", cohort_limit=1,
            semantic_scholar_api_key="offline-key", reference_strategy="batch_m1",
        )
    assert fetch.call_count == 1

"""Regression cases for interruption, restore, and publication export."""

import json
import io
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.export import export_references_table, export_to_parquet
from src.arxiv_source import extract_source
from src.fast_production import FastProductionRunner, RestoreRollbackError
from src.session_evidence import summarize_discovery_timings, summarize_paper_timings


def test_interrupted_batch_propagates_reason(tmp_path):
    runner = FastProductionRunner(tmp_path / "local", tmp_path / "drive")
    with patch("src.benchmark_runner.run_benchmark", return_value={"batch_status": "interrupted"}), \
         patch.object(runner, "_archive_cohort"):
        result = runner.run_cohort(1, max_ids=1)
    assert result["fast_runner"]["stopped"]
    assert result["fast_runner"]["stop_reason"]["type"] == "interrupted"


def test_keyboard_interrupt_from_runner_propagates_reason(tmp_path):
    runner = FastProductionRunner(tmp_path / "local", tmp_path / "drive")
    with patch("src.benchmark_runner.run_benchmark", side_effect=KeyboardInterrupt), \
         patch.object(runner, "_archive_cohort"):
        result = runner.run_cohort(1, max_ids=1)
    assert result["fast_runner"]["stop_reason"]["type"] == "interrupted"


def test_failed_publish_and_rollback_preserves_backup(tmp_path):
    runner = FastProductionRunner(tmp_path / "local", tmp_path / "drive")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "manifest.json").write_text(json.dumps({"status": "complete"}))
    (target / "manifest.json").write_text(json.dumps({"status": "partial"}))
    (target / "original.tex").write_bytes(b"original")
    real_replace = os.replace
    calls = []

    def fail_after_backup(src, dst):
        calls.append((src, dst))
        if len(calls) > 1:
            raise OSError("injected failure")
        return real_replace(src, dst)

    with patch("src.fast_production.os.replace", side_effect=fail_after_backup):
        with pytest.raises(RestoreRollbackError, match="original paper preserved at") as error:
            runner._safe_replace_paper(source, target)
    backup = Path(str(error.value).split(" at ", 1)[1])
    assert (backup / "original.tex").read_bytes() == b"original"
    assert not target.exists()


@pytest.mark.parametrize("fail_publish", [True, False])
def test_restore_cleans_backup_after_success_or_successful_rollback(tmp_path, fail_publish):
    runner = FastProductionRunner(tmp_path / "local", tmp_path / "drive")
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "manifest.json").write_text('{"status":"complete"}')
    (source / "data.tex").write_bytes(b"new")
    (target / "manifest.json").write_text('{"status":"partial"}')
    (target / "data.tex").write_bytes(b"old")
    real_replace = os.replace
    count = 0

    def replace(src, dst):
        nonlocal count
        count += 1
        if fail_publish and count == 2:
            raise OSError("publish failed")
        return real_replace(src, dst)

    with patch("src.fast_production.os.replace", side_effect=replace):
        assert runner._safe_replace_paper(source, target) is not fail_publish
    assert (target / "data.tex").read_bytes() == (b"old" if fail_publish else b"new")
    assert not list(tmp_path.glob(".backup-*"))


def test_duplicate_references_fill_missing_fields_and_record_conflict():
    class Store:
        def read_references(self, *_):
            return {"status": "complete", "coverage": {"complete": True}, "items": [
                {"arxiv_id": "2101.00001", "title": None, "authors": None},
                {"arxiv_id": "2101.00001", "title": "First", "authors": ["A"]},
                {"arxiv_id": "2101.00001", "title": "Other", "authors": ["A"]},
            ]}
    conflicts = []
    rows = export_references_table(Store(), [("member", "2501.00001")], conflicts=conflicts)
    assert len(rows) == 1
    assert rows[0]["ref_title"] == "First"
    assert rows[0]["ref_authors"] == ["A"]
    assert conflicts[0]["field"] == "ref_title"


def test_extraction_records_uncompressed_sizes_before_filtering(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in [("main.tex", b"abc"), ("image.png", b"12345")]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    result = extract_source(archive, tmp_path / "output")
    assert result["extracted_bytes"] == 8
    assert result["removed_files"] == 1
    assert result["files"] == ["main.tex"]


def test_paper_timings_deduplicate_resumed_source_attempts():
    source = {"stage": "source", "cohort": 1, "result": {"timing_metrics": {
        "paper_intervals": [{"attempt_id": "a1", "arxiv_id": "x", "state": "processed", "wall_seconds": 3.0}]}}}
    s2 = {"stage": "s2", "observation_id": "o1", "result": {
        "paper_timings": [{"arxiv_id": "x", "s2_wall_seconds": 2.0}]}}
    summary = summarize_paper_timings([source, source, s2], ["x"])
    assert summary["average_seconds"] == 5.0
    incomplete = summarize_paper_timings([source, s2], ["x", "missing"])
    assert incomplete["average_seconds"] is None
    assert incomplete["missing_ids"] == ["missing"]


def test_s2_429_cooldown_is_counted_once_in_assigned_paper_timings():
    source = {"stage": "source", "cohort": 1, "result": {"timing_metrics": {
        "paper_intervals": [
            {"attempt_id": "a1", "arxiv_id": "x", "state": "processed", "wall_seconds": 1.0},
            {"attempt_id": "a1", "arxiv_id": "y", "state": "processed", "wall_seconds": 3.0},
        ]}}}
    s2 = {"stage": "s2", "observation_id": "s2o", "result": {
        "paper_timings": [
            {"arxiv_id": "x", "s2_wall_seconds": 2.0},
            {"arxiv_id": "y", "s2_wall_seconds": 4.0},
        ]}}
    cooldown = {
        "stage": "s2_retry_wait", "session_id": "session", "observation_id": "wait1",
        "execution_status": "returned", "wall_seconds": 5.0,
        "configuration": {
            "affected_ids": ["x", "y"],
            "trigger_stage": "s2_metadata", "trigger_http_status": 429,
        },
        "result": {
            "status": "waited", "retry_wait_seconds": 5.0,
            "affected_ids": ["x", "y"],
            "trigger_stage": "s2_metadata", "trigger_http_status": 429,
        },
    }

    summary = summarize_paper_timings([source, s2, cooldown], ["x", "y"])

    assert summary["status"] == "complete"
    assert summary["s2_retry_wait_seconds"] == 5.0
    assert summary["s2_retry_wait_observation_count"] == 1
    assert summary["average_seconds"] == 7.5
    assert summary["papers"] == [
        {"arxiv_id": "x", "source_seconds": 1.0, "retry_wait_seconds": 0.0,
         "s2_seconds": 2.0, "s2_retry_wait_seconds": 2.5, "total_seconds": 5.5},
        {"arxiv_id": "y", "source_seconds": 3.0, "retry_wait_seconds": 0.0,
         "s2_seconds": 4.0, "s2_retry_wait_seconds": 2.5, "total_seconds": 9.5},
    ]


def test_discovery_timing_deduplicates_source_attempts_and_counts_setup():
    row = {"attempt_id": "a1", "arxiv_id": "x", "state": "processed",
           "discovery_action": "queried", "discovery_seconds": 3.0,
           "revision_action": "queried", "revision_seconds": 2.0}
    source = {"stage": "source", "cohort": 1, "result": {"timing_metrics": {"paper_intervals": [row]}}}
    snapshot = {"stage": "snapshot_prepare", "execution_status": "returned", "wall_seconds": 4.0}
    result = summarize_discovery_timings([snapshot, source, source], ["x"])
    assert result["wall_seconds"] == 9.0
    assert summarize_discovery_timings([snapshot, source], ["x", "y"])["wall_seconds"] is None


def test_parquet_schema_stable_for_empty_and_null_records(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "papers.parquet"
    export_to_parquet([], path)
    empty = pq.read_schema(path)
    export_to_parquet([{"arxiv_id": "2501.00001", "venue": None, "revised_dates": []}], path)
    populated = pq.read_schema(path)
    assert populated == empty
    assert populated.field("venue").type == pa.string()
    assert populated.field("revised_dates").type == pa.list_(pa.string())

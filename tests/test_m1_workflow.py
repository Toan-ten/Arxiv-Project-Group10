"""Kiểm thử ngoại tuyến cho bộ điều phối M1 Colab."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from src import m1_workflow
from src.fast_production import cohort_pairs, member_cohort_range
from src.id_ranges import load_config


class _Store:
    def read_metadata(self, *_args, **_kwargs):
        raise FileNotFoundError


class _FakeRunner:
    source_stop = False
    s2_stop = False
    calls: list[tuple] = []

    def __init__(self, local_root, drive_root, **_kwargs):
        self.local_root = Path(local_root)
        self.drive_root = Path(drive_root)
        self.corpus_dir = self.local_root / "corpus"
        self.scratch_dir = self.local_root / "scratch"
        self.logs_dir = self.drive_root / "logs"
        self.reports_dir = self.drive_root / "reports"
        self.archives_dir = self.drive_root / "archives"
        for path in (self.corpus_dir, self.scratch_dir, self.logs_dir,
                     self.reports_dir, self.archives_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.store = _Store()
        self.calls = []

    def _compute_sha256(self, _path):
        return "archive-sha"

    def backup_cohort_archive(self, *_args, **_kwargs):
        self.calls.append(("backup", _args[0]))
        return None

    def restore_cohort(self, cohort, **_kwargs):
        self.calls.append(("restore", cohort))
        return 0

    def run_cohort(self, cohort, **kwargs):
        self.calls.append(("source", cohort, kwargs))
        if kwargs.get("on_progress"):
            summary = {"papers": [{"arxiv_id": f"2501.{cohort:02d}000"}]}
            kwargs["on_progress"]("paper_started", summary)
            kwargs["on_progress"]("paper_finished", summary)
        return {
            "fast_runner": {
                "stopped": self.source_stop,
                "stop_reason": {"type": "http_policy", "http_status": 429} if self.source_stop else None,
                "completed_papers": 100,
                "target_papers": 100,
                "newly_completed_papers": 0,
            }
        }

    def enrich_cohort_s2(self, cohort, **kwargs):
        self.calls.append(("s2", cohort))
        if kwargs.get("on_progress"):
            kwargs["on_progress"]("paper_started", {
                "cohort": cohort,
                "index": 1,
                "total": 100,
                "arxiv_id": f"2501.{cohort:02d}000",
                "attempted_papers": 0,
            })
        return {
            "status": "returned",
            "attempted_papers": 4,
            "metadata_complete": 3,
            "references_complete": 3,
            "stopped": self.s2_stop,
            "stop_reason": {"type": "http_policy", "http_status": 429,
                             "checkpoint_saved": True} if self.s2_stop else None,
        }

    def count_source_completed_papers(self, _cohort):
        return 100

    def count_m1_completed_papers(self, _cohort):
        return {
            "found_m1_complete": 0,
            "confirmed_not_found": 0,
            "discovery_failed": 0,
            "unprocessed": 100,
            "integrity_failures": 0,
            "is_cohort_complete": False,
        }


class TestMemberScope(unittest.TestCase):
    def test_each_member_keeps_its_assigned_fifty_cohorts(self):
        self.assertEqual(len(load_config()), 4)
        for member in load_config():
            assigned, selected, scope = m1_workflow._select_cohorts(member, None)
            self.assertEqual(assigned, member_cohort_range(member))
            self.assertEqual(len(selected), 50)
            self.assertEqual(scope, "member_full_assignment")

    def test_sample_is_a_prefix_of_the_same_member_assignment(self):
        assigned, selected, scope = m1_workflow._select_cohorts("24120260", 1)
        self.assertEqual(selected, assigned[:1])
        self.assertEqual(scope, "member_subset_same_pipeline")

    def test_rejects_invalid_member_scope(self):
        for limit in (0, 51, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                m1_workflow._select_cohorts("24120260", limit)


class TestUnifiedWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_calls = []
        self.runners = []
        _FakeRunner.source_stop = False
        _FakeRunner.s2_stop = False

        def runner_factory(*args, **kwargs):
            runner = _FakeRunner(*args, **kwargs)
            self.runners.append(runner)
            return runner

        def observe(runner, stage, cohort, operation, **_kwargs):
            self.stage_calls.append((stage, cohort))
            return operation()

        export_patcher = patch.object(m1_workflow, "_export_datasets")
        self.export = export_patcher.start()
        self.patches = [
            patch.object(m1_workflow, "member_cohort_range", return_value=[1, 2]),
            patch.object(m1_workflow, "cohort_pairs", side_effect=lambda c: [
                ("24120260", f"2501.{c:02d}{n:03d}") for n in range(100)
            ]),
            patch.object(m1_workflow, "FastProductionRunner", side_effect=runner_factory),
            patch.object(m1_workflow, "observe_stage", side_effect=observe),
            patch.object(m1_workflow, "prepare_snapshot", return_value={
                "status": "ready", "source": "cached", "snapshot_path": str(self.root / "snapshot.jsonl"),
                "snapshot_sha256": "snapshot-sha", "snapshot_time": "unknown",
            }),
            patch.object(m1_workflow, "reuse_or_build_index", return_value={
                "index_path": str(self.root / "index.json"), "reused": True,
                "index": {"candidates": {"x": {}}, "diagnostics": {"malformed_lines": 0}},
            }),
            patch.object(m1_workflow, "revision_history_counts", return_value={"verified": 100}),
            patch.object(m1_workflow, "verify_cohort_handoff", return_value={
                "archive_state": "m1_handoff_verified",
            }),
            patch.object(m1_workflow, "_compute_statistics", return_value={
                "is_reconciled": True,
                "integrity_failed_papers": 0,
                "reconciliation": {"has_integrity_failure": False},
                "unprocessed_papers": 100,
                "discovery_failed": 0,
                "m1_complete_papers": 0,
                "confirmed_not_found": 0,
                "assigned_candidates": 100,
            }),
            patch.object(m1_workflow, "_load_campaign_observations", return_value=[]),
            patch.object(m1_workflow, "summarize_workflow", return_value={
                "status": "incomplete", "missing_stages": [["s2", 1]],
            }),
            patch("src.reporting.format_markdown_summary", return_value="audit text"),
        ]
        self.patches.append(export_patcher)
        for item in self.patches:
            if item is export_patcher:
                continue
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def run_pipeline(self, *, cohort_limit=1):
        return m1_workflow.run_m1_member(
            "24120260",
            drive_root=self.root / "drive" / "24120260",
            local_root=self.root / "local",
            kaggle_token="kaggle-secret",
            semantic_scholar_api_key="s2-secret",
            cohort_limit=cohort_limit,
        )

    def test_runs_source_then_s2_then_audit_in_one_call(self):
        pairs = [("24120260", f"2501.{1:02d}{n:03d}") for n in range(100)]
        member_fingerprint = m1_workflow.hashlib.sha256(
            json.dumps(pairs, separators=(",", ":")).encode()
        ).hexdigest()
        campaign_path = (
            self.root / "drive" / "24120260" / "reports"
            / f"campaign_state_24120260_{member_fingerprint[:16]}.json"
        )
        campaign_path.parent.mkdir(parents=True, exist_ok=True)
        campaign_path.write_text(json.dumps({
            "schema_version": 1,
            "mssv": "24120260",
            "campaign_id": "existing-campaign",
            "session_ids": [],
            "starting_archive_sha256": {"1": "empty", "2": "empty"},
            "starting_state_fingerprint": "prior-start-state",
            "starting_state_empty": True,
            "cohorts": {"1": {"status": "source_partial"}},
        }), encoding="utf-8")
        result = self.run_pipeline()
        stages = [stage for stage, _cohort in self.stage_calls]
        self.assertLess(stages.index("source"), stages.index("s2"))
        self.assertIn("handoff_audit", stages)
        self.assertIn("audit", stages)
        self.assertEqual([call[0:2] for call in self.runners[0].calls if call[0] in {"source", "s2"}],
                         [("source", 1), ("s2", 1)])
        source_call = next(call for call in self.runners[0].calls if call[0] == "source")
        self.assertTrue(source_call[2]["resume"])
        self.assertTrue(source_call[2]["revision_history"])
        self.assertEqual(result["handoff_status"], "blocked")
        self.assertEqual(result["campaign_id"], "existing-campaign")
        self.assertEqual(result["kaggle_index"]["candidate_count"], 1)
        self.assertEqual(result["cohort_results"]["1"]["s2_status"], "partial")
        self.export.assert_not_called()
        saved_text = "\n".join(path.read_text(encoding="utf-8") for path in (self.root / "drive").rglob("*") if path.is_file())
        self.assertNotIn("kaggle-secret", saved_text)
        self.assertNotIn("s2-secret", saved_text)

    def test_run_prints_stage_and_id_progress_for_notebook_cell(self):
        output = StringIO()
        with redirect_stdout(output):
            self.run_pipeline()

        text = output.getvalue()
        self.assertIn("Bắt đầu chuẩn bị snapshot Kaggle", text)
        self.assertIn("Bắt đầu tải mã nguồn và xác minh lịch sử phiên bản lô 1", text)
        self.assertIn("giai đoạn mã nguồn có trạng thái cuối 100/100 ID", text)
        self.assertIn("Bắt đầu lấy siêu dữ liệu và tài liệu tham khảo lô 1", text)
        self.assertIn("S2 đang xét ID 1/100", text)
        self.assertIn("Bắt đầu kiểm toán dữ liệu", text)

    def test_kaggle_fallback_keeps_arxiv_and_s2_path(self):
        with patch.object(m1_workflow, "prepare_snapshot", return_value={
            "status": "fallback", "reason": "missing_kaggle_token",
        }), patch.object(m1_workflow, "reuse_or_build_index") as index:
            result = self.run_pipeline()
        index.assert_not_called()
        self.assertEqual(result["kaggle"]["status"], "fallback")
        self.assertIn(("source", 1), [(c[0], c[1]) for c in self.runners[0].calls if len(c) > 1])
        self.assertIn(("s2", 1), [(c[0], c[1]) for c in self.runners[0].calls if len(c) > 1])

    def test_s2_429_stops_new_lots_but_preserves_checkpoint_reason(self):
        _FakeRunner.s2_stop = True
        result = self.run_pipeline(cohort_limit=2)
        calls = [(call[0], call[1]) for call in self.runners[0].calls if call[0] in {"source", "s2"}]
        self.assertEqual(calls, [("source", 1), ("s2", 1)])
        self.assertEqual(result["stop_reason"]["http_status"], 429)
        self.assertEqual(result["cohort_results"]["1"]["s2_status"], "stopped")
        state = json.loads(Path(result["campaign_state_path"]).read_text(encoding="utf-8"))
        self.assertTrue(state["cohorts"]["1"]["s2_stop_reason"]["checkpoint_saved"])

    def test_arxiv_429_still_runs_s2_for_eligible_checkpoint_then_stops(self):
        _FakeRunner.source_stop = True
        result = self.run_pipeline(cohort_limit=2)
        calls = [(call[0], call[1]) for call in self.runners[0].calls if call[0] in {"source", "s2"}]
        self.assertEqual(calls, [("source", 1), ("s2", 1)])
        self.assertEqual(result["stop_reason"]["http_status"], 429)


if __name__ == "__main__":
    unittest.main()

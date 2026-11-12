"""Offline qualification for independent M1 phase checkpoints and reconciliation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import m1_phases, semantic_scholar
from src.fast_production import FastProductionRunner, cohort_pairs
from src.storage import _atomic_json


MSSV = "24120260"
STAMP = "2026-11-11T00:00:00Z"


def _metadata(arxiv_id: str, *, found: bool) -> dict:
    paper = None
    if found:
        from test_semantic_scholar import paper as fixture_paper

        paper = fixture_paper(arxiv_id)
    return {
        "arxiv_id": arxiv_id,
        "status": "found" if found else "not_found",
        "http_status": 200,
        "attempts": 1,
        "paper": paper,
        "error": None,
    }


def _references(arxiv_id: str, *, found: bool) -> dict:
    if found:
        return {
            "contract_version": 2,
            "arxiv_id": arxiv_id,
            "status": "complete",
            "provenance": {
                "provider": "semantic_scholar",
                "query_identifier": "ARXIV:" + arxiv_id,
                "s2_paper_id": None,
                "retrieved_at": STAMP,
            },
            "coverage": {
                "complete": True,
                "continuation": None,
                "reported_total": 0,
                "retrieved_count": 0,
            },
            "items": [],
            "error": None,
        }
    return {
        "contract_version": 2,
        "arxiv_id": arxiv_id,
        "status": "not_found",
        "provenance": {
            "provider": "semantic_scholar",
            "query_identifier": "ARXIV:" + arxiv_id,
            "s2_paper_id": None,
            "retrieved_at": STAMP,
        },
        "coverage": {
            "complete": False,
            "continuation": None,
            "reported_total": None,
            "retrieved_count": 0,
        },
        "items": [],
        "error": {
            "type": "http_error",
            "message": "not_found",
            "http_status": 404,
            "attempts": 1,
            "offset": 0,
        },
    }


class TestMemberSelection(unittest.TestCase):
    def test_full_and_pilot_selections_are_stable_and_share_one_group_fingerprint(self):
        full = m1_phases.build_member_selection(MSSV)
        pilot = m1_phases.build_member_selection(MSSV, 1)
        self.assertEqual((len(full["cohorts"]), len(full["pairs"])), (50, 5000))
        self.assertEqual((pilot["cohorts"], len(pilot["pairs"])), ([full["cohorts"][0]], 100))
        self.assertEqual(pilot["pairs"], full["pairs"][:100])
        self.assertEqual(full["group_fingerprint"], pilot["group_fingerprint"])
        expected = hashlib.sha256(
            json.dumps(full["pairs"], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(full["fingerprint"], expected)

    def test_writer_lock_covers_subset_and_full_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive" / MSSV
            root.mkdir(parents=True)
            full = m1_phases.build_member_selection(MSSV)
            pilot = m1_phases.build_member_selection(MSSV, 1)
            with m1_phases._member_writer_lock(root, full):
                with self.assertRaisesRegex(m1_phases.M1PhaseError, "lock"):
                    with m1_phases._member_writer_lock(root, pilot):
                        pass

    def test_incompatible_checkpoint_for_same_scope_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive" / MSSV
            root.mkdir(parents=True)
            selection = m1_phases.build_member_selection(MSSV, 1)
            prior_root = m1_phases._persist_selection(root, selection)
            prior_bytes = (prior_root / "selection.json").read_bytes()
            changed = dict(selection, fingerprint="0" * 64)
            with self.assertRaisesRegex(m1_phases.M1PhaseError, "fingerprint khác"):
                m1_phases._persist_selection(root, changed)
            self.assertEqual((prior_root / "selection.json").read_bytes(), prior_bytes)


class TestIndependentS2Phase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.drive = self.base / "drive" / MSSV
        self.local = self.base / "local" / MSSV
        self.selection = m1_phases.build_member_selection(MSSV, 1)
        self.first_id = self.selection["pairs"][0][1]
        self.metadata_calls = []
        self.metadata_batches = []
        self.reference_calls = []
        self.first_response_is_429 = True

        def fetch_metadata(ids, **_kwargs):
            self.assertLessEqual(len(ids), m1_phases.S2_METADATA_BATCH_SIZE)
            self.metadata_batches.append(list(ids))
            self.metadata_calls.extend(ids)
            if self.first_id in ids and self.first_response_is_429:
                self.first_response_is_429 = False
                return [{
                    "arxiv_id": arxiv_id,
                    "status": "rate_limited",
                    "http_status": 429,
                    "attempts": 1,
                    "paper": None,
                    "error": "rate limited fixture",
                } for arxiv_id in ids]
            return [_metadata(arxiv_id, found=False) for arxiv_id in ids]

        def fetch_references(arxiv_id, **_kwargs):
            self.reference_calls.append(arxiv_id)
            return _references(arxiv_id, found=False)

        self.patches = [
            patch.object(semantic_scholar, "RateLimiter", return_value=object()),
            patch.object(semantic_scholar, "fetch_metadata", side_effect=fetch_metadata),
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def run_s2(self, **options):
        settings = {"budget_seconds": 60}
        settings.update(options)
        return m1_phases.run_m1_s2_member(
            MSSV,
            drive_root=self.drive,
            local_root=self.local,
            cohort_limit=1,
            **settings,
        )

    def _failed_references_429(self, arxiv_id):
        return {
            "contract_version": 2,
            "arxiv_id": arxiv_id,
            "status": "failed",
            "provenance": {
                "provider": "semantic_scholar",
                "query_identifier": "ARXIV:" + arxiv_id,
                "s2_paper_id": None,
                "retrieved_at": STAMP,
            },
            "coverage": {
                "complete": False,
                "continuation": {"offset": 0},
                "reported_total": None,
                "retrieved_count": 0,
            },
            "items": [],
            "error": {
                "type": "rate_limited", "message": "http_429",
                "http_status": 429, "attempts": 1, "offset": 0,
            },
        }

    def test_s2_before_arxiv_stops_on_429_then_resumes_without_arxiv_or_duplicate_ids(self):
        with patch.object(semantic_scholar, "_key_from_environment", return_value=None):
            first = self.run_s2()
            self.assertTrue(first["stopped"])
            self.assertEqual(first["stop_reason"]["http_status"], 429)
            self.assertEqual(len(self.metadata_calls), 50)
            self.assertEqual(self.metadata_calls[0], self.first_id)
            self.assertEqual(self.reference_calls, [])
            self.assertFalse(any(self.local.rglob("manifest.json")))

            second = self.run_s2()

        self.assertFalse(second["stopped"])
        self.assertEqual(second["selection_fingerprint"], self.selection["fingerprint"])
        self.assertEqual(second["metadata_complete"], 100)
        self.assertEqual(second["references_complete"], 100)
        self.assertEqual(self.metadata_calls.count(self.first_id), 2)
        self.assertEqual(len(self.metadata_calls), 150)
        self.assertEqual([len(batch) for batch in self.metadata_batches], [50, 50, 50])
        self.assertEqual(len(self.reference_calls), 100)
        checkpoint = (
            self.drive / "reports" / "phase_checkpoints" / self.selection["fingerprint"]
            / "s2" / "cohort_0151" / "checkpoint.json"
        )
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["papers"][self.first_id]["metadata_status"], "not_found")
        metadata_file = checkpoint.parent / "papers" / (self.first_id.replace(".", "-") + "/metadata.json")
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        self.assertEqual(metadata["history"][0]["status"], "rate_limited")

    def test_bounded_auto_resume_after_references_429_uses_verified_checkpoint(self):
        first_429 = {"value": True}
        resume_snapshots = []
        self.first_response_is_429 = False

        def fetch_references(arxiv_id, **kwargs):
            resume = kwargs.get("resume_from")
            if resume is not None:
                resume_snapshots.append(resume)
            if arxiv_id == self.first_id and first_429["value"]:
                first_429["value"] = False
                return self._failed_references_429(arxiv_id)
            return _references(arxiv_id, found=False)

        with (
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
            patch.object(m1_phases.random, "uniform", return_value=300.0),
            patch.object(m1_phases.time, "sleep") as sleep,
        ):
            result = self.run_s2(
                auto_resume_429=True,
                auto_resume_429_max_per_cohort=1,
                auto_resume_429_max_per_session=2,
                auto_resume_429_cooldown_min_seconds=300,
                auto_resume_429_cooldown_max_seconds=600,
                budget_seconds=3600,
            )

        self.assertFalse(result["stopped"])
        self.assertEqual(result["metadata_complete"], 100)
        self.assertEqual(result["references_complete"], 100)
        self.assertEqual(result["auto_resume_429"]["resume_attempts"], 1)
        self.assertEqual(sleep.call_args.args, (300.0,))
        self.assertEqual(len(resume_snapshots), 1)
        self.assertEqual(resume_snapshots[0]["error"]["http_status"], 429)
        event = result["auto_resume_429"]["events"][0]
        self.assertEqual(event["stage"], "s2_references")
        self.assertTrue(event["checkpoint_saved"])
        self.assertEqual(event["outcome"], "resumed")
        self.assertEqual(event["affected_ids"], [self.first_id])
        evidence_path = Path(result["auto_resume_429"]["events_path"])
        saved_events = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_events["selection_fingerprint"], self.selection["fingerprint"])
        self.assertEqual(saved_events["events"], result["auto_resume_429"]["events"])
        observations = list(
            (self.drive / "reports" / "stage_observations").glob(
                f"{result['session_id']}_*_s2_retry_wait_*.json"
            )
        )
        self.assertEqual(len(observations), 1)
        wait = json.loads(observations[0].read_text(encoding="utf-8"))
        self.assertEqual(wait["configuration"]["trigger_stage"], "s2_references")
        self.assertEqual(wait["result"]["trigger_http_status"], 429)

    def test_bounded_auto_resume_after_metadata_429_retries_only_unfinished_metadata(self):
        with (
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
            patch.object(m1_phases.random, "uniform", return_value=300.0),
            patch.object(m1_phases.time, "sleep") as sleep,
        ):
            result = self.run_s2(auto_resume_429=True, budget_seconds=3600)

        self.assertFalse(result["stopped"])
        self.assertEqual(result["metadata_complete"], 100)
        self.assertEqual(result["references_complete"], 100)
        self.assertEqual(result["auto_resume_429"]["resume_attempts"], 1)
        self.assertEqual(sleep.call_args.args, (300.0,))
        self.assertEqual(len(self.metadata_batches), 3)
        self.assertEqual(self.metadata_calls.count(self.first_id), 2)
        first_event = result["auto_resume_429"]["events"][0]
        self.assertEqual(first_event["stage"], "s2_metadata")
        self.assertTrue(first_event["checkpoint_saved"])
        self.assertEqual(first_event["outcome"], "resumed")
        self.assertEqual(len(first_event["affected_ids"]), 50)

    def test_auto_resume_limit_stops_after_one_retry_and_keeps_checkpoint(self):
        reference_calls = []
        self.first_response_is_429 = False

        def fetch_references(arxiv_id, **_kwargs):
            reference_calls.append(arxiv_id)
            if arxiv_id == self.first_id:
                return self._failed_references_429(arxiv_id)
            return _references(arxiv_id, found=False)

        with (
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
            patch.object(m1_phases.random, "uniform", return_value=300.0),
            patch.object(m1_phases.time, "sleep"),
        ):
            result = self.run_s2(auto_resume_429=True, budget_seconds=3600)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["auto_resume_429"]["resume_attempts"], 1)
        self.assertEqual(result["auto_resume_429"]["events"][-1]["deferred_reason"], "cohort_resume_limit")
        self.assertEqual(reference_calls.count(self.first_id), 2)

    def test_auto_resume_does_not_retry_http_403(self):
        self.first_response_is_429 = False
        def fetch_references(arxiv_id, **_kwargs):
            result = self._failed_references_429(arxiv_id)
            result["error"].update(type="http_error", message="http_403", http_status=403)
            return result

        with (
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
            patch.object(m1_phases.time, "sleep") as sleep,
        ):
            result = self.run_s2(auto_resume_429=True, budget_seconds=3600)

        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_reason"]["http_status"], 403)
        self.assertEqual(result["auto_resume_429"]["resume_attempts"], 0)
        self.assertEqual(result["auto_resume_429"]["events"], [])
        sleep.assert_not_called()

    def test_auto_resume_uses_retry_after_and_stops_when_session_budget_is_insufficient(self):
        self.first_response_is_429 = False

        def fetch_references(arxiv_id, **_kwargs):
            return self._failed_references_429(arxiv_id)

        limiter = type("LimiterWithRetryAfter", (), {"cooldown_remaining_seconds": 450.0})()
        with (
            patch.object(semantic_scholar, "RateLimiter", return_value=limiter),
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
            patch.object(m1_phases.random, "uniform", return_value=300.0),
            patch.object(m1_phases.time, "sleep") as sleep,
        ):
            result = self.run_s2(auto_resume_429=True, budget_seconds=60)

        self.assertTrue(result["stopped"])
        event = result["auto_resume_429"]["events"][0]
        self.assertTrue(event["checkpoint_saved"])
        self.assertEqual(event["deferred_reason"], "session_budget")
        self.assertEqual(event["cooldown_seconds"], 450.0)
        sleep.assert_not_called()

    def test_batch_length_mismatch_fails_before_any_metadata_evidence_write(self):
        with (
            patch.object(semantic_scholar, "fetch_metadata", return_value=[
                _metadata(self.first_id, found=False),
            ]),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
        ):
            with self.assertRaisesRegex(m1_phases.M1PhaseError, "batch không khớp"):
                self.run_s2()
        checkpoint = (
            self.drive / "reports" / "phase_checkpoints" / self.selection["fingerprint"]
            / "s2" / "cohort_0151" / "checkpoint.json"
        )
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(saved["files"], {})

    def test_invalid_batch_is_split_and_each_id_is_checkpointed(self):
        sizes = []

        def fetch_metadata(ids, **_kwargs):
            sizes.append(len(ids))
            if len(ids) == 50 and ids[0] == self.first_id:
                return [{"arxiv_id": aid, "status": "invalid_response",
                         "http_status": 200, "attempts": 1, "paper": None,
                         "error": "invalid_batch_length_or_structure"} for aid in ids]
            return [_metadata(aid, found=False) for aid in ids]

        with (
            patch.object(semantic_scholar, "fetch_metadata", side_effect=fetch_metadata),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
        ):
            result = self.run_s2()
        self.assertFalse(result["stopped"])
        self.assertEqual(sizes, [50, 25, 25, 50])
        checkpoint = (
            self.drive / "reports" / "phase_checkpoints" / self.selection["fingerprint"]
            / "s2" / "cohort_0151" / "checkpoint.json"
        )
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(len([name for name in saved["files"] if name.endswith("metadata.json")]), 100)

    def test_checkpoint_checksum_mismatch_fails_closed_and_preserves_file(self):
        with patch.object(semantic_scholar, "_key_from_environment", return_value=None):
            self.first_response_is_429 = False
            self.run_s2()
        checkpoint = (
            self.drive / "reports" / "phase_checkpoints" / self.selection["fingerprint"]
            / "s2" / "cohort_0151" / "checkpoint.json"
        )
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        relative = next(name for name in saved["files"] if name.endswith("/metadata.json"))
        evidence = checkpoint.parent / "papers" / relative
        evidence.write_text("{}\n", encoding="utf-8")
        before = evidence.read_bytes()
        with patch.object(semantic_scholar, "_key_from_environment", return_value=None):
            with self.assertRaisesRegex(m1_phases.M1PhaseError, "Checksum"):
                self.run_s2()
        self.assertEqual(evidence.read_bytes(), before)

    def test_partial_reference_page_resumes_from_saved_continuation(self):
        second_id = self.selection["pairs"][50][1]
        stopped_once = {"value": False}
        partial_once = {"value": False}
        resumes = []

        def fetch_metadata(ids, **_kwargs):
            if second_id in ids and not stopped_once["value"]:
                stopped_once["value"] = True
                return [{"arxiv_id": arxiv_id, "status": "rate_limited", "http_status": 429,
                         "attempts": 1, "paper": None, "error": "rate limited fixture"}
                        for arxiv_id in ids]
            return [_metadata(arxiv_id, found=False) for arxiv_id in ids]

        def fetch_references(arxiv_id, **kwargs):
            resume = kwargs.get("resume_from")
            if resume is not None:
                resumes.append(resume["coverage"]["continuation"])
            if arxiv_id == self.first_id and not partial_once["value"]:
                partial_once["value"] = True
                result = _references(arxiv_id, found=True)
                result.update(status="partial", coverage={
                    "complete": False, "continuation": {"offset": 0},
                    "reported_total": 1, "retrieved_count": 0,
                }, error={"type": "timeout", "message": "timeout", "http_status": None,
                         "attempts": 1, "offset": 0})
                return result
            return _references(arxiv_id, found=False)

        with (
            patch.object(semantic_scholar, "RateLimiter", return_value=object()),
            patch.object(semantic_scholar, "fetch_metadata", side_effect=fetch_metadata),
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
        ):
            first = self.run_s2()
            self.assertTrue(first["stopped"])
            second = self.run_s2()

        self.assertFalse(second["stopped"])
        self.assertEqual(resumes, [{"offset": 0}])
        self.assertEqual(second["references_complete"], 100)


class TestArxivAndAuditGates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.drive = self.base / "drive" / MSSV
        self.local = self.base / "local" / MSSV
        self.selection = m1_phases.build_member_selection(MSSV, 1)
        self.cohort = self.selection["cohorts"][0]

    def tearDown(self):
        self.temp.cleanup()

    def test_starting_state_fingerprint_is_captured_once_and_survives_resume(self):
        runner = FastProductionRunner(local_root=self.local, drive_root=self.drive)
        phase_root = m1_phases._persist_selection(self.drive, self.selection)
        initial = m1_phases._ensure_starting_state(runner, phase_root, self.selection)
        self.assertTrue(initial["starting_state_empty"])
        (runner.corpus_dir / "new-local-state").mkdir()
        resumed = m1_phases._ensure_starting_state(runner, phase_root, self.selection)
        self.assertEqual(resumed, initial)

    def test_s2_not_found_conflicting_with_arxiv_found_is_reported_before_merge(self):
        runner = FastProductionRunner(local_root=self.local, drive_root=self.drive)
        member, arxiv_id = self.selection["pairs"][0]
        runner.store.record_discovery({
            "mssv": member, "arxiv_id": arxiv_id, "status": "not_found",
        })
        selection = dict(self.selection, pairs=[[member, arxiv_id]], expected_count=1)
        evidence = {
            arxiv_id: (
                _metadata(arxiv_id, found=True),
                _references(arxiv_id, found=False),
            ),
        }
        issues = m1_phases._preflight_merge(runner, selection, evidence)
        self.assertEqual(issues, [{
            "arxiv_id": arxiv_id, "issue": "s2_conflicts_with_arxiv_not_found",
        }])
        self.assertEqual(runner.store.read_manifest(member, arxiv_id)["discovery_status"], "not_found")

    def test_arxiv_phase_never_calls_s2_and_never_claims_m1(self):
        with (
            patch.object(FastProductionRunner, "run_cohort", return_value={
                "fast_runner": {"stopped": False, "stop_reason": None},
            }),
            patch.object(FastProductionRunner, "count_source_completed_papers", return_value=100),
            patch.object(m1_phases, "_mark_archive_arxiv_only"),
            patch.object(semantic_scholar, "fetch_metadata", side_effect=AssertionError("S2 must stay separate")),
        ):
            result = m1_phases.run_m1_arxiv_member(
                MSSV,
                drive_root=self.drive,
                local_root=self.local,
                cohort_limit=1,
                budget_seconds=60,
            )
        self.assertEqual(result["source_complete_papers"], 100)
        self.assertFalse(result["m1_complete"])
        saved = json.loads(Path(result["checkpoint_root"]).joinpath("cohort_0151.json").read_text())
        self.assertEqual(saved["status"], "source_complete")

    def test_arxiv_phase_does_not_preserve_m1_archive_certification_from_cached_s2(self):
        runner = FastProductionRunner(local_root=self.local, drive_root=self.drive)
        archive = runner.archives_dir / f"cohort_{self.cohort:04d}_corpus.tar.gz"
        archive.write_bytes(b"archive bytes")
        manifest_path = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))
        manifest_path.write_text(json.dumps({
            "archive_sha256": runner._compute_sha256(archive),
            "archive_state": "m1_handoff_verified",
        }), encoding="utf-8")
        with patch.object(runner, "count_source_completed_papers", return_value=100):
            m1_phases._mark_archive_arxiv_only(runner, self.cohort)
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["archive_state"], "source_verified")
        self.assertEqual(saved["phase_certification"], "arxiv_only")
        self.assertEqual(archive.read_bytes(), b"archive bytes")

    def test_audit_blocks_export_when_source_or_s2_evidence_is_missing(self):
        with patch("src.export.export_official_datasets") as export:
            result = m1_phases.run_m1_audit_member(
                MSSV,
                drive_root=self.drive,
                local_root=self.local,
                cohort_limit=1,
                budget_seconds=60,
            )
        self.assertFalse(result["is_reconciled"])
        self.assertEqual(result["export_status"], "blocked")
        self.assertFalse(result["s2_issues"] == [])
        export.assert_not_called()

    def test_audit_blocks_export_when_kaggle_discrepancy_checksum_is_invalid(self):
        from src.kaggle_candidates import persist_discrepancy_evidence

        self.drive.mkdir(parents=True, exist_ok=True)
        phase_root = m1_phases._persist_selection(self.drive, self.selection)
        evidence = {
            "schema_version": 1, "session_id": "tampered-session",
            "arxiv_id": self.selection["pairs"][0][1],
            "mssv": MSSV, "decision": "use_arxiv_latest",
        }
        pointer = persist_discrepancy_evidence(
            evidence,
            evidence_root=phase_root / "arxiv" / "kaggle_discrepancies",
            checkpoint_root=phase_root / "arxiv" / "kaggle_discrepancy_checkpoints",
            session_id="tampered-session",
        )
        Path(pointer["artifact_path"]).with_suffix(".json.sha256").write_text("0" * 64)
        with patch("src.export.export_official_datasets") as export:
            result = m1_phases.run_m1_audit_member(
                MSSV, drive_root=self.drive, local_root=self.local,
                cohort_limit=1, budget_seconds=60,
            )
        self.assertEqual(result["kaggle_evidence"]["status"], "invalid")
        self.assertEqual(result["kaggle_evidence_issues"][0]["issue"],
                         "kaggle_discrepancy_evidence_invalid")
        self.assertEqual(result["export_status"], "blocked")
        export.assert_not_called()

    def test_successful_audit_reconciles_s2_without_changing_source_bytes(self):
        found_id = self.selection["pairs"][0][1]

        def fetch_metadata(ids, **_kwargs):
            return [_metadata(arxiv_id, found=arxiv_id == found_id) for arxiv_id in ids]

        def fetch_references(arxiv_id, **_kwargs):
            return _references(arxiv_id, found=arxiv_id == found_id)

        with (
            patch.object(semantic_scholar, "RateLimiter", return_value=object()),
            patch.object(semantic_scholar, "fetch_metadata", side_effect=fetch_metadata),
            patch.object(semantic_scholar, "fetch_references", side_effect=fetch_references),
            patch.object(semantic_scholar, "_key_from_environment", return_value=None),
        ):
            s2_result = m1_phases.run_m1_s2_member(
                MSSV,
                drive_root=self.drive,
                local_root=self.local,
                cohort_limit=1,
                budget_seconds=120,
            )
        self.assertFalse(s2_result["stopped"])

        runner = FastProductionRunner(local_root=self.local, drive_root=self.drive)
        for member, arxiv_id in self.selection["pairs"]:
            if arxiv_id != found_id:
                runner.store.record_discovery({
                    "mssv": member,
                    "arxiv_id": arxiv_id,
                    "status": "not_found",
                })
                continue
            runner.store.record_discovery({
                "mssv": member,
                "arxiv_id": arxiv_id,
                "status": "found",
                "version_status": "resolved",
                "version_ids": [arxiv_id + "v1"],
                "latest_version_id": arxiv_id + "v1",
                "title": "Verified arXiv title",
                "authors": ["Verified Author"],
                "published": "2020-01-02",
                "updated": "2020-01-02",
                "missing_fields": [],
            })
            destination = runner.store.begin_version(member, arxiv_id, "v1")
            assert destination is not None
            destination.mkdir(parents=True, exist_ok=True)
            tex = destination / "main.tex"
            tex.write_bytes(b"\\documentclass{article}\\begin{document}fixture\\end{document}")
            before = tex.read_bytes()
            runner.store.record_source_result({
                "mssv": member,
                "arxiv_id": arxiv_id,
                "versioned_id": arxiv_id + "v1",
                "status": "success",
                "files": ["main.tex"],
                "downloaded_bytes": len(before),
                "error": None,
            })

        result = m1_phases.run_m1_audit_member(
            MSSV,
            drive_root=self.drive,
            local_root=self.local,
            cohort_limit=1,
            budget_seconds=120,
        )
        self.assertTrue(result["is_reconciled"], json.dumps({
            "source_issues": result.get("source_issues"),
            "s2_issues": result.get("s2_issues"),
            "merge_conflicts": result.get("merge_conflicts"),
            "statistics": result.get("statistics"),
            "cohorts": result.get("cohorts"),
        }, default=str))
        self.assertEqual(result["merged_s2_ids"], 1)
        self.assertEqual(result["export_status"], "exported")
        self.assertTrue(result["export"]["is_valid"])
        self.assertEqual(result["export"]["papers_count"], 1)
        self.assertFalse(result["bbl_audit"]["source_corpus_modified"])
        self.assertTrue(Path(result["export"]["output_dir"]).is_dir())
        after = runner.store.paper_path(MSSV, found_id) / "v1" / "main.tex"
        self.assertEqual(after.read_bytes(), before)
        metadata = runner.store.read_metadata(MSSV, found_id)
        self.assertEqual(metadata["provenance"]["arxiv"]["snapshot"]["status"], "found")
        self.assertEqual(metadata["provenance"]["semantic_scholar"]["snapshot"]["status"], "found")


if __name__ == "__main__":
    unittest.main()

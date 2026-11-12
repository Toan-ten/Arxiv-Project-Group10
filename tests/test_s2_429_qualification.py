"""Regression tests for Semantic Scholar HTTP 429 handling, retry/wait-budget exhaustion,
data preservation, clean resumption, secret scrubbing, and configuration validation.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from typing import Tuple, Dict, List, Optional
from urllib.error import HTTPError

from src import batch, benchmark_runner, pipeline, s2_integration as adapter, semantic_scholar as s2
from src.fast_production import FastProductionRunner, cohort_pairs
from src.storage import CorpusStorage, TERMINAL, arxiv_id_to_folder_key

import test_pipeline as fx
from test_semantic_scholar import Response, http_error, paper as s2_paper
from test_semantic_scholar_references import reference, page

PAIR = ("24120260", "2501.02642")
OTHER = ("24120260", "2501.02643")
THIRD = ("24120260", "2501.02644")


class TestSemanticScholar429Qualification(unittest.TestCase):
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response
    manifest = fx.PipelineTests.manifest
    paper_path = fx.PipelineTests.paper_path

    @staticmethod
    def write_fresh_metadata(paper_dir, mssv="24120010", arxiv_id="2508.07872"):
        retrieved_at = "2026-11-09T00:00:00+00:00"
        payload = {
            "contract_version": 2, "arxiv_id": arxiv_id, "mssv": mssv,
            "status": "partial", "title": "Paper", "authors": ["Author"],
            "submission_date": "2025-08-01", "revised_dates": [], "venue": None,
            "version_status": "resolved", "versions": [{"version": "v1",
                "versioned_id": f"{arxiv_id}v1", "date": "2025-08-01",
                "date_status": "verified"}], "missing_fields": [],
            "provenance": {
                "arxiv": {"query_identifier": arxiv_id, "retrieved_at": retrieved_at,
                    "snapshot": {"mssv": mssv, "arxiv_id": arxiv_id, "status": "found"}},
                "semantic_scholar": None, "version_history": {"provider": "arxiv_atom_versioned"},
            },
        }
        (paper_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")

    def setUp(self):
        fx.PipelineTests.setUp(self)
        self.s2_outcomes = []
        self.s2_requests = []
        self.s2_starts = []
        self.transport = Mock()
        self.transport.open.side_effect = self._s2_open

        patches = [
            patch.object(s2, "_opener", self.transport),
            patch.object(s2.time, "monotonic", lambda: self.now),
            patch.object(s2.time, "sleep", self.sleep),
            patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": ""}),
            patch.object(socket, "create_connection", side_effect=AssertionError("Real network forbidden")),
            patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._close_remaining)

    def _close_remaining(self):
        for outcome in self.outcomes + self.s2_outcomes:
            if hasattr(outcome, "close"):
                outcome.close()

    def _s2_open(self, request, timeout):
        self.s2_starts.append(self.now)
        self.s2_requests.append(request)
        self.assertTrue(self.s2_outcomes, "Unexpected S2 request without outcome queued")
        val = self.s2_outcomes.pop(0)
        if isinstance(val, BaseException):
            raise val
        return val

    def test_s2_metadata_429_retry_exhaustion_stops_batch(self):
        """When S2 metadata returns 429 on all retries, pipeline stops cleanly on that paper."""
        pairs = [PAIR, OTHER]
        # ArXiv discovery & source succeed for PAIR
        self.outcomes.extend([
            fx.xml_response(PAIR[1], 1),
            fx.Response(fx.TEX),
        ])
        # S2 metadata returns 429 across 3 attempts
        self.s2_outcomes.extend([
            http_error(429),
            http_error(429),
            http_error(429),
        ])

        s2_cfg = dict(timeout=15.0, max_attempts=3, min_interval=1.0, reference_limit=100)
        report = pipeline.run_selected(
            pairs, self.root, timeout=15.0, max_attempts=1, versions=("v1",),
            run_id="s2-429-exhaustion-test", s2_config=s2_cfg, revision_history=False,
        )

        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"]["type"], "http_policy")
        self.assertEqual(report["stop_reason"]["stage"], "s2_metadata")
        self.assertEqual(report["stop_reason"]["http_status"], 429)
        self.assertEqual(report["stop_reason"]["attempts"], 3)

        # Paper 1 state on disk
        store = CorpusStorage(self.root)
        m1 = store.read_manifest(*PAIR)
        self.assertEqual(m1["discovery_status"], "found")
        self.assertEqual(m1["versions"]["v1"]["status"], "success")
        self.assertEqual(m1["metadata_status"], "failed")
        self.assertEqual(m1["references_status"], "not_attempted")
        self.assertEqual(m1["status"], "partial")

        # Paper 2 was never queried
        self.assertFalse(store.paper_path(*OTHER).exists())

    def test_s2_metadata_429_wait_budget_exhaustion(self):
        """When Retry-After exceeds max_retry_wait, wait budget exhaustion triggers 429 stop."""
        pairs = [PAIR]
        self.outcomes.extend([
            fx.xml_response(PAIR[1], 1),
            fx.Response(fx.TEX),
        ])
        # S2 returns 429 with Retry-After 120 seconds
        self.s2_outcomes.append(http_error(429, 120))

        s2_cfg = dict(timeout=15.0, max_attempts=3, min_interval=1.0, reference_limit=100, max_retry_wait=10.0)
        report = pipeline.run_selected(
            pairs, self.root, timeout=15.0, max_attempts=1, versions=("v1",),
            run_id="s2-budget-test", s2_config=s2_cfg, revision_history=False,
        )

        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"]["stage"], "s2_metadata")
        self.assertEqual(report["stop_reason"]["http_status"], 429)
        self.assertEqual(report["stop_reason"]["attempts"], 1)
        self.assertEqual(report["stop_reason"]["retry_wait_budget_seconds"], 10.0)
        # Attempted only 1 request because 120s exceeds 10s budget
        self.assertEqual(len(self.s2_requests), 1)

    def test_s2_429_preserves_sources_and_completed_papers_then_resumes_cleanly(self):
        """End-to-end verification:
        Session 1: Paper 1 completes. Paper 2 downloads TeX source, then hits S2 429.
        Session 2: Resume. Paper 1 is skipped (0 HTTP). Paper 2 TeX source is skipped (0 HTTP),
                   S2 metadata & references succeed. Paper 3 runs and completes.
        """
        pairs = [PAIR, OTHER, THIRD]
        s2_cfg = dict(timeout=15.0, max_attempts=1, min_interval=1.0, reference_limit=100)

        # --- SESSION 1 ---
        # Paper 1: ArXiv discovery + source + S2 metadata + references
        self.outcomes.extend([
            fx.xml_response(PAIR[1], 1),
            fx.Response(fx.TEX),
        ])
        self.s2_outcomes.extend([
            Response([s2_paper(PAIR[1])]),
            page([]),
        ])
        # Paper 2: ArXiv discovery + source succeed, then S2 metadata returns 429
        self.outcomes.extend([
            fx.xml_response(OTHER[1], 1),
            fx.Response(fx.TEX),
        ])
        self.s2_outcomes.append(http_error(429))

        report1 = pipeline.run_selected(
            pairs, self.root, timeout=15.0, max_attempts=1, versions=("v1",),
            run_id="session-1", s2_config=s2_cfg, revision_history=False,
        )
        self.assertTrue(report1["stopped"])
        self.assertEqual(report1["stop_reason"]["http_status"], 429)

        # Inspect on-disk state after Session 1
        store = CorpusStorage(self.root)
        m1 = store.read_manifest(*PAIR)
        self.assertEqual(m1["status"], "complete")
        self.assertEqual(m1["metadata_status"], "complete")
        self.assertEqual(m1["references_status"], "complete")

        m2 = store.read_manifest(*OTHER)
        self.assertEqual(m2["status"], "partial")
        self.assertEqual(m2["versions"]["v1"]["status"], "success")
        self.assertEqual(m2["metadata_status"], "failed")
        self.assertEqual(m2["references_status"], "not_attempted")
        v1_dir = store.paper_path(*OTHER) / "v1"
        self.assertTrue(v1_dir.exists())
        files = list(v1_dir.iterdir())
        self.assertTrue(len(files) > 0, "Source files must exist in v1")
        source_mtime_before = max(f.stat().st_mtime_ns for f in files)

        self.assertFalse(store.paper_path(*THIRD).exists())

        # Reset request trackers
        self.requests.clear()
        self.s2_requests.clear()

        # --- SESSION 2 (RESUME) ---
        # When resuming:
        # Paper 1: already complete -> 0 ArXiv calls, 0 S2 calls.
        # Paper 2: discovery cached, source exists & valid -> 0 ArXiv source calls!
        #          S2 metadata retried -> success. S2 references -> success.
        # Paper 3: fresh -> ArXiv discovery + source + S2 metadata + references.
        self.s2_outcomes.extend([
            Response([s2_paper(OTHER[1])]),
            page([]),
        ])
        self.outcomes.extend([
            fx.xml_response(THIRD[1], 1),
            fx.Response(fx.TEX),
        ])
        self.s2_outcomes.extend([
            Response([s2_paper(THIRD[1])]),
            page([]),
        ])

        report2 = pipeline.run_selected(
            pairs, self.root, timeout=15.0, max_attempts=1, versions=("v1",),
            run_id="session-2", s2_config=s2_cfg, revision_history=False,
        )
        self.assertFalse(report2["stopped"])

        # Verification of no redundant I/O:
        # Paper 2's source files were NEVER re-downloaded or overwritten
        source_mtime_after = max(f.stat().st_mtime_ns for f in (store.paper_path(*OTHER) / "v1").iterdir())
        self.assertEqual(source_mtime_before, source_mtime_after, "Paper 2 source files must not be rewritten on resume")

        # All 3 papers are now complete
        for p in pairs:
            m = store.read_manifest(*p)
            self.assertEqual(m["status"], "complete", f"Paper {p[1]} must be complete")
            self.assertEqual(m["metadata_status"], "complete")
            self.assertEqual(m["references_status"], "complete")

    def test_s2_limiter_global_pacing_across_papers_with_clock(self):
        """Verify that RateLimiter paces S2 calls across multiple distinct papers with measured intervals."""
        pairs = [PAIR, OTHER]
        for p in pairs:
            self.outcomes.extend([fx.xml_response(p[1], 1), fx.Response(fx.TEX)])
            self.s2_outcomes.extend([Response([s2_paper(p[1])]), page([])])

        min_interval = 2.5
        s2_cfg = dict(timeout=15.0, max_attempts=1, min_interval=min_interval, reference_limit=100)
        report = pipeline.run_selected(
            pairs, self.root, timeout=15.0, max_attempts=1, versions=("v1",),
            run_id="global-pacing-test", s2_config=s2_cfg, revision_history=False,
        )
        self.assertFalse(report["stopped"])
        self.assertEqual(len(self.s2_requests), 4)

        # Confirm measured elapsed intervals using controlled monotonic clock
        self.assertEqual(len(self.s2_starts), 4)
        for i in range(1, len(self.s2_starts)):
            interval = self.s2_starts[i] - self.s2_starts[i - 1]
            self.assertGreaterEqual(
                interval, min_interval,
                f"Interval between request {i-1} and {i} ({interval:.2f}s) must be at least min_interval ({min_interval:.2f}s)"
            )

    def test_s2_config_none_completely_disables_s2_requests(self):
        """Prove that s2_config=None executes a pure source-only run without making any S2 requests."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        p1 = cohort_pairs(1)[0]
        self.outcomes.extend([fx.xml_response(p1[1], 1), fx.Response(fx.TEX)])

        report = runner.run_cohort(1, resume=False, revision_history=True, s2_config=None, max_ids=1)

        # S2 transport must NEVER have been called
        self.assertEqual(len(self.s2_requests), 0, "No S2 requests must be sent when s2_config is None")

        # The paper must have source downloaded, but S2 unattempted (zero S2 provenance)
        m = runner.store.read_manifest(*p1)
        self.assertEqual(m["discovery_status"], "found")
        self.assertEqual(m["versions"]["v1"]["status"], "success")
        self.assertEqual(m["references_status"], "not_attempted")
        meta = runner.store.read_metadata(*p1)
        self.assertIsNone(meta["provenance"]["semantic_scholar"], "S2 provenance must be None")

    def test_run_cohort_s2_repeated_429_then_resume_success(self):
        """Verify: S2 repeated 429 stops run_cohort cleanly, preserves archive and checksums,
        and subsequent resume retries the 429 paper and completes all papers.
        """
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        pairs = cohort_pairs(1)[:2]
        p1, p2 = pairs[0], pairs[1]
        s2_cfg = dict(timeout=15.0, max_attempts=3, min_interval=1.0, reference_limit=100)

        # --- SESSION 1: p1 completes, p2 downloads source then hits 3x 429 on S2 ---
        self.outcomes.extend([
            fx.xml_response(p1[1], 1), fx.Response(fx.TEX),
            fx.xml_response(p2[1], 1), fx.Response(fx.TEX),
        ])
        self.s2_outcomes.extend([
            Response([s2_paper(p1[1])]), page([]),
            http_error(429), http_error(429), http_error(429),
        ])

        report1 = runner.run_cohort(1, resume=False, revision_history=True, s2_config=s2_cfg, max_ids=2)
        self.assertTrue(report1["fast_runner"]["stopped"])
        self.assertEqual(report1["fast_runner"]["stop_reason"]["http_status"], 429)

        # Check archive generation after stopping
        arch_path = Path(report1["fast_runner"]["archive_path"])
        self.assertTrue(arch_path.exists(), "Archive must be generated when stopped")
        arch_sha_stopped = runner._compute_sha256(arch_path)

        # Verify on-disk paper states
        m1 = runner.store.read_manifest(*p1)
        self.assertEqual(m1["status"], "complete")

        m2 = runner.store.read_manifest(*p2)
        self.assertEqual(m2["status"], "partial")
        self.assertEqual(m2["metadata_status"], "failed", "429 paper must be marked failed, not complete")
        p2_tex = list((runner.store.paper_path(*p2) / "v1").iterdir())[0]
        p2_sha_before = runner._compute_sha256(p2_tex)

        # --- SESSION 2: Resume with S2 succeeding ---
        self.s2_outcomes.extend([
            Response([s2_paper(p2[1])]), page([]),
        ])

        report2 = runner.run_cohort(1, resume=True, revision_history=True, s2_config=s2_cfg, max_ids=2)
        self.assertFalse(report2["fast_runner"]["stopped"])
        self.assertEqual(report2["fast_runner"]["completed_papers"], 2)

        # Confirm p2 source was NOT modified or re-downloaded
        p2_sha_after = runner._compute_sha256(p2_tex)
        self.assertEqual(p2_sha_before, p2_sha_after, "Source checksum must not change on resume")

        # Confirm p2 was retried and now completed
        m2_final = runner.store.read_manifest(*p2)
        self.assertEqual(m2_final["status"], "complete")
        self.assertEqual(m2_final["metadata_status"], "complete")
        self.assertEqual(m2_final["references_status"], "complete")

        # Final archive updated
        arch_sha_final = runner._compute_sha256(arch_path)
        self.assertNotEqual(arch_sha_stopped, arch_sha_final, "Final archive must be updated with enriched corpus")

    def test_enrich_cohort_s2_handles_429_cleanly_and_archives(self):
        """enrich_cohort_s2 catches _HttpPolicyStop on 429, archives progress to Drive, and returns stopped report."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)

        # Prepare a cohort where paper 1 has source complete (2508-07872 in cohort 1)
        paper_dir = runner.corpus_dir / "2508-07872"
        v1_dir = paper_dir / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        tex_content = r"\documentclass{article}\begin{document}Hello\end{document}"
        (v1_dir / "main.tex").write_text(tex_content, encoding="utf-8")
        sha = hashlib.sha256(tex_content.encode("utf-8")).hexdigest()
        (paper_dir / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": "2508.07872",
            "mssv": "24120010",
            "run_id": "run_1",
            "status": "partial",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "partial",
            "references_status": "not_attempted",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {
                "v1": {
                    "versioned_id": "2508.07872v1",
                    "status": "success",
                    "source_status": "success",
                    "http_status": 200,
                    "content_type": "application/x-eprint",
                    "source_url": "https://export.arxiv.org/src/2508.07872v1",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": ["main.tex"],
                    "file_checksums": {"main.tex": sha},
                    "kept_bytes": len(tex_content.encode("utf-8")),
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "run_1",
                    "completed_at": "2026-11-09T00:00:00Z",
                    "error": None,
                }
            },
        }), encoding="utf-8")

        self.write_fresh_metadata(paper_dir)

        fake_paper = dict(mssv="24120010", arxiv_id="2508.07872")
        with patch("src.pipeline._run_s2", side_effect=pipeline._HttpPolicyStop(fake_paper, "s2_metadata", 429, None)):
            report = runner.enrich_cohort_s2(1, max_ids=1)

        self.assertTrue(report.get("stopped"))
        self.assertEqual(report.get("stop_reason", {}).get("http_status"), 429)

        arch = Path(report["archive_path"])
        self.assertTrue(arch.exists(), "Archive must be created even when S2 stops on 429")

    def test_enrich_cohort_s2_exception_scrubs_secrets_from_stop_reason(self):
        """Verify that any diagnostic exception in enrich_cohort_s2 scrubs API credentials."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        secret_token = "SECRET_API_KEY_xyz987654321"
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret_token}):
            runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)

            # Prepare a source-complete paper
            paper_dir = runner.corpus_dir / "2508-07872"
            v1_dir = paper_dir / "v1"
            v1_dir.mkdir(parents=True, exist_ok=True)
            (v1_dir / "main.tex").write_text("Hello", encoding="utf-8")
            (paper_dir / "manifest.json").write_text(json.dumps({
                "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
                "run_id": "run_1", "status": "partial", "discovery_status": "found",
                "version_status": "resolved", "metadata_status": "partial",
                "references_status": "not_attempted", "updated_at": "2026-11-09T00:00:00Z",
                "versions": {"v1": {"versioned_id": "2508.07872v1", "status": "success", "files": ["main.tex"]}},
            }), encoding="utf-8")

            self.write_fresh_metadata(paper_dir)

            # Mock pipeline._run_s2 to raise an unexpected exception containing the secret token
            leak_msg = f"Failed to connect using token {secret_token} on remote endpoint"
            with patch("src.pipeline._run_s2", side_effect=RuntimeError(leak_msg)):
                report = runner.enrich_cohort_s2(1, max_ids=1)

            self.assertTrue(report.get("stopped"))
            stop_reason = report.get("stop_reason")
            self.assertIsNotNone(stop_reason)
            # Ensure the secret token NEVER appears in stop_reason or serialized report
            report_str = json.dumps(report)
            self.assertNotIn(secret_token, report_str, "Secret API key must be scrubbed from report")
            self.assertIn("[REDACTED]", stop_reason["message"])
            self.assertEqual(stop_reason["exception_type"], "RuntimeError")

    def test_s2_configuration_parameter_validation(self):
        """Verify that max_attempts, min_interval, max_retry_wait, backoff_base, backoff_cap
        are all accepted, validated, and normalized by s2_integration.configuration().
        """
        valid_cfg = {
            "timeout": 30.0,
            "max_attempts": 5,
            "min_interval": 3.5,
            "reference_limit": 100,
            "max_retry_wait": 300.0,
            "backoff_base": 5.0,
            "backoff_cap": 60.0,
        }
        res = adapter.configuration(valid_cfg)
        self.assertEqual(res["max_attempts"], 5)
        self.assertEqual(res["min_interval"], 3.5)
        self.assertEqual(res["max_retry_wait"], 300.0)
        self.assertEqual(res["backoff_base"], 5.0)
        self.assertEqual(res["backoff_cap"], 60.0)

        # max_attempts must be >= 1 integer
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, max_attempts=0))
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, max_attempts="5"))

        # min_interval must be positive finite float/int
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, min_interval=-1.0))
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, min_interval=0.0))

        # max_retry_wait must be positive finite float/int
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, max_retry_wait=-5.0))

        # backoff_base must not exceed backoff_cap
        with self.assertRaises(ValueError):
            adapter.configuration(dict(valid_cfg, backoff_base=70.0, backoff_cap=60.0))

    def test_source_only_resume_against_existing_s1_batch_fixture(self):
        """Phase 7 Gate: Prove that the source-only invocation (s2_config=None) against
        an existing s1 batch (with 4 complete papers, 9 source-complete papers, and paper 9 stopped at S2 429):
        1. Selects the new non-colliding batch_id 'fast-arxiv-0151-a6-h1-s0'.
        2. Sets can_resume=False and effective_resume=False for the new definition.
        3. Preserves the previous s1 batch directory, logs, and checkpoints completely.
        4. Reuses all existing source files without rewriting or re-downloading (SHA256 unchanged).
        5. Skips source downloads for all 9 source-complete papers.
        6. Sends ZERO requests to Semantic Scholar.
        7. Counts source-completed papers accurately without depending on S2 completion.
        """
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        c151_pairs = cohort_pairs(151)

        # 1. Simulate existing S2-enabled batch: fast-arxiv-0151-a6-h1-s1
        s1_batch_id = "fast-arxiv-0151-a6-h1-s1"
        s1_dir = runner.logs_dir / s1_batch_id
        s1_dir.mkdir(parents=True, exist_ok=True)
        s1_def = {
            "max_attempts": 6,
            "revision_history": True,
            "timeout": 15.0,
            "s2": adapter.configuration(dict(timeout=15.0, max_attempts=3, min_interval=1.0, reference_limit=100)),
        }
        (s1_dir / "batch.json").write_text(json.dumps({
            "schema_version": 1, "batch_id": s1_batch_id, "definition": s1_def,
        }), encoding="utf-8")
        (s1_dir / "marker.log").write_text("historical s1 log content", encoding="utf-8")

        # 2. Setup on-disk corpus fixture:
        # Papers 1..4: fully complete (both source and S2)
        # Papers 5..8: source complete + S2 complete
        # Paper 9 (2501.02650): source complete, but stopped at S2 429 (metadata_status="failed", status="partial")
        source_shas = {}
        for idx in range(9):
            mssv, arxiv_id = c151_pairs[idx]
            paper_dir = runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)
            v1_dir = paper_dir / "v1"
            v1_dir.mkdir(parents=True, exist_ok=True)
            tex_content = f"\\documentclass{{article}}\\begin{{document}}Paper {idx+1}: {arxiv_id}\\end{{document}}"
            tex_path = v1_dir / "main.tex"
            tex_path.write_text(tex_content, encoding="utf-8")
            tex_sha = runner._compute_sha256(tex_path)
            source_shas[arxiv_id] = tex_sha

            is_p9 = (idx == 8)
            p_status = "partial" if is_p9 else "complete"
            m_status = "failed" if is_p9 else "complete"
            r_status = "not_attempted" if is_p9 else "complete"

            manifest_payload = {
                "contract_version": 2, "arxiv_id": arxiv_id, "mssv": mssv, "run_id": "run_s1",
                "status": p_status, "discovery_status": "found", "version_status": "resolved",
                "metadata_status": m_status, "references_status": r_status,
                "updated_at": "2026-11-10T00:00:00Z",
                "versions": {
                    "v1": {
                        "versioned_id": f"{arxiv_id}v1", "status": "success", "source_status": "success",
                        "http_status": 200, "content_type": "application/x-eprint",
                        "source_url": f"https://export.arxiv.org/src/{arxiv_id}v1",
                        "files": ["main.tex"], "file_checksums": {"main.tex": tex_sha},
                        "kept_bytes": len(tex_content.encode("utf-8")), "extracted_bytes": None,
                        "removed_files": None, "attempts": 1, "run_id": "run_s1",
                        "archive_bytes": None, "archive_sha256": None,
                        "completed_at": "2026-11-10T00:00:00Z", "error": None,
                    }
                },
            }
            (paper_dir / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")

            # metadata.json
            s2_prov = None if is_p9 else dict(
                provider="semantic_scholar", query_identifier=f"ARXIV:{arxiv_id}",
                retrieved_at="2026-11-10T00:00:00Z", snapshot=dict(status="found", http_status=200, attempts=1),
            )
            if is_p9:
                s2_prov = dict(
                    provider="semantic_scholar", query_identifier=f"ARXIV:{arxiv_id}",
                    retrieved_at="2026-11-10T00:00:00Z", snapshot=dict(status="rate_limited", http_status=429, attempts=3),
                )
            meta_payload = {
                "contract_version": 2, "arxiv_id": arxiv_id, "mssv": mssv, "status": m_status,
                "title": f"Title {idx+1}", "authors": ["Author"], "submission_date": "2025-01-01T00:00:00Z",
                "revised_dates": [], "version_status": "resolved", "venue": None,
                "missing_fields": [],
                "versions": [dict(version="v1", versioned_id=f"{arxiv_id}v1", date="2025-01-01T00:00:00Z", date_status="verified")],
                "provenance": dict(
                    arxiv=dict(
                        query_identifier=arxiv_id,
                        retrieved_at="2026-11-10T00:00:00Z",
                        snapshot=dict(mssv=mssv, arxiv_id=arxiv_id, status="found"),
                    ),
                    semantic_scholar=s2_prov,
                    version_history=None,
                ),
            }
            (paper_dir / "metadata.json").write_text(json.dumps(meta_payload), encoding="utf-8")

            # references.json
            ref_payload = {
                "contract_version": 2, "arxiv_id": arxiv_id, "status": r_status,
                "provenance": dict(
                    provider="semantic_scholar",
                    query_identifier=f"ARXIV:{arxiv_id}",
                    s2_paper_id=None,
                    retrieved_at="2026-11-10T00:00:00Z",
                ),
                "coverage": dict(complete=(not is_p9), continuation=None, reported_total=0, retrieved_count=0),
                "items": [],
            }
            (paper_dir / "references.json").write_text(json.dumps(ref_payload), encoding="utf-8")

        # Confirm 9 source completed papers before invocation
        self.assertEqual(runner.count_source_completed_papers(151), 9)

        # 3. Simulate Paper 10 (2501.02651) discovery & source from arXiv
        p10 = c151_pairs[9]
        self.outcomes.extend([
            fx.xml_response(p10[1], 1),
            fx.Response(fx.TEX),
        ])

        # Capture pre-run state of historical s1 batch files
        s1_files_before = {p.relative_to(s1_dir): p.read_bytes() for p in s1_dir.rglob("*") if p.is_file()}
        self.requests.clear()

        # 4. Explicitly test batch resolution before running
        batch_id, can_resume = runner._resolve_batch_id(151, max_attempts=6, revision_history=True, timeout=15.0, s2_config=None)
        self.assertEqual(batch_id, "fast-arxiv-0151-a6-h1-s0", "Source-only run must select s0 batch ID")
        self.assertFalse(can_resume, "Different s2 configuration must safely set can_resume=False")

        # 5. Invoke the exact proposed source-only command (for 10 papers)
        report = runner.run_cohort(151, resume=True, revision_history=True, s2_config=None, max_ids=10)

        # 6. Verify assertions
        fast_stats = report.get("fast_runner", {})
        # A. Batch identity, diagnostics, and execution status
        self.assertFalse(fast_stats.get("stopped"))
        self.assertEqual(fast_stats.get("completed_papers"), 10)
        self.assertEqual(fast_stats.get("batch_id"), "fast-arxiv-0151-a6-h1-s0")
        self.assertFalse(fast_stats.get("effective_resume"))
        self.assertFalse(fast_stats.get("can_resume"))
        self.assertFalse(fast_stats.get("s2_enabled"))

        # B. Previous s1 batch directory, batch.json, logs, and checkpoints remain 100% byte-identical
        self.assertTrue(s1_dir.exists(), "Previous s1 batch directory must be preserved")
        s1_files_after = {p.relative_to(s1_dir): p.read_bytes() for p in s1_dir.rglob("*") if p.is_file()}
        self.assertEqual(s1_files_before, s1_files_after, "Historical s1 batch files must remain byte-identical")

        # C. New s0 batch directory created independently
        s0_dir = runner.logs_dir / "fast-arxiv-0151-a6-h1-s0"
        self.assertTrue(s0_dir.exists(), "New s0 batch directory must be created")
        self.assertNotEqual(s1_dir, s0_dir)

        # D. Assert exact list of papers planned as skip and assert paper 10 downloaded
        attempt_files = list((s0_dir / "attempts").glob("*.json"))
        self.assertEqual(len(attempt_files), 1, "Expected exactly 1 attempt file in s0_dir")
        attempt_data = json.loads(attempt_files[0].read_text(encoding="utf-8"))
        logged_papers = attempt_data.get("papers", [])
        self.assertEqual(len(logged_papers), 10)

        # Papers 1..9: verify they were all planned as skip and source was skipped
        for idx in range(9):
            p_data = logged_papers[idx]
            aid = c151_pairs[idx][1]
            self.assertEqual(p_data["arxiv_id"], aid)
            self.assertEqual(p_data["result"]["skipped"], ["v1"], f"Paper {aid} must have v1 planned as skip")
            self.assertEqual(p_data["result"]["source"]["v1"]["action"], "skipped")

        # Paper 10: verify downloaded
        p10_data = logged_papers[9]
        self.assertEqual(p10_data["arxiv_id"], p10[1])
        self.assertEqual(p10_data["result"]["skipped"], [])
        self.assertEqual(p10_data["result"]["source"]["v1"]["action"], "downloaded")

        # E. Assert actual arXiv HTTP request count and URLs
        # Only 2 requests: 1 for paper 10 metadata discovery + 1 for paper 10 source download
        self.assertEqual(len(self.requests), 2, "Expected exactly 2 arXiv requests (discovery + source for paper 10)")
        self.assertIn(f"id_list={p10[1]}", self.requests[0])
        self.assertIn(f"{p10[1]}v1", self.requests[1])
        # Assert no source or discovery request was sent for any of the 9 completed papers
        for idx in range(9):
            aid = c151_pairs[idx][1]
            for req_url in self.requests:
                self.assertNotIn(aid, req_url, f"No request should be made for existing paper {aid}")

        # F. All 9 previous source files and their SHA256 checksums remain identical
        for idx in range(9):
            aid = c151_pairs[idx][1]
            p_file = runner.corpus_dir / arxiv_id_to_folder_key(aid) / "v1" / "main.tex"
            self.assertTrue(p_file.exists())
            self.assertEqual(runner._compute_sha256(p_file), source_shas[aid], f"Source for {aid} must be unchanged")

        # G. ZERO requests sent to Semantic Scholar
        self.assertEqual(len(self.s2_requests), 0, "No S2 requests must be sent in source-only run")

        # H. Paper 10 source downloaded and verified
        self.assertEqual(runner.count_source_completed_papers(151), 10)

    def _create_mock_valid_archive(self, runner: FastProductionRunner, number: int = 151) -> Tuple[Path, str]:
        """Helper to create a cryptographically valid readable tar.gz archive with sidecars on Drive."""
        import tarfile, io
        arch_dir = runner.archives_dir
        arch_dir.mkdir(parents=True, exist_ok=True)
        arch_name = f"cohort_{number:04d}_corpus.tar.gz"
        arch_path = arch_dir / arch_name

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w:gz") as tar:
            data = b"dummy latex content for backup testing"
            ti = tarfile.TarInfo("test_paper/main.tex")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        arch_path.write_bytes(tar_stream.getvalue())
        orig_sha = runner._compute_sha256(arch_path)

        sha_path = arch_dir / f"{arch_name}.sha256"
        sha_path.write_text(f"{orig_sha}  {arch_name}\n", encoding="utf-8")

        manifest_path = arch_dir / f"cohort_{number:04d}_corpus.manifest.json"
        manifest_path.write_text(json.dumps({
            "cohort_number": number,
            "archive_filename": arch_name,
            "archive_sha256": orig_sha,
            "archive_state": "checkpoint_partial",
        }), encoding="utf-8")
        return arch_path, orig_sha

    def test_backup_cohort_archive_reuses_backup_when_archive_is_unchanged(self):
        """Repeated backups of identical bytes reuse the verified immutable copy."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        b1 = runner.backup_cohort_archive(151)
        self.assertIsNotNone(b1)
        b2 = runner.backup_cohort_archive(151)
        self.assertIsNotNone(b2)

        self.assertEqual(b1["backup_archive"], b2["backup_archive"])
        self.assertTrue(b2.get("reused"))
        p1 = Path(b1["backup_archive"])
        p2 = Path(b2["backup_archive"])
        self.assertTrue(p1.exists(), "First backup archive must exist")
        self.assertTrue(p2.exists(), "Second backup archive must exist simultaneously")
        self.assertEqual(runner._compute_sha256(p1), orig_sha)
        self.assertEqual(runner._compute_sha256(p2), orig_sha)

    def test_backup_cohort_archive_same_mtime_and_hash_reuses_copy(self):
        """Same mtime and same content do not create a redundant generation."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        fixed_mtime = 1700000000
        os.utime(arch_path, (fixed_mtime, fixed_mtime))

        b1 = runner.backup_cohort_archive(151)
        os.utime(arch_path, (fixed_mtime, fixed_mtime))
        b2 = runner.backup_cohort_archive(151)

        self.assertIn("1700000000", Path(b1["backup_archive"]).name)
        self.assertIn("1700000000", Path(b2["backup_archive"]).name)
        self.assertEqual(b1["backup_archive"], b2["backup_archive"])
        self.assertTrue(b2.get("reused"))
        self.assertTrue(Path(b1["backup_archive"]).exists())
        self.assertTrue(Path(b2["backup_archive"]).exists())

    def test_backup_cohort_archive_never_overwrites_existing_backup_file(self):
        """3. Existing backup files are never overwritten even if collision occurs."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        mtime = int(arch_path.stat().st_mtime)
        short_sha = orig_sha[:12]
        colliding_tar = runner.archives_dir / f"cohort_0151_corpus.bak_{mtime}_{short_sha}.tar.gz"
        colliding_content = b"pre-existing unmodifiable backup content"
        colliding_tar.write_bytes(colliding_content)

        b = runner.backup_cohort_archive(151)
        self.assertIsNotNone(b)
        self.assertNotEqual(b["backup_archive"], str(colliding_tar))
        self.assertEqual(colliding_tar.read_bytes(), colliding_content, "Pre-existing backup must remain untouched")
        self.assertTrue(Path(b["backup_archive"]).exists())

    def test_backup_cohort_archive_sidecars_match_digest_and_name(self):
        """4. Each backup has a matching SHA-256 sidecar and validated manifest sidecar."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        b = runner.backup_cohort_archive(151)
        backup_tar = Path(b["backup_archive"])
        backup_sha = Path(b["backup_sha256_file"])
        backup_manifest = Path(b["backup_manifest_file"])

        self.assertTrue(backup_sha.exists())
        sha_text = backup_sha.read_text(encoding="utf-8")
        self.assertIn(f"{orig_sha}  {backup_tar.name}", sha_text)

        self.assertTrue(backup_manifest.exists())
        mf_data = json.loads(backup_manifest.read_text(encoding="utf-8"))
        self.assertEqual(mf_data["archive_filename"], backup_tar.name)
        self.assertEqual(mf_data["archive_sha256"], orig_sha)
        self.assertTrue(mf_data.get("is_backup"))

    def test_backup_cohort_archive_fails_closed_on_corrupt_or_mismatched_sidecar(self):
        """5. A corrupted or mismatched sidecar causes a clear failure without leaving partial backup files."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        # A. Corrupt sha256 sidecar
        sha_file = runner.archives_dir / "cohort_0151_corpus.tar.gz.sha256"
        sha_file.write_text("0123456789abcdef" * 4 + "  cohort_0151_corpus.tar.gz\n", encoding="utf-8")

        with self.assertRaises(ValueError) as cm:
            runner.backup_cohort_archive(151)
        self.assertIn("Original sha256 sidecar is corrupt or mismatched", str(cm.exception))

        # Ensure no dangling backup files created
        backup_files = list(runner.archives_dir.glob("*.bak_*"))
        self.assertEqual(backup_files, [], "Fail-closed must leave zero backup files")

        # B. Corrupt manifest sidecar
        sha_file.write_text(f"{orig_sha}  cohort_0151_corpus.tar.gz\n", encoding="utf-8")
        manifest_file = runner.archives_dir / "cohort_0151_corpus.manifest.json"
        manifest_file.write_text(json.dumps({"archive_sha256": "bad_sha", "cohort_number": 151}), encoding="utf-8")

        with self.assertRaises(ValueError) as cm:
            runner.backup_cohort_archive(151)
        self.assertIn("Original manifest sidecar archive_sha256 mismatch", str(cm.exception))

        backup_files = list(runner.archives_dir.glob("*.bak_*"))
        self.assertEqual(backup_files, [])

    def test_backup_cohort_archive_readable_after_primary_replaced(self):
        """6. Historical backup remains readable after the primary archive is replaced."""
        import tarfile
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        arch_path, orig_sha = self._create_mock_valid_archive(runner, 151)

        b = runner.backup_cohort_archive(151)
        backup_tar = Path(b["backup_archive"])

        # Replace primary archive with completely new content
        arch_path.write_bytes(b"completely different primary archive content")
        self.assertNotEqual(runner._compute_sha256(arch_path), orig_sha)

        # Historical backup remains valid, untouched and extractable
        self.assertEqual(runner._compute_sha256(backup_tar), orig_sha)
        with tarfile.open(backup_tar, "r:gz") as tar:
            members = tar.getnames()
            self.assertIn("test_paper/main.tex", members)
            f = tar.extractfile("test_paper/main.tex")
            self.assertEqual(f.read(), b"dummy latex content for backup testing")

    def test_count_m1_completed_papers_integration_with_s2_stages(self):
        """BUG-009: Test count_m1_completed_papers integration across multi-stage M1 states."""
        local_dir = Path(tempfile.mkdtemp(prefix="test_local_"))
        drive_dir = Path(tempfile.mkdtemp(prefix="test_drive_"))
        self.addCleanup(lambda: shutil.rmtree(local_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(drive_dir, ignore_errors=True))

        runner = FastProductionRunner(local_dir, drive_dir, timeout=15.0)
        c151_pairs = cohort_pairs(151)
        store = runner.store

        # 1. Initially, all 100 papers are missing/unprocessed
        init_stats = runner.count_m1_completed_papers(151)
        self.assertEqual(init_stats["found_m1_complete"], 0)
        self.assertEqual(init_stats["confirmed_not_found"], 0)
        self.assertEqual(init_stats["unprocessed"], 100)
        self.assertEqual(init_stats["integrity_failures"], 0)
        self.assertEqual(init_stats["discovery_failed"], 0)
        self.assertFalse(init_stats["is_cohort_complete"])

        # 2. Papers 0..3 (4 papers): Complete M1 (source + S2 metadata + S2 references)
        #    Papers 4..8 (5 papers): Source complete, but S2 metadata & references pending
        #    Paper 9: Negative discovery confirmed (not_found on arXiv)
        def _write_paper(mssv, aid, complete_m1=True):
            store.record_discovery({
                "mssv": mssv,
                "arxiv_id": aid,
                "status": "found",
                "version_status": "resolved",
                "version_ids": [f"{aid}v1"],
                "latest_version_id": f"{aid}v1",
                "title": f"Paper Title {aid}",
                "authors": ["Author One"],
                "published": "2025-08-01",
                "updated": "2025-08-01",
                "missing_fields": [],
            })
            dest = store.begin_version(mssv, aid, "v1")
            dest.mkdir(parents=True, exist_ok=True)
            content = b"\\documentclass{article}\\begin{document}Hello\\end{document}\n"
            (dest / "main.tex").write_bytes(content)
            store.record_source_result({
                "mssv": mssv,
                "arxiv_id": aid,
                "versioned_id": f"{aid}v1",
                "status": "success",
                "files": ["main.tex"],
                "downloaded_bytes": len(content),
                "error": None,
            })
            if complete_m1:
                meta = store.read_metadata(mssv, aid)
                meta["status"] = "complete"
                meta["title"] = f"Paper Title {aid}"
                meta["authors"] = ["Author One"]
                meta["submission_date"] = "2025-08-01"
                meta["revised_dates"] = []
                meta["venue"] = "NeurIPS"
                meta["missing_fields"] = []
                meta["version_status"] = "resolved"
                meta["versions"] = [{"version": "v1", "versioned_id": f"{aid}v1", "date": "2025-08-01", "date_status": "verified"}]
                meta["provenance"]["semantic_scholar"] = {
                    "query_identifier": aid,
                    "s2_paper_id": f"s2_{aid}",
                    "retrieved_at": "2025-08-01T00:00:00Z",
                    "snapshot": {"arxiv_id": aid, "status": "found"},
                }
                store.save_metadata(mssv, aid, meta)

                refs = store.read_references(mssv, aid)
                refs["status"] = "complete"
                refs["provenance"]["query_identifier"] = aid
                refs["provenance"]["retrieved_at"] = "2025-08-01T00:00:00Z"
                refs["coverage"] = {
                    "complete": True,
                    "continuation": None,
                    "reported_total": 1,
                    "retrieved_count": 1,
                }
                refs["items"] = [{
                    "arxiv_id": "2101.00001",
                    "title": "Ref Title",
                    "authors": ["Author Two"],
                    "submission_date": "2021-01-01",
                    "publication_date": "2021-01-01",
                    "year": 2021,
                    "venue": "ICML",
                    "s2_paper_id": f"s2_ref_{aid}",
                    "missing_fields": [],
                    "raw": {},
                }]
                store.save_references(mssv, aid, refs)

        for idx in range(4):
            mssv, aid = c151_pairs[idx]
            _write_paper(mssv, aid, complete_m1=True)

        for idx in range(4, 9):
            mssv, aid = c151_pairs[idx]
            _write_paper(mssv, aid, complete_m1=False)

        # Paper 9: negative discovery
        mssv9, aid9 = c151_pairs[9]
        store.record_discovery({
            "mssv": mssv9,
            "arxiv_id": aid9,
            "status": "not_found",
            "version_status": "not_applicable",
            "version_ids": [],
            "latest_version_id": None,
            "title": None,
            "authors": None,
            "published": None,
            "updated": None,
            "missing_fields": [],
        })

        # Verify counts:
        # found_m1_complete: 4 (papers 0..3)
        # confirmed_not_found: 1 (paper 9)
        # unprocessed: 5 (papers 4..8) + 90 (unprocessed papers 10..99) = 95
        # integrity_failures: 0
        # discovery_failed: 0
        # is_cohort_complete: False
        stats = runner.count_m1_completed_papers(151)
        self.assertEqual(stats["found_m1_complete"], 4)
        self.assertEqual(stats["confirmed_not_found"], 1)
        self.assertEqual(stats["unprocessed"], 95)
        self.assertEqual(stats["integrity_failures"], 0)
        self.assertEqual(stats["discovery_failed"], 0)
        self.assertFalse(stats["is_cohort_complete"])

        # Source count must be exactly 10 (9 with TeX + 1 not_found)
        self.assertEqual(runner.count_source_completed_papers(151), 10)

        # 3. Test integrity failure detection: tamper paper 0 main.tex
        p0_tex = runner.corpus_dir / arxiv_id_to_folder_key(c151_pairs[0][1]) / "v1" / "main.tex"
        p0_tex.write_bytes(b"tampered content")

        tampered_stats = runner.count_m1_completed_papers(151)
        self.assertEqual(tampered_stats["integrity_failures"], 1)
        self.assertEqual(tampered_stats["found_m1_complete"], 3)
        self.assertFalse(tampered_stats["is_cohort_complete"])

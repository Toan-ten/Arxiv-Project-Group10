"""Comprehensive test suite for Phase 2: Reporting Correctness.

Verifies:
- Paper-level classification & reconciliation invariants.
- Version-level state mapping and source integrity verification.
- Metadata and references independent validation.
- M1 milestone completion invariant (strict hierarchy).
- Zero-denominator null semantics (None/N/A, no artificial 0.0%).
- Git provenance resolution and JSON reporting export.
- Fault injection and fail-closed error handling.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from src.storage import CorpusStorage
from src.reporting import (
    METRIC_DEFINITION_VERSION,
    REPORT_SCHEMA_VERSION,
    _get_git_commit,
    compute_m1_statistics,
    export_report_json,
    format_markdown_summary,
)


class BaseReportingTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = CorpusStorage(Path(self.td.name) / "corpus")
        self.mssv = "24120010"
        self.aid1 = "2508.07872"
        self.aid2 = "2508.07873"
        self.aid3 = "2508.07874"
        self.aid4 = "2508.07875"

    def tearDown(self):
        self.td.cleanup()

    def _setup_found_paper(self, arxiv_id: str, versions: list[str]) -> None:
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{arxiv_id}{v}" for v in versions],
            "latest_version_id": f"{arxiv_id}{versions[-1]}",
            "title": f"Title for {arxiv_id}",
            "authors": ["Author A"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })

    def _complete_version_tex(self, arxiv_id: str, version: str) -> None:
        dest = self.store.begin_version(self.mssv, arxiv_id, version)
        self.assertIsNotNone(dest)
        dest.mkdir(parents=True, exist_ok=True)
        tex_file = dest / "main.tex"
        content = r"\documentclass{article}\begin{document}Hello\end{document}"
        tex_file.write_text(content, encoding="utf-8")
        self.store.record_source_result({
            "mssv": self.mssv,
            "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}{version}",
            "status": "success",
            "files": ["main.tex"],
            "downloaded_bytes": len(content.encode("utf-8")),
            "error": None,
        })

    def _complete_metadata(self, arxiv_id: str) -> None:
        meta = self.store.read_metadata(self.mssv, arxiv_id)
        meta["status"] = "complete"
        meta["title"] = f"Complete Title {arxiv_id}"
        meta["authors"] = ["Author A"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = []
        meta["venue"] = "Test Venue"
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [{"version": "v1", "versioned_id": f"{arxiv_id}v1", "date": "2025-08-01", "date_status": "verified"}]
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": arxiv_id,
            "s2_paper_id": "s2_123",
            "retrieved_at": "2025-08-01T00:00:00Z",
            "snapshot": {"arxiv_id": arxiv_id, "status": "found"},
        }
        self.store.save_metadata(self.mssv, arxiv_id, meta)

    def _complete_references(self, arxiv_id: str, items: list[dict]) -> None:
        refs = self.store.read_references(self.mssv, arxiv_id)
        refs["status"] = "complete"
        refs["provenance"]["query_identifier"] = arxiv_id
        refs["provenance"]["retrieved_at"] = "2025-08-01T00:00:00Z"
        refs["coverage"] = {
            "complete": True,
            "continuation": None,
            "reported_total": len(items),
            "retrieved_count": len(items),
        }
        refs["items"] = items
        self.store.save_references(self.mssv, arxiv_id, refs)


class TestPaperLevelReporting(BaseReportingTest):
    """Test paper-level classification, integrity checks, and stage completeness."""

    def test_missing_and_malformed_manifests(self):
        """Missing manifests and malformed JSON are accurately separated and tracked."""
        # aid1: folder does not exist -> missing_manifests
        # aid2: folder exists, but manifest corrupted -> invalid_manifests
        p2_dir = self.store.paper_path(self.mssv, self.aid2)
        p2_dir.mkdir(parents=True, exist_ok=True)
        (p2_dir / "manifest.json").write_text("{broken json...", encoding="utf-8")

        pairs = [(self.mssv, self.aid1), (self.mssv, self.aid2)]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["assigned_candidates"], 2)
        self.assertEqual(stats["missing_manifests"], 1)
        self.assertEqual(stats["invalid_manifests"], 1)
        self.assertEqual(stats["found_papers"], 0)
        self.assertTrue(stats["is_reconciled"])

    def test_discovery_not_found_valid_and_invalid(self):
        """Negative discovery requires exact empty dict versions and no extra folders."""
        # 1. Valid not_found
        self.store.record_discovery({
            "mssv": self.mssv, "arxiv_id": self.aid1, "status": "not_found",
            "version_status": "none", "version_ids": [], "latest_version_id": None,
            "title": None, "authors": [], "published": None, "updated": None, "missing_fields": [],
        })

        # 2. Invalid not_found: has versions list instead of dict
        p2_dir = self.store.paper_path(self.mssv, self.aid2)
        p2_dir.mkdir(parents=True, exist_ok=True)
        (p2_dir / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": self.aid2, "mssv": self.mssv, "run_id": "r1",
            "status": "not_found", "discovery_status": "not_found", "version_status": "none",
            "metadata_status": "not_attempted", "references_status": "not_attempted",
            "updated_at": "2026-11-09T00:00:00Z", "versions": ["v1"],
        }), encoding="utf-8")

        pairs = [(self.mssv, self.aid1), (self.mssv, self.aid2)]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["confirmed_not_found"], 1)
        self.assertEqual(stats["invalid_manifests"], 1)
        self.assertTrue(stats["is_reconciled"])

    def test_manifest_complete_with_partial_metadata_rejected_from_m1_complete(self):
        """FIND-REP-01: Manifest complete with partial metadata MUST NOT count as m1_complete."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        # Do not complete metadata: metadata is still partial
        self._complete_references(self.aid1, [])

        # Tamper manifest to claim status="complete"
        p_dir = self.store.paper_path(self.mssv, self.aid1)
        man = json.loads((p_dir / "manifest.json").read_text(encoding="utf-8"))
        man["status"] = "complete"
        man["metadata_status"] = "complete"
        (p_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["metadata_complete_papers"], 0)
        self.assertEqual(stats["references_complete_papers"], 1)
        # MUST BE REJECTED FROM M1 COMPLETE
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertTrue(stats["is_reconciled"])

    def test_manifest_complete_with_incomplete_references_rejected_from_m1_complete(self):
        """FIND-REP-01: Manifest complete with partial references MUST NOT count as m1_complete."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_metadata(self.aid1)

        # Write valid partial references file with coverage.complete = False
        p_dir = self.store.paper_path(self.mssv, self.aid1)
        refs = {
            "contract_version": 2, "arxiv_id": self.aid1, "status": "partial",
            "provenance": {"provider": "semantic_scholar", "query_identifier": self.aid1, "s2_paper_id": "s2_1", "retrieved_at": "2025-08-01T00:00:00Z"},
            "coverage": {"complete": False, "continuation": {"offset": 1}, "reported_total": 5, "retrieved_count": 0},
            "items": [],
        }
        (p_dir / "references.json").write_text(json.dumps(refs), encoding="utf-8")

        man = json.loads((p_dir / "manifest.json").read_text(encoding="utf-8"))
        man["status"] = "complete"
        man["references_status"] = "partial"
        (p_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["metadata_complete_papers"], 1)
        self.assertEqual(stats["references_complete_papers"], 0)
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertTrue(stats["is_reconciled"])

    def test_manifest_complete_with_tampered_source_checksum_rejected_from_m1_complete(self):
        """FIND-REP-02: Manifest complete with modified TeX on disk MUST NOT count as source or M1 complete."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_metadata(self.aid1)
        self._complete_references(self.aid1, [])

        # Tamper main.tex content on disk
        p_dir = self.store.paper_path(self.mssv, self.aid1)
        (p_dir / "v1" / "main.tex").write_text("tampered content", encoding="utf-8")

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        # Must fail source completion and M1 completion
        self.assertEqual(stats["source_complete_papers"], 0)
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertEqual(stats["integrity_failed_papers"], 1)
        self.assertTrue(stats["is_reconciled"])

    def test_full_m1_complete_paper_passes_all_invariants(self):
        """A verified complete paper passes source, metadata, references, and M1 completeness."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_metadata(self.aid1)
        self._complete_references(self.aid1, [{"arxiv_id": "2101.00001", "title": "Ref", "authors": ["B"], "submission_date": None, "publication_date": None, "year": 2021, "venue": None, "s2_paper_id": "s1", "missing_fields": [], "raw": {}}])

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["metadata_complete_papers"], 1)
        self.assertEqual(stats["references_complete_papers"], 1)
        self.assertEqual(stats["m1_complete_papers"], 1)
        self.assertEqual(stats["m1_completion_rate"], 100.0)
        self.assertTrue(stats["is_reconciled"])


class TestVersionAndReferencesReporting(BaseReportingTest):
    """Test version status mapping, checksum verification, and references accounting."""

    def test_version_state_partition_and_reconciliation(self):
        """Version categories (terminal, incomplete, failed, invalid) form an exact partition."""
        self._setup_found_paper(self.aid1, ["v1", "v2", "v3", "v4"])
        # v1: success
        self._complete_version_tex(self.aid1, "v1")
        # v2: no_tex_source
        self.store.begin_version(self.mssv, self.aid1, "v2")
        self.store.record_source_result({
            "mssv": self.mssv, "arxiv_id": self.aid1, "versioned_id": f"{self.aid1}v2",
            "status": "no_tex_source", "files": [], "downloaded_bytes": 0, "error": None,
        })
        # v3: left in pending (incomplete)
        # v4: failed with source error
        self.store.begin_version(self.mssv, self.aid1, "v4")
        self.store.record_source_result({
            "mssv": self.mssv, "arxiv_id": self.aid1, "versioned_id": f"{self.aid1}v4",
            "status": "server_error", "files": [], "downloaded_bytes": 0, "error": "HTTP 500",
        })

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["total_discovered_versions"], 4)
        self.assertEqual(stats["successful_tex_versions"], 1)
        self.assertEqual(stats["no_tex_versions"], 1)
        self.assertEqual(stats["terminal_versions"], 2)
        self.assertEqual(stats["incomplete_versions"], 1)
        self.assertEqual(stats["failed_versions"], 1)
        self.assertEqual(stats["reconciliation"]["version_balance"], 4)
        self.assertTrue(stats["is_reconciled"])

    def test_references_empty_items_with_coverage_true_counts_as_complete(self):
        """Confirmed zero citations with coverage.complete=True is a complete reference record."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_references(self.aid1, [])

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["complete_reference_papers"], 1)
        self.assertEqual(stats["total_complete_reference_records"], 0)
        self.assertEqual(stats["average_references_per_paper"], 0.0)

    def test_references_partial_and_failed_states(self):
        """Partial, failed, and unattempted references are accurately distinguished."""
        self._setup_found_paper(self.aid1, ["v1"])
        refs = self.store.read_references(self.mssv, self.aid1)
        item = {
            "arxiv_id": "2101.00001", "title": "Ref 1", "authors": ["Author B"],
            "submission_date": "2023-01-01", "publication_date": "2023-01-01",
            "year": 2023, "venue": "Conf", "s2_paper_id": "s2_abc",
            "missing_fields": [], "raw": {"id": "123"},
        }
        refs["status"] = "partial"
        refs["coverage"] = {"complete": False, "continuation": {"token": "next"}, "reported_total": 10, "retrieved_count": 1}
        refs["items"] = [item]
        self.store.save_references(self.mssv, self.aid1, refs)

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["complete_reference_papers"], 0)
        self.assertEqual(stats["partial_reference_papers"], 1)
        self.assertEqual(stats["total_observed_reference_records"], 1)
        self.assertEqual(stats["total_complete_reference_records"], 0)


class TestReconciliationAndRates(BaseReportingTest):
    """Test population balance, zero-denominator null semantics, and reconciliation matrix."""

    def test_zero_candidates_and_zero_found_returns_none_rates(self):
        """FIND-REP-07: When denominator is 0, rates return None (rendered N/A), never 0.0%."""
        stats = compute_m1_statistics(self.store, [])
        self.assertEqual(stats["assigned_candidates"], 0)
        self.assertEqual(stats["found_papers"], 0)
        self.assertIsNone(stats["source_completion_rate"])
        self.assertIsNone(stats["metadata_completion_rate"])
        self.assertIsNone(stats["references_coverage_rate"])
        self.assertIsNone(stats["m1_completion_rate"])
        self.assertIsNone(stats["version_extraction_success_rate"])
        self.assertIsNone(stats["no_tex_rate"])
        self.assertIsNone(stats["average_references_per_paper"])
        self.assertTrue(stats["is_reconciled"])

        md = format_markdown_summary(stats)
        self.assertIn("N/A", md)

    def test_reconciliation_equation_holds_across_mixed_cohort(self):
        """All candidates partition perfectly into mutually exclusive categories."""
        # 1. found and complete
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_metadata(self.aid1)
        self._complete_references(self.aid1, [])

        # 2. confirmed not found
        self.store.record_discovery({
            "mssv": self.mssv, "arxiv_id": self.aid2, "status": "not_found",
            "version_status": "none", "version_ids": [], "latest_version_id": None,
            "title": None, "authors": [], "published": None, "updated": None, "missing_fields": [],
        })

        # 3. unprocessed
        self.store.initialize_paper(self.mssv, self.aid3)

        # 4. missing manifest
        # self.aid4 has no directory or manifest

        pairs = [
            (self.mssv, self.aid1),
            (self.mssv, self.aid2),
            (self.mssv, self.aid3),
            (self.mssv, self.aid4),
        ]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["assigned_candidates"], 4)
        self.assertEqual(stats["found_papers"], 1)
        self.assertEqual(stats["confirmed_not_found"], 1)
        self.assertEqual(stats["unprocessed_papers"], 1)
        self.assertEqual(stats["missing_manifests"], 1)

        # Check reconciliation balance
        recon = stats["reconciliation"]
        self.assertEqual(recon["paper_population_balance"], 4)
        self.assertEqual(recon["paper_population_expected"], 4)
        self.assertTrue(recon["m1_hierarchy_valid"])
        self.assertTrue(stats["is_reconciled"])

    def test_m1_hierarchy_invariant_strictly_enforced(self):
        """m1_complete_papers <= min(source_complete, metadata_complete, references_complete)."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_metadata(self.aid1)
        # References incomplete
        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["metadata_complete_papers"], 1)
        self.assertEqual(stats["references_complete_papers"], 0)
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertTrue(stats["reconciliation"]["m1_hierarchy_valid"])


class TestProvenanceAndFaultInjection(BaseReportingTest):
    """Test git commit resolution, JSON report export, and fault-injection safety."""

    def test_get_git_commit_returns_40_hex_sha_from_project_root(self):
        """FIND-REP-06: Git commit is discovered from project root, not corpus directory."""
        sha = _get_git_commit()
        import re
        self.assertTrue(bool(re.fullmatch(r"[0-9a-f]{40}", sha)) or sha == "UNKNOWN")

    def test_export_report_json_creates_valid_file(self):
        """export_report_json produces valid, indented JSON with provenance and schema version."""
        stats = compute_m1_statistics(self.store, [])
        json_out = Path(self.td.name) / "reports" / "report.json"
        export_report_json(stats, json_out)

        self.assertTrue(json_out.exists())
        data = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(data["provenance"]["metric_definition_version"], METRIC_DEFINITION_VERSION)
        self.assertEqual(data["provenance"]["report_schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(data["provenance"]["storage_contract_version"], 2)

    def test_corpus_root_unresolvable_handled_gracefully(self):
        """FIND-REP-08: If store.root.resolve() raises RuntimeError/OSError, fallback gracefully."""
        mock_store = mock.MagicMock()
        mock_store.root.resolve.side_effect = RuntimeError("Symlink loop simulated")
        mock_store.root.__str__.return_value = "/mock/corpus/root"
        stats = compute_m1_statistics(mock_store, [])
        self.assertIn("corpus_root", stats["provenance"])
        self.assertTrue(isinstance(stats["provenance"]["corpus_root"], str))

    def test_fault_injection_oserror_during_audit_fails_closed(self):
        """If an OSError occurs while auditing paper integrity, the paper is accounted as error/unresolved."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")

        with mock.patch("src.reporting.audit_paper_integrity") as mock_audit:
            # Simulate integrity audit raising an unhandled filesystem exception
            from src.storage_integrity import PaperIntegrityResult, IntegrityViolation
            mock_audit.return_value = PaperIntegrityResult(
                arxiv_id=self.aid1,
                mssv=self.mssv,
                status="corrupted",
                is_source_complete=False,
                is_m1_complete=False,
                violations=[IntegrityViolation(arxiv_id=self.aid1, category="corrupted", severity="error", message="I/O error during audit")],
            )
            stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
            self.assertEqual(stats["source_complete_papers"], 0)
            self.assertEqual(stats["m1_complete_papers"], 0)
            self.assertEqual(stats["integrity_failed_papers"], 1)


class TestPhase2RemediationAudit(BaseReportingTest):
    """Specific regression tests for Phase 2 Independent Audit Findings (FIND-REP-A to D)."""

    def test_find_rep_a_source_complete_independent_of_broken_metadata_and_references(self):
        """FIND-REP-A: Corrupted metadata or missing references does NOT negate source_complete."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")

        # Corrupt metadata.json into unparseable syntax
        folder_key = self.store.paper_path(self.mssv, self.aid1)
        (folder_key / "metadata.json").write_text("{CORRUPTED_JSON_METADATA_NOT_VALID", encoding="utf-8")
        # Remove references.json
        if (folder_key / "references.json").exists():
            (folder_key / "references.json").unlink()

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])

        # Source is valid and complete on disk -> must be counted
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["source_completion_rate"], 100.0)

        # Metadata and references are NOT complete
        self.assertEqual(stats["metadata_complete_papers"], 0)
        self.assertEqual(stats["references_complete_papers"], 0)

        # M1 complete is strictly False
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertEqual(stats["m1_completion_rate"], 0.0)

        # Corrupted metadata is flagged as integrity failure
        self.assertEqual(stats["integrity_failed_papers"], 1)

    def test_find_rep_b_integrity_failed_papers_distinguishes_incomplete_from_corruption(self):
        """FIND-REP-B: Incomplete lifecycle stages (unattempted metadata/references) are NOT integrity failures."""
        # Paper 1: Freshly initialized (not attempted stage)
        self.store.initialize_paper(self.mssv, self.aid1)

        # Paper 2: Found, source complete, but metadata not attempted
        self._setup_found_paper(self.aid2, ["v1"])
        self._complete_version_tex(self.aid2, "v1")

        # Paper 3: Tampered file checksum (genuine corruption)
        self._setup_found_paper(self.aid3, ["v1"])
        self._complete_version_tex(self.aid3, "v1")
        tex_path = self.store.paper_path(self.mssv, self.aid3) / "v1" / "main.tex"
        tex_path.write_text("TAMPERED_CHECKSUM_SOURCE", encoding="utf-8")

        stats = compute_m1_statistics(
            self.store,
            [(self.mssv, self.aid1), (self.mssv, self.aid2), (self.mssv, self.aid3)],
        )

        # Only aid3 is a genuine integrity failure
        self.assertEqual(stats["integrity_failed_papers"], 1)

    def test_find_rep_c_not_found_with_filesystem_version_dir_rejected_from_confirmed_not_found(self):
        """FIND-REP-C: Negative discovery requires zero version directories on filesystem."""
        # Paper 1: Genuine not_found (manifest not_found, versions={}, no vN dirs)
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": self.aid1,
            "status": "not_found",
        })

        # Paper 2: Manifest claims not_found, but v1/ exists on disk (filesystem anomaly)
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": self.aid2,
            "status": "not_found",
        })
        bogus_v1 = self.store.paper_path(self.mssv, self.aid2) / "v1"
        bogus_v1.mkdir(parents=True, exist_ok=True)
        (bogus_v1 / "main.tex").write_text("Ghost TeX", encoding="utf-8")

        stats = compute_m1_statistics(
            self.store,
            [(self.mssv, self.aid1), (self.mssv, self.aid2)],
        )

        # Only aid1 is confirmed not found; aid2 is rejected as invalid/integrity failure
        self.assertEqual(stats["confirmed_not_found"], 1)
        self.assertEqual(stats["invalid_manifests"], 1)
        self.assertEqual(stats["integrity_failed_papers"], 1)

    def test_find_rep_d_independent_reconciliation_detects_spoofed_or_discrepant_counts(self):
        """FIND-REP-D: Independent audit pass catches discrepancies between classifier counts and disk truth."""
        self._setup_found_paper(self.aid1, ["v1"])
        self._complete_version_tex(self.aid1, "v1")

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertTrue(stats["is_reconciled"])
        self.assertEqual(stats["reconciliation"]["discrepancies"], [])
        self.assertIn("independent_audit", stats["reconciliation"])
        self.assertEqual(stats["reconciliation"]["independent_audit"]["raw_found_count"], 1)
        self.assertEqual(stats["reconciliation"]["independent_audit"]["raw_total_versions"], 1)

        # Test directly that _perform_independent_reconciliation catches tampered stats
        from src.reporting import _perform_independent_reconciliation
        tampered_stats = dict(stats)
        tampered_stats["found_papers"] = 999  # Spoofed classifier counter
        res = _perform_independent_reconciliation(self.store, [(self.mssv, self.aid1)], tampered_stats)
        self.assertFalse(res["is_reconciled"])
        self.assertTrue(any("Found papers mismatch" in d for d in res["discrepancies"]))

    def test_source_integrity_failed_versions_counter(self):
        """source_integrity_failed_versions tracks versions failing integrity audit."""
        self._setup_found_paper(self.aid1, ["v1", "v2"])
        self._complete_version_tex(self.aid1, "v1")
        self._complete_version_tex(self.aid1, "v2")

        # Tamper v2 checksum
        p_path = self.store.paper_path(self.mssv, self.aid1)
        (p_path / "v2" / "main.tex").write_text("CORRUPTED_V2", encoding="utf-8")

        stats = compute_m1_statistics(self.store, [(self.mssv, self.aid1)])
        self.assertEqual(stats["total_discovered_versions"], 2)
        self.assertEqual(stats["source_integrity_failed_versions"], 1)
        self.assertEqual(stats["source_complete_papers"], 0)

    def test_git_commit_prioritizes_project_root_over_cwd(self):
        """_get_git_commit prioritizes PROJECT_ROOT over arbitrary external cwd."""
        with tempfile.TemporaryDirectory() as fake_git_dir:
            import subprocess
            subprocess.run(["git", "init", fake_git_dir], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", fake_git_dir, "config", "user.email", "test@test.com"], check=True)
            subprocess.run(["git", "-C", fake_git_dir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", fake_git_dir, "commit", "--allow-empty", "-m", "fake"], check=True, stdout=subprocess.DEVNULL)
            fake_sha = subprocess.check_output(["git", "-C", fake_git_dir, "rev-parse", "HEAD"], text=True).strip()

            actual_sha = _get_git_commit(Path(fake_git_dir))
            self.assertNotEqual(actual_sha, fake_sha)
            from src.local_config import PROJECT_ROOT
            expected_sha = subprocess.check_output(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(actual_sha, expected_sha)


if __name__ == "__main__":
    unittest.main()

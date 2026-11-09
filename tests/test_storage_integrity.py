"""Unit and regression tests for Storage Contract and Data Integrity Audit API.

Verifies cross-file consistency, checksum integrity, file extension filtering,
unmanaged file detection, and completeness semantics.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.export import export_papers_table, export_references_table
from src.fast_production import FastProductionRunner
from src.storage import CorpusStorage, arxiv_id_to_folder_key
from src.storage_integrity import (
    audit_cohort_integrity,
    audit_paper_integrity,
    audit_version_integrity,
    format_integrity_report_markdown,
)


class TestStorageIntegrity(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.corpus_dir = Path(self.td.name) / "corpus"
        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.store = CorpusStorage(self.corpus_dir)
        self.mssv = "24120010"
        self.arxiv_id = "2508.07872"
        self.folder_key = arxiv_id_to_folder_key(self.arxiv_id)
        self.paper_dir = self.corpus_dir / self.folder_key

    def tearDown(self):
        self.td.cleanup()

    def _setup_valid_complete_paper(self) -> None:
        """Helper to create a fully verified, complete paper."""
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": self.arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{self.arxiv_id}v1"],
            "latest_version_id": f"{self.arxiv_id}v1",
            "title": "A Great Paper",
            "authors": ["Author One"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        dest = self.store.begin_version(self.mssv, self.arxiv_id, "v1")
        assert dest is not None
        dest.mkdir(parents=True, exist_ok=True)
        tex_file = dest / "main.tex"
        tex_file.write_text(r"\documentclass{article}\begin{document}Hi\end{document}", encoding="utf-8")
        self.store.record_source_result({
            "mssv": self.mssv,
            "arxiv_id": self.arxiv_id,
            "versioned_id": f"{self.arxiv_id}v1",
            "status": "success",
            "files": ["main.tex"],
            "downloaded_bytes": 100,
            "error": None,
        })
        meta = self.store.read_metadata(self.mssv, self.arxiv_id)
        meta["status"] = "complete"
        meta["title"] = "A Great Paper"
        meta["authors"] = ["Author One"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = []
        meta["venue"] = "NeurIPS"
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [{"version": "v1", "versioned_id": f"{self.arxiv_id}v1", "date": "2025-08-01", "date_status": "verified"}]
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": self.arxiv_id,
            "s2_paper_id": "s2_123",
            "retrieved_at": "2025-08-01T00:00:00Z",
            "snapshot": {"arxiv_id": self.arxiv_id, "status": "found"},
        }
        self.store.save_metadata(self.mssv, self.arxiv_id, meta)

        refs = self.store.read_references(self.mssv, self.arxiv_id)
        refs["status"] = "complete"
        refs["provenance"]["query_identifier"] = self.arxiv_id
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
            "s2_paper_id": "s2_ref_1",
            "missing_fields": [],
            "raw": {},
        }]
        self.store.save_references(self.mssv, self.arxiv_id, refs)

    def test_audit_missing_paper_directory(self):
        """Audit detects non-existent paper directory."""
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertEqual(res.status, "missing")
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_source_complete)
        self.assertFalse(res.is_m1_complete)
        self.assertEqual(len(res.violations), 1)
        self.assertEqual(res.violations[0].category, "missing")

    def test_audit_missing_mandatory_jsons(self):
        """Audit detects missing manifest.json, metadata.json, or references.json."""
        self.paper_dir.mkdir(parents=True, exist_ok=True)
        # Create only manifest.json
        (self.paper_dir / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": self.arxiv_id, "mssv": self.mssv,
            "status": "not_attempted", "discovery_status": "not_attempted",
            "version_status": "needs_verification", "metadata_status": "not_attempted",
            "references_status": "not_attempted", "versions": {}, "updated_at": "2025-08-01T00:00:00Z",
            "run_id": None,
        }), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertEqual(res.status, "missing")
        cats = [v.category for v in res.violations]
        self.assertIn("missing", cats)

    def test_audit_corrupted_json(self):
        """Audit detects unparseable JSON files."""
        self.paper_dir.mkdir(parents=True, exist_ok=True)
        (self.paper_dir / "manifest.json").write_text("NOT_VALID_JSON{", encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertEqual(res.status, "corrupted")
        self.assertIn("corrupted", [v.category for v in res.violations])

    def test_audit_cross_file_status_mismatch(self):
        """Audit detects contradictions between manifest and metadata/references status."""
        self._setup_valid_complete_paper()
        # Tamper manifest to say references_status is failed while references.json is complete
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        man["references_status"] = "failed"
        (self.paper_dir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertIn("invalid", [v.category for v in res.violations])

    def test_audit_source_checksum_verification(self):
        """Audit detects corrupted file checksum on disk."""
        self._setup_valid_complete_paper()
        tex_path = self.paper_dir / "v1" / "main.tex"
        # Tamper the file content without updating checksum
        tex_path.write_text("TAMPERED_CONTENT_CORRUPTED", encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_source_complete)
        self.assertFalse(res.is_m1_complete)
        self.assertEqual(res.status, "corrupted")
        self.assertTrue(any("checksum mismatch" in v.message.lower() for v in res.violations))

    def test_audit_unmanaged_extraneous_files_in_paper_dir(self):
        """Audit detects unmanaged files in paper directory."""
        self._setup_valid_complete_paper()
        # Add extraneous file in paper directory
        extra = self.paper_dir / ".manifest.json.tmp"
        extra.write_text("temp garbage", encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.status, "invalid")
        self.assertTrue(any(v.category == "unmanaged" for v in res.violations))

    def test_audit_unmanaged_extraneous_files_in_version_dir(self):
        """Audit detects unmanaged files inside version directory."""
        self._setup_valid_complete_paper()
        # Add extraneous file inside v1 directory
        extra = self.paper_dir / "v1" / "extra.bib"
        extra.write_text("@misc{test, title={Test}}", encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertIn(res.status, {"invalid", "corrupted"})
        self.assertTrue(any(v.category == "unmanaged" for v in res.violations))

    def test_audit_forbidden_extensions_in_source(self):
        """Audit rejects non-.tex/.bib files in version directory."""
        self._setup_valid_complete_paper()
        fig = self.paper_dir / "v1" / "figure.png"
        fig.write_bytes(b"\x89PNG\r\n\x1a\n")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("forbidden source file extension" in v.message.lower() for v in res.violations))

    def test_audit_no_tex_source_semantics(self):
        """Audit verifies no_tex_source invariants."""
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": self.arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{self.arxiv_id}v1"],
            "latest_version_id": f"{self.arxiv_id}v1",
            "title": "No TeX Paper",
            "authors": ["Author One"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        self.store.begin_version(self.mssv, self.arxiv_id, "v1")
        self.store.record_source_result({
            "mssv": self.mssv,
            "arxiv_id": self.arxiv_id,
            "versioned_id": f"{self.arxiv_id}v1",
            "status": "no_tex_source",
            "files": [],
            "downloaded_bytes": 0,
            "error": None,
        })
        # Empty no_tex_source: directory v1 should not exist
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        # Should be incomplete (metadata/references not yet complete), but version audit clean
        self.assertEqual(len(res.violations), 0)

        # Violation: empty no_tex_source but directory v1 exists on disk
        v1_dir = self.paper_dir / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        res2 = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res2.is_valid)
        self.assertTrue(any("unexpected version directory" in v.message.lower() for v in res2.violations))

    def test_audit_references_coverage_semantics(self):
        """Audit enforces coverage.complete semantics (null vs empty vs complete)."""
        self._setup_valid_complete_paper()
        refs_path = self.paper_dir / "references.json"
        refs = json.loads(refs_path.read_text(encoding="utf-8"))

        # Incomplete coverage while claiming complete status
        refs["coverage"]["complete"] = False
        refs_path.write_text(json.dumps(refs), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("coverage" in v.message.lower() for v in res.violations))

        # Valid empty references: 0 items, but coverage.complete == True and status == complete
        refs["items"] = []
        refs["coverage"] = {
            "complete": True,
            "continuation": None,
            "reported_total": 0,
            "retrieved_count": 0,
        }
        refs_path.write_text(json.dumps(refs), encoding="utf-8")
        res2 = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertTrue(res2.is_valid)
        self.assertTrue(res2.is_m1_complete)

    def test_audit_cohort_integrity_and_report_generation(self):
        """Audit cohort aggregates stats accurately and formats markdown report."""
        self._setup_valid_complete_paper()
        report = audit_cohort_integrity(self.store, [(self.mssv, self.arxiv_id)], cohort_number=1)
        self.assertEqual(report.total_candidates, 1)
        self.assertEqual(report.valid_count, 1)
        self.assertEqual(report.source_complete_count, 1)
        self.assertEqual(report.m1_complete_count, 1)

        md = format_integrity_report_markdown(report)
        self.assertIn("Storage Contract & Data Integrity Audit Report", md)
        self.assertIn("Cohort 0001", md)
        self.assertIn("100.0%", md)

    def test_count_completed_papers_verifies_disk_source(self):
        """FastProductionRunner.count_completed_papers requires verified source files on disk."""
        runner_root = Path(self.td.name) / "runner_local"
        drive_root = Path(self.td.name) / "drive"
        runner = FastProductionRunner(runner_root, drive_root)

        # Paper 1: Valid complete paper
        self._setup_valid_complete_paper()
        p1_folder = runner.corpus_dir / self.folder_key
        p1_folder.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copytree(self.paper_dir, p1_folder)

        count = runner.count_completed_papers(1)
        self.assertEqual(count, 1)

        # Tamper paper on disk by deleting main.tex
        tex_path = p1_folder / "v1" / "main.tex"
        tex_path.unlink()

        # Corrupted paper should NOT be counted as complete
        count_tampered = runner.count_completed_papers(1)
        self.assertEqual(count_tampered, 0)

    def test_export_verified_only_enforcement(self):
        """Export functions respect verified_only=True and do not leak partial data."""
        self._setup_valid_complete_paper()
        pairs = [(self.mssv, self.arxiv_id)]

        # Valid complete paper exports cleanly
        papers = export_papers_table(self.store, pairs, verified_only=True)
        self.assertEqual(len(papers), 1)

        refs = export_references_table(self.store, pairs, verified_only=True)
        self.assertEqual(len(refs), 1)

        # Change references to partial
        r_path = self.paper_dir / "references.json"
        r_data = json.loads(r_path.read_text(encoding="utf-8"))
        r_data["status"] = "partial"
        r_data["coverage"]["complete"] = False
        r_path.write_text(json.dumps(r_data), encoding="utf-8")

        # Verified-only references export must skip partial references
        refs_skipped = export_references_table(self.store, pairs, verified_only=True)
        self.assertEqual(len(refs_skipped), 0)


if __name__ == "__main__":
    unittest.main()

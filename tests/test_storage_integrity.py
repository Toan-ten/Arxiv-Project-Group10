"""Unit and regression tests for Storage Contract and Data Integrity Audit API.

Verifies cross-file consistency, checksum integrity, file extension filtering,
unmanaged file detection, and completeness semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.export import export_papers_table, export_references_table
from src.fast_production import FastProductionRunner
from src.storage import CorpusStorage, arxiv_id_to_folder_key
from src.storage_integrity import (
    _open_and_hash_source_file,
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

    # ---------------------------------------------------------
    # R3: Enforce Complete Checksum Coverage Tests
    # ---------------------------------------------------------
    def test_audit_checksum_coverage_missing_entry(self):
        """R3: Expected file missing checksum entry in manifest is flagged as incomplete."""
        self._setup_valid_complete_paper()
        man_path = self.paper_dir / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        # Delete checksum entry for main.tex while keeping file in files list
        man["versions"]["v1"]["file_checksums"] = {}
        man_path.write_text(json.dumps(man), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "incomplete" and "missing checksum" in v.message.lower() for v in res.violations))

    def test_audit_checksum_coverage_unexpected_entry(self):
        """R3: Extra checksum entry in manifest not in files list is flagged as invalid."""
        self._setup_valid_complete_paper()
        man_path = self.paper_dir / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        fake_sha = hashlib.sha256(b"dummy").hexdigest()
        man["versions"]["v1"]["file_checksums"]["ghost.tex"] = fake_sha
        man_path.write_text(json.dumps(man), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "invalid" and "unexpected checksum entries" in v.message.lower() for v in res.violations))

    def test_audit_checksum_malformed_format(self):
        """R3: Checksum not matching ^[0-9a-f]{64}$ is flagged as invalid."""
        self._setup_valid_complete_paper()
        man_path = self.paper_dir / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        # Malformed: uppercase hex or non-hex string
        man["versions"]["v1"]["file_checksums"]["main.tex"] = "not_a_valid_sha256_hash"
        man_path.write_text(json.dumps(man), encoding="utf-8")

        # 1. Direct version-level audit detects malformed checksum
        v_violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "malformed sha-256" in v.message.lower() for v in v_violations))

        # 2. Paper-level audit flags invalid schema
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "invalid" and "invalid sha-256" in v.message.lower() for v in res.violations))

    def test_audit_content_mismatch_vs_missing_file_independent(self):
        """R3: Content mismatch (corrupted) and missing file (missing) detected independently."""
        self._setup_valid_complete_paper()
        # Case A: Content mismatch (file exists, but modified)
        tex_path = self.paper_dir / "v1" / "main.tex"
        tex_path.write_text(r"\documentclass{article}\begin{document}Modified\end{document}", encoding="utf-8")
        res_corrupt = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res_corrupt.is_valid)
        self.assertTrue(any(v.category == "corrupted" and "mismatch" in v.message.lower() for v in res_corrupt.violations))
        self.assertFalse(any(v.category == "missing" for v in res_corrupt.violations))

        # Case B: Missing file on disk
        tex_path.unlink()
        res_missing = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res_missing.is_valid)
        self.assertTrue(any(v.category == "missing" for v in res_missing.violations))

    def test_audit_kept_bytes_mismatch(self):
        """R3: kept_bytes mismatch with actual on-disk total bytes is flagged as corrupted."""
        self._setup_valid_complete_paper()
        man_path = self.paper_dir / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        man["versions"]["v1"]["kept_bytes"] = 999999
        man_path.write_text(json.dumps(man), encoding="utf-8")

        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "corrupted" and "kept bytes mismatch" in v.message.lower() for v in res.violations))

    # ---------------------------------------------------------
    # R4: Fully Validate no_tex_source Tests
    # ---------------------------------------------------------
    def test_audit_no_tex_source_valid_bib_only(self):
        """R4: Valid bib-only no_tex_source passes audit cleanly."""
        self.store.record_discovery({
            "mssv": self.mssv, "arxiv_id": self.arxiv_id, "status": "found",
            "version_status": "resolved", "version_ids": [f"{self.arxiv_id}v1"],
            "latest_version_id": f"{self.arxiv_id}v1", "title": "Bib Paper",
            "authors": ["Author"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        dest = self.store.begin_version(self.mssv, self.arxiv_id, "v1")
        assert dest is not None
        dest.mkdir(parents=True, exist_ok=True)
        bib_content = "@article{key, title={Test}}"
        bib_file = dest / "refs.bib"
        bib_file.write_text(bib_content, encoding="utf-8")
        self.store.record_source_result({
            "mssv": self.mssv, "arxiv_id": self.arxiv_id,
            "versioned_id": f"{self.arxiv_id}v1", "status": "no_tex_source",
            "files": ["refs.bib"], "downloaded_bytes": len(bib_content.encode("utf-8")),
            "error": None,
        })
        # Version record audit for v1 must pass cleanly
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertEqual(len(violations), 0)

    def test_audit_no_tex_source_missing_bib(self):
        """R4: Missing bib file on disk in bib-only no_tex_source is flagged as missing."""
        self.test_audit_no_tex_source_valid_bib_only()
        (self.paper_dir / "v1" / "refs.bib").unlink()
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "missing" for v in violations))

    def test_audit_no_tex_source_tampered_bib(self):
        """R4: Checksum mismatch on bib file in no_tex_source is flagged as corrupted."""
        self.test_audit_no_tex_source_valid_bib_only()
        (self.paper_dir / "v1" / "refs.bib").write_text("@article{tampered}", encoding="utf-8")
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "corrupted" and "mismatch" in v.message.lower() for v in violations))

    def test_audit_no_tex_source_rejects_non_bib_files(self):
        """R4: no_tex_source rejects .tex, .png, and any other non-.bib files."""
        self.test_audit_no_tex_source_valid_bib_only()
        # Add forbidden .png file on disk
        extra_png = self.paper_dir / "v1" / "chart.png"
        extra_png.write_bytes(b"\x89PNG")
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "non-.bib file" in v.message.lower() for v in violations))

        # Add forbidden .tex file on disk
        extra_png.unlink()
        extra_tex = self.paper_dir / "v1" / "forbidden.tex"
        extra_tex.write_text(r"\documentclass{article}", encoding="utf-8")
        violations_tex = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "non-.bib file" in v.message.lower() for v in violations_tex))

    # ---------------------------------------------------------
    # R5: Harden Filesystem Link Handling Tests
    # ---------------------------------------------------------
    def test_audit_rejects_symlink_file(self):
        """R5: Symlink file pointing outside corpus is rejected as corrupted and NEVER hashed."""
        self._setup_valid_complete_paper()
        v1_dir = self.paper_dir / "v1"
        tex_path = v1_dir / "main.tex"
        tex_path.unlink()

        # Create outside target in separate temporary directory
        with tempfile.TemporaryDirectory() as outside_td:
            outside_target = Path(outside_td) / "secret.tex"
            outside_target.write_text("SENSITIVE_OUTSIDE_DATA", encoding="utf-8")

            # Create symlink pointing outside corpus
            tex_path.symlink_to(outside_target)
            self.assertTrue(tex_path.is_symlink())

            man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
            violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])

            self.assertTrue(any(v.category == "corrupted" and "symbolic link" in v.message.lower() for v in violations))

    def test_audit_rejects_symlink_directory(self):
        """R5: Symlink directory pointing outside corpus is rejected as corrupted and not traversed."""
        self._setup_valid_complete_paper()
        v1_dir = self.paper_dir / "v1"

        with tempfile.TemporaryDirectory() as outside_td:
            outside_dir = Path(outside_td) / "outside_sub"
            outside_dir.mkdir(parents=True, exist_ok=True)
            (outside_dir / "sub.tex").write_text("sub file", encoding="utf-8")

            sym_dir = v1_dir / "sub_sym"
            sym_dir.symlink_to(outside_dir, target_is_directory=True)
            self.assertTrue(sym_dir.is_symlink())

            man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
            violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])

            self.assertTrue(any(v.category == "corrupted" and "symbolic link" in v.message.lower() for v in violations))

    def test_audit_rejects_version_dir_symlink_loop(self):
        """R12: Symlink loop for version directory is rejected as corrupted without raising RuntimeError."""
        self._setup_valid_complete_paper()
        v1_dir = self.paper_dir / "v1"
        import shutil
        shutil.rmtree(v1_dir)

        # 1. Direct self-referential symlink loop: v1 -> v1
        try:
            v1_dir.symlink_to(v1_dir)
        except OSError:
            self.skipTest("Filesystem does not support symlinks")

        rec = {
            "versioned_id": f"{self.arxiv_id}v1",
            "status": "success",
            "source_status": "success",
            "files": ["main.tex"],
            "file_checksums": {"main.tex": "a" * 64},
            "kept_bytes": 100,
            "completed_at": "2026-11-09T00:00:00Z",
        }

        # Must not raise RuntimeError: Symlink loop
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", rec)
        self.assertTrue(any(v.category == "corrupted" and "symbolic link" in v.message.lower() for v in violations))

        # 2. Mutual symlink loop: v1 -> v_loop and v_loop -> v1
        v1_dir.unlink()
        v_loop = self.paper_dir / "v_loop"
        v1_dir.symlink_to(v_loop)
        v_loop.symlink_to(v1_dir)

        violations_mutual = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", rec)
        self.assertTrue(any(v.category == "corrupted" and "symbolic link" in v.message.lower() for v in violations_mutual))
        v_loop.unlink()
        v1_dir.unlink()

    def test_audit_version_integrity_handles_resolve_exception(self):
        """R12: If version_dir.resolve() raises RuntimeError or OSError, fail closed with corrupted violation."""
        self._setup_valid_complete_paper()
        rec = {
            "versioned_id": f"{self.arxiv_id}v1",
            "status": "success",
            "source_status": "success",
            "files": ["main.tex"],
            "file_checksums": {"main.tex": "a" * 64},
            "kept_bytes": 100,
            "completed_at": "2026-11-09T00:00:00Z",
        }

        # 1. Simulate RuntimeError (e.g. symlink loop in ancestor path during resolve)
        with unittest.mock.patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop inside resolve")):
            violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", rec)
            self.assertTrue(any(v.category == "corrupted" and "Filesystem error resolving version directory" in v.message for v in violations))

        # 2. Simulate OSError (e.g. I/O or permission error during resolve)
        with unittest.mock.patch.object(Path, "resolve", side_effect=OSError("I/O error during resolve")):
            violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", rec)
            self.assertTrue(any(v.category == "corrupted" and "Filesystem error resolving version directory" in v.message for v in violations))

    def test_audit_rejects_hardlink_file(self):
        """R5: Hardlinked file with st_nlink != 1 is rejected as corrupted."""
        self._setup_valid_complete_paper()
        v1_dir = self.paper_dir / "v1"
        main_tex = v1_dir / "main.tex"

        # Create hard link
        hardlink_path = v1_dir / "hardlink.tex"
        import os
        try:
            os.link(main_tex, hardlink_path)
        except OSError:
            self.skipTest("Filesystem does not support hard links")

        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])

        self.assertTrue(any(v.category == "corrupted" and "multiple hard links" in v.message.lower() for v in violations))

    # ---------------------------------------------------------
    # R6: Strengthen count_completed_papers Tests
    # ---------------------------------------------------------
    def test_count_completed_papers_tampered_content_hash(self):
        """R6: count_completed_papers rejects paper with tampered content even if filename matches."""
        runner_root = Path(self.td.name) / "runner_local_tamper"
        drive_root = Path(self.td.name) / "drive_tamper"
        runner = FastProductionRunner(runner_root, drive_root)

        self._setup_valid_complete_paper()
        p_folder = runner.corpus_dir / self.folder_key
        p_folder.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copytree(self.paper_dir, p_folder)

        self.assertEqual(runner.count_completed_papers(1), 1)

        # Tamper content of main.tex without changing filename or manifest
        (p_folder / "v1" / "main.tex").write_text(r"\documentclass{article}\begin{document}TAMPERED\end{document}", encoding="utf-8")

        # Tampered content must fail cryptographic verification and NOT be counted
        self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_missing_checksum_entry(self):
        """R6: count_completed_papers rejects paper if checksum entry missing in manifest."""
        runner_root = Path(self.td.name) / "runner_local_nochecksum"
        drive_root = Path(self.td.name) / "drive_nochecksum"
        runner = FastProductionRunner(runner_root, drive_root)

        self._setup_valid_complete_paper()
        p_folder = runner.corpus_dir / self.folder_key
        p_folder.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copytree(self.paper_dir, p_folder)

        man_path = p_folder / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        man["versions"]["v1"]["file_checksums"] = {}
        man_path.write_text(json.dumps(man), encoding="utf-8")

        self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_invalid_no_tex_source_layout(self):
        """R6: count_completed_papers rejects paper with invalid no_tex_source layout."""
        runner_root = Path(self.td.name) / "runner_local_notex"
        drive_root = Path(self.td.name) / "drive_notex"
        runner = FastProductionRunner(runner_root, drive_root)

        # Setup paper with empty no_tex_source in manifest
        p_folder = runner.corpus_dir / self.folder_key
        p_folder.mkdir(parents=True, exist_ok=True)
        (p_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": self.arxiv_id,
            "mssv": self.mssv,
            "run_id": "test_run",
            "status": "complete_with_no_tex",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "complete",
            "references_status": "complete",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {
                "v1": {
                    "versioned_id": f"{self.arxiv_id}v1",
                    "status": "no_tex_source",
                    "source_status": "no_tex_source",
                    "http_status": 200,
                    "content_type": "application/x-eprint",
                    "source_url": f"https://export.arxiv.org/src/{self.arxiv_id}v1",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": [],
                    "file_checksums": {},
                    "kept_bytes": 0,
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "test_run",
                    "completed_at": "2026-11-09T00:00:00Z",
                    "error": None,
                }
            },
        }), encoding="utf-8")

        # Valid empty no_tex_source (v1 dir does not exist) -> counts as complete
        self.assertEqual(runner.count_completed_papers(1), 1)

        # Invalid layout: unexpected v1 directory on disk for empty no_tex_source
        (p_folder / "v1").mkdir(parents=True, exist_ok=True)
        self.assertEqual(runner.count_completed_papers(1), 0)

    # ---------------------------------------------------------
    # R7: Require kept_bytes for Terminal Source Records Tests
    # ---------------------------------------------------------
    def test_audit_kept_bytes_contract_success_cases(self):
        """R7: For status == 'success', kept_bytes is required, exact int >= 0, and matches disk bytes."""
        self._setup_valid_complete_paper()
        man_path = self.paper_dir / "manifest.json"
        base_man = json.loads(man_path.read_text(encoding="utf-8"))
        tex_bytes = len((self.paper_dir / "v1" / "main.tex").read_bytes())

        # 1. Missing kept_bytes
        man = json.loads(json.dumps(base_man))
        del man["versions"]["v1"]["kept_bytes"]
        man_path.write_text(json.dumps(man), encoding="utf-8")
        # Direct audit_version_integrity catches missing kept_bytes
        v_violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "incomplete" and "missing or null kept_bytes" in v.message.lower() for v in v_violations))
        # Paper audit catches manifest violation
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)

        # 2. Null kept_bytes
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = None
        man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "incomplete" and "missing or null kept_bytes" in v.message.lower() for v in res.violations))

        # 3. Negative kept_bytes
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = -1
        man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "cannot be negative" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)

        # 4. Bool kept_bytes (reject bool as non-int)
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = True
        man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "exact integer" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)

        # 5. Incorrect/mismatched kept_bytes
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = tex_bytes + 100
        man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "corrupted" and "kept bytes mismatch" in v.message.lower() for v in res.violations))

        # 6. Correct kept_bytes passes cleanly
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = tex_bytes
        man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, self.arxiv_id)
        self.assertTrue(res.is_valid)

    def test_audit_kept_bytes_contract_no_tex_source_cases(self):
        """R7: For status == 'no_tex_source', kept_bytes is strictly validated for empty and bib-only."""
        # --- Case A: Empty no_tex_source ---
        empty_aid = "2508.07873"
        self.store.record_discovery({
            "mssv": self.mssv, "arxiv_id": empty_aid, "status": "found",
            "version_status": "resolved", "version_ids": [f"{empty_aid}v1"],
            "latest_version_id": f"{empty_aid}v1", "title": "Empty No TeX Paper",
            "authors": ["Author"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        self.store.begin_version(self.mssv, empty_aid, "v1")
        self.store.record_source_result({
            "mssv": self.mssv, "arxiv_id": empty_aid, "versioned_id": f"{empty_aid}v1",
            "status": "no_tex_source", "files": [], "error": None,
        })
        empty_folder = self.corpus_dir / arxiv_id_to_folder_key(empty_aid)
        man_path = empty_folder / "manifest.json"
        base_man = json.loads(man_path.read_text(encoding="utf-8"))

        # Missing kept_bytes
        man = json.loads(json.dumps(base_man))
        del man["versions"]["v1"]["kept_bytes"]
        man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(empty_folder, empty_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "incomplete" and "missing or null kept_bytes" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, empty_aid)
        self.assertFalse(res.is_valid)

        # Null kept_bytes
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = None
        man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, empty_aid)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "incomplete" and "missing or null kept_bytes" in v.message.lower() for v in res.violations))

        # Non-zero int for empty no_tex_source
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = 10
        man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(empty_folder, empty_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "kept_bytes=0" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, empty_aid)
        self.assertFalse(res.is_valid)

        # Bool for empty no_tex_source
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = False
        man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(empty_folder, empty_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "kept_bytes=0" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, empty_aid)
        self.assertFalse(res.is_valid)

        # Exact integer 0 passes
        man = json.loads(json.dumps(base_man))
        man["versions"]["v1"]["kept_bytes"] = 0
        man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, empty_aid)
        self.assertTrue(res.is_valid)

        # --- Case B: Bib-only no_tex_source ---
        bib_aid = "2508.07874"
        self.store.record_discovery({
            "mssv": self.mssv, "arxiv_id": bib_aid, "status": "found",
            "version_status": "resolved", "version_ids": [f"{bib_aid}v1"],
            "latest_version_id": f"{bib_aid}v1", "title": "Bib Paper",
            "authors": ["Author"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        dest = self.store.begin_version(self.mssv, bib_aid, "v1")
        dest.mkdir(parents=True, exist_ok=True)
        bib_content = "@article{test, title={Test Paper}}"
        (dest / "refs.bib").write_text(bib_content, encoding="utf-8")
        self.store.record_source_result({
            "mssv": self.mssv, "arxiv_id": bib_aid, "versioned_id": f"{bib_aid}v1",
            "status": "no_tex_source", "files": ["refs.bib"], "error": None,
        })
        bib_folder = self.corpus_dir / arxiv_id_to_folder_key(bib_aid)
        bib_man_path = bib_folder / "manifest.json"
        base_bib_man = json.loads(bib_man_path.read_text(encoding="utf-8"))
        actual_bib_bytes = len(bib_content.encode("utf-8"))

        # Missing kept_bytes
        man = json.loads(json.dumps(base_bib_man))
        del man["versions"]["v1"]["kept_bytes"]
        bib_man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(bib_folder, bib_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "incomplete" and "missing or null kept_bytes" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, bib_aid)
        self.assertFalse(res.is_valid)

        # Negative kept_bytes
        man = json.loads(json.dumps(base_bib_man))
        man["versions"]["v1"]["kept_bytes"] = -5
        bib_man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(bib_folder, bib_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "cannot be negative" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, bib_aid)
        self.assertFalse(res.is_valid)

        # Bool kept_bytes
        man = json.loads(json.dumps(base_bib_man))
        man["versions"]["v1"]["kept_bytes"] = True
        bib_man_path.write_text(json.dumps(man), encoding="utf-8")
        v_violations = audit_version_integrity(bib_folder, bib_aid, "v1", man["versions"]["v1"])
        self.assertTrue(any(v.category == "invalid" and "exact integer" in v.message.lower() for v in v_violations))
        res = audit_paper_integrity(self.store, self.mssv, bib_aid)
        self.assertFalse(res.is_valid)

        # Mismatched kept_bytes
        man = json.loads(json.dumps(base_bib_man))
        man["versions"]["v1"]["kept_bytes"] = actual_bib_bytes + 999
        bib_man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, bib_aid)
        self.assertFalse(res.is_valid)
        self.assertTrue(any(v.category == "corrupted" and "kept bytes mismatch" in v.message.lower() for v in res.violations))

        # Correct kept_bytes passes
        man = json.loads(json.dumps(base_bib_man))
        man["versions"]["v1"]["kept_bytes"] = actual_bib_bytes
        bib_man_path.write_text(json.dumps(man), encoding="utf-8")
        res = audit_paper_integrity(self.store, self.mssv, bib_aid)
        self.assertTrue(res.is_valid)

    # ---------------------------------------------------------
    # R8: Fail-Closed Filesystem Traversal Tests
    # ---------------------------------------------------------
    def test_audit_traversal_error_fail_closed(self):
        """R8: Filesystem traversal errors invoke onerror and cause audit to fail closed."""
        self._setup_valid_complete_paper()
        man = json.loads((self.paper_dir / "manifest.json").read_text(encoding="utf-8"))
        v1_record = man["versions"]["v1"]

        real_walk = os.walk

        def failing_walk(top, **kwargs):
            onerror = kwargs.get("onerror")
            if onerror:
                onerror(PermissionError(f"Simulated permission denied on {top}/subdir"))
            # Yield top without entering subdirs
            yield top, [], ["main.tex"]

        with patch("os.walk", side_effect=failing_walk):
            violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", v1_record)
            self.assertTrue(any(v.category == "corrupted" and "traversal error" in v.message.lower() for v in violations))
            self.assertTrue(any(v.category == "corrupted" and "failed to inspect full subtree" in v.message.lower() for v in violations))

    # ---------------------------------------------------------
    # R9: TOCTOU and Descriptor Hashing Tests
    # ---------------------------------------------------------
    def test_audit_descriptor_open_and_hash_symlink_rejection(self):
        """R9: _open_and_hash_source_file opens with O_NOFOLLOW and rejects symlinks directly via descriptor."""
        with tempfile.TemporaryDirectory() as outside_td:
            outside_file = Path(outside_td) / "target.tex"
            outside_file.write_text("OUTSIDE_SOURCE", encoding="utf-8")

            link_file = Path(self.td.name) / "test_link.tex"
            link_file.symlink_to(outside_file)

            sha, fstat_res, err = _open_and_hash_source_file(link_file)
            self.assertIsNone(sha)
            self.assertIsNone(fstat_res)
            self.assertIsNotNone(err)
            self.assertIn("symbolic link", err.lower())

            # Regular file hashes correctly without error
            regular_file = Path(self.td.name) / "regular.tex"
            content = "REGULAR_SOURCE_CONTENT"
            regular_file.write_text(content, encoding="utf-8")

            sha, fstat_res, err = _open_and_hash_source_file(regular_file)
            self.assertIsNone(err)
            self.assertIsNotNone(sha)
            self.assertIsNotNone(fstat_res)
            self.assertEqual(sha, hashlib.sha256(content.encode("utf-8")).hexdigest())
            self.assertEqual(fstat_res.st_size, len(content.encode("utf-8")))

    # ---------------------------------------------------------
    # R12: Validate Version Name Format and Path Containment
    # ---------------------------------------------------------
    def test_audit_version_integrity_rejects_invalid_version_names(self):
        """R12: Validate version name strictly with _version() before path creation or checks."""
        # 1. version_name="../../missing" with empty no_tex_source must be rejected
        outside_rec = {
            "versioned_id": self.arxiv_id + "../../missing",
            "status": "no_tex_source",
            "source_status": "no_tex_source",
            "files": [],
            "file_checksums": {},
            "kept_bytes": 0,
            "completed_at": "2026-11-09T00:00:00Z",
        }
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "../../missing", outside_rec)
        self.assertTrue(any(v.category in {"invalid", "corrupted"} and "version name" in v.message.lower() for v in violations))

        # 2. Absolute version name
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "/etc/passwd", outside_rec)
        self.assertTrue(any(v.category == "invalid" and "version name" in v.message.lower() for v in violations))

        # 3. v0 (forbidden: versions start at v1)
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v0", outside_rec)
        self.assertTrue(any(v.category == "invalid" and "version name" in v.message.lower() for v in violations))

        # 4. v01 (forbidden: no leading zeros)
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v01", outside_rec)
        self.assertTrue(any(v.category == "invalid" and "version name" in v.message.lower() for v in violations))

        # 5. ../v1 (forbidden: path traversal)
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "../v1", outside_rec)
        self.assertTrue(any(v.category == "invalid" and "version name" in v.message.lower() for v in violations))

        # 6. Valid version name passes
        valid_rec = {
            "versioned_id": self.arxiv_id + "v1",
            "status": "no_tex_source",
            "source_status": "no_tex_source",
            "files": [],
            "file_checksums": {},
            "kept_bytes": 0,
            "completed_at": "2026-11-09T00:00:00Z",
        }
        violations = audit_version_integrity(self.paper_dir, self.arxiv_id, "v1", valid_rec)
        self.assertEqual(len(violations), 0)


if __name__ == "__main__":
    unittest.main()

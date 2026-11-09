import tempfile
from pathlib import Path
import unittest

from src.storage import CorpusStorage
from src.export import (
    export_diagnostic_papers_table,
    export_papers_table,
    export_references_table,
    export_to_parquet,
    validate_exported_tables,
)


class TestExportTables(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = CorpusStorage(Path(self.td.name) / "corpus")

    def tearDown(self):
        self.td.cleanup()

    def _setup_complete_paper(self, mssv="24120010", aid="2508.07872"):
        self.store.record_discovery({
            "mssv": mssv, "arxiv_id": aid, "status": "found",
            "version_status": "resolved", "version_ids": [f"{aid}v1"],
            "latest_version_id": f"{aid}v1", "title": "Sample Title",
            "authors": ["Alice"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        meta = self.store.read_metadata(mssv, aid)
        meta["status"] = "complete"
        meta["title"] = "Sample Title"
        meta["authors"] = ["Alice"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = []
        meta["venue"] = "NeurIPS"
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [{"version": "v1", "versioned_id": f"{aid}v1", "date": "2025-08-01", "date_status": "verified"}]
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": aid,
            "s2_paper_id": "s2p1",
            "retrieved_at": "2026-11-09T00:00:00Z",
            "snapshot": {"arxiv_id": aid, "status": "found"},
        }
        self.store.save_metadata(mssv, aid, meta)

        self.store.save_references(mssv, aid, {
            "contract_version": 2, "arxiv_id": aid, "status": "complete",
            "provenance": {
                "provider": "semantic_scholar",
                "query_identifier": aid,
                "s2_paper_id": "s2p1",
                "retrieved_at": "2026-11-09T00:00:00Z",
            },
            "coverage": {
                "complete": True,
                "continuation": None,
                "reported_total": 1,
                "retrieved_count": 1,
            },
            "items": [
                {
                    "arxiv_id": "2101.00001",
                    "title": "Ref Title",
                    "authors": ["Bob"],
                    "submission_date": None,
                    "publication_date": "2021-01-15",
                    "year": 2021,
                    "venue": "NeurIPS",
                    "s2_paper_id": "s2_ref_1",
                    "missing_fields": [],
                    "raw": {},
                }
            ],
        })

    def test_export_papers_and_references_tables(self):
        mssv = "24120010"
        aid = "2508.07872"
        self._setup_complete_paper(mssv, aid)

        pairs = [(mssv, aid)]
        papers_rows = export_papers_table(self.store, pairs)
        self.assertEqual(len(papers_rows), 1)
        self.assertEqual(
            set(papers_rows[0].keys()),
            {"arxiv_id", "title", "authors", "submission_date", "revised_dates", "venue"},
        )

        ref_rows = export_references_table(self.store, pairs)
        self.assertEqual(len(ref_rows), 1)
        self.assertEqual(
            set(ref_rows[0].keys()),
            {"arxiv_id", "ref_arxiv_id", "ref_title", "ref_authors", "ref_submission_date", "ref_s2_id"},
        )
        self.assertEqual(ref_rows[0]["ref_arxiv_id"], "2101.00001")
        self.assertEqual(ref_rows[0]["ref_s2_id"], "s2_ref_1")
        self.assertIsNone(ref_rows[0]["ref_submission_date"])

    def test_export_papers_fail_safe_defaults(self):
        """R2: export_papers_table defaults to verified_only=True and excludes partial/failed records."""
        mssv = "24120010"
        aid = "2508.07872"
        # 1. Partial paper: only discovery run
        self.store.record_discovery({
            "mssv": mssv, "arxiv_id": aid, "status": "found",
            "version_status": "resolved", "version_ids": [f"{aid}v1"],
            "latest_version_id": f"{aid}v1", "title": "Partial Paper",
            "authors": ["Author"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        pairs = [(mssv, aid)]

        # Official export defaults to verified_only=True -> MUST EXCLUDE PARTIAL
        self.assertEqual(len(export_papers_table(self.store, pairs)), 0)
        self.assertEqual(len(export_papers_table(self.store, pairs, verified_only=True)), 0)

        # Diagnostic export mode clearly separated -> includes partial
        diag_rows = export_diagnostic_papers_table(self.store, pairs)
        self.assertEqual(len(diag_rows), 1)
        self.assertEqual(diag_rows[0]["title"], "Partial Paper")

        # Explicit verified_only=False also works
        self.assertEqual(len(export_papers_table(self.store, pairs, verified_only=False)), 1)

        # Missing / unattempted / malformed papers are always excluded
        missing_pairs = [(mssv, "2508.07873")]
        self.assertEqual(len(export_papers_table(self.store, missing_pairs)), 0)
        self.assertEqual(len(export_diagnostic_papers_table(self.store, missing_pairs)), 0)

    def test_export_references_fail_safe(self):
        """R2: export_references_table requires status=='complete' and coverage.complete==True."""
        mssv = "24120010"
        aid = "2508.07872"
        self._setup_complete_paper(mssv, aid)
        pairs = [(mssv, aid)]

        # Verified export works
        self.assertEqual(len(export_references_table(self.store, pairs)), 1)

        # Tamper references on disk: coverage.complete = False
        ref_file = self.store.root / "2508-07872" / "references.json"
        import json
        refs = json.loads(ref_file.read_text(encoding="utf-8"))
        refs["coverage"]["complete"] = False
        ref_file.write_text(json.dumps(refs), encoding="utf-8")
        self.assertEqual(len(export_references_table(self.store, pairs)), 0)

        # Tamper references on disk: status = "partial"
        refs["status"] = "partial"
        refs["coverage"]["complete"] = True
        ref_file.write_text(json.dumps(refs), encoding="utf-8")
        self.assertEqual(len(export_references_table(self.store, pairs)), 0)

    def test_export_preserves_null_semantics(self):
        """R1: Null semantics preserved for authors, revised_dates, and ref_authors."""
        import json
        mssv = "24120010"
        aid_null = "2508.07872"
        aid_empty = "2508.07873"

        # Case 1: unobserved / null lists in metadata and references
        dir_null = self.store.root / "2508-07872"
        dir_null.mkdir(parents=True, exist_ok=True)
        (dir_null / "metadata.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": aid_null, "mssv": mssv,
            "status": "partial", "title": "Null Paper", "authors": None,
            "submission_date": "2025-08-01", "revised_dates": None, "venue": None,
            "version_status": "resolved",
            "versions": [{"version": "v1", "versioned_id": f"{aid_null}v1", "date": "2025-08-01", "date_status": "verified"}],
            "missing_fields": [],
            "provenance": {"arxiv": None, "semantic_scholar": None, "version_history": None},
        }), encoding="utf-8")

        (dir_null / "references.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": aid_null, "status": "complete",
            "provenance": {"provider": "semantic_scholar", "query_identifier": aid_null, "s2_paper_id": "s2_null", "retrieved_at": "2026-11-09T00:00:00Z"},
            "coverage": {"complete": True, "continuation": None, "reported_total": 1, "retrieved_count": 1},
            "items": [{
                "arxiv_id": "2101.00002", "title": "Ref", "authors": None,
                "submission_date": None, "publication_date": None, "year": 2021,
                "venue": None, "s2_paper_id": "s2_r2", "missing_fields": [], "raw": {},
            }],
        }), encoding="utf-8")

        # Test diagnostic export preserves None
        p_rows_null = export_diagnostic_papers_table(self.store, [(mssv, aid_null)])
        self.assertEqual(len(p_rows_null), 1)
        self.assertIsNone(p_rows_null[0]["authors"])
        self.assertIsNone(p_rows_null[0]["revised_dates"])

        r_rows_null = export_references_table(self.store, [(mssv, aid_null)])
        self.assertEqual(len(r_rows_null), 1)
        self.assertIsNone(r_rows_null[0]["ref_authors"])

        # Case 2: explicit empty lists [] preserved
        dir_empty = self.store.root / "2508-07873"
        dir_empty.mkdir(parents=True, exist_ok=True)
        (dir_empty / "metadata.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": aid_empty, "mssv": mssv,
            "status": "partial", "title": "Empty Paper", "authors": [],
            "submission_date": "2025-08-01", "revised_dates": [], "venue": None,
            "version_status": "resolved",
            "versions": [{"version": "v1", "versioned_id": f"{aid_empty}v1", "date": "2025-08-01", "date_status": "verified"}],
            "missing_fields": [],
            "provenance": {"arxiv": None, "semantic_scholar": None, "version_history": None},
        }), encoding="utf-8")

        (dir_empty / "references.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": aid_empty, "status": "complete",
            "provenance": {"provider": "semantic_scholar", "query_identifier": aid_empty, "s2_paper_id": "s2_empty", "retrieved_at": "2026-11-09T00:00:00Z"},
            "coverage": {"complete": True, "continuation": None, "reported_total": 1, "retrieved_count": 1},
            "items": [{
                "arxiv_id": "2101.00003", "title": "Ref Empty", "authors": [],
                "submission_date": None, "publication_date": None, "year": 2021,
                "venue": None, "s2_paper_id": "s2_r3", "missing_fields": [], "raw": {},
            }],
        }), encoding="utf-8")

        p_rows_empty = export_diagnostic_papers_table(self.store, [(mssv, aid_empty)])
        self.assertEqual(len(p_rows_empty), 1)
        self.assertEqual(p_rows_empty[0]["authors"], [])
        self.assertEqual(p_rows_empty[0]["revised_dates"], [])

        r_rows_empty = export_references_table(self.store, [(mssv, aid_empty)])
        self.assertEqual(len(r_rows_empty), 1)
        self.assertEqual(r_rows_empty[0]["ref_authors"], [])

    def test_validate_exported_tables_nullable_semantics(self):
        """R1: validate_exported_tables correctly accepts None, [], and list[str]."""
        # Valid cases with None and []
        papers = [
            {"arxiv_id": "2508.07871", "title": "P1", "authors": None, "submission_date": "2025-08-01", "revised_dates": None, "venue": None},
            {"arxiv_id": "2508.07872", "title": "P2", "authors": [], "submission_date": "2025-08-01", "revised_dates": [], "venue": None},
            {"arxiv_id": "2508.07873", "title": "P3", "authors": ["Alice"], "submission_date": "2025-08-01", "revised_dates": ["2025-08-02"], "venue": "NeurIPS"},
        ]
        refs = [
            {"arxiv_id": "2508.07871", "ref_arxiv_id": "2101.00001", "ref_title": "R1", "ref_authors": None, "ref_submission_date": None, "ref_s2_id": "s1"},
            {"arxiv_id": "2508.07872", "ref_arxiv_id": "2101.00002", "ref_title": "R2", "ref_authors": [], "ref_submission_date": None, "ref_s2_id": "s2"},
            {"arxiv_id": "2508.07873", "ref_arxiv_id": "2101.00003", "ref_title": "R3", "ref_authors": ["Bob"], "ref_submission_date": "2021-01-01", "ref_s2_id": "s3"},
        ]
        val = validate_exported_tables(papers, refs)
        self.assertTrue(val["is_valid"])
        self.assertEqual(val["total_papers"], 3)
        self.assertEqual(val["total_references"], 3)

        # Invalid cases: non-list non-null authors
        bad_papers = [{"arxiv_id": "2508.07871", "title": "P1", "authors": "Alice", "submission_date": "2025-08-01", "revised_dates": [], "venue": None}]
        val_bad = validate_exported_tables(bad_papers, refs)
        self.assertFalse(val_bad["is_valid"])
        self.assertTrue(any("authors must be a list or None" in i for i in val_bad["issues"]))

        # Invalid cases: non-string elements in list
        bad_papers2 = [{"arxiv_id": "2508.07871", "title": "P1", "authors": [123], "submission_date": "2025-08-01", "revised_dates": [], "venue": None}]
        val_bad2 = validate_exported_tables(bad_papers2, refs)
        self.assertFalse(val_bad2["is_valid"])
        self.assertTrue(any("authors elements must be strings" in i for i in val_bad2["issues"]))

        # Invalid references: non-list non-null ref_authors
        bad_refs = [{"arxiv_id": "2508.07871", "ref_arxiv_id": "2101.00001", "ref_title": "R1", "ref_authors": 123, "ref_submission_date": None, "ref_s2_id": "s1"}]
        val_bad_ref = validate_exported_tables(papers, bad_refs)
        self.assertFalse(val_bad_ref["is_valid"])
        self.assertTrue(any("ref_authors must be a list or None" in i for i in val_bad_ref["issues"]))

    def test_export_to_parquet_creates_file(self):
        records = [{"arxiv_id": "2508.07872", "title": "Test"}]
        out_path = Path(self.td.name) / "test.parquet"
        export_to_parquet(records, out_path)
        self.assertTrue(out_path.exists() or out_path.with_suffix(".jsonl").exists())

    def test_export_papers_require_m1_complete_contract(self):
        """R11: Harmonize coverage accounting and export contracts.

        - verified_only=True: official metadata catalog (metadata.status == 'complete').
        - require_m1_complete=True: strict M1 end-to-end completion (manifest.status in complete states).
        """
        import json
        mssv = "24120010"
        aid_meta_only = "2508.07872"
        aid_full_m1 = "2508.07873"

        self._setup_complete_paper(mssv, aid_meta_only)
        self._setup_complete_paper(mssv, aid_full_m1)

        # Modify Paper 1 manifest to be partial (source incomplete)
        man1_file = self.store.root / "2508-07872" / "manifest.json"
        man1 = json.loads(man1_file.read_text(encoding="utf-8"))
        man1["status"] = "partial"
        man1_file.write_text(json.dumps(man1), encoding="utf-8")

        # Set Paper 2 manifest to complete (full M1 milestone complete)
        man2_file = self.store.root / "2508-07873" / "manifest.json"
        man2 = json.loads(man2_file.read_text(encoding="utf-8"))
        man2["status"] = "complete"
        man2_file.write_text(json.dumps(man2), encoding="utf-8")

        pairs = [(mssv, aid_meta_only), (mssv, aid_full_m1)]

        # 1. Default verified_only=True: exports both papers with complete metadata
        meta_export = export_papers_table(self.store, pairs, verified_only=True)
        self.assertEqual(len(meta_export), 2)
        self.assertEqual({r["arxiv_id"] for r in meta_export}, {aid_meta_only, aid_full_m1})

        # 2. Strict require_m1_complete=True: exports only the paper with complete manifest
        full_export = export_papers_table(self.store, pairs, verified_only=True, require_m1_complete=True)
        self.assertEqual(len(full_export), 1)
        self.assertEqual(full_export[0]["arxiv_id"], aid_full_m1)

    def test_export_papers_require_m1_complete_with_verified_only_false_rejects_partial(self):
        """R15: require_m1_complete=True must require actual metadata status=='complete' even if verified_only=False."""
        import json
        mssv = "24120010"
        aid = "2508.07872"

        self._setup_complete_paper(mssv, aid)

        # Ensure manifest status is complete
        man_file = self.store.root / "2508-07872" / "manifest.json"
        man = json.loads(man_file.read_text(encoding="utf-8"))
        man["status"] = "complete"
        man["metadata_status"] = "complete"
        man_file.write_text(json.dumps(man), encoding="utf-8")

        # Demote metadata status to partial
        meta_file = self.store.root / "2508-07872" / "metadata.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["status"] = "partial"
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

        pairs = [(mssv, aid)]

        # When require_m1_complete=True, metadata must be complete even if verified_only=False
        rows_strict = export_papers_table(self.store, pairs, verified_only=False, require_m1_complete=True)
        self.assertEqual(len(rows_strict), 0, "require_m1_complete=True must reject partial metadata even if verified_only=False")

        # When require_m1_complete=False and verified_only=False, partial metadata is permitted (diagnostic mode)
        rows_diag = export_papers_table(self.store, pairs, verified_only=False, require_m1_complete=False)
        self.assertEqual(len(rows_diag), 1, "Diagnostic mode (verified_only=False, require_m1_complete=False) permits partial metadata")


if __name__ == "__main__":
    unittest.main()

import tempfile
from pathlib import Path
import unittest

from src.storage import CorpusStorage
from src.export import (
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

    def test_export_papers_and_references_tables(self):
        mssv = "24120010"
        aid = "2508.07872"
        self.store.record_discovery({
            "mssv": mssv, "arxiv_id": aid, "status": "found",
            "version_status": "resolved", "version_ids": [f"{aid}v1"],
            "latest_version_id": f"{aid}v1", "title": "Sample Title",
            "authors": ["Alice"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
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
        # Task 6.2 & 6.3: ref_submission_date preserves null when not present in source (no fabricated dates)
        self.assertIsNone(ref_rows[0]["ref_submission_date"])

    def test_export_to_parquet_creates_file(self):
        records = [{"arxiv_id": "2508.07872", "title": "Test"}]
        out_path = Path(self.td.name) / "test.parquet"
        export_to_parquet(records, out_path)
        # Should create out_path or out_path.with_suffix('.jsonl') if pyarrow is missing
        self.assertTrue(out_path.exists() or out_path.with_suffix(".jsonl").exists())

    def test_validate_exported_tables(self):
        """Task 6.3: validate_exported_tables verifies canonical schemas and catches defects."""
        valid_papers = [{
            "arxiv_id": "2508.07872",
            "title": "Title",
            "authors": ["Author"],
            "submission_date": "2025-08-01",
            "revised_dates": [],
            "venue": "Venue",
        }]
        valid_refs = [{
            "arxiv_id": "2508.07872",
            "ref_arxiv_id": "2101.00001",
            "ref_title": "Ref",
            "ref_authors": ["Ref Author"],
            "ref_submission_date": None,
            "ref_s2_id": "s2_1",
        }]
        res = validate_exported_tables(valid_papers, valid_refs)
        self.assertTrue(res["is_valid"])
        self.assertEqual(res["total_papers"], 1)
        self.assertEqual(res["total_references"], 1)

        # Duplicate paper detection
        dup_papers = valid_papers + valid_papers
        res_dup = validate_exported_tables(dup_papers, valid_refs)
        self.assertFalse(res_dup["is_valid"])
        self.assertIn("Duplicate paper arxiv_id", res_dup["issues"][0])


if __name__ == "__main__":
    unittest.main()

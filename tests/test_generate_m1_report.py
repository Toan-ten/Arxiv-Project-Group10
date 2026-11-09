import tempfile
from pathlib import Path
import unittest

from src.storage import CorpusStorage
from scripts.generate_m1_report import compute_m1_statistics, format_markdown_summary


class TestGenerateM1Report(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = CorpusStorage(Path(self.td.name) / "corpus")

    def tearDown(self):
        self.td.cleanup()

    def test_compute_m1_statistics(self):
        self.store.record_discovery({
            "mssv": "24120010",
            "arxiv_id": "2508.07872",
            "status": "found",
            "version_status": "resolved",
            "version_ids": ["2508.07872v1"],
            "latest_version_id": "2508.07872v1",
            "title": "Paper 1",
            "authors": ["Author"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        stats = compute_m1_statistics(self.store, [("24120010", "2508.07872")])
        self.assertEqual(stats["total_candidates"], 1)
        self.assertEqual(stats["found_papers"], 1)
        self.assertEqual(stats["not_found_papers"], 0)
        self.assertIn("success_rate", stats)
        md = format_markdown_summary(stats)
        self.assertIn("| Chỉ số | Giá trị |", md)


if __name__ == "__main__":
    unittest.main()

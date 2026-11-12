from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.bbl_handling import BblConversionError, apply_bbl_source_policy
from src.storage import CorpusStorage, IntegrityError
from scripts.generate_m1_report import (
    _validate_bbl_artifacts,
    compute_m1_statistics,
    format_markdown_summary,
    METRIC_DEFINITION_VERSION,
)


class TestGenerateM1Report(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = CorpusStorage(Path(self.td.name) / "corpus")
        self.mssv = "24120010"
        self.arxiv_id_1 = "2508.07872"
        self.arxiv_id_2 = "2508.07873"
        self.arxiv_id_3 = "2508.07874"
        self.arxiv_id_4 = "2508.07875"
        self.arxiv_id_5 = "2508.07876"

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
        tex_file.write_text(r"\documentclass{article}\begin{document}Hello\end{document}", encoding="utf-8")
        self.store.record_source_result({
            "mssv": self.mssv,
            "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}{version}",
            "status": "success",
            "files": ["main.tex"],
            "downloaded_bytes": 100,
            "error": None,
        })

    def _complete_version_no_tex(self, arxiv_id: str, version: str) -> None:
        self.store.begin_version(self.mssv, arxiv_id, version)
        self.store.record_source_result({
            "mssv": self.mssv,
            "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}{version}",
            "status": "no_tex_source",
            "files": [],
            "downloaded_bytes": 0,
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

    def test_version_state_mapping_and_source_completion(self):
        """REP-01 & REP-02: verify terminal states 'success' and 'no_tex_source' count properly."""
        # Paper 1: 1 version with TeX success
        self._setup_found_paper(self.arxiv_id_1, ["v1"])
        self._complete_version_tex(self.arxiv_id_1, "v1")

        # Paper 2: 1 version with valid no_tex_source
        self._setup_found_paper(self.arxiv_id_2, ["v1"])
        self._complete_version_no_tex(self.arxiv_id_2, "v1")

        pairs = [(self.mssv, self.arxiv_id_1), (self.mssv, self.arxiv_id_2)]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["total_candidates"], 2)
        self.assertEqual(stats["found_papers"], 2)
        self.assertEqual(stats["successful_tex_versions"], 1)
        self.assertEqual(stats["no_tex_versions"], 1)
        self.assertEqual(stats["terminal_versions"], 2)
        self.assertEqual(stats["source_complete_papers"], 2)
        self.assertEqual(stats["source_completion_rate"], 100.0)
        # Because metadata and references are not complete yet, m1_complete must be 0
        self.assertEqual(stats["m1_complete_papers"], 0)
        self.assertEqual(stats["m1_completion_rate"], 0.0)

    def test_report_preflight_requires_and_verifies_bbl_artifact_root(self):
        arxiv_id = self.arxiv_id_1
        self._setup_found_paper(arxiv_id, ["v1"])
        version_dir = self.store.begin_version(self.mssv, arxiv_id, "v1")
        version_dir.mkdir(parents=True)
        (version_dir / "main.tex").write_bytes(b"\\documentclass{article}\\begin{document}\\cite{x}\\end{document}")
        (version_dir / "refs.bbl").write_bytes(
            b"\\begin{thebibliography}{1}\\bibitem{x} Entry\\end{thebibliography}"
        )
        artifact_root = Path(self.td.name) / "bbl-artifacts"
        files, policy = apply_bbl_source_policy(
            version_dir, ["main.tex", "refs.bbl"], artifact_root=artifact_root,
            mssv=self.mssv, arxiv_id=arxiv_id, version="v1",
        )
        configured_store = CorpusStorage(
            self.store.root, bbl_artifact_root=artifact_root,
        )
        configured_store.record_source_result({
            "mssv": self.mssv, "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}v1", "status": "success",
            "files": sorted(files), "downloaded_bytes": 100, "error": None,
            "bbl_policy": policy,
        })

        with self.assertRaisesRegex(BblConversionError, "bbl_artifact_root_unconfigured"):
            _validate_bbl_artifacts(self.store, [(self.mssv, arxiv_id)])

        _validate_bbl_artifacts(configured_store, [(self.mssv, arxiv_id)])

        index_path = artifact_root / policy["index_path"]
        index_path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(BblConversionError, "bbl_policy_index_checksum_mismatch"):
            _validate_bbl_artifacts(configured_store, [(self.mssv, arxiv_id)])

    def test_multi_version_paper(self):
        """Verify paper with multiple versions sums counts correctly."""
        self._setup_found_paper(self.arxiv_id_1, ["v1", "v2"])
        self._complete_version_tex(self.arxiv_id_1, "v1")
        # v2 left in pending
        stats = compute_m1_statistics(self.store, [(self.mssv, self.arxiv_id_1)])
        self.assertEqual(stats["total_versions"], 2)
        self.assertEqual(stats["successful_tex_versions"], 1)
        self.assertEqual(stats["incomplete_versions"], 1)
        self.assertEqual(stats["source_complete_papers"], 0)

    def test_full_m1_completion(self):
        """REP-02: verify paper is marked M1 complete when all stages are complete."""
        self._setup_found_paper(self.arxiv_id_1, ["v1"])
        self._complete_version_tex(self.arxiv_id_1, "v1")
        self._complete_metadata(self.arxiv_id_1)
        self._complete_references(self.arxiv_id_1, [{
            "arxiv_id": "2508.07873",
            "title": "Ref 1",
            "authors": ["Author B"],
            "submission_date": "2023-01-01",
            "publication_date": "2023-01-01",
            "year": 2023,
            "venue": "Conf",
            "s2_paper_id": "s2_abc",
            "missing_fields": [],
            "raw": {"id": "123"},
        }])

        stats = compute_m1_statistics(self.store, [(self.mssv, self.arxiv_id_1)])
        self.assertEqual(stats["source_complete_papers"], 1)
        self.assertEqual(stats["m1_complete_papers"], 1)
        self.assertEqual(stats["m1_completion_rate"], 100.0)
        self.assertEqual(stats["complete_reference_papers"], 1)
        self.assertEqual(stats["total_complete_reference_records"], 1)
        self.assertEqual(stats["average_references_per_paper"], 1.0)

    def test_references_coverage_complete_empty_items(self):
        """REP-03: complete references with empty items list must count as verified complete."""
        self._setup_found_paper(self.arxiv_id_1, ["v1"])
        self._complete_version_tex(self.arxiv_id_1, "v1")
        self._complete_references(self.arxiv_id_1, [])  # zero items, but coverage complete

        stats = compute_m1_statistics(self.store, [(self.mssv, self.arxiv_id_1)])
        self.assertEqual(stats["complete_reference_papers"], 1)
        self.assertEqual(stats["total_complete_reference_records"], 0)
        self.assertEqual(stats["average_references_per_paper"], 0.0)

    def test_references_partial_with_items_not_counted_as_complete(self):
        """REP-03: partial references with items must not be counted as complete coverage."""
        self._setup_found_paper(self.arxiv_id_1, ["v1"])
        refs = self.store.read_references(self.mssv, self.arxiv_id_1)
        item = {
            "arxiv_id": "2508.07873",
            "title": "Ref 1",
            "authors": ["Author B"],
            "submission_date": "2023-01-01",
            "publication_date": "2023-01-01",
            "year": 2023,
            "venue": "Conf",
            "s2_paper_id": "s2_abc",
            "missing_fields": [],
            "raw": {"id": "123"},
        }
        refs.update(
            status="partial",
            coverage={"complete": False, "continuation": {"offset": 1}, "reported_total": 5, "retrieved_count": 1},
            items=[item],
        )
        self.store.save_references(self.mssv, self.arxiv_id_1, refs)

        stats = compute_m1_statistics(self.store, [(self.mssv, self.arxiv_id_1)])
        self.assertEqual(stats["complete_reference_papers"], 0)
        self.assertEqual(stats["partial_reference_papers"], 1)
        self.assertEqual(stats["total_complete_reference_records"], 0)
        self.assertEqual(stats["total_observed_reference_records"], 1)

    def test_missing_and_corrupt_manifests(self):
        """REP-04 & REP-05: missing or corrupted manifests are explicitly tracked."""
        # arxiv_id_1: missing manifest
        # arxiv_id_2: corrupt manifest JSON
        folder_2 = self.store.paper_path(self.mssv, self.arxiv_id_2)
        folder_2.mkdir(parents=True, exist_ok=True)
        (folder_2 / "manifest.json").write_text("{corrupt json ...", encoding="utf-8")

        pairs = [(self.mssv, self.arxiv_id_1), (self.mssv, self.arxiv_id_2)]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["missing_manifests"], 1)
        self.assertEqual(stats["invalid_manifests"], 1)
        self.assertEqual(stats["found_papers"], 0)

    def test_not_found_paper_and_unprocessed(self):
        """Verify not_found and unprocessed (not_attempted) papers."""
        self.store.record_discovery({
            "mssv": self.mssv,
            "arxiv_id": self.arxiv_id_1,
            "status": "not_found",
            "version_status": "none",
            "version_ids": [],
            "latest_version_id": None,
            "title": None,
            "authors": [],
            "published": None,
            "updated": None,
            "missing_fields": [],
        })
        self.store.initialize_paper(self.mssv, self.arxiv_id_2)  # status: not_attempted

        pairs = [(self.mssv, self.arxiv_id_1), (self.mssv, self.arxiv_id_2)]
        stats = compute_m1_statistics(self.store, pairs)

        self.assertEqual(stats["not_found_papers"], 1)
        self.assertEqual(stats["unprocessed_papers"], 1)
        self.assertEqual(stats["found_papers"], 0)

    def test_format_markdown_summary_provenance(self):
        """REP-06: markdown output includes audit provenance and canonical metrics."""
        self._setup_found_paper(self.arxiv_id_1, ["v1"])
        self._complete_version_tex(self.arxiv_id_1, "v1")
        stats = compute_m1_statistics(
            self.store,
            [(self.mssv, self.arxiv_id_1)],
            repository_commit="0123456789abcdef0123456789abcdef01234567",
        )
        md = format_markdown_summary(stats)

        self.assertIn("Xuất xứ kiểm toán (Audit Provenance)", md)
        self.assertIn("0123456789abcdef0123456789abcdef01234567", md)
        self.assertIn(f"v{METRIC_DEFINITION_VERSION}", md)
        self.assertIn("source_complete", md)
        self.assertIn("m1_complete", md)


if __name__ == "__main__":
    unittest.main()

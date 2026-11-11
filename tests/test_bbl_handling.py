"""Kiểm thử ngoại tuyến cho chính sách chuyển đổi và bảo toàn tệp .bbl."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.bbl_handling import audit_bbl_sources, convert_structured_bbl
from src.storage import CorpusStorage


class TestBblHandling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CorpusStorage(self.root / "corpus")
        self.pair = ("24120010", "2508.07872")

    def tearDown(self):
        self.temp.cleanup()

    def _store_version(self, *, tex: bytes, bbl: bytes, bib: bytes | None = None):
        mssv, arxiv_id = self.pair
        self.store.record_discovery({
            "mssv": mssv,
            "arxiv_id": arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{arxiv_id}v1"],
            "latest_version_id": f"{arxiv_id}v1",
            "title": "BBL test paper",
            "authors": ["Author"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        version_dir = self.store.begin_version(mssv, arxiv_id, "v1")
        assert version_dir is not None
        version_dir.mkdir(parents=True, exist_ok=True)
        files = {"main.tex": tex, "main.bbl": bbl}
        if bib is not None:
            files["refs.bib"] = bib
        for name, content in files.items():
            (version_dir / name).write_bytes(content)
        self.store.record_source_result({
            "mssv": mssv,
            "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}v1",
            "status": "success",
            "files": sorted(files),
            "downloaded_bytes": sum(map(len, files.values())),
            "error": None,
        })
        return version_dir, files

    def _audit(self):
        return audit_bbl_sources(
            self.store,
            [self.pair],
            output_dir=self.root / "drive" / "reports" / "derived_bibliography" / "session",
            report_path=self.root / "drive" / "reports" / "bbl_handling_session.json",
            session_id="session",
        )

    def test_classic_thebibliography_is_reported_and_original_is_retained(self):
        tex = b"\\documentclass{article}\\begin{document}\\cite{foo}\\end{document}"
        bbl = b"\\begin{thebibliography}{1}\\bibitem{foo} Foo Author\\end{thebibliography}"
        version_dir, _files = self._store_version(tex=tex, bbl=bbl)
        manifest_path = self.store.paper_path(*self.pair) / "manifest.json"
        before_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        result = self._audit()

        item = result["items"][0]
        self.assertEqual(item["status"], "not_converted_bbl_retained")
        self.assertTrue(item["conversion_attempted"])
        self.assertEqual(item["reason"], "rendered_thebibliography_is_not_reversible_locally")
        self.assertIn("không thể suy ra chính xác", item["reason_detail"])
        self.assertEqual((version_dir / "main.bbl").read_bytes(), bbl)
        self.assertFalse(list((self.root / "drive").rglob("*.bib")))
        after_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(before_manifest, after_manifest)
        self.assertTrue(result["source_corpus_modified"] is False)

    def test_structured_biblatex_fields_create_separate_bib_without_mutating_source(self):
        tex = b"\\documentclass{article}\\begin{document}\\cite{foo}\\end{document}"
        bbl = (
            b"\\refsection{0}\n\\datalist[entry]{test}\n"
            b"\\entry{foo}{article}{}\n"
            b"  \\field{title}{A {nested} title}\n"
            b"  \\field{year}{2025}\n"
            b"\\endentry\n\\enddatalist\n\\endrefsection\n"
        )
        version_dir, _files = self._store_version(tex=tex, bbl=bbl)
        original_hash = hashlib.sha256(bbl).hexdigest()

        result = self._audit()

        item = result["items"][0]
        self.assertEqual(item["status"], "converted_structured_fields")
        self.assertEqual(item["entry_count"], 1)
        derived = Path(item["derived_bib_path"])
        self.assertTrue(derived.is_file())
        content = derived.read_text(encoding="utf-8")
        self.assertIn("@article{foo,", content)
        self.assertIn("title = {A {nested} title}", content)
        self.assertEqual(item["bbl_sha256"], original_hash)
        self.assertEqual(hashlib.sha256((version_dir / "main.bbl").read_bytes()).hexdigest(), original_hash)

    def test_existing_bib_skips_conversion(self):
        version_dir, files = self._store_version(
            tex=b"\\documentclass{article}\\begin{document}T\\end{document}",
            bbl=b"\\begin{thebibliography}{1}\\bibitem{x} X\\end{thebibliography}",
            bib=b"@misc{x, title={X}}",
        )

        result = self._audit()

        self.assertEqual(result["items"][0]["status"], "not_required_bib_present")
        self.assertFalse(result["items"][0].get("conversion_attempted", False))
        self.assertEqual((version_dir / "main.bbl").read_bytes(), files["main.bbl"])

    def test_active_bibitem_in_tex_skips_conversion(self):
        _version_dir, _files = self._store_version(
            tex=b"\\documentclass{article}\\begin{document}\\bibitem{foo}Text\\end{document}",
            bbl=b"\\begin{thebibliography}{1}\\bibitem{foo} Foo\\end{thebibliography}",
        )

        result = self._audit()

        self.assertEqual(result["items"][0]["status"], "not_required_bibitem_in_tex")

    def test_bibitem_mentioned_only_in_tex_comment_does_not_skip_attempt(self):
        _version_dir, _files = self._store_version(
            tex=b"\\documentclass{article}% \\bibitem{commented}\n\\begin{document}T\\end{document}",
            bbl=b"\\begin{thebibliography}{1}\\bibitem{foo} Foo\\end{thebibliography}",
        )

        result = self._audit()

        self.assertEqual(result["items"][0]["status"], "not_converted_bbl_retained")
        self.assertTrue(result["items"][0]["conversion_attempted"])

    def test_structured_converter_rejects_name_macros_without_guessing(self):
        data = b"\\entry{x}{article}{}\\name{author}{1}{}{}\\endentry"
        with self.assertRaisesRegex(ValueError, "unsupported_entry_macro:name"):
            convert_structured_bbl(data)

    def test_commented_biblatex_entry_is_not_converted(self):
        data = (
            b"% \\entry{commented}{article}{}\\field{title}{Fake}\\endentry\n"
            b"\\entry{real}{article}{}\\field{title}{Real}\\endentry\n"
        )
        converted, count = convert_structured_bbl(data)
        self.assertEqual(count, 1)
        self.assertIn("@article{real,", converted)
        self.assertNotIn("commented", converted)


if __name__ == "__main__":
    unittest.main()

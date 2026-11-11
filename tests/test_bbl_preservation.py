import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from src import arxiv_source as source
from src.fast_production import FastProductionRunner
from src.storage import CorpusStorage
from src.storage_integrity import audit_version_integrity


class TestBblPreservation(unittest.TestCase):
    """Phase 2: Comprehensive tests for .bbl bibliography preservation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "output"
        self.store = CorpusStorage(self.root / "corpus")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_tar(self, entries):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in entries:
                ti = tarfile.TarInfo(name=name)
                ti.size = len(content)
                tar.addfile(ti, io.BytesIO(content))
        return buf.getvalue()

    def test_archive_with_tex_bib_bbl_and_figures_keeps_only_text_sources(self):
        """1. Archive containing main.tex, .bib, .bbl, .png, .pdf: only .tex, .bib, .bbl kept."""
        tex_bytes = b"\\documentclass{article}\\begin{document}Hello\\end{document}"
        bib_bytes = b"@article{a, title={A}}"
        bbl_bytes = b"\\begin{thebibliography}{1}\\bibitem{a} Ref A\\end{thebibliography}"
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        pdf_bytes = b"%PDF-1.4 binary data"

        archive_data = self._create_tar([
            ("main.tex", tex_bytes),
            ("refs.bib", bib_bytes),
            ("main.bbl", bbl_bytes),
            ("fig1.png", png_bytes),
            ("appendix.pdf", pdf_bytes),
        ])

        archive_file = self.root / "source.tar.gz"
        archive_file.write_bytes(archive_data)

        res = source.extract_source(archive_file, self.dest)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["files"], ["main.bbl", "main.tex", "refs.bib"])

        # Verify byte-for-byte preservation of .tex, .bib, and .bbl
        self.assertEqual((self.dest / "main.tex").read_bytes(), tex_bytes)
        self.assertEqual((self.dest / "refs.bib").read_bytes(), bib_bytes)
        self.assertEqual((self.dest / "main.bbl").read_bytes(), bbl_bytes)

        # Verify figures and pdfs are strictly excluded
        self.assertFalse((self.dest / "fig1.png").exists())
        self.assertFalse((self.dest / "appendix.pdf").exists())

    def test_bbl_kept_when_no_bib_file_present(self):
        """2. .bbl is retained even when there is no .bib file."""
        tex_bytes = b"\\documentclass{article}\\begin{document}\\cite{foo}\\end{document}"
        bbl_bytes = b"\\begin{thebibliography}{1}\\bibitem{foo} Foo Author\\end{thebibliography}"

        archive_data = self._create_tar([
            ("main.tex", tex_bytes),
            ("main.bbl", bbl_bytes),
        ])
        archive_file = self.root / "source_nobib.tar.gz"
        archive_file.write_bytes(archive_data)

        res = source.extract_source(archive_file, self.dest)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["files"], ["main.bbl", "main.tex"])
        self.assertEqual((self.dest / "main.bbl").read_bytes(), bbl_bytes)

    def test_bbl_only_archive_reports_no_tex_source_and_preserves_bbl(self):
        """3. Archive with .bbl only (e.g. PDF submission with bibliography) reports no_tex_source and keeps .bbl."""
        bbl_bytes = b"\\begin{thebibliography}{1}\\bibitem{bar} Bar Author\\end{thebibliography}"
        archive_data = self._create_tar([
            ("references.bbl", bbl_bytes),
            ("paper.pdf", b"%PDF-1.4 dummy"),
        ])
        archive_file = self.root / "bbl_only.tar.gz"
        archive_file.write_bytes(archive_data)

        res = source.extract_source(archive_file, self.dest)
        self.assertEqual(res["status"], "no_tex_source")
        self.assertEqual(res["files"], ["references.bbl"])
        self.assertEqual((self.dest / "references.bbl").read_bytes(), bbl_bytes)

    def test_bbl_checksum_and_integrity_audit_round_trip(self):
        """4. Checksum calculation and integrity audit pass cleanly for .bbl files."""
        arxiv_id = "2508.07872"
        mssv = "24120010"
        self.store.record_discovery({
            "mssv": mssv, "arxiv_id": arxiv_id, "status": "found",
            "version_status": "resolved", "version_ids": [f"{arxiv_id}v1"],
            "latest_version_id": f"{arxiv_id}v1", "title": "BBL Test Paper",
            "authors": ["Author"], "published": "2025-08-01",
            "updated": "2025-08-01", "missing_fields": [],
        })
        v_dest = self.store.begin_version(mssv, arxiv_id, "v1")
        assert v_dest is not None
        v_dest.mkdir(parents=True, exist_ok=True)

        tex_bytes = b"\\documentclass{article}\\begin{document}Test\\end{document}"
        bbl_bytes = b"\\bibitem{key} Test citation"
        (v_dest / "main.tex").write_bytes(tex_bytes)
        (v_dest / "main.bbl").write_bytes(bbl_bytes)

        self.store.record_source_result({
            "mssv": mssv, "arxiv_id": arxiv_id, "versioned_id": f"{arxiv_id}v1",
            "status": "success", "files": ["main.bbl", "main.tex"],
            "downloaded_bytes": len(tex_bytes) + len(bbl_bytes),
            "error": None,
        })

        p_dir = self.root / "corpus" / "2508-07872"
        man = json.loads((p_dir / "manifest.json").read_text(encoding="utf-8"))
        v1_record = man["versions"]["v1"]

        # Checksums must be computed for both main.tex and main.bbl
        self.assertIn("main.bbl", v1_record["file_checksums"])
        self.assertEqual(v1_record["file_checksums"]["main.bbl"], hashlib.sha256(bbl_bytes).hexdigest())

        violations = audit_version_integrity(p_dir, arxiv_id, "v1", v1_record)
        self.assertEqual(len(violations), 0)

    def test_bbl_archive_restore_and_roundtrip(self):
        """5. FastProductionRunner archive and restore correctly preserve .bbl files."""
        drive_root = self.root / "drive"
        runner = FastProductionRunner(self.root / "local", drive_root, retry_429=False)

        p = runner.corpus_dir / "2508-07872"
        v1 = p / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        tex_bytes = b"\\documentclass{article}\\begin{document}Hello\\end{document}"
        bbl_bytes = b"\\bibitem{foo} Bar"
        (v1 / "main.tex").write_bytes(tex_bytes)
        (v1 / "main.bbl").write_bytes(bbl_bytes)

        manifest = {
            "status": "complete", "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "completed_at": "2026-11-10T00:00:00Z", "discovery_status": "found", "version_status": "resolved",
            "metadata_status": "complete", "references_status": "complete",
            "versions": {
                "v1": {
                    "status": "success",
                    "files": ["main.bbl", "main.tex"],
                    "file_checksums": {
                        "main.tex": hashlib.sha256(tex_bytes).hexdigest(),
                        "main.bbl": hashlib.sha256(bbl_bytes).hexdigest(),
                    },
                }
            },
        }
        (p / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Archive cohort 1
        arch = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        runner._archive_cohort(1, arch)
        self.assertTrue(arch.exists())

        # Restore in fresh runner
        fresh_runner = FastProductionRunner(self.root / "fresh_local", drive_root, retry_429=False)
        restored = fresh_runner.restore_cohort(1)
        self.assertEqual(restored, 1)

        restored_v1 = fresh_runner.corpus_dir / "2508-07872" / "v1"
        self.assertEqual((restored_v1 / "main.tex").read_bytes(), tex_bytes)
        self.assertEqual((restored_v1 / "main.bbl").read_bytes(), bbl_bytes)


if __name__ == "__main__":
    unittest.main()

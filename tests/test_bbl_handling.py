"""Kiểm thử ngoại tuyến cho chính sách chuyển đổi và bảo toàn tệp .bbl."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.bbl_handling import (
    BblConversionError,
    apply_bbl_source_policy,
    audit_bbl_sources,
    convert_structured_bbl,
    validate_bbl_source_policy,
)
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

    def _apply_policy(self, files, *, artifact_root=None):
        directory = self.root / "staging" / "v1"
        directory.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return directory, apply_bbl_source_policy(
            directory,
            sorted(files),
            artifact_root=artifact_root or self.root / "artifacts",
            mssv=self.pair[0],
            arxiv_id=self.pair[1],
            version="v1",
        )

    def test_policy_removes_bbl_when_same_version_has_bib_and_keeps_evidence(self):
        directory, (files, policy) = self._apply_policy({
            "main.tex": b"\\documentclass{article}",
            "refs.bib": b"@misc{x, title={X}}",
            "refs.bbl": b"rendered source",
        })

        self.assertEqual(files, ["main.tex", "refs.bib"])
        self.assertFalse((directory / "refs.bbl").exists())
        item = policy["items"][0]
        self.assertEqual(item["status"], "removed_bib_present")
        evidence = self.root / "artifacts" / item["source_evidence_path"]
        self.assertEqual(evidence.read_bytes(), b"rendered source")
        self.assertEqual(
            validate_bbl_source_policy(
                directory, files, policy, artifact_root=self.root / "artifacts",
                mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
            )["status"],
            "verified",
        )

    def test_policy_removes_bbl_when_tex_has_active_bibitem(self):
        directory, (files, policy) = self._apply_policy({
            "main.tex": b"\\documentclass{article}\\bibitem{key}Entry",
            "refs.bbl": b"rendered source",
        })

        self.assertEqual(files, ["main.tex"])
        self.assertFalse((directory / "refs.bbl").exists())
        self.assertEqual(policy["items"][0]["status"], "removed_bibitem_in_tex")

    def test_policy_ignores_commented_bibitem_and_retains_failed_conversion(self):
        directory, (files, policy) = self._apply_policy({
            "main.tex": b"% \\bibitem{commented}\n\\documentclass{article}",
            "refs.bbl": b"\\begin{thebibliography}{1}\\bibitem{x} X\\end{thebibliography}",
        })

        self.assertIn("refs.bbl", files)
        self.assertTrue((directory / "refs.bbl").is_file())
        self.assertEqual(policy["items"][0]["status"], "retained_conversion_failed")
        self.assertEqual(
            validate_bbl_source_policy(
                directory, files, policy, artifact_root=self.root / "artifacts",
                mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
            )["status"],
            "verified",
        )

    def test_policy_converts_supported_bbl_and_records_source_and_derived_checksums(self):
        directory, (files, policy) = self._apply_policy({
            "main.tex": b"\\documentclass{article}",
            "refs.bbl": (
                b"\\entry{x}{article}{}\\field{title}{Title}\\field{year}{2025}\\endentry"
            ),
        })

        item = policy["items"][0]
        self.assertEqual(files, ["main.tex"])
        self.assertEqual(item["status"], "converted_and_removed")
        self.assertEqual(item["entry_count"], 1)
        artifact_root = self.root / "artifacts"
        self.assertEqual(
            hashlib.sha256((artifact_root / item["source_evidence_path"]).read_bytes()).hexdigest(),
            item["bbl_sha256"],
        )
        self.assertEqual(
            hashlib.sha256((artifact_root / item["derived_bib_path"]).read_bytes()).hexdigest(),
            item["derived_bib_sha256"],
        )
        self.assertEqual(
            validate_bbl_source_policy(
                directory, files, policy, artifact_root=artifact_root,
                mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
            )["status"],
            "verified",
        )

    def test_policy_handles_multiple_bbl_files_independently(self):
        directory, (files, policy) = self._apply_policy({
            "main.tex": b"\\documentclass{article}",
            "refs/a.bbl": b"\\entry{x}{article}{}\\field{title}{Title}\\endentry",
            "refs/b.bbl": b"\\begin{thebibliography}{1}\\bibitem{x} X\\end{thebibliography}",
        })

        self.assertEqual([item["status"] for item in policy["items"]], [
            "converted_and_removed", "retained_conversion_failed",
        ])
        self.assertEqual(files, ["main.tex", "refs/b.bbl"])
        self.assertFalse((directory / "refs/a.bbl").exists())
        self.assertTrue((directory / "refs/b.bbl").exists())

    def test_policy_keeps_no_tex_source_status_decision_for_unconvertible_bbl(self):
        directory, (files, policy) = self._apply_policy({
            "refs.bbl": b"\\begin{thebibliography}{1}\\bibitem{x} X\\end{thebibliography}",
        })

        self.assertEqual(files, ["refs.bbl"])
        self.assertTrue((directory / "refs.bbl").is_file())
        self.assertEqual(policy["items"][0]["status"], "retained_conversion_failed")

    def test_policy_fails_closed_when_tex_cannot_be_read(self):
        directory = self.root / "staging" / "v1"
        directory.mkdir(parents=True)
        (directory / "main.tex").write_bytes(b"\\documentclass{article}")
        (directory / "refs.bbl").write_bytes(b"source")
        with patch("src.bbl_handling._tex_has_bibitem", return_value=(None, "tex_unreadable")):
            with self.assertRaisesRegex(BblConversionError, "tex_unreadable"):
                apply_bbl_source_policy(
                    directory, ["main.tex", "refs.bbl"],
                    artifact_root=self.root / "artifacts",
                    mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
                )
        self.assertEqual((directory / "refs.bbl").read_bytes(), b"source")

    def test_policy_refuses_to_overwrite_conflicting_artifact(self):
        files = {
            "main.tex": b"\\documentclass{article}",
            "refs.bbl": b"\\entry{x}{article}{}\\field{title}{Title}\\endentry",
        }
        directory, (_remaining, policy) = self._apply_policy(files)
        derived = self.root / "artifacts" / policy["items"][0]["derived_bib_path"]
        derived.write_bytes(b"conflicting user data")
        retry_dir = self.root / "retry" / "v1"
        retry_dir.mkdir(parents=True)
        (retry_dir / "main.tex").write_bytes(files["main.tex"])
        (retry_dir / "refs.bbl").write_bytes(files["refs.bbl"])

        with self.assertRaisesRegex(BblConversionError, "derived_output_conflict"):
            apply_bbl_source_policy(
                retry_dir, sorted(files), artifact_root=self.root / "artifacts",
                mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
            )

    def test_policy_publishes_with_atomic_rename_when_hardlinks_are_unavailable(self):
        files = {
            "main.tex": b"\\documentclass{article}",
            "refs.bbl": b"\\entry{x}{article}{}\\field{title}{Title}\\endentry",
        }
        with patch("src.bbl_handling.os.link", side_effect=OSError("hardlinks unsupported")):
            _directory, (_remaining, policy) = self._apply_policy(files)
        derived = self.root / "artifacts" / policy["items"][0]["derived_bib_path"]
        self.assertTrue(derived.is_file())
        self.assertIn(b"@article{x,", derived.read_bytes())

    def test_policy_cleans_partial_artifact_on_interrupted_write(self):
        directory = self.root / "staging" / "v1"
        directory.mkdir(parents=True)
        (directory / "main.tex").write_bytes(b"\\documentclass{article}")
        (directory / "refs.bbl").write_bytes(
            b"\\entry{x}{article}{}\\field{title}{Title}\\endentry"
        )
        with patch("src.bbl_handling.os.fsync", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                apply_bbl_source_policy(
                    directory, ["main.tex", "refs.bbl"],
                    artifact_root=self.root / "artifacts",
                    mssv=self.pair[0], arxiv_id=self.pair[1], version="v1",
                )
        self.assertFalse(list((self.root / "artifacts").rglob("*.bib")))
        self.assertTrue((directory / "refs.bbl").is_file())

    def test_storage_resume_verifies_policy_index_and_external_bbl_checksum(self):
        artifact_root = self.root / "artifacts"
        store = CorpusStorage(
            self.root / "policy-corpus", bbl_artifact_root=artifact_root,
        )
        mssv, arxiv_id = self.pair
        store.record_discovery({
            "mssv": mssv,
            "arxiv_id": arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": [f"{arxiv_id}v1"],
            "latest_version_id": f"{arxiv_id}v1",
            "title": "BBL policy test",
            "authors": ["Author"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })
        version_dir = store.begin_version(mssv, arxiv_id, "v1")
        version_dir.mkdir(parents=True)
        (version_dir / "main.tex").write_bytes(b"\\documentclass{article}")
        (version_dir / "refs.bib").write_bytes(b"@misc{x, title={X}}")
        (version_dir / "refs.bbl").write_bytes(b"original bbl")
        files, policy = apply_bbl_source_policy(
            version_dir,
            ["main.tex", "refs.bib", "refs.bbl"],
            artifact_root=artifact_root,
            mssv=mssv,
            arxiv_id=arxiv_id,
            version="v1",
        )
        store.record_source_result({
            "mssv": mssv,
            "arxiv_id": arxiv_id,
            "versioned_id": f"{arxiv_id}v1",
            "status": "success",
            "files": sorted(files),
            "downloaded_bytes": 100,
            "error": None,
            "bbl_policy": policy,
        })

        self.assertTrue(store.should_skip_version(mssv, arxiv_id, "v1"))
        evidence = artifact_root / policy["items"][0]["source_evidence_path"]
        evidence.write_bytes(b"tampered")
        self.assertFalse(store.should_skip_version(mssv, arxiv_id, "v1"))


if __name__ == "__main__":
    unittest.main()

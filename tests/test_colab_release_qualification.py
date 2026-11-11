"""Kiểm thử hợp đồng notebook Colab M1 và các trường hợp lưu trữ ngoại tuyến."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tarfile
import unittest

from src.fast_production import FastProductionRunner, member_cohort_range
from src.id_ranges import load_config
from src.storage import CorpusStorage, arxiv_id_to_folder_key
from src.storage_integrity import audit_paper_integrity


class TestUnifiedNotebookContract(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.path = self.root / "notebooks" / "m1_pipeline_thu_thap_va_kiem_toan.ipynb"
        self.notebook = json.loads(self.path.read_text(encoding="utf-8"))
        self.cells = self.notebook["cells"]
        self.code = ["".join(cell["source"]) for cell in self.cells if cell["cell_type"] == "code"]
        self.text = "\n".join("".join(cell["source"]) for cell in self.cells)

    def test_only_one_operational_notebook_remains(self):
        paths = sorted((self.root / "notebooks").glob("*.ipynb"))
        self.assertEqual(paths, [self.path])

    def test_removed_runner_has_no_documentation_references(self):
        self.assertFalse((self.root / "scripts" / "m1_production.py").exists())
        self.assertFalse((self.root / "tests" / "test_m1_production.py").exists())
        files = [self.root / "README.md", self.root / "CONTRIBUTING.md"]
        files.extend((self.root / "docs").rglob("*.md"))
        for path in files:
            with self.subTest(path=path.relative_to(self.root)):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("scripts/m1_production.py", text)
                self.assertNotIn("test_m1_production.py", text)

    def test_local_documentation_links_resolve(self):
        files = [self.root / "README.md", self.root / "CONTRIBUTING.md"]
        files.extend((self.root / "docs").rglob("*.md"))
        for path in files:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = target.split()[0].split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(path=path.relative_to(self.root), target=target):
                    self.assertTrue((path.parent / target).exists())

    def test_code_cells_compile_and_have_no_stale_outputs(self):
        self.assertEqual(self.notebook["nbformat"], 4)
        for index, cell in enumerate(self.cells):
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), f"cell-{index}", "exec")
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])
        self.assertIn("Đang chạy toàn bộ quy trình M1", self.code[-1])
        self.assertIn("Đang đọc Colab Secret", self.code[-1])

    def test_all_members_use_the_same_full_assignment_configuration(self):
        config = load_config()
        self.assertEqual(len(config), 4)
        for mssv in config:
            cohorts = member_cohort_range(mssv)
            self.assertEqual(len(cohorts), 50)
        source = self.code[1]
        self.assertIn('MSSV = ""', source)
        self.assertIn("COHORT_LIMIT = None", source)
        self.assertNotIn('MSSV = "24120260"', self.text)
        self.assertNotIn("24120260/archives", self.text)

    def test_pipeline_has_one_required_m1_path(self):
        for forbidden in ("BENCHMARK_ARM", "MEMBER_PRODUCTION", 'MODE = "PILOT"', "Source-Only pilot"):
            self.assertNotIn(forbidden, self.text)
        workflow_path = self.root / "src" / "m1_workflow.py"
        workflow = workflow_path.read_text(encoding="utf-8")
        for fragment in ("prepare_snapshot", "reuse_or_build_index", "revision_history=True",
                         "runner.enrich_cohort_s2", "verify_cohort_handoff", "compute_m1_statistics",
                         "export_official_datasets", "summarize_workflow", "SessionBudget"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)
        self.assertEqual(sum("run_m1_member(" in cell for cell in self.code), 1)
        self.assertNotIn("runner.run_cohort", self.text)
        self.assertNotIn("runner.enrich_cohort_s2", self.text)
        self.assertNotIn("prepare_snapshot", self.text)
        self.assertNotIn("export_official_datasets", self.text)
        self.assertLess(workflow.index("runner.run_cohort("), workflow.index("runner.enrich_cohort_s2("))
        self.assertLess(workflow.index("runner.enrich_cohort_s2("), workflow.index("def run_data_audit"))
        self.assertIn("audit_bbl_sources", workflow)
        self.assertIn("arXiv xác nhận bài báo và phiên bản", self.text)
        self.assertIn("COHORT_LIMIT = None", self.text)

    def test_data_handling_rules_live_in_the_data_contract(self):
        contract = (self.root / "docs" / "contracts" / "source_data_contracts.md").read_text(encoding="utf-8")
        for fragment in ("thử chuyển đổi cục bộ", "giữ nguyên `.bbl`", "không sửa manifest hay checksum nguồn",
                         "`ref_submission_date`", "`publicationDate`", "xuất xứ"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, contract)
        self.assertNotIn("publicationDate", self.text)
        self.assertNotIn("thử chuyển đổi cục bộ", self.text)

    def test_runtime_pins_match_implementation_commit(self):
        expected = "cdfde34b8beaed0afc20c7fb660070987cbec4e3"
        setup = self.code[0]
        self.assertIn(f'M1_RUNTIME_COMMIT = "{expected}"', setup)
        self.assertEqual(len(expected), 40)
        import subprocess
        subprocess.run(["git", "cat-file", "-e", f"{expected}^{{commit}}"],
                       cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "merge-base", "--is-ancestor", expected, "HEAD"],
                       cwd=self.root, check=True, capture_output=True)
        for module in ("session_evidence.py", "kaggle_candidates.py", "m1_workflow.py", "bbl_handling.py"):
            self.assertTrue((self.root / "src" / module).is_file())

    def test_s2_and_audit_are_mandatory_and_fail_closed(self):
        workflow = (self.root / "src" / "m1_workflow.py").read_text(encoding="utf-8")
        self.assertIn("runner.enrich_cohort_s2", workflow)
        self.assertIn("export_eligible = reconciled and not integrity_failed and data_complete and not handoff_issues", workflow)
        self.assertIn('"handoff_status": "verified" if handoff_verified', workflow)
        self.assertNotIn("if True:", self.text)

    def test_colab_s2_429_retry_is_bounded_and_benchmark_visible(self):
        workflow = (self.root / "src" / "m1_workflow.py").read_text(encoding="utf-8")
        self.assertIn('"max_attempts": 8', workflow)
        self.assertIn('"max_retry_wait": 600.0', workflow)
        self.assertIn('"backoff_base": 3.0', workflow)
        self.assertIn('"backoff_cap": 60.0', workflow)
        self.assertIn('**S2_RETRY_POLICY', workflow)
        self.assertIn('"s2_stop_reason"', workflow)
        self.assertIn("should_stop=budget.should_stop", workflow)



class TestControlledPilotAndEdgeCases(unittest.TestCase):
    """Phase 4: Test 12 controlled pilot edge cases with expected storage and manifest verification."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_root = Path(self.tmp_dir.name) / "local"
        self.drive_root = Path(self.tmp_dir.name) / "drive"
        self.runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=Path(self.tmp_dir.name) / "scratch",
            retry_429=False,
        )
        self.store = self.runner.store

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _setup_compliant_paper(
        self,
        arxiv_id: str,
        mssv: str,
        versions: list[str],
        files_by_version: dict[str, dict[str, bytes]],
        is_no_tex: bool = False,
        custom_refs: list[dict] | None = None,
    ):
        version_ids = [f"{arxiv_id}{v}" for v in versions]
        self.store.record_discovery({
            "mssv": mssv,
            "arxiv_id": arxiv_id,
            "status": "found",
            "version_status": "resolved",
            "version_ids": version_ids,
            "latest_version_id": version_ids[-1],
            "title": f"Paper {arxiv_id}",
            "authors": ["Author One"],
            "published": "2025-08-01",
            "updated": "2025-08-01",
            "missing_fields": [],
        })

        for v in versions:
            dest = self.store.begin_version(mssv, arxiv_id, v)
            if is_no_tex:
                self.store.record_source_result({
                    "mssv": mssv,
                    "arxiv_id": arxiv_id,
                    "versioned_id": f"{arxiv_id}{v}",
                    "status": "no_tex_source",
                    "files": [],
                    "downloaded_bytes": 0,
                    "error": None,
                })
            else:
                assert dest is not None
                dest.mkdir(parents=True, exist_ok=True)
                vfiles = files_by_version.get(v, {})
                for fname, fbytes in vfiles.items():
                    (dest / fname).write_bytes(fbytes)
                self.store.record_source_result({
                    "mssv": mssv,
                    "arxiv_id": arxiv_id,
                    "versioned_id": f"{arxiv_id}{v}",
                    "status": "success",
                    "files": list(vfiles.keys()),
                    "downloaded_bytes": sum(len(b) for b in vfiles.values()),
                    "error": None,
                })

        meta = self.store.read_metadata(mssv, arxiv_id)
        meta["status"] = "complete"
        meta["title"] = f"Paper {arxiv_id}"
        meta["authors"] = ["Author One"]
        meta["submission_date"] = "2025-08-01"
        meta["revised_dates"] = [f"2025-08-0{i+1}" for i in range(1, len(versions))]
        meta["missing_fields"] = []
        meta["version_status"] = "resolved"
        meta["versions"] = [
            {
                "version": v,
                "versioned_id": f"{arxiv_id}{v}",
                "date": "2025-08-01" if i == 0 else f"2025-08-0{i+1}",
                "date_status": "verified",
            }
            for i, v in enumerate(versions)
        ]
        if len(versions) > 1:
            meta["provenance"]["version_history"] = {
                "source": "arxiv_atom",
                "retrieved_at": "2025-08-01T00:00:00Z",
            }
        meta["provenance"]["semantic_scholar"] = {
            "query_identifier": arxiv_id,
            "s2_paper_id": f"s2_{arxiv_id}",
            "retrieved_at": "2025-08-01T00:00:00Z",
            "snapshot": {"arxiv_id": arxiv_id, "status": "found"},
        }
        self.store.save_metadata(mssv, arxiv_id, meta)

        refs = self.store.read_references(mssv, arxiv_id)
        refs["status"] = "complete"
        refs["provenance"]["query_identifier"] = arxiv_id
        refs["provenance"]["retrieved_at"] = "2025-08-01T00:00:00Z"
        items = custom_refs if custom_refs is not None else []
        refs["coverage"] = {
            "complete": True,
            "continuation": None,
            "reported_total": len(items),
            "retrieved_count": len(items),
        }
        refs["items"] = items
        self.store.save_references(mssv, arxiv_id, refs)

    def test_single_version_paper_storage(self):
        """Case 1 & 3: Single version paper with valid TeX source."""
        self._setup_compliant_paper(
            "2508.07872",
            "24120010",
            ["v1"],
            {"v1": {"main.tex": b"\\documentclass{article}\\begin{document}Hello\\end{document}"}},
        )
        audit_res = audit_paper_integrity(self.store, "24120010", "2508.07872")
        self.assertFalse(audit_res.has_integrity_failure)
        self.assertTrue(audit_res.is_m1_complete)

    def test_multi_version_paper_storage(self):
        """Case 2: Multi-version paper with v1 and v2 sources."""
        self._setup_compliant_paper(
            "2508.07873",
            "24120010",
            ["v1", "v2"],
            {
                "v1": {"main.tex": b"\\documentclass{article}\\begin{document}v1\\end{document}"},
                "v2": {"main.tex": b"\\documentclass{article}\\begin{document}v2 revision\\end{document}"},
            },
        )
        audit_res = audit_paper_integrity(self.store, "24120010", "2508.07873")
        self.assertFalse(audit_res.has_integrity_failure)
        self.assertTrue(audit_res.is_m1_complete)

    def test_no_tex_source_explicit_record(self):
        """Case 4: PDF-only / NO_TEX_SOURCE explicitly recorded without fake tex files."""
        self._setup_compliant_paper(
            "2508.07875",
            "24120010",
            ["v1"],
            {},
            is_no_tex=True,
        )
        audit_res = audit_paper_integrity(self.store, "24120010", "2508.07875")
        self.assertFalse(audit_res.has_integrity_failure)
        self.assertTrue(audit_res.is_m1_complete)

    def test_multi_source_and_bib_files(self):
        """Case 5 & 6: Multi-source files including .bib."""
        self._setup_compliant_paper(
            "2508.07876",
            "24120010",
            ["v1"],
            {
                "v1": {
                    "main.tex": b"\\include{intro}",
                    "intro.tex": b"Introduction content",
                    "refs.bib": b"@article{key, title={Test}}",
                }
            },
        )
        audit_res = audit_paper_integrity(self.store, "24120010", "2508.07876")
        self.assertFalse(audit_res.has_integrity_failure)
        self.assertTrue(audit_res.is_m1_complete)

    def test_figure_exclusion_and_tex_command_preservation(self):
        """Case 7: Figure commands preserved in TeX, image binary files excluded."""
        tex_with_fig = b"\\documentclass{article}\n\\includegraphics{fig1.png}\n\\end{document}"
        folder = self.runner.corpus_dir / "2508-07878"
        v1 = folder / "src" / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        (v1 / "main.tex").write_bytes(tex_with_fig)

        self.assertIn(b"\\includegraphics{fig1.png}", (v1 / "main.tex").read_bytes())
        # Ensure no png was created
        self.assertFalse((v1 / "fig1.png").exists())

    def test_s2_references_without_arxiv_ids_preserved(self):
        """Case 8 & 9: S2 references preserved even when without arXiv ID."""
        custom_refs = [
            {
                "arxiv_id": "2501.00001",
                "title": "Paper One",
                "authors": ["A1"],
                "submission_date": "2024-01-01",
                "publication_date": "2024-01-01",
                "year": 2024,
                "venue": "NeurIPS",
                "s2_paper_id": "s2_1",
                "missing_fields": [],
                "raw": {},
            },
            {
                "arxiv_id": None,
                "title": "Book Citation Without Arxiv",
                "authors": ["A2"],
                "submission_date": None,
                "publication_date": "2023-01-01",
                "year": 2023,
                "venue": "Book Title",
                "s2_paper_id": "s2_2",
                "missing_fields": ["arxiv_id", "submission_date"],
                "raw": {},
            },
            {
                "arxiv_id": None,
                "title": "Conference Paper",
                "authors": ["A3"],
                "submission_date": None,
                "publication_date": "2022-01-01",
                "year": 2022,
                "venue": "ICML",
                "s2_paper_id": "s2_3",
                "missing_fields": ["arxiv_id", "submission_date"],
                "raw": {},
            },
        ]
        self._setup_compliant_paper(
            "2508.07879",
            "24120010",
            ["v1"],
            {"v1": {"main.tex": b"\\documentclass{article}\\begin{document}content\\end{document}"}},
            custom_refs=custom_refs,
        )

        read_refs = self.store.read_references("24120010", "2508.07879")
        self.assertEqual(len(read_refs["items"]), 3)
        self.assertIsNone(read_refs["items"][1]["arxiv_id"])
        self.assertIsNone(read_refs["items"][2]["arxiv_id"])


class TestCheckpointInterruptionAndResume(unittest.TestCase):
    """Phase 5: Test interruption simulation and fresh-runtime resume idempotency."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_root = Path(self.tmp_dir.name) / "local"
        self.drive_root = Path(self.tmp_dir.name) / "drive"
        self.runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=Path(self.tmp_dir.name) / "scratch",
            retry_429=False,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_interruption_and_fresh_runtime_resume(self):
        """Simulate partial completion of 1 paper in cohort 1, then resume in a fresh runner."""
        corpus = self.runner.corpus_dir
        p1 = corpus / "2508-07872"
        v1 = p1 / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        (v1 / "main.tex").write_text("p1", encoding="utf-8")
        (p1 / "manifest.json").write_text(json.dumps({
            "status": "complete",
            "arxiv_id": "2508.07872",
            "mssv": "24120010",
            "completed_at": "2026-11-10T00:00:00Z",
        }), encoding="utf-8")

        # Archive partial state to Drive
        archive_path = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        self.runner._archive_cohort(1, archive_path)
        self.assertTrue(archive_path.exists())

        # Fresh runtime simulation: create a new runner pointing to fresh local workspace
        fresh_local = Path(self.tmp_dir.name) / "fresh_local"
        fresh_runner = FastProductionRunner(
            fresh_local,
            self.drive_root,
            scratch_root=Path(self.tmp_dir.name) / "fresh_scratch",
            retry_429=False,
        )
        self.assertFalse((fresh_runner.corpus_dir / "2508-07872").exists())

        # Restore from Drive in fresh runtime
        restored = fresh_runner.restore_cohort(1)
        self.assertEqual(restored, 1)
        self.assertTrue((fresh_runner.corpus_dir / "2508-07872" / "manifest.json").exists())
        self.assertEqual((fresh_runner.corpus_dir / "2508-07872" / "v1" / "main.tex").read_text(encoding="utf-8"), "p1")

    def test_idempotent_repeated_execution(self):
        """Running the same completed cohort again does not alter files or member contents."""
        p1 = self.runner.corpus_dir / "2508-07872"
        (p1 / "src" / "v1").mkdir(parents=True, exist_ok=True)
        tex_path = p1 / "src" / "v1" / "main.tex"
        tex_path.write_text("unchanged content", encoding="utf-8")
        orig_hash = hashlib.sha256(tex_path.read_bytes()).hexdigest()
        (p1 / "manifest.json").write_text(json.dumps({
            "status": "complete", "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "completed_at": "2026-11-10T00:00:00Z", "discovery_status": "found", "version_status": "resolved",
            "metadata_status": "complete", "references_status": "complete",
            "versions": {"v1": {"status": "success", "files": ["main.tex"], "file_checksums": {"main.tex": orig_hash}}}
        }), encoding="utf-8")

        # Archive once
        a1 = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        self.runner._archive_cohort(1, a1)

        with tarfile.open(a1, "r:gz") as tar1:
            members1 = sorted(m.name for m in tar1.getmembers())
            f1_bytes = tar1.extractfile("2508-07872/src/v1/main.tex").read()

        # Re-archive (idempotency check)
        self.runner._archive_cohort(1, a1)

        with tarfile.open(a1, "r:gz") as tar2:
            members2 = sorted(m.name for m in tar2.getmembers())
            f2_bytes = tar2.extractfile("2508-07872/src/v1/main.tex").read()

        self.assertEqual(orig_hash, hashlib.sha256(tex_path.read_bytes()).hexdigest())
        self.assertEqual(members1, members2)
        self.assertEqual(f1_bytes, f2_bytes)
        self.assertEqual(f1_bytes, b"unchanged content")

    def test_real_subprocess_interruption_and_clean_resume(self):
        """R04: Test real process SIGTERM termination and fresh-process resumption."""
        flag_file = Path(self.tmp_dir.name) / "process_ready.flag"
        local_p = self.local_root
        drive_p = self.drive_root

        child_code = f"""
import sys, time, json, hashlib
from pathlib import Path
from src.fast_production import FastProductionRunner

runner = FastProductionRunner({repr(str(local_p))}, {repr(str(drive_p))}, retry_429=False)
p1 = runner.corpus_dir / "2508-07872"
v1 = p1 / "v1"
v1.mkdir(parents=True, exist_ok=True)
content = b"subprocess paper 1"
(v1 / "main.tex").write_bytes(content)
(p1 / "manifest.json").write_text(json.dumps({{
    "status": "complete", "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
    "completed_at": "2026-11-10T00:00:00Z", "discovery_status": "found", "version_status": "resolved",
    "metadata_status": "complete", "references_status": "complete",
    "versions": {{"v1": {{"status": "success", "files": ["main.tex"], "file_checksums": {{"main.tex": hashlib.sha256(content).hexdigest()}}}}}}
}}))
arch = runner.archives_dir / "cohort_0001_corpus.tar.gz"
runner._archive_cohort(1, arch)

# Signal parent and sleep (simulating running process mid-cohort)
Path({repr(str(flag_file))}).write_text("ready")
time.sleep(30)
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            start_wait = time.time()
            while not flag_file.exists() and time.time() - start_wait < 10.0:
                time.sleep(0.05)
            self.assertTrue(flag_file.exists(), "Child process did not signal readiness")

            # Terminate process with SIGTERM
            proc.terminate()
            proc.wait(timeout=5.0)
            self.assertIsNotNone(proc.returncode, "Child process failed to terminate")
            self.assertNotEqual(proc.returncode, 0, "Terminated process should have non-zero exit")
        finally:
            if proc.poll() is None:
                proc.kill()

        # Start clean fresh process that resumes from Drive
        fresh_local = Path(self.tmp_dir.name) / "fresh_resumed_local"
        fresh_runner = FastProductionRunner(fresh_local, drive_p, retry_429=False)
        self.assertFalse((fresh_runner.corpus_dir / "2508-07872").exists())

        restored_count = fresh_runner.restore_cohort(1)
        self.assertEqual(restored_count, 1)
        self.assertTrue((fresh_runner.corpus_dir / "2508-07872" / "v1" / "main.tex").exists())
        self.assertEqual((fresh_runner.corpus_dir / "2508-07872" / "v1" / "main.tex").read_bytes(), b"subprocess paper 1")


class TestArchiveRestoreAndHandoffIntegrity(unittest.TestCase):
    """Phase 6 / R03: Test production archive, restore API, corruption rejection, and handoff verification."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_root = Path(self.tmp_dir.name) / "local"
        self.drive_root = Path(self.tmp_dir.name) / "drive"
        self.runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=Path(self.tmp_dir.name) / "scratch",
            retry_429=False,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_independent_destination_restore_via_production_api(self):
        """Restore archive into a completely separate destination using production restore_cohort() API."""
        p = self.runner.corpus_dir / "2508-07872"
        v1 = p / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        content = b"\\documentclass{article}\\begin{document}hello\\end{document}"
        (v1 / "main.tex").write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        (p / "manifest.json").write_text(json.dumps({
            "arxiv_id": "2508.07872",
            "mssv": "24120010",
            "completed_at": "2026-11-10T00:00:00Z",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "complete",
            "references_status": "complete",
            "status": "complete",
            "contract_version": 2,
            "versions": {"v1": {"status": "success", "files": ["main.tex"], "file_checksums": {"main.tex": sha}}},
        }), encoding="utf-8")

        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        self.runner._archive_cohort(1, arch)
        self.assertTrue(arch.exists())

        # Test restore via production API into a clean independent runner
        separate_local = Path(self.tmp_dir.name) / "separate_destination"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        restored = fresh_runner.restore_cohort(1)
        self.assertEqual(restored, 1)

        restored_paper = fresh_runner.corpus_dir / "2508-07872"
        self.assertTrue(restored_paper.exists())
        self.assertTrue((restored_paper / "v1" / "main.tex").exists())
        restored_sha = hashlib.sha256((restored_paper / "v1" / "main.tex").read_bytes()).hexdigest()
        self.assertEqual(sha, restored_sha)

    def test_restore_rejects_checksum_mismatch(self):
        """R03: restore_cohort rejects archive when .sha256 sidecar does not match."""
        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        with tarfile.open(arch, "w:gz") as tar:
            pass
        sha_file = arch.with_name(arch.name + ".sha256")
        sha_file.write_text("bad_checksum cohort_0001_corpus.tar.gz")

        separate_local = Path(self.tmp_dir.name) / "separate_local"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        with self.assertRaises(ValueError) as cm:
            fresh_runner.restore_cohort(1, require_manifest=True)
        self.assertIn("checksum mismatch", str(cm.exception).lower())

    def test_restore_rejects_path_traversal_archive(self):
        """R03: restore_cohort rejects tar member containing relative path traversal (../)."""
        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        with tarfile.open(arch, "w:gz") as tar:
            ti = tarfile.TarInfo(name="../evil.txt")
            ti.size = 4
            tar.addfile(ti, io.BytesIO(b"evil"))
        actual_sha = self.runner._compute_sha256(arch)
        arch.with_name(arch.name + ".sha256").write_text(f"{actual_sha} {arch.name}")

        separate_local = Path(self.tmp_dir.name) / "separate_local_traversal"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        with self.assertRaises(ValueError) as cm:
            fresh_runner.restore_cohort(1)
        self.assertIn("Unsafe archive member", str(cm.exception))

    def test_restore_rejects_absolute_path_archive(self):
        """R03: restore_cohort rejects tar member containing absolute path."""
        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        with tarfile.open(arch, "w:gz") as tar:
            ti = tarfile.TarInfo(name="/etc/passwd")
            ti.size = 4
            tar.addfile(ti, io.BytesIO(b"root"))
        actual_sha = self.runner._compute_sha256(arch)
        arch.with_name(arch.name + ".sha256").write_text(f"{actual_sha} {arch.name}")

        separate_local = Path(self.tmp_dir.name) / "separate_local_abs"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        with self.assertRaises(ValueError) as cm:
            fresh_runner.restore_cohort(1)
        self.assertIn("Unsafe archive member", str(cm.exception))

    def test_restore_rejects_symlink_in_archive(self):
        """R03: restore_cohort rejects archive containing symlinks."""
        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        with tarfile.open(arch, "w:gz") as tar:
            ti = tarfile.TarInfo(name="symlink_member")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)
        actual_sha = self.runner._compute_sha256(arch)
        arch.with_name(arch.name + ".sha256").write_text(f"{actual_sha} {arch.name}")

        separate_local = Path(self.tmp_dir.name) / "separate_local_symlink"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        with self.assertRaises(ValueError) as cm:
            fresh_runner.restore_cohort(1)
        self.assertIn("Special files not allowed", str(cm.exception))

    def test_restore_ignores_foreign_papers_not_in_cohort(self):
        """R03: restore_cohort strictly ignores folders belonging to another cohort."""
        arch = self.runner.archives_dir / "cohort_0001_corpus.tar.gz"
        with tarfile.open(arch, "w:gz") as tar:
            # Add paper belonging to cohort 51 (2507-16659)
            ti = tarfile.TarInfo(name="2507-16659/v1/main.tex")
            ti.size = 5
            tar.addfile(ti, io.BytesIO(b"hello"))
        actual_sha = self.runner._compute_sha256(arch)
        arch.with_name(arch.name + ".sha256").write_text(f"{actual_sha} {arch.name}")

        separate_local = Path(self.tmp_dir.name) / "separate_local_foreign"
        fresh_runner = FastProductionRunner(separate_local, self.drive_root, retry_429=False)
        restored = fresh_runner.restore_cohort(1)
        self.assertEqual(restored, 0)
        self.assertFalse((fresh_runner.corpus_dir / "2507-16659").exists())


if __name__ == "__main__":
    unittest.main()

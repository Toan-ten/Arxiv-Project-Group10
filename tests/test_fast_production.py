"""Tests for high-throughput fast production runner."""
import ast
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
    validate_and_checkout_commit,
)


class TestFastProduction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.local_root = self.root / "local"
        self.drive_root = self.root / "drive"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cohort_pairs_bounds(self):
        c1 = cohort_pairs(1)
        self.assertEqual(len(c1), 100)
        self.assertEqual(c1[0][0], "24120010")
        self.assertEqual(c1[0][1], "2508.07872")
        self.assertEqual(c1[-1][0], "24120010")
        self.assertEqual(c1[-1][1], "2508.07971")

        with self.assertRaises(ValueError):
            cohort_pairs(0)
        with self.assertRaises(ValueError):
            cohort_pairs(201)

    def test_runner_initialization_and_directories(self):
        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
            retry_429=True,
            retry_wait_budget=150.0,
            timeout=20.0,
        )
        self.assertTrue(runner.corpus_dir.exists())
        self.assertTrue(runner.logs_dir.exists())
        self.assertTrue(runner.reports_dir.exists())
        self.assertTrue(runner.archives_dir.exists())
        self.assertTrue(runner.scratch_dir.exists())
        self.assertEqual(runner.retry_wait_budget, 150.0)
        self.assertEqual(runner.timeout, 20.0)

    def test_archive_and_restore_cohort(self):
        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
            retry_429=False,
        )
        # Create a fake paper directory
        sample_paper = runner.corpus_dir / "2508-07872"
        v1_dir = sample_paper / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        (v1_dir / "main.tex").write_text("Sample LaTeX content", encoding="utf-8")
        (sample_paper / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")

        # Archive cohort 1
        archive_dest = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        runner._archive_cohort(1, archive_dest)
        self.assertTrue(archive_dest.exists())

        # Simulate fresh Colab session: wipe local_root
        import shutil
        shutil.rmtree(self.local_root)
        self.assertFalse((self.local_root / "corpus").exists())

        # Re-initialize runner and restore cohort 1 from Drive archive
        runner2 = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
        )
        restored_count = runner2.restore_cohort(1)
        self.assertEqual(restored_count, 1)
        self.assertTrue((runner2.corpus_dir / "2508-07872/v1/main.tex").exists())
        self.assertEqual(
            (runner2.corpus_dir / "2508-07872/v1/main.tex").read_text(encoding="utf-8"),
            "Sample LaTeX content"
        )

    def test_preload_kaggle_discovery_logs_benchmark(self):
        import json
        snapshot_file = self.root / "snapshot.json"
        snapshot_file.write_text(json.dumps({
            "id": "2508.07872",
            "title": "Test Title",
            "authors_parsed": [["Doe", "Jane", ""]],
            "versions": [{"version": "v1", "created": "Mon, 11 Aug 2025 12:00:00 GMT"}],
        }) + "\n", encoding="utf-8")

        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
        )
        res = runner.preload_kaggle_discovery(snapshot_file, [1])
        self.assertEqual(res["found_papers"], 1)
        self.assertIn("elapsed_seconds", res)
        self.assertTrue(Path(res["report_path"]).exists())

    def test_golden_ratio_backoff_policy_schedule(self):
        from src import arxiv_client
        with arxiv_client.golden_ratio_backoff_policy(base_seconds=8.0, cap_seconds=60.0) as schedule:
            waits = [schedule(k) for k in range(1, 6)]
            # Deterministic bounds: 8.0 * phi^(k-1) <= w_k <= 8.0 * phi^(k-1) + 1.0
            for k, w in enumerate(waits, start=1):
                lower = min(8.0 * (arxiv_client.PHI ** (k - 1)), 60.0)
                self.assertGreaterEqual(w, lower)
                self.assertLessEqual(w, lower + 1.0)
            # Cumulative wait across 5 retries exceeds the 120s CDN sliding window
            self.assertGreater(sum(waits), 130.0)

    def test_failed_archive_preserves_existing_backup_and_restore_never_overwrites_newer_local(self):
        runner = FastProductionRunner(self.local_root, self.drive_root, scratch_root=self.local_root / "scratch")
        paper_dir = runner.corpus_dir / "2508-07872"
        (paper_dir / "v1").mkdir(parents=True, exist_ok=True)
        (paper_dir / "v1/main.tex").write_text("OLD_CONTENT", encoding="utf-8")
        (paper_dir / "manifest.json").write_text('{"updated_at": "2026-01-01T00:00:00Z", "status": "partial"}', encoding="utf-8")
        archive_dest = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        runner._archive_cohort(1, archive_dest)
        original_bytes = archive_dest.read_bytes()

        # Simulate failure during _archive_cohort: original archive must remain intact
        with patch("tarfile.TarFile.add", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                runner._archive_cohort(1, archive_dest)
        self.assertEqual(archive_dest.read_bytes(), original_bytes)

        # Update local file with newer timestamp; restore_cohort must not overwrite it with OLD_CONTENT
        (paper_dir / "v1/main.tex").write_text("NEW_CONTENT", encoding="utf-8")
        (paper_dir / "manifest.json").write_text('{"updated_at": "2026-11-09T00:00:00Z", "status": "complete"}', encoding="utf-8")
        runner.restore_cohort(1)
        self.assertEqual((paper_dir / "v1/main.tex").read_text(encoding="utf-8"), "NEW_CONTENT")

    def test_config_change_never_deletes_existing_batch_logs_and_migrates_legacy_drive_corpus(self):
        # Create legacy Drive corpus paper and existing batch log
        legacy_paper = self.drive_root / "corpus" / "2508-07872"
        legacy_paper.mkdir(parents=True, exist_ok=True)
        (legacy_paper / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")

        runner = FastProductionRunner(self.local_root, self.drive_root, retry_429=False)
        old_log_dir = runner.logs_dir / "fast-arxiv-0001" / "attempts"
        old_log_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = old_log_dir / "attempt1.json"
        evidence_file.write_text('{"evidence": true}', encoding="utf-8")
        (runner.logs_dir / "fast-arxiv-0001" / "batch.json").write_text(
            '{"definition": {"max_attempts": 1, "revision_history": false}}', encoding="utf-8"
        )

        # Restoring cohort 1 migrates legacy Drive corpus to local SSD
        restored = runner.restore_cohort(1)
        self.assertGreaterEqual(restored, 1)
        self.assertTrue((runner.corpus_dir / "2508-07872" / "manifest.json").exists())

        # Changing runner retry_429/max_attempts must NEVER delete old_log_dir/attempt1.json
        runner_retry = FastProductionRunner(self.local_root, self.drive_root, retry_429=True)
        batch_id, _ = runner_retry._resolve_batch_id(1, max_attempts=6, revision_history=False)
        self.assertTrue(evidence_file.exists())
        self.assertNotEqual(batch_id, "fast-arxiv-0001")

    def test_notebooks_do_not_contain_hardcoded_credentials(self):
        repo_root = Path(__file__).resolve().parents[1]
        forbidden_prefixes = ["K" + "GAT_", "g" + "hp_", "s" + "2k-", "g" + "ho_", "gl" + "pat-"]
        for nb_path in (repo_root / "notebooks").glob("*.ipynb"):
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
            for cell in nb.get("cells", []):
                src = "".join(cell.get("source", []))
                for prefix in forbidden_prefixes:
                    self.assertNotIn(prefix, src, f"Hardcoded credential '{prefix}' found in {nb_path}")

    def test_restore_rejects_corrupted_backup_and_preserves_good_local_data(self):
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_dir = runner.corpus_dir / "2508-07872"
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "manifest.json").write_text('{"updated_at": "2026-11-09T01:00:00Z", "status": "complete"}')
        (paper_dir / "v1").mkdir(exist_ok=True)
        (paper_dir / "v1" / "main.tex").write_text("GOOD_LOCAL_BYTES")

        # Giả lập archive trên Drive có manifest corrupt cho 2508-07872 và 2508-07873 (chưa có ở local)
        archive_path = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        import tarfile
        with tarfile.open(archive_path, "w:gz") as tar:
            ti = tarfile.TarInfo("2508-07872/manifest.json")
            data = b"NOT_VALID_JSON"
            ti.size = len(data)
            import io
            tar.addfile(ti, io.BytesIO(data))

            ti2 = tarfile.TarInfo("2508-07873/manifest.json")
            data2 = b"CORRUPTED_MANIFEST"
            ti2.size = len(data2)
            tar.addfile(ti2, io.BytesIO(data2))

        # Restore phải bỏ qua các bản hỏng này, bảo toàn 2508-07872 và không nạp 2508-07873
        runner.restore_cohort(1)
        self.assertEqual((paper_dir / "v1" / "main.tex").read_text(), "GOOD_LOCAL_BYTES")
        self.assertFalse((runner.corpus_dir / "2508-07873").exists())

    def test_restore_rejects_checksum_mismatched_archive(self):
        """Task 2.4: restore_cohort rejects archive if its .sha256 digest does not match."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_dir = runner.corpus_dir / "2508-07872"
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")

        archive_dest = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        runner._archive_cohort(1, archive_dest)
        self.assertTrue(archive_dest.exists())
        sha_file = archive_dest.with_name(archive_dest.name + ".sha256")
        self.assertTrue(sha_file.exists())

        # Tamper with the sha256 file
        sha_file.write_text("0000000000000000000000000000000000000000000000000000000000000000  cohort_0001_corpus.tar.gz\n")

        with self.assertRaises(ValueError) as ctx:
            runner.restore_cohort(1)
        self.assertIn("checksum mismatch", str(ctx.exception).lower())

    def test_restore_ignores_foreign_papers_not_in_cohort(self):
        """Task 2.4: restore_cohort ignores foreign papers in archive that do not belong to the cohort."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        archive_path = runner.archives_dir / "cohort_0001_corpus.tar.gz"

        # Construct archive containing valid cohort 1 paper and a foreign paper (from cohort 200)
        import io
        import tarfile
        with tarfile.open(archive_path, "w:gz") as tar:
            # Valid paper 2508-07872 (in cohort 1)
            t1 = tarfile.TarInfo("2508-07872/manifest.json")
            data1 = b'{"status": "complete", "arxiv_id": "2508.07872"}'
            t1.size = len(data1)
            tar.addfile(t1, io.BytesIO(data1))

            # Foreign paper 2501-07641 (belongs to student 24120260 in cohort 200)
            t2 = tarfile.TarInfo("2501-07641/manifest.json")
            data2 = b'{"status": "complete", "arxiv_id": "2501.07641"}'
            t2.size = len(data2)
            tar.addfile(t2, io.BytesIO(data2))

        # Update matching sha256
        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        sha_file.write_text(f"{runner._compute_sha256(archive_path)}  {archive_path.name}\n")

        restored = runner.restore_cohort(1)
        self.assertEqual(restored, 1)
        self.assertTrue((runner.corpus_dir / "2508-07872").exists())
        self.assertFalse((runner.corpus_dir / "2501-07641").exists())

    def test_safe_replace_paper_verifies_file_checksums(self):
        """Task 2.4: _safe_replace_paper rejects replacing if source file sha256 does not match."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        src_paper = self.root / "stage_src" / "2508-07872"
        src_paper.mkdir(parents=True, exist_ok=True)
        v1_dir = src_paper / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        (v1_dir / "main.tex").write_text("TAMPERED_TEX_CONTENT", encoding="utf-8")

        # Manifest claims hash of different content
        expected_hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        manifest_data = {
            "status": "complete",
            "arxiv_id": "2508.07872",
            "versions": {
                "v1": {
                    "status": "success",
                    "file_checksums": {"main.tex": expected_hash},
                }
            },
        }
        (src_paper / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

        target_paper = runner.corpus_dir / "2508-07872"
        ok = runner._safe_replace_paper(src_paper, target_paper, expected_arxiv_id="2508.07872")
        self.assertFalse(ok)
        self.assertFalse(target_paper.exists())

    def test_count_completed_papers_terminal_only(self):
        """Task 3.2: count_completed_papers only counts terminal verified papers, not failed/unsafe."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        # Paper 1: Terminal success
        p1 = runner.corpus_dir / "2508-07872"
        p1.mkdir(parents=True, exist_ok=True)
        (p1 / "manifest.json").write_text(json.dumps({
            "discovery_status": "found",
            "version_status": "resolved",
            "versions": {"v1": {"status": "success"}},
        }), encoding="utf-8")

        # Paper 2: unsafe_archive error (non-terminal)
        p2 = runner.corpus_dir / "2508-07873"
        p2.mkdir(parents=True, exist_ok=True)
        (p2 / "manifest.json").write_text(json.dumps({
            "discovery_status": "found",
            "version_status": "resolved",
            "versions": {"v1": {"status": "unsafe_archive"}},
        }), encoding="utf-8")

        # Paper 3: not_found (terminal negative)
        p3 = runner.corpus_dir / "2508-07874"
        p3.mkdir(parents=True, exist_ok=True)
        (p3 / "manifest.json").write_text(json.dumps({
            "discovery_status": "not_found",
            "version_status": "none",
        }), encoding="utf-8")

        completed = runner.count_completed_papers(1)
        # Only Paper 1 and Paper 3 should count, NOT Paper 2
        self.assertEqual(completed, 2)

    def test_run_cohort_defaults_to_revision_history_true(self):
        """Task 3.1: FastProductionRunner.run_cohort has revision_history=True as default."""
        import inspect
        sig = inspect.signature(FastProductionRunner.run_cohort)
        self.assertTrue(sig.parameters["revision_history"].default)

    def test_resolve_batch_id_checks_timeout_fingerprint(self):
        """Task 3.4: _resolve_batch_id checks timeout and prevents resuming if definition differs."""
        runner = FastProductionRunner(self.local_root, self.drive_root, timeout=30.0)
        base_dir = runner.logs_dir / "fast-arxiv-0001"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "batch.json").write_text(json.dumps({
            "definition": {"max_attempts": 6, "revision_history": True, "timeout": 30.0}
        }), encoding="utf-8")

        # Matching config returns base_id and True
        batch_id, can_resume = runner._resolve_batch_id(1, max_attempts=6, revision_history=True, timeout=30.0)
        self.assertEqual(batch_id, "fast-arxiv-0001")
        self.assertTrue(can_resume)

        # Mismatched timeout creates variant ID and does not resume base_id
        batch_id_mismatch, can_resume_mismatch = runner._resolve_batch_id(1, max_attempts=6, revision_history=True, timeout=60.0)
        self.assertNotEqual(batch_id_mismatch, "fast-arxiv-0001")
        self.assertFalse(can_resume_mismatch)

    def test_enrich_cohort_s2_invokes_run_cohort_with_s2(self):
        """Task 5.3: enrich_cohort_s2 configures S2 and runs safe enrichment."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        with patch.object(runner, "run_cohort", return_value={"status": "enriched"}) as mock_run:
            custom_s2 = {"timeout": 15.0, "max_attempts": 2, "min_interval": 2.0}
            res = runner.enrich_cohort_s2(1, s2_config=custom_s2)
            self.assertEqual(res, {"status": "enriched"})
            mock_run.assert_called_once_with(1, resume=True, revision_history=True, s2_config=custom_s2)


    def test_notebooks_and_source_do_not_contain_hardcoded_credentials(self):
        """Phase 0 / Step 0.3: No hardcoded credentials (Kaggle, S2, GitHub) in notebooks or source/test files.
        
        Scans:
        1. Notebooks: source cells, cell outputs (text, stream, display, errors), cell metadata, notebook metadata.
        2. Python files in src/, scripts/, and tests/.
        """
        repo_root = Path(__file__).resolve().parents[1]
        forbidden_prefixes = ["K" + "GAT_", "g" + "hp_", "s" + "2k-", "g" + "ho_", "gl" + "pat-"]
        credential_patterns = [
            # Direct variable assignment: KEY = "token_value"
            re.compile(r"""(?:KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)\s*=\s*['"][a-zA-Z0-9_\-]{16,}['"]"""),
            # os.environ assignment: os.environ[...] = "token_value"
            re.compile(r"""(?:os\.environ|environ)(?:\[['"]|\.get\(['"]|\.setdefault\(['"])(?:KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)['"]\s*\]?\s*=\s*['"][a-zA-Z0-9_\-]{16,}['"]"""),
            # Dict literal assignment: "KEY": "token_value"
            re.compile(r"""['"](?:KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)['"]\s*:\s*['"][a-zA-Z0-9_\-]{16,}['"]"""),
        ]
        mock_keywords = {"mock", "fake", "dummy", "test", "offline", "example", "sample"}

        # Check all notebooks: source, outputs, and metadata
        for nb_path in (repo_root / "notebooks").glob("*.ipynb"):
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
            
            # Check notebook-level metadata
            nb_meta_str = json.dumps(nb.get("metadata", {}))
            for prefix in forbidden_prefixes:
                self.assertNotIn(prefix, nb_meta_str, f"Credential prefix '{prefix}' found in notebook metadata of {nb_path.name}")
            for pat in credential_patterns:
                self.assertIsNone(pat.search(nb_meta_str), f"Credential pattern found in notebook metadata of {nb_path.name}")

            for idx, cell in enumerate(nb.get("cells", [])):
                # Check cell metadata
                cell_meta_str = json.dumps(cell.get("metadata", {}))
                for prefix in forbidden_prefixes:
                    self.assertNotIn(prefix, cell_meta_str, f"Credential prefix '{prefix}' in {nb_path.name} cell {idx} metadata")
                for pat in credential_patterns:
                    self.assertIsNone(pat.search(cell_meta_str), f"Credential pattern in {nb_path.name} cell {idx} metadata")

                # Check cell source
                src = "".join(cell.get("source", []))
                for prefix in forbidden_prefixes:
                    self.assertNotIn(prefix, src, f"Credential prefix '{prefix}' in {nb_path.name} cell {idx} source")
                for pat in credential_patterns:
                    self.assertIsNone(pat.search(src), f"Credential assignment in {nb_path.name} cell {idx} source")

                # Check cell outputs
                for out_idx, out in enumerate(cell.get("outputs", [])):
                    out_str = json.dumps(out)
                    for prefix in forbidden_prefixes:
                        self.assertNotIn(prefix, out_str, f"Credential prefix '{prefix}' in {nb_path.name} cell {idx} output {out_idx}")
                    for pat in credential_patterns:
                        self.assertIsNone(pat.search(out_str), f"Credential assignment in {nb_path.name} cell {idx} output {out_idx}")

        # Check all python files in src/, scripts/, and tests/
        for folder in ["src", "scripts", "tests"]:
            for py_path in (repo_root / folder).rglob("*.py"):
                content = py_path.read_text(encoding="utf-8")
                # Exclude this test file itself from prefix check to avoid self-match on test assertions
                if py_path.resolve() != Path(__file__).resolve():
                    for prefix in forbidden_prefixes:
                        self.assertNotIn(prefix, content, f"Hardcoded credential '{prefix}...' found in {py_path}")
                for pat in credential_patterns:
                    for match in pat.finditer(content):
                        matched_val = match.group(0)
                        # In tests/, allow mock/offline values for mocking environment
                        if folder == "tests" and any(k in matched_val.lower() for k in mock_keywords):
                            continue
                        self.fail(f"Hardcoded credential assignment found in {py_path}: {matched_val}")

    def test_notebooks_do_not_contain_destructive_drive_cleanup(self):
        """Phase 0 / P0-01: No notebook may contain destructive deletion of drive/production data.
        
        Detection Heuristics:
        1. Regex scanning: detects rmtree, remove, unlink, and rm -rf targeting drive/production paths.
        2. AST inspection: parses code cells into AST and verifies no call to rmtree, remove, unlink, or rmdir
           passes string arguments containing drive/production paths.
        3. Strict policy: fast production notebook contains 0 calls to rmtree.
        
        Limitation Notice:
        Static analysis (Regex and AST) verifies direct and statically-resolvable calls. It cannot guarantee
        absolute detection against runtime dynamic path resolution or obfuscated execution (e.g. getattr,
        eval, or external binaries). Operational safeguards (Colab single-writer, isolated local scratch)
        remain required defense-in-depth measures.
        """
        repo_root = Path(__file__).resolve().parents[1]
        drive_danger_patterns = [
            re.compile(r"""(?:shutil\.)?rmtree\s*\(\s*[^)]*(?:drive|Drive|DRIVE|MyDrive|Group10|production)[^)]*\)"""),
            re.compile(r"""(?:os\.)?(?:remove|unlink)\s*\(\s*[^)]*(?:drive|Drive|DRIVE|MyDrive|Group10)[^)]*\)"""),
            re.compile(r"""Path\s*\(\s*['"][^'"]*(?:drive|Drive|MyDrive|Group10)[^'"]*['"]\s*\)\s*\.unlink"""),
            re.compile(r"""(?:!|%|subprocess\.run\s*\(\s*\[\s*['"])rm\s+-[rR]f\s+.*(?:drive|Drive|MyDrive|Group10)"""),
        ]
        drive_keywords = {"drive", "mydrive", "group10"}

        class DestructiveCallVisitor(ast.NodeVisitor):
            def __init__(self, filename):
                self.filename = filename
                self.violations = []

            def visit_Call(self, node):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name in {"rmtree", "remove", "unlink", "rmdir"}:
                    for arg in ast.walk(node):
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if any(k in arg.value.lower() for k in drive_keywords):
                                self.violations.append((node.lineno, func_name, arg.value))
                self.generic_visit(node)

        for nb_path in (repo_root / "notebooks").glob("*.ipynb"):
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
            for idx, cell in enumerate(nb.get("cells", [])):
                src = "".join(cell.get("source", []))
                
                # 1. Regex check
                for pattern in drive_danger_patterns:
                    match = pattern.search(src)
                    self.assertIsNone(
                        match,
                        f"Destructive drive cleanup operation matching {pattern.pattern} found in {nb_path.name} cell {idx}: {match.group(0) if match else ''}",
                    )

                # 2. AST check (for code cells)
                if cell.get("cell_type") == "code":
                    filtered_lines = [line if not line.strip().startswith(("!", "%")) else f"# {line}" for line in src.splitlines()]
                    tree = ast.parse("\n".join(filtered_lines), filename=f"{nb_path.name}:{idx}")
                    visitor = DestructiveCallVisitor(f"{nb_path.name}:{idx}")
                    visitor.visit(tree)
                    self.assertEqual(visitor.violations, [], f"AST destructive call violation in {nb_path.name} cell {idx}: {visitor.violations}")

        # In fast production notebook, assert NO rmtree call exists at all
        fast_nb = json.loads((repo_root / "notebooks" / "m1_colab_fast_production.ipynb").read_text(encoding="utf-8"))
        for idx, cell in enumerate(fast_nb.get("cells", [])):
            src = "".join(cell.get("source", []))
            self.assertNotIn("rmtree", src, f"Production notebook should not call rmtree in cell {idx}")

    def test_fast_production_notebook_safety_guards(self):
        """Phase 0 / P1-15: Fast production notebook enforces CPU-only and fail-closed CODE_COMMIT."""
        repo_root = Path(__file__).resolve().parents[1]
        nb_path = repo_root / "notebooks" / "m1_colab_fast_production.ipynb"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        all_src = "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))

        # CPU-only guard
        self.assertIn("COLAB_GPU", all_src)
        self.assertIn("Chuyển accelerator về None/CPU", all_src)

        # Fail-closed commit check in source text
        self.assertIn("not re.fullmatch(r\"[0-9a-f]{40}\", CODE_COMMIT)", all_src)
        self.assertIn("not isinstance(CODE_COMMIT, str)", all_src)
        self.assertNotIn("sử dụng HEAD hiện tại của nhánh", all_src)

    def test_fast_production_notebook_fail_closed_execution(self):
        """Phase 0 / P1-15: Directly execute the actual notebook Step 1 cell in a simulated Colab environment."""
        repo_root = Path(__file__).resolve().parents[1]
        nb_path = repo_root / "notebooks" / "m1_colab_fast_production.ipynb"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))

        step1_src = None
        for cell in nb.get("cells", []):
            src = "".join(cell.get("source", []))
            if "CODE_COMMIT =" in src and "checkout" in src:
                step1_src = src
                break
        self.assertIsNotNone(step1_src, "Step 1 checkout code cell must exist in m1_colab_fast_production.ipynb")

        def run_actual_cell(code_commit, checkout_exc=None, mock_head=None):
            pattern = re.compile(r'CODE_COMMIT\s*=\s*[\'"][^\'"]*[\'"]')
            modified_src = pattern.sub(f'CODE_COMMIT = {repr(code_commit)}', step1_src, count=1)

            mock_drive = unittest.mock.MagicMock()
            mock_colab = unittest.mock.MagicMock()
            mock_colab.drive = mock_drive

            def mock_run_call(cmd, **kwargs):
                if isinstance(cmd, list) and "checkout" in cmd and checkout_exc:
                    raise checkout_exc
                return unittest.mock.MagicMock(returncode=0)

            with patch.dict("sys.modules", {"google.colab": mock_colab}), \
                 patch.dict("os.environ", {"COLAB_GPU": "0"}), \
                 patch("os.chdir"), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("subprocess.run", side_effect=mock_run_call), \
                 patch("subprocess.check_output", return_value=(mock_head if mock_head is not None else str(code_commit)) + "\n"):
                exec(modified_src, {"__name__": "__main__"})

        valid_sha = "a" * 40

        # Sub-case A: Malformed SHA raises RuntimeError
        for bad_sha in ["", "not_a_sha", "12345", "a" * 39, "a" * 41, "g" * 40, None, 12345]:
            with self.assertRaises(RuntimeError, msg=f"Should raise RuntimeError for malformed SHA: {bad_sha}"):
                run_actual_cell(bad_sha, mock_head=valid_sha)

        # Sub-case B: Checkout failure propagates error
        with self.assertRaises(subprocess.CalledProcessError):
            run_actual_cell(valid_sha, checkout_exc=subprocess.CalledProcessError(1, "git checkout"))

        # Sub-case C: Post-checkout HEAD mismatch raises RuntimeError
        different_head = "b" * 40
        with self.assertRaises(RuntimeError, msg="Should raise RuntimeError when post-checkout HEAD does not match"):
            run_actual_cell(valid_sha, mock_head=different_head)

        # Sub-case D: Matching valid SHA succeeds
        run_actual_cell(valid_sha, mock_head=valid_sha)

    def test_validate_and_checkout_commit_production_module(self):
        """Phase 0 / P1-15: Test reusable production function src.fast_production.validate_and_checkout_commit."""
        valid_sha = "c" * 40

        # Sub-case A: Malformed SHA raises RuntimeError
        for bad_sha in ["", "invalid_sha", "12345", "c" * 39, "c" * 41, "h" * 40, None, 9999]:
            with self.assertRaises(RuntimeError):
                validate_and_checkout_commit(bad_sha)

        # Sub-case B: Checkout failure propagates error
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git checkout")):
            with self.assertRaises(subprocess.CalledProcessError):
                validate_and_checkout_commit(valid_sha)

        # Sub-case C: HEAD mismatch raises RuntimeError
        with patch("subprocess.run"), patch("subprocess.check_output", return_value="d" * 40 + "\n"):
            with self.assertRaises(RuntimeError):
                validate_and_checkout_commit(valid_sha)

        # Sub-case D: Valid checkout succeeds and returns HEAD
        with patch("subprocess.run") as mock_run, patch("subprocess.check_output", return_value=valid_sha + "\n"):
            res = validate_and_checkout_commit(valid_sha, repo_dir="/fake/repo")
            self.assertEqual(res, valid_sha)
            mock_run.assert_any_call(["git", "checkout", "--detach", valid_sha], cwd="/fake/repo", check=True)


if __name__ == "__main__":
    unittest.main()

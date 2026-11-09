"""Tests for high-throughput fast production runner."""
import ast
import hashlib
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
    member_cohort_range,
    verify_cohort_handoff,
)

FORBIDDEN_CREDENTIAL_PREFIXES = [
    "K" + "GAT_",
    "g" + "hp_",
    "s" + "2k-",
    "g" + "ho_",
    "gl" + "pat-",
]

CREDENTIAL_ASSIGNMENT_PATTERNS = [
    (
        "direct",
        re.compile(
            r"""\b(KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)\s*=\s*['"]([a-zA-Z0-9_\-]{16,})['"]"""
        ),
    ),
    (
        "environ_index",
        re.compile(
            r"""\b(?:os\.)?environ\s*\[\s*['"](KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)['"]\s*\]\s*=\s*['"]([a-zA-Z0-9_\-]{16,})['"]"""
        ),
    ),
    (
        "environ_setdefault",
        re.compile(
            r"""\b(?:os\.)?environ\.setdefault\s*\(\s*['"](KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)['"]\s*,\s*['"]([a-zA-Z0-9_\-]{16,})['"]\s*\)"""
        ),
    ),
    (
        "dict_literal",
        re.compile(
            r"""['"](KAGGLE_API_TOKEN|KAGGLE_KEY|S2_API_KEY|SEMANTIC_SCHOLAR_API_KEY|GITHUB_TOKEN|GH_TOKEN)['"]\s*:\s*['"]([a-zA-Z0-9_\-]{16,})['"]"""
        ),
    ),
]

ALLOWED_TEST_FIXTURE_ASSIGNMENTS = {
    # Narrowly scoped allowlist: (relative_file_path, credential_key, exact_value)
    ("tests/test_s2_integration.py", "SEMANTIC_SCHOLAR_API_KEY", "offline-response-secret-key"),
}


def scan_credential_text(text: str, rel_path: str = "") -> list[tuple[str, str, str]]:
    """Shared credential scanner: detects forbidden prefixes and assignment patterns.

    Returns a list of violations as (category, key_or_prefix, matched_value).
    """
    violations = []
    for prefix in FORBIDDEN_CREDENTIAL_PREFIXES:
        if prefix in text:
            violations.append(("prefix", prefix, prefix))
    for ptype, pat in CREDENTIAL_ASSIGNMENT_PATTERNS:
        for match in pat.finditer(text):
            key = match.group(1)
            val = match.group(2)
            if (rel_path, key, val) in ALLOWED_TEST_FIXTURE_ASSIGNMENTS:
                continue
            violations.append((ptype, key, val))
    return violations


DRIVE_DANGER_KEYWORDS = {"drive", "mydrive", "group10", "production"}


class DestructiveCallVisitor(ast.NodeVisitor):
    """Shared AST visitor detecting direct calls and scope-aware local-alias destructive operations.

    Features:
    - Lexical scope tracking: pushes and pops scope frames for FunctionDef, AsyncFunctionDef, and ClassDef.
    - Variable reassignment tracking: updates variable taint status sequentially within the current scope
      (tainted if RHS contains a drive keyword, cleared/untainted if RHS is reassigned to a safe path).
    - Scope lookup: checks innermost to outermost active scope, respecting local shadowing.
    - Direct calls: detects rmtree, remove, unlink, rmdir with drive keyword in argument strings.
    - Simple aliases: detects destructive calls on variables whose RHS contained drive paths.
    - Method-on-alias: detects target.unlink(), target.rmdir() on tainted variables.

    Documented Limitations:
    - Chained aliases (target2 = target) are NOT tracked (no symbolic dataflow engine).
    - Inter-procedural dataflow (parameters passed across function calls, return values) is NOT tracked.
    - Dynamic runtime path resolution, getattr/eval, and external processes are NOT tracked.
    """

    def __init__(self, filename: str = "<test>"):
        self.filename = filename
        self.violations: list[tuple[int, str, str]] = []
        # Stack of scopes; each scope maps variable name -> is_tainted bool
        self.scope_stack: list[dict[str, bool]] = [{}]

    @property
    def tainted_vars(self) -> set[str]:
        """Set of currently tainted variable names across all active scopes."""
        result: set[str] = set()
        for scope in self.scope_stack:
            for var_name, is_tainted in scope.items():
                if is_tainted:
                    result.add(var_name)
                else:
                    result.discard(var_name)
        return result

    def _has_drive_keyword(self, val: str) -> bool:
        val_lower = val.lower()
        return any(k in val_lower for k in DRIVE_DANGER_KEYWORDS)

    def _is_rhs_tainted(self, node_val: ast.AST | None) -> bool:
        if node_val is None:
            return False
        for sub in ast.walk(node_val):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if self._has_drive_keyword(sub.value):
                    return True
        return False

    def _set_var_taint(self, var_name: str, is_tainted: bool) -> None:
        self.scope_stack[-1][var_name] = is_tainted

    def is_tainted(self, var_name: str) -> bool:
        for scope in reversed(self.scope_stack):
            if var_name in scope:
                return scope[var_name]
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope_stack.append({})
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope_stack.append({})
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append({})
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        rhs_tainted = self._is_rhs_tainted(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._set_var_taint(target.id, rhs_tainted)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        self._set_var_taint(elt.id, rhs_tainted)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            rhs_tainted = self._is_rhs_tainted(node.value)
            if isinstance(node.target, ast.Name):
                self._set_var_taint(node.target.id, rhs_tainted)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = ""
        called_on_var = None
        if isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                called_on_var = node.func.value.id
        elif isinstance(node.func, ast.Name):
            func_name = node.func.id

        if func_name in {"rmtree", "remove", "unlink", "rmdir"}:
            for arg in ast.walk(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if self._has_drive_keyword(arg.value):
                        self.violations.append((node.lineno, func_name, arg.value))
            for arg in node.args:
                if isinstance(arg, ast.Name) and self.is_tainted(arg.id):
                    self.violations.append((node.lineno, func_name, f"alias:{arg.id}"))
            for kw in node.keywords:
                if isinstance(kw.value, ast.Name) and self.is_tainted(kw.value.id):
                    self.violations.append((node.lineno, func_name, f"alias:{kw.value.id}"))
            if called_on_var and self.is_tainted(called_on_var):
                self.violations.append((node.lineno, func_name, f"method_on:{called_on_var}"))

        self.generic_visit(node)


class TightenedSimulatedGitRunner:
    """Stateful Git runner that verifies exact commands and rejects unexpected Git operations."""

    def __init__(self, initial_head: str = "0" * 40, checkout_error=None, mismatch_head: str | None = None):
        self.current_head = initial_head
        self.checkout_error = checkout_error
        self.mismatch_head = mismatch_head
        self.run_calls = []
        self.check_output_calls = []

    def run(self, cmd, check=False, **kwargs):
        self.run_calls.append({"cmd": list(cmd), "check": check})

        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "git":
            if cmd == ["git", "fetch", "origin"]:
                if not check:
                    raise AssertionError("git fetch origin must be invoked with check=True")
                return unittest.mock.MagicMock(returncode=0)

            if len(cmd) == 4 and cmd[:3] == ["git", "checkout", "--detach"]:
                if not check:
                    raise AssertionError("git checkout --detach must be invoked with check=True")
                if self.checkout_error:
                    raise self.checkout_error
                self.current_head = self.mismatch_head if self.mismatch_head is not None else cmd[3]
                return unittest.mock.MagicMock(returncode=0)

            raise AssertionError(f"Unexpected Git command executed: {cmd}")

        # Allowed non-git command: pip install
        if isinstance(cmd, list) and len(cmd) > 0 and "pip" in cmd:
            return unittest.mock.MagicMock(returncode=0)

        raise AssertionError(f"Unexpected non-Git command executed in cell: {cmd}")

    def check_output(self, cmd, text=False, **kwargs):
        self.check_output_calls.append({"cmd": list(cmd), "text": text})

        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "git":
            if cmd == ["git", "rev-parse", "HEAD"]:
                if not text:
                    raise AssertionError("git rev-parse HEAD must be invoked with text=True")
                return self.current_head + "\n"
            raise AssertionError(f"Unexpected Git check_output command executed: {cmd}")

        raise AssertionError(f"Unexpected check_output command: {cmd}")


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
        manifest_dest = runner.archives_dir / "cohort_0001_corpus.manifest.json"
        self.assertTrue(manifest_dest.exists())
        manifest_data = json.loads(manifest_dest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["cohort_number"], 1)
        self.assertEqual(manifest_data["included_count"], 1)
        self.assertEqual(manifest_data["omitted_count"], 99)
        self.assertEqual(manifest_data["expected_count"], 100)
        self.assertEqual(manifest_data["archive_sha256"], runner._compute_sha256(archive_dest))

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
        for nb_path in (repo_root / "notebooks").glob("*.ipynb"):
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
            for cell in nb.get("cells", []):
                src = "".join(cell.get("source", []))
                hits = scan_credential_text(src, rel_path=nb_path.name)
                self.assertEqual(hits, [], f"Hardcoded credential found in {nb_path.name}: {hits}")

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
        v1_dir = p1 / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        tex_path = v1_dir / "main.tex"
        tex_content = r"\documentclass{article}\begin{document}Hello\end{document}"
        tex_path.write_text(tex_content, encoding="utf-8")
        sha = hashlib.sha256(tex_content.encode("utf-8")).hexdigest()
        (p1 / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": "2508.07872",
            "mssv": "24120010",
            "run_id": "run_1",
            "status": "complete",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "complete",
            "references_status": "complete",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {
                "v1": {
                    "versioned_id": "2508.07872v1",
                    "status": "success",
                    "source_status": "success",
                    "http_status": 200,
                    "content_type": "application/x-eprint",
                    "source_url": "https://export.arxiv.org/src/2508.07872v1",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": ["main.tex"],
                    "file_checksums": {"main.tex": sha},
                    "kept_bytes": len(tex_content.encode("utf-8")),
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "run_1",
                    "completed_at": "2026-11-09T00:00:00Z",
                    "error": None,
                }
            },
        }), encoding="utf-8")

        # Paper 2: unsafe_archive error (non-terminal)
        p2 = runner.corpus_dir / "2508-07873"
        p2.mkdir(parents=True, exist_ok=True)
        (p2 / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": "2508.07873",
            "mssv": "24120010",
            "run_id": "run_2",
            "status": "failed",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "not_attempted",
            "references_status": "not_attempted",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {
                "v1": {
                    "versioned_id": "2508.07873v1",
                    "status": "unsafe_archive",
                    "source_status": "unsafe_archive",
                    "http_status": 200,
                    "content_type": "application/x-eprint",
                    "source_url": "https://export.arxiv.org/src/2508.07873v1",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": [],
                    "file_checksums": {},
                    "kept_bytes": None,
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "run_2",
                    "completed_at": None,
                    "error": {"stage": "source", "type": "unsafe_archive", "message": "Unsafe archive detected"},
                }
            },
        }), encoding="utf-8")

        # Paper 3: not_found (terminal negative)
        p3 = runner.corpus_dir / "2508-07874"
        p3.mkdir(parents=True, exist_ok=True)
        (p3 / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": "2508.07874",
            "mssv": "24120010",
            "run_id": "run_3",
            "status": "not_found",
            "discovery_status": "not_found",
            "version_status": "not_applicable",
            "metadata_status": "not_found",
            "references_status": "not_attempted",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {},
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

    def test_enrich_cohort_s2_isolated_from_arxiv(self):
        """F-06: enrich_cohort_s2 enriches source-complete papers directly without arXiv discovery or downloads."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        # Create a source-complete paper (2508-07872 in cohort 1)
        paper_dir = runner.corpus_dir / "2508-07872"
        v1_dir = paper_dir / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        tex_content = r"\documentclass{article}\begin{document}Hello\end{document}"
        (v1_dir / "main.tex").write_text(tex_content, encoding="utf-8")
        sha = hashlib.sha256(tex_content.encode("utf-8")).hexdigest()
        (paper_dir / "manifest.json").write_text(json.dumps({
            "contract_version": 2,
            "arxiv_id": "2508.07872",
            "mssv": "24120010",
            "run_id": "run_1",
            "status": "complete",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "complete",
            "references_status": "complete",
            "updated_at": "2026-11-09T00:00:00Z",
            "versions": {
                "v1": {
                    "versioned_id": "2508.07872v1",
                    "status": "success",
                    "source_status": "success",
                    "http_status": 200,
                    "content_type": "application/x-eprint",
                    "source_url": "https://export.arxiv.org/src/2508.07872v1",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": ["main.tex"],
                    "file_checksums": {"main.tex": sha},
                    "kept_bytes": len(tex_content.encode("utf-8")),
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "run_1",
                    "completed_at": "2026-11-09T00:00:00Z",
                    "error": None,
                }
            },
        }), encoding="utf-8")

        with patch("src.arxiv_client.query_arxiv") as mock_arxiv_discovery, \
             patch("src.arxiv_source.download_source") as mock_arxiv_download, \
             patch("src.pipeline._run_s2") as mock_pipeline_s2:
            res = runner.enrich_cohort_s2(1, max_ids=1)
            # Must invoke pipeline._run_s2 directly
            self.assertEqual(mock_pipeline_s2.call_count, 1)
            # Must NEVER invoke arXiv discovery or source downloading
            mock_arxiv_discovery.assert_not_called()
            mock_arxiv_download.assert_not_called()
            self.assertEqual(res["enriched_papers"], 1)
            self.assertEqual(res["skipped_incomplete"], 0)

    def test_archive_state_tagging(self):
        """F-03: _archive_cohort tags checkpoint_partial, checkpoint_complete, and handoff_verified correctly."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        dest = runner.archives_dir / "cohort_0001_corpus.tar.gz"

        # Case 1: Partial cohort (1 paper out of 100) -> checkpoint_partial
        p1 = runner.corpus_dir / "2508-07872"
        p1.mkdir(parents=True, exist_ok=True)
        (p1 / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")
        runner._archive_cohort(1, dest)
        mf_path = runner.archives_dir / "cohort_0001_corpus.manifest.json"
        payload = json.loads(mf_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["archive_state"], "checkpoint_partial")
        self.assertEqual(payload["included_count"], 1)
        self.assertEqual(payload["expected_count"], 100)

    def test_restore_cohort_rejects_inventory_mismatch(self):
        """F-03: restore_cohort rejects archive whose tar inventory does not match accounting manifest."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        archive_path = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        manifest_file = archive_path.with_name("cohort_0001_corpus.manifest.json")

        import io
        import tarfile
        with tarfile.open(archive_path, "w:gz") as tar:
            # Tar contains paper 2508-07872
            t1 = tarfile.TarInfo("2508-07872/manifest.json")
            data1 = b'{"status": "complete", "arxiv_id": "2508.07872"}'
            t1.size = len(data1)
            tar.addfile(t1, io.BytesIO(data1))
            # Tar ALSO contains paper 2508-07873
            t2 = tarfile.TarInfo("2508-07873/manifest.json")
            data2 = b'{"status": "complete", "arxiv_id": "2508.07873"}'
            t2.size = len(data2)
            tar.addfile(t2, io.BytesIO(data2))

        actual_sha = runner._compute_sha256(archive_path)
        sha_file.write_text(f"{actual_sha}  {archive_path.name}\n", encoding="utf-8")

        # But manifest ONLY lists 2508.07872 (mismatch!)
        manifest_file.write_text(json.dumps({
            "cohort_number": 1,
            "archive_filename": archive_path.name,
            "archive_sha256": actual_sha,
            "included_count": 1,
            "included_ids": ["2508.07872"],
            "archive_state": "checkpoint_partial",
        }), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            runner.restore_cohort(1)
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_member_cohort_range(self):
        """F-02: member_cohort_range accurately assigns exactly 50 cohorts per member for all 200 cohorts."""
        self.assertEqual(member_cohort_range("24120010"), list(range(1, 51)))
        self.assertEqual(member_cohort_range("24120035"), list(range(51, 101)))
        self.assertEqual(member_cohort_range("24120232"), list(range(101, 151)))
        self.assertEqual(member_cohort_range("24120260"), list(range(151, 201)))

        all_cohorts = (
            member_cohort_range("24120010")
            + member_cohort_range("24120035")
            + member_cohort_range("24120232")
            + member_cohort_range("24120260")
        )
        self.assertEqual(len(all_cohorts), 200)
        self.assertEqual(all_cohorts, list(range(1, 201)))

        with self.assertRaises(ValueError):
            member_cohort_range("99999999")

    def test_verify_cohort_handoff(self):
        """F-03: verify_cohort_handoff enforces all requirements for final M1 handoff."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        archive_path = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        manifest_file = archive_path.with_name("cohort_0001_corpus.manifest.json")

        # Missing archive raises ValueError
        with self.assertRaises(ValueError):
            verify_cohort_handoff(runner, 1)

        # Create archive
        import io
        import tarfile
        with tarfile.open(archive_path, "w:gz") as tar:
            t1 = tarfile.TarInfo("dummy.txt")
            data = b"test"
            t1.size = len(data)
            tar.addfile(t1, io.BytesIO(data))
        actual_sha = runner._compute_sha256(archive_path)

        # Missing sha raises ValueError
        with self.assertRaises(ValueError):
            verify_cohort_handoff(runner, 1)

        sha_file.write_text(f"{actual_sha}  {archive_path.name}\n", encoding="utf-8")

        # Missing manifest raises ValueError
        with self.assertRaises(ValueError):
            verify_cohort_handoff(runner, 1)

        # Non-handoff_verified state raises ValueError
        manifest_file.write_text(json.dumps({
            "cohort_number": 1,
            "archive_filename": archive_path.name,
            "archive_sha256": actual_sha,
            "included_count": 100,
            "included_ids": ["2508.07872"],
            "archive_state": "checkpoint_partial",
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_cohort_handoff(runner, 1)

        # Proper handoff_verified passes
        manifest_file.write_text(json.dumps({
            "cohort_number": 1,
            "archive_filename": archive_path.name,
            "archive_sha256": actual_sha,
            "included_count": 100,
            "included_ids": ["2508.07872"],
            "archive_state": "handoff_verified",
        }), encoding="utf-8")
        res = verify_cohort_handoff(runner, 1)
        self.assertTrue(res["verified"])


    def test_notebooks_and_source_do_not_contain_hardcoded_credentials(self):
        """Phase 0 / Step 0.3: No hardcoded credentials in notebooks or source/test files (including tests/test_fast_production.py)."""
        repo_root = Path(__file__).resolve().parents[1]

        # 1. Check all notebooks: source, outputs, and metadata
        for nb_path in (repo_root / "notebooks").glob("*.ipynb"):
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
            rel_path = str(nb_path.relative_to(repo_root))

            nb_meta_str = json.dumps(nb.get("metadata", {}))
            hits = scan_credential_text(nb_meta_str, rel_path=rel_path)
            self.assertEqual(hits, [], f"Credential found in notebook metadata of {rel_path}: {hits}")

            for idx, cell in enumerate(nb.get("cells", [])):
                cell_meta_str = json.dumps(cell.get("metadata", {}))
                hits = scan_credential_text(cell_meta_str, rel_path=rel_path)
                self.assertEqual(hits, [], f"Credential in {rel_path} cell {idx} metadata: {hits}")

                src = "".join(cell.get("source", []))
                hits = scan_credential_text(src, rel_path=rel_path)
                self.assertEqual(hits, [], f"Credential in {rel_path} cell {idx} source: {hits}")

                for out_idx, out in enumerate(cell.get("outputs", [])):
                    out_str = json.dumps(out)
                    hits = scan_credential_text(out_str, rel_path=rel_path)
                    self.assertEqual(hits, [], f"Credential in {rel_path} cell {idx} output {out_idx}: {hits}")

        # 2. Check all python files in src/, scripts/, and tests/ (including this test file)
        for folder in ["src", "scripts", "tests"]:
            for py_path in (repo_root / folder).rglob("*.py"):
                content = py_path.read_text(encoding="utf-8")
                rel_path = str(py_path.relative_to(repo_root))
                hits = scan_credential_text(content, rel_path=rel_path)
                self.assertEqual(hits, [], f"Hardcoded credential found in {rel_path}: {hits}")

    def test_credential_scanner_regression(self):
        """Phase 0 / Regression: Verify shared scan_credential_text detects all forms and never exempts real-looking keys."""
        k_key = "KAGGLE" + "_API_TOKEN"
        s_key = "S2" + "_API_KEY"
        gh_key = "GITHUB" + "_TOKEN"
        ss_key = "SEMANTIC_SCHOLAR" + "_API_KEY"
        val_16 = "abcdef1234567890"

        # 1. Direct assignment
        hits = scan_credential_text(f'{k_key} = "{val_16}"', rel_path="mock.py")
        self.assertEqual(hits, [("direct", k_key, val_16)])

        # 2. os.environ[...] index assignment
        hits = scan_credential_text(f'os.environ["{s_key}"] = "{val_16}"', rel_path="mock.py")
        self.assertEqual(hits, [("environ_index", s_key, val_16)])

        # 3. os.environ.setdefault(...) call
        hits = scan_credential_text(f'os.environ.setdefault("{gh_key}", "{val_16}")', rel_path="mock.py")
        self.assertEqual(hits, [("environ_setdefault", gh_key, val_16)])

        # 4. Dict literal
        hits = scan_credential_text(f'cfg = {{"{ss_key}": "{val_16}"}}', rel_path="mock.py")
        self.assertEqual(hits, [("dict_literal", ss_key, val_16)])

        # 5. Invalid syntax os.environ.setdefault(...) = ... is NOT matched as environ_setdefault
        hits = scan_credential_text(f'os.environ.setdefault("{k_key}") = "{val_16}"', rel_path="mock.py")
        self.assertNotIn("environ_setdefault", [h[0] for h in hits])

        # 6. Real-looking credential containing words 'test', 'mock', 'sample' is NOT exempted
        bad_token = "K" + "GAT_test1234567890mock_sample"
        hits = scan_credential_text(f'{k_key} = "{bad_token}"', rel_path="mock.py")
        hit_types = [h[0] for h in hits]
        self.assertIn("prefix", hit_types)
        self.assertIn("direct", hit_types)

        # 7. Token prefix detection
        prefix_sample = "s" + "2k-abcdef1234567890"
        hits = scan_credential_text(f'key = "{prefix_sample}"', rel_path="mock.py")
        self.assertEqual(hits, [("prefix", "s" + "2k-", "s" + "2k-")])

        # 8. Allowed fixture in tests/test_s2_integration.py is permitted
        allowed_hits = scan_credential_text(
            f'patch.dict(os.environ, {{"{ss_key}": "offline-response-secret-key"}})',
            rel_path="tests/test_s2_integration.py",
        )
        self.assertEqual(allowed_hits, [])

    def test_notebooks_do_not_contain_destructive_drive_cleanup(self):
        """Phase 0 / P0-01: No notebook may contain destructive deletion of drive/production data.
        
        Detection Heuristics:
        1. Regex scanning: detects rmtree, remove, unlink, and rm -rf targeting drive/production paths.
        2. Shared AST inspection (DestructiveCallVisitor): parses code cells into AST and tracks direct strings
           as well as local variable aliases targeting drive/production paths.
        3. Strict policy: fast production notebook contains 0 calls to rmtree.
        
        Limitation Notice:
        Static analysis (Regex and AST) verifies direct calls and simple local variable aliases.
        It cannot guarantee detection against inter-procedural aliases, runtime dynamic path resolution,
        obfuscated execution (e.g. getattr, eval), or external binaries. Operational safeguards
        (Colab single-writer rule, isolated local scratch) remain required defense-in-depth measures.
        """
        repo_root = Path(__file__).resolve().parents[1]
        drive_danger_patterns = [
            re.compile(r"""(?:shutil\.)?rmtree\s*\(\s*[^)]*(?:drive|Drive|DRIVE|MyDrive|Group10|production)[^)]*\)"""),
            re.compile(r"""(?:os\.)?(?:remove|unlink)\s*\(\s*[^)]*(?:drive|Drive|DRIVE|MyDrive|Group10|production)[^)]*\)"""),
            re.compile(r"""Path\s*\(\s*['"][^'"]*(?:drive|Drive|MyDrive|Group10|production)[^'"]*['"]\s*\)\s*\.unlink"""),
            re.compile(r"""(?:!|%|subprocess\.run\s*\(\s*\[\s*['"])rm\s+-[rR]f\s+.*(?:drive|Drive|MyDrive|Group10|production)"""),
        ]

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

                # 2. Shared AST check (for code cells)
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

    def test_destructive_call_visitor_cases(self):
        """Phase 0 / Unit Test: Verify shared DestructiveCallVisitor detects direct keywords,
        scope-aware aliases, variable reassignment, and documents chained alias limitation."""
        # 1. Direct string paths targeting each supported keyword
        for kw in ["drive", "MyDrive", "Group10", "production"]:
            v = DestructiveCallVisitor()
            v.visit(ast.parse(f"shutil.rmtree('/content/{kw}/data')"))
            self.assertEqual(len(v.violations), 1, f"Should detect direct call for keyword '{kw}'")

        # 2. Hai function co cung ten bien nhung gia tri duong dan khac nhau (scope isolation)
        two_funcs_code = """
import shutil
from pathlib import Path

def clean_drive_archive():
    target = Path("/content/drive/MyDrive/production")
    shutil.rmtree(target)

def clean_local_temp():
    target = Path("/tmp/scratch_dir")
    shutil.rmtree(target)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(two_funcs_code))
        self.assertEqual(len(v.violations), 1, "Only clean_drive_archive should trigger a violation")
        self.assertEqual(v.violations[0][1], "rmtree")
        self.assertEqual(v.violations[0][2], "alias:target")
        self.assertEqual(v.violations[0][0], 7)

        # 3. Bien nguy hiem duoc gan lai thanh duong dan an toan trong cung scope
        reassigned_safe_code = """
import shutil
from pathlib import Path

def cleanup_workspace():
    target = Path("/content/drive/MyDrive/production")
    target = Path("/tmp/safe_scratch")
    shutil.rmtree(target)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(reassigned_safe_code))
        self.assertEqual(
            v.violations,
            [],
            "Reassigning target to safe path before destructive call must clear taint and trigger 0 violations",
        )

        # 4. Su dung bien nguy hiem truoc khi gan lai (kiem tra thu tu thuc thi tuan tu trong scope)
        reassigned_sequential_code = """
import shutil
from pathlib import Path

def cleanup_stepwise():
    target = Path("/content/drive/MyDrive/production")
    shutil.rmtree(target)
    target = Path("/tmp/safe_scratch")
    shutil.rmtree(target)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(reassigned_sequential_code))
        self.assertEqual(len(v.violations), 1, "Only the first call on tainted target should trigger a violation")
        self.assertEqual(v.violations[0][0], 7)
        self.assertEqual(v.violations[0][2], "alias:target")

        # 5. Bien duong dan nguy hiem duoc su dung trong cung function
        func_dangerous_code = """
import shutil
from pathlib import Path

def purge_drive():
    target = Path("/content/drive/MyDrive/production")
    shutil.rmtree(target)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(func_dangerous_code))
        self.assertEqual(len(v.violations), 1)
        self.assertEqual(v.violations[0][1], "rmtree")
        self.assertEqual(v.violations[0][2], "alias:target")

        # 6. Simple alias o module scope: target = Path(...); shutil.rmtree(target)
        alias_code = """
from pathlib import Path
import shutil
target = Path("/content/drive/MyDrive/production")
shutil.rmtree(target)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(alias_code))
        self.assertEqual(len(v.violations), 1)
        self.assertEqual(v.violations[0][1], "rmtree")
        self.assertEqual(v.violations[0][2], "alias:target")

        # 7. Method call on alias: target = Path(...); target.unlink()
        method_alias_code = """
from pathlib import Path
target = Path("/content/drive/MyDrive/production/report.pdf")
target.unlink()
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(method_alias_code))
        self.assertEqual(len(v.violations), 1)
        self.assertEqual(v.violations[0][1], "unlink")
        self.assertEqual(v.violations[0][2], "method_on:target")

        # 8. Clean paths: no false positives
        clean_code = """
import os
import shutil
shutil.rmtree('/tmp/local_scratch_space')
os.remove('/tmp/local_temp_file.txt')
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(clean_code))
        self.assertEqual(v.violations, [], "Should have 0 violations on clean local scratch paths")

        # 9. Unsupported chained alias case: target2 = target; shutil.rmtree(target2)
        chained_code = """
from pathlib import Path
import shutil
target = Path("/content/drive/MyDrive/production")
target2 = target
shutil.rmtree(target2)
"""
        v = DestructiveCallVisitor()
        v.visit(ast.parse(chained_code))
        # Prove that chained variable alias is unsupported and not detected by scope visitor without dataflow engine
        self.assertEqual(
            v.violations,
            [],
            "Chained alias propagation (target2 = target) is an explicitly documented limitation and not detected.",
        )

    def test_fast_production_notebook_safety_guards(self):
        """Phase 0 / P1-15: Fast production notebook enforces CPU-only and fail-closed CODE_COMMIT & EXECUTION_COMMIT."""
        repo_root = Path(__file__).resolve().parents[1]
        nb_path = repo_root / "notebooks" / "m1_colab_fast_production.ipynb"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        all_src = "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))

        # CPU-only guard
        self.assertIn("COLAB_GPU", all_src)
        self.assertIn("Chuyển accelerator về None/CPU", all_src)

        # Fail-closed commit checks in source text
        self.assertIn("not re.fullmatch(r\"[0-9a-f]{40}\", CODE_COMMIT)", all_src)
        self.assertIn("not isinstance(CODE_COMMIT, str)", all_src)
        self.assertIn("not re.fullmatch(r\"[0-9a-f]{40}\", EXECUTION_COMMIT)", all_src)
        self.assertIn("not isinstance(EXECUTION_COMMIT, str)", all_src)
        self.assertNotIn("sử dụng HEAD hiện tại của nhánh", all_src)

    def test_fast_production_notebook_fail_closed_execution(self):
        """Phase 0 / P1-15: Directly execute actual notebook Step 1 cell with tightened stateful Git verification."""
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

        def run_actual_cell(exec_commit, code_commit="PRESERVE", checkout_error=None, mismatch_head=None):
            pattern_exec = re.compile(r'EXECUTION_COMMIT\s*=\s*[\'"][^\'"]*[\'"]')
            modified_src = pattern_exec.sub(f'EXECUTION_COMMIT = {repr(exec_commit)}', step1_src, count=1)
            if code_commit != "PRESERVE":
                pattern_code = re.compile(r'CODE_COMMIT\s*=\s*[\'"][^\'"]*[\'"]')
                modified_src = pattern_code.sub(f'CODE_COMMIT = {repr(code_commit)}', modified_src, count=1)

            git_runner = TightenedSimulatedGitRunner(checkout_error=checkout_error, mismatch_head=mismatch_head)
            mock_drive = unittest.mock.MagicMock()
            mock_colab = unittest.mock.MagicMock()
            mock_colab.drive = mock_drive

            with patch.dict("sys.modules", {"google.colab": mock_colab}), \
                 patch.dict("os.environ", {"COLAB_GPU": "0"}), \
                 patch("os.chdir"), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("subprocess.run", side_effect=git_runner.run), \
                 patch("subprocess.check_output", side_effect=git_runner.check_output):
                exec(modified_src, {"__name__": "__main__"})
            return git_runner

        valid_sha = "a" * 40

        # Sub-case A: Malformed SHA raises RuntimeError before executing ANY git command
        for bad_sha in ["", "not_a_sha", "12345", "a" * 39, "a" * 41, "g" * 40, None, 12345]:
            with self.assertRaises(RuntimeError, msg=f"Should raise RuntimeError for malformed EXECUTION_COMMIT: {bad_sha}"):
                run_actual_cell(bad_sha)
            with self.assertRaises(RuntimeError, msg=f"Should raise RuntimeError for malformed CODE_COMMIT: {bad_sha}"):
                run_actual_cell(valid_sha, code_commit=bad_sha)

        # Sub-case B: Checkout failure propagates error and verifies exact sequence up to failure
        spied_runner = TightenedSimulatedGitRunner(checkout_error=subprocess.CalledProcessError(1, "git checkout"))
        with patch.dict("sys.modules", {"google.colab": unittest.mock.MagicMock()}), \
             patch.dict("os.environ", {"COLAB_GPU": "0"}), \
             patch("os.chdir"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", side_effect=spied_runner.run), \
             patch("subprocess.check_output", side_effect=spied_runner.check_output):
            pattern_exec = re.compile(r'EXECUTION_COMMIT\s*=\s*[\'"][^\'"]*[\'"]')
            modified_src = pattern_exec.sub(f'EXECUTION_COMMIT = {repr(valid_sha)}', step1_src, count=1)
            with self.assertRaises(subprocess.CalledProcessError):
                exec(modified_src, {"__name__": "__main__"})
        self.assertEqual(len(spied_runner.run_calls), 2)
        self.assertEqual(spied_runner.run_calls[0], {"cmd": ["git", "fetch", "origin"], "check": True})
        self.assertEqual(spied_runner.run_calls[1], {"cmd": ["git", "checkout", "--detach", valid_sha], "check": True})
        self.assertEqual(spied_runner.check_output_calls, [], "rev-parse HEAD must NOT be invoked when checkout fails")

        # Sub-case C: Post-checkout HEAD mismatch raises RuntimeError
        different_head = "b" * 40
        spied_mismatch = TightenedSimulatedGitRunner(mismatch_head=different_head)
        with patch.dict("sys.modules", {"google.colab": unittest.mock.MagicMock()}), \
             patch.dict("os.environ", {"COLAB_GPU": "0"}), \
             patch("os.chdir"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", side_effect=spied_mismatch.run), \
             patch("subprocess.check_output", side_effect=spied_mismatch.check_output):
            pattern_exec = re.compile(r'EXECUTION_COMMIT\s*=\s*[\'"][^\'"]*[\'"]')
            modified_src = pattern_exec.sub(f'EXECUTION_COMMIT = {repr(valid_sha)}', step1_src, count=1)
            with self.assertRaises(RuntimeError, msg="Commit sau khi checkout ... không khớp"):
                exec(modified_src, {"__name__": "__main__"})
        self.assertEqual(spied_mismatch.run_calls[0], {"cmd": ["git", "fetch", "origin"], "check": True})
        self.assertEqual(spied_mismatch.run_calls[1], {"cmd": ["git", "checkout", "--detach", valid_sha], "check": True})
        self.assertEqual(spied_mismatch.check_output_calls, [{"cmd": ["git", "rev-parse", "HEAD"], "text": True}])

        # Sub-case D: Matching valid SHA succeeds with exact command assertions
        git_success = run_actual_cell(valid_sha)
        self.assertEqual(git_success.run_calls[0], {"cmd": ["git", "fetch", "origin"], "check": True})
        self.assertEqual(git_success.run_calls[1], {"cmd": ["git", "checkout", "--detach", valid_sha], "check": True})
        self.assertEqual(git_success.check_output_calls, [{"cmd": ["git", "rev-parse", "HEAD"], "text": True}])
        self.assertEqual(git_success.current_head, valid_sha)

        # Sub-case E: Unexpected Git commands or non-check calls fail explicitly
        runner = TightenedSimulatedGitRunner()
        with self.assertRaises(AssertionError, msg="Unexpected Git command must fail explicitly"):
            runner.run(["git", "pull"], check=True)
        with self.assertRaises(AssertionError, msg="check=False on git fetch must fail explicitly"):
            runner.run(["git", "fetch", "origin"], check=False)
        with self.assertRaises(AssertionError, msg="check=False on git checkout must fail explicitly"):
            runner.run(["git", "checkout", "--detach", valid_sha], check=False)
        with self.assertRaises(AssertionError, msg="text=False on git rev-parse HEAD must fail explicitly"):
            runner.check_output(["git", "rev-parse", "HEAD"], text=False)

    def test_notebook_member_selection_covers_entire_group(self):
        """F-02: Fast production notebook dynamically derives 50 cohorts per member, accounting for all 200 cohorts."""
        repo_root = Path(__file__).resolve().parents[1]
        nb_path = repo_root / "notebooks" / "m1_colab_fast_production.ipynb"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))

        # Extract Step 3 cell source
        step3_src = None
        for cell in nb.get("cells", []):
            src = "".join(cell.get("source", []))
            if "member_cohort_range" in src and "MSSV =" in src:
                step3_src = src
                break
        self.assertIsNotNone(step3_src, "Step 3 cell must exist in fast production notebook")

        # Execute selection logic for each member in isolation
        for mssv, expected_start in [("24120010", 1), ("24120035", 51), ("24120232", 101), ("24120260", 151)]:
            ns = {
                "member_cohort_range": member_cohort_range,
                "MSSV": mssv,
                "MODE": "MEMBER_PRODUCTION",
            }
            # Execute derivation snippet
            exec(
                "assigned = member_cohort_range(MSSV)\n"
                "if MODE == 'PILOT': COHORTS = [assigned[0]]\n"
                "elif MODE == 'MEMBER_PRODUCTION': COHORTS = assigned\n",
                ns,
            )
            self.assertEqual(len(ns["COHORTS"]), 50)
            self.assertEqual(ns["COHORTS"][0], expected_start)
            self.assertEqual(ns["COHORTS"][-1], expected_start + 49)

        # Pilot mode gives exactly 1 cohort
        ns_pilot = {
            "member_cohort_range": member_cohort_range,
            "MSSV": "24120260",
            "MODE": "PILOT",
        }
        exec(
            "assigned = member_cohort_range(MSSV)\n"
            "if MODE == 'PILOT': COHORTS = [assigned[0]]\n"
            "elif MODE == 'MEMBER_PRODUCTION': COHORTS = assigned\n",
            ns_pilot,
        )
        self.assertEqual(ns_pilot["COHORTS"], [151])

    def test_notebook_all_cells_compile_cleanly(self):
        """Phase 0 / P1: All code cells in m1_colab_fast_production.ipynb parse cleanly without syntax errors."""
        repo_root = Path(__file__).resolve().parents[1]
        nb_path = repo_root / "notebooks" / "m1_colab_fast_production.ipynb"
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        for idx, cell in enumerate(nb.get("cells", [])):
            if cell.get("cell_type") == "code":
                src = "".join(cell.get("source", []))
                filtered = [l if not l.strip().startswith(("!", "%")) else f"# {l}" for l in src.splitlines()]
                try:
                    ast.parse("\n".join(filtered), filename=f"notebook_cell_{idx}")
                except SyntaxError as exc:
                    self.fail(f"Cell {idx} failed to parse: {exc}")

    # ---------------------------------------------------------
    # R10: Adversarial count_completed_papers Containment & Safety Tests
    # ---------------------------------------------------------
    def test_count_completed_papers_rejects_symlinked_paper_directory(self):
        """R10: Paper directory that is a symlink or escapes corpus root is rejected."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        with tempfile.TemporaryDirectory() as outside_td:
            outside_paper = Path(outside_td) / "fake_paper"
            outside_paper.mkdir(parents=True, exist_ok=True)
            (outside_paper / "manifest.json").write_text(json.dumps({
                "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
                "status": "complete", "discovery_status": "found", "version_status": "resolved",
                "metadata_status": "complete", "references_status": "complete",
                "versions": {
                    "v1": {
                        "versioned_id": "2508.07872v1", "status": "no_tex_source",
                        "source_status": "no_tex_source", "files": [], "file_checksums": {},
                        "kept_bytes": 0, "completed_at": "2026-11-09T00:00:00Z",
                    }
                },
            }), encoding="utf-8")

            # Symlink paper folder pointing outside corpus
            symlink_paper = runner.corpus_dir / "2508-07872"
            symlink_paper.symlink_to(outside_paper)

            self.assertEqual(runner.count_completed_papers(1), 0)

            # Symlink loop paper folder must fail closed without raising RuntimeError
            symlink_paper.unlink()
            symlink_paper.symlink_to(symlink_paper)
            self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_fails_closed_when_corpus_dir_unresolvable(self):
        """R12: count_completed_papers fails closed (returns 0) when corpus_dir cannot be resolved or is invalid."""
        runner = FastProductionRunner(self.local_root, self.drive_root)

        # 1. corpus_dir is a symlink loop
        import shutil
        shutil.rmtree(runner.corpus_dir)
        try:
            runner.corpus_dir.symlink_to(runner.corpus_dir)
        except OSError:
            self.skipTest("Filesystem does not support symlinks")

        self.assertEqual(runner.count_completed_papers(1), 0)
        runner.corpus_dir.unlink()

        # 2. corpus_dir does not exist
        if runner.corpus_dir.exists() or runner.corpus_dir.is_symlink():
            if runner.corpus_dir.is_dir():
                shutil.rmtree(runner.corpus_dir)
            else:
                runner.corpus_dir.unlink()
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 3. corpus_dir is a regular file instead of a directory
        runner.corpus_dir.write_text("regular file", encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)
        runner.corpus_dir.unlink()

        # 4. corpus_dir.resolve() raises RuntimeError (e.g. symlink loop in parent)
        runner.corpus_dir.mkdir(parents=True, exist_ok=True)
        with unittest.mock.patch.object(Path, "resolve", side_effect=RuntimeError("Corpus root symlink loop")):
            self.assertEqual(runner.count_completed_papers(1), 0)

        # 5. corpus_dir.resolve() raises OSError (e.g. permission or I/O error)
        with unittest.mock.patch.object(Path, "resolve", side_effect=OSError("Corpus root I/O error")):
            self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_rejects_symlinked_manifest(self):
        """R10: manifest.json that is a symlink or hardlink is rejected."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as outside_td:
            outside_manifest = Path(outside_td) / "outside_manifest.json"
            outside_manifest.write_text(json.dumps({
                "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
                "status": "complete", "discovery_status": "found", "version_status": "resolved",
                "metadata_status": "complete", "references_status": "complete",
                "versions": {
                    "v1": {
                        "versioned_id": "2508.07872v1", "status": "no_tex_source",
                        "source_status": "no_tex_source", "files": [], "file_checksums": {},
                        "kept_bytes": 0, "completed_at": "2026-11-09T00:00:00Z",
                    }
                },
            }), encoding="utf-8")

            # Symlink manifest.json
            (paper_folder / "manifest.json").symlink_to(outside_manifest)
            self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_rejects_malformed_manifest_with_terminal_status(self):
        """R10: Manifest with terminal status but malformed schema is rejected."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        # 1. Missing contract_version
        (paper_folder / "manifest.json").write_text(json.dumps({
            "arxiv_id": "2508.07872", "mssv": "24120010", "status": "complete",
            "discovery_status": "found", "version_status": "resolved", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 2. Wrong contract_version
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 999, "arxiv_id": "2508.07872", "mssv": "24120010",
            "status": "complete", "discovery_status": "found", "version_status": "resolved", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 3. Invalid status string
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "status": "invalid_status_xyz", "discovery_status": "found", "version_status": "resolved", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 4. Invalid discovery_status
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "status": "complete", "discovery_status": "unknown_discovery", "version_status": "resolved", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_rejects_wrong_identity_manifest(self):
        """R10: Manifest with mismatched arxiv_id or mssv is rejected."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        # Wrong arxiv_id
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.99999", "mssv": "24120010",
            "status": "complete", "discovery_status": "found", "version_status": "resolved",
            "metadata_status": "complete", "references_status": "complete",
            "versions": {
                "v1": {
                    "versioned_id": "2508.99999v1", "status": "no_tex_source",
                    "source_status": "no_tex_source", "files": [], "file_checksums": {},
                    "kept_bytes": 0, "completed_at": "2026-11-09T00:00:00Z",
                }
            },
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # Wrong mssv
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "99999999",
            "status": "complete", "discovery_status": "found", "version_status": "resolved",
            "metadata_status": "complete", "references_status": "complete",
            "versions": {
                "v1": {
                    "versioned_id": "2508.07872v1", "status": "no_tex_source",
                    "source_status": "no_tex_source", "files": [], "file_checksums": {},
                    "kept_bytes": 0, "completed_at": "2026-11-09T00:00:00Z",
                }
            },
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_negative_discovery_valid_and_invalid(self):
        """R10: Negative discovery (not_found) requires valid terminal status and no version dirs on disk."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        # Valid negative discovery
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "run_id": "test_run", "updated_at": "2026-11-09T00:00:00Z",
            "status": "not_found", "discovery_status": "not_found", "version_status": "not_applicable",
            "metadata_status": "not_found", "references_status": "not_attempted", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 1)

        # Invalid: non-terminal manifest.status with discovery_status='not_found'
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "run_id": "test_run", "updated_at": "2026-11-09T00:00:00Z",
            "status": "in_progress", "discovery_status": "not_found", "version_status": "not_applicable",
            "metadata_status": "not_found", "references_status": "not_attempted", "versions": {},
        }), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # Invalid: unexpected version directory exists on disk for not_found
        (paper_folder / "manifest.json").write_text(json.dumps({
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "run_id": "test_run", "updated_at": "2026-11-09T00:00:00Z",
            "status": "not_found", "discovery_status": "not_found", "version_status": "not_applicable",
            "metadata_status": "not_found", "references_status": "not_attempted", "versions": {},
        }), encoding="utf-8")
        (paper_folder / "v1").mkdir(parents=True, exist_ok=True)
        self.assertEqual(runner.count_completed_papers(1), 0)

    def test_count_completed_papers_rejects_missing_mandatory_manifest_fields(self):
        """R13: Manifest missing any mandatory schema field is rejected by count_completed_papers."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        base_valid = {
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "run_id": "test_run", "status": "not_found", "discovery_status": "not_found",
            "version_status": "not_applicable", "metadata_status": "not_found",
            "references_status": "not_attempted", "versions": {}, "updated_at": "2026-11-09T00:00:00Z",
        }

        # Check each mandatory field being missing
        mandatory_fields = [
            "run_id", "updated_at", "version_status", "metadata_status",
            "references_status", "versions", "discovery_status", "status",
        ]
        for field in mandatory_fields:
            bad_manifest = dict(base_valid)
            del bad_manifest[field]
            (paper_folder / "manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
            self.assertEqual(runner.count_completed_papers(1), 0, f"Must reject manifest missing '{field}'")

    def test_count_completed_papers_not_found_requires_exact_empty_dict(self):
        """R14: not_found branch requires versions to be an exact empty dict (reject list, None, string)."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        base_not_found = {
            "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
            "run_id": "test_run", "status": "not_found", "discovery_status": "not_found",
            "version_status": "not_applicable", "metadata_status": "not_found",
            "references_status": "not_attempted", "updated_at": "2026-11-09T00:00:00Z",
        }

        # 1. versions = [] (list instead of dict) -> must be rejected
        bad_list = dict(base_not_found, versions=[])
        (paper_folder / "manifest.json").write_text(json.dumps(bad_list), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 2. versions = None -> must be rejected
        bad_none = dict(base_not_found, versions=None)
        (paper_folder / "manifest.json").write_text(json.dumps(bad_none), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 3. versions = "" -> must be rejected
        bad_str = dict(base_not_found, versions="")
        (paper_folder / "manifest.json").write_text(json.dumps(bad_str), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 4. versions = {"v1": {...}} (non-empty) -> must be rejected for not_found
        bad_nonempty = dict(base_not_found, versions={"v1": {}})
        (paper_folder / "manifest.json").write_text(json.dumps(bad_nonempty), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 0)

        # 5. versions = {} (valid empty dict) -> must be accepted
        good_dict = dict(base_not_found, versions={})
        (paper_folder / "manifest.json").write_text(json.dumps(good_dict), encoding="utf-8")
        self.assertEqual(runner.count_completed_papers(1), 1)

    def test_count_completed_papers_rejects_path_traversal_version_names(self):
        """R12: count_completed_papers rejects manifests containing invalid version names or path traversal."""
        runner = FastProductionRunner(self.local_root, self.drive_root)
        paper_folder = runner.corpus_dir / "2508-07872"
        paper_folder.mkdir(parents=True, exist_ok=True)

        for bad_name in ["../../missing", "/etc/passwd", "v0", "v01", "../v1"]:
            bad_manifest = {
                "contract_version": 2, "arxiv_id": "2508.07872", "mssv": "24120010",
                "run_id": "test_run", "status": "complete_with_no_tex", "discovery_status": "found",
                "version_status": "resolved", "metadata_status": "complete",
                "references_status": "complete", "updated_at": "2026-11-09T00:00:00Z",
                "versions": {
                    bad_name: {
                        "versioned_id": f"2508.07872{bad_name}", "status": "no_tex_source",
                        "source_status": "no_tex_source", "http_status": 200,
                        "content_type": "application/x-eprint",
                        "source_url": f"https://export.arxiv.org/src/2508.07872{bad_name}",
                        "archive_bytes": None, "archive_sha256": None, "files": [],
                        "file_checksums": {}, "kept_bytes": 0, "extracted_bytes": None,
                        "removed_files": None, "attempts": 1, "run_id": "test_run",
                        "completed_at": "2026-11-09T00:00:00Z", "error": None,
                    }
                },
            }
            (paper_folder / "manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
            self.assertEqual(runner.count_completed_papers(1), 0, f"Must reject bad version name '{bad_name}'")

"""High-throughput M1 production engine with SSD/RAM-disk & Kaggle discovery caching.

Extends the M1 production workflow with:
1. Local NVMe SSD storage (/content/corpus_ultra) to eliminate Google Drive FUSE latency.
2. RAM Disk (/dev/shm) scratch space for zero-I/O-latency TeX archive decompression.
3. Pre-cached Discovery ingestion via Cornell arXiv Kaggle snapshot (src.kaggle_discovery).
4. Bounded arXiv 429 auto-retry enabled by default (60s fallback, 300s budget).
5. Automated archive synchronization (.tar.gz) to Google Drive per cohort.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from src import arxiv_client, batch, benchmark, benchmark_runner, id_ranges
from src.kaggle_discovery import ingest_kaggle_snapshot
from src.storage import CorpusStorage, _atomic_json


def validate_and_checkout_commit(code_commit: str, repo_dir: Optional[str | Path] = None) -> str:
    """Validate CODE_COMMIT 40-char hex format and checkout commit in detached HEAD mode (fail-closed).
    
    1. If code_commit is not a 40-character hexadecimal string, raises RuntimeError.
    2. Runs git fetch origin.
    3. Runs git checkout --detach <code_commit>.
    4. Verifies git rev-parse HEAD matches code_commit; raises RuntimeError if mismatched.
    """
    if not isinstance(code_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", code_commit):
        raise RuntimeError("Bắt buộc phải điền đầy đủ mã commit 40 ký tự cho CODE_COMMIT để bảo đảm tính tái lập (fail-closed).")
    cwd = str(repo_dir) if repo_dir else None
    subprocess.run(["git", "fetch", "origin"], cwd=cwd, check=True)
    subprocess.run(["git", "checkout", "--detach", code_commit], cwd=cwd, check=True)
    current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
    if current_head != code_commit:
        raise RuntimeError(f"Commit sau khi checkout ({current_head}) không khớp với CODE_COMMIT ({code_commit}).")
    return current_head


COHORT_SIZE = 100


def cohort_pairs(number: int) -> List[Tuple[str, str]]:
    """Return 100 candidate (mssv, arxiv_id) pairs for cohort 1..200."""
    if not isinstance(number, int) or not 1 <= number <= 200:
        raise ValueError("Cohort number must be between 1 and 200")
    config = id_ranges.load_config()
    start = (number - 1) * COHORT_SIZE
    pairs = list(id_ranges.iter_assigned_ids(config))
    return pairs[start:start + COHORT_SIZE]


class FastProductionRunner:
    """High-throughput cohort runner with SSD acceleration and automated Drive backup."""

    def __init__(
        self,
        local_root: str | Path,
        drive_root: str | Path,
        *,
        scratch_root: Optional[str | Path] = None,
        retry_429: bool = True,
        retry_wait_budget: float = 300.0,
        timeout: float = 30.0,
    ):
        self.local_root = Path(local_root).resolve()
        self.drive_root = Path(drive_root).resolve()
        self.corpus_dir = self.local_root / "corpus"
        self.logs_dir = self.drive_root / "logs"
        self.reports_dir = self.drive_root / "reports"
        self.archives_dir = self.drive_root / "archives"

        if scratch_root is not None:
            self.scratch_dir = Path(scratch_root).resolve()
        elif Path("/dev/shm").exists() and os.access("/dev/shm", os.W_OK):
            self.scratch_dir = Path("/dev/shm/arxiv_scratch")
        else:
            self.scratch_dir = self.local_root / "scratch"

        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

        self.retry_429 = retry_429
        self.retry_wait_budget = float(retry_wait_budget)
        self.timeout = float(timeout)
        self.store = CorpusStorage(self.corpus_dir)

    def preload_kaggle_discovery(self, snapshot_path: str | Path, cohorts: List[int]) -> dict:
        """Pre-populate discovery metadata from Kaggle snapshot and record benchmark metrics."""
        target_pairs = []
        for c in cohorts:
            target_pairs.extend(cohort_pairs(c))

        exp_id = f"exp-kaggle-discovery-{int(time.time() * 1000)}"
        exp_dir = self.reports_dir / exp_id
        exp_dir.mkdir(parents=True, exist_ok=True)
        resources_path = exp_dir / "resources.json"
        report_path = exp_dir / "report.json"

        discovery_res: dict = {}

        def _run_ingest() -> None:
            nonlocal discovery_res
            discovery_res = ingest_kaggle_snapshot(snapshot_path, self.store, target_pairs)

        try:
            bench_report = benchmark.benchmark_pipeline(
                _run_ingest,
                corpus_output_dir=self.corpus_dir,
                report_path=resources_path,
                verbose=False,
                disk_roots=[self.corpus_dir, self.scratch_dir],
            )
        except KeyboardInterrupt:
            print("\nĐã tạm dừng bước đối chiếu siêu dữ liệu Kaggle theo yêu cầu người dùng.")
            return {
                "target_papers": len(target_pairs),
                "found_papers": 0,
                "found_versions": 0,
                "missing_papers": [],
                "elapsed_seconds": 0.0,
                "peak_ram_mb": 0.0,
                "average_ram_mb": 0.0,
                "report_path": str(report_path),
                "interrupted": True,
            }
        elapsed = float(bench_report.get("elapsed_seconds") or 0.0)
        full_report = {
            "experiment_id": exp_id,
            "batch_id": "kaggle-discovery",
            "cohorts": list(cohorts),
            "started_at": bench_report.get("started_at"),
            "ended_at": bench_report.get("ended_at"),
            "discovery_summary": discovery_res,
            "resource_metrics": bench_report,
            "timing_metrics": {
                "discovery_wall_seconds": elapsed,
                "paper_wall_seconds": 0.0,
            },
        }
        _atomic_json(report_path, full_report)
        return {
            **discovery_res,
            "elapsed_seconds": elapsed,
            "peak_ram_mb": bench_report.get("peak_ram_mb") or 0.0,
            "average_ram_mb": bench_report.get("average_ram_mb") or 0.0,
            "report_path": str(report_path),
        }

    def _reset_recoverable_archive_conflicts(self, number: int) -> int:
        """Reset versions previously marked unsafe_archive due to duplicate non-TeX/non-Bib files."""
        from src.storage import arxiv_id_to_folder_key
        reset_count = 0
        for mssv, arxiv_id in cohort_pairs(number):
            manifest_path = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = self.store.read_manifest(mssv, arxiv_id)
                changed = False
                for ver_name, rec in list(manifest.get("versions", {}).items()):
                    err_msg = (rec.get("error") or {}).get("message") or ""
                    if rec.get("status") == "unsafe_archive" and err_msg.startswith("Conflicting path:"):
                        ver_dir = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / ver_name
                        if not ver_dir.exists():
                            manifest["versions"][ver_name] = self.store._pending(arxiv_id, ver_name)
                            changed = True
                if changed:
                    self.store._commit(manifest)
                    reset_count += 1
            except Exception:
                pass
        if reset_count > 0:
            print(f"Lô {number}: Đã phục hồi {reset_count} phiên bản bị xung đột tệp không-TeX trước đó.")
        return reset_count

    def count_completed_papers(self, number: int) -> int:
        """Count how many papers in the cohort have verified terminal completion."""
        from src.storage import TERMINAL, arxiv_id_to_folder_key
        done = 0
        for _, arxiv_id in cohort_pairs(number):
            manifest_path = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if manifest_path.is_file():
                try:
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    disc = m.get("discovery_status")
                    if disc == "not_found":
                        done += 1
                        continue
                    versions = m.get("versions") or {}
                    if disc == "found" and versions and m.get("version_status") == "resolved":
                        if all(v.get("status") in TERMINAL for v in versions.values()):
                            done += 1
                except Exception:
                    pass
        return done

    def _resolve_batch_id(
        self,
        number: int,
        max_attempts: int,
        revision_history: bool,
        timeout: Optional[float] = None,
        s2_config: Optional[dict] = None,
    ) -> Tuple[str, bool]:
        """Resolve a non-destructive batch_id and determine if it can safely resume."""
        effective_timeout = timeout if timeout is not None else self.timeout
        base_id = f"fast-arxiv-{number:04d}" if s2_config is None else f"fast-arxiv-{number:04d}-s2"
        base_dir = self.logs_dir / base_id
        batch_json_path = base_dir / "batch.json"
        if not batch_json_path.is_file():
            return base_id, False
        try:
            saved_def = json.loads(batch_json_path.read_text(encoding="utf-8")).get("definition", {})
            if (
                saved_def.get("max_attempts") == max_attempts
                and saved_def.get("revision_history", False) == bool(revision_history)
                and saved_def.get("timeout") == effective_timeout
                and saved_def.get("s2_config") == s2_config
            ):
                return base_id, True
        except Exception:
            pass
        s2_flag = 1 if s2_config is not None else 0
        variant_id = f"fast-arxiv-{number:04d}-a{max_attempts}-h{int(bool(revision_history))}-s{s2_flag}"
        variant_dir = self.logs_dir / variant_id
        variant_json = variant_dir / "batch.json"
        if variant_json.is_file():
            try:
                saved_def = json.loads(variant_json.read_text(encoding="utf-8")).get("definition", {})
                if (
                    saved_def.get("max_attempts") == max_attempts
                    and saved_def.get("revision_history", False) == bool(revision_history)
                    and saved_def.get("timeout") == effective_timeout
                    and saved_def.get("s2_config") == s2_config
                ):
                    return variant_id, True
            except Exception:
                pass
        return variant_id, False

    def run_cohort(
        self,
        number: int,
        *,
        resume: bool = True,
        revision_history: bool = True,
        s2_config: Optional[dict] = None,
        max_ids: Optional[int] = None,
    ) -> dict:
        """Execute one cohort (100 papers) with SSD acceleration, backoff retries, Drive sync, and revision history."""
        self._reset_recoverable_archive_conflicts(number)
        pairs = cohort_pairs(number)
        target_limit = min(max_ids, len(pairs)) if max_ids is not None else len(pairs)
        max_attempts = 6 if (self.retry_429 and self.retry_wait_budget > 0) else 1
        batch_id, can_resume = self._resolve_batch_id(
            number, max_attempts, revision_history, timeout=self.timeout, s2_config=s2_config
        )
        effective_resume = can_resume if resume else False
        experiment_id = f"exp-fast-{number:04d}-{int(time.time())}"

        already_done = self.count_completed_papers(number)
        if already_done > 0:
            print(f"Lô {number}: Đã có {already_done}/{len(pairs)} bài báo hoàn thành từ phiên trước, tiếp tục thu thập các bài còn lại.")

        started = time.time()
        archive_name = f"cohort_{number:04d}_corpus.tar.gz"
        archive_dest = self.archives_dir / archive_name
        report = {}
        interrupted = False
        old_temp, old_tmpdir = tempfile.tempdir, os.environ.get("TMPDIR")
        try:
            tempfile.tempdir = os.environ["TMPDIR"] = str(self.scratch_dir)
            with arxiv_client.golden_ratio_backoff_policy(
                base_seconds=8.0,
                cap_seconds=60.0,
                wait_budget_seconds=self.retry_wait_budget if self.retry_429 else 0.0,
            ):
                report = benchmark_runner.run_benchmark(
                    pairs=pairs,
                    corpus_root=self.corpus_dir,
                    batch_id=batch_id,
                    max_ids=target_limit,
                    log_root=self.logs_dir,
                    report_root=self.reports_dir,
                    experiment_id=experiment_id,
                    resume=effective_resume,
                    max_attempts=max_attempts,
                    timeout=self.timeout,
                    disk_roots=[self.corpus_dir, self.scratch_dir],
                    s2_config=s2_config,
                    revision_history=revision_history,
                )
        except KeyboardInterrupt:
            interrupted = True
            done_now = self.count_completed_papers(number)
            print(f"\nĐã tạm dừng lô {number} tại bài báo {done_now}/{len(pairs)}. Chạy lại ô lệnh để tiếp tục từ bài {done_now + 1}.")
        finally:
            tempfile.tempdir = old_temp
            if old_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old_tmpdir
            self._archive_cohort(number, archive_dest)

        elapsed = time.time() - started
        completed_final = self.count_completed_papers(number)
        newly_done = max(0, completed_final - already_done)
        avg_seconds = (elapsed / newly_done) if newly_done > 0 else None
        batch_stopped = bool(
            interrupted
            or report.get("stopped")
            or report.get("batch_status") in {"stopped", "failed"}
            or report.get("execution_status") == "raised"
        )
        report["fast_runner"] = {
            "cohort": number,
            "interrupted": interrupted,
            "stopped": batch_stopped,
            "stop_reason": report.get("stop_reason"),
            "completed_papers": completed_final,
            "newly_completed_papers": newly_done,
            "elapsed_seconds": elapsed,
            "avg_seconds_per_paper": avg_seconds,
            "archive_path": str(archive_dest),
        }
        return report

    def enrich_cohort_s2(
        self,
        number: int,
        *,
        s2_config: Optional[dict] = None,
        max_ids: Optional[int] = None,
    ) -> dict:
        """Enrich a completed cohort with Semantic Scholar metadata and references.

        Runs S2 enrichment safely without re-downloading arXiv TeX sources.
        Synchronizes the enriched cohort back to Google Drive upon completion.
        """
        already_done = self.count_completed_papers(number)
        if already_done == 0:
            self.restore_cohort(number)
            already_done = self.count_completed_papers(number)

        effective_s2 = s2_config if s2_config is not None else {
            "timeout": 30.0,
            "max_attempts": 3,
            "min_interval": 3.0,
            "reference_limit": 100,
        }
        run_kwargs = {
            "resume": True,
            "revision_history": True,
            "s2_config": effective_s2,
        }
        if max_ids is not None:
            run_kwargs["max_ids"] = max_ids
        return self.run_cohort(number, **run_kwargs)

    @staticmethod
    def _compute_sha256(file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _archive_cohort(self, number: int, destination: Path) -> None:
        """Create a compressed archive for the cohort and store in Google Drive atomically."""
        pairs = cohort_pairs(number)
        import tarfile
        from src.storage import arxiv_id_to_folder_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as f_out:
                with tarfile.open(fileobj=f_out, mode="w:gz") as tar:
                    for _, arxiv_id in pairs:
                        safe_folder = arxiv_id_to_folder_key(arxiv_id)
                        paper_path = self.corpus_dir / safe_folder
                        if paper_path.is_dir():
                            mf = paper_path / "manifest.json"
                            if not mf.is_file():
                                continue
                            try:
                                m_data = json.loads(mf.read_text(encoding="utf-8"))
                                if not isinstance(m_data, dict):
                                    continue
                            except Exception:
                                continue
                            has_unsafe = False
                            for root, dirs, files in os.walk(paper_path):
                                for name in dirs + files:
                                    full_p = Path(root) / name
                                    if full_p.is_symlink() or not (full_p.is_file() or full_p.is_dir()):
                                        has_unsafe = True
                                        break
                                if has_unsafe:
                                    break
                            if not has_unsafe:
                                tar.add(paper_path, arcname=safe_folder, recursive=True)
                f_out.flush()
                os.fsync(f_out.fileno())

            # Verify readability of the created archive before atomic replacement
            with tarfile.open(tmp_path, "r:gz") as test_tar:
                _ = test_tar.getmembers()

            archive_sha256 = self._compute_sha256(tmp_path)
            os.replace(tmp_path, destination)

            # Atomic publication of SHA-256 checksum file
            sha_file = destination.with_name(destination.name + ".sha256")
            sha_tmp = destination.with_name(f".{destination.name}.sha256.tmp")
            sha_tmp.write_text(f"{archive_sha256}  {destination.name}\n", encoding="utf-8")
            os.replace(sha_tmp, sha_file)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _safe_replace_paper(
        self,
        src_folder: Path,
        target_folder: Path,
        *,
        expected_arxiv_id: Optional[str] = None,
    ) -> bool:
        """Validate source folder manifest and checksums before staging and replacing target folder."""
        src_mf = src_folder / "manifest.json"
        if not src_mf.is_file():
            return False
        try:
            m_src = json.loads(src_mf.read_text(encoding="utf-8"))
            if not isinstance(m_src, dict) or ("discovery_status" not in m_src and "status" not in m_src):
                return False
            if expected_arxiv_id is not None and "arxiv_id" in m_src:
                if m_src.get("arxiv_id") != expected_arxiv_id:
                    return False
        except Exception:
            return False

        # Validate file checksums in src_folder if versions with file_checksums exist
        versions = m_src.get("versions") or {}
        for ver_name, ver_info in versions.items():
            if isinstance(ver_info, dict) and ver_info.get("status") == "success" and ver_info.get("file_checksums"):
                ver_dir = src_folder / ver_name
                if not ver_dir.is_dir():
                    return False
                for fname, expected_hash in ver_info["file_checksums"].items():
                    fpath = ver_dir / fname
                    if not fpath.is_file():
                        return False
                    if self._compute_sha256(fpath) != expected_hash:
                        return False

        if target_folder.exists():
            target_mf = target_folder / "manifest.json"
            if target_mf.is_file():
                try:
                    m_target = json.loads(target_mf.read_text(encoding="utf-8"))
                    src_up = str(m_src.get("updated_at", ""))
                    tgt_up = str(m_target.get("updated_at", ""))
                    if tgt_up and src_up and tgt_up >= src_up:
                        return False
                except Exception:
                    pass

        # Atomic replacement: stage to temporary directory on the same filesystem
        target_parent = target_folder.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(target_parent)))
        backup_dir = None
        try:
            stage_paper = stage_dir / target_folder.name
            shutil.copytree(src_folder, stage_paper)

            if target_folder.exists():
                backup_dir = Path(tempfile.mkdtemp(prefix=".backup-", dir=str(target_parent)))
                backup_paper = backup_dir / target_folder.name
                os.replace(target_folder, backup_paper)

            os.replace(stage_paper, target_folder)

            if backup_dir and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            return True
        except Exception:
            if backup_dir and (backup_dir / target_folder.name).exists() and not target_folder.exists():
                try:
                    os.replace(backup_dir / target_folder.name, target_folder)
                except Exception:
                    pass
            return False
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            if backup_dir and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)

    def restore_cohort(self, number: int) -> int:
        """Unpack an existing cohort archive or legacy Drive corpus back to local SSD safely.

        Enables seamless resumption across Colab runtime reconnects in seconds.
        Migrates verified papers from legacy drive_root / "corpus" and extracts
        tar archive members without overwriting newer local files.
        Restricts restoration strictly to the cohort's assigned papers.
        Returns the count of restored/migrated paper folders.
        """
        restored = 0
        legacy_corpus = self.drive_root / "corpus"
        from src.storage import arxiv_id_to_folder_key

        valid_pairs = cohort_pairs(number)
        allowed_folders = {arxiv_id_to_folder_key(aid): (mssv, aid) for mssv, aid in valid_pairs}

        # 1. Migrate from legacy Drive corpus layout if present
        for folder_name, (mssv, arxiv_id) in allowed_folders.items():
            legacy_folder = legacy_corpus / folder_name
            local_folder = self.corpus_dir / folder_name
            if legacy_folder.is_dir():
                if self._safe_replace_paper(legacy_folder, local_folder, expected_arxiv_id=arxiv_id):
                    restored += 1

        # 2. Extract cohort archive from Drive without overwriting newer local papers
        archive_path = self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
        if not archive_path.is_file():
            return restored

        sha_file = archive_path.with_name(archive_path.name + ".sha256")
        if sha_file.is_file():
            try:
                expected_sha = sha_file.read_text(encoding="utf-8").strip().split()[0]
                actual_sha = self._compute_sha256(archive_path)
                if expected_sha != actual_sha:
                    raise ValueError(f"Archive checksum mismatch for {archive_path.name}: expected {expected_sha}, got {actual_sha}")
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"Cannot verify archive checksum: {e}") from e

        import tarfile
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=str(self.local_root)) as temp_extract:
            temp_dir = Path(temp_extract)
            with tarfile.open(archive_path, "r:gz") as tar:
                for m in tar.getmembers():
                    p = PurePosixPath(m.name)
                    if p.is_absolute() or ".." in p.parts:
                        raise ValueError(f"Unsafe archive member: {m.name}")
                    if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                        raise ValueError(f"Special files not allowed in backup: {m.name}")
                    if not (m.isfile() or m.isdir()):
                        raise ValueError(f"Unrecognized member type in backup: {m.name}")
                tar.extractall(path=temp_dir)

            for item in temp_dir.iterdir():
                if item.is_dir() and item.name in allowed_folders:
                    _, expected_aid = allowed_folders[item.name]
                    target_folder = self.corpus_dir / item.name
                    if self._safe_replace_paper(item, target_folder, expected_arxiv_id=expected_aid):
                        restored += 1
        return restored

"""High-throughput M1 production engine with SSD/RAM-disk & Kaggle discovery caching.

Extends the M1 production workflow with:
1. Local NVMe SSD storage (/content/corpus_ultra) to eliminate Google Drive FUSE latency.
2. RAM Disk (/dev/shm) scratch space for zero-I/O-latency TeX archive decompression.
3. Pre-cached Discovery ingestion via Cornell arXiv Kaggle snapshot (src.kaggle_discovery).
4. Bounded arXiv 429 auto-retry enabled by default (60s fallback, 300s budget).
5. Automated archive synchronization (.tar.gz) to Google Drive per cohort.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from src import arxiv_client, batch, benchmark, benchmark_runner, id_ranges
from src.kaggle_discovery import ingest_kaggle_snapshot
from src.storage import CorpusStorage, _atomic_json


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
            if not manifest_path.exists():
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
        return reset_count

    def count_completed_papers(self, number: int) -> int:
        """Count how many papers in the cohort have finished processing (no retryable versions left)."""
        from src.storage import RETRYABLE, arxiv_id_to_folder_key
        done = 0
        for _, arxiv_id in cohort_pairs(number):
            manifest_path = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if manifest_path.exists():
                try:
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    versions = m.get("versions") or {}
                    if m.get("discovery_status") == "not_found" or (
                        versions and all(
                            v.get("status") not in RETRYABLE and v.get("status") != "in_progress"
                            for v in versions.values()
                        )
                    ):
                        done += 1
                except Exception:
                    pass
        return done

    def _resolve_batch_id(self, number: int, max_attempts: int, revision_history: bool) -> Tuple[str, bool]:
        """Resolve a non-destructive batch_id and determine if it can safely resume."""
        base_id = f"fast-arxiv-{number:04d}"
        base_dir = self.logs_dir / base_id
        batch_json_path = base_dir / "batch.json"
        if not batch_json_path.exists():
            return base_id, False
        try:
            saved_def = json.loads(batch_json_path.read_text(encoding="utf-8")).get("definition", {})
            if (
                saved_def.get("max_attempts") == max_attempts
                and saved_def.get("revision_history", False) == bool(revision_history)
            ):
                return base_id, True
        except Exception:
            pass
        variant_id = f"fast-arxiv-{number:04d}-a{max_attempts}-h{int(bool(revision_history))}"
        variant_dir = self.logs_dir / variant_id
        variant_json = variant_dir / "batch.json"
        if variant_json.exists():
            try:
                saved_def = json.loads(variant_json.read_text(encoding="utf-8")).get("definition", {})
                if (
                    saved_def.get("max_attempts") == max_attempts
                    and saved_def.get("revision_history", False) == bool(revision_history)
                ):
                    return variant_id, True
            except Exception:
                pass
        return variant_id, False

    def run_cohort(self, number: int, *, resume: bool = False, revision_history: bool = False) -> dict:
        """Execute one cohort (100 papers) with SSD acceleration, exponential backoff retries and Drive sync."""
        self._reset_recoverable_archive_conflicts(number)
        pairs = cohort_pairs(number)
        max_attempts = 6 if (self.retry_429 and self.retry_wait_budget > 0) else 1
        batch_id, effective_resume = self._resolve_batch_id(number, max_attempts, revision_history)
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
                    max_ids=len(pairs),
                    log_root=self.logs_dir,
                    report_root=self.reports_dir,
                    experiment_id=experiment_id,
                    resume=effective_resume,
                    max_attempts=max_attempts,
                    timeout=self.timeout,
                    disk_roots=[self.corpus_dir, self.scratch_dir],
                    s2_config=None,
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
        newly_done = max(1, completed_final - already_done)
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
            "elapsed_seconds": elapsed,
            "avg_seconds_per_paper": elapsed / newly_done,
            "archive_path": str(archive_dest),
        }
        return report

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
                        if paper_path.exists():
                            tar.add(paper_path, arcname=safe_folder)
                f_out.flush()
                os.fsync(f_out.fileno())
            os.replace(tmp_path, destination)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def restore_cohort(self, number: int) -> int:
        """Unpack an existing cohort archive or legacy Drive corpus back to local SSD safely.

        Enables seamless resumption across Colab runtime reconnects in seconds.
        Migrates verified papers from legacy drive_root / "corpus" and extracts
        tar archive members without overwriting newer local files.
        Returns the count of restored/migrated paper folders.
        """
        restored = 0
        legacy_corpus = self.drive_root / "corpus"
        from src.storage import arxiv_id_to_folder_key

        # 1. Migrate from legacy Drive corpus layout if present
        for _, arxiv_id in cohort_pairs(number):
            folder_name = arxiv_id_to_folder_key(arxiv_id)
            legacy_folder = legacy_corpus / folder_name
            local_folder = self.corpus_dir / folder_name
            if legacy_folder.is_dir():
                should_copy = not local_folder.exists()
                if not should_copy and (legacy_folder / "manifest.json").exists() and (local_folder / "manifest.json").exists():
                    try:
                        m_leg = json.loads((legacy_folder / "manifest.json").read_text(encoding="utf-8"))
                        m_loc = json.loads((local_folder / "manifest.json").read_text(encoding="utf-8"))
                        if m_leg.get("updated_at", "") > m_loc.get("updated_at", ""):
                            should_copy = True
                    except Exception:
                        pass
                if should_copy:
                    if local_folder.exists():
                        shutil.rmtree(local_folder)
                    shutil.copytree(legacy_folder, local_folder)
                    restored += 1

        # 2. Extract cohort archive from Drive without overwriting newer local papers
        archive_path = self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
        if not archive_path.exists():
            return restored

        import tarfile
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=self.local_root) as temp_extract:
            temp_dir = Path(temp_extract)
            with tarfile.open(archive_path, "r:gz") as tar:
                for m in tar.getmembers():
                    p = PurePosixPath(m.name)
                    if p.is_absolute() or ".." in p.parts:
                        raise ValueError(f"Unsafe archive member: {m.name}")
                    if not (m.isfile() or m.isdir()):
                        raise ValueError(f"Special files not allowed in backup: {m.name}")
                tar.extractall(path=temp_dir)
            for item in temp_dir.iterdir():
                if item.is_dir():
                    target_folder = self.corpus_dir / item.name
                    if not target_folder.exists():
                        shutil.copytree(item, target_folder)
                        restored += 1
                    else:
                        local_mf = target_folder / "manifest.json"
                        arch_mf = item / "manifest.json"
                        should_replace = False
                        if arch_mf.exists() and local_mf.exists():
                            try:
                                m_arch = json.loads(arch_mf.read_text(encoding="utf-8"))
                                m_loc = json.loads(local_mf.read_text(encoding="utf-8"))
                                if m_arch.get("updated_at", "") > m_loc.get("updated_at", ""):
                                    should_replace = True
                            except Exception:
                                pass
                        if should_replace:
                            shutil.rmtree(target_folder)
                            shutil.copytree(item, target_folder)
                            restored += 1
        return restored

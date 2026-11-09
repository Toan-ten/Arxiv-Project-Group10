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
from pathlib import Path
import shutil
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

    def count_completed_papers(self, number: int) -> int:
        """Count how many papers in the cohort have already completed source crawling."""
        from src.storage import arxiv_id_to_folder_key
        done = 0
        for _, arxiv_id in cohort_pairs(number):
            manifest_path = self.corpus_dir / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if manifest_path.exists():
                try:
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    versions = m.get("versions") or {}
                    if m.get("discovery_status") == "not_found" or (
                        versions and all(v.get("status") in {"success", "no_tex_source"} for v in versions.values())
                    ):
                        done += 1
                except Exception:
                    pass
        return done

    def run_cohort(self, number: int, *, resume: bool = False, revision_history: bool = False) -> dict:
        """Execute one cohort (100 papers) with SSD acceleration, exponential backoff retries and Drive sync."""
        pairs = cohort_pairs(number)
        batch_id = f"fast-arxiv-{number:04d}"
        experiment_id = f"exp-fast-{number:04d}-{int(time.time())}"
        max_attempts = 6 if self.retry_429 else 1
        batch_log_dir = self.logs_dir / batch_id
        batch_json_path = batch_log_dir / "batch.json"
        if batch_json_path.exists():
            try:
                saved_def = json.loads(batch_json_path.read_text(encoding="utf-8")).get("definition", {})
                if (
                    saved_def.get("max_attempts") != max_attempts
                    or saved_def.get("revision_history", False) != bool(revision_history)
                ):
                    shutil.rmtree(batch_log_dir)
            except Exception:
                pass
        effective_resume = batch_json_path.exists()
        already_done = self.count_completed_papers(number)
        if already_done > 0:
            print(f"Lô {number}: Đã có {already_done}/{len(pairs)} bài báo hoàn thành từ phiên trước, tiếp tục thu thập các bài còn lại.")

        started = time.time()
        archive_name = f"cohort_{number:04d}_corpus.tar.gz"
        archive_dest = self.archives_dir / archive_name
        report = {}
        interrupted = False
        import tempfile
        old_temp, old_tmpdir = tempfile.tempdir, os.environ.get("TMPDIR")
        try:
            tempfile.tempdir = os.environ["TMPDIR"] = str(self.scratch_dir)
            with arxiv_client.golden_ratio_backoff_policy(base_seconds=8.0, cap_seconds=60.0):
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
        report["fast_runner"] = {
            "cohort": number,
            "interrupted": interrupted,
            "completed_papers": completed_final,
            "elapsed_seconds": elapsed,
            "avg_seconds_per_paper": elapsed / newly_done,
            "archive_path": str(archive_dest),
        }
        return report

    def _archive_cohort(self, number: int, destination: Path) -> None:
        """Create a compressed archive for the cohort and store in Google Drive."""
        pairs = cohort_pairs(number)
        import tarfile
        from src.storage import arxiv_id_to_folder_key
        with tarfile.open(destination, "w:gz") as tar:
            for _, arxiv_id in pairs:
                safe_folder = arxiv_id_to_folder_key(arxiv_id)
                paper_path = self.corpus_dir / safe_folder
                if paper_path.exists():
                    tar.add(paper_path, arcname=safe_folder)

    def restore_cohort(self, number: int) -> int:
        """Unpack an existing cohort archive from Google Drive back to local SSD.

        Enables seamless resumption across Colab runtime reconnects in seconds.
        Returns the count of restored paper folders.
        """
        archive_path = self.archives_dir / f"cohort_{number:04d}_corpus.tar.gz"
        if not archive_path.exists():
            return 0

        import tarfile
        restored = 0
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if member.isdir() and "/" not in member.name:
                    restored += 1
            tar.extractall(path=self.corpus_dir)
        return restored

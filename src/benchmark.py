"""Benchmark the M1 crawling pipeline.

The benchmark measures elapsed wall time, sampled process-tree RSS, corpus size,
and filesystem usage. ``psutil`` is used when available; on Linux the module
falls back to ``/proc`` and ``resource`` so it still runs in minimal Colab or
WSL environments.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    import psutil
except ImportError:  # pragma: no cover - depends on the environment
    psutil = None

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None

CrawlerCallable = Callable[[], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mb(value_bytes: float) -> float:
    return round(value_bytes / (1024 * 1024), 3)


def _existing_path(path: Path) -> Path:
    """Return the path itself or its nearest existing parent."""

    candidate = path.expanduser().resolve()
    for current in (candidate, *candidate.parents):
        if current.exists():
            return current
    return Path.cwd()


def get_directory_size(path: str | os.PathLike[str]) -> int:
    """Return the total size of regular files under ``path`` in bytes."""

    root = Path(path)
    if not root.exists():
        return 0

    total = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0

    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                total += get_directory_size(entry.path)
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _read_proc_rss_bytes(pid: int = 0) -> int | None:
    """Read current RSS from Linux procfs, or return None when unavailable."""

    if not sys.platform.startswith("linux"):
        return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        statm = Path(f"/proc/{pid or 'self'}/statm").read_text(encoding="ascii")
        return int(statm.split()[1]) * page_size
    except (OSError, ValueError, AttributeError, IndexError):
        return None


def _psutil_tree_rss_bytes() -> int | None:
    if psutil is None:
        return None
    try:
        process = psutil.Process(os.getpid())
        processes = [process, *process.children(recursive=True)]
    except (psutil.Error, OSError):
        return None

    total = 0
    for item in processes:
        try:
            total += item.memory_info().rss
        except (psutil.Error, OSError):
            continue
    return total


def _resource_peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    # Linux reports KiB; macOS reports bytes.
    return int(peak * 1024) if sys.platform.startswith("linux") else int(peak)


def _current_rss_bytes() -> int:
    value = _psutil_tree_rss_bytes()
    if value is None:
        value = _read_proc_rss_bytes()
    if value is None:
        value = _resource_peak_rss_bytes()
    return value or 0


def _ram_source() -> str:
    if psutil is not None:
        return "psutil process tree"
    if sys.platform.startswith("linux"):
        return "linux procfs (/proc/self/statm)"
    if resource is not None:
        return "resource.ru_maxrss"
    return "unavailable"


def _total_ram_bytes() -> int | None:
    if psutil is not None:
        try:
            return psutil.virtual_memory().total
        except (psutil.Error, OSError):
            return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


class _RssMonitor:
    """Sample RSS in a background thread and keep the peak value."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.initial_bytes = _current_rss_bytes()
        self.peak_bytes = self.initial_bytes
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 10))
        self.peak_bytes = max(self.peak_bytes, _current_rss_bytes())

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.peak_bytes = max(self.peak_bytes, _current_rss_bytes())


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _print_report(report: dict) -> None:
    print("\n" + "=" * 40)
    print("        KẾT QUẢ ĐO HIỆU NĂNG M1")
    print("=" * 40)
    print(f"Trạng thái: {report['status']}")
    print(
        "Thời gian: "
        f"{report['elapsed_seconds']} giây (~{report['elapsed_minutes']} phút)"
    )
    print(f"RAM ban đầu: {report['initial_ram_mb']} MB")
    print(f"RAM kết thúc: {report['final_ram_mb']} MB")
    print(f"RAM đỉnh điểm: {report['peak_ram_mb']} MB ({report['ram_source']})")
    print(f"Dung lượng corpus: {report['disk_corpus_size_mb']} MB")
    print("=" * 40)


def benchmark_pipeline(
    crawler_main_func: CrawlerCallable,
    corpus_output_dir: str | os.PathLike[str] = "./data/corpus",
    report_path: str | os.PathLike[str] = "./benchmark_results/m1_benchmark.json",
    sample_interval: float = 0.1,
    verbose: bool = True,
) -> dict:
    """Run ``crawler_main_func`` and write a benchmark report.

    The crawler exception is recorded in the report and re-raised; failures are
    never silently converted into a successful measurement.
    """

    if not callable(crawler_main_func):
        raise TypeError("crawler_main_func must be callable")

    corpus_dir = Path(corpus_output_dir)
    report_file = Path(report_path)
    filesystem_root = _existing_path(corpus_dir)

    if verbose:
        print("=" * 60)
        print("   BẮT ĐẦU ĐO HIỆU NĂNG CRAWLER M1")
        print("=" * 60)
        print(f"--> Đang thực thi: {getattr(crawler_main_func, '__name__', 'crawler')}")

    monitor = _RssMonitor(sample_interval)
    initial_disk_used = shutil.disk_usage(filesystem_root).used
    started_at = _utc_now()
    started_perf = time.perf_counter()
    failure: BaseException | None = None
    failure_info: dict | None = None
    monitor.start()

    try:
        crawler_main_func()
    except BaseException as exc:  # record and re-raise after writing the report
        failure = exc
        failure_info = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        }
    finally:
        monitor.stop()
        elapsed = time.perf_counter() - started_perf
        final_disk_used = shutil.disk_usage(filesystem_root).used
        total_ram = _total_ram_bytes()
        report = {
            "status": "failed" if failure is not None else "success",
            "crawler": getattr(crawler_main_func, "__name__", repr(crawler_main_func)),
            "started_at": started_at,
            "ended_at": _utc_now(),
            "elapsed_seconds": round(elapsed, 3),
            "elapsed_minutes": round(elapsed / 60, 3),
            "initial_ram_mb": _mb(monitor.initial_bytes),
            "final_ram_mb": _mb(_current_rss_bytes()),
            "peak_ram_mb": _mb(monitor.peak_bytes),
            "ram_source": _ram_source(),
            "ram_sample_interval_seconds": monitor.interval_seconds,
            "disk_corpus_size_mb": _mb(get_directory_size(corpus_dir)),
            "disk_used_diff_mb": _mb(final_disk_used - initial_disk_used),
            "corpus_output_dir": str(corpus_dir.expanduser().resolve()),
            "filesystem_path": str(filesystem_root),
            "report_path": str(report_file.expanduser().resolve()),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "total_ram_mb": _mb(total_ram) if total_ram is not None else None,
                "git_commit": _git_commit(),
                "cwd": str(Path.cwd()),
            },
            "error": failure_info,
        }
        _write_report(report_file, report)
        if verbose:
            _print_report(report)
            print(f"-> Đã xuất kết quả đo ra file: {report['report_path']}")

    if failure is not None:
        raise failure
    return report


def _dummy_crawler() -> None:
    target = Path("./data/corpus/dummy_paper/v1")
    target.mkdir(parents=True, exist_ok=True)
    time.sleep(1.0)
    (target / "main.tex").write_text("A" * (1024 * 1024), encoding="utf-8")


if __name__ == "__main__":
    benchmark_pipeline(_dummy_crawler)

"""Sampling and timing definitions for M1; no network or corpus mutation."""
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def sampling_config(corpus_root, sample_interval=0.1, disk_interval=30.0, disk_roots=None):
    if not finite(sample_interval) or sample_interval == 0:
        raise ValueError("sample_interval must be finite and positive")
    if not finite(disk_interval) or disk_interval == 0:
        raise ValueError("disk_interval must be finite and positive")
    corpus = Path(corpus_root).expanduser().resolve()
    if disk_roots is None:
        roots = [corpus]
    else:
        if not isinstance(disk_roots, (list, tuple)) or not disk_roots:
            raise ValueError("disk_roots must be an explicit nonempty list/tuple")
        roots = [Path(p).expanduser().resolve() for p in disk_roots]
    if corpus not in roots:
        raise ValueError("disk_roots must include the corpus root")
    for i, root in enumerate(roots):
        if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots[:i]):
            raise ValueError("disk_roots must be disjoint (no duplicates/nesting)")
    return dict(
        ram_interval_seconds=max(0.01, float(sample_interval)),
        disk_interval_seconds=max(1.0, float(disk_interval)),
        disk_roots=[str(p) for p in roots], corpus_root=str(corpus),
    )


def directory_bytes(root):
    """Strict logical file size; missing root is empty, unreadable is unknown."""
    total, pending = 0, [Path(root)]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path == Path(root):
                return 0
            raise  # A disappearing entry makes this scan incomplete.
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise OSError("Unsafe disk entry")
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
        else:
            raise OSError("Unsupported disk entry")
    return total


def _ram_reader():
    if psutil is not None:
        def read_tree():
            parent = psutil.Process(os.getpid())
            processes = [parent, *parent.children(recursive=True)]
            total = 0
            for process in processes:
                try:
                    total += process.memory_info().rss
                except psutil.NoSuchProcess:
                    if process is parent:
                        raise
                    # The child exited between enumeration and observation.
            return total
        return read_tree, "psutil", "current process and observed recursive descendants (sum RSS)"
    if sys.platform.startswith("linux"):
        def read_self():
            fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        return read_self, "proc_self_statm", "current process only; descendants not collected"
    def unavailable():
        raise ImportError("RSS backend unavailable")
    return unavailable, "unavailable", "not_collected"


class _Series:
    def __init__(self):
        self.count, self.total, self.peak = 0, 0, None
        self.initial = self.final = self.first = self.last = None
        self.failures = {}

    def add(self, value, at):
        if type(value) is not int or value < 0 or not finite(at):
            raise ValueError("Invalid sample")
        if self.last is not None and at < self.last:
            raise ValueError("Nonmonotonic sample")
        if self.count == 0:
            self.initial, self.first = value, at
        self.final, self.last = value, at
        self.count += 1
        self.total += value
        self.peak = max(self.peak or 0, value)

    def failed(self, reason):
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def report(self):
        return dict(
            status=("not_collected" if not self.count else
                    "partial" if self.failures else "collected"),
            reason="no_valid_samples" if not self.count else None,
            failure_reasons=dict(self.failures), unit="bytes",
            sample_count=self.count, failed_sample_count=sum(self.failures.values()),
            sum_sample_bytes=self.total if self.count else None,
            initial_bytes=self.initial, final_bytes=self.final,
            sampled_peak_bytes=self.peak,
            average_bytes=self.total / self.count if self.count else None,
            average_method="arithmetic mean of valid RSS observations; not time weighted",
            first_sample_seconds=self.first, last_sample_seconds=self.last,
        )


class ResourceSampler:
    """Two probe threads, bounded memory, configurable RAM and slower disk scans.

    Clock/readers can be injected for deterministic offline unit tests. Endpoints
    are sampled as well. Disk byte totals are logical sizes, not allocated blocks.
    """
    def __init__(self, corpus_root, *, sample_interval=0.1, disk_interval=30.0,
                 disk_roots=None, ram_reader=None, disk_reader=None, clock=None):
        self.config = sampling_config(corpus_root, sample_interval, disk_interval, disk_roots)
        read_ram, self.ram_source, self.ram_scope = _ram_reader()
        self.read_ram = read_ram if ram_reader is None else ram_reader
        self.read_disk = directory_bytes if disk_reader is None else disk_reader
        self.clock = time.perf_counter if clock is None else clock
        self.started = self.clock()
        self.ram, self.disk = _Series(), _Series()
        self.final_corpus_bytes = None
        self._stop = threading.Event()
        self._threads = []

    def sample_ram(self):
        try:
            value = self.read_ram()
            self.ram.add(value, self.clock() - self.started)
        except Exception as exc:
            self.ram.failed("backend_missing" if isinstance(exc, ImportError) else "ram_read_failed")

    def sample_disk(self, *, final=False):
        sizes, failed = {}, False
        for root in self.config["disk_roots"]:
            try:
                value = self.read_disk(Path(root))
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid size")
                sizes[root] = value
            except Exception:
                failed = True
        if final:
            self.final_corpus_bytes = sizes.get(self.config["corpus_root"])
        if failed:
            self.disk.failed("disk_scan_failed")
            return
        try:
            self.disk.add(sum(sizes.values()), self.clock() - self.started)
        except Exception:
            self.disk.failed("disk_scan_failed")

    def _loop(self, interval, callback):
        while not self._stop.wait(interval):
            callback()

    def start(self):
        self.sample_disk()
        self.sample_ram()
        for interval, callback in (
            (self.config["ram_interval_seconds"], self.sample_ram),
            (self.config["disk_interval_seconds"], self.sample_disk),
        ):
            thread = threading.Thread(target=self._loop, args=(interval, callback), daemon=True)
            try:
                thread.start()
                self._threads.append((thread, callback == self.sample_ram))
            except RuntimeError:
                (self.ram if callback == self.sample_ram else self.disk).failed("sampler_thread_failed")

    def stop(self):
        self._stop.set()
        for thread, is_ram in self._threads:
            if is_ram:
                thread.join()
        self.sample_ram()  # Avoid waiting for a potentially slow disk scan first.
        for thread, is_ram in self._threads:
            if not is_ram:
                thread.join()
        self.sample_disk(final=True)

    def report(self):
        ram, disk = self.ram.report(), self.disk.report()
        ram.update(source=self.ram_source, scope=self.ram_scope,
                   backend_version=getattr(psutil, "__version__", None) if self.ram_source == "psutil" else None,
                   peak_method="sampled maximum, not absolute OS high-water mark")
        disk.update(
            roots=self.config["disk_roots"], corpus_root=self.config["corpus_root"],
            scope="logical regular-file bytes under disjoint listed roots only",
            final_corpus_bytes=self.final_corpus_bytes,
            final_corpus_status="collected" if self.final_corpus_bytes is not None else "not_collected",
            peak_method="sampled maximum; may miss short-lived archives/files",
        )
        disk.pop("average_method")
        disk.pop("average_bytes")
        return dict(schema_version=2, sampling=self.config, ram=ram, disk=disk)


def timing_summary(history):
    """Derive timings only from actual logged intervals; legacy fields stay unknown."""
    queried_seconds, paper_seconds = 0.0, 0.0
    query_count, cached_count, observations, unknown_discovery, unknown_paper = 0, 0, 0, 0, 0
    processed_ids = set()
    paper_intervals = []
    for attempt in history:
        for row in attempt["papers"]:
            if row["started_at"] is not None:
                result = row.get("result") or {}
                discovery = result.get("discovery") or {}
                revision = result.get("revision_history") or {}
                paper_intervals.append({"attempt_id": attempt.get("attempt_id"),
                                        "arxiv_id": row["arxiv_id"],
                                        "state": row["state"],
                                        "wall_seconds": row["wall_seconds"],
                                        "discovery_action": discovery.get("action"),
                                        "discovery_status": discovery.get("status"),
                                        "discovery_seconds": discovery.get("wall_seconds"),
                                        "revision_action": revision.get("action"),
                                        "revision_seconds": revision.get("wall_seconds")})
            if row["state"] == "processed":
                processed_ids.add(row["arxiv_id"])
            if row["started_at"] is not None or row["state"] == "processed":
                if row["started_at"] is not None and finite(row["wall_seconds"]):
                    paper_seconds += row["wall_seconds"]
                    observations += 1
                else:
                    unknown_paper += 1
            result = row["result"]
            discovery = result.get("discovery", {}) if result else {}
            if discovery.get("action") == "queried":
                query_count += 1
                if discovery.get("timing_status") == "measured" and finite(discovery.get("wall_seconds")):
                    queried_seconds += discovery["wall_seconds"]
                else:
                    unknown_discovery += 1
            elif discovery.get("action") == "cached":
                cached_count += 1
            elif row["state"] not in {"pending"}:
                # Interrupted before the final callback: no evidence whether a
                # Discovery call was started; never silently infer zero seconds.
                unknown_discovery += 1
    cohort = len(history[0]["papers"]) if history else 0
    all_processed = bool(cohort) and len(processed_ids) == cohort and not unknown_paper
    return dict(
        unit="seconds", discovery_queried=query_count, discovery_cached=cached_count,
        discovery_wall_seconds=queried_seconds if not unknown_discovery else None,
        discovery_known_wall_seconds=queried_seconds,
        discovery_timing_status="collected" if not unknown_discovery else "not_collected",
        discovery_missing_intervals=unknown_discovery,
        discovery_scope="query_arxiv call including pacing/retry/parsing; excludes cache and Storage writes",
        paper_observations=observations, unique_processed_papers=len(processed_ids),
        paper_intervals=paper_intervals,
        cohort_papers=cohort, known_paper_wall_seconds=paper_seconds,
        missing_paper_intervals=unknown_paper,
        average_paper_seconds=paper_seconds / cohort if all_processed else None,
        average_paper_status="collected" if all_processed else "not_collected",
        average_paper_method="sum of logged started-paper intervals across attempts / selected unique IDs; includes retries/cache revisits",
    )

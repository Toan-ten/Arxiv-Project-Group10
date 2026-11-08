"""Bounded arXiv-first cohorts; existing Batch/Benchmark own crawling and resume.

No requests on import. Single writer/runtime only, including Google Drive.
No S2 configuration, credentials, new retry engine or Storage schema.
"""
from dataclasses import asdict
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from uuid import uuid4

from src import arxiv_client, arxiv_history, batch, benchmark_runner, id_ranges
from src.storage import CorpusStorage, _atomic_json


COHORT_SIZE = 100
_WRITER = threading.Lock()  # Process-local only, not a distributed Drive lock.


class ProductionError(ValueError):
    """Unsafe configuration or evidence; preserve it for operator review."""


def _require(condition, message):
    if not condition:
        raise ProductionError(message)


def _assignments():
    config = id_ranges.load_config()
    _require(len(config) == 4 and all(r.end_id - r.start_id + 1 == 5000
                                     for r in config.values()),
             "Expected four assigned ranges of 5000 candidate IDs")
    return config


def cohort_pairs(number):
    """Cohorts 1..200, 100 positions each, in assigned-config order."""
    _require(type(number) is int and 1 <= number <= 200, "Cohort must be 1..200")
    start = (number - 1) * COHORT_SIZE
    return list(islice(id_ranges.iter_assigned_ids(_assignments()), start, start + COHORT_SIZE))


def _code_state(repo):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    try:
        _require(Path(git("rev-parse", "--show-toplevel")).resolve() == repo,
                 "Use the Code repository root")
        return git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=normal")
    except (OSError, subprocess.SubprocessError):
        raise ProductionError("Cannot verify code checkout") from None


class ProductionRun:
    """One immutable campaign, shared corpus, fixed non-overlapping cohorts.

    Re-running a cohort audits first. Only incomplete, valid evidence enters
    run_benchmark(resume=True); complete arXiv cohorts never invoke it.
    An explicit later cell Run is a new attempt, including after a policy stop.
    """

    def __init__(self, repo, drive_root, code_commit, *, timeout=60.0,
                 sample_interval=1.0, disk_interval=120.0):
        self.repo, self.root = Path(repo).resolve(), batch._safe(drive_root)
        _require(not (self.root.is_relative_to(self.repo) or self.repo.is_relative_to(self.root)),
                 "Production data must be outside the code checkout")
        _require(isinstance(code_commit, str) and re.fullmatch(r"[0-9a-f]{40}", code_commit),
                 "Pin the full reviewed code commit")
        commit, dirty = _code_state(self.repo)
        _require(commit == code_commit and not dirty, "Code must be clean and match the pinned commit")
        for value in (timeout, sample_interval, disk_interval):
            _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                     "Timeout and sampling intervals must be finite positive numbers")
        self.corpus, self.logs = self.root / "corpus", self.root / "logs"
        self.reports, self.scratch = self.root / "reports", self.root / "scratch"
        self.settings = dict(timeout=float(timeout), sample_interval=float(sample_interval),
                             disk_interval=float(disk_interval))
        self.definition = dict(schema_version=1, mode="arxiv_first", code_commit=commit,
            candidate_positions=20000, cohort_size=COHORT_SIZE,
            assignments=[asdict(r) for r in _assignments().values()],
            corpus_root=str(self.corpus), versions=None, revision_history=True,
            s2_config=None, max_attempts=1, user_agent=arxiv_client.USER_AGENT, **self.settings)
        path = self.root / "campaign.json"
        if self.root.exists():
            if path.exists():
                _require(batch._read(path) == self.definition,
                         "Campaign configuration changed; do not edit existing evidence")
            else:
                _require(not any(self.root.iterdir()),
                         "Nonempty directory without campaign.json; choose a new production root")

    def _batch_id(self, number):
        cohort_pairs(number)  # Validate before constructing any path.
        return f"m1-arxiv-{number:04d}"

    def _expected(self, pairs):
        return benchmark_runner._definition(pairs, self.corpus, len(pairs), None,
            self.settings["timeout"], 1, arxiv_client.USER_AGENT, None, True)

    def audit(self, number):
        """Read-only source/checksum/history audit, including confirmed absence."""
        pairs, batch_id = cohort_pairs(number), self._batch_id(number)
        directory = batch._safe(self.logs / batch_id)
        if not directory.exists():
            store = CorpusStorage(self.corpus)
            _require(not any(store.paper_path(*pair).exists() for pair in pairs),
                     "Selected checkpoint/source exists without batch logs; preserve and review")
            return dict(cohort=number, batch_id=batch_id, mode="new", arxiv_phase_complete=False,
                        aggregation=None, history_checks=[], issues=[])
        definition = batch._read(directory / "batch.json")
        _require(definition.get("definition") == self._expected(pairs),
                 "Batch definition differs from the production configuration")
        aggregate = benchmark_runner.aggregate_batch(self.corpus, batch_id=batch_id, log_root=self.logs)
        _require(aggregate["status"] == "verified", "Invalid logs/checkpoint/source; review without repair")
        coverage = aggregate["data_coverage"]
        _require(coverage is not None and coverage["cohort_papers"] == len(pairs), "Incomplete cohort evidence")
        checks, complete = [], []
        store = CorpusStorage(self.corpus)
        for paper in coverage["papers"]:
            pair = (paper["mssv"], paper["arxiv_id"])
            ready, history = False, "not_verified"
            if paper["checkpoint_status"] == "verified":
                if paper["discovery_status"] == "not_found":
                    ready, history = True, "not_applicable"
                elif paper["discovery_status"] == "found" and paper["version_status"] == "resolved":
                    metadata = store.read_metadata(*pair)
                    try:
                        valid_history = arxiv_history._cached(metadata) or arxiv_history._single_version_cached(metadata)
                    except (ValueError, KeyError, TypeError):
                        raise ProductionError("Invalid revision history provenance; preserve and review") from None
                    history = "verified" if valid_history else "not_verified"
                    ready = bool(paper["versions"]) and valid_history and all(
                        v["verified"] and v["status"] in {"success", "no_tex_source"}
                        for v in paper["versions"])
            checks.append(dict(mssv=pair[0], arxiv_id=pair[1], revision_history=history, arxiv_ready=ready))
            complete.append(ready)
        ready = len(complete) == len(pairs) and all(complete)
        return dict(cohort=number, batch_id=batch_id, mode="audit" if ready else "resume",
                    arxiv_phase_complete=ready, aggregation=aggregate, history_checks=checks, issues=[])

    def _export(self, result):
        path = batch._safe(self.root / "audits" / (f"cohort-{result['cohort']:04d}-" + uuid4().hex + ".json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _require(not path.exists(), "Audit destination already exists")
        result["audit_path"] = str(path)
        _atomic_json(path, result)

    @staticmethod
    def _print(result):
        aggregate = result["aggregation"] or {}
        coverage, history = aggregate.get("data_coverage") or {}, aggregate.get("history") or {}
        summary = dict(cohort=result["cohort"], mode=result["mode"],
            arxiv_phase_complete=result["arxiv_phase_complete"], batch_status=result.get("batch_status"),
            stop_reason=result.get("stop_reason"), candidates=coverage.get("cohort_papers", COHORT_SIZE),
            found=coverage.get("found_papers"), not_found=coverage.get("not_found_papers"),
            valid_source_versions=coverage.get("valid_tex_versions"), no_tex_versions=coverage.get("no_tex_versions"),
            version_statuses=coverage.get("version_statuses"),
            publication_complete=aggregate.get("publication_complete"),
            http_metrics=history.get("http_metrics"), timing=history.get("timings"),
            benchmark=aggregate.get("benchmark_metrics"), audit_path=result.get("audit_path"))
        # Detailed per-attempt metrics remain in the exported JSON, not a huge cell output.
        if summary["benchmark"]:
            summary["benchmark"] = {k: v for k, v in summary["benchmark"].items() if k != "attempts"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    def run(self, first=1, last=1):
        """Explicit inclusive cohort window. Stop on policy, failure or incomplete data.

        No automatic outer retry. A completed cohort receives only a new audit
        export; corpus, Batch logs and Benchmark reports remain byte-for-byte.
        """
        cohort_pairs(first)
        cohort_pairs(last)
        _require(first <= last, "first_cohort must not exceed last_cohort")
        _require(_WRITER.acquire(blocking=False), "Another production call is active in this process")
        results = []
        try:
            # Check again if the operator edited/replaced code since construction.
            commit, dirty = _code_state(self.repo)
            _require(commit == self.definition["code_commit"] and not dirty, "Code checkout changed")
            campaign = self.root / "campaign.json"
            if campaign.exists():
                _require(batch._read(campaign) == self.definition, "Campaign definition changed")
            else:
                _require(not self.root.exists() or not any(self.root.iterdir()), "Unowned production directory")
                self.root.mkdir(parents=True, exist_ok=True)
                _atomic_json(campaign, self.definition)
            for number in range(first, last + 1):
                result = self.audit(number)
                mode = result["mode"]
                if mode != "audit":
                    self.scratch.mkdir(exist_ok=True)
                    old_temp, old_tmpdir = tempfile.tempdir, os.environ.get("TMPDIR")
                    try:
                        tempfile.tempdir = os.environ["TMPDIR"] = str(self.scratch)
                        report = benchmark_runner.run_benchmark(cohort_pairs(number), self.corpus,
                            batch_id=result["batch_id"], max_ids=COHORT_SIZE,
                            log_root=self.logs, report_root=self.reports,
                            resume=mode == "resume", versions=None, revision_history=True,
                            s2_config=None, max_attempts=1, disk_roots=[self.corpus, self.scratch], **self.settings)
                    finally:
                        tempfile.tempdir = old_temp
                        if old_tmpdir is None:
                            os.environ.pop("TMPDIR", None)
                        else:
                            os.environ["TMPDIR"] = old_tmpdir
                    result = self.audit(number)
                    result.update(mode=mode, execution_status=report["execution_status"],
                                  batch_status=report["batch_status"], stop_reason=report["stop_reason"],
                                  benchmark_report_path=report["report_path"])
                self._export(result)
                self._print(result)
                results.append(result)
                if (not result["arxiv_phase_complete"] or
                        result.get("batch_status", "finished") != "finished" or
                        result.get("execution_status", "returned") != "returned"):
                    print("DỪNG: xem audit/logs. Không tự chạy lại. Với 403/429, chờ và kiểm tra trước khi bấm Run lần nữa.")
                    break
            return results
        except BaseException as exc:
            print("DỪNG an toàn; xem logs/reports và checkpoint tại:", self.root,
                  "| loại lỗi:", type(exc).__name__)
            raise
        finally:
            _WRITER.release()

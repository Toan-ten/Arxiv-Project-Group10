"""Bounded notebook A/B orchestration; existing crawler/Storage/metrics unchanged.

No import-time I/O. Real HTTP only from explicit preflight/run actions.
One kernel-wide lock; operators must exclude writers in other runtimes.
"""
import copy
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
from threading import Lock
import time
from uuid import uuid4

from src import arxiv_client, arxiv_history, benchmark_runner, http_metrics, pipeline
from src import s2_integration, semantic_scholar
from src.id_ranges import format_arxiv_id, load_config
from src.storage import CorpusStorage, StorageError, _atomic_json

_LOCK = Lock()
PHASES = {"A": ("combined",), "B": ("arxiv", "s2")}
DEFAULT_S2 = dict(timeout=30, max_attempts=1, min_interval=2,
                  reference_limit=100, max_pages=1000)


class ComparisonError(ValueError):
    """Safe operator-facing message; never includes credentials/HTTP exception text."""


def _require(condition, message):
    if not condition:
        raise ComparisonError(message)


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ComparisonError("Missing or corrupt experiment evidence; preserve it for audit") from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def code_state():
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    return dict(commit=git("rev-parse", "HEAD"), dirty=bool(git("status", "--porcelain")))


def runtime_profile():
    import platform
    import psutil
    return dict(python=platform.python_version(), system=platform.platform(),
                cpu_count=os.cpu_count(), psutil=psutil.__version__,
                kernel=f"{os.getpid()}:{psutil.Process().create_time()}")


def select_cohort(seed=10):
    """50 unfiltered candidate positions, balanced/interleaved across 4 owners."""
    _require(type(seed) is int, "Seed must be an integer")
    members = list(load_config().values())
    rng, chosen = random.Random(seed), []
    for index, member in enumerate(members):
        count = 13 if index < 2 else 12
        serials = sorted(rng.sample(range(member.start_id, member.end_id + 1), count))
        chosen.append([(member.mssv, format_arxiv_id(member.start_month, n)) for n in serials])
    result = [chosen[owner][offset] for offset in range(13)
              for owner in range(4) if offset < len(chosen[owner])]
    _require(len(result) == len(set(result)) == 50, "Invalid cohort")
    return result


def filesystem_probe(directory):
    """Isolated fixture only; roundtrip is not proof of Drive crash durability."""
    probe = Path(directory) / ("fixture-" + uuid4().hex)
    probe.mkdir(parents=True, exist_ok=False)
    path = probe / "checkpoint.json"
    _atomic_json(path, dict(fixture=True, generation=1))
    _atomic_json(path, dict(fixture=True, generation=2))
    store = CorpusStorage(probe)
    _require(store._read(path) == dict(fixture=True, generation=2), "Filesystem JSON probe failed")
    source = probe / "source"
    source.mkdir()
    payload = b"% OFFLINE FILESYSTEM FIXTURE ONLY\r\n"
    with (source / "main.tex").open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    files, hashes, size = store._snapshot(source)
    _require(files == ["main.tex"] and size == len(payload)
             and hashes["main.tex"] == hashlib.sha256(payload).hexdigest(), "Filesystem checksum probe failed")
    return dict(verified=True, fixture=True, probe_path=str(probe))


def resource_totals(audits, *, cold=False):
    """Consume validated per-attempt observations once, never cumulative reports."""
    observations, expected, seen, issues = [], [], set(), []
    for audit in audits:
        history = audit.get("history") or {}
        attempts = history.get("attempts", [])
        wanted = attempts[:1] if cold else attempts
        ids = {a["attempt_id"] for a in wanted}
        expected.extend((audit["batch_id"], a["attempt_id"]) for a in wanted)
        for row in (audit.get("benchmark_metrics") or {}).get("attempts", []):
            if row["attempt_id"] not in ids:
                continue
            key = audit["batch_id"], row["attempt_id"]
            if key in seen:
                issues.append("duplicate_resource_observation")
                continue
            seen.add(key)
            observations.append(row)
    issues += ["missing_resource_observation"] if set(expected) - seen else []
    resources = [row["resources"] for row in observations]
    complete = bool(expected) and not issues and len(seen) == len(expected)
    ram = [r["ram"] for r in resources]
    disk = [r["disk"] for r in resources]
    good_ram = (complete and all(r["status"] == "collected" for r in ram)
                and len({(r["source"], r["scope"]) for r in ram}) == 1)
    good_disk = (complete and all(d["status"] == "collected" for d in disk)
                 and len({tuple(sorted(d["roots"])) for d in disk}) == 1)
    by_key = {(a["batch_id"], row["attempt_id"]): row["resources"]
              for a in audits for row in (a.get("benchmark_metrics") or {}).get("attempts", [])}
    last = by_key.get(expected[-1]) if expected else None
    samples = sum(r["sample_count"] for r in ram)
    profiles = {_digest(row.get("environment", {})) for row in observations}
    return dict(wall_seconds=sum(r["wall_seconds"] for r in resources) if complete else None,
        known_wall_seconds=sum(r["wall_seconds"] for r in resources),
        peak_ram_bytes=max((r["sampled_peak_bytes"] for r in ram), default=None) if good_ram else None,
        average_ram_bytes=sum(r["sum_sample_bytes"] for r in ram) / samples if good_ram and samples else None,
        ram_samples=samples, peak_disk_bytes=max((d["sampled_peak_bytes"] for d in disk), default=None) if good_disk else None,
        final_corpus_bytes=last["disk"]["final_corpus_bytes"] if last else None,
        resources_complete=complete and good_ram and good_disk,
        observation_count=len(observations), environment_count=len(profiles), issues=issues)


class Comparison:
    def __init__(self, root, scratch, commit, *, seed=10, s2_config=None, secret_loader=None):
        self.root, self.scratch = Path(root).resolve(), Path(scratch).resolve()
        repo = Path(__file__).resolve().parents[1]
        _require(not any(a.is_relative_to(b) or b.is_relative_to(a)
                         for a, b in ((self.root, repo), (self.scratch, repo), (self.root, self.scratch))),
                 "Experiment/scratch must be disjoint and outside the Git repository")
        self.commit, self.pairs, self.secret_loader = commit, select_cohort(seed), secret_loader
        self.s2 = s2_integration.configuration(DEFAULT_S2 if s2_config is None else s2_config)
        _require(self.s2["max_attempts"] == 1, "Comparison uses S2 max_attempts=1 for immediate HTTP stops")
        self._check_code()
        definition = dict(schema_version=1, commit=commit, selection=[list(p) for p in self.pairs],
            selection_fingerprint=_digest(self.pairs), seed=seed, s2_config=self.s2,
            versions=None, revision_history=True, timeout=30.0, max_attempts=1,
            arxiv_min_interval=arxiv_client.MIN_INTERVAL, user_agent=arxiv_client.USER_AGENT,
            root=str(self.root), scratch=str(self.scratch), secret_name="SEMANTIC_SCHOLAR_API_KEY")
        path = self.root / "experiment.json"
        if path.exists():
            _require(_read(path) == definition, "Experiment configuration changed; use a new experiment ID")
        else:
            _require(not self.root.exists() or not any(self.root.iterdir()), "Unknown existing experiment directory")
            self.root.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, definition)
        self.definition = definition

    def _check_code(self):
        state = code_state()
        _require(not state["dirty"] and state["commit"] == self.commit, "Code must be clean and match the pinned commit")

    @contextmanager
    def _exclusive(self):
        _require(_LOCK.acquire(blocking=False), "Another action is running; do not start concurrent writers")
        try:
            yield
        finally:
            _LOCK.release()

    def locations(self, arm):
        _require(arm in PHASES, "Unknown strategy")
        base = self.root / arm
        return dict(corpus=base / "corpus", logs=base / "logs", reports=base / "reports",
                    scratch=self.scratch / arm)

    def _audits(self, arm):
        paths = self.locations(arm)
        return [benchmark_runner.aggregate_batch(paths["corpus"], batch_id=phase, log_root=paths["logs"])
                for phase in PHASES[arm] if (paths["logs"] / phase / "batch.json").exists()]

    def _receipts(self):
        return [_read(p) for p in sorted((self.root / "preflights").glob("*.json"))]

    def _admit(self, reviewed, *, require_preflight):
        self._check_code()
        _require(_read(self.root / "experiment.json") == self.definition
                 and _digest(self.pairs) == self.definition["selection_fingerprint"],
                 "Experiment definition/selection changed; preserve evidence and start a new experiment")
        receipts = self._receipts()
        stopped = any(r["http_status"] in (403, 429) for r in receipts)
        for arm in PHASES:
            for audit in self._audits(arm):
                _require(audit["status"] == "verified", "Checkpoint/log integrity failed; preserve evidence and audit")
                stopped |= any(a["status"] == "stopped" for a in audit["history"]["attempts"])
        _require(not stopped or reviewed is True, "HTTP policy stop recorded; review cooldown/credentials before another network action")
        if require_preflight:
            latest = max(receipts, key=lambda r: r["recorded_at"]) if receipts else None
            _require(latest is not None and latest["ok"], "Run a successful S2 preflight before the 50-ID strategies")

    def _credentials(self):
        _require(callable(self.secret_loader), "A Colab Secrets loader is required")
        try:
            key = self.secret_loader()
            _require(isinstance(key, str) and key and key.isascii()
                     and all(33 <= ord(c) <= 126 for c in key), "Missing or malformed API key")
        except Exception:
            raise ComparisonError("Cannot load a valid Semantic Scholar key from Colab Secrets") from None
        os.environ["SEMANTIC_SCHOLAR_API_KEY"] = key

    def preflight(self, *, reviewed=False):
        with self._exclusive():
            self._admit(reviewed, require_preflight=False)
            self._credentials()
            time.sleep(max(arxiv_client.MIN_INTERVAL, self.s2["min_interval"]))
            collector = http_metrics.HttpMetrics()
            try:
                with http_metrics.collect(collector):
                    result = semantic_scholar.fetch_metadata([p[1] for p in self.pairs[:4]],
                        batch_size=4, limiter=semantic_scholar.RateLimiter(self.s2["min_interval"]),
                        timeout=self.s2["timeout"], max_attempts=1, require_arxiv_identity=True,
                        max_response_bytes=self.s2["max_response_bytes"])
                ok = (len(result) == 4 and all(r["arxiv_id"] == p[1] and r["status"] in {"found", "not_found"}
                      and r["http_status"] == 200 for r, p in zip(result, self.pairs[:4])))
                status = next((r["http_status"] for r in result if r["http_status"] in (403, 429)),
                              result[0]["http_status"] if result else None)
            except Exception:
                result, ok, status = [], False, None
            receipt = dict(ok=ok, http_status=status, recorded_at=datetime.now(timezone.utc).isoformat(),
                ids=[p[1] for p in self.pairs[:4]], statuses=[r["status"] for r in result],
                http_metrics=collector.snapshot(complete=True), scope="one metadata POST; no references; no corpus cache")
            directory = self.root / "preflights"
            directory.mkdir(exist_ok=True)
            _atomic_json(directory / (uuid4().hex + ".json"), receipt)
            return receipt

    def _integrity(self, arm, *, s2):
        audits = self._audits(arm)
        if not audits or audits[-1]["status"] != "verified":
            return dict(ready=False, signature=None, cacheable=False)
        store = CorpusStorage(self.locations(arm)["corpus"])
        signatures, ready, cacheable = [], True, True
        try:
            for pair in self.pairs:
                m, f, refs = store.read_metadata(*pair), store.read_manifest(*pair), store.read_references(*pair)
                if f["discovery_status"] == "not_found":
                    signatures.append([*pair, "not_found"])
                    continue
                dates = arxiv_history._cached(m) or arxiv_history._single_version_cached(m)
                sources = (f["discovery_status"] == "found" and m["version_status"] == "resolved"
                           and bool(m["versions"]) and all(store.should_skip_version(*pair, v) for v in f["versions"]))
                ready &= bool(dates and sources)
                provider = s2_integration.metadata_cached(m) if s2 else False
                if s2:
                    ready &= bool(provider and refs["status"] == "complete")
                cacheable &= bool(dates and sources and (not s2 or provider and refs["status"] == "complete"))
                signatures.append([*pair, {k: m[k] for k in ("title", "authors", "submission_date", "revised_dates", "venue")},
                    {v: dict(status=r["status"], checksums=r["file_checksums"]) for v, r in f["versions"].items()},
                    refs["status"], _digest(refs["items"])])
        except (StorageError, OSError, KeyError, TypeError, ValueError):
            return dict(ready=False, signature=None, cacheable=False)
        return dict(ready=ready, signature=_digest(signatures) if ready else None, cacheable=cacheable)

    def _hashes(self, arm, *, all_json=False):
        corpus = self.locations(arm)["corpus"]
        result = {}
        for path in corpus.rglob("*"):
            if path.is_file() and (path.suffix.lower() in {".tex", ".bib"}
                                  or all_json and path.name in {"metadata.json", "references.json"}):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(65536), b""):
                        digest.update(block)
                result[str(path.relative_to(corpus))] = digest.hexdigest()
        return result

    def _phase(self, arm, phase, *, resume):
        paths = self.locations(arm)
        paths["scratch"].mkdir(parents=True, exist_ok=True)
        notes = self.root / "sessions"
        notes.mkdir(exist_ok=True)
        experiment = phase + "-" + uuid4().hex
        _atomic_json(notes / (experiment + ".json"), dict(arm=arm, phase=phase, resume=resume,
            recorded_at=datetime.now(timezone.utc).isoformat(), runtime=runtime_profile(),
            commit=self.commit, experiment_id=experiment, accelerator="None (operator confirmed)"))
        time.sleep(max(arxiv_client.MIN_INTERVAL, self.s2["min_interval"]))
        old_temp, old_env = tempfile.tempdir, os.environ.get("TMPDIR")
        try:
            tempfile.tempdir, os.environ["TMPDIR"] = str(paths["scratch"]), str(paths["scratch"])
            report = benchmark_runner.run_benchmark(self.pairs, paths["corpus"], batch_id=phase,
                max_ids=50, log_root=paths["logs"], report_root=paths["reports"], experiment_id=experiment,
                resume=resume, versions=None, revision_history=True, timeout=30, max_attempts=1,
                s2_config=self.s2 if phase != "arxiv" else None, sample_interval=0.1,
                disk_interval=30, disk_roots=[paths["corpus"], paths["scratch"]])
        finally:
            tempfile.tempdir = old_temp
            if old_env is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old_env
        return dict(phase=phase, **{k: report[k] for k in (
            "execution_status", "batch_status", "stop_reason", "report_path", "attempt_ids")})

    def run(self, arm, *, resume=False, reviewed=False):
        _require(arm in PHASES and type(resume) is bool, "Unknown strategy/action")
        with self._exclusive():
            self._admit(reviewed, require_preflight=True)
            directory = self.root / "operations"
            directory.mkdir(exist_ok=True)
            path = directory / (uuid4().hex + ".json")
            operation = dict(arm=arm, resume=resume, status="running", wall_seconds=None,
                started_at=datetime.now(timezone.utc).isoformat(), runtime=runtime_profile(),
                scope="admitted button action: credentials, boundary waits, crawler callbacks, phase gates, audits; excludes admission and operation-log writes")
            _atomic_json(path, operation)
            started = time.perf_counter()
            try:
                outcomes = self._run_locked(arm, resume=resume, reviewed=reviewed)
                operation["status"] = ("stopped" if any(o["batch_status"] == "stopped" for o in outcomes)
                    else "finished" if self._integrity(arm, s2=True)["ready"] else "partial")
                return outcomes
            except BaseException as exc:
                operation["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
                raise
            finally:
                operation.update(wall_seconds=max(0.0, time.perf_counter()-started),
                                 ended_at=datetime.now(timezone.utc).isoformat())
                _atomic_json(path, operation)

    def _run_locked(self, arm, *, resume=False, reviewed=False):
        _require(arm in PHASES and type(resume) is bool, "Unknown strategy/action")
        paths = self.locations(arm)
        audits = self._audits(arm)
        if not resume:
            _require(not any(p.exists() and any(p.iterdir()) for p in paths.values()),
                     "First run requires a fresh strategy directory; use Resume for existing work")
        else:
            _require(bool(audits), "No strategy batch exists to Resume")
        self._credentials()
        cacheable = self._integrity(arm, s2=True)["cacheable"] if resume else False
        protected = self._hashes(arm, all_json=cacheable) if resume else {}
        outcomes = []
        phases = PHASES[arm]
        for phase in phases:
            exists = (paths["logs"] / phase / "batch.json").exists()
            if arm == "B" and phase == "arxiv" and resume and self._integrity(arm, s2=False)["ready"]:
                continue
            try:
                outcome = self._phase(arm, phase, resume=exists)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise ComparisonError("Strategy failed; inspect durable batch/benchmark reports; details withheld") from None
            outcomes.append(outcome)
            if outcome["execution_status"] != "returned" or outcome["batch_status"] != "finished":
                break  # Especially 403/429: no next phase, no other strategy.
            if arm == "B" and phase == "arxiv" and not self._integrity(arm, s2=False)["ready"]:
                break
        if resume:
            after = self._hashes(arm, all_json=cacheable)
            _require(all(after.get(k) == v for k, v in protected.items()), "Resume changed existing data; audit before more requests")
            if cacheable:
                _require(self._integrity(arm, s2=True)["ready"], "Resume integrity is no longer verified")
                for outcome in outcomes:
                    attempt = _read(paths["logs"] / outcome["phase"]
                                    / "attempts" / (outcome["attempt_ids"][0] + ".json"))
                    _require(attempt["http_metrics"]["status"] == "collected"
                             and attempt["http_metrics"]["total"]["attempts"] == 0,
                             "Verified Resume unexpectedly used HTTP; inspect its attempt log")
        return outcomes

    def _row(self, arm):
        audits = self._audits(arm)
        ready = self._integrity(arm, s2=True)
        histories = [a["history"] for a in audits if a.get("history")]
        attempts = [t for h in histories for t in h["attempts"]]
        cold = resource_totals(audits, cold=True)
        total = resource_totals(audits)
        http = [h.get("http_metrics", {}) for h in histories]
        complete_http = bool(http) and all(not h.get(k) for h in http for k in (
            "missing_attempt_ids", "invalid_attempt_ids", "partial_attempt_ids"))
        stop_count = sum(a["status"] == "stopped" for a in attempts)
        timings = [h["timings"] for h in histories]
        data = audits[-1].get("data_coverage") if audits else None
        coverage = data or {}
        publications = audits[-1].get("publication_complete") if audits else None
        first_ids = {h["attempts"][0]["attempt_id"] for h in histories if h["attempts"]}
        cold_errors = any(e["attempt_id"] in first_ids for h in histories for e in h["error_history"])
        phases_done = len(audits) == len(PHASES[arm])
        latest_finished = phases_done and all(h["attempts"][-1]["status"] == "finished" for h in histories)
        first_finished = phases_done and all(h["attempts"][0]["status"] == "finished" for h in histories)
        operations = sorted((_read(p) for p in (self.root / "operations").glob("*.json")),
                            key=lambda o: o["started_at"])
        operations = [o for o in operations if o["arm"] == arm]
        first_operation = operations[0] if operations and not operations[0]["resume"] else None
        operation_complete = bool(operations) and all(o["wall_seconds"] is not None for o in operations)
        return dict(strategy=arm, phases_recorded=len(audits), attempts=len(attempts),
            selected_ids=50, found_papers=coverage.get("found_papers"), not_found_papers=coverage.get("not_found_papers"),
            valid_tex_versions=coverage.get("valid_tex_versions"), no_tex_versions=coverage.get("no_tex_versions"),
            references_complete_papers=coverage.get("references_complete_papers"),
            publication_complete_papers=(publications or {}).get("papers"),
            stopped_attempts=stop_count, integrity_ready=ready["ready"], data_signature=ready["signature"],
            cold_eligible=bool(ready["ready"] and latest_finished and first_finished and not cold_errors
                               and not stop_count and cold["resources_complete"] and complete_http
                               and coverage.get("found_papers", 0) > 0
                               and first_operation and first_operation["status"] == "finished"),
            cold_end_to_end_seconds=first_operation["wall_seconds"] if first_operation else None,
            all_actions_end_to_end_seconds=sum(o["wall_seconds"] for o in operations) if operation_complete else None,
            cold_wall_seconds=cold["wall_seconds"] if phases_done else None,
            cold_peak_ram_bytes=cold["peak_ram_bytes"] if phases_done else None,
            cold_average_ram_bytes=cold["average_ram_bytes"] if phases_done else None,
            cold_peak_disk_bytes=cold["peak_disk_bytes"] if phases_done else None,
            active_wall_seconds=total["wall_seconds"] if phases_done else None,
            known_active_wall_seconds=total["known_wall_seconds"],
            peak_ram_bytes=total["peak_ram_bytes"], average_ram_bytes=total["average_ram_bytes"],
            peak_disk_bytes=total["peak_disk_bytes"], final_corpus_bytes=total["final_corpus_bytes"],
            discovery_seconds=sum(t["discovery_wall_seconds"] for t in timings)
                if timings and all(t["discovery_wall_seconds"] is not None for t in timings) else None,
            average_paper_seconds=sum(t["known_paper_wall_seconds"] for t in timings)/50
                if phases_done and all(t["average_paper_status"] == "collected" for t in timings) else None,
            http_requests=sum(h["known"]["attempts"] for h in http) if complete_http else None,
            http_429=sum(h["known"]["status_counts"].get("429", 0) for h in http) if complete_http else None,
            http_403=sum(h["known"]["status_counts"].get("403", 0) for h in http) if complete_http else None,
            data_coverage=data, publication_complete=audits[-1].get("publication_complete") if audits else None,
            measurement_issues=total["issues"], resource_status="collected" if total["resources_complete"] else "incomplete")

    def compare(self):
        with self._exclusive():
            rows = [self._row(arm) for arm in PHASES]
            sessions = [_read(p) for p in (self.root / "sessions").glob("*.json")]
            runtime_count = len({_digest(s["runtime"]) for s in sessions})
            eligible = all(r["cold_eligible"] for r in rows) and rows[0]["data_signature"] == rows[1]["data_signature"] and runtime_count == 1
            reasons = [r["strategy"] + ": first pass, coverage or measurements incomplete/stopped"
                       for r in rows if not r["cold_eligible"]]
            if rows[0]["data_signature"] != rows[1]["data_signature"]:
                reasons.append("Verified output fingerprints differ")
            if runtime_count != 1:
                reasons.append("Runtime profile is missing or changed")
            winner = None
            if eligible:
                delta = rows[0]["cold_end_to_end_seconds"] - rows[1]["cold_end_to_end_seconds"]
                winner = "A" if delta < 0 else "B" if delta > 0 else "tie"
            report = dict(definition=self.definition, rows=rows, runtime_count=runtime_count,
                comparison_reasons=reasons,
                speed_comparison_eligible=eligible, faster_observed_arm=winner,
                conclusion="Exploratory cold-crawl observation, not a 20000-ID prediction" if eligible
                else "Insufficient evidence: stopped/incomplete/different coverage, metrics or runtime; no speed winner",
                scope="End-to-end=admitted button intervals incl boundary waits, phase gates/audits/reporting; callback wall is separate. No session gaps. Resource peaks sample crawler callbacks, not every wrapper interval. RSS includes notebook kernel. Disk=per-strategy corpus+scratch logical bytes.")
            destination = self.root / "comparisons" / uuid4().hex
            destination.mkdir(parents=True, exist_ok=False)
            _atomic_json(destination / "comparison.json", report)
            flat = [{k: v for k, v in row.items() if not isinstance(v, (dict, list))} for row in rows]
            with (destination / "comparison.csv").open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
                writer.writeheader()
                writer.writerows(flat)
            return report, destination


def comparison_html(report):
    columns = [("strategy", "Strategy"), ("attempts", "Attempts"),
        ("cold_end_to_end_seconds", "First-pass end-to-end (s)"),
        ("cold_wall_seconds", "First-pass crawler callbacks (s)"),
        ("all_actions_end_to_end_seconds", "All actions end-to-end (s)"),
        ("active_wall_seconds", "All crawler attempts (s)"),
        ("cold_peak_ram_bytes", "First-pass sampled RAM peak (bytes)"),
        ("cold_average_ram_bytes", "First-pass RSS sample mean (bytes)"),
        ("cold_peak_disk_bytes", "First-pass sampled disk peak (bytes)"), ("final_corpus_bytes", "Final corpus (bytes)"),
        ("http_requests", "HTTP attempts"), ("http_429", "HTTP 429"),
        ("found_papers", "Found"), ("not_found_papers", "Not found"),
        ("valid_tex_versions", "Valid TeX versions"), ("references_complete_papers", "References complete"),
        ("publication_complete_papers", "Publication complete"), ("cold_eligible", "Cold eligible")]
    def value(v):
        return "not_collected" if v is None else html.escape(f"{v:.3f}" if type(v) is float else str(v))
    header = "".join("<th>" + html.escape(title) + "</th>" for _, title in columns)
    rows = "".join("<tr>" + "".join("<td>" + value(row.get(key)) + "</td>" for key, _ in columns)
                   + "</tr>" for row in report["rows"])
    return ('<div style="overflow-x:auto"><table><tr>' + header + '</tr>' + rows + '</table></div><p>'
            + html.escape(report["conclusion"]) + '</p><p>'
            + html.escape('; '.join(report.get("comparison_reasons", []))) + '</p>')


def build_controls(session, widgets, display, render):
    """Register callbacks only; construction never loads keys or sends HTTP."""
    cpu = widgets.Checkbox(description="CPU-only / accelerator None")
    writer = widgets.Checkbox(description="Only writer; old runtimes stopped")
    reviewed = widgets.Checkbox(description="Policy stop reviewed; cooldown observed")
    output = widgets.Output()
    actions = [("preflight", "1. S2 preflight (4 IDs)"), ("A", "2. Run A: Combined 50"),
               ("B", "3. Run B: Two-phase 50"), ("resume_A", "Resume A"),
               ("resume_B", "Resume B"), ("compare", "Audit + compare (no HTTP)")]
    buttons = {name: widgets.Button(description=label, layout=widgets.Layout(width="auto")) for name, label in actions}
    def callback(action):
        def clicked(_):
            with output:
                if not cpu.value or not writer.value:
                    print("Confirm CPU-only and single writer first.")
                    return
                review = reviewed.value
                reviewed.value = False
                for button in buttons.values():
                    button.disabled = True
                try:
                    if action == "preflight":
                        result = session.preflight(reviewed=review)
                        print(json.dumps(result, indent=2))
                    elif action == "compare":
                        report, destination = session.compare()
                        render(comparison_html(report))
                        print("JSON/CSV reports:", destination)
                    else:
                        arm = action[-1] if action.startswith("resume_") else action
                        result = session.run(arm, resume=action.startswith("resume_"), reviewed=review)
                        print(json.dumps(result, indent=2))
                        print("Review stopped/failed/partial results before any further action.")
                except ComparisonError as exc:
                    print(str(exc))
                except BaseException as exc:
                    print("Action stopped:", type(exc).__name__, "(details withheld; inspect durable logs)")
                finally:
                    for button in buttons.values():
                        button.disabled = False
        return clicked
    for name, button in buttons.items():
        button.on_click(callback(name))
    display(widgets.VBox([cpu, writer, reviewed, widgets.HBox(list(buttons.values())), output]))
    return dict(buttons=buttons, cpu=cpu, writer=writer, reviewed=reviewed, output=output)

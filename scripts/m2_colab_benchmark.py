"""Independent CPU-only M2 benchmark; input is read-only, reports are observed data."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import io
from functools import lru_cache
import re
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import time
import traceback
import uuid

from scripts import m2_colab_export as export
from scripts import m2_colab_recovery as recovery
from src.m2 import runner

PARSER_SOURCE_COMMIT = "e6869ff26e36b7122128b7ff165a2bd3da9003b4"
PARSER_FINGERPRINT = "4e88c7fb0d3463c4c6815b5afff3f64066815068dd4c34db1e92528966d60b70"
PAPER_COLUMNS = ("cohort", "arxiv_id", "version", "status", "elements_count", "hierarchy_count",
                 "bibtex_count", "runtime_seconds", "error_type", "parser_sha", "audit_outcome", "runtime_scope")
ERROR_COLUMNS = ("cohort", "arxiv_id", "version", "error_category", "error_type", "error_message",
                 "source_file", "exception_class", "traceback_path", "status", "parser_sha")
CATEGORIES = ("input_missing", "archive_corrupt", "checksum_mismatch", "no_tex_source",
              "latex_parse_error", "bibliography_error", "citation_mapping_error", "version_mismatch",
              "audit_invariant_error", "unexpected_exception", "unknown")


def now():
    return datetime.now(timezone.utc).isoformat()


class BenchmarkFailure(ValueError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def verify_runtime(config):
    repo = Path(config["repo"]).resolve()
    git = ["git", "-C", str(repo)]
    head = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    if (head != config["release_commit"] or len(head) != 40
            or subprocess.check_output(git + ["status", "--porcelain"], text=True).strip()):
        raise ValueError("Benchmark requires the exact clean release checkout; restart Setup")
    if Path(runner.__file__).resolve() != repo / "src/m2/runner.py":
        raise ValueError("Imported parser belongs to another checkout; restart Python")
    if recovery.fingerprint(repo) != PARSER_FINGERPRINT:
        raise ValueError("Parser differs from the verified nocite release")
    if os.environ.get("COLAB_GPU") == "1" or os.environ.get("COLAB_TPU_ADDR"):
        raise ValueError("Select a CPU-only Colab runtime")
    import pyarrow
    return dict(parser_sha=head, parser_source_commit=PARSER_SOURCE_COMMIT,
                dependencies=dict(pyarrow=pyarrow.__version__),
                logical_cpus=os.cpu_count(), machine=platform.machine(),
                parser_fingerprint=PARSER_FINGERPRINT, python=platform.python_version(),
                platform=platform.platform(), environment=runner.measurement_environment())


def paths(config):
    start, end = config["start_cohort"], config["end_cohort"]
    if any(type(n) is not int for n in (start, end)) or not 1 <= start <= end <= 200:
        raise ValueError("START_COHORT/END_COHORT must satisfy 1 <= start <= end <= 200")
    roots = {k: Path(config[k]).resolve() for k in ("repo", "input_drive_dir", "output_drive_dir", "scratch")}
    for key, root in roots.items():
        if any(p.is_symlink() for p in [Path(config[key]), *Path(config[key]).parents]):
            raise ValueError("Symlink root is not accepted; use the mounted Drive path")
    values = list(roots.values())
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(values) for b in values[i + 1:]):
        raise ValueError("Input, output, scratch and checkout must be disjoint")
    if not roots["input_drive_dir"].is_dir():
        raise ValueError(f"INPUT_DRIVE_DIR does not exist: {roots['input_drive_dir']}")
    for parent in [roots["output_drive_dir"], *roots["output_drive_dir"].parents]:
        if (parent / "campaign.json").exists() or (parent / "recovery.json").exists():
            raise ValueError("Benchmark output must not be inside an export/recovery campaign")
    identity = dict(format_version=1, start_cohort=start, end_cohort=end,
                    input_drive_dir=str(roots["input_drive_dir"]), release_commit=config["release_commit"],
                    parser_fingerprint=PARSER_FINGERPRINT, helper_sha256=export.sha(__file__), timeout_seconds=300)
    root = roots["output_drive_dir"] / ("benchmark-" + runner.digest(export.encoded(identity))[:20])
    if config.get("benchmark_campaign"):
        original = Path(config["benchmark_campaign"])
        if any(p.is_symlink() for p in [original, *original.parents]):
            raise ValueError("Symlink legacy campaign path")
        root = original.resolve()
        recovery.contained(root, roots["output_drive_dir"])
        if root.parent != roots["output_drive_dir"]:
            raise ValueError("BENCHMARK_CAMPAIGN must be directly inside OUTPUT_DRIVE_DIR")
        saved = export.read(root / "benchmark.json")
        previous = saved["identity"]
        if root.name != "benchmark-" + runner.digest(export.encoded(previous))[:20]:
            raise ValueError("Legacy campaign identity hash differs")
        if set(previous) != set(identity) or any(previous[k] != identity[k] for k in identity
                if k not in {"release_commit", "helper_sha256"}):
            raise ValueError("Legacy campaign scope/input/parser/timeout differs; preserve old campaign")
        validate_parser_origin(str(roots["repo"]), previous["release_commit"], config["release_commit"])
        blob = subprocess.check_output(["git", "-C", str(roots["repo"]), "show",
                                        previous["release_commit"] + ":scripts/m2_colab_benchmark.py"])
        if runner.digest(blob) != previous["helper_sha256"]:
            raise ValueError("Legacy helper checksum differs")
        expected = dict(parser_sha=previous["release_commit"], parser_source_commit=PARSER_SOURCE_COMMIT,
                        parser_fingerprint=PARSER_FINGERPRINT)
        if saved["provenance"] != expected:
            raise ValueError("Legacy provenance differs")
        identity = previous
    if root.exists() and any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Symlink in benchmark output; preserve and review")
    return roots, root, identity


@lru_cache(maxsize=64)
def validate_parser_origin(repo, origin, release):
    """Prove historical execution SHA has the SAME parser, preserve that SHA."""
    if not all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{40}", s) for s in (origin, release)):
        raise ValueError("Invalid execution commit")
    git = ["git", "-C", repo]
    if subprocess.run(git + ["merge-base", "--is-ancestor", origin, release], capture_output=True).returncode:
        raise ValueError("Historical parser is not in release ancestry")
    files = subprocess.check_output(git + ["ls-tree", "-r", "--name-only", origin, "src/m2"], text=True).splitlines()
    data = b"".join(subprocess.check_output(git + ["show", f"{origin}:{name}"])
                    for name in sorted(files) if name.endswith(".py"))
    if runner.digest(data) != PARSER_FINGERPRINT:
        raise ValueError("Historical parser fingerprint differs; explicit migration required")


def batches(start, end, size=50):
    if type(size) is not int or not 1 <= size <= 200:
        raise ValueError("COHORTS_PER_BATCH must be an integer in 1..200")
    return [list(range(n, min(n + size, end + 1))) for n in range(start, end + 1, size)]


def input_triplet(roots, name):
    archive_root = roots["input_drive_dir"]
    if "triplet_index" not in roots:
        index = defaultdict(list)
        for path in archive_root.rglob("*.manifest.json"):
            index[path.name.removesuffix(".manifest.json")].append(path.parent)
        roots["triplet_index"] = index
    candidates = roots["triplet_index"].get(name, [])
    if len(candidates) > 1:
        raise BenchmarkFailure("input_missing", f"Ambiguous archive triplets for {name}: {candidates}")
    folder = candidates[0] if candidates else archive_root
    required = [folder / (name + suffix) for suffix in (".tar.gz", ".manifest.json", ".tar.gz.sha256")]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise BenchmarkFailure("input_missing", "Missing required inputs: " + ", ".join(missing))
    if any(p.is_symlink() for path in required for p in [path, *path.parents]):
        raise BenchmarkFailure("input_missing", "Symlink input archive path")
    return required


def input_inventory(roots, names):
    """Presence/uniqueness preflight. Full content hash is checked on consumption."""
    result = []
    for name in names:
        try:
            files = input_triplet(roots, name)
            result.append(dict(cohort=name, available=True, files=[str(p) for p in files]))
        except BenchmarkFailure as exc:
            result.append(dict(cohort=name, available=False, error=str(exc)))
    return result


def scope_for(config, number):
    members = export.read(Path(config["repo"]) / "config/group10_id_ranges.json")["members"]
    owner = members[(number - 1) // 50]["mssv"]
    return export.scope_for(dict(config, student_id=owner, start_cohort=number, end_cohort=number,
                                 batch_size=1, hf_repo_id="benchmark/not-uploaded"))


def input_for(config, roots, name):
    number = int(name[7:11]); scope = scope_for(config, number)
    required = input_triplet(roots, name)
    folder = required[0].parent
    # Verify hashes separately so classification does not guess from exception text.
    manifest = export.read(required[1])
    parts = required[2].read_text(encoding="utf-8-sig").strip().split()
    if (len(parts) != 2 or parts[1].lstrip("*") != required[0].name
            or parts[0] != manifest.get("archive_sha256") or export.sha(required[0]) != parts[0]):
        raise BenchmarkFailure("checksum_mismatch", f"Archive/manifest/sidecar SHA mismatch: {required[0]}")
    try:
        item = export.discover_item(name, scope, folder)
    except ValueError as exc:
        raise BenchmarkFailure("version_mismatch", str(exc)) from exc
    try:
        corpus, rows = export.materialize(name, item, folder, roots["scratch"], runner, scope)
    except (tarfile.TarError, EOFError) as exc:
        raise BenchmarkFailure("archive_corrupt", str(exc)) from exc
    item["archive_directory"] = str(folder)
    return corpus, rows, item


@contextmanager
def measure_versions():
    """Observe the unchanged executor; preserve its timeout, exceptions and results.

    The CLI is single-threaded. This is a scoped instrumentation hook, never a
    replacement parser. The worker function/arguments and budget are unchanged.
    """
    original = runner.VersionExecutor
    durations = defaultdict(float)
    class MeasuredExecutor:
        def __init__(self, seconds):
            self.executor = original(seconds)
        def __call__(self, version, function, arguments):
            started = time.perf_counter()
            try:
                return self.executor(version, function, arguments)
            finally:
                durations[arguments[0], version] += time.perf_counter() - started
    runner.VersionExecutor = MeasuredExecutor
    try:
        yield durations
    finally:
        runner.VersionExecutor = original


def error_category(record):
    if record["status"] == "empty_no_tex":
        return "no_tex_source"
    if record["status"] == "source_unavailable":
        return "input_missing"
    if record["status"] == "input_error":
        return "unknown"  # Native records do not preserve a typed cause.
    if record["status"] == "parse_error":
        if record.get("missing_citation_keys"):
            return "citation_mapping_error"
        return "latex_parse_error"  # Outcome, not a claim about the root cause.
    return None


def version_rows(name, attempt, audit_outcome, durations=None):
    """Counts are version-associated; union table totals live in cohort summaries."""
    durations = durations or {}
    result, errors = [], []
    for rec in attempt["versions_detail"]:
        category = error_category(rec)
        success = rec["status"] == "success"
        result.append(dict(cohort=name, arxiv_id=rec["arxiv_id"], version=rec["version"],
            status=rec["status"], elements_count=rec.get("element_count") if success else 0,
            hierarchy_count=rec.get("edge_count") if success else 0, bibtex_count=rec.get("bibliography_count") if success else None,
            runtime_seconds=durations.get((rec["arxiv_id"], rec["version"])), error_type=category,
            parser_sha=attempt["provenance"]["git_commit"], audit_outcome=audit_outcome,
            runtime_scope="prepare_render_worker_wall_including_spawn_this_attempt" if (rec["arxiv_id"], rec["version"]) in durations else "not_measured_no_worker_or_cached"))
        if category:
            errors.append(dict(cohort=name, arxiv_id=rec["arxiv_id"], version=rec["version"],
                error_category=category,
                # This is an observed message family, not a guessed root cause.
                error_type=((rec.get("error") or rec["status"]).partition(":")[0][:160]),
                error_message=rec.get("error") or rec["status"], source_file=rec.get("root"),
                exception_class=None, traceback_path=None, status=rec["status"],
                parser_sha=attempt["provenance"]["git_commit"]))
    return result, errors


def csv_bytes(rows, columns):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="raise")
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def evidence(root, attempt, rows):
    native = Path(attempt["run_root"])
    files = [native / "definition.json", native / "output_checksums.json"]
    files += [native / "papers" / d.name / "result.json" for d in sorted({r[3] for r in rows})]
    files += [Path(attempt["publication_root"]) / "data" / n / "part-00000.parquet" for n in export.TABLES]
    return {str(recovery.contained(p, root).relative_to(root)): export.sha(p) for p in files}


def saved_cohort(root, name, rows, item, identity):
    marker = root / "completed" / (name + ".json")
    if not marker.exists():
        return None
    envelope = export.read(marker); record = envelope["payload"]
    if runner.digest(export.encoded(record)) != envelope["sha256"] or record["input"] != item:
        raise BenchmarkFailure("audit_invariant_error", "Completion checksum/input differs; do not overwrite")
    for relative, expected in record["evidence"].items():
        if export.sha(recovery.contained(root / relative, root)) != expected:
            raise BenchmarkFailure("audit_invariant_error", f"Changed completion evidence: {relative}")
    attempt = export.read(recovery.contained(root / record["attempt_path"], root))
    expected = recovery.native_definition(item, rows, PARSER_FINGERPRINT)
    if export.read(Path(attempt["run_root"]) / "definition.json") != expected:
        raise BenchmarkFailure("audit_invariant_error", "Native checkpoint definition differs")
    origin = attempt["provenance"]["git_commit"]
    validate_parser_origin(str(Path(runner.__file__).resolve().parents[2]), origin, identity["parser_sha"])
    recovery.audit_attempt(attempt, rows, runner, expected_parser_commit=origin)
    return record


def summary(records, events, expected_names, root, identity):
    versions = [v for r in records for v in r.get("versions", [])]
    states = Counter(r["status"] for r in versions)
    grouped = defaultdict(list)
    for v in versions:
        grouped[v["cohort"], v["arxiv_id"]].append(v)
    papers_success = sum(all(v["status"] == "success" for v in group) for group in grouped.values())
    handled = sum(all(v["status"] in {"success", "empty_no_tex"} for v in group) for group in grouped.values())
    measured = sum(e["elapsed_seconds"] for e in events if e.get("elapsed_seconds") is not None)
    unfinished = sum(e.get("elapsed_seconds") is None for e in events)
    completed = [r["cohort"] for r in records if r.get("audit_outcome") == "verified"]
    count = len(versions)
    rate = lambda n, d: 100 * n / d if d else None
    certified = [v for r in records if r.get("audit_outcome") == "verified" for v in r.get("versions", [])]
    certified_states = Counter(v["status"] for v in certified)
    certified_papers = defaultdict(list)
    for v in certified:
        certified_papers[v["cohort"], v["arxiv_id"]].append(v)
    certified_count = len(certified)
    certified_eligible = certified_states["success"] + certified_states["parse_error"]
    eligible = states["success"] + states["parse_error"]
    errors = [e for r in records for e in r.get("errors", [])]
    return dict(run_id=root.name, timestamp=now(), provenance=identity,
        parser_shas=sorted({v["parser_sha"] for v in versions}),
        status="complete" if set(completed) == set(expected_names) else "partial",
        cohort_start=int(expected_names[0][7:11]), cohort_end=int(expected_names[-1][7:11]),
        cohorts_requested=len(expected_names), cohorts_completed_count=len(completed),
        rate_denominators=dict(version_rates=certified_count, eligible_success=certified_eligible, paper_rates=len(certified_papers)),
        rate_scope="audit-verified cohorts only; raw observed outcomes retained separately",
        verified_versions=certified_count, verified_statuses=dict(certified_states),
        corpus_range=expected_names, cohorts_completed=completed, cohorts_observed=len(records),
        requested_candidate_positions=100 * len(expected_names),
        measurement_environments=[dict(event_id=e["event_id"], **e.get("environment", {})) for e in events],
        input_coverage=[dict(cohort=r["cohort"], included_publications=len(r["input"]["included_ids"]),
                            omitted_ids=r["input"]["omitted_ids"]) for r in records if r.get("input")],
        benchmark_complete=set(completed) == set(expected_names), papers=len(grouped), versions=count,
        statuses=dict(states), successful_versions=states["success"], parse_error=states["parse_error"],
        empty_no_tex=states["empty_no_tex"], attempted_versions=count, eligible_versions=eligible,
        version_success_rate_percent=rate(certified_states["success"], certified_count),
        eligible_success_rate_percent=rate(certified_states["success"], certified_eligible),
        parse_error_rate_percent=rate(certified_states["parse_error"], certified_count),
        empty_no_tex_rate_percent=rate(certified_states["empty_no_tex"], certified_count),
        papers_any_version_parsed=sum(any(v["status"] == "success" for v in g) for g in grouped.values()),
        paper_any_success_rate_percent=rate(sum(any(v["status"] == "success" for v in g) for g in certified_papers.values()), len(certified_papers)),
        failure_rate_percent=rate(certified_count - certified_states["success"] - certified_states["empty_no_tex"], certified_count),
        missing_latex_rate_percent=rate(certified_states["empty_no_tex"], certified_count),
        paper_success_rate_percent=rate(sum(all(v["status"] == "success" for v in g) for g in certified_papers.values()), len(certified_papers)),
        papers_all_versions_parsed=papers_success, papers_all_versions_handled=handled,
        input_archive_checksum_errors=sum(e["error_category"] in {"input_missing", "archive_corrupt", "checksum_mismatch", "version_mismatch"} and not e.get("version") for e in errors),
        audit_errors=sum(e["error_category"] == "audit_invariant_error" for e in errors),
        tables={n: sum(r.get("tables", {}).get(n, 0) for r in records if r.get("audit_outcome") == "verified") for n in export.TABLES},
        measured_active_seconds=measured, interrupted_intervals_unmeasured=unfinished,
        throughput_versions_per_second=count / measured if measured and not unfinished else None,
        timing_scope="sum of measured cohort invocations: input verification/extraction, native read/parse/write, audit; excludes setup, session gaps, report rendering and valid-completion re-audits",
        per_version_timing="observed executor prepare/render worker wall including spawn, excludes cross-version merge, source validation and Parquet write; null for no worker or native cached version",
        table_scope="unique native union rows from audit-verified cohorts only; version-associated counts are not additive",
        unaudited_parse_outcomes=any(r.get("audit_outcome") != "verified" and r.get("versions") for r in records),
        error_categories=dict(Counter(e["error_category"] for e in errors)),
        error_types=dict(Counter(e["error_type"] for e in errors)))


def load_events(root):
    events = []
    for path in sorted((root / "events").glob("*/started.json")):
        start = export.read(path)
        finished = path.with_name("finished.json")
        events.append(export.read(finished) if finished.exists() else start)
    return sorted(events, key=lambda e: e["started_at"])


def write_reports(root, identity, names, verified_records=None, *, report_group=None):
    # Valid only within one invocation under the exclusive benchmark writer.
    # Completed evidence is immutable; do not reread all prior Parquet/paper
    # bytes after every cohort (quadratic Drive I/O). A new invocation starts
    # empty and verifies hashes again, including the standalone report action.
    verified_records = {} if verified_records is None else verified_records
    events = [e for e in load_events(root) if e["cohort"] in names]
    latest = {}
    for event in events:
        latest[event["cohort"]] = event.get("record", dict(cohort=event["cohort"], audit_outcome="interrupted", versions=[], errors=[]))
    # A runtime can die between publishing completion and finishing its timing
    # event. Recover the certified payload, but never invent the missing duration.
    for name in names:
        marker = root / "completed" / (name + ".json")
        if marker.exists() and (name not in latest or latest[name]["audit_outcome"] == "interrupted"):
            envelope = export.read(marker)
            if runner.digest(export.encoded(envelope["payload"])) == envelope["sha256"]:
                latest[name] = envelope["payload"]
    records = json.loads(json.dumps([latest[n] for n in names if n in latest]))
    # Reports are not allowed to certify changed evidence merely because an old
    # event said verified. Preserve the parse outcomes, fail their current audit.
    for record in records:
        if record["audit_outcome"] != "verified":
            continue
        try:
            envelope = export.read(root / "completed" / (record["cohort"] + ".json"))
            if runner.digest(export.encoded(envelope["payload"])) != envelope["sha256"]:
                raise ValueError("Completion marker checksum mismatch")
            if envelope["payload"] != record:
                raise ValueError("Event differs from certified completion marker")
            if verified_records.get(record["cohort"]) != envelope["sha256"]:
                for relative, expected in record["evidence"].items():
                    if export.sha(recovery.contained(root / relative, root)) != expected:
                        raise ValueError(f"Changed completion evidence: {relative}")
                verified_records[record["cohort"]] = envelope["sha256"]
        except (ValueError, KeyError, OSError) as exc:
            record["audit_outcome"] = "failed"
            diagnostic = root / "diagnostics" / (uuid.uuid4().hex + ".txt")
            export.immutable(diagnostic, traceback.format_exc().encode("utf8"))
            record["versions"] = [dict(v, audit_outcome="failed") for v in record["versions"]]
            record["errors"] = list(record["errors"]) + [dict(cohort=record["cohort"], arxiv_id=None,
                version=None, error_category="audit_invariant_error", error_type=type(exc).__name__,
                error_message=str(exc), source_file=None, exception_class=type(exc).__name__,
                traceback_path=str(diagnostic), status="stopped", parser_sha=identity["parser_sha"])]
    for record in records:
        record["measured_active_seconds"] = sum(e.get("elapsed_seconds") or 0 for e in events if e["cohort"] == record["cohort"])
    totals = summary(records, events, names, root, identity)
    group = root / "reports" / (report_group or f"aggregate_{names[0][7:11]}-{names[-1][7:11]}")
    stable = dict(totals); stable.pop("timestamp")
    signature = runner.digest(export.encoded(dict(summary=stable, records=records)))
    pointer = group / "LATEST.json"
    if pointer.exists():
        prior = export.read(pointer)
        destination = recovery.contained(Path(prior["report_directory"]), group)
        ready = verify_snapshot(destination)
        if ready["signature"] == signature:
            publish_pointer(root / "LATEST.json", prior)
            return dict(summary=export.read(destination / "benchmark_summary.json"), report_directory=str(destination))
    # An interrupted staging folder is retained as evidence, never treated as ready.
    destination = group / (".building-" + uuid.uuid4().hex)
    destination.mkdir(parents=True)
    export.immutable(destination / "benchmark_summary.json", export.encoded(totals))
    versions = [v for r in records for v in r.get("versions", [])]
    errors = [e for r in records for e in r.get("errors", [])]
    export.immutable(destination / "paper_results.csv", csv_bytes(versions, PAPER_COLUMNS))
    export.immutable(destination / "m2_errors.csv", csv_bytes(errors, ERROR_COLUMNS))
    cohort_rows = []
    for r in records:
        states = Counter(v["status"] for v in r.get("versions", []))
        cohort_rows.append(dict(cohort=r["cohort"], audit_outcome=r["audit_outcome"], versions=sum(states.values()),
            success=states["success"], parse_error=states["parse_error"], empty_no_tex=states["empty_no_tex"],
            source_unavailable=states["source_unavailable"], input_error=states["input_error"],
            measured_active_seconds=r["measured_active_seconds"],
            elements=r.get("tables", {}).get("elements") if r["audit_outcome"] == "verified" else None,
            hierarchy=r.get("tables", {}).get("hierarchy") if r["audit_outcome"] == "verified" else None,
            bibtex=r.get("tables", {}).get("bibtex") if r["audit_outcome"] == "verified" else None))
    rate_columns = ("papers", "verified_versions", "version_success_rate_percent", "paper_success_rate_percent",
                    "paper_any_success_rate_percent", "parse_error_rate_percent", "empty_no_tex_rate_percent", "audit_errors")
    for row, record in zip(cohort_rows, records):
        scoped = summary([record], [e for e in events if e["cohort"] == record["cohort"]], [record["cohort"]], root, identity)
        row.update({k: scoped[k] for k in rate_columns})
        row.update(scope="cohort", status=scoped["status"], parser_sha=",".join(scoped["parser_shas"]),
                   cohort_start=scoped["cohort_start"], cohort_end=scoped["cohort_end"],
                   cohorts_completed=scoped["cohorts_completed_count"])
    cohort_rows.append(dict(cohort="ALL_REQUESTED", scope="aggregate", status=totals["status"],
        audit_outcome="verified" if totals["benchmark_complete"] else "partial", versions=totals["versions"],
        success=totals["successful_versions"], parse_error=totals["parse_error"], empty_no_tex=totals["empty_no_tex"],
        source_unavailable=totals["statuses"].get("source_unavailable", 0), input_error=totals["statuses"].get("input_error", 0),
        measured_active_seconds=totals["measured_active_seconds"], **totals["tables"],
        **{k: totals[k] for k in rate_columns}, parser_sha=",".join(totals["parser_shas"]),
        cohort_start=totals["cohort_start"], cohort_end=totals["cohort_end"], cohorts_completed=totals["cohorts_completed_count"]))
    columns = ("cohort", "scope", "status", "parser_sha", "cohort_start", "cohort_end", "cohorts_completed", "audit_outcome",
               "versions", "success", "parse_error", "empty_no_tex", "source_unavailable", "input_error",
               "measured_active_seconds", "elements", "hierarchy", "bibtex", *rate_columns)
    export.immutable(destination / "benchmark_summary.csv", csv_bytes(cohort_rows, columns))
    statistics = []
    for name in [*names, "ALL_OBSERVED"]:
        selected = errors if name == "ALL_OBSERVED" else [e for e in errors if e["cohort"] == name]
        denominator = len(versions) if name == "ALL_OBSERVED" else sum(v["cohort"] == name for v in versions)
        pairs = sorted({(e["error_category"], e["error_type"]) for e in selected})
        for category, kind in pairs:
            same = [e for e in selected if e["error_category"] == category and e["error_type"] == kind]
            version_count = sum(e.get("version") is not None for e in same)
            cohort_count = sum(e.get("version") is None for e in same)
            statistics.append(dict(cohort=name, error_category=category, error_type=kind,
                count=len(same), percentage=100 * len(same) / len(selected) if selected else None,
                denominator=len(selected), denominator_unit="diagnostic_records_in_scope",
                version_diagnostics=version_count, cohort_diagnostics=cohort_count,
                observed_versions_denominator=denominator,
                version_rate_percent=100 * version_count / denominator if denominator else None))
    export.immutable(destination / "error_statistics.csv", csv_bytes(statistics,
        ("cohort", "error_category", "error_type", "count", "percentage", "denominator", "denominator_unit", "version_diagnostics", "cohort_diagnostics", "observed_versions_denominator", "version_rate_percent")))
    checksums = {p.name: export.sha(p) for p in destination.iterdir() if p.is_file()}
    export.immutable(destination / "report_checksums.json", export.encoded(checksums))
    if any(export.sha(destination / name) != digest for name, digest in checksums.items()):
        raise BenchmarkFailure("audit_invariant_error", "Report readback checksum mismatch")
    final = group / ("snapshot-" + signature[:16] + "-" + uuid.uuid4().hex[:8])
    os.replace(destination, final)
    export.immutable(final / "READY.json", export.encoded(dict(signature=signature,
        status=totals["status"], corpus_range=names, checksums_sha256=export.sha(final / "report_checksums.json"))))
    verify_snapshot(final)
    value = dict(report_directory=str(final), checksums=checksums)
    publish_pointer(pointer, value)
    publish_pointer(root / "LATEST.json", value)
    print(f"Report {totals['status'].upper()}: {final}", flush=True)
    return dict(summary=totals, report_directory=str(final))


REPORT_FILES = {"benchmark_summary.json", "benchmark_summary.csv", "paper_results.csv", "m2_errors.csv", "error_statistics.csv"}


def verify_snapshot(path):
    ready = export.read(path / "READY.json")
    if export.sha(path / "report_checksums.json") != ready["checksums_sha256"]:
        raise BenchmarkFailure("audit_invariant_error", "Report checksum manifest changed")
    summary = export.read(path / "benchmark_summary.json")
    if ready["status"] != summary["status"] or ready["corpus_range"] != summary["corpus_range"]:
        raise BenchmarkFailure("audit_invariant_error", "Report marker scope/status differs")
    checksums = export.read(path / "report_checksums.json")
    if set(checksums) != REPORT_FILES:
        raise BenchmarkFailure("audit_invariant_error", "Report files differ")
    for name, digest in checksums.items():
        if export.sha(recovery.contained(path / name, path)) != digest:
            raise BenchmarkFailure("audit_invariant_error", f"Report checksum mismatch: {name}")
    return ready


def publish_pointer(path, value):
    temporary = path.with_name(".LATEST-" + uuid.uuid4().hex)
    export.immutable(temporary, export.encoded(value))
    os.replace(temporary, path)


def operate(config, action):
    identity = verify_runtime(config)
    roots, root, key = paths(config)
    names = [f"cohort_{n:04d}_corpus" for n in range(config["start_cohort"], config["end_cohort"] + 1)]
    groups = [[f"cohort_{n:04d}_corpus" for n in group] for group in
              batches(config["start_cohort"], config["end_cohort"], config.get("cohorts_per_batch", 50))]
    if type(config.get("auto_continue", True)) is not bool:
        raise ValueError("AUTO_CONTINUE must be True or False")
    if action == "plan":
        return dict(run_id=root.name, campaign=str(root), cohorts=names, batches=groups,
                    input_inventory=input_inventory(roots, names),
                    existing_completions=[n for n in names if (root / "completed" / (n + ".json")).exists()],
                    note="Chỉ liệt kê marker; phải audit checksum trước khi skip. Không đọc/tạo campaign export.")
    root.mkdir(parents=True, exist_ok=True)
    # A new Colab VM may have a different platform/Python patch version. Keep
    # source identity immutable, record the actual environment per invocation.
    source = {k: identity[k] for k in ("parser_sha", "parser_source_commit", "parser_fingerprint")}
    if not config.get("benchmark_campaign"):
        export.immutable(root / "benchmark.json", export.encoded(dict(identity=key, provenance=source)))
    if action == "report":
        with export.writer(root):
            verified = {}
            for group in groups:
                write_reports(root, identity, group, verified, report_group=f"batch_{group[0][7:11]}-{group[-1][7:11]}")
            return write_reports(root, identity, names, verified)
    selected = names[:1] if action == "smoke" else names
    verified_records = {}
    with export.writer(root):
        if action == "run":
            missing = [r for r in input_inventory(roots, names) if not r["available"]]
            if missing:
                name = missing[0]["cohort"]
                event = dict(cohort=name, event_id=uuid.uuid4().hex, started_at=now(), elapsed_seconds=0, environment=identity)
                error = dict(cohort=name, arxiv_id=None, version=None, error_category="input_missing", error_type="BenchmarkFailure",
                    error_message="; ".join(r["error"] for r in missing), source_file=None, exception_class="BenchmarkFailure",
                    traceback_path=None, status="stopped", parser_sha=identity["parser_sha"])
                try:
                    raise BenchmarkFailure("input_missing", error["error_message"])
                except BenchmarkFailure:
                    diagnostic = root / "events" / event["event_id"] / "traceback.txt"
                    export.immutable(diagnostic, traceback.format_exc().encode("utf8"))
                    error["traceback_path"] = str(diagnostic)
                record = dict(cohort=name, audit_outcome="failed", versions=[], errors=[error])
                folder = root / "events" / event["event_id"]
                export.immutable(folder / "started.json", export.encoded(event))
                export.immutable(folder / "finished.json", export.encoded(dict(event, record=record)))
                group = next(g for g in groups if name in g)
                write_reports(root, identity, group, verified_records, report_group=f"batch_{group[0][7:11]}-{group[-1][7:11]}")
                write_reports(root, identity, names, verified_records)
                raise BenchmarkFailure("input_missing", error["error_message"])
        newly_processed = False
        previous_events = {e["cohort"]: e for e in load_events(root)}
        for name in selected:
            newly_processed = newly_processed or not (root / "completed" / (name + ".json")).exists()
            group = next(g for g in groups if name in g)
            started = time.perf_counter(); event_id = uuid.uuid4().hex
            event_dir = root / "events" / event_id
            event = dict(cohort=name, event_id=event_id, started_at=now(), elapsed_seconds=None, environment=identity)
            stage, native_attempt, durations = "input", None, {}
            if not (root / "completed" / (name + ".json")).exists():
                export.immutable(event_dir / "started.json", export.encoded(event))
            try:
                corpus, rows, item = input_for(config, roots, name)
                stage = "audit"
                prior = saved_cohort(root, name, rows, item, identity)
                if prior is not None:
                    print(f"{name}: checkpoint verified; skip parse", flush=True)
                    # A prior preflight/transient failure may follow an older valid
                    # completion. Record the successful re-audit without counting
                    # its duration as new parser work or altering the completion.
                    verified_records[name] = runner.digest(export.encoded(prior))
                    previous = previous_events.get(name)
                    if previous and previous.get("record", {}).get("audit_outcome") == "failed":
                        export.immutable(event_dir / "started.json", export.encoded(event))
                        export.immutable(event_dir / "finished.json", export.encoded(dict(event, record=prior,
                            ended_at=now(), elapsed_seconds=0, timing_scope="completion_reaudit_excluded")))
                    if name == group[-1]:
                        report = write_reports(root, identity, group, verified_records, report_group=f"batch_{group[0][7:11]}-{group[-1][7:11]}")
                        if report["summary"]["audit_errors"]:
                            raise BenchmarkFailure("audit_invariant_error", "Stored report audit failed")
                        if newly_processed and not config.get("auto_continue", True):
                            break
                    continue
                export.immutable(event_dir / "started.json", export.encoded(event))
                stage = "parse"
                with measure_versions() as durations:
                    native_attempt = runner.run(corpus, root / "native" / name)
                if native_attempt["retryable"]:
                    raise BenchmarkFailure("unknown", "Retryable native timeout; preserve partial checkpoint and resume")
                stage = "audit"
                expected = recovery.native_definition(item, rows, PARSER_FINGERPRINT)
                if export.read(Path(native_attempt["run_root"]) / "definition.json") != expected:
                    raise BenchmarkFailure("audit_invariant_error", "Native definition differs from verified benchmark input/parser/selection")
                audit = recovery.audit_attempt(native_attempt, rows, runner, expected_parser_commit=identity["parser_sha"])
                versions, errors = version_rows(name, native_attempt, "verified", durations)
                attempt_path = root / "native_evidence" / (event_id + ".json")
                export.immutable(attempt_path, export.encoded(native_attempt))
                hashes = evidence(root, native_attempt, rows)
                hashes[str(attempt_path.relative_to(root))] = export.sha(attempt_path)
                record = dict(cohort=name, input=item, attempt_path=str(attempt_path.relative_to(root)), versions=versions, errors=errors,
                              audit_outcome="verified", tables=audit["tables"], evidence=hashes)
                export.immutable(root / "completed" / (name + ".json"), export.encoded(
                    dict(payload=record, sha256=runner.digest(export.encoded(record)))))
                verified_records[name] = runner.digest(export.encoded(record))
            except (Exception, KeyboardInterrupt) as exc:
                category = (exc.category if isinstance(exc, BenchmarkFailure) else
                            "audit_invariant_error" if stage == "audit" else
                            "unexpected_exception" if stage == "parse" else "unknown")
                export.immutable(event_dir / "started.json", export.encoded(event))
                diagnostic = event_dir / "traceback.txt"
                export.immutable(diagnostic, traceback.format_exc().encode("utf8"))
                versions, errors = version_rows(name, native_attempt, "failed", durations) if native_attempt else ([], [])
                errors.append(dict(cohort=name, arxiv_id=None, version=None, error_category=category,
                    error_type=type(exc).__name__, error_message=str(exc), source_file=None,
                    exception_class=type(exc).__name__, traceback_path=str(diagnostic),
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "stopped", parser_sha=identity["parser_sha"]))
                record = dict(cohort=name, versions=versions, errors=errors, audit_outcome="failed", tables={})
                export.immutable(event_dir / "finished.json", export.encoded(dict(event, record=record,
                    ended_at=now(), elapsed_seconds=time.perf_counter() - started)))
                write_reports(root, identity, group, verified_records, report_group=f"batch_{group[0][7:11]}-{group[-1][7:11]}")
                result = write_reports(root, identity, names, verified_records)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                raise BenchmarkFailure(category, f"Dừng an toàn tại {name}; diagnostic: {diagnostic}") from exc
            export.immutable(event_dir / "finished.json", export.encoded(dict(event, record=record,
                ended_at=now(), elapsed_seconds=time.perf_counter() - started)))
            if name == group[-1] or action == "smoke":
                report = write_reports(root, identity, group, verified_records, report_group=f"batch_{group[0][7:11]}-{group[-1][7:11]}")
                if report["summary"]["audit_errors"]:
                    raise BenchmarkFailure("audit_invariant_error", "Stored benchmark evidence failed report audit; stop and review")
                if not config.get("auto_continue", True):
                    break
        return write_reports(root, identity, names, verified_records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", choices=("plan", "smoke", "run", "report"), required=True)
    args = parser.parse_args()
    try:
        result = operate(export.read(args.config), args.action)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except (BenchmarkFailure, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

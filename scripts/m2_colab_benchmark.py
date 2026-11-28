"""Independent CPU-only M2 benchmark; input is read-only, reports are observed data."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import io
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
    import matplotlib
    return dict(parser_sha=head, parser_source_commit=PARSER_SOURCE_COMMIT,
                dependencies=dict(pyarrow=pyarrow.__version__, matplotlib=matplotlib.__version__),
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
    if root.exists() and any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Symlink in benchmark output; preserve and review")
    return roots, root, identity


def scope_for(config, number):
    members = export.read(Path(config["repo"]) / "config/group10_id_ranges.json")["members"]
    owner = members[(number - 1) // 50]["mssv"]
    return export.scope_for(dict(config, student_id=owner, start_cohort=number, end_cohort=number,
                                 batch_size=1, hf_repo_id="benchmark/not-uploaded"))


def input_for(config, roots, name):
    number = int(name[7:11]); scope = scope_for(config, number)
    archive_root = roots["input_drive_dir"]
    # All three files must be adjacent. Use a shared parent and per-member archive
    # subdirectories only when the exact triplet resolves uniquely.
    candidates = [p.parent for p in archive_root.rglob(name + ".manifest.json")]
    if len(candidates) > 1:
        raise BenchmarkFailure("input_missing", f"Ambiguous archive triplets for {name}: {candidates}")
    folder = candidates[0] if candidates else archive_root
    required = [folder / (name + suffix) for suffix in (".tar.gz", ".manifest.json", ".tar.gz.sha256")]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise BenchmarkFailure("input_missing", "Missing required inputs: " + ", ".join(missing))
    if any(p.is_symlink() for path in required for p in [path, *path.parents]):
        raise BenchmarkFailure("input_missing", "Symlink input archive path")
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
    recovery.audit_attempt(attempt, rows, runner, expected_parser_commit=identity["parser_sha"])
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
    eligible = states["success"] + states["parse_error"]
    errors = [e for r in records for e in r.get("errors", [])]
    return dict(run_id=root.name, timestamp=now(), provenance=identity,
        corpus_range=expected_names, cohorts_completed=completed, cohorts_observed=len(records),
        requested_candidate_positions=100 * len(expected_names),
        measurement_environments=[dict(event_id=e["event_id"], **e.get("environment", {})) for e in events],
        input_coverage=[dict(cohort=r["cohort"], included_publications=len(r["input"]["included_ids"]),
                            omitted_ids=r["input"]["omitted_ids"]) for r in records if r.get("input")],
        benchmark_complete=set(completed) == set(expected_names), papers=len(grouped), versions=count,
        statuses=dict(states), successful_versions=states["success"], parse_error=states["parse_error"],
        empty_no_tex=states["empty_no_tex"], attempted_versions=count, eligible_versions=eligible,
        version_success_rate_percent=rate(states["success"], count),
        eligible_success_rate_percent=rate(states["success"], eligible),
        failure_rate_percent=rate(count - states["success"] - states["empty_no_tex"], count),
        missing_latex_rate_percent=rate(states["empty_no_tex"], count),
        paper_success_rate_percent=rate(papers_success, len(grouped)),
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


def charts(destination, records, totals):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    statuses = totals["statuses"]
    ranked = Counter(totals["error_types"]).most_common()
    categories = dict(ranked[:15])
    if len(ranked) > 15:
        categories["Other diagnostic types"] = sum(n for _, n in ranked[15:])
    series = [
        ("parse_status_distribution.png", list(statuses), list(statuses.values()), "Parse outcomes (observed versions)", "Versions"),
        ("error_type_distribution.png", list(categories), list(categories.values()), "Diagnostics (no_tex_source is informational)", "Diagnostic records"),
        ("success_rate_by_cohort.png", [r["cohort"][7:11] for r in records if r.get("versions")],
         [100 * sum(v["status"] == "success" for v in r["versions"]) / len(r["versions"]) for r in records if r.get("versions")],
         "Success / observed versions per cohort", "Percent (%)"),
        ("runtime_by_cohort.png", [r["cohort"][7:11] for r in records],
         [r.get("measured_active_seconds", 0) for r in records], "Measured active cohort time (all recorded attempts)", "Seconds"),
    ]
    for filename, labels, values, title, unit in series:
        fig, ax = plt.subplots(figsize=(max(7, min(18, len(labels) * .3)), 5))
        ax.bar(labels, values); ax.set(title=title, ylabel=unit, xlabel="Observed scope only")
        ax.tick_params(axis="x", labelrotation=65)
        if not labels:
            ax.text(.5, .5, "No observed records", transform=ax.transAxes, ha="center")
        fig.tight_layout(); fig.savefig(destination / filename, dpi=150); plt.close(fig)


def load_events(root):
    events = []
    for path in sorted((root / "events").glob("*/started.json")):
        start = export.read(path)
        finished = path.with_name("finished.json")
        events.append(export.read(finished) if finished.exists() else start)
    return sorted(events, key=lambda e: e["started_at"])


def write_reports(root, identity, names, verified_records=None):
    # Valid only within one invocation under the exclusive benchmark writer.
    # Completed evidence is immutable; do not reread all prior Parquet/paper
    # bytes after every cohort (quadratic Drive I/O). A new invocation starts
    # empty and verifies hashes again, including the standalone report action.
    verified_records = {} if verified_records is None else verified_records
    events = load_events(root)
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
    records = [latest[n] for n in names if n in latest]
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
    destination = root / "reports" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
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
    columns = ("cohort", "audit_outcome", "versions", "success", "parse_error", "empty_no_tex", "source_unavailable", "input_error",
               "measured_active_seconds", "elements", "hierarchy", "bibtex")
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
                version_diagnostics=version_count, cohort_diagnostics=cohort_count,
                observed_versions_denominator=denominator,
                version_rate_percent=100 * version_count / denominator if denominator else None))
    export.immutable(destination / "error_statistics.csv", csv_bytes(statistics,
        ("cohort", "error_category", "error_type", "version_diagnostics", "cohort_diagnostics", "observed_versions_denominator", "version_rate_percent")))
    pct = lambda n: "chưa đo" if n is None else f"{n:.2f}%"
    lines = ["# Báo cáo benchmark M2", "", f"Run ID: `{root.name}`; parser SHA: `{identity['parser_sha']}`.",
        f"Nguồn parser nocite: `{PARSER_SOURCE_COMMIT}`; fingerprint: `{PARSER_FINGERPRINT}`.",
        f"Phạm vi yêu cầu: {names[0]}–{names[-1]}. Hoàn tất: {totals['benchmark_complete']}.",
        f"Đã quan sát {len(records)} cohort, {totals['papers']} bài, {totals['versions']} phiên bản; audit verified {len(totals['cohorts_completed'])} cohort.",
        "", "| Chỉ số | Kết quả | Mẫu số |", "|---|---:|---:|",
        f"| Version success | {pct(totals['version_success_rate_percent'])} | {totals['attempted_versions']} phiên bản quan sát |",
        f"| Failure (parse_error + source_unavailable + input_error) | {pct(totals['failure_rate_percent'])} | {totals['attempted_versions']} |",
        f"| M1 xác nhận không có TeX | {pct(totals['missing_latex_rate_percent'])} | {totals['attempted_versions']} |",
        f"| Eligible success | {pct(totals['eligible_success_rate_percent'])} | {totals['eligible_versions']} success + parse_error |",
        f"| Paper success (mọi version đều success) | {pct(totals['paper_success_rate_percent'])} | {totals['papers']} bài quan sát |",
        "", f"Trạng thái: `{totals['statuses']}`. Bảng unique đã audit: `{totals['tables']}`.",
        f"Phạm vi yêu cầu: {totals['requested_candidate_positions']} vị trí ID ứng viên; không đồng nghĩa số bài tồn tại. input_coverage trong JSON giữ included/omitted IDs theo manifest đã xác minh.",
        f"Thời gian active đã đo: {totals['measured_active_seconds']:.3f} giây. Throughput: {totals['throughput_versions_per_second']} versions/second.",
        f"Khoảng bị ngắt chưa đo đủ: {totals['interrupted_intervals_unmeasured']}; không tính khoảng nghỉ giữa runtime.",
        "Thời gian gồm kiểm tra/extract input, đọc/parse/ghi Parquet và audit, không gồm Setup, báo cáo hay re-audit cohort đã hoàn tất.",
        "`runtime_seconds` theo version đo prepare/render worker (gồm spawn), không phải end-to-end: không gồm merge cross-version, kiểm tra input hay ghi Parquet. Phiên bản không gọi worker/cached để trống; xem runtime_scope.",
        "Native parser giữ error message/root nhưng không giữ exception class/traceback cho lỗi đã bắt: các ô tương ứng để trống. Lỗi ngoài parser có traceback thật.",
        "`latex_parse_error` chỉ phân loại parse outcome; không khẳng định lỗi nguồn hay lỗi implementation. Chưa có chứng cứ nguyên nhân thì giữ unknown.",
        "`empty_no_tex` là kết quả hợp lệ của M1, không phải parser bug. Record bibliography dùng union cross-version; các count gắn version không được cộng làm tổng unique.",
        "", "## Ví dụ diagnostic", ""]
    lines += [f"- `{e['cohort']}/{e['arxiv_id']}/{e['version']}` [{e['error_category']}]: {e['error_message']}" for e in errors[:12]]
    lines += ["", "## Diagnostic types", "", "| Observed message family | Count |", "|---|---:|"]
    lines += [f"| {kind.replace('|', '/')} | {count} |" for kind, count in Counter(totals["error_types"]).most_common(20)]
    lines += ["", "## Giới hạn và sử dụng Poster", "",
        "Chỉ sử dụng số liệu ở phạm vi quan sát và ghi kèm parser SHA, môi trường, input fingerprints và trạng thái audit. Parse success không tự chứng minh đầy đủ ngữ nghĩa khoa học.",
        "Không diễn giải partial run thành kết quả toàn bộ 200 cohort. Archive chưa đọc được không có mẫu số version đáng tin cậy và được báo riêng ở cohort diagnostics.",
        "Nếu audit thất bại, parse outcomes chưa được chứng nhận; tổng bảng chỉ tính cohort verified. Đây là benchmark M2, không phải benchmark M1 đầu-cuối hay final benchmark tự động.",
        "Resume dùng checkpoint native có checksum; thời gian là campaign có resume/cache, không phải cold-run đồng nhất. Khi runtime bị kill, khoảng chưa ghi kết thúc không thể phục hồi chính xác.",
        "Drive mounted filesystem chưa được nghiệm thu live trong release này; cần smoke trước. Không tự tải BBL artifacts thiếu hoặc mượn bibliography của version khác."]
    export.immutable(destination / "benchmark_report.md", ("\n".join(lines) + "\n").encode("utf8"))
    charts(destination, records, totals)
    # Publish a complete report snapshot only after all CSV/JSON/PNG exist.
    checksums = {p.name: export.sha(p) for p in destination.iterdir() if p.is_file()}
    export.immutable(destination / "report_checksums.json", export.encoded(checksums))
    temporary = root / (".LATEST-" + uuid.uuid4().hex)
    export.immutable(temporary, export.encoded(dict(report_directory=str(destination), checksums=checksums)))
    os.replace(temporary, root / "LATEST.json")
    return dict(summary=totals, report_directory=str(destination))


def operate(config, action):
    identity = verify_runtime(config)
    roots, root, key = paths(config)
    names = [f"cohort_{n:04d}_corpus" for n in range(config["start_cohort"], config["end_cohort"] + 1)]
    if action == "plan":
        return dict(run_id=root.name, campaign=str(root), cohorts=names,
                    existing_completions=[n for n in names if (root / "completed" / (n + ".json")).exists()],
                    note="Chỉ liệt kê marker; phải audit checksum trước khi skip. Không đọc/tạo campaign export.")
    root.mkdir(parents=True, exist_ok=True)
    # A new Colab VM may have a different platform/Python patch version. Keep
    # source identity immutable, record the actual environment per invocation.
    source = {k: identity[k] for k in ("parser_sha", "parser_source_commit", "parser_fingerprint")}
    export.immutable(root / "benchmark.json", export.encoded(dict(identity=key, provenance=source)))
    if action == "report":
        return write_reports(root, identity, names)
    selected = names[:1] if action == "smoke" else names
    verified_records = {}
    with export.writer(root):
        for name in selected:
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
                result = write_reports(root, identity, names, verified_records)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                raise BenchmarkFailure(category, f"Dừng an toàn tại {name}; diagnostic: {diagnostic}") from exc
            export.immutable(event_dir / "finished.json", export.encoded(dict(event, record=record,
                ended_at=now(), elapsed_seconds=time.perf_counter() - started)))
            # Per-cohort durable reports; never wait until all 200 cohorts complete.
            report = write_reports(root, identity, names, verified_records)
            if report["summary"]["audit_errors"]:
                raise BenchmarkFailure("audit_invariant_error", "Stored benchmark evidence failed report audit; stop and review")
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

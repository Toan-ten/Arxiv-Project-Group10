"""Benchmark Batch without confusing execution with corpus completeness.

Single writer only. aggregate_batch is read-only; run_benchmark invokes the
real Batch (and therefore real HTTP unless the caller supplies offline fakes).
"""
import copy
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from src import arxiv_client, batch, benchmark, pipeline, s2_integration
from src.benchmark_metrics import sampling_config, timing_summary
from src.storage import CorpusStorage, StorageError, TERMINAL, DISCOVERY_STATES, SOURCE_ERRORS, _atomic_json


class BenchmarkError(ValueError):
    """Invalid experiment, batch definition, or report destination."""


class _ExecutionFailed(RuntimeError):
    """Sanitized signal to the foundation; original exceptions stay in memory."""


def _require(condition):
    if not condition:
        raise BenchmarkError("Invalid benchmark input or evidence")


def _identifier(value):
    _require(isinstance(value, str)
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is not None
             and value.upper() not in {"CON", "PRN", "AUX", "NUL"} |
             {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)})
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _definition(pairs, corpus, max_ids, versions, timeout, max_attempts, user_agent, s2_config=None):
    _require(type(max_ids) is int and max_ids > 0)
    chosen = pipeline._version_selection(versions)
    validated = pipeline._inputs(pairs, CorpusStorage(corpus), timeout, max_attempts, user_agent)
    _require(bool(validated))
    definition = dict(
        input_pairs=[list(pair) for pair in validated],
        selection=[list(pair) for pair in validated[:max_ids]],
        corpus_root=os.path.normcase(str(corpus)), max_ids=max_ids,
        versions=None if chosen is None else sorted(chosen, key=lambda v: int(v[1:])),
        timeout=float(timeout), max_attempts=max_attempts, user_agent=user_agent,
    )
    s2_config = s2_integration.configuration(s2_config)
    if s2_config is not None:
        definition["s2"] = s2_config
    return definition


def _locations(corpus_root, log_root, batch_id):
    _identifier(batch_id)
    corpus, logs = batch._safe(corpus_root), batch._safe(log_root)
    _require(not (corpus.is_relative_to(logs) or logs.is_relative_to(corpus)))
    return corpus, logs, batch._safe(logs / batch_id)


def _load_evidence(directory, batch_id, corpus):
    value = batch._read(directory / "batch.json")
    _require(value.get("schema_version") == 1 and value.get("batch_id") == batch_id)
    definition = value["definition"]
    _require(isinstance(definition, dict))
    expected = _definition(
        definition["input_pairs"], corpus, definition["max_ids"],
        definition["versions"], definition["timeout"], definition["max_attempts"],
        definition["user_agent"], definition.get("s2"),
    )
    _require(definition == expected)
    history = batch._history(batch._safe(directory / "attempts"), batch_id, definition["selection"])
    _require(bool(history))
    for attempt in history:
        _require(_number(attempt["wall_seconds"]))
        _require(type(attempt["stopped"]) is bool)
        _require((attempt["status"] == "stopped") <= attempt["stopped"])
        for row in attempt["papers"]:
            _require(row["wall_seconds"] is None or _number(row["wall_seconds"]))
            if row["state"] == "processed":
                _require(_number(row["wall_seconds"]))
            result = row["result"]
            if result is None:
                continue
            _require(result.get("mssv") == row["mssv"]
                     and result.get("arxiv_id") == row["arxiv_id"])
            discovery, sources = result["discovery"], result["source"]
            _require(isinstance(discovery, dict) and isinstance(sources, dict))
            _require(discovery["action"] in {"not_attempted", "queried", "cached"})
            _require(discovery.get("status") is None
                     or isinstance(discovery["status"], str) and discovery["status"] in DISCOVERY_STATES)
            _require(isinstance(result["errors"], list) and isinstance(result["blocked"], dict))
            for name, source in sources.items():
                _require(re.fullmatch(r"v[1-9][0-9]*", name) is not None
                         and isinstance(source, dict))
                _require(source["action"] in {
                    "pending", "in_progress", "downloaded", "skipped", "failed", "blocked",
                })
                _require(source.get("status") is None
                         or isinstance(source["status"], str))
            for error in result["errors"]:
                _require(error.get("version") is None or isinstance(error["version"], str)
                         and re.fullmatch(r"v[1-9][0-9]*", error["version"]) is not None)
                _require(isinstance(error, dict) and isinstance(error.get("stage"), str)
                         and isinstance(error.get("type"), str))
    return definition, history


def _load(directory, batch_id, corpus):
    try:
        return _load_evidence(directory, batch_id, corpus)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BenchmarkError("Missing or invalid batch definition/history") from None


def _stop_reason(attempt):
    reason = attempt.get("stop_reason")
    if not isinstance(reason, dict):
        return None
    return dict(
        type="http_policy" if reason.get("type") == "http_policy" else "unclassified",
        stage=reason.get("stage") if reason.get("stage") in (
            "source", "discovery", "s2_metadata", "s2_references") else None,
        http_status=reason.get("http_status") if reason.get("http_status") in (403, 429) else None,
        arxiv_id=reason.get("arxiv_id") if any(
            row["arxiv_id"] == reason.get("arxiv_id") for row in attempt["papers"]) else None,
        version=reason.get("version") if isinstance(reason.get("version"), str)
        and re.fullmatch(r"v[1-9][0-9]*", reason["version"]) else None,
    )


def _history_summary(history):
    attempts, errors = [], []
    operations = dict(discovery_queried=0, discovery_cached=0,
                      source_downloaded=0, source_skipped=0, source_failed=0)
    for attempt in history:
        attempts.append({key: attempt[key] for key in (
            "attempt_id", "run_id", "attempt_number", "status",
            "started_at", "ended_at", "wall_seconds",
        )})
        attempts[-1].update(stop_reason=_stop_reason(attempt),
                            environment_provenance=None, resource_measurements=None,
                            timing_metrics=timing_summary([attempt]))
        for row in attempt["papers"]:
            result = row["result"]
            if result is None:
                continue
            # Only final paper snapshots count operations; in-progress snapshots
            # cannot establish that a download finished.
            if row["state"] == "processed":
                action = result["discovery"]["action"]
                if action in {"queried", "cached"}:
                    operations["discovery_" + action] += 1
                for source in result["source"].values():
                    action = source["action"]
                    if action in {"downloaded", "skipped", "failed"}:
                        operations["source_" + action] += 1
                for stage in ("s2_metadata", "s2_references"):
                    action = result.get(stage, {}).get("action")
                    if action in {"queried", "resumed", "cached", "blocked"}:
                        key = stage + "_" + action
                        operations[key] = operations.get(key, 0) + 1
            for error in result["errors"]:
                errors.append(dict(
                    attempt_id=attempt["attempt_id"], arxiv_id=row["arxiv_id"],
                    mssv=row["mssv"],
                    stage=error["stage"] if error["stage"] in {
                        "source", "discovery", "storage", "checkpoint", "pipeline",
                        "s2_metadata", "s2_references",
                    } else "unclassified",
                    type=error["type"] if error["type"] in SOURCE_ERRORS | DISCOVERY_STATES | {
                        "needs_verification", "not_discovered", "StorageError", "IntegrityError",
                        "OSError", "RuntimeError", "ValueError", "PipelineStopped", "pagination_limit",
                    } else "unclassified",
                    version=error.get("version"),
                ))  # Deliberately omit free-form messages and tracebacks.
        if attempt.get("error"):
            errors.append(dict(attempt_id=attempt["attempt_id"], stage="batch",
                               type="execution_exception"))
    paper_times = []
    for row in history[0]["papers"]:
        durations = [a["papers"][row["index"]]["wall_seconds"] for a in history]
        paper_times.append(dict(
            mssv=row["mssv"], arxiv_id=row["arxiv_id"],
            logged_wall_seconds=sum(d for d in durations if d is not None),
            missing_intervals=sum(d is None for d in durations),
        ))
    return dict(
        timings=timing_summary(history),
        attempts=attempts, error_history=errors, operations=operations,
        operations_scope="final processed-paper snapshots; not HTTP request counts",
        logged_attempt_wall_seconds=sum(a["wall_seconds"] for a in history),
        wall_scope="sum of Batch persisted perf_counter intervals; excludes session gaps and final log writes",
        wall_status="lower_bound" if any(a["status"] == "running" for a in history) else "logged_intervals",
        paper_wall_seconds=paper_times,
        http_requests=None, http_errors=None, http_metrics_status="not_collected",
        downloaded_bytes=None, downloaded_bytes_status="not_collected",
    )


def _paper(store, pair, batch_id, known_runs, *, s2_enabled=False):
    mssv, identifier = pair
    manifest = store.read_manifest(*pair)
    metadata, references = store.read_metadata(*pair), store.read_references(*pair)
    if s2_enabled:
        s2_integration.validate_checkpoint(store, *pair, for_resume=False)
    # _sync validates dependencies but writes nothing. Compare its in-memory
    # result to refuse incomplete multi-JSON commits rather than repairing them.
    synced = copy.deepcopy(manifest)
    store._sync(synced, metadata, references)
    _require(synced == manifest)
    provenance = metadata["provenance"]["arxiv"]
    if manifest["discovery_status"] in {"found", "not_found"}:
        _require(isinstance(provenance, dict)
                 and provenance["snapshot"]["status"] == manifest["discovery_status"])
    versions = []
    for name, record in manifest["versions"].items():
        if record["run_id"] and record["run_id"].startswith(batch_id + "-"):
            _require(record["run_id"] in known_runs)
        valid = False
        if record["status"] in TERMINAL:
            valid = store.should_skip_version(*pair, name)
            _require(valid)
        else:
            # Pending/error source directories are not evidence of success.
            _require(not store._safe(store.paper_path(*pair) / name).exists())
        versions.append(dict(
            version=name, status=record["status"], verified=valid,
            files=len(record["files"]) if valid else None,
            kept_bytes=record["kept_bytes"] if valid else None,
            archive_bytes=record["archive_bytes"] if valid else None,
        ))
    if manifest["run_id"] and manifest["run_id"].startswith(batch_id + "-"):
        _require(manifest["run_id"] in known_runs)
    complete = (
        manifest["discovery_status"] == "found"
        and manifest["version_status"] == "resolved" and bool(versions)
        and all(v["verified"] for v in versions)
        and metadata["status"] == references["status"] == "complete"
    )
    # A claimed complete paper must satisfy every contract condition, not just
    # have one successful source version.
    if manifest["status"] in {"complete", "complete_with_no_tex"}:
        _require(complete)
        expected = "complete_with_no_tex" if any(
            v["status"] == "no_tex_source" for v in versions) else "complete"
        _require(manifest["status"] == expected)
    return dict(
        mssv=mssv, arxiv_id=identifier, checkpoint_status="verified",
        paper_status=manifest["status"], discovery_status=manifest["discovery_status"],
        version_status=manifest["version_status"], versions=versions,
        references_status=references["status"],
        reference_items=len(references["items"]) if references["status"] == "complete" else None,
        publication_complete=complete and manifest["status"] in {"complete", "complete_with_no_tex"},
    )


def _valid_resources(value):
    """Validate v2 numeric invariants before including a persisted measurement."""
    _require(isinstance(value, dict) and value["schema_version"] == 2
             and _number(value["wall_seconds"]))
    for key in ("ram", "disk"):
        series = value[key]
        _require(isinstance(series, dict) and series["unit"] == "bytes")
        count = series["sample_count"]
        _require(type(count) is int and count >= 0)
        _require(series["status"] in {"collected", "partial", "not_collected"})
        if count:
            _require(series["status"] != "not_collected")
            _require(all(_number(series[f]) for f in (
                "sum_sample_bytes", "initial_bytes", "final_bytes", "sampled_peak_bytes",
            )))
            _require(series["sampled_peak_bytes"] >= max(series["initial_bytes"], series["final_bytes"])
                     and series["sampled_peak_bytes"] <= series["sum_sample_bytes"]
                     <= series["sampled_peak_bytes"] * count)
            if key == "ram":
                _require(_number(series["average_bytes"])
                         and math.isclose(series["average_bytes"], series["sum_sample_bytes"] / count))
        else:
            _require(series["status"] == "not_collected")
            _require(all(series[f] is None for f in (
                "sum_sample_bytes", "sampled_peak_bytes", "initial_bytes", "final_bytes",
            )))
            if key == "ram":
                _require(series["average_bytes"] is None)
    disk = value["disk"]
    _require(disk["final_corpus_bytes"] is None or _number(disk["final_corpus_bytes"]))
    _require(isinstance(disk["roots"], list) and bool(disk["roots"])
             and all(isinstance(p, str) for p in disk["roots"]))
    _require(isinstance(value["ram"]["source"], str) and isinstance(value["ram"]["scope"], str))
    _require(isinstance(value["sampling"], dict))
    # Do not propagate unknown fields such as credentials from edited sidecars.
    result = {k: value[k] for k in ("schema_version", "wall_seconds", "sampling")}
    if "wall_scope" in value:
        result["wall_scope"] = value["wall_scope"]
    fields = {
        "status", "reason", "failure_reasons", "unit", "sample_count", "failed_sample_count",
        "sum_sample_bytes", "initial_bytes", "final_bytes", "sampled_peak_bytes",
        "average_bytes", "average_method", "first_sample_seconds", "last_sample_seconds",
        "source", "scope", "backend_version", "peak_method", "roots", "corpus_root", "final_corpus_bytes",
        "final_corpus_status",
    }
    for kind in ("ram", "disk"):
        result[kind] = {k: v for k, v in value[kind].items() if k in fields}
    return result


def _measurement_summary(attempts, directory, fingerprint):
    observations, missing, invalid = [], [], []
    for attempt in attempts:
        path = directory / "measurements" / (attempt["attempt_id"] + ".json")
        if not path.exists():
            missing.append(attempt["attempt_id"])
            continue
        try:
            value = batch._read(path)
            _require(value["schema_version"] == 2
                     and value["batch_id"] == directory.name
                     and value["attempt_id"] == attempt["attempt_id"]
                     and value["run_id"] == attempt["run_id"]
                     and value["selection_fingerprint"] == fingerprint)
            resources = _valid_resources(value["resources"])
            observations.append(dict(
                attempt_id=attempt["attempt_id"], run_id=attempt["run_id"],
                resources=resources, environment={k: v for k, v in value["environment"].items()
                    if k in {"git_commit", "git_dirty", "python", "operating_system",
                             "cpu_count", "measurement_environment"}},
                report_path=value["report_path"],
            ))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            invalid.append(attempt["attempt_id"])
    complete = bool(attempts) and not missing and not invalid
    resources = [o["resources"] for o in observations]
    rams, disks = [r["ram"] for r in resources], [r["disk"] for r in resources]
    ram_scopes = {(r["source"], r["scope"]) for r in rams}
    disk_scopes = {tuple(sorted(d["roots"])) for d in disks}
    good_ram = complete and len(ram_scopes) == 1 and all(r["status"] == "collected" for r in rams)
    good_disk = complete and len(disk_scopes) == 1 and all(d["status"] == "collected" for d in disks)
    samples = sum(r["sample_count"] for r in rams)
    last = next((o["resources"] for o in observations
                 if o["attempt_id"] == attempts[-1]["attempt_id"]), None) if attempts else None
    return dict(
        status="collected" if complete and good_ram and good_disk else
               "partial" if observations else "not_collected",
        missing_attempt_ids=missing, invalid_attempt_ids=invalid,
        scope_mismatch=dict(ram=len(ram_scopes) > 1, disk=len(disk_scopes) > 1),
        attempts=observations, unit_bytes="bytes", unit_time="seconds",
        end_to_end_wall_seconds=sum(r["wall_seconds"] for r in resources) if complete else None,
        known_end_to_end_wall_seconds=sum(r["wall_seconds"] for r in resources),
        wall_scope="sum of measured callback intervals only; never calendar gaps or logged Batch time again",
        average_ram_bytes=sum(r["sum_sample_bytes"] or 0 for r in rams) / samples if good_ram and samples else None,
        average_ram_method="sample-count weighted mean across all valid per-attempt RSS observations",
        sampled_peak_ram_bytes=max((r["sampled_peak_bytes"] for r in rams), default=None) if good_ram else None,
        sampled_peak_disk_bytes=max((d["sampled_peak_bytes"] for d in disks), default=None) if good_disk else None,
        final_corpus_bytes=last["disk"]["final_corpus_bytes"] if last else None,
        final_size_scope="last measured attempt's corpus snapshot; never a sum across attempts or a fresh audit size",
    )


def aggregate_batch(corpus_root, *, batch_id, log_root):
    """Read one batch's entire history and cohort Storage without any writes.

    Invalid/missing evidence yields issues and incomplete coverage, never repair.
    Time and operation histories are observations; final coverage is checksum-led.
    """
    corpus, logs, directory = _locations(corpus_root, log_root, batch_id)
    report = dict(
        schema_version=1, batch_id=batch_id, audited_at=batch._now(),
        corpus_root=str(corpus), log_root=str(logs), issues=[],
        configuration=None, history=None, benchmark_metrics=None,
        data_coverage=None, publication_complete=None,
    )
    try:
        definition, history = _load(directory, batch_id, corpus)
        report["history"] = _history_summary(history)
        report["benchmark_metrics"] = _measurement_summary(
            report["history"]["attempts"], directory, _fingerprint(definition["selection"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        report["issues"].append(dict(scope="run_logs", code="missing_or_invalid_history"))
        report["status"] = "incomplete"
        return report
    selection = definition["selection"]
    report["configuration"] = {
        key: definition[key] for key in ("selection", "max_ids", "versions", "timeout", "max_attempts")
    }
    report["configuration"]["selection_fingerprint"] = _fingerprint(selection)
    report["configuration"]["user_agent"] = "not_recorded"
    if "s2" in definition:
        report["configuration"]["s2"] = copy.deepcopy(definition["s2"])
    store = CorpusStorage(corpus)
    papers = []
    runs = {a["run_id"] for a in history}
    persisted_ids = {
        row["arxiv_id"] for attempt in history for row in attempt["papers"]
        if row["result"] and (row["result"]["discovery"].get("status") is not None
                             or row["result"]["source"])
    }
    for pair in selection:
        if pair[1] not in persisted_ids and not store.paper_path(*pair).exists():
            papers.append(dict(mssv=pair[0], arxiv_id=pair[1], checkpoint_status="not_created"))
            continue
        try:
            paper = _paper(store, pair, batch_id, runs, s2_enabled="s2" in definition)
        except (OSError, StorageError, BenchmarkError, KeyError, TypeError, AttributeError):
            report["issues"].append(dict(
                scope="checkpoint", code="missing_inconsistent_or_invalid_source",
                mssv=pair[0], arxiv_id=pair[1],
            ))
            paper = dict(mssv=pair[0], arxiv_id=pair[1], checkpoint_status="invalid")
        papers.append(paper)
    valid = [p for p in papers if p["checkpoint_status"] == "verified"]
    versions = [v for p in valid for v in p["versions"]]
    terminal = [v for v in versions if v["verified"]]
    selected = definition["versions"]
    def selected_ok(p):
        by_name = {v["version"]: v for v in p["versions"]}
        names = list(by_name) if selected is None else selected
        return (p["discovery_status"] == "found" and p["version_status"] == "resolved"
                and bool(names) and all(n in by_name and by_name[n]["verified"] for n in names))
    def all_ok(p):
        return (p["discovery_status"] == "found" and bool(p["versions"])
                and all(v["verified"] for v in p["versions"]))
    complete_count = sum(p["publication_complete"] for p in valid)
    report["data_coverage"] = dict(
        cohort_papers=len(selection), verified_checkpoints=len(valid),
        invalid_checkpoints=sum(p["checkpoint_status"] == "invalid" for p in papers),
        not_created_checkpoints=sum(p["checkpoint_status"] == "not_created" for p in papers),
        found_papers=sum(p["discovery_status"] == "found" for p in valid),
        not_found_papers=sum(p["discovery_status"] == "not_found" for p in valid),
        papers_with_valid_tex=sum(any(v["status"] == "success" and v["verified"]
                                     for v in p["versions"]) for p in valid),
        known_versions=len(versions),
        valid_tex_versions=sum(v["status"] == "success" for v in terminal),
        no_tex_versions=sum(v["status"] == "no_tex_source" for v in terminal),
        version_statuses={s: sum(v["status"] == s for v in versions)
                          for s in sorted({v["status"] for v in versions})},
        selected_sources_terminal=not report["issues"] and len(valid) == len(selection) and all(selected_ok(p) for p in valid),
        all_version_sources_terminal=not report["issues"] and len(valid) == len(selection) and all(all_ok(p) for p in valid),
        verified_source_files=sum(v["files"] for v in terminal),
        verified_source_bytes=sum(v["kept_bytes"] for v in terminal),
        final_archive_payload_bytes=sum(v["archive_bytes"] for v in terminal
                                        if v["archive_bytes"] is not None),
        missing_archive_size_versions=sum(v["archive_bytes"] is None for v in terminal),
        size_scope="current verified cohort source snapshot, counted once; not traffic or whole-root disk usage",
        reference_items=(sum(p["reference_items"] for p in valid)
                         if len(valid) == len(papers) and all(p["reference_items"] is not None for p in valid)
                         else None),
        references_complete_papers=sum(p["references_status"] == "complete" for p in valid),
        paper_statuses={s: sum(p["paper_status"] == s for p in valid)
                        for s in sorted({p["paper_status"] for p in valid})},
        unresolved_papers=sum(p["version_status"] == "needs_verification" for p in valid),
        papers=papers,
    )
    report["publication_complete"] = dict(
        papers=complete_count, cohort_papers=len(selection),
        all_complete=not report["issues"] and complete_count == len(selection),
    )
    report["status"] = "verified" if not report["issues"] else "incomplete"
    return report


def _environment():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    commit, dirty = None, None
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, env=env,
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root, env=env, stderr=subprocess.DEVNULL, timeout=5,
        ).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return dict(
        git_commit=commit, git_dirty=dirty, python=platform.python_version(),
        operating_system=platform.platform(), cpu_count=os.cpu_count(),
        measurement_environment="not_collected",
    )


def run_benchmark(
    pairs, corpus_root, *, batch_id, max_ids, log_root, report_root,
    experiment_id=None, resume=False, versions=None, timeout=30.0,
    max_attempts=1, user_agent=arxiv_client.USER_AGENT, sample_interval=0.1,
    disk_interval=30.0, disk_roots=None, s2_config=None,
):
    """Measure one Batch invocation with the existing foundation, then audit.

    Reports are isolated in a newly reserved experiment directory. Exceptions
    produce a failed execution report then re-raise the original exception.
    Historical attempts have logs only, not retroactively measured RAM/time.
    """
    corpus, logs, directory = _locations(corpus_root, log_root, batch_id)
    definition = _definition(pairs, corpus, max_ids, versions, timeout, max_attempts, user_agent, s2_config)
    _require(type(resume) is bool and _number(sample_interval) and sample_interval > 0)
    sampling = sampling_config(corpus, sample_interval, disk_interval, disk_roots)
    previous = []
    if directory.exists():
        existing, previous = _load(directory, batch_id, corpus)
        _require(resume and existing == definition)
    else:
        _require(not resume)
    reports = batch._safe(report_root)
    for other in (corpus, logs):
        _require(not (reports.is_relative_to(other) or other.is_relative_to(reports)))
    experiment_id = _identifier(uuid4().hex if experiment_id is None else experiment_id)
    destination = batch._safe(reports / experiment_id)
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise BenchmarkError("Experiment already exists; reports are never overwritten") from None
    started_at = batch._now()
    intent = dict(
        schema_version=1, experiment_id=experiment_id, batch_id=batch_id,
        started_at=started_at, environment=_environment(),
        metrics_version=2, measurement_configuration=sampling,
        configuration={k: definition[k] for k in (
            "selection", "max_ids", "versions", "timeout", "max_attempts",
        )},
        selection_fingerprint=_fingerprint(definition["selection"]),
        corpus_root=str(corpus), log_root=str(logs),
        report_path=str(destination / "report.json"),
        resources_path=str(destination / "resources.json"),
        previous_attempt_ids=[a["attempt_id"] for a in previous],
    )
    if "s2" in definition:
        intent["configuration"]["s2"] = copy.deepcopy(definition["s2"])
    _atomic_json(destination / "intent.json", intent)
    failure, measured_failure = [], None

    def measured_batch():
        try:
            batch.run_batch(
                pairs, corpus, batch_id=batch_id, max_ids=max_ids, log_root=logs,
                resume=resume, versions=versions, timeout=timeout,
                max_attempts=max_attempts, user_agent=user_agent,
                **({"s2_config": definition["s2"]} if "s2" in definition else {}),
            )
        except BaseException as exc:
            failure.append(exc)
            # Suppress exception context so foundation traceback cannot serialize
            # credentials from arbitrary exception text.
            raise _ExecutionFailed("Batch exception; original details withheld") from None

    resources = None
    try:
        resources = benchmark.benchmark_pipeline(
            measured_batch, corpus_output_dir=corpus,
            report_path=destination / "resources.json",
            sample_interval=sample_interval, verbose=False,
            disk_interval=disk_interval, disk_roots=disk_roots,
        )
    except BaseException as exc:
        measured_failure = exc
        if (destination / "resources.json").is_file():
            try:
                resources = batch._read(destination / "resources.json")
            except (OSError, ValueError):
                pass  # Never substitute a successful measurement for corrupt evidence.
    if resources is not None and resources.get("ram_source") == "unavailable":
        resources = copy.deepcopy(resources)
        for field in ("initial_ram_mb", "final_ram_mb", "peak_ram_mb"):
            resources[field] = None
    audit = aggregate_batch(corpus, batch_id=batch_id, log_root=logs)
    history = audit["history"]
    old_ids = set(intent["previous_attempt_ids"])
    current = [a for a in history["attempts"] if a["attempt_id"] not in old_ids] if history else []
    batch_status = current[0]["status"] if len(current) == 1 and current[0]["status"] != "running" else None
    sidecar_issue, current_v2 = None, None
    if len(current) == 1 and resources is not None and "metrics_v2" in resources:
        try:
            sampled = _valid_resources(resources["metrics_v2"])
            current_v2 = sampled
            measurements = batch._safe(directory / "measurements")
            measurements.mkdir(exist_ok=True)
            path = batch._safe(measurements / (current[0]["attempt_id"] + ".json"))
            _require(not path.exists())
            _atomic_json(path, dict(
                schema_version=2, batch_id=batch_id,
                attempt_id=current[0]["attempt_id"], run_id=current[0]["run_id"],
                selection_fingerprint=intent["selection_fingerprint"],
                resources=sampled, environment=intent["environment"],
                report_path=intent["report_path"],
            ))
            audit["benchmark_metrics"] = _measurement_summary(
                history["attempts"], directory, intent["selection_fingerprint"],
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            sidecar_issue = "measurement_not_persisted"
    metrics = audit.get("benchmark_metrics")
    timings = history["timings"] if history else None
    unavailable = dict(http_requests=None, http_errors=None)
    if not current_v2 or current_v2["ram"]["average_bytes"] is None:
        unavailable["average_ram_bytes"] = None
    if not current_v2 or current_v2["disk"]["sampled_peak_bytes"] is None:
        unavailable["peak_disk_bytes"] = None
    if not timings or timings["discovery_wall_seconds"] is None:
        unavailable["discovery_wall_seconds"] = None
    execution_status = "raised" if failure or measured_failure else "returned"
    report = dict(
        **intent, ended_at=batch._now(), execution_status=execution_status,
        execution_error="batch_exception" if failure else ("measurement_exception" if measured_failure else None),
        batch_status=batch_status,
        stop_reason=current[0]["stop_reason"] if len(current) == 1 else None,
        durable_batch_status=current[0]["status"] if len(current) == 1 else None,
        attempt_ids=[a["attempt_id"] for a in current],
        run_ids=[a["run_id"] for a in current],
        measurement_status="recorded" if resources is not None else "not_collected",
        resource_metrics=resources,
        benchmark_metrics=metrics, measurement_issue=sidecar_issue,
        timing_metrics=timings,
        foundation_status_semantics="execution only; never corpus or publication success",
        resource_scope="one invocation including pacing/retry; excludes adapter aggregation and session gaps",
        resource_size_scope="whole-root logical bytes, including non-cohort files; v2 strict scan, legacy reports may be best-effort",
        resource_units="foundation *_mb fields are MiB (1024**2 bytes); elapsed time is seconds",
        aggregation=audit,
        data_coverage=audit["data_coverage"],
        publication_complete=audit["publication_complete"],
        unavailable_metrics=unavailable,
    )
    if len(current) != 1:
        report["aggregation"]["issues"].append(dict(scope="attempt_link", code="missing_or_ambiguous_attempt"))
        report["aggregation"]["status"] = "incomplete"
        if report["data_coverage"]:
            report["data_coverage"]["selected_sources_terminal"] = False
            report["data_coverage"]["all_version_sources_terminal"] = False
            report["publication_complete"]["all_complete"] = False
    _atomic_json(destination / "report.json", report)
    if failure:
        raise failure[0]
    if measured_failure:
        raise measured_failure
    return report

"""Sequential batches with durable audit logs; Storage alone verifies source.

Single process/writer. Importing this module performs no I/O or requests.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime, timezone
from uuid import uuid4

from src import arxiv_client, pipeline, s2_integration, http_metrics
from src.pilots import PilotEntry
from src.storage import CorpusStorage, _atomic_json, _pairs


class BatchError(ValueError):
    """Invalid input, changed batch definition, or invalid run history."""


class BatchLogError(RuntimeError):
    """Mandatory logging failed; no further paper may be requested."""


def pilot_pairs(entries):
    """Adapt PilotEntry objects; run_batch still validates every pair."""
    if not isinstance(entries, (list, tuple)):
        raise BatchError("entries must be an explicit list/tuple of PilotEntry")
    if any(not isinstance(entry, PilotEntry) for entry in entries):
        raise BatchError("Each pilot entry must be a PilotEntry")
    return [(entry.mssv, entry.arxiv_id) for entry in entries]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise BatchError("Links/junctions are not allowed in log paths")
    if path.exists():
        info = path.stat()
        if not (stat.S_ISDIR(info.st_mode) or
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise BatchError("Unsafe log path")
    return path.resolve()


def _read(path):
    try:
        path = _safe(path)
        if not path.is_file():
            raise BatchError("Missing run log")
        with path.open(encoding="utf-8") as stream:
            def nonfinite(value):
                raise BatchError("Nonfinite run log")
            value = json.load(stream, object_pairs_hook=_pairs, parse_constant=nonfinite)
        if not isinstance(value, dict):
            raise BatchError("Run log must be an object")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise BatchError("Cannot read valid run log: " + path.name) from exc


def _save(path, value):
    try:
        _atomic_json(_safe(path), value)
    except Exception as exc:
        raise BatchLogError("Mandatory run log write failed") from exc


def _stats(rows):
    done = [row["result"] for row in rows if row["state"] == "processed"]
    discovery = {}
    source = {}
    statuses = {}
    for result in done:
        action = result["discovery"]["action"]
        discovery[action] = discovery.get(action, 0) + 1
        state = result["paper_status"] or "unknown"
        statuses[state] = statuses.get(state, 0) + 1
        for record in result["source"].values():
            action = record["action"]
            source[action] = source.get(action, 0) + 1
    return dict(
        selected_papers=len(rows), processed_papers=len(done),
        unprocessed_papers=len(rows) - len(done),
        discovery_actions=discovery, source_actions=source,
        paper_statuses=statuses,
        http_requests=None, http_errors=None,
        http_metrics_status="not_collected",
    )


def _require(condition):
    if not condition:
        raise BatchError("Invalid run history")


def _history(directory, batch_id, selection):
    history = []
    try:
        paths = sorted(directory.iterdir()) if directory.exists() else []
    except OSError as exc:
        raise BatchError("Cannot inspect attempt history") from exc
    for path in paths:
        if path.suffix != ".json":
            continue  # Preserve temporary evidence from an abruptly killed writer.
        value = _read(path)
        try:
            _require(re.fullmatch(r"[0-9a-f]{32}", path.stem))
            _require(value["schema_version"] == 1 and value["batch_id"] == batch_id)
            _require(value["attempt_id"] == path.stem)
            _require(value["run_id"] == batch_id + "-" + path.stem)
            _require(type(value["attempt_number"]) is int and value["attempt_number"] > 0)
            _require(value["status"] in {"running", "finished", "stopped", "interrupted", "failed"})
            _require(isinstance(value["started_at"], str))
            datetime.fromisoformat(value["started_at"])
            _require((value["ended_at"] is None) == (value["status"] == "running"))
            if value["ended_at"] is not None:
                datetime.fromisoformat(value["ended_at"])
            _require(len(value["papers"]) == len(selection))
            for index, (row, pair) in enumerate(zip(value["papers"], selection)):
                _require(row["index"] == index)
                _require([row["mssv"], row["arxiv_id"]] == pair)
                _require(row["state"] in {"pending", "in_progress", "processed", "interrupted", "failed"})
                _require(row["result"] is None or isinstance(row["result"], dict))
                if row["state"] == "processed":
                    _require(isinstance(row["result"], dict))
            if value["status"] == "finished":
                _require(all(row["state"] == "processed" for row in value["papers"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchError("Invalid attempt history: " + path.name) from exc
        history.append(value)
    history.sort(key=lambda value: value["attempt_number"])
    if [h["attempt_number"] for h in history] != list(range(1, len(history) + 1)):
        raise BatchError("Attempt history has missing or duplicate sequence numbers")
    return history


def run_batch(
    pairs, corpus_root, *, batch_id, max_ids, log_root, resume=False,
    versions=None, timeout=30.0, max_attempts=1,
    user_agent=arxiv_client.USER_AGENT, s2_config=None,
):
    """Run the first max_ids validated pairs, preserving input order.

    Existing batches require resume=True, including an explicitly authorized
    restart after 403/429. Every invocation uses Storage/checksum again.
    Log failures and interrupts propagate. No automatic outer retries.
    """
    if (not isinstance(batch_id, str) or
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", batch_id) is None or
            batch_id.upper() in {"CON", "PRN", "AUX", "NUL"} |
            {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}):
        raise BatchError("Invalid batch_id")
    if type(max_ids) is not int or max_ids < 1:
        raise BatchError("max_ids must be a positive integer")
    if type(resume) is not bool:
        raise BatchError("resume must be bool")
    chosen_versions = pipeline._version_selection(versions)
    s2_config = s2_integration.configuration(s2_config)
    store = CorpusStorage(corpus_root)
    validated = pipeline._inputs(pairs, store, timeout, max_attempts, user_agent)
    selection = [list(pair) for pair in validated[:max_ids]]
    logs = _safe(log_root)
    corpus = store.root
    if logs == corpus or logs.is_relative_to(corpus) or corpus.is_relative_to(logs):
        raise BatchError("Log root and corpus root must be separate directories")
    directory = _safe(logs / batch_id)
    manifest_path = directory / "batch.json"
    attempts = _safe(directory / "attempts")
    definition = dict(
        input_pairs=[list(pair) for pair in validated], selection=selection,
        corpus_root=os.path.normcase(str(corpus)), max_ids=max_ids,
        versions=None if chosen_versions is None else sorted(chosen_versions, key=lambda v: int(v[1:])),
        timeout=float(timeout), max_attempts=max_attempts, user_agent=user_agent,
    )
    if s2_config is not None:
        definition["s2"] = s2_config
    if directory.exists():
        manifest = _read(manifest_path)
        if (manifest.get("schema_version") != 1 or manifest.get("batch_id") != batch_id or
                manifest.get("definition") != definition):
            raise BatchError("Batch definition changed; create a new batch_id")
        if not resume:
            raise BatchError("Existing batch requires an explicit resume=True")
        history = _history(attempts, batch_id, selection)
    else:
        if resume:
            raise BatchError("Cannot resume a missing batch")
        history = []
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise BatchLogError("Cannot create batch log directory") from exc
        _save(manifest_path, dict(
            schema_version=1, batch_id=batch_id, created_at=_now(), definition=definition,
        ))
    try:
        attempts.mkdir(exist_ok=True)
    except OSError as exc:
        raise BatchLogError("Cannot create attempt log directory") from exc
    attempt_id = uuid4().hex
    path = _safe(attempts / (attempt_id + ".json"))
    if path.exists():
        raise BatchError("Attempt ID collision")
    run_id = batch_id + "-" + attempt_id
    started = time.perf_counter()
    value = dict(
        schema_version=1, batch_id=batch_id, attempt_id=attempt_id, run_id=run_id,
        attempt_number=len(history) + 1, started_at=_now(), ended_at=None,
        wall_seconds=0.0, status="running", stopped=False, stop_reason=None, error=None,
        previous_incomplete_attempts=[
            h["attempt_id"] for h in history if h["status"] == "running"
        ],
        papers=[dict(
            index=index, mssv=pair[0], arxiv_id=pair[1], state="pending",
            started_at=None, ended_at=None, wall_seconds=None, result=None,
        ) for index, pair in enumerate(selection)],
    )
    paper_starts = {}
    metrics = http_metrics.HttpMetrics()

    def persist():
        value["wall_seconds"] = max(0.0, time.perf_counter() - started)
        value["stats"] = _stats(value["papers"])
        value["http_metrics"] = metrics.snapshot(complete=value["status"] != "running")
        total = value["http_metrics"]["total"]
        collected = value["http_metrics"]["status"] == "collected"
        value["stats"].update(
            http_requests=total["attempts"] if collected else None,
            http_errors=total["http_errors"] if collected else None,
            http_metrics_status=value["http_metrics"]["status"],
        )
        _save(path, value)

    def progress(event, summary):
        index = len(summary["papers"]) - 1
        row = value["papers"][index]
        row["result"] = copy.deepcopy(summary["papers"][-1])
        value["stopped"] = summary["stopped"]
        value["stop_reason"] = copy.deepcopy(summary.get("stop_reason"))
        if event == "paper_started":
            row.update(state="in_progress", started_at=_now())
            paper_starts[index] = time.perf_counter()
        else:
            row.update(state="processed", ended_at=_now(),
                       wall_seconds=max(0.0, time.perf_counter() - paper_starts[index]))
        persist()

    persist()  # The attempt intent is durable before any HTTP or corpus write.
    try:
        with http_metrics.collect(metrics):
            summary = pipeline.run_selected(
                selection, corpus, run_id=run_id, timeout=timeout,
                max_attempts=max_attempts, user_agent=user_agent,
                versions=definition["versions"], on_progress=progress,
                **({"s2_config": s2_config} if s2_config is not None else {}),
            )
    except BatchLogError:
        raise  # Preserve the last durable snapshot; never continue/retry.
    except BaseException as exc:
        value["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        value["stopped"] = True
        value["error"] = dict(type=type(exc).__name__)
        partial = exc.summary if isinstance(exc, pipeline.PipelineStopped) else None
        if partial:
            value["stop_reason"] = copy.deepcopy(partial.get("stop_reason"))
            for index, result in enumerate(partial["papers"]):
                value["papers"][index]["result"] = copy.deepcopy(result)
        for row in value["papers"]:
            if row["state"] == "in_progress":
                row.update(state=value["status"], ended_at=_now(),
                           wall_seconds=max(0.0, time.perf_counter() - paper_starts[row["index"]]))
        value["ended_at"] = _now()
        persist()
        raise
    value.update(
        status="stopped" if summary["stopped"] else "finished",
        stopped=summary["stopped"], stop_reason=copy.deepcopy(summary.get("stop_reason")),
        ended_at=_now(),
    )
    persist()
    return copy.deepcopy(value)

"""Sequential integration for explicitly selected assigned papers.

No requests or output are produced on import. Calls use the existing HTTP
client; offline callers/tests must provide fake HTTP. Single writer only.
"""
import copy
import math
import re
import time

from src import arxiv_client, arxiv_source, semantic_scholar, s2_integration
from src.storage import CorpusStorage, StorageError, TERMINAL, RETRYABLE


class PipelineStopped(RuntimeError):
    """A fatal error stopped the run; summary contains progress so far."""

    def __init__(self, message, summary):
        super().__init__(message)
        self.summary = summary


class _HttpPolicyStop(Exception):
    """Internal signal to stop the batch for a final 403/429 result."""

    def __init__(self, paper, stage, http_status, version, *, checkpoint_saved=True):
        self.reason = dict(
            type="http_policy", stage=stage, http_status=http_status,
            mssv=paper["mssv"], arxiv_id=paper["arxiv_id"], version=version,
            checkpoint_saved=checkpoint_saved,
        )
        super().__init__(f"Batch stopped after HTTP {http_status} at {stage}")


def _inputs(pairs, store, timeout, max_attempts, user_agent):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive finite number")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if (not isinstance(user_agent, str) or not user_agent.strip()
            or not user_agent.isascii() or "\r" in user_agent or "\n" in user_agent):
        raise ValueError("Invalid user_agent header")
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("pairs must be an explicit list or tuple")
    selected, seen = [], set()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Each selection must be (MSSV, base_arxiv_id)")
        mssv, arxiv_id = pair
        # Public Storage validation delegates to ID/config and checks ownership.
        # This returns a path without creating a paper/version directory.
        store.paper_path(mssv, arxiv_id)
        if arxiv_id in seen:
            raise ValueError(f"Duplicate arXiv ID: {arxiv_id}")
        seen.add(arxiv_id)
        selected.append((mssv, arxiv_id))
    return selected


def _version_selection(versions):
    if versions is None:
        return None
    if not isinstance(versions, (list, tuple)) or not versions:
        raise ValueError("versions must be None or a nonempty list/tuple of vN names")
    if any(not isinstance(name, str) or re.fullmatch(r"v[1-9][0-9]*", name) is None
           for name in versions):
        raise ValueError("Each selected version must be a canonical vN name, such as v1")
    selected = frozenset(versions)
    if len(selected) != len(versions):
        raise ValueError("Duplicate version names are not allowed")
    return selected


def _error(paper, stage, kind, message, version=None):
    paper["errors"].append(dict(
        stage=stage, type=kind, message=str(message), version=version,
    ))


def _stop_on_http(paper, stage, http_status, version=None):
    if http_status in (403, 429):
        raise _HttpPolicyStop(paper, stage, http_status, version)


def _checkpoint(paper, manifest):
    paper["paper_status"] = manifest["status"]
    paper["metadata_status"] = manifest["metadata_status"]
    paper["references_status"] = manifest["references_status"]
    paper["discovery"].update(
        status=manifest["discovery_status"], version_status=manifest["version_status"],
    )
    for name, record in manifest["versions"].items():
        source = paper["source"].setdefault(name, dict(action="pending"))
        source.update(status=record["status"], http_status=record["http_status"],
                      attempts=record["attempts"], error=record["error"])
    paper["pending"] = [name for name, record in manifest["versions"].items()
                        if record["status"] == "pending"]


def _cached_snapshot(store, paper):
    provenance = store.read_metadata(paper["mssv"], paper["arxiv_id"])["provenance"]["arxiv"]
    snapshot = provenance.get("snapshot") if isinstance(provenance, dict) else None
    if (not isinstance(snapshot, dict)
            or snapshot.get("status") != paper["discovery"]["status"]):
        raise StorageError("Cached discovery requires matching arXiv provenance and snapshot")
    return snapshot


def _record_http_result(paper, stage, result, save, version=None):
    try:
        manifest = save(result)
    except StorageError as exc:
        if result.get("http_status") in (403, 429):
            # A rejected checkpoint must not allow another HTTP request either.
            paper["blocked"][version or "paper"] = type(exc).__name__
            _error(paper, "storage", type(exc).__name__, exc, version)
            raise _HttpPolicyStop(
                paper, stage, result["http_status"], version, checkpoint_saved=False,
            ) from exc
        raise
    _checkpoint(paper, manifest)
    return manifest


def _run_paper(store, paper, options, versions):
    mssv, arxiv_id = paper["mssv"], paper["arxiv_id"]
    _checkpoint(paper, store.initialize_paper(mssv, arxiv_id))
    if paper["discovery"]["status"] in {"found", "not_found"}:
        paper["discovery"]["action"] = "cached"
        # Reject invalid cached evidence before resume can write recovery state.
        _cached_snapshot(store, paper)
    if "s2_metadata" in paper:
        s2_integration.validate_checkpoint(store, mssv, arxiv_id)
    plan = store.resume(mssv, arxiv_id)
    _checkpoint(paper, plan["manifest"])

    if paper["discovery"]["status"] in {"found", "not_found"}:
        paper["discovery"]["action"] = "cached"
        snapshot = _cached_snapshot(store, paper)
        paper["discovery"].update(wall_seconds=0.0, timing_status="cached")
    else:
        paper["discovery"]["action"] = "queried"
        started = time.perf_counter()
        try:
            snapshot = arxiv_client.query_arxiv(mssv, arxiv_id, **options)
        finally:
            paper["discovery"].update(
                wall_seconds=max(0.0, time.perf_counter() - started), timing_status="measured",
            )
        _record_http_result(paper, "discovery", snapshot, store.record_discovery)
    paper["discovery"].update(
        http_status=snapshot.get("http_status"), attempts=snapshot.get("attempts", 0),
        error=snapshot.get("error"),
    )

    status = paper["discovery"]["status"]
    if status == "not_found":
        return True
    if status != "found":
        _error(paper, "discovery", status, snapshot.get("error") or status)
        _stop_on_http(paper, "discovery", snapshot.get("http_status"))
        return True
    if paper["discovery"]["version_status"] != "resolved":
        paper["blocked"]["discovery"] = "needs_verification"
        _error(paper, "discovery", "needs_verification", "Version list is not resolved")
        return True

    if versions is not None:
        missing = versions.difference(paper["source"])
        if missing:
            for name in sorted(missing):
                paper["blocked"][name] = "not_discovered"
                _error(paper, "discovery", "not_discovered",
                       f"Selected version {name} is not confirmed by discovery", name)
            return True

    # Also checks orphan destinations for newly discovered versions.
    plan = store.resume(mssv, arxiv_id)
    _checkpoint(paper, plan["manifest"])
    paper["skipped"] = list(plan["skip"])
    for name in plan["skip"]:
        paper["source"][name]["action"] = "skipped"
    for name, reason in plan["blocked"].items():
        paper["source"][name]["action"] = "blocked"
        paper["blocked"][name] = reason
        record = plan["manifest"]["versions"][name]
        message = record["error"]["message"] if record["error"] else reason
        _error(paper, "source", reason, message, name)

    for name in sorted(plan["retry"], key=lambda value: int(value[1:])):
        if versions is not None and name not in versions:
            continue
        try:
            destination = store.begin_version(mssv, arxiv_id, name)
            if destination is None:
                paper["source"][name]["action"] = "skipped"
                paper["skipped"].append(name)
                continue
            _checkpoint(paper, store.read_manifest(mssv, arxiv_id))
            paper['source'][name]['action'] = 'in_progress'
            result = arxiv_source.download_source(mssv, arxiv_id + name, destination, **options)
            manifest = _record_http_result(
                paper, "source", result, store.record_source_result, name,
            )
        except StorageError as exc:
            # Storage may already have persisted validation_failed.
            manifest = store.read_manifest(mssv, arxiv_id)
            _checkpoint(paper, manifest)
            state = manifest["versions"][name]["status"]
            reason = state if state == "validation_failed" else type(exc).__name__
            paper["source"][name]["action"] = "blocked"
            paper["blocked"][name] = reason
            _error(paper, "storage", reason, exc, name)
            continue

        state = manifest["versions"][name]["status"]
        paper["source"][name]["action"] = "downloaded" if state in TERMINAL else "failed"
        if state not in TERMINAL:
            _error(paper, "source", state, result["error"] or state, name)
            if state not in RETRYABLE:
                paper["blocked"][name] = state
            if state == "storage_error":
                return False  # Disk/permissions failures can affect all papers.
            _stop_on_http(paper, "source", result.get("http_status"), name)
    return True



def _save_s2_result(store, paper, stage, result, build, save, *, references=False):
    """Preserve the HTTP stop signal even when validation/persistence fails."""
    http = s2_integration.http_status(result, references=references)
    try:
        manifest = save(build())
    except Exception as exc:
        if http in (403, 429):
            paper["blocked"][stage] = "checkpoint_write_failed"
            _error(paper, "storage", type(exc).__name__, "S2 checkpoint was not committed")
            raise _HttpPolicyStop(
                paper, stage, http, None, checkpoint_saved=False,
            ) from None
        raise
    _checkpoint(paper, manifest)
    _stop_on_http(paper, stage, http)


def _run_s2(store, paper, config, limiter):
    mssv, identifier = paper["mssv"], paper["arxiv_id"]
    if paper["discovery"]["status"] != "found":
        return
    # Source corruption/orphans are audit conditions, not permission for more HTTP.
    if any(row["action"] == "blocked" for row in paper["source"].values()):
        for stage in ("s2_metadata", "s2_references"):
            paper[stage]["action"] = "blocked"
            paper["blocked"][stage] = "source_requires_audit"
        return
    metadata = store.read_metadata(mssv, identifier)
    if s2_integration.metadata_cached(metadata):
        paper["s2_metadata"].update(action="cached", status="found")
    else:
        intent = copy.deepcopy(metadata)
        intent["status"] = "in_progress"
        _checkpoint(paper, store.save_metadata(mssv, identifier, intent))
        info = paper["s2_metadata"]
        info.update(action="queried", status="in_progress")
        options = {k: config[k] for k in ("timeout", "max_attempts", "max_response_bytes")}
        options.update({k: config[k] for k in ("max_retry_wait", "backoff_base", "backoff_cap") if k in config})
        result = semantic_scholar.fetch_metadata(
            [identifier], limiter=limiter, require_arxiv_identity=True, **options,
        )[0]
        info.update(status=result["status"], http_status=result["http_status"],
                    attempts=result["attempts"], error=result["error"])
        if result["status"] not in {"found", "not_found"}:
            _error(paper, "s2_metadata", result["status"], result["error"])
        _save_s2_result(
            store, paper, "s2_metadata", result,
            lambda: s2_integration.metadata_snapshot(metadata, result),
            lambda value: store.save_metadata(mssv, identifier, value),
        )
        if result["status"] == "invalid_response":
            paper["blocked"]["s2_metadata"] = "invalid_response"
            return

    previous = store.read_references(mssv, identifier)
    action = s2_integration.references_evidence(previous, identifier, for_resume=True)
    info = paper["s2_references"]
    if action == "cached":
        info.update(action="cached", status="complete")
        return
    if action == "fresh" and previous["status"] != "not_found":
        intent = copy.deepcopy(previous)
        intent["status"] = "in_progress"
        _checkpoint(paper, store.save_references(mssv, identifier, intent))
    info.update(action=action if action == "resumed" else "queried", status="in_progress")
    options = {k: config[k] for k in ("timeout", "max_attempts", "max_response_bytes", "max_pages")}
    options.update({k: config[k] for k in ("max_retry_wait", "backoff_base", "backoff_cap") if k in config})
    result = semantic_scholar.fetch_references(
        identifier, limit=config["reference_limit"], limiter=limiter,
        resume_from=previous if action == "resumed" else None, **options,
    )
    error = result.get("error")
    info.update(status=result["status"], http_status=s2_integration.http_status(result, references=True),
                attempts=(error or {}).get("attempts"), error=error)
    if error:
        _error(paper, "s2_references", error["type"], error["message"])
    try:
        _save_s2_result(
            store, paper, "s2_references", result,
            lambda: s2_integration.reference_result(previous, result),
            lambda value: store.save_references(mssv, identifier, value), references=True,
        )
    except StorageError:
        paper["blocked"]["s2_references"] = "invalid_reference_result"
        _error(paper, "s2_references", "StorageError", "Reference result requires audit")
        # Existing partial snapshots remain byte-for-byte intact on a conflict.
        # Never roll back a result that Storage may already have saved.
        raise


def run_selected(
    pairs, corpus_root, *, run_id=None, timeout=30.0, max_attempts=3,
    user_agent=arxiv_client.USER_AGENT, versions=None, on_progress=None, s2_config=None,
):
    """Process a caller-supplied list of (MSSV, base ID), in order.

    All selections/options are checked before any HTTP or checkpoint writes.
    Duplicate IDs are rejected. There is no range expansion, outer retry loop,
    or CLI. S2 is opt-in via s2_config (None disables it). A resolved checkpoint is reused as-is.
    versions=None processes all versions; a nonempty list/tuple of distinct vN
    names limits source requests while retaining the full discovery/checkpoint.
    An unconfirmed selected name blocks source requests for that paper.

    The returned summary separates operations from contract states: skipped,
    blocked and failed actions never replace a stored version's actual status.
    A final HTTP 403/429 result stops the batch after attempting persistence.
    The summary has stopped=True and stop_reason; intermediate retries are client-owned.
    StorageError blocks one paper/version; fatal I/O or unexpected exceptions
    raise PipelineStopped with a partial summary. KeyboardInterrupt propagates.
    on_progress(event, summary_copy) runs before/after each paper. Callback
    exceptions propagate immediately and prevent the next HTTP operation.
    """
    if on_progress is not None and not callable(on_progress):
        raise ValueError("on_progress must be callable or None")
    versions = _version_selection(versions)
    store = CorpusStorage(corpus_root, run_id=run_id)
    selected = _inputs(pairs, store, timeout, max_attempts, user_agent)
    s2_config = s2_integration.configuration(s2_config)
    if s2_config is not None:
        semantic_scholar._key_from_environment()  # Validate header before any I/O.
    limiter = None if s2_config is None else semantic_scholar.RateLimiter(s2_config["min_interval"])
    options = dict(timeout=timeout, max_attempts=max_attempts, user_agent=user_agent)
    summary = dict(run_id=run_id, papers=[], stopped=False)
    for mssv, arxiv_id in selected:
        paper = dict(
            mssv=mssv, arxiv_id=arxiv_id, paper_status=None,
            metadata_status=None, references_status=None,
            discovery=dict(action="not_attempted", status=None, version_status=None,
                           http_status=None, attempts=0, error=None,
                           wall_seconds=None, timing_status="not_attempted"),
            source={}, skipped=[], pending=[], blocked={}, errors=[],
        )
        if s2_config is not None:
            for stage in ("s2_metadata", "s2_references"):
                paper[stage] = dict(action="not_attempted", status=None, http_status=None,
                                    attempts=None, error=None)
        summary["papers"].append(paper)
        if on_progress is not None:
            on_progress("paper_started", copy.deepcopy(summary))
        try:
            if not _run_paper(store, paper, options, versions):
                summary["stopped"] = True
                raise PipelineStopped("Downloader reported storage_error; audit before continuing", summary)
            if s2_config is not None:
                _run_s2(store, paper, s2_config, limiter)
        except _HttpPolicyStop as exc:
            summary.update(stopped=True, stop_reason=exc.reason)
        except StorageError as exc:
            paper["blocked"]["paper"] = type(exc).__name__
            _error(paper, "checkpoint", type(exc).__name__, exc)
        except PipelineStopped:
            raise
        except Exception as exc:
            summary["stopped"] = True
            stage = "storage" if isinstance(exc, OSError) else "pipeline"
            _error(paper, stage, type(exc).__name__, exc)
            raise PipelineStopped(str(exc), summary) from exc
        if on_progress is not None:
            on_progress("paper_finished", copy.deepcopy(summary))
        if summary["stopped"]:
            break
    return summary

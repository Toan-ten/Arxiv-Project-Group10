"""Resumable Semantic Scholar batching, references and rate-limit handling."""
from __future__ import annotations

import math
import os
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.fast_production import FastProductionRunner, cohort_pairs
from src.session_evidence import SessionBudget, finish_session_envelope, observe_stage
from src.s2_quality import summarize_quality
from src.storage import IntegrityError, StorageError

from .checkpoint import (
    _ensure_directory,
    _ensure_starting_state,
    _member_writer_lock,
    _persist_selection,
    _record_session,
    _session_start,
    _sha256_file,
    _write_json,
)
from .common import (
    M1PhaseError,
    _required_m1_secret,
    build_member_selection,
)
from .s2_evidence import (
    _save_s2_quality,
    _cohort_s2_paths,
    _load_s2_manifest,
    _read_s2_paper,
    _save_cached_evidence,
    _save_s2_metadata,
    _save_s2_references,
)

S2_DEFAULTS = {
    "timeout": 30.0,
    "max_attempts": 8,
    "min_interval": 1.0,
    "reference_limit": 100,
    "max_pages": 1000,
    "max_response_bytes": 10_000_000,
    "max_retry_wait": 600.0,
    "backoff_base": 3.0,
    "backoff_cap": 60.0,
}


S2_METADATA_BATCH_SIZE = 50
S2_ENRICHMENT_BATCH_SIZE = 100
_REQUEST_SCOPE = ContextVar("m1_s2_request_scope", default=None)


@contextmanager
def _s2_request_scope(strategy: str):
    """Keep authenticated pacing/cooldown across cohorts and automatic resumes."""
    from src import semantic_scholar

    limiter = semantic_scholar.RateLimiter(
        S2_DEFAULTS["min_interval"], max_requests_per_window=None,
    )
    token = _REQUEST_SCOPE.set((limiter, strategy))
    try:
        yield
    finally:
        _REQUEST_SCOPE.reset(token)


def _fetch_s2_enrichment_batch(
    manifest: dict, cohort_root: Path, identifiers: list[str], config: dict,
    limiter: Any, checkpoint_write: Callable[[], None],
) -> tuple[bool, int | None, int, list[str]]:
    """Persist only fully validated rows; stop without splitting 403/429."""
    from src import s2_batch, s2_integration

    rows = s2_batch.fetch_enrichment(
        identifiers, batch_size=S2_ENRICHMENT_BATCH_SIZE, limiter=limiter,
        **{name: config[name] for name in (
            "timeout", "max_attempts", "max_response_bytes", "max_retry_wait",
            "backoff_base", "backoff_cap",
        )},
    )
    if (len(rows) != len(identifiers)
            or any(row["metadata"].get("arxiv_id") != identifier
                   for row, identifier in zip(rows, identifiers))):
        raise M1PhaseError("S2 enrichment batch không khớp danh sách ID.")
    # Validate the entire response before the first evidence write.
    for identifier, row in zip(identifiers, rows):
        if (row["metadata"].get("error") or "").startswith("not_attempted_"):
            continue
        s2_integration.metadata_result(row["metadata"], identifier)
        if row["references"] is not None:
            s2_integration.references_evidence(row["references"], identifier)
    stop, affected = None, []
    for identifier, row in zip(identifiers, rows):
        metadata = row["metadata"]
        if (metadata.get("error") or "").startswith("not_attempted_"):
            continue
        _save_s2_metadata(manifest, cohort_root, identifier, metadata)
        if (row["references"] is not None
                and manifest["papers"][identifier]["references_status"] == "not_attempted"):
            _save_s2_references(manifest, cohort_root, identifier, row["references"])
        checkpoint_write()
        if metadata.get("http_status") in {403, 429}:
            stop = metadata["http_status"]
            if stop == 429:
                affected.append(identifier)
    return stop is not None, stop, 1, affected


@contextmanager
def _api_key_environment(api_key: str):
    names = ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
    previous = {name: os.environ.get(name) for name in names}
    value = _required_m1_secret(api_key, "SEMANTIC_SCHOLAR_API_KEY")
    for name in names:
        os.environ[name] = value
    try:
        yield value
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _auto_resume_s2_429_policy(
    enabled: bool,
    max_per_cohort: int,
    max_per_session: int,
    cooldown_min_seconds: float,
    cooldown_max_seconds: float,
) -> dict:
    """Validate bounded automatic resume for terminal S2 429 policy stops."""
    if type(enabled) is not bool:
        raise ValueError("auto_resume_429 phải là bool.")
    for name, value in (("max_per_cohort", max_per_cohort),
                        ("max_per_session", max_per_session)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"auto_resume_429_{name} phải là số nguyên không âm.")
    if (isinstance(cooldown_min_seconds, bool)
            or not isinstance(cooldown_min_seconds, (int, float))
            or not math.isfinite(cooldown_min_seconds)
            or cooldown_min_seconds <= 0):
        raise ValueError("Cooldown S2 tối thiểu phải là số giây hữu hạn dương.")
    if (isinstance(cooldown_max_seconds, bool)
            or not isinstance(cooldown_max_seconds, (int, float))
            or not math.isfinite(cooldown_max_seconds)
            or cooldown_max_seconds < cooldown_min_seconds):
        raise ValueError("Cooldown S2 tối đa phải hữu hạn và không nhỏ hơn cooldown tối thiểu.")
    return {
        "schema_version": 1,
        "enabled": enabled and max_per_cohort > 0 and max_per_session > 0,
        "max_per_cohort": max_per_cohort,
        "max_per_session": max_per_session,
        "cooldown_min_seconds": float(cooldown_min_seconds),
        "cooldown_max_seconds": float(cooldown_max_seconds),
        "trigger": {
            "stages": ["s2_metadata", "s2_references"],
            "http_status": 429,
        },
        "retry_wait_budget_seconds": float(S2_DEFAULTS["max_retry_wait"]),
    }


def _durable_s2_429_checkpoint(
    phase_root: Path,
    selection: dict,
    cohort: int,
    stop_reason: dict,
) -> bool:
    """Verify the S2 checkpoint and the evidence that caused the 429 stop."""
    if (not isinstance(stop_reason, dict)
            or stop_reason.get("type") != "http_policy"
            or stop_reason.get("stage") not in {"s2_metadata", "s2_references"}
            or stop_reason.get("http_status") != 429
            or stop_reason.get("checkpoint_saved") is not True):
        return False
    cohort_root, checkpoint_path = _cohort_s2_paths(phase_root, cohort)
    try:
        manifest = _load_s2_manifest(checkpoint_path, selection, cohort)
        saved_reason = manifest.get("stop_reason") or {}
        if (manifest.get("status") != "partial"
                or manifest.get("stopped") is not True
                or saved_reason.get("type") != "http_policy"
                or saved_reason.get("stage") != stop_reason["stage"]
                or saved_reason.get("http_status") != 429):
            return False
        affected_ids = stop_reason.get("affected_ids")
        if (not isinstance(affected_ids, list) or not affected_ids
                or any(not isinstance(identifier, str) for identifier in affected_ids)
                or len(affected_ids) != len(set(affected_ids))
                or not set(affected_ids).issubset(set(manifest["ids"]))):
            return False
        evidence = {
            identifier: _read_s2_paper(manifest, cohort_root, identifier)
            for identifier in manifest["ids"]
        }
        if stop_reason["stage"] == "s2_metadata":
            return any(
                evidence[identifier][0] is not None
                and evidence[identifier][0].get("http_status") == 429
                for identifier in affected_ids
            )
        if len(affected_ids) != 1:
            return False
        references = evidence[affected_ids[0]][1]
        return (
            isinstance(references, dict)
            and isinstance(references.get("error"), dict)
            and references["error"].get("http_status") == 429
        )
    except (OSError, ValueError, TypeError, KeyError, M1PhaseError, StorageError, IntegrityError):
        return False


def _s2_provider_cooldown_seconds(limiter: Any) -> float:
    """Read the limiter's remaining Retry-After wait without trusting bad values."""
    value = getattr(limiter, "cooldown_remaining_seconds", 0.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return 0.0
    return float(value)


def _reuse_corpus_s2(runner: FastProductionRunner, mssv: str, arxiv_id: str) -> tuple[dict | None, dict | None]:
    """Return only v2 S2 evidence that passes the same validators as fresh data."""
    from src import s2_integration

    try:
        metadata = runner.store.read_metadata(mssv, arxiv_id)
        references = runner.store.read_references(mssv, arxiv_id)
    except (StorageError, IntegrityError, OSError, ValueError):
        return None, None
    provenance = (metadata.get("provenance") or {}).get("semantic_scholar")
    meta_result = None
    if isinstance(provenance, dict) and isinstance(provenance.get("snapshot"), dict):
        try:
            meta_result = s2_integration.metadata_result(provenance["snapshot"], arxiv_id)
        except StorageError:
            meta_result = None
    refs_result = None
    if references.get("status") in {"complete", "not_found"}:
        try:
            s2_integration.references_evidence(references, arxiv_id)
            refs_result = references
        except StorageError:
            refs_result = None
    return meta_result, refs_result


def _fetch_s2_metadata_batch(
    manifest: dict,
    cohort_root: Path,
    identifiers: list[str],
    config: dict,
    limiter: Any,
    checkpoint_write: Callable[[], None],
) -> tuple[bool, int | None, int, list[str]]:
    """Fetch and commit one identity-checked metadata batch, one ID at a time."""
    from src import s2_integration, semantic_scholar

    calls = 0

    def request(chunk: list[str]) -> list[dict]:
        nonlocal calls
        calls += 1
        rows = semantic_scholar.fetch_metadata(
            chunk,
            batch_size=S2_METADATA_BATCH_SIZE,
            limiter=limiter,
            require_arxiv_identity=True,
            **{
                name: config[name]
                for name in (
                    "timeout", "max_attempts", "max_response_bytes",
                    "max_retry_wait", "backoff_base", "backoff_cap",
                )
            },
        )
        if (len(rows) != len(chunk)
                or any(not isinstance(row, dict) or row.get("arxiv_id") != identifier
                       for identifier, row in zip(chunk, rows))):
            raise M1PhaseError("S2 metadata batch không khớp danh sách ID; chưa ghi bằng chứng.")
        if (len(chunk) > 1 and all(
            row.get("status") == "invalid_response"
            or row.get("error") == "http_413_request_too_large"
            for row in rows
        )):
            midpoint = len(chunk) // 2
            left = request(chunk[:midpoint])
            if any(row.get("http_status") in {403, 429} for row in left):
                return left + [
                    {"arxiv_id": identifier, "status": "rate_limited",
                     "http_status": None, "attempts": 0, "paper": None,
                     "error": "not_attempted_after_rate_limit"}
                    for identifier in chunk[midpoint:]
                ]
            return left + request(chunk[midpoint:])
        return rows

    batch_started = time.perf_counter()
    print(
        f"[M1/S2] Metadata batch: bắt đầu {len(identifiers)} ID "
        f"({identifiers[0]}..{identifiers[-1]}).",
        flush=True,
    )
    try:
        responses = request(identifiers)
    except Exception as exc:
        print(
            f"[M1/S2] Metadata batch thất bại sau {time.perf_counter() - batch_started:.1f}s "
            f"({type(exc).__name__}); checkpoint trước đó vẫn được giữ.",
            flush=True,
        )
        raise
    policy_status = None
    policy_ids = []
    for identifier, row in zip(identifiers, responses):
        error = row.get("error")
        if row.get("attempts") == 0 and isinstance(error, str) and error.startswith("not_attempted_"):
            continue
        result = s2_integration.metadata_result(row, identifier)
        _save_s2_metadata(manifest, cohort_root, identifier, result)
        checkpoint_write()
        if result.get("http_status") in {403, 429}:
            policy_status = result["http_status"]
            if result["http_status"] == 429:
                policy_ids.append(identifier)
    status_counts: dict[str, int] = {}
    for row in responses:
        status = row.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    print(
        f"[M1/S2] Metadata batch xong: {len(identifiers)} ID, {calls} request, "
        f"{time.perf_counter() - batch_started:.1f}s; trạng thái {status_counts}.",
        flush=True,
    )
    return policy_status is not None, policy_status, calls, policy_ids


def _process_s2_cohort(
    runner: FastProductionRunner,
    selection: dict,
    cohort: int,
    phase_root: Path,
    s2_key: str,
    budget: SessionBudget,
    session_id: str,
) -> dict:
    from src import s2_integration, semantic_scholar

    cohort_root, checkpoint_path = _cohort_s2_paths(phase_root, cohort)
    _ensure_directory(cohort_root, runner.drive_root)
    _ensure_directory(cohort_root / "papers", runner.drive_root)
    manifest = _load_s2_manifest(checkpoint_path, selection, cohort)
    _write_json(checkpoint_path, manifest)
    config = s2_integration.configuration(S2_DEFAULTS)
    scope = _REQUEST_SCOPE.get()
    limiter = scope[0] if scope is not None else semantic_scholar.RateLimiter(config["min_interval"])
    strategy = scope[1] if scope is not None else "paginated"
    batch_size = S2_ENRICHMENT_BATCH_SIZE if strategy == "batch_m1" else S2_METADATA_BATCH_SIZE
    timing_by_id: dict[str, float] = {}
    prefetched: set[str] = set()
    phase_metrics = {
        "metadata_batch_calls": 0,
        "metadata_fetch_calls": 0,
        "metadata_ids_requested": 0,
        "metadata_batch_wall_seconds": 0.0,
        "references_wall_seconds": 0.0,
        "checkpoint_wall_seconds": 0.0,
    }
    if strategy == "batch_m1":
        phase_metrics.update(enrichment_batch_calls=0, references_paginated_fallbacks=0)

    def checkpoint_write() -> None:
        started_write = time.perf_counter()
        try:
            _write_json(checkpoint_path, manifest)
        finally:
            phase_metrics["checkpoint_wall_seconds"] += max(
                0.0, time.perf_counter() - started_write,
            )

    stopped = False
    stop_reason = None
    attempted = 0
    pairs = [(member, aid) for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    cohort_started = time.perf_counter()
    initially_metadata_complete = sum(
        record.get("metadata_status") in {"found", "not_found"}
        for record in manifest["papers"].values()
    )
    initially_references_complete = sum(
        record.get("references_status") in {"complete", "not_found"}
        for record in manifest["papers"].values()
    )
    print(
        f"[M1/S2] Lô {cohort}: bắt đầu/tiếp tục {len(pairs)} ID; "
        f"đã có metadata {initially_metadata_complete}, references "
        f"{initially_references_complete}; metadata batch tối đa "
        f"{batch_size} ID."
        + (" Chiến lược batch_m1." if strategy == "batch_m1" else ""),
        flush=True,
    )

    with _api_key_environment(s2_key):
        semantic_scholar._key_from_environment()
        for index, (member, arxiv_id) in enumerate(pairs, start=1):
            if index == 1 or (index - 1) % 10 == 0:
                metadata_done = sum(
                    item.get("metadata_status") in {"found", "not_found"}
                    for item in manifest["papers"].values()
                )
                references_done = sum(
                    item.get("references_status") in {"complete", "not_found"}
                    for item in manifest["papers"].values()
                )
                print(
                    f"[M1/S2] Lô {cohort}: đang xử lý ID {index}/{len(pairs)} "
                    f"({arxiv_id}); metadata {metadata_done}/{len(pairs)}, "
                    f"references {references_done}/{len(pairs)}.",
                    flush=True,
                )
            record = manifest["papers"][arxiv_id]
            try:
                metadata_result, references_result = _read_s2_paper(
                    manifest, cohort_root, arxiv_id
                )
            except M1PhaseError:
                raise
            except (StorageError, ValueError, TypeError) as exc:
                raise M1PhaseError(
                    f"S2 checkpoint không hợp lệ cho {arxiv_id}: {type(exc).__name__}; giữ nguyên file."
                ) from exc

            if metadata_result is None and references_result is None:
                cached_meta, cached_refs = _reuse_corpus_s2(runner, member, arxiv_id)
                if cached_meta is not None or cached_refs is not None:
                    _save_cached_evidence(
                        manifest, cohort_root, arxiv_id, cached_meta, cached_refs
                    )
                    metadata_result, references_result = cached_meta, cached_refs
                    checkpoint_write()

            if (
                metadata_result is not None
                and metadata_result["status"] in {"found", "not_found"}
                and references_result is not None
                and references_result["status"] in {"complete", "not_found"}
            ):
                _save_s2_quality(manifest, cohort_root, arxiv_id)
                continue
            if budget.should_stop():
                stopped = True
                stop_reason = {"type": "session_budget", "stage": "s2_before_paper"}
                break

            started = time.perf_counter()
            batch_elapsed = 0.0
            attempted += 1
            try:
                if (arxiv_id not in prefetched
                        and (metadata_result is None
                             or metadata_result["status"] not in {"found", "not_found"})):
                    batch_started = time.perf_counter()
                    try:
                        candidates = []
                        for future_member, future_id in pairs[index - 1:index - 1 + batch_size]:
                            if future_id in prefetched or manifest["papers"][future_id]["metadata_status"] in {"found", "not_found"}:
                                continue
                            future_record = manifest["papers"][future_id]
                            if (future_record["metadata_status"] == "not_attempted"
                                    and future_record["references_status"] == "not_attempted"):
                                cached_meta, cached_refs = _reuse_corpus_s2(
                                    runner, future_member, future_id,
                                )
                                if cached_meta is not None or cached_refs is not None:
                                    _save_cached_evidence(
                                        manifest, cohort_root, future_id, cached_meta, cached_refs,
                                    )
                                    checkpoint_write()
                            if manifest["papers"][future_id]["metadata_status"] not in {"found", "not_found"}:
                                candidates.append(future_id)
                        if candidates:
                            phase_metrics["metadata_batch_calls"] += 1
                            phase_metrics["metadata_ids_requested"] += len(candidates)
                            fetch = (_fetch_s2_enrichment_batch if strategy == "batch_m1"
                                     else _fetch_s2_metadata_batch)
                            if strategy == "batch_m1":
                                phase_metrics["enrichment_batch_calls"] += 1
                            policy_stop, policy_status, fetch_calls, policy_ids = fetch(
                                manifest, cohort_root, candidates,
                                config, limiter, checkpoint_write,
                            )
                            phase_metrics["metadata_fetch_calls"] += fetch_calls
                        else:
                            policy_stop, policy_status = False, None
                    finally:
                        batch_elapsed = max(0.0, time.perf_counter() - batch_started)
                        phase_metrics["metadata_batch_wall_seconds"] += batch_elapsed
                        if candidates:
                            share = batch_elapsed / len(candidates)
                            for candidate in candidates:
                                timing_by_id[candidate] = timing_by_id.get(candidate, 0.0) + share
                            prefetched.update(candidates)
                    metadata_result, references_result = _read_s2_paper(
                        manifest, cohort_root, arxiv_id,
                    )
                    if policy_stop:
                        stopped = True
                        stop_reason = {
                            "type": "http_policy", "stage": "s2_metadata",
                            "http_status": policy_status, "checkpoint_saved": True,
                            "affected_ids": policy_ids,
                            "provider_cooldown_seconds": _s2_provider_cooldown_seconds(limiter),
                        }
                        break

                if budget.should_stop():
                    stopped = True
                    stop_reason = {"type": "session_budget", "stage": "s2_before_references"}
                    break

                if references_result is None or references_result.get("status") not in {"complete", "not_found"}:
                    if strategy == "batch_m1":
                        phase_metrics["references_paginated_fallbacks"] += 1
                    previous_snapshot = references_result
                    reference_started = time.perf_counter()
                    try:
                        reference_response = semantic_scholar.fetch_references(
                            arxiv_id,
                            limit=config["reference_limit"],
                            max_pages=config["max_pages"],
                            timeout=config["timeout"],
                            max_attempts=config["max_attempts"],
                            limiter=limiter,
                            max_response_bytes=config["max_response_bytes"],
                            resume_from=previous_snapshot if previous_snapshot else None,
                            max_retry_wait=config["max_retry_wait"],
                            backoff_base=config["backoff_base"],
                            backoff_cap=config["backoff_cap"],
                        )
                    finally:
                        phase_metrics["references_wall_seconds"] += max(
                            0.0, time.perf_counter() - reference_started,
                        )
                    s2_integration.references_evidence(
                        reference_response, arxiv_id, for_resume=True
                    )
                    _save_s2_references(manifest, cohort_root, arxiv_id, reference_response)
                    references_result = reference_response
                    checkpoint_write()
                    http_status = (reference_response.get("error") or {}).get("http_status")
                    if http_status in {403, 429}:
                        stopped = True
                        stop_reason = {
                            "type": "http_policy", "stage": "s2_references",
                            "http_status": http_status,
                            "checkpoint_saved": True,
                            "affected_ids": [arxiv_id],
                            "provider_cooldown_seconds": _s2_provider_cooldown_seconds(limiter),
                        }
                        break
            except KeyboardInterrupt:
                stopped = True
                stop_reason = {"type": "interrupted", "stage": "s2"}
                break
            finally:
                elapsed = max(0.0, time.perf_counter() - started - batch_elapsed)
                timing_by_id[arxiv_id] = timing_by_id.get(arxiv_id, 0.0) + elapsed
                checkpoint_write()
            if stopped:
                break

    statuses = manifest["papers"].values()
    metadata_complete = sum(item.get("metadata_status") in {"found", "not_found"} for item in statuses)
    references_complete = sum(item.get("references_status") in {"complete", "not_found"} for item in statuses)
    complete = metadata_complete == len(pairs) and references_complete == len(pairs)
    quality = summarize_quality({
        aid: _save_s2_quality(manifest, cohort_root, aid)
        for aid, record in manifest["papers"].items()
        if record.get("references_status") != "not_attempted"
    })
    manifest.update(
        status="complete" if complete else "partial",
        updated_at=datetime.now(timezone.utc).isoformat(),
        stopped=stopped,
        stop_reason=stop_reason,
        session_id=session_id,
        metadata_complete=metadata_complete,
        references_complete=references_complete,
        reference_quality=quality,
    )
    _write_json(checkpoint_path, manifest)
    print(
        f"[M1/S2] Lô {cohort}: kết thúc lượt trong "
        f"{time.perf_counter() - cohort_started:.1f}s; metadata "
        f"{metadata_complete}/{len(pairs)}, references "
        f"{references_complete}/{len(pairs)}; trạng thái {manifest['status']}.",
        flush=True,
    )
    return {
        "status": manifest["status"],
        "cohort": cohort,
        "attempted_papers": attempted,
        "metadata_complete": metadata_complete,
        "references_complete": references_complete,
        "reference_quality": quality,
        "fully_enriched": sum(
            item.get("metadata_status") == "found"
            and item.get("references_status") == "complete"
            for item in manifest["papers"].values()
        ),
        "stopped": stopped,
        "stop_reason": stop_reason,
        "paper_timings": [
            {"arxiv_id": identifier, "s2_wall_seconds": seconds}
            for identifier, seconds in timing_by_id.items()
        ],
        "phase_metrics": phase_metrics,
        "selection_fingerprint": selection["fingerprint"],
        "checkpoint_path": str(checkpoint_path),
    }


def run_m1_s2_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    semantic_scholar_api_key: str,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
    auto_resume_429: bool = False,
    auto_resume_429_max_per_cohort: int = 1,
    auto_resume_429_max_per_session: int = 2,
    auto_resume_429_cooldown_min_seconds: float = 300.0,
    auto_resume_429_cooldown_max_seconds: float = 600.0,
    reference_strategy: str = "paginated",
    runner_factory: Callable[..., FastProductionRunner],
) -> dict:
    """Lấy metadata/references S2; có thể tự tiếp tục hữu hạn sau HTTP 429."""
    if reference_strategy not in {"paginated", "batch_m1"}:
        raise ValueError("reference_strategy must be paginated or batch_m1")
    semantic_scholar_api_key = _required_m1_secret(
        semantic_scholar_api_key, "SEMANTIC_SCHOLAR_API_KEY",
    )
    retry_policy = _auto_resume_s2_429_policy(
        auto_resume_429,
        auto_resume_429_max_per_cohort,
        auto_resume_429_max_per_session,
        auto_resume_429_cooldown_min_seconds,
        auto_resume_429_cooldown_max_seconds,
    )
    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = runner_factory(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "s2")
    phase_root = _persist_selection(drive, selection)
    cohort_results = {}
    stopped = False
    stop_reason = None
    automatic_resumes = 0
    automatic_cooldown_seconds = 0.0
    auto_events: list[dict] = []
    auto_events_path = None

    def persist_auto_events() -> None:
        if auto_events_path is not None:
            payload = {
                "schema_version": 1,
                "stage": "s2",
                "mssv": selection["mssv"],
                "selection_fingerprint": selection["fingerprint"],
                "session_id": session_id,
                "policy": retry_policy,
                "events": auto_events,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(auto_events_path, payload)

    try:
        with _member_writer_lock(drive, selection), _s2_request_scope(reference_strategy):
            _record_session(phase_root, selection, session_id, "s2")
            _ensure_starting_state(runner, phase_root, selection)
            if retry_policy["enabled"]:
                event_root = phase_root / "s2" / "auto_resume_429"
                _ensure_directory(event_root, drive)
                auto_events_path = event_root / f"{session_id}.json"
                if auto_events_path.exists() or auto_events_path.is_symlink():
                    raise M1PhaseError("S2 auto-resume evidence path already exists; preserve it for inspection.")
                persist_auto_events()
            for cohort in selection["cohorts"]:
                cohort_resume_count = 0
                pending_resume_event = None
                while True:
                    result = observe_stage(
                        runner,
                        "s2",
                        cohort,
                        lambda number=cohort: _process_s2_cohort(
                            runner, selection, number, phase_root,
                            semantic_scholar_api_key, budget, session_id,
                        ),
                        session_id=session_id,
                        configuration={
                            "selection_fingerprint": selection["fingerprint"],
                            "mssv": selection["mssv"],
                            "run_scope": selection["scope"],
                            "authenticated": bool(semantic_scholar_api_key),
                            "metadata_batch_size": S2_METADATA_BATCH_SIZE,
                            "s2_min_interval": S2_DEFAULTS["min_interval"],
                            "max_attempts": S2_DEFAULTS["max_attempts"],
                            "max_retry_wait": S2_DEFAULTS["max_retry_wait"],
                            "backoff_base": S2_DEFAULTS["backoff_base"],
                            "backoff_cap": S2_DEFAULTS["backoff_cap"],
                            "auto_resume_429_policy": retry_policy,
                            "session_budget_seconds": budget_seconds,
                            **({"reference_strategy": reference_strategy,
                                "enrichment_batch_size": S2_ENRICHMENT_BATCH_SIZE,
                                "relation_fields_collected": False,
                                "limiter_scope": "session",
                                "authenticated_window_cap": None}
                               if reference_strategy == "batch_m1" else {}),
                        },
                    )
                    reason = result.get("stop_reason") or {}
                    if pending_resume_event is not None:
                        pending_resume_event.update(
                            outcome="resumed",
                            resumed_status=result.get("status"),
                            resumed_stop_reason=reason or None,
                        )
                        persist_auto_events()
                        pending_resume_event = None

                    is_s2_429 = (
                        retry_policy["enabled"]
                        and result.get("stopped") is True
                        and reason.get("type") == "http_policy"
                        and reason.get("stage") in {"s2_metadata", "s2_references"}
                        and reason.get("http_status") == 429
                    )
                    if not is_s2_429:
                        break

                    affected_ids = reason.get("affected_ids")
                    event = {
                        "session_id": session_id,
                        "cohort": cohort,
                        "stage": reason["stage"],
                        "http_status": 429,
                        "affected_ids": affected_ids if isinstance(affected_ids, list) else [],
                        "checkpoint_saved": False,
                        "outcome": "not_resumed",
                    }
                    auto_events.append(event)
                    try:
                        event["checkpoint_saved"] = _durable_s2_429_checkpoint(
                            phase_root, selection, cohort, reason,
                        )
                    except (OSError, ValueError, TypeError, KeyError,
                            M1PhaseError, StorageError, IntegrityError):
                        event["checkpoint_saved"] = False
                        event["checkpoint_validation"] = "failed"
                    if not event["checkpoint_saved"]:
                        event["deferred_reason"] = "checkpoint_missing_or_invalid"
                        persist_auto_events()
                        break
                    if cohort_resume_count >= retry_policy["max_per_cohort"]:
                        event["deferred_reason"] = "cohort_resume_limit"
                        persist_auto_events()
                        break
                    if automatic_resumes >= retry_policy["max_per_session"]:
                        event["deferred_reason"] = "session_resume_limit"
                        persist_auto_events()
                        break

                    provider_cooldown = reason.get("provider_cooldown_seconds", 0.0)
                    if (isinstance(provider_cooldown, bool)
                            or not isinstance(provider_cooldown, (int, float))
                            or not math.isfinite(provider_cooldown)
                            or provider_cooldown < 0):
                        event["deferred_reason"] = "provider_cooldown_evidence_invalid"
                        persist_auto_events()
                        break
                    cooldown_seconds = max(
                        random.uniform(
                            retry_policy["cooldown_min_seconds"],
                            retry_policy["cooldown_max_seconds"],
                        ),
                        float(provider_cooldown),
                    )
                    required_budget = (
                        cooldown_seconds + S2_DEFAULTS["max_retry_wait"] + 30.0
                    )
                    if budget.remaining_seconds < required_budget:
                        event.update(
                            deferred_reason="session_budget",
                            cooldown_seconds=cooldown_seconds,
                            provider_cooldown_seconds=float(provider_cooldown),
                        )
                        persist_auto_events()
                        break

                    retry_not_before = datetime.fromtimestamp(
                        time.time() + cooldown_seconds, timezone.utc,
                    ).isoformat()
                    event.update(
                        outcome="cooldown_scheduled",
                        cooldown_seconds=cooldown_seconds,
                        provider_cooldown_seconds=float(provider_cooldown),
                        retry_not_before_utc=retry_not_before,
                        resume_attempt_number=cohort_resume_count + 1,
                    )
                    persist_auto_events()
                    display_ids = event["affected_ids"]
                    affected_label = (
                        f"{len(display_ids)} ID metadata"
                        if reason["stage"] == "s2_metadata"
                        else ", ".join(display_ids)
                    )
                    print(
                        f"[Semantic Scholar 429] {reason['stage']} ({affected_label}): "
                        f"checkpoint đã xác minh; tự resume sau {cooldown_seconds:.0f}s "
                        f"(lần {cohort_resume_count + 1}/{retry_policy['max_per_cohort']} "
                        f"của lô, {automatic_resumes + 1}/"
                        f"{retry_policy['max_per_session']} của phiên).",
                        flush=True,
                    )
                    wait_started = time.perf_counter()

                    def wait_for_s2_cooldown() -> dict:
                        time.sleep(cooldown_seconds)
                        return {
                            "status": "waited",
                            "retry_wait_seconds": max(0.0, time.perf_counter() - wait_started),
                            "affected_ids": list(event["affected_ids"]),
                            "trigger_stage": reason["stage"],
                            "trigger_http_status": 429,
                            "resume_attempt_number": cohort_resume_count + 1,
                        }

                    wait_result = observe_stage(
                        runner,
                        "s2_retry_wait",
                        cohort,
                        wait_for_s2_cooldown,
                        session_id=session_id,
                        configuration={
                            "selection_fingerprint": selection["fingerprint"],
                            "mssv": selection["mssv"],
                            "affected_ids": list(event["affected_ids"]),
                            "trigger_stage": reason["stage"],
                            "trigger_http_status": 429,
                            "resume_attempt_number": cohort_resume_count + 1,
                            "scheduled_seconds": cooldown_seconds,
                            "provider_cooldown_seconds": float(provider_cooldown),
                            "retry_not_before_utc": retry_not_before,
                        },
                    )
                    measured_cooldown = float(wait_result["retry_wait_seconds"])
                    event.update(
                        outcome="cooldown_complete",
                        measured_cooldown_seconds=measured_cooldown,
                    )
                    automatic_cooldown_seconds += measured_cooldown
                    if budget.should_stop():
                        event["deferred_reason"] = "session_budget_after_cooldown"
                        persist_auto_events()
                        break

                    cohort_resume_count += 1
                    automatic_resumes += 1
                    event["outcome"] = "resume_started"
                    pending_resume_event = event
                    persist_auto_events()
                    continue

                cohort_results[str(cohort)] = result
                result["auto_resume_429"] = {
                    "policy": retry_policy,
                    "resume_attempts": cohort_resume_count,
                    "cooldown_seconds": sum(
                        item.get("measured_cooldown_seconds", 0.0)
                        for item in auto_events if item.get("cohort") == cohort
                    ),
                    "events": [item for item in auto_events if item.get("cohort") == cohort],
                }
                if result.get("stopped"):
                    stopped = True
                    stop_reason = result.get("stop_reason")
                    break
        return {
            "stage": "s2",
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "expected_id_count": selection["expected_count"],
            "metadata_complete": sum(r.get("metadata_complete", 0) for r in cohort_results.values()),
            "references_complete": sum(r.get("references_complete", 0) for r in cohort_results.values()),
            "reference_quality": summarize_quality({
                aid: assessment for r in cohort_results.values()
                for aid, assessment in r.get("reference_quality", {}).get("papers", {}).items()
            }),
            "cohorts": cohort_results,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "m1_complete": False,
            "checkpoint_root": str(phase_root / "s2"),
            "session_id": session_id,
            "auto_resume_429": {
                "policy": retry_policy,
                "resume_attempts": automatic_resumes,
                "cooldown_seconds": automatic_cooldown_seconds,
                "events": auto_events,
                "events_path": str(auto_events_path) if auto_events_path else None,
                "events_sha256": _sha256_file(auto_events_path) if auto_events_path else None,
            },
        }
    finally:
        finish_session_envelope(envelope, started_monotonic=started)

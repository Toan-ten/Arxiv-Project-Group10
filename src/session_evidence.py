"""Durable, scoped observations for separate Colab pipeline stages.

Observations describe measured intervals, not a complete benchmark by themselves.
"""
from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.benchmark_metrics import ResourceSampler
from src.storage import _atomic_json


class SessionBudget:
    """Soft wall-clock budget checked between papers, never during a request."""

    def __init__(self, seconds: float = 10 * 60 * 60, *, clock=time.monotonic):
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds <= 0):
            raise ValueError("session budget must be positive seconds")
        self.seconds = float(seconds)
        self._clock = clock
        self.started = clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self.started)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.seconds - self.elapsed_seconds)

    def should_stop(self) -> bool:
        return self.remaining_seconds <= 0

    def snapshot(self) -> dict:
        return dict(budget_seconds=self.seconds, elapsed_seconds=self.elapsed_seconds,
                    remaining_seconds=self.remaining_seconds, stop_requested=self.should_stop())


def load_campaign_state(path: str | Path, *, mssv: str, cohorts: list[int]) -> dict:
    """Load compatible durable campaign state; preserve unknown prior fields."""
    path = Path(path)
    if not path.exists():
        return dict(schema_version=1, mssv=mssv, cohorts={str(c): {"status": "not_started"} for c in cohorts})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("campaign state is unreadable; preserve and inspect it") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("mssv") != mssv or not isinstance(value.get("cohorts"), dict)):
        raise ValueError("campaign state identity/schema mismatch; preserve and inspect it")
    states = value["cohorts"]
    for cohort in cohorts:
        states.setdefault(str(cohort), {"status": "not_started"})
    return value


def save_campaign_state(path: str | Path, state: dict, *, cohort: int, update: dict) -> None:
    """Atomically update one cohort without dropping history from other cohorts."""
    current = dict(state)
    if Path(path).exists():
        persisted = load_campaign_state(path, mssv=state["mssv"], cohorts=[])
        current["cohorts"] = {**current.get("cohorts", {}), **persisted.get("cohorts", {})}
    current.setdefault("cohorts", {}).setdefault(str(cohort), {}).update(update)
    current["updated_at"] = _now()
    _atomic_json(Path(path), current)
    state.clear()
    state.update(current)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_session_envelope(report_root: str | Path, session_id: str) -> Path:
    """Persist a session start so abrupt termination remains visible as incomplete."""
    root = Path(report_root) / "session_envelopes"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session_id}.json"
    if path.exists():
        raise ValueError("session envelope already exists; create a new session id")
    _atomic_json(path, dict(schema_version=1, session_id=session_id, started_at=_now(),
                            finished_at=None, wall_seconds=None, status="running"))
    return path


def annotate_session_envelope(path: str | Path, **metadata: Any) -> None:
    """Attach campaign identity without replacing the original start boundary."""
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("session envelope is unreadable") from exc
    if not isinstance(value, dict) or value.get("status") != "running":
        raise ValueError("session envelope is not running")
    value.update(metadata)
    _atomic_json(path, value)


def finish_session_envelope(path: str | Path, *, started_monotonic: float,
                            clock=time.perf_counter) -> dict:
    """Close a measured session envelope; duration includes all in-session waits."""
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("session envelope is unreadable") from exc
    if not isinstance(value, dict) or value.get("status") != "running":
        raise ValueError("session envelope is not running")
    value.update(finished_at=_now(), wall_seconds=max(0.0, clock() - started_monotonic), status="finished")
    _atomic_json(path, value)
    return value


def observe_stage(runner: Any, stage: str, cohort: int, operation: Callable[[], Any], *,
                  session_id: str, configuration: dict | None = None) -> Any:
    """Measure an invocation and persist its evidence, including on exceptions.

    Values in configuration must never contain credentials. The interval excludes
    setup and evidence writes; session gaps and unobserved prior work are unknown.
    """
    roots = [runner.corpus_dir]
    candidates = [runner.scratch_dir]
    drive_root = getattr(runner, "drive_root", None)
    if drive_root is not None:
        drive_root = Path(drive_root).resolve()
        candidates.extend([drive_root, drive_root.parent / "shared" / "kaggle_snapshot"])
    for candidate in candidates:
        candidate = Path(candidate).resolve()
        if all(not candidate.is_relative_to(root) and not root.is_relative_to(candidate) for root in roots):
            roots.append(candidate)
    sampler = ResourceSampler(runner.corpus_dir, disk_roots=roots)
    destination = runner.reports_dir / "stage_observations"
    destination.mkdir(parents=True, exist_ok=True)
    observation_id = uuid.uuid4().hex
    target = destination / f"{session_id}_{cohort:04d}_{stage}_{observation_id}.json"
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    record = dict(schema_version=1, observation_id=observation_id, session_id=session_id,
                  stage=stage, cohort=cohort, started_at=_now(), finished_at=None,
                  execution_status="running", commit=commit, configuration=configuration or {},
                  environment=dict(python=platform.python_version(), platform=platform.platform(),
                                   cpu_count=os.cpu_count()),
                  wall_seconds=None, wall_scope="operation only; includes waits, excludes observation setup and writes",
                  resources=None, result=None, error_type=None,
                  completeness="partial; other stages, sessions and setup require separate evidence")
    _atomic_json(target, record)
    started = time.perf_counter()
    try:
        sampler.start()
        started = time.perf_counter()
        result = operation()
        record["execution_status"] = "returned"
        if isinstance(result, dict):
            # Danh sách cho phép tường minh ngăn lưu thông tin xác thực hoặc bản ghi bài báo lớn.
            record["result"] = {k: result[k] for k in ("status", "reason", "source",
                                "download_seconds", "extraction_seconds",
                                "json_validation_seconds", "checksum_seconds", "publish_seconds",
                                "validation_seconds", "snapshot_prepare_seconds",
                                "snapshot_precheck_seconds", "index_checksum_seconds",
                                "checksum_in_scan", "index_scan_seconds", "index_scanned_lines",
                                "snapshot_size_bytes",
                                "candidate_count", "snapshot_sha256", "reused", "stopped", "stop_reason", "attempted_papers",
                                "metadata_complete", "references_complete", "fully_enriched", "archive_state",
                                "is_reconciled", "integrity_failed_papers", "is_cohort_complete",
                                "corpus_size_bytes", "archive_size_bytes",
                                "export_size_bytes", "paper_timings", "phase_metrics",
                                "retry_wait_seconds", "arxiv_id", "trigger_stage",
                                "trigger_http_status", "resume_attempt_number",
                                "affected_ids", "provider_cooldown_seconds") if k in result}
            for key in ("reference_quality", "reference_dates"):
                if key in result:
                    record["result"][key] = result[key]
            if isinstance(result.get("fast_runner"), dict):
                record["result"]["fast_runner"] = result["fast_runner"]
            if stage == "source" and isinstance(result.get("timing_metrics"), dict):
                record["result"]["timing_metrics"] = result["timing_metrics"]
        return result
    except BaseException as exc:
        record["execution_status"] = "raised"
        record["error_type"] = type(exc).__name__
        raise
    finally:
        record["wall_seconds"] = time.perf_counter() - started
        cleanup_started = time.perf_counter()
        # Persist the operation outcome before cleanup can itself be interrupted.
        record["cleanup_status"] = "running"
        _atomic_json(target, record)
        cleanup_interrupt = None
        try:
            sampler.stop()
            record["cleanup_status"] = "finished"
        except (Exception, KeyboardInterrupt) as exc:
            record["cleanup_status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            record["cleanup_error_type"] = type(exc).__name__
            if isinstance(exc, KeyboardInterrupt):
                cleanup_interrupt = exc
        finally:
            record["resources"] = sampler.report()
            record["finished_at"] = _now()
            record["cleanup_seconds"] = time.perf_counter() - cleanup_started
            _atomic_json(target, record)
        if cleanup_interrupt is not None and record["execution_status"] != "raised":
            raise cleanup_interrupt


def revision_history_counts(runner: Any, cohort: int) -> dict:
    """Count evidence-verified revision histories for discovered papers, offline."""
    from src.arxiv_history import _cached, _single_version_cached
    from src.fast_production import cohort_pairs

    counts = dict(verified=0, missing_or_unverified=0, invalid=0)
    for mssv, arxiv_id in cohort_pairs(cohort):
        try:
            manifest = runner.store.read_manifest(mssv, arxiv_id)
            if manifest.get("discovery_status") != "found":
                continue
            metadata = runner.store.read_metadata(mssv, arxiv_id)
            verified = _cached(metadata) or _single_version_cached(metadata)
            counts["verified" if verified else "missing_or_unverified"] += 1
        except Exception:
            counts["invalid"] += 1
    return counts


def summarize_observations(records: list[dict]) -> dict:
    """Summarize nonoverlapping observations without claiming end-to-end coverage.

    Intervals with missing ends, repeated IDs or overlapping clocks cannot be
    combined safely. Time between sessions and unobserved work remain unknown.
    """
    from datetime import datetime
    import math

    seen = set()
    intervals = []
    for record in records:
        try:
            identifier = record['observation_id']
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError('duplicate or invalid observation')
            seen.add(identifier)
            start = datetime.fromisoformat(record['started_at'])
            end = datetime.fromisoformat(record['finished_at'])
            seconds = record['wall_seconds']
            if (start.utcoffset() is None or end.utcoffset() is None or end < start
                    or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                    or record['execution_status'] not in {'returned', 'raised'}):
                raise ValueError('incomplete observation')
            intervals.append((start, end, seconds))
        except (KeyError, TypeError, ValueError):
            return dict(status='incomplete', measured_wall_seconds=None,
                        reason='invalid, duplicate or unfinished observation', end_to_end_complete=False)
    intervals.sort(key=lambda item: item[0])
    if any(curr[0] < prev[1] for prev, curr in zip(intervals, intervals[1:])):
        return dict(status='incomplete', measured_wall_seconds=None,
                    reason='overlapping intervals', end_to_end_complete=False)
    return dict(status='partial' if intervals else 'not_collected',
                measured_wall_seconds=sum(item[2] for item in intervals) if intervals else None,
                reason='only observed operation intervals; setup, gaps and other stages are not certified',
                end_to_end_complete=False)


def summarize_discovery_timings(records: list[dict], expected_ids: list[str] | None) -> dict:
    """Summarize arXiv discovery, retry waits and Kaggle preparation."""
    import math

    total, arxiv_total, kaggle_total, retry_wait_total = 0.0, 0.0, 0.0, 0.0
    seen, processed = {}, set()
    valid = arxiv_valid = kaggle_valid = True
    kaggle_observations = []
    for record in records:
        stage = record.get("stage")
        if stage == "arxiv_retry_wait":
            configuration = record.get("configuration") or {}
            result = record.get("result") or {}
            seconds = record.get("wall_seconds")
            valid_measurement = (
                configuration.get("trigger_stage") == "discovery"
                and configuration.get("trigger_http_status") == 429
                and isinstance(configuration.get("arxiv_id"), str)
                and type(seconds) in (int, float)
                and math.isfinite(seconds) and seconds >= 0
            )
            if not valid_measurement:
                arxiv_valid = False
                valid = False
                continue
            retry_wait_total += seconds
            arxiv_total += seconds
            total += seconds
            valid_return = (
                record.get("execution_status") == "returned"
                and type(result.get("retry_wait_seconds")) in (int, float)
                and math.isfinite(result["retry_wait_seconds"])
                and result["retry_wait_seconds"] >= 0
                and result.get("arxiv_id") == configuration.get("arxiv_id")
                and result.get("trigger_stage") == "discovery"
                and result.get("trigger_http_status") == 429
            )
            if not valid_return:
                arxiv_valid = False
                valid = False
            continue
        if stage in {"snapshot_prepare", "kaggle_index"}:
            kaggle_observations.append(record)
            seconds = record.get("wall_seconds")
            if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                    or seconds < 0):
                kaggle_valid = False
                valid = False
            else:
                kaggle_total += seconds
                if record.get("execution_status") == "returned":
                    total += seconds
                else:
                    kaggle_valid = False
                    valid = False
        if stage != "source":
            continue
        timing = (record.get("result") or {}).get("timing_metrics")
        if not isinstance(timing, dict) or not isinstance(timing.get("paper_intervals"), list):
            arxiv_valid = False
            valid = False
            continue
        for row in timing["paper_intervals"]:
            key = (record.get("cohort"), row.get("attempt_id"), row.get("arxiv_id"))
            if not all(key):
                arxiv_valid = False
                valid = False
                continue
            if key in seen:
                if seen[key] != row:
                    arxiv_valid = False
                    valid = False
                continue
            seen[key] = row
            if row.get("state") == "processed":
                processed.add(key[2])
            for action_key, seconds_key in (("discovery_action", "discovery_seconds"),
                                            ("revision_action", "revision_seconds")):
                action, seconds = row.get(action_key), row.get(seconds_key)
                if action == "cached" and seconds is None:
                    seconds = 0.0
                if (action_key == "revision_action"
                        and row.get("discovery_status") == "not_found"
                        and action in (None, "not_attempted", "skipped")):
                    continue
                if (action in (None, "not_attempted")
                        or type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    arxiv_valid = False
                    valid = False
                else:
                    total += seconds
                    arxiv_total += seconds
    expected = set(expected_ids) if expected_ids is not None else None
    complete = bool(expected) and expected <= processed and valid
    arxiv_complete = bool(expected) and expected <= processed and arxiv_valid

    # Mỗi phiên mã nguồn chuẩn bị bản chụp; chỉ dựng chỉ mục khi
    # bước chuẩn bị bản chụp của phiên đó trả về trạng thái sẵn sàng. Nhờ vậy,
    # điểm khôi phục cũ vẫn đọc được nhưng thiếu phép đo chuẩn bị không bị gán là
    # phép đo discovery tổng hợp hoàn chỉnh.
    source_sessions = {
        record.get("session_id") for record in records
        if record.get("stage") == "source" and record.get("session_id")
    }
    prep_by_session: dict[str, dict[str, list[dict]]] = {}
    prep_structure_complete = bool(source_sessions)
    for record in kaggle_observations:
        session_id = record.get("session_id")
        if session_id:
            prep_by_session.setdefault(session_id, {}).setdefault(record["stage"], []).append(record)
        else:
            prep_structure_complete = False
    if set(prep_by_session) - source_sessions:
        prep_structure_complete = False
    for session_id in source_sessions:
        session_prep = prep_by_session.get(session_id, {})
        snapshots = session_prep.get("snapshot_prepare", [])
        if len(snapshots) != 1:
            prep_structure_complete = False
            continue
        snapshot = snapshots[0]
        snapshot_ready = ((snapshot.get("result") or {}).get("status") == "ready")
        indexes = session_prep.get("kaggle_index", [])
        if (snapshot_ready and len(indexes) != 1) or (not snapshot_ready and indexes):
            prep_structure_complete = False
    kaggle_complete = bool(kaggle_observations) and kaggle_valid and prep_structure_complete
    combined_complete = arxiv_complete and kaggle_complete
    combined_known = arxiv_total + kaggle_total
    return {"status": "complete" if complete else "incomplete",
            "wall_seconds": total if complete else None,
            "known_wall_seconds": total, "missing_ids": sorted(expected - processed) if expected is not None else None,
            "scope": ("Legacy aggregate of arXiv discovery/version history plus returned Kaggle snapshot/index "
                      "preparation intervals; use components for explicit scopes."),
            "components": {
                "arxiv_discovery_and_version_history": {
                    "status": "complete" if arxiv_complete else "incomplete",
                    "wall_seconds": arxiv_total if arxiv_complete else None,
                    "known_wall_seconds": arxiv_total,
                    "retry_wait_seconds": retry_wait_total,
                    "retry_wait_observation_count": sum(
                        record.get("stage") == "arxiv_retry_wait" for record in records
                    ),
                    "scope": "arXiv discovery and version history across observed attempts, including bounded automatic 429 cooldowns and internal retries.",
                },
                "kaggle_snapshot_and_index_preparation": {
                    "status": "complete" if kaggle_complete else "incomplete",
                    "wall_seconds": kaggle_total if kaggle_complete else None,
                    "known_wall_seconds": kaggle_total,
                    "observation_count": len(kaggle_observations),
                    "scope": "Observed snapshot preparation and index intervals; fallback attempts are included.",
                },
                "discovery_with_kaggle_preparation": {
                    "status": "complete" if combined_complete else "incomplete",
                    "wall_seconds": combined_known if combined_complete else None,
                    "known_wall_seconds": combined_known,
                    "scope": (
                        "arXiv discovery/version history plus Kaggle snapshot/index preparation; "
                        "includes waits and retries, excludes unrelated archive/audit/export stages."
                    ),
                },
            }}


def summarize_paper_timings(records: list[dict], found_ids: list[str] | None) -> dict:
    """Combine source, discovery retry waits and S2 for the requested ID population.

    ``found_ids`` is retained as the parameter name for compatibility. Callers may
    pass discovered papers for a diagnostic average or all assigned IDs for the
    benchmark denominator.
    """
    import math

    source, source_retry_waits, s2, s2_retry_waits = {}, {}, {}, {}
    seen_source, seen_s2, seen_waits, seen_s2_waits, processed = {}, set(), set(), set(), set()
    valid = True
    for record in records:
        result = record.get("result") or {}
        stage = record.get("stage")
        if stage == "arxiv_retry_wait":
            configuration = record.get("configuration") or {}
            arxiv_id = configuration.get("arxiv_id")
            seconds = record.get("wall_seconds")
            key = (record.get("session_id"), record.get("observation_id"), arxiv_id)
            if (configuration.get("trigger_stage") != "discovery"
                    or configuration.get("trigger_http_status") != 429
                    or not all(key)
                    or key in seen_waits
                    or type(seconds) not in (int, float)
                    or not math.isfinite(seconds) or seconds < 0):
                valid = False
                continue
            seen_waits.add(key)
            source[arxiv_id] = source.get(arxiv_id, 0.0) + seconds
            source_retry_waits[arxiv_id] = source_retry_waits.get(arxiv_id, 0.0) + seconds
            if (record.get("execution_status") != "returned"
                    or result.get("arxiv_id") != arxiv_id
                    or result.get("trigger_stage") != "discovery"
                    or result.get("trigger_http_status") != 429):
                valid = False
        elif stage == "source":
            for row in (result.get("timing_metrics") or {}).get("paper_intervals", []):
                key = (record.get("cohort"), row.get("attempt_id"), row.get("arxiv_id"))
                seconds = row.get("wall_seconds")
                if not all(key) or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                    valid = False
                    continue
                if row.get("state") == "processed":
                    processed.add(key[2])
                if key in seen_source:
                    if seen_source[key] != seconds:
                        valid = False
                    continue
                seen_source[key] = seconds
                source[key[2]] = source.get(key[2], 0.0) + seconds
        elif stage == "s2":
            for row in result.get("paper_timings", []):
                key = (record.get("observation_id"), row.get("arxiv_id"))
                seconds = row.get("s2_wall_seconds")
                if not all(key) or key in seen_s2 or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                    valid = False
                    continue
                seen_s2.add(key)
                s2[key[1]] = s2.get(key[1], 0.0) + seconds
        elif stage == "s2_retry_wait":
            configuration = record.get("configuration") or {}
            affected_ids = configuration.get("affected_ids")
            seconds = record.get("wall_seconds")
            key = (record.get("session_id"), record.get("observation_id"))
            if (configuration.get("trigger_stage") not in {"s2_metadata", "s2_references"}
                    or configuration.get("trigger_http_status") != 429
                    or not isinstance(affected_ids, list) or not affected_ids
                    or any(not isinstance(aid, str) or not aid for aid in affected_ids)
                    or len(affected_ids) != len(set(affected_ids))
                    or not all(key) or key in seen_s2_waits
                    or type(seconds) not in (int, float)
                    or not math.isfinite(seconds) or seconds < 0):
                valid = False
                continue
            wait_result = result
            if (record.get("execution_status") != "returned"
                    or wait_result.get("status") != "waited"
                    or wait_result.get("trigger_stage") != configuration.get("trigger_stage")
                    or wait_result.get("trigger_http_status") != 429
                    or wait_result.get("affected_ids") != affected_ids
                    or type(wait_result.get("retry_wait_seconds")) not in (int, float)
                    or not math.isfinite(wait_result["retry_wait_seconds"])
                    or wait_result["retry_wait_seconds"] < 0):
                valid = False
                continue
            seen_s2_waits.add(key)
            share = float(seconds) / len(affected_ids)
            for aid in affected_ids:
                s2[aid] = s2.get(aid, 0.0) + share
                s2_retry_waits[aid] = s2_retry_waits.get(aid, 0.0) + share
    found = set(found_ids) if found_ids is not None else None
    covered = set(source) & set(s2) & processed
    complete = bool(found) and valid and found <= covered
    items = [{"arxiv_id": aid, "source_seconds": source[aid],
              "retry_wait_seconds": source_retry_waits.get(aid, 0.0),
              "s2_seconds": s2[aid] - s2_retry_waits.get(aid, 0.0),
              "s2_retry_wait_seconds": s2_retry_waits.get(aid, 0.0),
              "total_seconds": source[aid] + s2[aid]}
             for aid in sorted(covered & found if found is not None else covered)]
    return {"status": "complete" if complete else "incomplete", "papers": items,
            "average_seconds": sum(row["total_seconds"] for row in items) / len(items) if complete else None,
            "missing_ids": sorted(found - covered) if found is not None else None,
            "s2_retry_wait_seconds": sum(s2_retry_waits.values()),
            "s2_retry_wait_observation_count": len(seen_s2_waits),
            "scope": "source paper intervals plus S2 request/processing intervals across recorded attempts; "
                     "includes measured source and S2 429 cooldowns, allocating a metadata-batch cooldown "
                     "equally across affected IDs; uses exactly the ID population passed by the caller; "
                     "excludes shared setup, snapshot, archive, audit and export overhead"}


def summarize_workflow(records: list[dict], required: list[tuple[str, int]], *,
                       session_envelopes: list[dict] | None = None) -> dict:
    """Summarize explicitly enumerated operation intervals without inventing data.

    Each required stage/cohort must have at least one completed observation;
    recorded repeats count toward active wall time. Session gaps are excluded.
    """
    timing = summarize_observations(records)

    def completed_observation(record: dict) -> bool:
        if record.get('execution_status') != 'returned':
            return False
        result = record.get('result') or {}
        stage = record.get('stage')
        if result.get('stopped') or (result.get('fast_runner') or {}).get('stopped'):
            return False
        if stage == 'source' and result.get('status') is not None:
            return result.get('status') == 'source_complete'
        if stage == 's2' and result.get('status') is not None:
            return result.get('status') == 'complete'
        if stage == 'audit' and 'is_reconciled' in result:
            return result.get('is_reconciled') is True
        if result.get('status') == 'failed':
            return False
        return result.get('status') != 'fallback' or stage == 'snapshot_prepare'

    observed = {(r.get('stage'), r.get('cohort')) for r in records
                if completed_observation(r)}
    missing = sorted(set(required) - observed)
    import math
    ram_samples = []
    system_ram_samples = []
    system_ram_scopes = set()
    system_ram_observations_complete = bool(records)
    ram_scopes = set()
    disk_scopes = set()
    disk_peaks = []
    resource_complete = bool(records)
    for r in records:
        resources = r.get('resources') or {}
        ram, disk = resources.get('ram') or {}, resources.get('disk') or {}
        system_ram = resources.get('system_ram') or {}
        if (system_ram.get('status') != 'collected'
                or system_ram.get('unit') != 'bytes'):
            system_ram_observations_complete = False
        else:
            count = system_ram.get('sample_count')
            total = system_ram.get('sum_sample_bytes')
            peak = system_ram.get('sampled_peak_bytes')
            source = system_ram.get('source')
            scope = system_ram.get('scope')
            if (type(count) is not int or count <= 0
                    or any(type(value) not in (int, float)
                           or not math.isfinite(value) or value < 0
                           for value in (total, peak))
                    or not isinstance(source, str) or not source
                    or not isinstance(scope, str) or not scope):
                system_ram_observations_complete = False
            else:
                system_ram_samples.append((count, total, peak))
                system_ram_scopes.add((source, scope))
        ram_source = ram.get('source')
        ram_scope = ram.get('scope')
        disk_roots = disk.get('roots')
        if (isinstance(ram_source, str) and ram_source
                and isinstance(ram_scope, str) and ram_scope):
            ram_scopes.add((ram_source, ram_scope))
        if (isinstance(disk_roots, list) and disk_roots
                and all(isinstance(root, str) and root for root in disk_roots)):
            disk_scopes.add(tuple(sorted(disk_roots)))
        if ram.get('status') != 'collected' or disk.get('status') != 'collected':
            resource_complete = False
            continue
        count, total, peak = ram.get('sample_count'), ram.get('sum_sample_bytes'), ram.get('sampled_peak_bytes')
        disk_peak = disk.get('sampled_peak_bytes')
        if type(count) is not int or count <= 0 or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (total, peak, disk_peak)):
            resource_complete = False
            continue
        ram_samples.append((count, total, peak))
        disk_peaks.append(disk_peak)
    resource_scope_complete = len(ram_scopes) == 1 and len(disk_scopes) == 1
    resource_scope_status = 'collected' if resource_scope_complete else 'incomplete'
    system_ram_complete = (
        system_ram_observations_complete
        and bool(system_ram_samples)
        and len(system_ram_scopes) == 1
    )
    complete = (
        bool(required) and not missing
        and timing['measured_wall_seconds'] is not None
        and resource_complete
    )
    system_ram_status = (
        'collected' if system_ram_complete
        else 'partial' if system_ram_samples else 'not_collected'
    )
    summary = dict(status='measured_intervals' if complete else 'incomplete',
                known_active_seconds=timing['measured_wall_seconds'],
                active_seconds=timing['measured_wall_seconds'] if complete else None,
                missing_stages=[list(x) for x in missing],
                sampled_peak_ram_bytes=max((x[2] for x in ram_samples), default=None) if resource_complete else None,
                sampled_average_ram_bytes=(sum(x[1] for x in ram_samples)/sum(x[0] for x in ram_samples)) if resource_complete and ram_samples else None,
                system_ram_status=system_ram_status,
                sampled_peak_system_ram_bytes=(
                    max((x[2] for x in system_ram_samples), default=None)
                    if system_ram_complete else None
                ),
                sampled_average_system_ram_bytes=(
                    sum(x[1] for x in system_ram_samples) / sum(x[0] for x in system_ram_samples)
                    if system_ram_complete else None
                ),
                system_ram_scope=(next(iter(system_ram_scopes))[1]
                                  if len(system_ram_scopes) == 1 else None),
                system_ram_source=(next(iter(system_ram_scopes))[0]
                                   if len(system_ram_scopes) == 1 else None),
                resource_scope_status=resource_scope_status,
                sampled_peak_disk_bytes=max(disk_peaks, default=None) if resource_complete else None,
                process_ram_source=(next(iter(ram_scopes))[0]
                                    if len(ram_scopes) == 1 else None),
                process_ram_scope=(next(iter(ram_scopes))[1]
                                   if len(ram_scopes) == 1 else None),
                disk_roots=(list(next(iter(disk_scopes)))
                            if len(disk_scopes) == 1 else None),
                scope='sum of explicit nonoverlapping measured intervals; excludes session gaps and unmeasured setup',
                end_to_end_complete=False)
    if session_envelopes is not None:
        rows = []
        seen = set()
        try:
            for row in session_envelopes:
                sid = row['session_id']
                start = datetime.fromisoformat(row['started_at'])
                end = datetime.fromisoformat(row['finished_at'])
                seconds = row['wall_seconds']
                if (sid in seen or row.get('status') != 'finished' or start.utcoffset() is None
                        or end.utcoffset() is None or end < start or type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    raise ValueError
                seen.add(sid)
                rows.append((start, end, float(seconds)))
        except (KeyError, TypeError, ValueError):
            summary.update(status='incomplete', active_seconds=None,
                           session_gaps_seconds=None, end_to_end_wall_seconds=None,
                           scope='incomplete session envelope')
            return summary
        rows.sort(key=lambda item: item[0])
        if seen != {record.get('session_id') for record in records}:
            summary.update(status='incomplete', active_seconds=None,
                           session_gaps_seconds=None, end_to_end_wall_seconds=None,
                           scope='session envelope does not cover observed sessions')
            return summary
        if any(cur[0] < prev[1] for prev, cur in zip(rows, rows[1:])):
            summary.update(status='incomplete', active_seconds=None,
                           session_gaps_seconds=None, end_to_end_wall_seconds=None,
                           scope='overlapping session envelopes')
            return summary
        summary['end_to_end_wall_seconds'] = sum(row[2] for row in rows)
        summary['session_gaps_seconds'] = sum(max(0.0, (cur[0] - prev[1]).total_seconds())
                                              for prev, cur in zip(rows, rows[1:]))
        summary['session_count'] = len(rows)
        summary['session_gap_scope'] = 'elapsed gap between completed session envelopes; excluded from runtime'
        summary['end_to_end_scope'] = 'sum of sequential phase session wall times through audit; excludes environment installation and inter-session gaps'
    return summary


def compare_workflows(left: dict, right: dict) -> dict:
    """Compare complete campaign wall envelopes only when scope fingerprints match."""
    reasons = []
    for field in ('selection_fingerprint', 'data_signature', 'environment_fingerprint',
                  'starting_state_fingerprint', 'measurement_protocol'):
        if not left.get(field) or left.get(field) != right.get(field):
            reasons.append(field + '_mismatch_or_missing')
    if left.get('status') != 'measured_intervals' or right.get('status') != 'measured_intervals':
        reasons.append('incomplete_measurements')
    if left.get('end_to_end_complete') is not True or right.get('end_to_end_complete') is not True:
        reasons.append('end_to_end_coverage_missing')
    a, b = left.get('end_to_end_wall_seconds'), right.get('end_to_end_wall_seconds')
    if type(a) not in (int, float) or type(b) not in (int, float):
        reasons.append('missing_active_seconds')
    return dict(eligible=not reasons, reasons=reasons,
                delta_seconds=(a-b) if not reasons else None,
                scope='complete campaign session wall envelopes; inter-session gaps excluded')


def aggregate_group_observations(records: list[dict], *, members: list[str],
                                 cohorts_by_member: dict[str, list[int]],
                                 session_envelopes: list[dict] | None = None) -> dict:
    """Aggregate source/S2/audit intervals for the new sequential M1 workflow."""
    required = []
    for member in members:
        for cohort in cohorts_by_member[member]:
            required.extend((stage, cohort) for stage in ('source', 's2'))
    required.append(('audit', 0))
    summary = summarize_workflow(records, required)
    audited_members = {
        (record.get('configuration') or {}).get('mssv')
        for record in records
        if record.get('stage') == 'audit'
        and record.get('cohort') == 0
        and record.get('execution_status') == 'returned'
        and (record.get('result') or {}).get('is_reconciled') is True
        and (record.get('result') or {}).get('integrity_failed_papers', 0) == 0
    }
    missing_audits = sorted(set(members) - audited_members)
    if missing_audits:
        summary['missing_stages'].extend([['audit', 0, member] for member in missing_audits])
        summary['status'] = 'incomplete'
        summary['active_seconds'] = None
        summary['end_to_end_complete'] = False
    starts = {}
    ends = {}
    for row in records:
        try:
            start = datetime.fromisoformat(row['started_at'])
            end = datetime.fromisoformat(row['finished_at'])
            if start.utcoffset() is None or end.utcoffset() is None or end < start:
                raise ValueError
            sid = row['session_id']
            starts[sid] = min(starts.get(sid, start), start)
            ends[sid] = max(ends.get(sid, end), end)
        except (KeyError, TypeError, ValueError):
            summary['status'] = 'incomplete'
            summary['session_gaps_seconds'] = None
            summary['session_gap_scope'] = 'unknown: invalid or unfinished session boundary'
            return summary
    envelopes = sorted((starts[sid], ends[sid]) for sid in starts)
    if any(cur[0] < prev[1] for prev, cur in zip(envelopes, envelopes[1:])):
        summary['status'] = 'incomplete'
        summary['session_gaps_seconds'] = None
        summary['session_gap_scope'] = 'unknown: session observations overlap'
        return summary
    summary['session_gaps_seconds'] = sum(max(0.0, (cur[0] - prev[1]).total_seconds())
                                          for prev, cur in zip(envelopes, envelopes[1:]))
    summary['session_gap_scope'] = 'between first and last recorded operation per session; setup outside observations is excluded'
    summary['session_count'] = len(envelopes)
    if session_envelopes is not None:
        envelope_rows = []
        seen_sessions = set()
        try:
            for row in session_envelopes:
                sid = row['session_id']
                start = datetime.fromisoformat(row['started_at'])
                end = datetime.fromisoformat(row['finished_at'])
                seconds = row['wall_seconds']
                if (sid in seen_sessions or row.get('status') != 'finished' or start.utcoffset() is None
                        or end.utcoffset() is None or end < start or type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    raise ValueError
                seen_sessions.add(sid)
                envelope_rows.append((start, end, float(seconds)))
        except (KeyError, TypeError, ValueError):
            summary.update(status='incomplete', active_seconds=None, end_to_end_wall_seconds=None,
                           end_to_end_complete=False, session_gap_scope='unknown: incomplete session envelope')
            return summary
        envelope_rows.sort(key=lambda item: item[0])
        if seen_sessions != {record.get('session_id') for record in records}:
            summary.update(status='incomplete', active_seconds=None, end_to_end_wall_seconds=None,
                           end_to_end_complete=False, session_gap_scope='unknown: session envelope coverage mismatch')
            return summary
        if any(cur[0] < prev[1] for prev, cur in zip(envelope_rows, envelope_rows[1:])):
            summary.update(status='incomplete', active_seconds=None, end_to_end_wall_seconds=None,
                           end_to_end_complete=False, session_gap_scope='unknown: overlapping session envelopes')
            return summary
        summary['end_to_end_wall_seconds'] = sum(row[2] for row in envelope_rows)
        summary['session_gaps_seconds'] = sum(max(0.0, (cur[0] - prev[1]).total_seconds())
                                              for prev, cur in zip(envelope_rows, envelope_rows[1:]))
        summary['session_gap_scope'] = 'elapsed gap between completed session envelopes; excluded from runtime'
        summary['session_count'] = len(envelope_rows)
        summary['end_to_end_scope'] = 'session wall time from snapshot preparation through audit, eligible export and size accounting; excludes environment installation and inter-session gaps'
    summary['required_cohort_count'] = sum(len(v) for v in cohorts_by_member.values())
    summary['expected_id_count'] = 20000 if summary['required_cohort_count'] == 200 else None
    summary['end_to_end_complete'] = (session_envelopes is not None
                                      and summary['status'] == 'measured_intervals'
                                      and summary.get('end_to_end_wall_seconds') is not None
                                      and summary.get('system_ram_status') == 'collected'
                                      and summary.get('resource_scope_status') == 'collected'
                                      and summary['expected_id_count'] == 20000)
    return summary


def fingerprint_complete_corpus(store: Any, pairs: list[tuple[str, str]]) -> str | None:
    """Hash comparable M1-complete papers and integrity-verified not-found IDs.

    Unknown, incomplete or invalid papers return None so different or partial
    coverage cannot accidentally qualify for a performance comparison.
    """
    import hashlib
    from src.storage_integrity import audit_paper_integrity

    normalized_pairs = [tuple(pair) for pair in pairs]
    if not normalized_pairs or len(set(normalized_pairs)) != len(normalized_pairs):
        return None
    rows = []
    try:
        for mssv, arxiv_id in sorted(normalized_pairs):
            audit = audit_paper_integrity(store, mssv, arxiv_id)
            if audit.has_integrity_failure:
                return None
            manifest = store.read_manifest(mssv, arxiv_id)
            if (audit.status == "valid" and manifest.get("discovery_status") == "not_found"
                    and manifest.get("status") == "not_found" and manifest.get("versions") == {}):
                rows.append(dict(mssv=mssv, arxiv_id=arxiv_id, discovery_status="not_found"))
                continue
            if not audit.is_m1_complete:
                return None
            metadata = store.read_metadata(mssv, arxiv_id)
            references = store.read_references(mssv, arxiv_id)
            versions = {name: {'status': rec.get('status'), 'file_checksums': rec.get('file_checksums')}
                        for name, rec in sorted(manifest['versions'].items())}
            rows.append(dict(mssv=mssv, arxiv_id=arxiv_id, versions=versions,
                             metadata={k: metadata.get(k) for k in ('title','authors','submission_date','revised_dates','venue')},
                             references=references.get('items')))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

"""Independent, resumable arXiv and Semantic Scholar phases for M1.

S2 evidence is staged outside the v2 corpus until the offline audit verifies
that it belongs to the exact assigned-ID selection and can be reconciled
without replacing arXiv provenance or source bytes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time
import uuid
from typing import Any, Callable

from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
    member_cohort_range,
    verify_cohort_handoff,
)
from src.session_evidence import (
    SessionBudget,
    annotate_session_envelope,
    finish_session_envelope,
    observe_stage,
    start_session_envelope,
)
from src.storage import IntegrityError, StorageError, _atomic_json, arxiv_id_to_folder_key


PHASE_SCHEMA_VERSION = 1
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


class M1PhaseError(RuntimeError):
    """A phase checkpoint is absent, incompatible, or fails verification."""


def build_member_selection(mssv: str, cohort_limit: int | None = None) -> dict:
    """Build the deterministic member selection shared by all M1 phases."""
    if not isinstance(mssv, str) or not mssv.strip():
        raise ValueError("MSSV không được để trống.")
    mssv = mssv.strip()
    assigned = member_cohort_range(mssv)
    if cohort_limit is not None and (
        isinstance(cohort_limit, bool)
        or not isinstance(cohort_limit, int)
        or not 1 <= cohort_limit <= len(assigned)
    ):
        raise ValueError(f"COHORT_LIMIT phải là số nguyên từ 1 đến {len(assigned)} hoặc None.")
    selected = assigned if cohort_limit is None else assigned[:cohort_limit]
    pairs = [pair for number in selected for pair in cohort_pairs(number)]
    if len(pairs) != 100 * len(selected) or any(member != mssv for member, _ in pairs):
        raise M1PhaseError("Phạm vi lô không khớp danh sách ID được giao trong cấu hình.")
    payload = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    group_pairs = [pair for number in range(1, 201) for pair in cohort_pairs(number)]
    group_payload = json.dumps(group_pairs, ensure_ascii=False, separators=(",", ":"))
    group_fingerprint = hashlib.sha256(group_payload.encode("utf-8")).hexdigest()
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": mssv,
        "cohorts": selected,
        "expected_count": len(pairs),
        "pairs": [[member, arxiv_id] for member, arxiv_id in pairs],
        "fingerprint": fingerprint,
        "group_fingerprint": group_fingerprint,
        "scope": "member_full_assignment" if cohort_limit is None else "member_subset_same_pipeline",
    }


def _phase_root(drive_root: Path, selection: dict) -> Path:
    return drive_root / "reports" / "phase_checkpoints" / selection["fingerprint"]


def _ensure_starting_state(runner: FastProductionRunner, phase_root: Path,
                           selection: dict) -> dict:
    """Persist the initial member state once so benchmark resumes stay comparable."""
    path = phase_root / "starting_state.json"
    if path.exists():
        state = _read_json(path)
        return _validate_starting_state(state, selection, path)
    archive_hashes = {}
    for cohort in selection["cohorts"]:
        archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
        archive_hashes[str(cohort)] = (
            runner._compute_sha256(archive) if archive.is_file() else "empty"
        )
    local_ids = sorted(
        arxiv_id for member, arxiv_id in selection["pairs"]
        if member == selection["mssv"]
        and ((runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).exists()
             or (runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).is_symlink())
    )
    fingerprint = hashlib.sha256(json.dumps(
        {"archives": archive_hashes, "local_ids": local_ids},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    state = {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": selection["mssv"],
        "fingerprint": selection["fingerprint"],
        "starting_state_fingerprint": fingerprint,
        "starting_state_empty": (
            all(value == "empty" for value in archive_hashes.values()) and not local_ids
        ),
        "starting_archive_sha256": archive_hashes,
        "starting_local_ids": local_ids,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(path, state)
    return state


def _validate_starting_state(state: Any, selection: dict, path: Path) -> dict:
    if (not isinstance(state, dict)
            or state.get("schema_version") != PHASE_SCHEMA_VERSION
            or state.get("mssv") != selection["mssv"]
            or state.get("fingerprint") != selection["fingerprint"]
            or not isinstance(state.get("starting_archive_sha256"), dict)
            or not isinstance(state.get("starting_local_ids"), list)):
        raise M1PhaseError(f"Starting-state checkpoint không tương thích: {path}")
    archives = state["starting_archive_sha256"]
    if set(archives) != {str(cohort) for cohort in selection["cohorts"]}:
        raise M1PhaseError(f"Starting-state archive coverage không khớp: {path}")
    if any(value != "empty" and (not isinstance(value, str)
                                  or len(value) != 64
                                  or any(char not in "0123456789abcdef" for char in value))
           for value in archives.values()):
        raise M1PhaseError(f"Starting-state archive checksum sai định dạng: {path}")
    local_ids = state["starting_local_ids"]
    assigned_ids = {arxiv_id for _member, arxiv_id in selection["pairs"]}
    if (any(not isinstance(arxiv_id, str) or arxiv_id not in assigned_ids for arxiv_id in local_ids)
            or local_ids != sorted(set(local_ids))):
        raise M1PhaseError(f"Starting-state ID không khớp phạm vi: {path}")
    expected_fingerprint = hashlib.sha256(json.dumps(
        {"archives": archives, "local_ids": local_ids},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    expected_empty = all(value == "empty" for value in archives.values()) and not local_ids
    if (state.get("starting_state_fingerprint") != expected_fingerprint
            or state.get("starting_state_empty") is not expected_empty):
        raise M1PhaseError(f"Starting-state fingerprint/checkpoint không khớp: {path}")
    return state


def _ensure_directory(path: Path, root: Path) -> None:
    """Create a path under root and reject symlinked components."""
    root = root.resolve()
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise M1PhaseError("Checkpoint path is outside its selected Drive root.") from exc
    current = root
    if current.is_symlink():
        raise M1PhaseError("Checkpoint root must not be a symlink.")
    for part in relative.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise M1PhaseError(f"Checkpoint path is not a plain directory: {current}")
        current.mkdir(exist_ok=True)


def _write_json(path: Path, payload: Any) -> str:
    _atomic_json(path, payload)
    return _sha256_file(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise M1PhaseError(f"Checkpoint evidence is not a regular file: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))
    except M1PhaseError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise M1PhaseError(f"Cannot read checkpoint evidence {path.name}; preserve it for inspection.") from exc


def _persist_selection(drive_root: Path, selection: dict) -> Path:
    root = _phase_root(drive_root, selection)
    _ensure_directory(root, drive_root)
    for sibling in root.parent.iterdir():
        if sibling == root or sibling.is_symlink() or not sibling.is_dir():
            continue
        prior_path = sibling / "selection.json"
        if not prior_path.exists():
            continue
        prior = _read_json(prior_path)
        if (isinstance(prior, dict)
                and prior.get("mssv") == selection["mssv"]
                and prior.get("cohorts") == selection["cohorts"]
                and prior.get("scope") == selection["scope"]
                and prior.get("fingerprint") != selection["fingerprint"]):
            raise M1PhaseError(
                f"Có checkpoint cũ cùng phạm vi nhưng fingerprint khác tại {prior_path}; "
                "giữ nguyên để kiểm tra, không tự thay thế."
            )
    path = root / "selection.json"
    if path.exists():
        saved = _read_json(path)
        if saved != selection:
            raise M1PhaseError(
                f"Selection checkpoint mismatch at {path}; no evidence was changed."
            )
    else:
        _write_json(path, selection)
    return root


def _record_session(phase_root: Path, selection: dict, session_id: str, stage: str) -> None:
    path = phase_root / "session_index.json"
    if path.exists():
        saved = _read_json(path)
        if (not isinstance(saved, dict)
                or saved.get("schema_version") != PHASE_SCHEMA_VERSION
                or saved.get("fingerprint") != selection["fingerprint"]
                or not isinstance(saved.get("sessions"), list)):
            raise M1PhaseError(f"Session index không tương thích: {path}")
    else:
        saved = {"schema_version": PHASE_SCHEMA_VERSION,
                 "fingerprint": selection["fingerprint"], "sessions": []}
    if not any(row.get("session_id") == session_id for row in saved["sessions"] if isinstance(row, dict)):
        saved["sessions"].append({"session_id": session_id, "stage": stage})
    _write_json(path, saved)


@contextmanager
def _member_writer_lock(drive_root: Path, selection: dict):
    """Enforce one writer for this member and selected assignment scope."""
    lock_dir = drive_root / "reports" / "phase_locks"
    _ensure_directory(lock_dir, drive_root)
    lock = lock_dir / f"{selection['mssv']}.lock"
    payload = {
        "pid": os.getpid(),
        "mssv": selection["mssv"],
        "fingerprint": selection["fingerprint"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise M1PhaseError(
            f"Một tiến trình khác đang ghi phạm vi này hoặc lock cũ cần kiểm tra: {lock}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


@contextmanager
def _api_key_environment(api_key: str | None):
    names = ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
    previous = {name: os.environ.get(name) for name in names}
    value = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    for name in names:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield value
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _session_start(runner: FastProductionRunner, selection: dict, stage: str) -> tuple[str, Path, float]:
    session_id = uuid.uuid4().hex
    started = time.perf_counter()
    envelope = start_session_envelope(runner.reports_dir, session_id)
    annotate_session_envelope(
        envelope,
        mssv=selection["mssv"],
        selection_fingerprint=selection["fingerprint"],
        run_scope=selection["scope"],
        cohorts=selection["cohorts"],
        expected_id_count=selection["expected_count"],
        stage=stage,
    )
    return session_id, envelope, started


def _source_state_manifest(path: Path, selection: dict, cohort: int) -> dict:
    if not path.exists():
        return {
            "schema_version": PHASE_SCHEMA_VERSION,
            "stage": "arxiv",
            "mssv": selection["mssv"],
            "cohort": cohort,
            "fingerprint": selection["fingerprint"],
            "ids": [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]],
            "status": "not_started",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    saved = _read_json(path)
    expected_ids = [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    if (
        not isinstance(saved, dict)
        or saved.get("schema_version") != PHASE_SCHEMA_VERSION
        or saved.get("stage") != "arxiv"
        or saved.get("mssv") != selection["mssv"]
        or saved.get("cohort") != cohort
        or saved.get("fingerprint") != selection["fingerprint"]
        or saved.get("ids") != expected_ids
        or saved.get("status") not in {"not_started", "source_partial", "source_complete"}
    ):
        raise M1PhaseError(f"arXiv phase checkpoint không tương thích: {path}")
    count = saved.get("source_complete_papers")
    if count is not None and (type(count) is not int or not 0 <= count <= len(expected_ids)):
        raise M1PhaseError(f"Số lượng source trong checkpoint arXiv sai: {path}")
    digest = saved.get("archive_sha256")
    if digest is not None and (
        not isinstance(digest, str) or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise M1PhaseError(f"Checksum archive trong checkpoint arXiv sai: {path}")
    for field in ("kaggle_snapshot_sha256", "kaggle_index_sha256"):
        value = saved.get(field)
        if value is not None and (
            not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise M1PhaseError(f"Checksum Kaggle trong checkpoint arXiv sai: {path}")
    policy = saved.get("kaggle_policy_version")
    if policy is not None and policy != "m1-kaggle-reconcile-v2":
        raise M1PhaseError(f"Phiên bản chính sách Kaggle trong checkpoint không được hỗ trợ: {path}")
    auto_resume = saved.get("auto_resume_429")
    if auto_resume is not None:
        if (not isinstance(auto_resume, dict)
                or auto_resume.get("schema_version") != 1
                or not isinstance(auto_resume.get("events"), list)):
            raise M1PhaseError(f"Checkpoint auto-resume 429 không hợp lệ: {path}")
        for event in auto_resume["events"]:
            if (not isinstance(event, dict)
                    or event.get("stage") != "discovery"
                    or event.get("http_status") != 429
                    or event.get("outcome") not in {
                        "not_resumed", "cooldown_scheduled", "cooldown_complete",
                        "resume_started", "resumed",
                    }):
                raise M1PhaseError(f"Bằng chứng auto-resume 429 không hợp lệ: {path}")
        policy = auto_resume.get("policy")
        if policy is not None and (
            not isinstance(policy, dict) or policy.get("schema_version") != 1
            or policy.get("trigger") != {"stage": "discovery", "http_status": 429}
        ):
            raise M1PhaseError(f"Chính sách auto-resume 429 không hợp lệ: {path}")
    return saved


def _mark_archive_arxiv_only(runner: FastProductionRunner, cohort: int) -> None:
    """Keep source-phase archive metadata from certifying M1 through cached S2."""
    archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
    manifest_path = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))
    if not archive.is_file() or not manifest_path.is_file():
        raise M1PhaseError(f"Thiếu archive hoặc accounting manifest sau pha arXiv: {archive}")
    manifest = _read_json(manifest_path)
    digest = runner._compute_sha256(archive)
    if manifest.get("archive_sha256") != digest:
        raise M1PhaseError(f"Archive checksum/accounting không khớp: {archive}")
    source_complete = runner.count_source_completed_papers(cohort) == len(cohort_pairs(cohort))
    manifest["archive_state"] = "source_verified" if source_complete else "checkpoint_partial"
    manifest["phase_certification"] = "arxiv_only"
    _write_json(manifest_path, manifest)


def _auto_resume_429_policy(
    enabled: bool,
    max_per_cohort: int,
    max_per_session: int,
    cooldown_min_seconds: float,
    cooldown_max_seconds: float,
) -> dict:
    """Validate the opt-in, bounded discovery-429 resume settings."""
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
        raise ValueError("Cooldown tối thiểu phải là số giây hữu hạn dương.")
    if (isinstance(cooldown_max_seconds, bool)
            or not isinstance(cooldown_max_seconds, (int, float))
            or not math.isfinite(cooldown_max_seconds)
            or cooldown_max_seconds < cooldown_min_seconds):
        raise ValueError("Cooldown tối đa phải hữu hạn và không nhỏ hơn cooldown tối thiểu.")
    return {
        "schema_version": 1,
        "enabled": enabled and max_per_cohort > 0 and max_per_session > 0,
        "max_per_cohort": max_per_cohort,
        "max_per_session": max_per_session,
        "cooldown_min_seconds": float(cooldown_min_seconds),
        "cooldown_max_seconds": float(cooldown_max_seconds),
        "trigger": {"stage": "discovery", "http_status": 429},
        "retry_wait_budget_seconds": 300.0,
    }


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


def _durable_discovery_429_checkpoint(
    runner: FastProductionRunner, mssv: str, arxiv_id: str, cohort: int,
    batch_id: str,
) -> bool:
    """Require the exact 429 attempt, valid paper checkpoint and archive."""
    from src import benchmark_runner
    from src.archive_validation import validate_archive_sidecars

    try:
        _, history = benchmark_runner._load(
            runner.logs_dir / batch_id, batch_id, runner.corpus_dir,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    matching_attempts = [
        attempt for attempt in history
        if isinstance(attempt.get("stop_reason"), dict)
        and attempt["stop_reason"].get("type") == "http_policy"
        and attempt["stop_reason"].get("stage") == "discovery"
        and attempt["stop_reason"].get("http_status") == 429
        and attempt["stop_reason"].get("arxiv_id") == arxiv_id
    ]
    if not matching_attempts:
        return False
    attempt = matching_attempts[-1]
    paper_rows = [row for row in attempt.get("papers", [])
                  if row.get("mssv") == mssv and row.get("arxiv_id") == arxiv_id]
    if len(paper_rows) != 1 or paper_rows[0].get("state") != "processed":
        return False
    paper_result = paper_rows[0].get("result") or {}
    discovery = paper_result.get("discovery") or {}
    if discovery.get("action") != "queried" or discovery.get("http_status") != 429:
        return False

    manifest = runner.store.read_manifest(mssv, arxiv_id)
    metadata = runner.store.read_metadata(mssv, arxiv_id)
    provenance = metadata.get("provenance", {}).get("arxiv") or {}
    snapshot = provenance.get("snapshot") or {}
    # 429 ở discovery lần đầu được ghi là rate_limited. Nếu lượt làm mới gặp 429,
    # phải giữ nguyên kết quả discovery đã được xác nhận trước đó.
    first_discovery_failure = (
        manifest.get("discovery_status") == "rate_limited"
        and snapshot.get("mssv") == mssv
        and snapshot.get("arxiv_id") == arxiv_id
        and snapshot.get("status") == "rate_limited"
        and snapshot.get("http_status") == 429
    )
    prior_confirmed_discovery = (
        manifest.get("discovery_status") == "found"
        and manifest.get("version_status") == "resolved"
        and bool(manifest.get("versions"))
        and snapshot.get("mssv") == mssv
        and snapshot.get("arxiv_id") == arxiv_id
        and snapshot.get("status") == "found"
        and snapshot.get("http_status") == 200
    )
    if not (first_discovery_failure or prior_confirmed_discovery):
        return False
    archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
    validate_archive_sidecars(archive, cohort, require_complete=True)
    return True


def _provider_cooldown_from_logs(runner: FastProductionRunner) -> tuple[float, str | None]:
    """Return a still-active server Retry-After deadline, if one was recorded."""
    from src import arxiv_client, batch

    try:
        batch.check_arxiv_cooldown(runner.logs_dir)
    except arxiv_client.ArxivCooldownError as exc:
        deadline = datetime.fromisoformat(exc.retry_at_utc.replace("Z", "+00:00"))
        return max(0.0, deadline.timestamp() - time.time()), exc.retry_at_utc
    return 0.0, None


def run_m1_arxiv_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
    kaggle_token: str | None = None,
    auto_resume_429: bool = False,
    auto_resume_429_max_per_cohort: int = 1,
    auto_resume_429_max_per_session: int = 2,
    auto_resume_429_cooldown_min_seconds: float = 300.0,
    auto_resume_429_cooldown_max_seconds: float = 600.0,
) -> dict:
    """Verify assigned IDs and archive every source version, without S2.

    Bounded automatic resume is opt-in and applies only to a checkpointed HTTP
    429 during discovery. All other stop conditions retain the existing flow.
    """
    retry_policy = _auto_resume_429_policy(
        auto_resume_429,
        auto_resume_429_max_per_cohort,
        auto_resume_429_max_per_session,
        auto_resume_429_cooldown_min_seconds,
        auto_resume_429_cooldown_max_seconds,
    )
    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(
        local_root=local,
        drive_root=drive,
        retry_429=True,
        retry_wait_budget=300.0,
        timeout=30.0,
    )
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "arxiv")
    phase_root = _persist_selection(drive, selection)
    from src.kaggle_candidates import (
        file_sha256,
        prepare_snapshot,
        reuse_or_build_index,
        validate_discrepancy_checkpoints,
    )
    evidence_root = phase_root / "arxiv" / "kaggle_discrepancies"
    discrepancy_checkpoint_root = phase_root / "arxiv" / "kaggle_discrepancy_checkpoints"
    kaggle_cache = drive.parent / "shared" / "kaggle_snapshot"
    kaggle_index_root = drive / "kaggle_indices"
    prior_environment = {
        key: os.environ.get(key) for key in (
            "M1_KAGGLE_CANDIDATE_INDEX", "M1_KAGGLE_SNAPSHOT_PATH",
            "M1_KAGGLE_SNAPSHOT_SHA256", "M1_KAGGLE_INDEX_SHA256",
            "M1_KAGGLE_POLICY_VERSION",
            "M1_KAGGLE_EVIDENCE_ROOT", "M1_KAGGLE_CHECKPOINT_ROOT",
            "M1_KAGGLE_EVIDENCE_SESSION",
        )
    }
    kaggle_evidence: dict[str, Any] = {"status": "fallback", "reason": "not_prepared"}
    index_path = None
    cohort_results = {}
    stopped = False
    stop_reason = None
    automatic_resumes = 0
    automatic_cooldown_seconds = 0.0
    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "arxiv")
            _ensure_starting_state(runner, phase_root, selection)
            validate_discrepancy_checkpoints(discrepancy_checkpoint_root, evidence_root)
            token = kaggle_token.strip() if isinstance(kaggle_token, str) and kaggle_token.strip() else None
            snapshot_result = observe_stage(
                runner, "snapshot_prepare", 0,
                lambda: prepare_snapshot(kaggle_cache, token=token),
                session_id=session_id,
                configuration={"snapshot_scope": "shared_cache", "accelerator": "CPU"},
            )
            kaggle_evidence = {key: value for key, value in snapshot_result.items()
                               if key not in {"snapshot_path", "token"}}
            if snapshot_result.get("status") == "ready":
                try:
                    selected_pairs = [tuple(pair) for pair in selection["pairs"]]
                    index_result = observe_stage(
                        runner, "kaggle_index", 0,
                        lambda: reuse_or_build_index(
                            snapshot_result["snapshot_path"], selected_pairs,
                            kaggle_index_root, snapshot_result["snapshot_sha256"],
                        ),
                        session_id=session_id,
                        configuration={
                            "run_scope": selection["scope"],
                            "selection_fingerprint": selection["fingerprint"],
                            "snapshot_sha256": snapshot_result["snapshot_sha256"],
                        },
                    )
                    index_path = index_result["index_path"]
                    index_sha256 = file_sha256(index_path)
                    kaggle_evidence.update(
                        status="ready", index_path=index_path, index_sha256=index_sha256,
                        reused_index=index_result["reused"],
                        candidate_count=index_result.get(
                            "candidate_count",
                            len(index_result["index"].get("candidates", {}))),
                        snapshot_size_bytes=index_result.get(
                            "snapshot_size_bytes",
                            index_result["index"].get("snapshot_size_bytes")),
                        snapshot_precheck_seconds=index_result.get("snapshot_precheck_seconds"),
                        index_checksum_seconds=index_result.get("index_checksum_seconds"),
                        checksum_in_scan=index_result["index"].get("checksum_in_scan"),
                        index_scan_seconds=index_result.get("index_scan_seconds"),
                        index_scanned_lines=index_result.get("index_scanned_lines"),
                    )
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    index_path = None
                    kaggle_evidence.update(status="fallback", reason="candidate_index_failed",
                                           error_type=type(exc).__name__)
            prep_path = runner.reports_dir / f"kaggle_preparation_{session_id}.json"
            try:
                _atomic_json(prep_path, {"session_id": session_id, **kaggle_evidence})
            except OSError:
                # Chỉ dùng Kaggle khi xuất xứ của dữ liệu đã được ghi bền vững.
                index_path = None
                kaggle_evidence = {
                    **kaggle_evidence, "status": "fallback",
                    "reason": "kaggle_preparation_evidence_not_persisted",
                }
            if index_path:
                os.environ.update({
                    "M1_KAGGLE_CANDIDATE_INDEX": str(index_path),
                    "M1_KAGGLE_SNAPSHOT_PATH": snapshot_result["snapshot_path"],
                    "M1_KAGGLE_SNAPSHOT_SHA256": snapshot_result["snapshot_sha256"],
                    "M1_KAGGLE_INDEX_SHA256": index_sha256,
                    "M1_KAGGLE_POLICY_VERSION": "m1-kaggle-reconcile-v2",
                    "M1_KAGGLE_EVIDENCE_ROOT": str(evidence_root),
                    "M1_KAGGLE_CHECKPOINT_ROOT": str(discrepancy_checkpoint_root),
                    "M1_KAGGLE_EVIDENCE_SESSION": session_id,
                })
            else:
                for key in ("M1_KAGGLE_CANDIDATE_INDEX", "M1_KAGGLE_SNAPSHOT_PATH",
                            "M1_KAGGLE_SNAPSHOT_SHA256", "M1_KAGGLE_INDEX_SHA256"):
                    os.environ.pop(key, None)
                os.environ.update({
                    "M1_KAGGLE_POLICY_VERSION": "m1-kaggle-reconcile-v2",
                    "M1_KAGGLE_EVIDENCE_ROOT": str(evidence_root),
                    "M1_KAGGLE_CHECKPOINT_ROOT": str(discrepancy_checkpoint_root),
                    "M1_KAGGLE_EVIDENCE_SESSION": session_id,
                })
            annotate_session_envelope(
                envelope,
                kaggle_status=kaggle_evidence.get("status"),
                kaggle_reason=kaggle_evidence.get("reason"),
                kaggle_snapshot_sha256=kaggle_evidence.get("snapshot_sha256"),
                kaggle_index_sha256=kaggle_evidence.get("index_sha256"),
                kaggle_downloaded_at=kaggle_evidence.get("downloaded_at"),
                kaggle_provider_updated_at=kaggle_evidence.get("provider_updated_at"),
                auto_resume_429_policy=retry_policy,
            )
            for cohort in selection["cohorts"]:
                state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
                _ensure_directory(state_path.parent, drive)
                state = _source_state_manifest(state_path, selection, cohort)
                state.update(
                    kaggle_status=kaggle_evidence.get("status"),
                    kaggle_snapshot_sha256=kaggle_evidence.get("snapshot_sha256"),
                    kaggle_index_sha256=kaggle_evidence.get("index_sha256"),
                    kaggle_policy_version="m1-kaggle-reconcile-v2" if index_path else None,
                )
                if budget.should_stop():
                    stopped = True
                    stop_reason = {"type": "session_budget", "stage": "arxiv_before_cohort"}
                    state.update(status="source_partial", stop_reason=stop_reason)
                    _write_json(state_path, state)
                    break

                def process_cohort(cohort_number=cohort, manifest=state):
                    archive = runner.archives_dir / f"cohort_{cohort_number:04d}_corpus.tar.gz"
                    sidecars = [
                        archive,
                        archive.with_name(archive.name + ".sha256"),
                        archive.with_name(archive.name.replace(".tar.gz", ".manifest.json")),
                    ]
                    if any(path.exists() for path in sidecars):
                        runner.backup_cohort_archive(cohort_number, require_sidecars=True)
                    print(
                        f"[M1/arXiv] Lô {cohort_number}: đang khôi phục và kiểm tra checkpoint source.",
                        flush=True,
                    )
                    restored = runner.restore_cohort(cohort_number)
                    cohort_started = time.perf_counter()
                    cohort_total = len(cohort_pairs(cohort_number))
                    print(
                        f"[M1/arXiv] Lô {cohort_number}: bắt đầu discovery/source; "
                        f"phạm vi {cohort_total} ID, đã khôi phục {restored}; "
                        "Atom batch tối đa 20 ID.",
                        flush=True,
                    )

                    def source_progress(event, detail):
                        if event == "paper_finished":
                            processed = len(detail.get("papers", []))
                            if processed and (processed % 10 == 0 or processed == cohort_total
                                              or detail.get("stopped")):
                                terminal_in_attempt = sum(
                                    item.get("paper_status") in {"complete", "not_found"}
                                    for item in detail.get("papers", [])
                                )
                                print(
                                    f"[M1/arXiv] Lô {cohort_number}: đã xử lý "
                                    f"{processed}/{cohort_total} paper trong lượt này; "
                                    f"{terminal_in_attempt} paper ở trạng thái source kết thúc trong lượt này.",
                                    flush=True,
                                )

                    def discovery_progress(event, detail):
                        if event == "request_started":
                            print(
                                f"[M1/arXiv] Atom {detail['request_type']}: "
                                f"{detail['first_arxiv_id']}..{detail['last_arxiv_id']} "
                                f"({detail['requested_count']} ID); đang chờ phản hồi.",
                                flush=True,
                            )
                        elif event == "request_finished":
                            print(
                                f"[M1/arXiv] Atom {detail['request_type']} xong: "
                                f"HTTP {detail.get('http_status')}, "
                                f"{detail.get('returned_count', 0)}/"
                                f"{detail['requested_count']} entry nhận được, "
                                f"{detail.get('attempts')} attempt, "
                                f"{detail.get('wall_seconds', 0.0):.1f}s; "
                                f"còn cần xác minh riêng: {detail.get('unresolved_count', 0)}.",
                                flush=True,
                            )

                    result = runner.run_cohort(
                        cohort_number,
                        resume=True,
                        revision_history=True,
                        s2_config=None,
                        max_ids=100,
                        should_stop=budget.should_stop,
                        on_progress=source_progress,
                        on_discovery_progress=discovery_progress,
                        discovery_batch_size=20,
                        force_discovery_refresh=True,
                    )
                    _mark_archive_arxiv_only(runner, cohort_number)
                    source_count = runner.count_source_completed_papers(cohort_number)
                    complete = source_count == len(cohort_pairs(cohort_number))
                    print(
                        f"[M1/arXiv] Lô {cohort_number}: kết thúc lượt trong "
                        f"{time.perf_counter() - cohort_started:.1f}s; "
                        f"source hoàn tất {source_count}/{cohort_total}; "
                        f"trạng thái {'đủ' if complete else 'còn tiếp tục'}.",
                        flush=True,
                    )
                    archive_sha = None
                    if archive.is_file():
                        archive_sha = runner._compute_sha256(archive)
                    fast = result.get("fast_runner", {})
                    manifest.update(
                        status="source_complete" if complete else "source_partial",
                        restored_papers=restored,
                        source_complete_papers=source_count,
                        expected_count=len(cohort_pairs(cohort_number)),
                        archive_sha256=archive_sha,
                        stop_reason=fast.get("stop_reason"),
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                    _write_json(state_path, manifest)
                    return {
                        "status": manifest["status"],
                        "source_complete_papers": source_count,
                        "expected_count": manifest["expected_count"],
                        "stopped": bool(fast.get("stopped")),
                        "stop_reason": fast.get("stop_reason"),
                        "fast_runner": fast,
                        "timing_metrics": result.get("timing_metrics"),
                    }

                saved_auto_state = state.get("auto_resume_429")
                if saved_auto_state is None:
                    saved_auto_state = {"schema_version": 1, "events": []}
                saved_auto_state["policy"] = retry_policy
                state["auto_resume_429"] = saved_auto_state
                cohort_resume_count = 0
                cohort_cooldown_seconds = 0.0
                cohort_events = []
                source_attempt_number = 0
                pending_resume_event = None
                while True:
                    if pending_resume_event is not None:
                        cohort_resume_count += 1
                        automatic_resumes += 1
                        pending_resume_event["outcome"] = "resume_started"
                        pending_resume_event["resumed_source_attempt_number"] = source_attempt_number + 1
                        _write_json(state_path, state)
                        pending_resume_event = None
                    source_attempt_number += 1
                    result = observe_stage(
                        runner,
                        "source",
                        cohort,
                        process_cohort,
                        session_id=session_id,
                        configuration={
                            "selection_fingerprint": selection["fingerprint"],
                            "mssv": selection["mssv"],
                            "run_scope": selection["scope"],
                            "resume": True,
                            "revision_history": True,
                            "discovery_batch_size": 20,
                            "force_discovery_refresh": True,
                            "kaggle_snapshot_sha256": kaggle_evidence.get("snapshot_sha256"),
                            "kaggle_index_sha256": kaggle_evidence.get("index_sha256"),
                            "s2_enabled": False,
                            "session_budget_seconds": budget_seconds,
                            "auto_resume_429_policy": retry_policy,
                            "source_attempt_number": source_attempt_number,
                        },
                    )
                    if (cohort_events
                            and cohort_events[-1].get("outcome") == "resume_started"):
                        cohort_events[-1]["outcome"] = "resumed"
                        _write_json(state_path, state)
                    fast = result.get("fast_runner") or {}
                    reason = result.get("stop_reason") or fast.get("stop_reason") or {}
                    if not (
                        retry_policy["enabled"]
                        and result.get("stopped") is True
                        and reason.get("type") == "http_policy"
                        and reason.get("stage") == "discovery"
                        and reason.get("http_status") == 429
                    ):
                        break

                    event = {
                        "session_id": session_id,
                        "source_attempt_number": source_attempt_number,
                        "arxiv_id": reason.get("arxiv_id"),
                        "stage": "discovery",
                        "http_status": 429,
                        "checkpoint_saved": False,
                        "outcome": "not_resumed",
                    }
                    batch_id = fast.get("batch_id")
                    try:
                        if isinstance(event["arxiv_id"], str) and isinstance(batch_id, str):
                            event["checkpoint_saved"] = _durable_discovery_429_checkpoint(
                                runner, selection["mssv"], event["arxiv_id"], cohort,
                                batch_id,
                            )
                    except (OSError, ValueError, KeyError, TypeError, IntegrityError, StorageError):
                        event["checkpoint_saved"] = False
                        event["checkpoint_validation"] = "failed"
                    if not event["checkpoint_saved"]:
                        event["deferred_reason"] = "checkpoint_missing_or_invalid"
                        cohort_events.append(event)
                        saved_auto_state["events"].append(event)
                        _write_json(state_path, state)
                        break

                    if cohort_resume_count >= retry_policy["max_per_cohort"]:
                        event["deferred_reason"] = "cohort_resume_limit"
                        cohort_events.append(event)
                        saved_auto_state["events"].append(event)
                        _write_json(state_path, state)
                        break
                    if automatic_resumes >= retry_policy["max_per_session"]:
                        event["deferred_reason"] = "session_resume_limit"
                        cohort_events.append(event)
                        saved_auto_state["events"].append(event)
                        _write_json(state_path, state)
                        break

                    try:
                        provider_cooldown, provider_deadline = _provider_cooldown_from_logs(runner)
                    except (OSError, ValueError, KeyError, TypeError, IntegrityError, StorageError):
                        event["deferred_reason"] = "provider_cooldown_evidence_invalid"
                        cohort_events.append(event)
                        saved_auto_state["events"].append(event)
                        _write_json(state_path, state)
                        break
                    cooldown_seconds = max(
                        random.uniform(
                            retry_policy["cooldown_min_seconds"],
                            retry_policy["cooldown_max_seconds"],
                        ),
                        provider_cooldown,
                    )
                    # Chừa đủ ngân sách phiên để cơ chế thử lại hiện có
                    # có thể chờ năm phút và chạy lại sau khi tiếp tục.
                    required_budget = cooldown_seconds + runner.retry_wait_budget + 30.0
                    if budget.remaining_seconds < required_budget:
                        event.update(
                            deferred_reason="session_budget",
                            cooldown_seconds=cooldown_seconds,
                            provider_retry_at_utc=provider_deadline,
                        )
                        cohort_events.append(event)
                        saved_auto_state["events"].append(event)
                        _write_json(state_path, state)
                        break

                    retry_not_before = datetime.fromtimestamp(
                        time.time() + cooldown_seconds, timezone.utc,
                    ).isoformat()
                    event.update(
                        outcome="cooldown_scheduled",
                        cooldown_seconds=cooldown_seconds,
                        provider_cooldown_seconds=provider_cooldown,
                        provider_retry_at_utc=provider_deadline,
                        retry_not_before_utc=retry_not_before,
                    )
                    cohort_events.append(event)
                    saved_auto_state["events"].append(event)
                    _write_json(state_path, state)
                    print(
                        f"[arXiv 429] Discovery {reason.get('arxiv_id')}: "
                        f"checkpoint đã xác minh; tự resume sau {cooldown_seconds:.0f}s "
                        f"(lần {cohort_resume_count + 1}/{retry_policy['max_per_cohort']}, "
                        f"giới hạn phiên {automatic_resumes + 1}/"
                        f"{retry_policy['max_per_session']}).",
                        flush=True,
                    )

                    wait_started = time.perf_counter()

                    def wait_for_arxiv_cooldown() -> dict:
                        time.sleep(cooldown_seconds)
                        return {
                            "status": "waited",
                            "retry_wait_seconds": max(0.0, time.perf_counter() - wait_started),
                            "arxiv_id": reason["arxiv_id"],
                            "trigger_stage": "discovery",
                            "trigger_http_status": 429,
                            "resume_attempt_number": cohort_resume_count + 1,
                        }

                    wait_result = observe_stage(
                        runner,
                        "arxiv_retry_wait",
                        cohort,
                        wait_for_arxiv_cooldown,
                        session_id=session_id,
                        configuration={
                            "selection_fingerprint": selection["fingerprint"],
                            "mssv": selection["mssv"],
                            "arxiv_id": reason["arxiv_id"],
                            "trigger_stage": "discovery",
                            "trigger_http_status": 429,
                            "resume_attempt_number": cohort_resume_count + 1,
                            "scheduled_seconds": cooldown_seconds,
                            "provider_retry_at_utc": provider_deadline,
                        },
                    )
                    measured_cooldown = float(wait_result["retry_wait_seconds"])
                    event.update(
                        outcome="cooldown_complete",
                        measured_cooldown_seconds=measured_cooldown,
                    )
                    saved_auto_state["events"][-1] = event
                    _write_json(state_path, state)
                    cohort_cooldown_seconds += measured_cooldown
                    automatic_cooldown_seconds += measured_cooldown
                    if budget.should_stop():
                        event["deferred_reason"] = "session_budget_after_cooldown"
                        _write_json(state_path, state)
                        break
                    pending_resume_event = event

                result["auto_resume_429"] = {
                    "enabled": retry_policy["enabled"],
                    "resume_attempts": cohort_resume_count,
                    "cooldown_seconds": cohort_cooldown_seconds,
                    "events": cohort_events,
                }
                cohort_results[str(cohort)] = result
                if result.get("stopped"):
                    stopped = True
                    stop_reason = result.get("stop_reason")
                    break
        completed = sum(
            value.get("source_complete_papers", 0) for value in cohort_results.values()
        )
        return {
            "stage": "arxiv",
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "expected_id_count": selection["expected_count"],
            "source_complete_papers": completed,
            "cohorts": cohort_results,
            "stopped": stopped,
            "stop_reason": stop_reason,
            "m1_complete": False,
            "checkpoint_root": str(phase_root / "arxiv"),
            "session_id": session_id,
            "kaggle": kaggle_evidence,
            "auto_resume_429": {
                "policy": retry_policy,
                "resume_attempts": automatic_resumes,
                "cooldown_seconds": automatic_cooldown_seconds,
            },
        }
    finally:
        for key, value in prior_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        finish_session_envelope(envelope, started_monotonic=started)


def _cohort_s2_paths(phase_root: Path, cohort: int) -> tuple[Path, Path]:
    cohort_root = phase_root / "s2" / f"cohort_{cohort:04d}"
    return cohort_root, cohort_root / "checkpoint.json"


def _new_s2_manifest(selection: dict, cohort: int) -> dict:
    ids = [aid for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
    return {
        "schema_version": PHASE_SCHEMA_VERSION,
        "stage": "s2",
        "mssv": selection["mssv"],
        "cohort": cohort,
        "fingerprint": selection["fingerprint"],
        "ids": ids,
        "status": "in_progress",
        "files": {},
        "papers": {
            aid: {
                "metadata_status": "not_attempted",
                "references_status": "not_attempted",
                "attempt_errors": [],
            }
            for aid in ids
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_s2_manifest(path: Path, selection: dict, cohort: int) -> dict:
    if not path.exists():
        return _new_s2_manifest(selection, cohort)
    saved = _read_json(path)
    expected = _new_s2_manifest(selection, cohort)
    if (
        not isinstance(saved, dict)
        or saved.get("schema_version") != PHASE_SCHEMA_VERSION
        or saved.get("stage") != "s2"
        or saved.get("mssv") != selection["mssv"]
        or saved.get("cohort") != cohort
        or saved.get("fingerprint") != selection["fingerprint"]
        or saved.get("ids") != expected["ids"]
        or saved.get("status") not in {"in_progress", "partial", "complete"}
        or not isinstance(saved.get("files"), dict)
        or not isinstance(saved.get("papers"), dict)
        or set(saved["papers"]) != set(expected["ids"])
    ):
        raise M1PhaseError(f"S2 checkpoint không tương thích; giữ nguyên để kiểm tra: {path}")
    for relative, digest in saved["files"].items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise M1PhaseError(f"S2 checkpoint có mục checksum sai định dạng: {path}")
        evidence = path.parent / "papers" / relative
        try:
            evidence.resolve().relative_to(path.parent.resolve())
        except ValueError as exc:
            raise M1PhaseError("Đường dẫn bằng chứng S2 thoát khỏi checkpoint.") from exc
        if evidence.is_symlink() or not evidence.is_file() or _sha256_file(evidence) != digest:
            raise M1PhaseError(
                f"Checksum S2 không khớp hoặc file bị thiếu: {evidence}; không tự xóa hay sửa."
            )
    return saved


def _evidence_file(cohort_root: Path, arxiv_id: str, kind: str) -> tuple[Path, str]:
    relative = f"{arxiv_id_to_folder_key(arxiv_id)}/{kind}.json"
    return cohort_root / "papers" / relative, relative


def _read_s2_paper(manifest: dict, cohort_root: Path, arxiv_id: str) -> tuple[dict | None, dict | None]:
    from src import s2_integration

    record = manifest["papers"][arxiv_id]
    metadata_result = None
    references_result = None
    if record.get("metadata_status") != "not_attempted":
        path, relative = _evidence_file(cohort_root, arxiv_id, "metadata")
        if relative not in manifest["files"]:
            raise M1PhaseError(f"Thiếu checksum cho metadata S2 {arxiv_id}.")
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("schema_version") != PHASE_SCHEMA_VERSION:
            raise M1PhaseError(f"Metadata S2 sai schema cho {arxiv_id}.")
        metadata_result = s2_integration.metadata_result(payload.get("result"), arxiv_id)
        if record.get("metadata_status") != metadata_result["status"]:
            raise M1PhaseError(f"Trạng thái metadata S2 không khớp evidence cho {arxiv_id}.")
    if record.get("references_status") != "not_attempted":
        path, relative = _evidence_file(cohort_root, arxiv_id, "references")
        if relative not in manifest["files"]:
            raise M1PhaseError(f"Thiếu checksum cho references S2 {arxiv_id}.")
        payload = _read_json(path)
        if not isinstance(payload, dict) or payload.get("schema_version") != PHASE_SCHEMA_VERSION:
            raise M1PhaseError(f"References S2 sai schema cho {arxiv_id}.")
        references_result = payload.get("snapshot")
        s2_integration.references_evidence(references_result, arxiv_id, for_resume=True)
        if record.get("references_status") != references_result.get("status"):
            raise M1PhaseError(f"Trạng thái references S2 không khớp evidence cho {arxiv_id}.")
    return metadata_result, references_result


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


def _save_s2_metadata(manifest: dict, cohort_root: Path, arxiv_id: str, result: dict) -> None:
    path, relative = _evidence_file(cohort_root, arxiv_id, "metadata")
    _ensure_directory(path.parent, cohort_root)
    previous = None
    if relative in manifest["files"]:
        previous = _read_json(path)
    history = list(previous.get("history", [])) if isinstance(previous, dict) else []
    if isinstance(previous, dict) and isinstance(previous.get("result"), dict):
        old = previous["result"]
        if old.get("status") not in {"found", "not_found"}:
            history.append(old)
    payload = {"schema_version": PHASE_SCHEMA_VERSION, "result": result, "history": history}
    manifest["files"][relative] = _write_json(path, payload)
    manifest["papers"][arxiv_id]["metadata_status"] = result["status"]
    if result.get("error"):
        manifest["papers"][arxiv_id]["attempt_errors"].append({
            "stage": "metadata", "status": result["status"],
            "http_status": result.get("http_status"), "error": result["error"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })


def _save_s2_references(manifest: dict, cohort_root: Path, arxiv_id: str, snapshot: dict) -> None:
    path, relative = _evidence_file(cohort_root, arxiv_id, "references")
    _ensure_directory(path.parent, cohort_root)
    previous = None
    if relative in manifest["files"]:
        previous = _read_json(path)
    history = list(previous.get("history", [])) if isinstance(previous, dict) else []
    if isinstance(previous, dict) and isinstance(previous.get("snapshot"), dict):
        old = previous["snapshot"]
        if old.get("status") not in {"complete", "not_found"}:
            history.append(old)
    payload = {"schema_version": PHASE_SCHEMA_VERSION, "snapshot": snapshot, "history": history}
    manifest["files"][relative] = _write_json(path, payload)
    manifest["papers"][arxiv_id]["references_status"] = snapshot["status"]
    if snapshot.get("error"):
        manifest["papers"][arxiv_id]["attempt_errors"].append({
            "stage": "references", "status": snapshot["status"],
            "http_status": (snapshot.get("error") or {}).get("http_status"),
            "error": snapshot["error"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })


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


def _save_cached_evidence(manifest: dict, cohort_root: Path, arxiv_id: str,
                          metadata_result: dict | None, references_result: dict | None) -> None:
    if metadata_result is not None:
        _save_s2_metadata(manifest, cohort_root, arxiv_id, metadata_result)
    if references_result is not None:
        _save_s2_references(manifest, cohort_root, arxiv_id, references_result)
    record = manifest["papers"][arxiv_id]
    record["reused_from_corpus_v2"] = {
        "metadata": metadata_result is not None,
        "references": references_result is not None,
    }


def _process_s2_cohort(
    runner: FastProductionRunner,
    selection: dict,
    cohort: int,
    phase_root: Path,
    s2_key: str | None,
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
    limiter = semantic_scholar.RateLimiter(config["min_interval"])
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
        f"{S2_METADATA_BATCH_SIZE} ID.",
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
                        for future_member, future_id in pairs[index - 1:index - 1 + S2_METADATA_BATCH_SIZE]:
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
                            policy_stop, policy_status, fetch_calls, policy_ids = _fetch_s2_metadata_batch(
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
    manifest.update(
        status="complete" if complete else "partial",
        updated_at=datetime.now(timezone.utc).isoformat(),
        stopped=stopped,
        stop_reason=stop_reason,
        session_id=session_id,
        metadata_complete=metadata_complete,
        references_complete=references_complete,
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
    semantic_scholar_api_key: str | None = None,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
    auto_resume_429: bool = False,
    auto_resume_429_max_per_cohort: int = 1,
    auto_resume_429_max_per_session: int = 2,
    auto_resume_429_cooldown_min_seconds: float = 300.0,
    auto_resume_429_cooldown_max_seconds: float = 600.0,
) -> dict:
    """Collect S2 metadata/references, optionally resuming bounded 429 stops."""
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
    runner = FastProductionRunner(local_root=local, drive_root=drive)
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
        with _member_writer_lock(drive, selection):
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


def _validate_s2_cohort(phase_root: Path, selection: dict, cohort: int) -> tuple[dict, dict[str, tuple[dict, dict]]]:
    from src import s2_integration

    cohort_root, checkpoint_path = _cohort_s2_paths(phase_root, cohort)
    manifest = _load_s2_manifest(checkpoint_path, selection, cohort)
    if (manifest.get("status") != "complete"
            or manifest.get("metadata_complete") != len(manifest["ids"])
            or manifest.get("references_complete") != len(manifest["ids"])):
        raise M1PhaseError(f"S2 cohort {cohort} chưa có checkpoint hoàn tất.")
    evidence = {}
    for member, arxiv_id in cohort_pairs(cohort):
        if member != selection["mssv"]:
            continue
        metadata_result, references_result = _read_s2_paper(manifest, cohort_root, arxiv_id)
        if metadata_result is None or references_result is None:
            raise M1PhaseError(f"S2 evidence chưa đầy đủ cho {arxiv_id}.")
        s2_integration.metadata_result(metadata_result, arxiv_id)
        if metadata_result["status"] not in {"found", "not_found"}:
            raise M1PhaseError(
                f"S2 metadata chưa đạt trạng thái cuối cho {arxiv_id}: {metadata_result['status']}"
            )
        status = s2_integration.references_evidence(references_result, arxiv_id)
        if status != "cached" and references_result.get("status") != "not_found":
            raise M1PhaseError(f"S2 references chưa hoàn tất cho {arxiv_id}.")
        if references_result.get("status") not in {"complete", "not_found"}:
            raise M1PhaseError(
                f"S2 references chưa đạt trạng thái cuối cho {arxiv_id}: {references_result.get('status')}"
            )
        evidence[arxiv_id] = (metadata_result, references_result)
    expected_files = set()
    for arxiv_id, record in manifest["papers"].items():
        folder = arxiv_id_to_folder_key(arxiv_id)
        if record.get("metadata_status") != "not_attempted":
            expected_files.add(f"{folder}/metadata.json")
        if record.get("references_status") != "not_attempted":
            expected_files.add(f"{folder}/references.json")
    if set(manifest["files"]) != expected_files:
        raise M1PhaseError(f"S2 checkpoint {cohort} có mục checksum thừa hoặc thiếu.")
    return manifest, evidence


def _validate_source_paper(runner: FastProductionRunner, mssv: str, arxiv_id: str) -> tuple[str, dict | None]:
    from src.storage_integrity import audit_paper_integrity

    try:
        result = audit_paper_integrity(runner.store, mssv, arxiv_id)
        if result.has_integrity_failure:
            return "integrity_failure", None
        manifest = runner.store.read_manifest(mssv, arxiv_id)
    except Exception:
        return "missing_or_invalid", None
    if manifest.get("discovery_status") == "not_found" and manifest.get("status") == "not_found":
        if manifest.get("versions") != {} or result.status != "valid":
            return "integrity_failure", manifest
        return "not_found", manifest
    if result.is_source_complete:
        return "found_source_complete", manifest
    return "source_incomplete", manifest


def _same_reference_snapshot(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("status", "coverage", "items"))


def _preflight_merge(runner: FastProductionRunner, selection: dict,
                     evidence_by_id: dict[str, tuple[dict, dict]]) -> list[dict]:
    """Validate source identity and S2 conflicts before any v2 corpus write."""
    from src import s2_integration

    issues = []
    for mssv, arxiv_id in selection["pairs"]:
        source_status, _manifest = _validate_source_paper(runner, mssv, arxiv_id)
        if source_status in {"missing_or_invalid", "source_incomplete", "integrity_failure"}:
            issues.append({"arxiv_id": arxiv_id, "issue": source_status})
            continue
        metadata_result, references_result = evidence_by_id[arxiv_id]
        if source_status == "not_found":
            if metadata_result["status"] == "found" or references_result.get("status") == "complete" and references_result.get("items"):
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_conflicts_with_arxiv_not_found"})
            continue
        try:
            old_metadata = runner.store.read_metadata(mssv, arxiv_id)
            old_references = runner.store.read_references(mssv, arxiv_id)
            s2_integration.metadata_cached(old_metadata)
            s2_integration.references_evidence(old_references, arxiv_id, for_resume=True)
        except (StorageError, IntegrityError, ValueError, KeyError, TypeError):
            issues.append({"arxiv_id": arxiv_id, "issue": "existing_v2_checkpoint_invalid"})
            continue
        old_s2 = (old_metadata.get("provenance") or {}).get("semantic_scholar")
        if isinstance(old_s2, dict):
            old_snapshot = old_s2.get("snapshot")
            if old_snapshot != metadata_result:
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_metadata_conflict"})
        if old_references.get("status") in {"complete", "not_found"}:
            if not _same_reference_snapshot(old_references, references_result):
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_references_conflict"})
        elif old_references.get("items"):
            if references_result.get("items", [])[:len(old_references["items"])] != old_references["items"]:
                issues.append({"arxiv_id": arxiv_id, "issue": "s2_references_prefix_conflict"})
    return issues


def _merge_s2(runner: FastProductionRunner, selection: dict,
              evidence_by_id: dict[str, tuple[dict, dict]]) -> list[str]:
    from src import s2_integration

    merged = []
    for mssv, arxiv_id in selection["pairs"]:
        source_status, _manifest = _validate_source_paper(runner, mssv, arxiv_id)
        if source_status == "not_found":
            continue
        if source_status != "found_source_complete":
            raise M1PhaseError(f"Nguồn arXiv đổi trạng thái trong lúc hợp nhất: {arxiv_id}")
        metadata_result, references_result = evidence_by_id[arxiv_id]
        old_metadata = runner.store.read_metadata(mssv, arxiv_id)
        existing_snapshot = (old_metadata.get("provenance") or {}).get("semantic_scholar")
        if not isinstance(existing_snapshot, dict):
            merged_metadata = s2_integration.metadata_snapshot(old_metadata, metadata_result)
            runner.store.save_metadata(mssv, arxiv_id, merged_metadata)
        old_references = runner.store.read_references(mssv, arxiv_id)
        if old_references.get("status") not in {"complete", "not_found"}:
            merged_references = s2_integration.reference_result(old_references, references_result)
            runner.store.save_references(mssv, arxiv_id, merged_references)
        merged.append(arxiv_id)
    return merged


def _load_phase_observations(runner: FastProductionRunner, phase_root: Path,
                             selection: dict) -> tuple[list[dict], list[dict]]:
    index_path = phase_root / "session_index.json"
    if not index_path.is_file():
        raise M1PhaseError("Thiếu session index; không thể cộng runtime giữa các phiên.")
    index = _read_json(index_path)
    if (not isinstance(index, dict)
            or index.get("fingerprint") != selection["fingerprint"]
            or not isinstance(index.get("sessions"), list)):
        raise M1PhaseError("Session index không khớp fingerprint lựa chọn.")
    session_ids = [
        row.get("session_id") for row in index["sessions"]
        if isinstance(row, dict) and isinstance(row.get("session_id"), str)
    ]
    if len(session_ids) != len(set(session_ids)):
        raise M1PhaseError("Session index chứa ID phiên trùng.")
    observations = []
    envelopes = []
    for session_id in session_ids:
        envelope_path = runner.reports_dir / "session_envelopes" / f"{session_id}.json"
        if not envelope_path.is_file():
            envelopes.append({})
        else:
            envelopes.append(_read_json(envelope_path))
        for path in sorted((runner.reports_dir / "stage_observations").glob(f"{session_id}_*.json")):
            row = _read_json(path)
            if row.get("session_id") != session_id:
                raise M1PhaseError(f"Observation không khớp phiên: {path.name}")
            observations.append(row)
    return observations, envelopes


def _member_benchmark_summary(runner: FastProductionRunner, phase_root: Path,
                              selection: dict, audit_result: dict) -> dict:
    from src.benchmark_metrics import directory_bytes
    from src.session_evidence import (
        fingerprint_complete_corpus,
        summarize_discovery_timings,
        summarize_paper_timings,
        summarize_workflow,
    )

    observations, envelopes = _load_phase_observations(runner, phase_root, selection)
    assigned_ids = [arxiv_id for _member, arxiv_id in selection["pairs"]]
    starting_state_path = phase_root / "starting_state.json"
    starting_state = (
        _validate_starting_state(_read_json(starting_state_path), selection, starting_state_path)
        if starting_state_path.is_file() else {}
    )
    required = [
        (stage, cohort)
        for cohort in selection["cohorts"]
        for stage in ("source", "s2")
    ]
    required.append(("audit", 0))
    workflow = summarize_workflow(observations, required, session_envelopes=envelopes)
    found_ids = []
    for member, arxiv_id in selection["pairs"]:
        try:
            if runner.store.read_manifest(member, arxiv_id).get("discovery_status") == "found":
                found_ids.append(arxiv_id)
        except Exception:
            continue
    paper_timings = summarize_paper_timings(observations, found_ids)
    assigned_paper_timings = summarize_paper_timings(observations, assigned_ids)
    discovery_timings = summarize_discovery_timings(
        observations, [aid for _member, aid in selection["pairs"]]
    )
    environment_rows = sorted({
        json.dumps(row.get("environment") or {}, sort_keys=True, separators=(",", ":"))
        for row in observations
    })
    runtime_commits = sorted({
        row.get("commit") for row in observations
        if isinstance(row.get("commit"), str) and row.get("commit")
    })
    environment_fingerprint = hashlib.sha256(
        "\n".join(environment_rows + runtime_commits).encode("utf-8")
    ).hexdigest() if environment_rows else None
    data_signature = (
        fingerprint_complete_corpus(runner.store, selection["pairs"])
        if audit_result.get("is_reconciled") else None
    )
    audit_stats = audit_result.get("statistics") or {}
    logical_count = audit_stats.get("logical_size_measured_papers")
    found_count = audit_stats.get("found_papers")
    logical_before = audit_stats.get("average_logical_before_bytes_per_paper")
    logical_after = audit_stats.get("average_logical_after_bytes_per_paper")
    logical_size_complete = (
        type(logical_count) is int and type(found_count) is int and logical_count == found_count
        and ((found_count == 0 and logical_before is None and logical_after is None)
             or (found_count > 0 and type(logical_before) in (int, float)
                 and type(logical_after) in (int, float)
                 and all(value >= 0 and value < float("inf") for value in (logical_before, logical_after))))
    )

    def measured_tree_size(path: Path | None) -> int | None:
        if path is None or not Path(path).is_dir():
            return None
        try:
            return directory_bytes(Path(path))
        except OSError:
            return None

    corpus_size_bytes = measured_tree_size(runner.corpus_dir)
    archive_sizes = []
    for cohort in selection["cohorts"]:
        archive_path = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
        if not archive_path.is_file():
            archive_sizes = []
            break
        try:
            archive_sizes.append(directory_bytes(archive_path))
        except OSError:
            archive_sizes = []
            break
    archive_size_bytes = sum(archive_sizes) if len(archive_sizes) == len(selection["cohorts"]) else None
    export_info = audit_result.get("export") or {}
    export_dir = export_info.get("output_dir") if audit_result.get("export_status") == "exported" else None
    export_size_bytes = measured_tree_size(Path(export_dir)) if isinstance(export_dir, str) else None
    bbl_artifact_root = getattr(runner, "bbl_artifact_dir", None)
    try:
        bbl_artifact_size_bytes = directory_bytes(Path(bbl_artifact_root)) if bbl_artifact_root else None
    except OSError:
        bbl_artifact_size_bytes = None
    storage_size_complete = all(
        type(value) is int and value >= 0
        for value in (corpus_size_bytes, archive_size_bytes, export_size_bytes)
    )
    storage_size_metrics = {
        "status": "complete" if storage_size_complete else "incomplete",
        "corpus_bytes": corpus_size_bytes,
        "source_archives_bytes": archive_size_bytes,
        "official_export_bytes": export_size_bytes,
        "derived_bibliography_artifacts_bytes": bbl_artifact_size_bytes,
        "derived_bibliography_status": (
            "collected" if type(bbl_artifact_size_bytes) is int else "not_collected"
        ),
        "derived_bibliography_scope": "configured shared artifact root; may include other selections and is not summed across members",
        "final_output_storage_bytes": (
            archive_size_bytes + export_size_bytes
            if archive_size_bytes is not None and export_size_bytes is not None else None
        ),
        "final_output_scope": "selected source archives plus official export directory; local corpus and derived bibliography artifacts are reported separately",
        "size_scope": "logical regular-file bytes; archive size includes selected cohort .tar.gz files, not their sidecars",
    }
    all_m1 = (
        audit_result.get("is_reconciled") is True
        and workflow.get("status") == "measured_intervals"
        and workflow.get("system_ram_status") == "collected"
        and workflow.get("resource_scope_status") == "collected"
        and assigned_paper_timings.get("status") == "complete"
        and discovery_timings.get("status") == "complete"
        and data_signature is not None
        and len(runtime_commits) == 1
        and logical_size_complete
        and storage_size_complete
    )
    summary = {
        "schema_version": PHASE_SCHEMA_VERSION,
        "mssv": selection["mssv"],
        "run_scope": selection["scope"],
        "cohorts": selection["cohorts"],
        "expected_id_count": selection["expected_count"],
        "selection_fingerprint": selection["group_fingerprint"],
        "member_selection_fingerprint": selection["fingerprint"],
        "campaign_id": selection["fingerprint"],
        "data_signature": data_signature,
        "environment_fingerprint": environment_fingerprint,
        "runtime_commit": runtime_commits[0] if len(runtime_commits) == 1 else None,
        "runtime_commits": runtime_commits,
        "starting_state_fingerprint": starting_state.get("starting_state_fingerprint"),
        "starting_state_empty": starting_state.get("starting_state_empty"),
        "execution_mode": (
            "from_scratch" if starting_state.get("starting_state_empty") is True
            else "resume" if starting_state.get("starting_state_empty") is False
            else "unknown"
        ),
        "discovery_arm": "arxiv_only",
        "session_ids": [row["session_id"] for row in _read_json(phase_root / "session_index.json")["sessions"]],
        "status": workflow.get("status"),
        "known_active_seconds": workflow.get("known_active_seconds"),
        "active_seconds": workflow.get("active_seconds"),
        "end_to_end_wall_seconds": workflow.get("end_to_end_wall_seconds"),
        "end_to_end_complete": bool(all_m1 and selection["expected_count"] == 5000),
        "sampled_peak_ram_bytes": workflow.get("sampled_peak_ram_bytes"),
        "sampled_average_ram_bytes": workflow.get("sampled_average_ram_bytes"),
        "process_ram_source": workflow.get("process_ram_source"),
        "process_ram_scope": workflow.get("process_ram_scope"),
        "system_ram_status": workflow.get("system_ram_status"),
        "sampled_peak_system_ram_bytes": workflow.get("sampled_peak_system_ram_bytes"),
        "sampled_average_system_ram_bytes": workflow.get("sampled_average_system_ram_bytes"),
        "system_ram_scope": workflow.get("system_ram_scope"),
        "system_ram_source": workflow.get("system_ram_source"),
        "sampled_peak_disk_bytes": workflow.get("sampled_peak_disk_bytes"),
        "disk_roots": workflow.get("disk_roots"),
        "paper_timings": paper_timings,
        "assigned_paper_timings": assigned_paper_timings,
        "assigned_paper_count": len(assigned_ids),
        "average_assigned_paper_seconds": assigned_paper_timings.get("average_seconds"),
        "discovery_timings": discovery_timings,
        "measurement_scope": (
            "Sequential arXiv source and S2 intervals across recorded sessions; "
            "retry/delay and audit/export session time are included, inter-session gaps "
            "and environment installation are excluded."
        ),
        "resource_measurement_scope": (
            "Crawler RAM is sampled process-tree RSS; system RAM is sampled host-wide "
            "used RAM and includes unrelated Colab processes. Disk is sampled logical "
            "regular-file bytes under each stage observation's disjoint roots. Sampled "
            "peaks can miss short-lived files or memory spikes."
        ),
        "storage_size_metrics": storage_size_metrics,
        "corpus_size_bytes": corpus_size_bytes,
        "archive_size_bytes": archive_size_bytes,
        "export_size_bytes": export_size_bytes,
        "final_output_storage_bytes": storage_size_metrics["final_output_storage_bytes"],
        "final_output_storage_scope": storage_size_metrics["final_output_scope"],
        "measurement_protocol": (
            "m1-independent-phases-v2; source/S2 retry and automatic 429 cooldown included; "
            "all-assigned-ID average; process RSS and host-wide RAM sampled; inter-session gaps excluded"
        ),
        "logical_size_metrics": {
            key: (audit_result.get("statistics") or {}).get(key)
            for key in (
                "logical_size_measured_papers",
                "average_logical_before_bytes_per_paper",
                "average_logical_after_bytes_per_paper",
                "logical_size_scope",
            )
        },
        "logical_size_status": "complete" if logical_size_complete else "incomplete",
        "reference_metadata_success": {
            "complete_records": audit_stats.get("reference_metadata_complete_records"),
            "eligible_records": audit_stats.get("total_complete_reference_records"),
            "success_rate_percent": audit_stats.get("reference_metadata_success_rate"),
            "definition": audit_stats.get("reference_metadata_success_definition"),
        },
        "reference_arxiv_id_coverage": {
            "identified_records": audit_stats.get("reference_arxiv_id_records"),
            "eligible_records": audit_stats.get("total_complete_reference_records"),
            "coverage_rate_percent": audit_stats.get("reference_arxiv_id_coverage_rate"),
            "definition": audit_stats.get("reference_arxiv_id_coverage_definition"),
        },
        "average_references_per_paper": {
            "value": audit_stats.get("average_references_per_paper"),
            "eligible_papers": audit_stats.get("complete_reference_papers"),
            "raw_reference_records": audit_stats.get("total_complete_reference_records"),
            "definition": audit_stats.get("average_references_per_paper_definition"),
        },
        "missing_stages": workflow.get("missing_stages", []),
        "session_gaps_seconds": workflow.get("session_gaps_seconds"),
    }
    return summary


def run_m1_audit_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    cohort_limit: int | None = None,
    output_dir: str | Path | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Validate both phases, safely merge S2, then export only complete M1 data."""
    from src.reporting import compute_m1_statistics
    from src.storage_integrity import audit_cohort_integrity
    from src.fast_production import verify_cohort_handoff
    from src.export import export_official_datasets
    from src.bbl_handling import audit_bbl_sources

    selection = build_member_selection(mssv, cohort_limit)
    drive = Path(drive_root).expanduser().resolve()
    local = Path(local_root).expanduser().resolve()
    runner = FastProductionRunner(local_root=local, drive_root=drive)
    budget = SessionBudget(budget_seconds)
    session_id, envelope, started = _session_start(runner, selection, "audit")
    phase_root = _persist_selection(drive, selection)

    def audit_and_reconcile() -> dict:
        from src.kaggle_candidates import validate_discrepancy_checkpoints

        source_issues = []
        source_states = {}
        discrepancy_checkpoint_root = phase_root / "arxiv" / "kaggle_discrepancy_checkpoints"
        discrepancy_evidence_root = phase_root / "arxiv" / "kaggle_discrepancies"
        kaggle_evidence_issues = []
        kaggle_evidence_summary = {"status": "valid", "verified_ids": 0}
        try:
            verified_discrepancies = validate_discrepancy_checkpoints(
                discrepancy_checkpoint_root, discrepancy_evidence_root,
            )
            selected_ids = {arxiv_id for _member, arxiv_id in selection["pairs"]}
            unrelated = [item.get("arxiv_id") for item in verified_discrepancies
                         if item.get("arxiv_id") not in selected_ids]
            if unrelated:
                raise ValueError("discrepancy checkpoint contains IDs outside this selection")
            kaggle_evidence_summary["verified_ids"] = len(verified_discrepancies)
            kaggle_evidence_summary["checkpoints"] = verified_discrepancies
        except (OSError, ValueError, KeyError, TypeError) as exc:
            kaggle_evidence_summary = {"status": "invalid", "error_type": type(exc).__name__}
            kaggle_evidence_issues.append({
                "issue": "kaggle_discrepancy_evidence_invalid",
                "error_type": type(exc).__name__,
            })
        for cohort in selection["cohorts"]:
            state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
            state = _source_state_manifest(state_path, selection, cohort)
            source_states[str(cohort)] = {
                key: state.get(key) for key in (
                    "status", "source_complete_papers", "expected_count",
                    "archive_sha256", "stop_reason", "updated_at",
                    "kaggle_status", "kaggle_snapshot_sha256",
                    "kaggle_index_sha256", "kaggle_policy_version",
                )
            }
            # Tiếp tục và xác minh mọi archive đã có trong phạm vi này.
            runner.restore_cohort(cohort)
            for member, arxiv_id in cohort_pairs(cohort):
                if member != selection["mssv"]:
                    continue
                status, _ = _validate_source_paper(runner, member, arxiv_id)
                if status not in {"found_source_complete", "not_found"}:
                    source_issues.append({"arxiv_id": arxiv_id, "issue": status})

        s2_evidence = {}
        s2_manifests = {}
        s2_issues = []
        for cohort in selection["cohorts"]:
            try:
                manifest, evidence = _validate_s2_cohort(phase_root, selection, cohort)
                s2_manifests[str(cohort)] = manifest
                s2_evidence.update(evidence)
            except M1PhaseError as exc:
                s2_issues.append({"cohort": cohort, "issue": str(exc)})

        preflight_issues = []
        merge_conflicts = []
        if not source_issues and not s2_issues and len(s2_evidence) == selection["expected_count"]:
            preflight_issues = _preflight_merge(runner, selection, s2_evidence)
            merge_conflicts.extend(preflight_issues)
        issues = source_issues + s2_issues + preflight_issues + kaggle_evidence_issues
        merged_ids = []
        if not issues:
            if budget.should_stop():
                merge_conflicts.append({"issue": "audit_session_budget"})
            else:
                try:
                    merged_ids = _merge_s2(runner, selection, s2_evidence)
                    # Làm mới archive bền vững sau khi đối soát S2; giữ lại
                    # archive của pha mã nguồn trước khi công bố bản thay thế.
                    for cohort in selection["cohorts"]:
                        archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
                        if archive.exists():
                            runner.backup_cohort_archive(cohort, require_sidecars=True)
                        runner._archive_cohort(cohort, archive)
                except Exception as exc:
                    merge_conflicts.append({
                        "issue": "merge_or_archive_failed",
                        "error_type": type(exc).__name__,
                        "detail": str(exc),
                    })

        bbl_audit = None
        bbl_audit_error = None
        try:
            bbl_audit = audit_bbl_sources(
                runner.store,
                selection["pairs"],
                output_dir=runner.reports_dir / "derived_bibliography" / session_id,
                report_path=runner.reports_dir / f"bbl_handling_{session_id}.json",
                session_id=session_id,
            )
        except Exception as exc:
            bbl_audit_error = {"error": type(exc).__name__, "detail": str(exc)}

        cohort_audits = {}
        all_handoffs = True
        all_cohort_audits = True
        for cohort in selection["cohorts"]:
            pairs = [(member, aid) for member, aid in cohort_pairs(cohort) if member == selection["mssv"]]
            try:
                integrity = audit_cohort_integrity(runner.store, pairs, cohort_number=cohort)
                counts = runner.count_m1_completed_papers(cohort)
                handoff = verify_cohort_handoff(runner, cohort, inspect_members=True)
                handoff_ok = handoff.get("archive_state") in {"m1_handoff_verified", "handoff_verified"}
                integrity_payload = integrity.to_dict()
                integrity_ok = (
                    integrity_payload.get("total_candidates") == len(pairs)
                    and integrity_payload.get("valid_count") == len(pairs)
                    and not any(integrity_payload.get(key, 0) for key in (
                        "invalid_count", "missing_count", "incomplete_count", "corrupted_count"
                    ))
                )
            except Exception as exc:
                integrity = {"is_reconciled": False, "integrity_failed_papers": len(pairs), "error": type(exc).__name__, "detail": str(exc)}
                counts = {"is_cohort_complete": False, "unprocessed": len(pairs), "integrity_failures": len(pairs)}
                handoff = {"archive_state": "invalid", "error": type(exc).__name__, "detail": str(exc)}
                handoff_ok = False
                integrity_payload = integrity
                integrity_ok = False
            integrity_payload["is_reconciled"] = integrity_ok
            cohort_audits[str(cohort)] = {
                "integrity": integrity_payload,
                "m1_counts": counts,
                "handoff": handoff,
                "s2_status": (s2_manifests.get(str(cohort)) or {}).get("status", "missing"),
            }
            all_handoffs = all_handoffs and handoff_ok
            all_cohort_audits = all_cohort_audits and integrity_ok and counts.get("is_cohort_complete") is True

        stats = compute_m1_statistics(
            runner.store,
            selection["pairs"],
            report_scope=selection["scope"],
        )
        data_complete = (
            not issues
            and not merge_conflicts
            and bbl_audit_error is None
            and stats.get("is_reconciled") is True
            and stats.get("integrity_failed_papers", 0) == 0
            and stats.get("unprocessed_papers", selection["expected_count"]) == 0
            and stats.get("m1_complete_papers", 0) + stats.get("confirmed_not_found", 0)
            == selection["expected_count"]
            and all_cohort_audits
            and all_handoffs
        )
        export_result = None
        export_error = None
        if data_complete:
            destination = Path(output_dir).expanduser().resolve() if output_dir else drive / "exports" / selection["fingerprint"]
            export_result = export_official_datasets(
                runner.store,
                [tuple(pair) for pair in selection["pairs"]],
                destination,
                require_m1_complete=True,
            )
        else:
            export_error = "Export blocked: both phase checkpoints and full M1 audit are required."
        report = {
            "schema_version": PHASE_SCHEMA_VERSION,
            "mssv": selection["mssv"],
            "selection_fingerprint": selection["fingerprint"],
            "scope": selection["scope"],
            "expected_id_count": selection["expected_count"],
            "source_issues": source_issues,
            "source_checkpoints": source_states,
            "kaggle_evidence": kaggle_evidence_summary,
            "kaggle_evidence_issues": kaggle_evidence_issues,
            "s2_issues": s2_issues,
            "merge_conflicts": merge_conflicts,
            "merged_s2_ids": len(merged_ids),
            "bbl_audit": bbl_audit,
            "bbl_audit_error": bbl_audit_error,
            "cohorts": cohort_audits,
            "statistics": stats,
            "is_reconciled": data_complete,
            "integrity_failed_papers": stats.get("integrity_failed_papers", 0),
            "unprocessed_papers": stats.get("unprocessed_papers"),
            "export_status": "exported" if export_result else "blocked",
            "export": export_result,
            "export_error": export_error,
            "stop_reason": None if data_complete else "incomplete_or_conflicting_phase_evidence",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        report_path = runner.reports_dir / f"m1_audit_{selection['mssv']}_{selection['fingerprint'][:16]}.json"
        _write_json(report_path, report)
        report["report_path"] = str(report_path)
        return report

    try:
        with _member_writer_lock(drive, selection):
            _record_session(phase_root, selection, session_id, "audit")
            result = observe_stage(
                runner,
                "audit",
                0,
                audit_and_reconcile,
                session_id=session_id,
                configuration={
                    "selection_fingerprint": selection["fingerprint"],
                    "mssv": selection["mssv"],
                    "run_scope": selection["scope"],
                    "expected_id_count": selection["expected_count"],
                    "session_budget_seconds": budget_seconds,
                },
            )
            finish_session_envelope(envelope, started_monotonic=started)
            benchmark = _member_benchmark_summary(runner, phase_root, selection, result)
            result["benchmark"] = benchmark
            report_path = Path(result["report_path"])
            saved_report = _read_json(report_path)
            saved_report["benchmark"] = benchmark
            _write_json(report_path, saved_report)
            summary_path = runner.reports_dir / (
                f"benchmark_observed_{selection['mssv']}_{selection['fingerprint'][:16]}.json"
            )
            _write_json(summary_path, benchmark)
            result["benchmark_summary_path"] = str(summary_path)
            return result
    finally:
        envelope_value = _read_json(envelope)
        if envelope_value.get("status") == "running":
            finish_session_envelope(envelope, started_monotonic=started)


def run_m1_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    kaggle_token: str | None = None,
    semantic_scholar_api_key: str | None = None,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Backward-compatible sequential source → S2 → audit M1 entry point."""
    started = time.monotonic()

    def remaining_budget() -> float:
        return max(0.001, budget_seconds - (time.monotonic() - started))

    arxiv_result = run_m1_arxiv_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    if arxiv_result["stopped"]:
        return {"stage": "arxiv", "arxiv": arxiv_result, "s2": None, "audit": None}
    s2_result = run_m1_s2_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        semantic_scholar_api_key=semantic_scholar_api_key,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    if s2_result["stopped"]:
        return {"stage": "s2", "arxiv": arxiv_result, "s2": s2_result, "audit": None}
    audit_result = run_m1_audit_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
    )
    return {
        "stage": "audit",
        "mssv": mssv,
        "selection_fingerprint": arxiv_result["selection_fingerprint"],
        "arxiv": arxiv_result,
        "s2": s2_result,
        "audit": audit_result,
        "handoff_status": "verified" if audit_result.get("is_reconciled") else "blocked",
        "export": audit_result.get("export"),
    }

"""Resumable arXiv and Kaggle source phase."""
from __future__ import annotations

import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.fast_production import FastProductionRunner, cohort_pairs
from src.session_evidence import (
    SessionBudget,
    annotate_session_envelope,
    finish_session_envelope,
    observe_stage,
)
from src.storage import IntegrityError, StorageError, _atomic_json

from .checkpoint import (
    _ensure_directory,
    _ensure_starting_state,
    _member_writer_lock,
    _persist_selection,
    _read_json,
    _record_session,
    _session_start,
    _sha256_file,
    _write_json,
)
from .common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
    _required_m1_secret,
    build_member_selection,
)


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
    kaggle_session_id = saved.get("kaggle_session_id")
    if kaggle_session_id is not None and (
        not isinstance(kaggle_session_id, str)
        or len(kaggle_session_id) != 32
        or any(char not in "0123456789abcdef" for char in kaggle_session_id)
    ):
        raise M1PhaseError(f"Mã phiên Kaggle trong checkpoint sai định dạng: {path}")
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


def _kaggle_source_provenance_issue(
    runner: FastProductionRunner, state: dict, cohort: int,
) -> dict | None:
    """Xác minh checkpoint source dẫn chiếu snapshot và index Kaggle bắt buộc."""
    session_id = state.get("kaggle_session_id")
    snapshot_sha = state.get("kaggle_snapshot_sha256")
    index_sha = state.get("kaggle_index_sha256")
    if (
        state.get("kaggle_status") != "ready"
        or state.get("kaggle_policy_version") != "m1-kaggle-reconcile-v2"
        or not isinstance(session_id, str)
        or len(session_id) != 32
        or any(char not in "0123456789abcdef" for char in session_id)
        or not isinstance(snapshot_sha, str)
        or len(snapshot_sha) != 64
        or any(char not in "0123456789abcdef" for char in snapshot_sha)
        or not isinstance(index_sha, str)
        or len(index_sha) != 64
        or any(char not in "0123456789abcdef" for char in index_sha)
    ):
        return {"cohort": cohort, "issue": "required_kaggle_provenance_missing"}

    preparation_path = runner.reports_dir / f"kaggle_preparation_{session_id}.json"
    try:
        preparation = _read_json(preparation_path)
        if (
            not isinstance(preparation, dict)
            or preparation.get("session_id") != session_id
            or preparation.get("status") != "ready"
            or preparation.get("snapshot_sha256") != snapshot_sha
            or preparation.get("index_sha256") != index_sha
            or not isinstance(preparation.get("index_path"), str)
        ):
            raise ValueError("preparation evidence mismatch")
        index_path_text = preparation["index_path"]
        index_path = Path(index_path_text).expanduser()
        index_root = runner.drive_root / "kaggle_indices"
        resolved_root = index_root.resolve()
        resolved_index = index_path.resolve(strict=True)
        if (
            index_path.is_symlink()
            or not resolved_index.is_file()
            or not resolved_index.is_relative_to(resolved_root)
            or _sha256_file(resolved_index) != index_sha
        ):
            raise ValueError("index checksum mismatch")
        index = _read_json(resolved_index)
        if index.get("snapshot_sha256") != snapshot_sha:
            raise ValueError("index snapshot mismatch")
    except (OSError, ValueError, TypeError, KeyError, M1PhaseError):
        return {"cohort": cohort, "issue": "required_kaggle_provenance_invalid"}
    return None


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
    kaggle_token: str,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
    auto_resume_429: bool = False,
    auto_resume_429_max_per_cohort: int = 1,
    auto_resume_429_max_per_session: int = 2,
    auto_resume_429_cooldown_min_seconds: float = 300.0,
    auto_resume_429_cooldown_max_seconds: float = 600.0,
    runner_factory: Callable[..., FastProductionRunner],
    mark_archive_arxiv_only: Callable[[FastProductionRunner, int], None],
) -> dict:
    """Xác minh ID được giao và lưu mã nguồn của mọi phiên bản, không chạy S2.

    Tự tiếp tục có giới hạn chỉ áp dụng cho HTTP 429 ở discovery khi checkpoint
    đã được xác minh. Snapshot và index Kaggle hợp lệ phải sẵn sàng trước đó.
    """
    kaggle_token = _required_m1_secret(kaggle_token, "KAGGLE_API_TOKEN")
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
    runner = runner_factory(
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
    kaggle_evidence: dict[str, Any] = {"status": "unavailable", "reason": "not_prepared"}
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
            snapshot_result = observe_stage(
                runner, "snapshot_prepare", 0,
                lambda: prepare_snapshot(kaggle_cache, token=kaggle_token),
                session_id=session_id,
                configuration={"snapshot_scope": "shared_cache", "accelerator": "CPU"},
            )
            kaggle_evidence = {key: value for key, value in snapshot_result.items()
                               if key not in {"snapshot_path", "token"}}
            if snapshot_result.get("status") != "ready":
                kaggle_evidence["status"] = "unavailable"
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
                    kaggle_evidence.update(status="unavailable", reason="candidate_index_failed",
                                           error_type=type(exc).__name__)
            prep_path = runner.reports_dir / f"kaggle_preparation_{session_id}.json"
            try:
                _atomic_json(prep_path, {"session_id": session_id, **kaggle_evidence})
            except OSError:
                # Chỉ dùng Kaggle khi xuất xứ của dữ liệu đã được ghi bền vững.
                index_path = None
                kaggle_evidence = {
                    **kaggle_evidence, "status": "unavailable",
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
            if index_path is None:
                stopped = True
                stop_reason = {
                    "type": "required_source_unavailable",
                    "stage": "kaggle_snapshot_or_index",
                    "reason": kaggle_evidence.get("reason", "snapshot_unavailable"),
                }
            for cohort in (selection["cohorts"] if index_path is not None else []):
                state_path = phase_root / "arxiv" / f"cohort_{cohort:04d}.json"
                _ensure_directory(state_path.parent, drive)
                state = _source_state_manifest(state_path, selection, cohort)
                state.update(
                    kaggle_status=kaggle_evidence.get("status"),
                    kaggle_snapshot_sha256=kaggle_evidence.get("snapshot_sha256"),
                    kaggle_index_sha256=kaggle_evidence.get("index_sha256"),
                    kaggle_session_id=session_id,
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
                    mark_archive_arxiv_only(runner, cohort_number)
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

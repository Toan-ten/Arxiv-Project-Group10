"""Điều phối thống nhất quy trình thu thập và kiểm toán M1 trên Colab."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.bbl_handling import audit_bbl_sources
from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
    member_cohort_range,
    verify_cohort_handoff,
)
from src.kaggle_candidates import prepare_snapshot, reuse_or_build_index
from src.session_evidence import (
    SessionBudget,
    annotate_session_envelope,
    finish_session_envelope,
    fingerprint_complete_corpus,
    load_campaign_state,
    observe_stage,
    revision_history_counts,
    save_campaign_state,
    start_session_envelope,
    summarize_workflow,
)
from src.storage import _atomic_json, arxiv_id_to_folder_key


SESSION_BUDGET_SECONDS = 10 * 60 * 60
S2_RETRY_POLICY = {
    "max_attempts": 8,
    "max_retry_wait": 600.0,
    "backoff_base": 3.0,
    "backoff_cap": 60.0,
}
MIN_FREE_BYTES = 2 * 1024**3


class _WorkflowProgress:
    """Hiển thị giai đoạn hiện tại và nhịp tiến độ cho ô chạy M1."""

    def __init__(self, interval_seconds: float = 30.0):
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._stage = None
        self._detail = None
        self._stage_started = None
        self._session_started = time.monotonic()

    @staticmethod
    def _print(message: str) -> None:
        print(f"[M1] {message}", flush=True)

    def start(self, mssv: str, cohorts: list[int], scope: str, budget_seconds: float) -> None:
        cohort_text = f"{cohorts[0]}..{cohorts[-1]} ({len(cohorts)} lô)" if cohorts else "không có lô"
        scope_text = (
            "toàn bộ dải ID được giao"
            if scope == "member_full_assignment"
            else "mẫu kiểm tra theo cùng quy trình"
        )
        self._print(
            f"Bắt đầu phiên cho MSSV {mssv}; phạm vi {cohort_text}; "
            f"chạy {scope_text}; ngân sách {budget_seconds / 3600:g} giờ."
        )
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with self._lock:
                stage = self._stage
                detail = self._detail
                elapsed = time.monotonic() - (self._stage_started or self._session_started)
            if stage:
                extra = f"; {detail}" if detail else ""
                self._print(f"Đang {stage}{extra}; đã chạy {elapsed:.0f} giây.")

    def begin(self, stage: str) -> None:
        with self._lock:
            self._stage = stage
            self._detail = None
            self._stage_started = time.monotonic()
        self._print(f"Bắt đầu {stage}.")

    def set_detail(self, detail: str) -> None:
        with self._lock:
            self._detail = detail

    def note(self, message: str) -> None:
        self._print(message)

    def end(
        self,
        stage: str,
        *,
        result: dict | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            started = self._stage_started or time.monotonic()
            elapsed = max(0.0, time.monotonic() - started)
            self._stage = None
            self._detail = None
            self._stage_started = None
        if error is not None:
            self._print(f"Dừng {stage} sau {elapsed:.1f} giây do {type(error).__name__}.")
            return
        detail = _progress_result_detail(result or {})
        suffix = f"; {detail}" if detail else ""
        self._print(f"Kết thúc {stage} sau {elapsed:.1f} giây{suffix}.")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _progress_result_detail(result: dict) -> str | None:
    """Tóm tắt kết quả thực tế của giai đoạn, không suy diễn trạng thái."""
    if "snapshot_sha256" in result:
        source = {"downloaded": "vừa tải", "cached": "bản lưu đã xác minh"}.get(
            result.get("source"), "không rõ nguồn"
        )
        status = "sẵn sàng" if result.get("status") == "ready" else "chuyển sang arXiv"
        return f"snapshot {status}; nguồn {source}"
    if result.get("status") == "fallback":
        reason = {
            "missing_kaggle_token": "thiếu khóa Kaggle",
            "snapshot_unavailable": "không có snapshot hợp lệ",
            "candidate_index_failed": "không lập được chỉ mục ứng viên",
            "snapshot_prepare_failed": "không chuẩn bị được snapshot",
        }.get(result.get("reason"), "snapshot không dùng được")
        return f"{reason}; tiếp tục xác minh bằng arXiv"
    if isinstance(result.get("index"), dict):
        index = result["index"]
        reused = "dùng lại chỉ mục đã có" if result.get("reused") else "đã lập chỉ mục mới"
        return f"{len(index.get('candidates', {}))} ứng viên; {reused}"
    fast = result.get("fast_runner")
    if isinstance(fast, dict):
        completed = fast.get("completed_papers", 0)
        target = fast.get("target_papers", 0)
        stop_reason = fast.get("stop_reason") or {}
        stopped_text = _stop_reason_text(stop_reason) if fast.get("stopped") else ""
        status_text = (
            "đã dừng" + (f": {stopped_text}" if stopped_text else "")
            if fast.get("stopped")
            else "đã trả kết quả"
        )
        return (
            f"giai đoạn mã nguồn có trạng thái cuối {completed}/{target} ID; "
            f"mới đạt trạng thái cuối {fast.get('newly_completed_papers', 0)} ID; "
            f"trạng thái {status_text}"
        )
    if "attempted_papers" in result:
        stopped = bool(result.get("stopped"))
        stop_text = _stop_reason_text(result.get("stop_reason") or {}) if stopped else ""
        status_text = (
            "đã dừng" + (f": {stop_text}" if stop_text else "")
            if stopped
            else "đã trả kết quả"
        )
        return (
            f"đã xử lý S2 với {result.get('attempted_papers', 0)} ID; "
            f"siêu dữ liệu hoàn tất {result.get('metadata_complete', 0)}; "
            f"tài liệu tham khảo hoàn tất {result.get('references_complete', 0)}; "
            f"trạng thái {status_text}"
        )
    if "is_reconciled" in result:
        return (
            f"đối chiếu {'đạt' if result.get('is_reconciled') else 'chưa đạt'}; "
            f"lỗi toàn vẹn {result.get('integrity_failed_papers', 0)}; "
            f"ID chưa xử lý {result.get('unprocessed_papers', 'chưa xác định')}"
        )
    if result.get("status") == "skipped":
        reasons = {
            "integrity_failure": "phát hiện lỗi toàn vẹn",
            "already_m1_complete": "lô đã đạt M1",
            "m1_audit_incomplete": "kiểm toán chưa đạt",
        }
        return f"bỏ qua vì {reasons.get(result.get('reason'), 'không đủ điều kiện')}"
    if result.get("archive_state") in {"m1_handoff_verified", "handoff_verified"}:
        return "archive đã được xác minh"
    return None


def _stop_reason_text(reason: dict) -> str:
    status = reason.get("http_status")
    if status in {403, 429}:
        return f"dịch vụ trả HTTP {status}"
    if reason.get("type") == "session_budget":
        return "đã chạm ngân sách phiên"
    if reason.get("type") == "integrity_failure":
        return "phát hiện lỗi toàn vẹn"
    if reason.get("type") == "interrupted":
        return "đã tạm dừng thủ công"
    return "cần xem lý do dừng trong kết quả cuối"


def _stage_label(stage: str, cohort: int, cohort_position: int, cohort_count: int) -> str:
    labels = {
        "snapshot_prepare": "chuẩn bị snapshot Kaggle",
        "kaggle_index": "lập chỉ mục ứng viên Kaggle",
        "log_backup": "sao lưu log phiên trước",
        "archive_backup": f"sao lưu archive lô {cohort}",
        "restore": f"khôi phục checkpoint lô {cohort}",
        "source": f"tải mã nguồn và xác minh lịch sử phiên bản lô {cohort}",
        "s2_restore": f"khôi phục checkpoint trước khi lấy dữ liệu S2 lô {cohort}",
        "s2": f"lấy siêu dữ liệu và tài liệu tham khảo lô {cohort}",
        "handoff_audit": f"kiểm tra archive bàn giao lô {cohort}",
        "audit": "kiểm toán dữ liệu",
        "dataset_export": "xuất dữ liệu đã qua kiểm toán",
        "audit_signature": "tính dấu vân tay dữ liệu",
        "size_accounting": "đo kích thước dữ liệu",
    }
    label = labels.get(stage, stage)
    if cohort and stage in {"archive_backup", "restore", "source", "s2_restore", "s2", "handoff_audit"}:
        label += f" ({cohort_position}/{cohort_count} lô)"
    return label


def _commit_id() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[ĐÃ ẨN]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, secrets) for item in value)
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _load_campaign_observations(report_root: Path, session_ids: set[str]) -> list[dict]:
    root = report_root / "stage_observations"
    records = []
    if not root.is_dir():
        return records
    for path in sorted(root.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if path.name.split("_", 1)[0] in session_ids:
                records.append({})
            continue
        if row.get("session_id") in session_ids:
            records.append(row)
    return records


def _tree_size_bytes(root: Path) -> int | None:
    """Tính dung lượng tệp thường dưới một thư mục, không đi theo liên kết."""
    if root.is_symlink():
        return None
    if not root.exists():
        return 0
    total = 0
    pending = [root]
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            return None
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(mode):
                        total += entry.stat(follow_symlinks=False).st_size
        return total
    except OSError:
        return None


def _regular_file_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _close_session(path: Path, started: float, outcome: str, detail: dict) -> dict:
    annotate_session_envelope(path, outcome=outcome, outcome_detail=detail)
    return finish_session_envelope(path, started_monotonic=started)


def _select_cohorts(mssv: str, cohort_limit: int | None) -> tuple[list[int], list[int], str]:
    if not isinstance(mssv, str) or not mssv.strip():
        raise ValueError("Cần nhập MSSV của thành viên.")
    assigned = member_cohort_range(mssv.strip())
    if cohort_limit is not None:
        if isinstance(cohort_limit, bool) or not isinstance(cohort_limit, int):
            raise ValueError("Giới hạn số lô phải là số nguyên dương hoặc None.")
        if not 1 <= cohort_limit <= len(assigned):
            raise ValueError(f"Giới hạn số lô phải nằm trong khoảng 1..{len(assigned)}.")
    selected = assigned if cohort_limit is None else assigned[:cohort_limit]
    scope = "member_full_assignment" if cohort_limit is None else "member_subset_same_pipeline"
    return assigned, selected, scope


def run_m1_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    kaggle_token: str | None = None,
    semantic_scholar_api_key: str | None = None,
    cohort_limit: int | None = None,
    budget_seconds: float = SESSION_BUDGET_SECONDS,
) -> dict:
    """Chạy một lượt M1 thống nhất trên dải ID được giao cho một thành viên.

    ``cohort_limit`` chỉ thu hẹp số lô đầu tiên của dải thành viên; các giai
    đoạn, kiểm tra, lưu checkpoint và tiêu chí bàn giao vẫn giữ nguyên.
    """
    mssv = mssv.strip() if isinstance(mssv, str) else mssv
    assigned_cohorts, cohorts, scope = _select_cohorts(mssv, cohort_limit)
    session_id = uuid.uuid4().hex
    commit = _commit_id()
    all_pairs = [pair for cohort in cohorts for pair in cohort_pairs(cohort)]
    group_pairs = [pair for cohort in range(1, 201) for pair in cohort_pairs(cohort)]
    selection_fingerprint = hashlib.sha256(
        json.dumps(group_pairs, separators=(",", ":")).encode()
    ).hexdigest()
    member_fingerprint = hashlib.sha256(
        json.dumps(all_pairs, separators=(",", ":")).encode()
    ).hexdigest()

    drive_root = Path(drive_root).resolve()
    local_root = Path(local_root).resolve()
    budget = SessionBudget(budget_seconds)
    runner = FastProductionRunner(
        local_root=local_root,
        drive_root=drive_root,
        retry_429=True,
        retry_wait_budget=300.0,
        timeout=30.0,
    )
    started_monotonic = time.perf_counter()
    envelope_path = start_session_envelope(runner.reports_dir, session_id)
    prior_index = os.environ.get("M1_KAGGLE_CANDIDATE_INDEX")
    prior_s2 = {name: os.environ.get(name) for name in ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")}
    safe_secrets = tuple(secret.strip() for secret in (kaggle_token, semantic_scholar_api_key) if secret and secret.strip())
    outcome = "running"
    outcome_detail: dict = {}
    kaggle_evidence: dict = {"status": "not_attempted", "reason": None}
    index_evidence: dict | None = None
    index_path = None
    snapshot_path = None
    cohort_results: dict[str, dict] = {}
    stop_reason = None
    export_result = None
    export_error = None
    report_path = None
    benchmark_path = None
    audit_stats = None
    bbl_audit = None
    handoff_issues: list[str] = []
    envelope = None

    progress = _WorkflowProgress()

    def observe_with_progress(stage: str, cohort: int, operation, **kwargs):
        position = cohorts.index(cohort) + 1 if cohort in cohorts else 0
        label = _stage_label(stage, cohort, position, len(cohorts))
        progress.begin(label)
        kwargs.pop("session_id", None)
        try:
            result = observe_stage(
                runner, stage, cohort, operation, session_id=session_id, **kwargs
            )
        except BaseException as exc:
            progress.end(label, error=exc)
            raise
        progress.end(label, result=result if isinstance(result, dict) else None)
        return result

    def source_progress(event: str, summary: dict) -> None:
        papers = summary.get("papers") or []
        if not papers:
            return
        index = len(papers)
        paper = papers[-1]
        arxiv_id = paper.get("arxiv_id", "không rõ ID")
        progress.set_detail(f"đã xử lý {index}/{len(cohort_pairs(active_cohort))} ID; ID hiện tại {arxiv_id}")
        if event == "paper_finished" and (index % 10 == 0 or index == len(cohort_pairs(active_cohort))):
            progress.note(f"Lô {active_cohort}: đã xử lý {index}/{len(cohort_pairs(active_cohort))} ID trong lượt này.")

    def s2_progress(_event: str, state: dict) -> None:
        index = state.get("index", 0)
        total = state.get("total", 0)
        arxiv_id = state.get("arxiv_id", "không rõ ID")
        progress.set_detail(f"đang xét ID {index}/{total}: {arxiv_id}")
        if index and (index == 1 or index % 10 == 0 or index == total):
            progress.note(f"Lô {state.get('cohort')}: S2 đang xét ID {index}/{total}.")

    annotate_session_envelope(
        envelope_path,
        mssv=mssv,
        commit=commit,
        run_scope=scope,
        cohorts=cohorts,
        expected_id_count=sum(len(cohort_pairs(cohort)) for cohort in cohorts),
    )
    try:
        progress.start(mssv, cohorts, scope, budget_seconds)
        try:
            snapshot_token = kaggle_token.strip() if kaggle_token and kaggle_token.strip() else None
            snapshot_result = observe_with_progress(
                "snapshot_prepare",
                0,
                lambda: prepare_snapshot(
                    drive_root.parent / "shared" / "kaggle_snapshot",
                    token=snapshot_token,
                ),
                session_id=session_id,
                configuration={"snapshot_scope": "shared_cache", "accelerator": "CPU"},
            )
            snapshot_path = snapshot_result.get("snapshot_path")
            kaggle_evidence = snapshot_result
            kaggle_evidence = {
                key: value for key, value in kaggle_evidence.items()
                if key not in {"snapshot_path", "token"}
            }
            _atomic_json(
                runner.reports_dir / f"kaggle_preparation_{session_id}.json",
                {"session_id": session_id, **kaggle_evidence},
            )
            if kaggle_evidence.get("status") == "ready":
                pairs = [pair for cohort in cohorts for pair in cohort_pairs(cohort)]
                index_started = time.perf_counter()
                index_observation = observe_with_progress(
                    "kaggle_index",
                    0,
                    lambda: reuse_or_build_index(
                        snapshot_path,
                        pairs,
                        drive_root / "kaggle_indices",
                        kaggle_evidence["snapshot_sha256"],
                    ),
                    session_id=session_id,
                    configuration={
                        "run_scope": scope,
                        "mssv": mssv,
                        "snapshot_sha256": kaggle_evidence["snapshot_sha256"],
                    },
                )
                index_path = index_observation["index_path"]
                index_data = index_observation["index"]
                index_evidence = {
                    "status": "ready",
                    "candidate_count": len(index_data["candidates"]),
                    "malformed_lines": index_data["diagnostics"].get("malformed_lines"),
                    "reused": index_observation["reused"],
                    "index_elapsed_seconds": time.perf_counter() - index_started,
                    "snapshot_checksum_seconds": None if index_observation["reused"] else index_data.get("checksum_seconds"),
                    "snapshot_scan_seconds": None if index_observation["reused"] else index_data.get("scan_seconds"),
                    "snapshot_sha256": kaggle_evidence.get("snapshot_sha256"),
                }
            else:
                kaggle_evidence.setdefault("reason", "snapshot_unavailable")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            kaggle_evidence = {
                **kaggle_evidence,
                "status": "fallback",
                "reason": "candidate_index_failed" if kaggle_evidence.get("status") == "ready" else "snapshot_prepare_failed",
                "error_type": type(exc).__name__,
            }
            index_path = None

        if prior_index is not None:
            os.environ.pop("M1_KAGGLE_CANDIDATE_INDEX", None)
        if index_path:
            os.environ["M1_KAGGLE_CANDIDATE_INDEX"] = str(index_path)
        else:
            os.environ.pop("M1_KAGGLE_CANDIDATE_INDEX", None)

        s2_key = semantic_scholar_api_key.strip() if semantic_scholar_api_key and semantic_scholar_api_key.strip() else None
        if s2_key:
            os.environ["SEMANTIC_SCHOLAR_API_KEY"] = s2_key
            os.environ["S2_API_KEY"] = s2_key
        else:
            os.environ.pop("SEMANTIC_SCHOLAR_API_KEY", None)
            os.environ.pop("S2_API_KEY", None)
        s2_pacing = 1.0 if s2_key else 3.0
        s2_config = {
            "timeout": 30.0,
            **S2_RETRY_POLICY,
            "min_interval": s2_pacing,
            "reference_limit": 100,
        }

        campaign_path = runner.reports_dir / f"campaign_state_{mssv}_{member_fingerprint[:16]}.json"
        campaign = load_campaign_state(campaign_path, mssv=mssv, cohorts=assigned_cohorts)
        campaign.setdefault("campaign_id", uuid.uuid4().hex)
        campaign.setdefault("session_ids", [])
        if session_id not in campaign["session_ids"]:
            campaign["session_ids"].append(session_id)
        if "starting_archive_sha256" not in campaign:
            initial = {}
            for cohort in assigned_cohorts:
                archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
                initial[str(cohort)] = runner._compute_sha256(archive) if archive.is_file() else "empty"
            local_existing = sorted(
                arxiv_id for member, arxiv_id in
                (pair for cohort in assigned_cohorts for pair in cohort_pairs(cohort))
                if member == mssv and (
                    (runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).exists()
                    or (runner.corpus_dir / arxiv_id_to_folder_key(arxiv_id)).is_symlink()
                )
            )
            campaign["starting_archive_sha256"] = initial
            campaign["starting_local_ids"] = local_existing
            campaign["starting_state_fingerprint"] = hashlib.sha256(
                json.dumps(
                    {"archives": initial, "local_ids": local_existing},
                    sort_keys=True, separators=(",", ":"),
                ).encode()
            ).hexdigest()
            campaign["starting_state_empty"] = (
                all(value == "empty" for value in initial.values()) and not local_existing
            )
        _atomic_json(campaign_path, campaign)
        annotate_session_envelope(
            envelope_path,
            campaign_id=campaign["campaign_id"],
            kaggle_status=kaggle_evidence.get("status"),
            snapshot_sha256=kaggle_evidence.get("snapshot_sha256"),
            s2_authenticated=bool(s2_key),
        )

        if any(runner.logs_dir.iterdir()):
            backup_path = runner.drive_root / "evidence_backups" / session_id / "logs"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            observe_with_progress(
                "log_backup",
                0,
                lambda: shutil.copytree(runner.logs_dir, backup_path),
                session_id=session_id,
                configuration={"destination": str(backup_path)},
            )

        cohort_state_path = runner.reports_dir / f"cohort_state_{session_id}.json"
        session_cohort_states = {
            str(cohort): dict(campaign["cohorts"].get(str(cohort), {"status": "not_started"}))
            for cohort in cohorts
        }
        _atomic_json(cohort_state_path, {"session_id": session_id, "cohorts": session_cohort_states})
        for cohort in cohorts:
            archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
            sidecars = (
                archive,
                archive.with_name(archive.name + ".sha256"),
                archive.with_name(archive.name.replace(".tar.gz", ".manifest.json")),
            )
            try:
                free_bytes = shutil.disk_usage(runner.local_root).free
                if free_bytes < MIN_FREE_BYTES:
                    raise RuntimeError(
                        f"Dung lượng còn trống trước lô {cohort}: {free_bytes} byte; "
                        f"yêu cầu tối thiểu {MIN_FREE_BYTES} byte."
                    )
                if any(path.exists() for path in sidecars):
                    backup = observe_with_progress(
                        "archive_backup",
                        cohort,
                        lambda: runner.backup_cohort_archive(cohort, require_sidecars=True),
                        session_id=session_id,
                    )
                    if backup:
                        backup_file = Path(backup["backup_archive"])
                        if not backup_file.is_file() or runner._compute_sha256(backup_file) != backup["archive_sha256"]:
                            raise RuntimeError(f"Bản sao lưu archive lô {cohort} không khớp SHA-256.")
                else:
                    observe_with_progress(
                        "archive_backup",
                        cohort,
                        lambda: {"status": "empty"},
                        session_id=session_id,
                    )
                restored = observe_with_progress(
                    "restore",
                    cohort,
                    lambda: runner.restore_cohort(cohort, require_manifest=any(path.exists() for path in sidecars)),
                    session_id=session_id,
                )
                session_cohort_states[str(cohort)] = {"status": "source_running", "session_id": session_id}
                _atomic_json(cohort_state_path, {"session_id": session_id, "cohorts": session_cohort_states})
                save_campaign_state(
                    campaign_path,
                    campaign,
                    cohort=cohort,
                    update={"status": "source_running", "session_id": session_id,
                            "updated_at": datetime.now(timezone.utc).isoformat()},
                )
                active_cohort = cohort
                source_result = observe_with_progress(
                    "source",
                    cohort,
                    lambda: runner.run_cohort(
                        cohort,
                        resume=True,
                        revision_history=True,
                        s2_config=None,
                        should_stop=budget.should_stop,
                        on_progress=source_progress,
                    ),
                    configuration={
                        "run_scope": scope,
                        "resume": True,
                        "revision_history": True,
                        "s2_enabled": False,
                        "mssv": mssv,
                        "session_budget_seconds": budget_seconds,
                        "discovery_arm": "kaggle" if index_path else "arxiv_only",
                    },
                )
                source_summary = source_result.get("fast_runner", {}) if isinstance(source_result, dict) else {}
                source_stopped = bool(source_summary.get("stopped"))
                source_stop_reason = _redact(source_summary.get("stop_reason"), safe_secrets)
                source_count = runner.count_source_completed_papers(cohort)
                revision_counts = revision_history_counts(runner, cohort)
                discovery_freshness = {"fresh": 0, "requires_verification": 0}
                from src.pipeline import cached_discovery_is_fresh
                for member_id, arxiv_id in cohort_pairs(cohort):
                    try:
                        metadata = runner.store.read_metadata(member_id, arxiv_id)
                        key = "fresh" if cached_discovery_is_fresh(metadata) else "requires_verification"
                    except Exception:
                        key = "requires_verification"
                    discovery_freshness[key] += 1
                cohort_results[str(cohort)] = {
                    "restored_papers": restored,
                    "source_complete_papers": source_count,
                    "source_stopped": source_stopped,
                    "source_stop_reason": source_stop_reason,
                    "revision_history": revision_counts,
                    "discovery_freshness": discovery_freshness,
                }

                pre_s2 = runner.count_m1_completed_papers(cohort)
                s2_skip_reason = None
                integrity_stop = pre_s2.get("integrity_failures", 0) > 0
                if integrity_stop:
                    s2_skip_reason = "integrity_failure"
                if pre_s2.get("is_cohort_complete"):
                    s2_skip_reason = "already_m1_complete"
                if s2_skip_reason:
                    s2_result = {"status": "skipped", "reason": s2_skip_reason, "stopped": False}
                    observe_with_progress(
                        "s2_restore",
                        cohort,
                        lambda: {"status": "returned"},
                        session_id=session_id,
                    )
                    observe_with_progress(
                        "s2",
                        cohort,
                        lambda: s2_result,
                        session_id=session_id,
                        configuration={"run_scope": scope, "skipped": True, "reason": s2_skip_reason},
                    )
                else:
                    save_campaign_state(
                        campaign_path,
                        campaign,
                        cohort=cohort,
                        update={"status": "s2_running", "session_id": session_id,
                                "updated_at": datetime.now(timezone.utc).isoformat()},
                    )
                    observe_with_progress(
                        "s2_restore",
                        cohort,
                        lambda: runner.restore_cohort(cohort, require_manifest=True),
                        session_id=session_id,
                    )
                    s2_result = observe_with_progress(
                        "s2",
                        cohort,
                        lambda: runner.enrich_cohort_s2(
                            cohort,
                            s2_config=s2_config,
                            should_stop=budget.should_stop,
                            on_progress=s2_progress,
                        ),
                        configuration={
                            "run_scope": scope,
                            "authenticated": bool(s2_key),
                            "min_interval": s2_pacing,
                            **S2_RETRY_POLICY,
                            "session_budget_seconds": budget_seconds,
                        },
                    )
                final_state = runner.count_m1_completed_papers(cohort)
                if s2_result.get("status") == "skipped":
                    s2_status = "skipped"
                elif s2_result.get("stopped"):
                    s2_status = "stopped"
                elif final_state.get("is_cohort_complete"):
                    s2_status = "complete"
                else:
                    s2_status = "partial"
                cohort_results[str(cohort)].update(
                    s2_status=s2_status,
                    s2_attempted_ids=s2_result.get("attempted_papers", 0),
                    s2_metadata_complete=s2_result.get("metadata_complete", 0),
                    s2_references_complete=s2_result.get("references_complete", 0),
                    s2_stop_reason=_redact(s2_result.get("stop_reason"), safe_secrets),
                    m1_state=final_state,
                )
                status = "m1_complete" if final_state.get("is_cohort_complete") else (
                    "stopped" if source_stopped or s2_result.get("stopped") else "m1_incomplete"
                )
                state_update = {
                    "status": status,
                    "session_id": session_id,
                    "source_complete_papers": source_count,
                    "source_stop_reason": source_stop_reason,
                    "s2_stop_reason": cohort_results[str(cohort)]["s2_stop_reason"],
                    "m1_state": final_state,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if integrity_stop:
                    state_update["stop_reason"] = {"type": "integrity_failure", "cohort": cohort}
                save_campaign_state(campaign_path, campaign, cohort=cohort, update=state_update)
                session_cohort_states[str(cohort)] = state_update
                _atomic_json(cohort_state_path, {"session_id": session_id, "cohorts": session_cohort_states})
                s2_status_text = {
                    "complete": "hoàn tất",
                    "partial": "chưa đủ dữ liệu",
                    "stopped": "đã dừng",
                    "skipped": "đã bỏ qua",
                }.get(s2_status, s2_status)
                progress.note(
                    f"Lô {cohort}: giai đoạn mã nguồn có trạng thái cuối {source_count}/100 ID; "
                    f"S2 {s2_status_text}; M1 đủ chuẩn {final_state.get('found_m1_complete', 0)} bài, "
                    f"xác nhận không tìm thấy {final_state.get('confirmed_not_found', 0)} ID, "
                    f"chưa xử lý {final_state.get('unprocessed', 0)} ID, "
                    f"lỗi toàn vẹn {final_state.get('integrity_failures', 0)}."
                )

                if integrity_stop:
                    stop_reason = {"type": "integrity_failure", "cohort": cohort}
                    break
                if s2_result.get("stopped"):
                    stop_reason = cohort_results[str(cohort)]["s2_stop_reason"]
                    break
                if source_stopped:
                    stop_reason = source_stop_reason
                    break
                if budget.should_stop():
                    stop_reason = {"type": "session_budget", "stage": "between_cohorts"}
                    break
            except Exception as exc:
                stop_reason = {"type": "workflow_stage_error", "cohort": cohort,
                               "error_type": type(exc).__name__,
                               "message": _redact(str(exc), safe_secrets)}
                cohort_results.setdefault(str(cohort), {})["workflow_error"] = stop_reason
                save_campaign_state(
                    campaign_path,
                    campaign,
                    cohort=cohort,
                    update={"status": "stopped", "session_id": session_id,
                            "stop_reason": stop_reason,
                            "updated_at": datetime.now(timezone.utc).isoformat()},
                )
                session_cohort_states[str(cohort)] = {"status": "stopped", **cohort_results[str(cohort)]}
                _atomic_json(cohort_state_path, {"session_id": session_id, "cohorts": session_cohort_states})
                break

        handoff_results: dict[int, dict] = {}
        for cohort in cohorts:
            try:
                handoff = observe_with_progress(
                    "handoff_audit",
                    cohort,
                    lambda: verify_cohort_handoff(runner, cohort),
                    session_id=session_id,
                )
                handoff_results[cohort] = handoff
                if handoff.get("archive_state") not in {"m1_handoff_verified", "handoff_verified"}:
                    handoff_issues.append(f"Lô {cohort}: trạng thái archive {handoff.get('archive_state')}.")
            except Exception as exc:
                handoff_issues.append(f"Lô {cohort}: {type(exc).__name__}: {_redact(str(exc), safe_secrets)}")

        bbl_audit_holder: dict[str, dict] = {}

        def run_data_audit():
            bbl_audit_holder["result"] = audit_bbl_sources(
                runner.store,
                all_pairs,
                output_dir=runner.reports_dir / "derived_bibliography" / session_id,
                report_path=runner.reports_dir / f"bbl_handling_{session_id}.json",
                session_id=session_id,
            )
            return _compute_statistics(runner, all_pairs, commit, scope)

        audit_stats = observe_with_progress(
            "audit",
            0,
            run_data_audit,
            session_id=session_id,
            configuration={"run_scope": scope, "cohorts": cohorts, "mssv": mssv},
        )
        bbl_audit = bbl_audit_holder.get("result")
        reconciled = bool(audit_stats.get("is_reconciled"))
        integrity_failed = (
            audit_stats.get("integrity_failed_papers", 0) > 0
            or (audit_stats.get("reconciliation") or {}).get("has_integrity_failure", False)
        )
        data_complete = (
            audit_stats.get("unprocessed_papers", 0) == 0
            and audit_stats.get("discovery_failed", 0) == 0
            and audit_stats.get("m1_complete_papers", 0) + audit_stats.get("confirmed_not_found", 0) == len(all_pairs)
        )
        export_eligible = reconciled and not integrity_failed and data_complete and not handoff_issues
        export_dir = drive_root / "final_output" / session_id
        if export_eligible:
            try:
                export_result = observe_with_progress(
                    "dataset_export",
                    0,
                    lambda: _export_datasets(runner, all_pairs, export_dir),
                    session_id=session_id,
                    configuration={"run_scope": scope, "mssv": mssv, "session_id": session_id},
                )
            except Exception as exc:
                export_error = {"type": type(exc).__name__, "message": _redact(str(exc), safe_secrets)}
        else:
            observe_with_progress(
                "dataset_export",
                0,
                lambda: {"status": "skipped", "reason": "m1_audit_incomplete"},
                session_id=session_id,
                configuration={"run_scope": scope, "skipped": True},
            )

        data_signature_expected = data_complete and reconciled and not integrity_failed
        data_signature = None
        if data_signature_expected:
            data_signature = observe_with_progress(
                "audit_signature",
                0,
                lambda: fingerprint_complete_corpus(runner.store, all_pairs),
                session_id=session_id,
                configuration={"run_scope": scope, "mssv": mssv},
            )
        archive_paths = [runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz" for cohort in cohorts]
        archive_sizes = [_regular_file_size(path) for path in archive_paths]
        archive_size_bytes = sum(archive_sizes) if all(
            size is not None for size in archive_sizes
        ) else None
        size_measurement = observe_with_progress(
            "size_accounting",
            0,
            lambda: {
                "status": "measured",
                "snapshot_size_bytes": _regular_file_size(Path(snapshot_path)) if snapshot_path else None,
                "corpus_size_bytes": _tree_size_bytes(runner.corpus_dir),
                "archive_size_bytes": archive_size_bytes,
                "export_size_bytes": _tree_size_bytes(export_dir) if export_result is not None else None,
            },
            session_id=session_id,
            configuration={"run_scope": scope, "mssv": mssv},
        )

        envelope = _close_session(
            envelope_path,
            started_monotonic,
            "finished",
            {"stop_reason": _redact(stop_reason, safe_secrets),
             "m1_complete": data_complete and reconciled and not integrity_failed and not handoff_issues},
        )
        outcome = "finished"

        session_ids = set(campaign.get("session_ids", []))
        observations = _load_campaign_observations(runner.reports_dir, session_ids)
        required = [("snapshot_prepare", 0), ("audit", 0)]
        if index_path:
            required.append(("kaggle_index", 0))
        required.append(("size_accounting", 0))
        if data_signature_expected:
            required.append(("audit_signature", 0))
        if export_eligible:
            required.append(("dataset_export", 0))
        for cohort in cohorts:
            required.extend((stage, cohort) for stage in (
                "archive_backup", "restore", "source", "s2_restore", "s2", "handoff_audit"
            ))
        envelopes = []
        for prior_session in campaign.get("session_ids", []):
            path = runner.reports_dir / "session_envelopes" / f"{prior_session}.json"
            try:
                envelopes.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                envelopes.append({})
        benchmark_summary = summarize_workflow(observations, required, session_envelopes=envelopes)
        benchmark_summary.update(
            session_id=session_id,
            session_ids=campaign.get("session_ids", []),
            campaign_id=campaign["campaign_id"],
            mssv=mssv,
            commit=commit,
            run_scope=scope,
            cohorts=cohorts,
            expected_id_count=len(all_pairs),
            selection_fingerprint=selection_fingerprint,
            member_selection_fingerprint=member_fingerprint,
            starting_state_fingerprint=campaign.get("starting_state_fingerprint"),
            execution_mode="from_scratch" if campaign.get("starting_state_empty") else "resume",
            starting_state_empty=campaign.get("starting_state_empty", False),
            data_signature=data_signature,
            environment_fingerprint=hashlib.sha256(json.dumps(
                {"python": platform.python_version(), "platform": platform.platform(),
                 "cpu_count": os.cpu_count(), "accelerator": "CPU", "commit": commit},
                sort_keys=True,
            ).encode()).hexdigest(),
            measurement_protocol="m1-colab-stage-v3; 10h soft budget; retries and waits included",
            discovery_arm="kaggle" if index_path else "arxiv_only",
            snapshot_source=kaggle_evidence.get("source"),
            snapshot_sha256=kaggle_evidence.get("snapshot_sha256"),
            snapshot_size_bytes=size_measurement.get("snapshot_size_bytes"),
            corpus_size_bytes=size_measurement.get("corpus_size_bytes"),
            corpus_size_scope="tệp thường trong corpus local sau kiểm toán; bỏ qua symbolic link",
            archive_size_bytes=size_measurement.get("archive_size_bytes"),
            export_size_bytes=size_measurement.get("export_size_bytes"),
            m1_data_complete=data_complete,
            audit_reconciled=reconciled,
            integrity_failed=integrity_failed,
            handoff_issues=handoff_issues,
            end_to_end_complete=bool(
                benchmark_summary.get("status") == "measured_intervals"
                and scope == "member_full_assignment"
                and len(all_pairs) == len(assigned_cohorts) * 100
                and data_complete and reconciled and not integrity_failed
                and not handoff_issues and export_error is None and export_result is not None
                and campaign.get("starting_state_empty") is True
                and data_signature is not None
            ),
        )
        benchmark_path = runner.reports_dir / f"benchmark_observed_{campaign['campaign_id']}_{session_id}.json"
        _atomic_json(benchmark_path, benchmark_summary)

        report_time = datetime.now(timezone.utc).isoformat()
        handoff_verified = export_eligible and export_error is None and export_result is not None
        archive_info = []
        histories = []
        for cohort in cohorts:
            archive = runner.archives_dir / f"cohort_{cohort:04d}_corpus.tar.gz"
            digest = handoff_results.get(cohort, {}).get("archive_sha256", "chưa xác minh")
            archive_info.append(f"- Lô {cohort}: `{archive}`; SHA-256: `{digest}`")
            histories.append(f"- Lô {cohort}: `{revision_history_counts(runner, cohort)}`")
        from src.reporting import format_markdown_summary
        report_text = (
            f"Phiên `{session_id}`; thời điểm `{report_time}`; commit `{commit or 'unknown'}`; "
            f"MSSV `{mssv}`; lô `{cohorts}`; phạm vi `{scope}`.\n\n"
            f"Kaggle `{kaggle_evidence.get('status')}`; nguồn `{kaggle_evidence.get('source', 'unknown')}`; "
            f"SHA-256 `{kaggle_evidence.get('snapshot_sha256', 'unknown')}`; "
            f"chỉ mục `{index_evidence or 'không dùng được; arXiv-only'}`.\n\n"
            f"Ngân sách `{budget_seconds}` giây; thời gian phiên `{budget.elapsed_seconds:.1f}` giây; "
            f"lý do dừng `{_redact(stop_reason, safe_secrets) or 'không có'}`.\n\n"
            f"Trạng thái bàn giao: `{'đạt' if handoff_verified else 'chưa đạt'}`. "
            f"Thư mục xuất: `{export_dir if export_result else 'chưa xuất vì kiểm toán chưa đạt'}`.\n\n"
            "SHA-256 archive xác định tệp đã lưu; báo cáo kiểm toán được tính từ corpus tại thời điểm chạy.\n\n"
            + "\n".join(archive_info)
            + "\n\nLịch sử revision theo lô:\n"
            + "\n".join(histories)
            + "\n\nXử lý tệp `.bbl`:\n"
            + f"- Báo cáo: `{bbl_audit.get('report_path') if bbl_audit else 'chưa tạo'}`\n"
            + f"- Kết quả: `{bbl_audit.get('counts') if bbl_audit else 'chưa có'}`\n"
            + "- Chỉ chuyển đổi cục bộ khi tệp có các trường BibLaTeX cấu trúc hỗ trợ; trường hợp không thể khôi phục chính xác được ghi lý do và giữ nguyên `.bbl`. Tệp `.bib` dẫn xuất nằm ngoài corpus.\n"
            + "\n\n"
            + format_markdown_summary(audit_stats)
        )
        report_path = runner.reports_dir / f"m1_audit_summary_{mssv}_{scope}_{session_id}.md"
        report_path.write_text(report_text, encoding="utf-8")
        outcome_detail = {"stop_reason": _redact(stop_reason, safe_secrets),
                          "m1_complete": handoff_verified}
        return {
            "session_id": session_id,
            "campaign_id": campaign["campaign_id"],
            "commit": commit,
            "mssv": mssv,
            "assigned_cohorts": [assigned_cohorts[0], assigned_cohorts[-1]],
            "cohorts": cohorts,
            "run_scope": scope,
            "execution_mode": "from_scratch" if campaign.get("starting_state_empty") else "resume",
            "kaggle": {**kaggle_evidence, "snapshot_path": None},
            "kaggle_index": index_evidence,
            "bbl_handling": bbl_audit,
            "cohort_results": cohort_results,
            "stop_reason": _redact(stop_reason, safe_secrets),
            "audit": {
                "is_reconciled": reconciled,
                "integrity_failed": integrity_failed,
                "data_complete": data_complete,
                "handoff_issues": handoff_issues,
                "m1_complete_papers": audit_stats.get("m1_complete_papers"),
                "confirmed_not_found": audit_stats.get("confirmed_not_found"),
                "assigned_candidates": audit_stats.get("assigned_candidates"),
            },
            "handoff_status": "verified" if handoff_verified else "blocked",
            "export": {"path": str(export_dir) if export_result else None, "error": export_error},
            "report_path": str(report_path),
            "benchmark_path": str(benchmark_path),
            "benchmark_status": benchmark_summary.get("status"),
            "benchmark_missing_stages": benchmark_summary.get("missing_stages"),
            "elapsed_seconds": budget.elapsed_seconds,
            "campaign_state_path": str(campaign_path),
            "cohort_state_path": str(cohort_state_path),
        }
    except BaseException as exc:
        outcome = "failed"
        outcome_detail = {"error_type": type(exc).__name__,
                          "message": _redact(str(exc), safe_secrets)}
        raise
    finally:
        progress.close()
        if envelope is None:
            try:
                _close_session(envelope_path, started_monotonic, outcome, outcome_detail)
            except ValueError:
                # Phiên có thể đã được đóng trước khi lỗi xảy ra ở khâu báo cáo.
                pass
        if prior_index is None:
            os.environ.pop("M1_KAGGLE_CANDIDATE_INDEX", None)
        else:
            os.environ["M1_KAGGLE_CANDIDATE_INDEX"] = prior_index
        for name, value in prior_s2.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _compute_statistics(runner: FastProductionRunner, pairs: list[tuple[str, str]],
                        commit: str | None, scope: str) -> dict:
    from src.reporting import compute_m1_statistics

    return compute_m1_statistics(
        runner.store,
        pairs,
        repository_commit=commit,
        report_scope=scope,
    )


def _export_datasets(runner: FastProductionRunner, pairs: list[tuple[str, str]],
                     output_dir: Path) -> dict:
    from src.export import export_official_datasets

    return export_official_datasets(
        store=runner.store,
        pairs=pairs,
        output_dir=output_dir,
        require_m1_complete=True,
    )

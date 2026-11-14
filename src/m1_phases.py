"""Backward-compatible entry points for the independent M1 phases.

Implementation lives in m1_phase_modules. The historical names remain here for
callers and test patches; wrappers resolve runner and archive callbacks at call
time. The sequential compatibility workflow intentionally stays in this module.
"""
from __future__ import annotations

# Retain historical import bindings as well as helpers for existing callers and
# monkeypatches. Phase wrappers pass the patched dependencies at invocation time.
import hashlib
import json
import math
import os
import random
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.fast_production import (
    FastProductionRunner,
    cohort_pairs,
    member_cohort_range,
    verify_cohort_handoff,
)
from src.m1_phase_modules.arxiv_phase import (
    _auto_resume_429_policy,
    _durable_discovery_429_checkpoint,
    _kaggle_source_provenance_issue,
    _mark_archive_arxiv_only,
    _provider_cooldown_from_logs,
    _source_state_manifest,
)
from src.m1_phase_modules.arxiv_phase import (
    run_m1_arxiv_member as _run_m1_arxiv_member_impl,
)
from src.m1_phase_modules.audit_phase import (
    _merge_s2,
    _preflight_merge,
    _same_reference_snapshot,
    _validate_source_paper,
)
from src.m1_phase_modules.audit_phase import (
    run_m1_audit_member as _run_m1_audit_member_impl,
)
from src.m1_phase_modules.benchmark_summary import (
    _load_phase_observations,
    _member_benchmark_summary,
)
from src.m1_phase_modules.checkpoint import (
    _ensure_directory,
    _ensure_starting_state,
    _member_writer_lock,
    _persist_selection,
    _phase_root,
    _read_json,
    _record_session,
    _session_start,
    _sha256_file,
    _validate_starting_state,
    _write_json,
)
from src.m1_phase_modules.common import (
    PHASE_SCHEMA_VERSION,
    M1PhaseError,
    _required_m1_secret,
    build_member_selection,
)
from src.m1_phase_modules.s2_evidence import (
    _cohort_s2_paths,
    _evidence_file,
    _load_s2_manifest,
    _new_s2_manifest,
    _read_s2_paper,
    _save_cached_evidence,
    _save_s2_metadata,
    _save_s2_references,
    _validate_s2_cohort,
)
from src.m1_phase_modules.s2_phase import (
    S2_DEFAULTS,
    S2_METADATA_BATCH_SIZE,
    _api_key_environment,
    _auto_resume_s2_429_policy,
    _durable_s2_429_checkpoint,
    _fetch_s2_metadata_batch,
    _process_s2_cohort,
    _reuse_corpus_s2,
    _s2_provider_cooldown_seconds,
)
from src.m1_phase_modules.s2_phase import run_m1_s2_member as _run_m1_s2_member_impl
from src.session_evidence import (
    SessionBudget,
    annotate_session_envelope,
    finish_session_envelope,
    observe_stage,
    start_session_envelope,
)
from src.storage import (
    IntegrityError,
    StorageError,
    _atomic_json,
    arxiv_id_to_folder_key,
)


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
) -> dict:
    """Xác minh ID được giao và lưu mã nguồn của mọi phiên bản, không chạy S2.

    Tự tiếp tục có giới hạn chỉ áp dụng cho HTTP 429 ở discovery khi checkpoint
    đã được xác minh. Snapshot và index Kaggle hợp lệ phải sẵn sàng trước đó.
    """
    return _run_m1_arxiv_member_impl(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        kaggle_token=kaggle_token,
        cohort_limit=cohort_limit,
        budget_seconds=budget_seconds,
        auto_resume_429=auto_resume_429,
        auto_resume_429_max_per_cohort=auto_resume_429_max_per_cohort,
        auto_resume_429_max_per_session=auto_resume_429_max_per_session,
        auto_resume_429_cooldown_min_seconds=auto_resume_429_cooldown_min_seconds,
        auto_resume_429_cooldown_max_seconds=auto_resume_429_cooldown_max_seconds,
        runner_factory=FastProductionRunner,
        mark_archive_arxiv_only=_mark_archive_arxiv_only,
    )


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
) -> dict:
    """Lấy metadata/references S2; có thể tự tiếp tục hữu hạn sau HTTP 429."""
    return _run_m1_s2_member_impl(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        semantic_scholar_api_key=semantic_scholar_api_key,
        cohort_limit=cohort_limit,
        budget_seconds=budget_seconds,
        auto_resume_429=auto_resume_429,
        auto_resume_429_max_per_cohort=auto_resume_429_max_per_cohort,
        auto_resume_429_max_per_session=auto_resume_429_max_per_session,
        auto_resume_429_cooldown_min_seconds=auto_resume_429_cooldown_min_seconds,
        auto_resume_429_cooldown_max_seconds=auto_resume_429_cooldown_max_seconds,
        reference_strategy=reference_strategy,
        runner_factory=FastProductionRunner,
    )


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
    return _run_m1_audit_member_impl(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        cohort_limit=cohort_limit,
        output_dir=output_dir,
        budget_seconds=budget_seconds,
        runner_factory=FastProductionRunner,
    )


def run_m1_member(
    mssv: str,
    *,
    drive_root: str | Path,
    local_root: str | Path,
    kaggle_token: str,
    semantic_scholar_api_key: str,
    cohort_limit: int | None = None,
    budget_seconds: float = 10 * 60 * 60,
) -> dict:
    """Chạy cùng quy trình M1 mà notebook dùng trên một phạm vi ID."""
    kaggle_token = _required_m1_secret(kaggle_token, "KAGGLE_API_TOKEN")
    semantic_scholar_api_key = _required_m1_secret(
        semantic_scholar_api_key, "SEMANTIC_SCHOLAR_API_KEY",
    )
    started = time.monotonic()

    def remaining_budget() -> float:
        return max(0.001, budget_seconds - (time.monotonic() - started))

    arxiv_result = run_m1_arxiv_member(
        mssv,
        drive_root=drive_root,
        local_root=local_root,
        kaggle_token=kaggle_token,
        cohort_limit=cohort_limit,
        budget_seconds=remaining_budget(),
        auto_resume_429=True,
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
        auto_resume_429=True,
        reference_strategy="batch_m1",
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

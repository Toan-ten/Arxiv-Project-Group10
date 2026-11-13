"""Shared M1 selection, schema and credential validation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from src.fast_production import cohort_pairs, member_cohort_range

PHASE_SCHEMA_VERSION = 1


class M1PhaseError(RuntimeError):
    """A phase checkpoint is absent, incompatible, or fails verification."""


def _required_m1_secret(value: Any, name: str) -> str:
    """Kiểm tra khóa M1 mà không đưa giá trị khóa vào thông báo lỗi."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"M1 yêu cầu Colab Secret `{name}` có giá trị. "
            "Hãy thêm Secret và cấp quyền truy cập cho notebook."
        )
    secret = value.strip()
    if "\r" in secret or "\n" in secret or "\x00" in secret:
        raise ValueError(f"Colab Secret `{name}` có định dạng không hợp lệ.")
    if name == "SEMANTIC_SCHOLAR_API_KEY" and (
        not secret.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in secret)
    ):
        raise ValueError(f"Colab Secret `{name}` có định dạng không hợp lệ.")
    return secret


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

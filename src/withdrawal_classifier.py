"""Evidence-Based Classification for arXiv Paper & Version Outcomes.

Implements the official evidence hierarchy from crawling.md and Phase 4:
1. arXiv API metadata and version history.
2. Official arXiv abstract / version comments.
3. Source endpoint response as supporting evidence only.

Distinguishes:
- success: valid TeX/bib source files present.
- no_tex_source: HTTP 200 with PDF-only or non-convertible content.
- withdrawn_version: confirmed withdrawn by author/arXiv with documented evidence.
- version_not_found: confirmed by metadata that version does not exist.
- http_error_404_unverified: HTTP 404 without confirmed metadata justification.
- rate_limited: HTTP 429 rate limiting.
- transient_error: HTTP 5xx or connection timeout.
- permanent_error: unrecoverable client or storage error.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.withdrawal_evidence import confirmed_withdrawal


WITHDRAWAL_PATTERN = re.compile(
    r"(?i)\b(?:withdrawn|withdraw|withdrew|withdrawing|retracted|retract|withdrawal)\b"
)

TERMINAL_SOURCE_STATUSES = {"success", "no_tex_source", "withdrawn_version", "version_not_found"}
RETRYABLE_SOURCE_STATUSES = {"rate_limited", "transient_error"}


def is_terminal_source_status(status: str) -> bool:
    """Return True if the version source status represents a verified terminal state."""
    return status in TERMINAL_SOURCE_STATUSES


def is_retryable_source_status(status: str) -> bool:
    """Return True if the version source status is transient and eligible for retry."""
    return status in RETRYABLE_SOURCE_STATUSES


def classify_version_outcome(
    arxiv_id: str,
    version: str,
    *,
    http_status: Optional[int] = None,
    content_type: Optional[str] = None,
    discovery_metadata: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    has_tex_files: bool = False,
    file_list: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Classify the terminal or intermediate outcome of a version source download.

    Returns a dictionary containing:
        - classification: str (one of the canonical states)
        - is_terminal: bool
        - evidence_source: str
        - evidence_timestamp: str (ISO-8601 UTC)
        - original_http_status: Optional[int]
        - reason: str
        - details: dict
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    version_clean = version if version.startswith("v") else f"v{version}"

    # Rule 1: HTTP 200 checks
    if http_status == 200:
        c_type = (content_type or "").lower().split(";", 1)[0].strip()
        if c_type == "application/pdf" or not has_tex_files:
            return {
                "classification": "no_tex_source",
                "is_terminal": True,
                "evidence_source": "http_response",
                "evidence_timestamp": now_iso,
                "original_http_status": 200,
                "reason": "HTTP 200 returned PDF-only or non-convertible source archive without TeX",
                "details": {"content_type": content_type, "files": file_list or []},
            }
        return {
            "classification": "success",
            "is_terminal": True,
            "evidence_source": "http_response",
            "evidence_timestamp": now_iso,
            "original_http_status": 200,
            "reason": "HTTP 200 source archive successfully downloaded and extracted TeX files",
            "details": {"content_type": content_type, "files": file_list or []},
        }

    # Rule 3: HTTP 429 Rate limiting (Never terminal no-source)
    if http_status == 429:
        return {
            "classification": "rate_limited",
            "is_terminal": False,
            "evidence_source": "http_response",
            "evidence_timestamp": now_iso,
            "original_http_status": 429,
            "reason": "HTTP 429 rate limit received; requires polite backoff and retry budget",
            "details": {"error_message": error_message},
        }

    # Rule 4: HTTP 5xx / timeout / network blips
    if (http_status is not None and http_status >= 500) or (
        error_message
        and any(x in error_message.lower() for x in ["timeout", "connection", "reset", "503", "502", "504"])
    ):
        return {
            "classification": "transient_error",
            "is_terminal": False,
            "evidence_source": "http_response",
            "evidence_timestamp": now_iso,
            "original_http_status": http_status,
            "reason": f"Transient network/server error: {error_message or f'HTTP {http_status}'}",
            "details": {"error_message": error_message},
        }

    # Rule 2: HTTP 404 Evidence-based resolution
    if http_status == 404:
        meta = discovery_metadata or {}

        # Evidence Source 1: Check if metadata explicitly flags withdrawal
        is_withdrawn = bool(meta.get("is_withdrawn"))
        comments = str(meta.get("comments") or "")
        title = str(meta.get("title") or "")
        abstract = str(meta.get("abstract") or "")
        withdrawal_reason = str(meta.get("withdrawal_reason") or "")
        combined_text = f"{comments} {title} {abstract} {withdrawal_reason}"

        if confirmed_withdrawal(meta, arxiv_id, version_clean):
            reason_text = (
                withdrawal_reason.strip()
                or comments.strip()
                or "Withdrawn by author / arXiv"
            )
            return {
                "classification": "withdrawn_version",
                "is_terminal": True,
                "evidence_source": "arxiv_metadata",
                "evidence_timestamp": now_iso,
                "original_http_status": 404,
                "reason": f"Confirmed withdrawn version: {reason_text}",
                "details": {
                    "comments": comments,
                    "title": title,
                    "abstract": abstract,
                    "withdrawal_reason": withdrawal_reason,
                    "official_url": meta.get("official_url"),
                    "versioned_id": meta.get("versioned_id"),
                    "arxiv_id": meta.get("arxiv_id"),
                    "page_sha256": meta.get("page_sha256"),
                    "verification_method": meta.get("verification_method"),
                    "verified_at": meta.get("verified_at"),
                },
            }

        # Evidence Source 2: Check if version is known not to exist
        version_ids = meta.get("version_ids") or []
        latest_id = meta.get("latest_version_id")
        if version_ids:
            expected_v_ids = [f"{arxiv_id}{version_clean}"]
            if expected_v_ids[0] not in version_ids:
                return {
                    "classification": "version_not_found",
                    "is_terminal": True,
                    "evidence_source": "arxiv_metadata",
                    "evidence_timestamp": now_iso,
                    "original_http_status": 404,
                    "reason": f"Version {version_clean} does not exist in resolved arXiv metadata (latest: {latest_id})",
                    "details": {"version_ids": version_ids, "latest_version_id": latest_id},
                }

        # Evidence Source 3: Unverified 404 (insufficient metadata evidence to claim withdrawal)
        return {
            "classification": "http_error_404_unverified",
            "is_terminal": False,
            "evidence_source": "http_response",
            "evidence_timestamp": now_iso,
            "original_http_status": 404,
            "reason": "HTTP 404 received but arXiv metadata does not confirm withdrawal or nonexistence",
            "details": {"error_message": error_message},
        }

    # Default fallback for unclassified errors
    return {
        "classification": "permanent_error",
        "is_terminal": False,
        "evidence_source": "error_handler",
        "evidence_timestamp": now_iso,
        "original_http_status": http_status,
        "reason": f"Unrecoverable error: {error_message or f'HTTP {http_status}'}",
        "details": {"error_message": error_message},
    }

"""Strict, version-scoped proof for arXiv withdrawn source versions.

A keyword in a paper-wide title, abstract or comments is NOT sufficient.
Only explicit official evidence with an exact version identity is promotable.
No I/O or HTTP calls occur in this module.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import unquote, urlsplit
from typing import Any

_VERSION = re.compile(r"v[1-9][0-9]*\Z")
_EXPLICIT_WITHDRAWAL = re.compile(
    r"\b(?:withdrawn\s+by|has\s+been\s+withdrawn|was\s+withdrawn|"
    r"withdrawn\s+from|retracted\s+by|has\s+been\s+retracted|was\s+retracted)\b",
    re.IGNORECASE,
)


def confirmed_withdrawal(meta: Any, arxiv_id: str, version: str) -> bool:
    """Return True *only* for version-scoped official withdrawal evidence.

    Accepted record requires:
      - exact paper and versioned IDs;
      - URL https://arxiv.org/abs/<exact-versioned-id> (no query/fragment);
      - explicit withdrawal statement in reason/comment/abstract (flag alone is insufficient);
      - no contradictory version inventory when provided.

    An incomplete record must be reviewed and cannot become a terminal status.
    This is intentionally stricter than paper-wide arXiv/Kaggle flags.
    """
    if not isinstance(meta, dict) or not isinstance(arxiv_id, str) or not isinstance(version, str):
        return False
    if _VERSION.fullmatch(version) is None:
        return False
    target = f"{arxiv_id}{version}"
    if meta.get("arxiv_id") != arxiv_id or meta.get("versioned_id") != target:
        return False
    inventory = meta.get("version_ids")
    if inventory is not None and (not isinstance(inventory, list) or target not in inventory):
        return False

    official_url = meta.get("official_url")
    if not isinstance(official_url, str):
        return False
    try:
        parsed = urlsplit(official_url)
        port = parsed.port  # Phát sinh ValueError nếu số cổng không đúng định dạng.
    except ValueError:
        return False
    if (parsed.scheme != "https" or parsed.hostname != "arxiv.org"
            or parsed.username is not None or parsed.password is not None
            or port is not None or parsed.query or parsed.fragment
            or unquote(parsed.path) != f"/abs/{target}"):
        return False

    explicit_statement = " ".join(
        value for field in ("withdrawal_reason", "comments", "abstract")
        if isinstance((value := meta.get(field)), str)
    )
    if not _EXPLICIT_WITHDRAWAL.search(explicit_statement):
        return False
    if meta.get("verification_method") == "official_arxiv_version_page":
        digest = meta.get("page_sha256")
        verified_at = meta.get("verified_at")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        try:
            stamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False
        return stamp.tzinfo is not None
    return True

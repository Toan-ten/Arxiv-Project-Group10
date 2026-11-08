"""Kaggle arXiv metadata snapshot ingestion for offline pre-cached Discovery.

Allows pre-populating CorpusStorage manifests and metadata from the official
Cornell arXiv Kaggle dataset snapshot (arxiv-metadata-oai-snapshot.json),
eliminating redundant 3.0-second network discovery queries on arXiv API.
"""
from __future__ import annotations

import email.utils
import gzip
import io
import json
import os
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.storage import CorpusStorage, StorageError, _valid_date


def _parse_iso_date(date_str: Optional[str]) -> Optional[str]:
    """Parse an email or ISO date string into a YYYY-MM-DD string."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    # Check if already ISO YYYY-MM-DD
    if len(date_str) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", date_str):
        candidate = date_str[:10]
        if _valid_date(candidate):
            return candidate
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        candidate = dt.date().isoformat()
        if _valid_date(candidate):
            return candidate
    except Exception:
        pass
    return None


def _clean_authors(authors_val) -> List[str]:
    """Convert raw authors text or list into a list of clean author strings."""
    if isinstance(authors_val, list):
        cleaned = [str(a).strip() for a in authors_val if str(a).strip()]
        return cleaned if cleaned else ["Unknown"]
    if isinstance(authors_val, str):
        parts = [p.strip() for p in re.split(r",|\band\b", authors_val) if p.strip()]
        return parts if parts else ["Unknown"]
    return ["Unknown"]


def build_discovery_snapshot(mssv: str, arxiv_id: str, kaggle_record: dict) -> dict:
    """Build a valid CorpusStorage discovery snapshot from a Kaggle JSON record."""
    versions_raw = kaggle_record.get("versions", [])
    revised_dates: List[str] = []
    if isinstance(versions_raw, list) and versions_raw:
        num_versions = len(versions_raw)
        v1_created = versions_raw[0].get("created") if isinstance(versions_raw[0], dict) else None
        latest_created = versions_raw[-1].get("created") if isinstance(versions_raw[-1], dict) else None
        for v in versions_raw[1:]:
            if isinstance(v, dict) and v.get("created"):
                iso_d = _parse_iso_date(v["created"])
                if iso_d:
                    revised_dates.append(iso_d)
    else:
        num_versions = 1
        v1_created = kaggle_record.get("update_date")
        latest_created = None

    version_ids = [f"{arxiv_id}v{n}" for n in range(1, num_versions + 1)]
    published = _parse_iso_date(v1_created) or _parse_iso_date(kaggle_record.get("update_date"))
    updated = _parse_iso_date(latest_created) if num_versions > 1 else None

    title = str(kaggle_record.get("title", "")).strip().replace("\n", " ")
    if not title:
        title = f"arXiv:{arxiv_id}"

    authors = _clean_authors(kaggle_record.get("authors"))

    return {
        "mssv": str(mssv),
        "arxiv_id": str(arxiv_id),
        "status": "found",
        "version_status": "resolved",
        "version_ids": version_ids,
        "latest_version_id": version_ids[-1],
        "title": title,
        "authors": authors,
        "published": published if published and _valid_date(published) else None,
        "updated": updated if updated and _valid_date(updated) else None,
        "revised_dates": revised_dates,
        "missing_fields": [],
    }


def ingest_kaggle_snapshot(
    snapshot_path: str | Path,
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
) -> dict:
    """Stream arxiv-metadata-oai-snapshot.json and pre-populate CorpusStorage manifests.

    Returns summary metrics:
      - target_papers: total pairs requested
      - found_papers: papers matched in Kaggle snapshot and recorded
      - found_versions: total versions discovered
      - missing_papers: list of arxiv_ids not found in snapshot
    """
    snapshot_file = Path(snapshot_path)
    if not snapshot_file.exists():
        raise FileNotFoundError(f"Kaggle snapshot file not found: {snapshot_file}")

    from src.storage import arxiv_id_to_folder_key
    target_map: Dict[str, str] = {}
    already_cached = 0
    for mssv, arxiv_id in pairs:
        try:
            manifest_file = store.root / arxiv_id_to_folder_key(str(arxiv_id)) / "manifest.json"
            if manifest_file.exists():
                m = json.loads(manifest_file.read_text(encoding="utf-8"))
                if m.get("discovery_status") in ("found", "not_found"):
                    already_cached += 1
                    continue
        except Exception:
            pass
        target_map[str(arxiv_id)] = str(mssv)

    remaining_ids: Set[str] = set(target_map.keys())
    found_count = already_cached
    total_versions = 0
    if not remaining_ids:
        return {
            "target_papers": len(pairs),
            "found_papers": found_count,
            "found_versions": total_versions,
            "missing_papers": [],
        }

    open_fn = gzip.open if snapshot_file.suffix == ".gz" else open
    mode = "rt" if snapshot_file.suffix == ".gz" else "r"

    with open_fn(snapshot_file, mode, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not remaining_ids:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_id = record.get("id")
            if record_id in remaining_ids:
                mssv = target_map[record_id]
                snapshot = build_discovery_snapshot(mssv, record_id, record)
                store.record_discovery(snapshot)
                if snapshot.get("revised_dates") is not None:
                    meta = store.read_metadata(mssv, record_id)
                    meta["revised_dates"] = snapshot["revised_dates"]
                    for idx, rev_date in enumerate(snapshot["revised_dates"], start=1):
                        if idx < len(meta.get("versions", [])) and _valid_date(rev_date):
                            meta["versions"][idx]["date"] = rev_date
                            meta["versions"][idx]["date_status"] = "verified"
                    store.save_metadata(mssv, record_id, meta)
                remaining_ids.remove(record_id)
                found_count += 1
                total_versions += len(snapshot["version_ids"])

    return {
        "target_papers": len(target_map),
        "found_papers": found_count,
        "found_versions": total_versions,
        "missing_papers": sorted(remaining_ids),
    }

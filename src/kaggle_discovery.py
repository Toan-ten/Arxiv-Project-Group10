"""Kaggle arXiv metadata snapshot ingestion for offline pre-cached Discovery.

Allows pre-populating CorpusStorage manifests and metadata from the official
Cornell arXiv Kaggle dataset snapshot (arxiv-metadata-oai-snapshot.json),
eliminating redundant 3.0-second network discovery queries on arXiv API.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
    """Convert raw authors text or list into a list of clean author strings without fabricating placeholders."""
    if isinstance(authors_val, list):
        return [str(a).strip() for a in authors_val if str(a).strip()]
    if isinstance(authors_val, str):
        return [p.strip() for p in re.split(r",|\band\b", authors_val) if p.strip()]
    return []


def build_discovery_snapshot(mssv: str, arxiv_id: str, kaggle_record: dict) -> Optional[dict]:
    """Build a valid CorpusStorage discovery snapshot from a Kaggle JSON record.

    Returns None when version list is missing, empty, or not contiguous from v1,
    so the pipeline falls back to live arXiv API discovery instead of fabricating v1.
    """
    versions_raw = kaggle_record.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        return None
    if not all(isinstance(v, dict) for v in versions_raw):
        return None
    expected_names = [f"v{n}" for n in range(1, len(versions_raw) + 1)]
    actual_names = [str(v.get("version", "")).strip() for v in versions_raw]
    if actual_names != expected_names:
        return None

    num_versions = len(versions_raw)
    v1_created = versions_raw[0].get("created")
    latest_created = versions_raw[-1].get("created")
    revised_dates: Optional[List[str]] = []
    for v in versions_raw[1:]:
        iso_d = _parse_iso_date(v.get("created"))
        if not iso_d:
            revised_dates = None
            break
        revised_dates.append(iso_d)

    version_ids = [f"{arxiv_id}v{n}" for n in range(1, num_versions + 1)]
    published = _parse_iso_date(v1_created)
    updated = _parse_iso_date(latest_created) if num_versions > 1 else published

    raw_title = kaggle_record.get("title")
    title_clean = " ".join(str(raw_title).split()) if isinstance(raw_title, str) else ""
    title: Optional[str] = title_clean if title_clean else None

    authors = _clean_authors(kaggle_record.get("authors"))
    missing_fields: List[str] = []
    for field_name, val in (("title", title), ("authors", authors), ("published", published), ("updated", updated)):
        if not val:
            missing_fields.append(field_name)

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
        "missing_fields": missing_fields,
    }


def _is_fully_resolved_found(store: CorpusStorage, mssv: str, arxiv_id: str) -> Tuple[bool, int]:
    """Check whether a paper with discovery_status=='found' already has complete version & date coverage."""
    try:
        meta = store.read_metadata(mssv, arxiv_id)
        versions = meta.get("versions") or []
        if (
            meta.get("version_status") == "resolved"
            and bool(versions)
            and meta.get("submission_date") is not None
            and meta.get("revised_dates") is not None
            and len(meta["revised_dates"]) == len(versions) - 1
            and all(v.get("date_status") == "verified" and _valid_date(v.get("date")) for v in versions)
        ):
            return True, len(versions)
    except Exception:
        pass
    return False, 0


def ingest_kaggle_snapshot(
    snapshot_path: str | Path,
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
) -> dict:
    """Stream arxiv-metadata-oai-snapshot.json and pre-populate CorpusStorage manifests.

    Returns summary metrics:
      - target_papers: total pairs requested
      - found_papers: papers matched (in cache or Kaggle snapshot) with status=='found'
      - not_found_papers: papers already recorded as 'not_found'
      - found_versions: total versions discovered across found_papers
      - missing_papers: list of arxiv_ids still needing live arXiv API discovery
    """
    snapshot_file = Path(snapshot_path)
    if not snapshot_file.exists():
        raise FileNotFoundError(f"Kaggle snapshot file not found: {snapshot_file}")

    from src.storage import arxiv_id_to_folder_key
    pairs_list = [(str(m), str(a)) for m, a in pairs]
    target_map: Dict[str, str] = {}
    found_count = 0
    not_found_count = 0
    total_versions = 0

    for mssv, arxiv_id in pairs_list:
        try:
            manifest_file = store.root / arxiv_id_to_folder_key(arxiv_id) / "manifest.json"
            if manifest_file.exists():
                m = json.loads(manifest_file.read_text(encoding="utf-8"))
                disc_status = m.get("discovery_status")
                if disc_status == "not_found":
                    not_found_count += 1
                    continue
                if disc_status == "found":
                    resolved, v_count = _is_fully_resolved_found(store, mssv, arxiv_id)
                    if resolved:
                        found_count += 1
                        total_versions += v_count
                        continue
        except Exception:
            pass
        target_map[arxiv_id] = mssv

    snapshot_size = snapshot_file.stat().st_size if snapshot_file.is_file() else 0
    snapshot_provenance = {
        "dataset": "Cornell-University/arxiv",
        "snapshot_path": str(snapshot_file.resolve()),
        "snapshot_size_bytes": snapshot_size,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    remaining_ids: Set[str] = set(target_map.keys())
    if not remaining_ids:
        return {
            "target_papers": len(pairs_list),
            "found_papers": found_count,
            "not_found_papers": not_found_count,
            "found_versions": total_versions,
            "missing_papers": [],
            "interrupted": False,
            "snapshot_provenance": snapshot_provenance,
        }

    open_fn = gzip.open if snapshot_file.suffix == ".gz" else open
    mode = "rt" if snapshot_file.suffix == ".gz" else "r"
    prefixes = tuple({aid[:5] for aid in remaining_ids if len(aid) >= 5})
    single_prefix = prefixes[0] if len(prefixes) == 1 else None

    interrupted = False
    try:
        with open_fn(snapshot_file, mode, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not remaining_ids:
                    break
                if single_prefix is not None:
                    if single_prefix not in line:
                        continue
                elif prefixes and not any(p in line for p in prefixes):
                    continue
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
                    if snapshot is None:
                        continue
                    store.record_discovery(snapshot)
                    if (
                        snapshot.get("published") is not None
                        and snapshot.get("revised_dates") is not None
                        and len(snapshot["revised_dates"]) == len(snapshot["version_ids"]) - 1
                    ):
                        meta = store.read_metadata(mssv, record_id)
                        if meta.get("status") != "complete":
                            meta["revised_dates"] = list(snapshot["revised_dates"])
                            for idx, rev_date in enumerate(snapshot["revised_dates"], start=1):
                                if idx < len(meta.get("versions", [])) and _valid_date(rev_date):
                                    meta["versions"][idx]["date"] = rev_date
                                    meta["versions"][idx]["date_status"] = "verified"
                            meta["provenance"]["version_history"] = {
                                "provider": "kaggle_snapshot",
                                "query_identifier": record_id,
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                "snapshot": list(snapshot["revised_dates"]),
                            }
                            store.save_metadata(mssv, record_id, meta)
                    remaining_ids.remove(record_id)
                    found_count += 1
                    total_versions += len(snapshot["version_ids"])
    except KeyboardInterrupt:
        interrupted = True

    return {
        "target_papers": len(pairs_list),
        "found_papers": found_count,
        "not_found_papers": not_found_count,
        "found_versions": total_versions,
        "missing_papers": sorted(remaining_ids),
        "interrupted": interrupted,
        "snapshot_provenance": snapshot_provenance,
    }

"""Evidence-backed M1 date enrichment; never edits S2 snapshots or source files."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.arxiv_discovery import ID_PATTERN, parse_arxiv_batch_response
from src.m1_phase_modules.checkpoint import _ensure_directory, _read_json, _write_json
from src.storage import CorpusStorage


def _base_id(identifier: str) -> str:
    match = ID_PATTERN.fullmatch(identifier) if isinstance(identifier, str) else None
    if match is None:
        raise ValueError("Invalid reference arXiv ID")
    return match.group("base")


def _day(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Unobserved submission date")
    if len(value) == 10:
        return date.fromisoformat(value).isoformat()
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Submission timestamp needs timezone")
    return instant.astimezone(timezone.utc).date().isoformat()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _blob(root: Path, digest: str, suffix: str) -> Path:
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("Invalid reference-date checksum")
    path = root / (digest + suffix)
    if path.is_symlink():
        raise ValueError("Symlinked reference-date evidence")
    return path


def _save_blob(root: Path, value: dict, suffix: str = ".json") -> str:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = _digest(raw)
    target = _blob(root, digest, suffix)
    if target.exists():
        if _digest(target.read_bytes()) != digest:
            raise ValueError("Reference-date blob checksum mismatch")
    elif _write_json(target, value) != digest:
        raise ValueError("Reference-date serialization mismatch")
    return digest


def _validate_entry(root: Path, mssv: str, identifier: str, entry: dict) -> None:
    if (entry.get("arxiv_id") != identifier or _base_id(identifier) != identifier
            or entry.get("status") not in {"found", "unavailable", "failed"}):
        raise ValueError("Invalid reference-date identity/status")
    stamp = datetime.fromisoformat(entry["checked_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Invalid reference-date timestamp")
    if entry["status"] != "found":
        if entry.get("date") is not None:
            raise ValueError("Failed lookup cannot provide a date")
        return
    if entry.get("source") == "arxiv_atom_published":
        digest = entry["response_sha256"]
        raw = _blob(root, digest, ".response.json").read_bytes()
        if _digest(raw) != digest:
            raise ValueError("Reference-date Atom checksum mismatch")
        response = json.loads(raw)
        if response.get("http_status") != 200:
            raise ValueError("Reference-date evidence needs HTTP 200")
        parsed = parse_arxiv_batch_response(response["raw_xml"], mssv, response["requested_ids"])
        evidence = parsed.get(identifier) or {}
        if evidence.get("arxiv_id") != identifier or evidence.get("status") != "found":
            raise ValueError("Reference-date Atom identity mismatch")
        value = evidence.get("published")
    elif entry.get("source") == "verified_local_arxiv":
        from src.arxiv_history import _cached, _single_version_cached
        evidence = entry["metadata"]
        if evidence.get("arxiv_id") != identifier or not (
                _cached(evidence) or _single_version_cached(evidence)):
            raise ValueError("Unverified local reference date")
        value = evidence.get("submission_date")
    else:
        raise ValueError("Unknown reference-date source")
    if entry.get("date") != _day(value):
        raise ValueError("Reference date differs from source evidence")


def load_reference_dates(root: Path, mssv: str) -> dict:
    """Read and verify immutable cache entries and their underlying evidence."""
    path = root / "index.json"
    if not path.exists():
        return {}
    index = _read_json(path)
    if index.get("schema_version") != 1 or index.get("mssv") != mssv:
        raise ValueError("Incompatible reference-date cache")
    entries = {}
    for identifier, digest in index["entries"].items():
        raw = _blob(root, digest, ".json").read_bytes()
        if _digest(raw) != digest:
            raise ValueError("Reference-date entry checksum mismatch")
        entry = json.loads(raw)
        _validate_entry(root, mssv, identifier, entry)
        entries[identifier] = entry
    return entries


def enrich_reference_dates(
    store: CorpusStorage, pairs: list[tuple[str, str]], root: Path, *,
    should_stop: Callable[[], bool] = lambda: False,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    """Fill missing projected dates using verified cache and paced arXiv batches."""
    from src import arxiv_client
    from src.arxiv_history import _cached, _single_version_cached

    members = {member for member, _ in pairs}
    if len(members) != 1:
        raise ValueError("Date enrichment requires one member selection")
    mssv = next(iter(members))
    root = Path(root)
    _ensure_directory(root, root.parent)
    entries = load_reference_dates(root, mssv)
    needed, edges = set(), 0
    for member, identifier in pairs:
        refs = store.read_references(member, identifier)
        if refs.get("status") != "complete" or (refs.get("coverage") or {}).get("complete") is not True:
            continue
        for item in refs["items"]:
            if item.get("arxiv_id") and not (item.get("submission_date") or item.get("publication_date")):
                needed.add(_base_id(item["arxiv_id"]))
                edges += 1

    def save() -> None:
        hashes = {identifier: _save_blob(root, entry) for identifier, entry in entries.items()}
        _write_json(root / "index.json", {"schema_version": 1, "mssv": mssv, "entries": hashes})

    pending, reused = [], 0
    for identifier in sorted(needed):
        cached = entries.get(identifier)
        if cached is not None:
            checked = datetime.fromisoformat(cached["checked_at"])
            wait = timedelta(hours=24) if cached["status"] == "unavailable" else timedelta(minutes=5)
            if cached["status"] == "found" or now() - checked < wait:
                reused += 1
                continue
        try:
            local = store.read_metadata(mssv, identifier)
            verified = local.get("arxiv_id") == identifier and (_cached(local) or _single_version_cached(local))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            verified = False
        if verified:
            entries[identifier] = {"arxiv_id": identifier, "status": "found",
                                   "date": _day(local["submission_date"]),
                                   "source": "verified_local_arxiv", "metadata": local,
                                   "checked_at": now().isoformat()}
        else:
            pending.append(identifier)
    save()
    stopped = False
    for start in range(0, len(pending), 20):
        if should_stop():
            stopped = True
            break
        batch = pending[start:start + 20]
        print(f"[M1/ngày references] Tra arXiv {start + 1}–{start + len(batch)}/{len(pending)} ID còn thiếu.", flush=True)
        with arxiv_client.golden_ratio_backoff_policy(wait_budget_seconds=120.0):
            response = arxiv_client.query_arxiv_batch(mssv, batch, max_attempts=3)
        raw = response.get("raw_xml")
        parsed = {}
        if response.get("status") == "found" and response.get("http_status") == 200 and isinstance(raw, str):
            parsed = parse_arxiv_batch_response(raw, mssv, batch)
            digest = _save_blob(root, {"raw_xml": raw, "requested_ids": batch,
                                       "http_status": 200}, ".response.json")
        for identifier in batch:
            observed = parsed.get(identifier) or {}
            entry = {"arxiv_id": identifier, "checked_at": now().isoformat(),
                     "status": "failed", "date": None,
                     "reason": "arxiv_id_unresolved" if parsed else "lookup_failed"}
            if observed.get("arxiv_id") == identifier and observed.get("status") == "found":
                entry.update(status="unavailable", reason="date_unobserved")
                try:
                    value = _day(observed.get("published"))
                except (ValueError, TypeError):
                    pass
                else:
                    entry.update(status="found", date=value, source="arxiv_atom_published",
                                 response_sha256=digest)
                    entry.pop("reason")
            entries[identifier] = entry
        save()
        if response.get("http_status") in {403, 429}:
            stopped = True
            break
    verified_entries = load_reference_dates(root, mssv)
    dates = {identifier: entry["date"] for identifier, entry in verified_entries.items()
             if identifier in needed and entry["status"] == "found"}
    return {"schema_version": 1, "missing_date_rows_before": edges,
            "missing_date_unique_ids": len(needed), "resolved_unique_ids": len(dates),
            "unresolved_unique_ids": len(needed - dates.keys()), "cache_reused_ids": reused,
            "stopped": stopped, "dates": dates, "entries": verified_entries,
            "cache_index": str(root / "index.json")}

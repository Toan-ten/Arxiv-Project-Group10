"""Opt-in version-date enrichment using version-specific Atom entries.

No import-time I/O, no source writes, and no implicit Pipeline refresh. Call
only for an existing, resolved checkpoint, with one corpus writer at a time.
"""
import copy
from datetime import datetime, timezone
import math
from urllib.parse import parse_qs, urlencode, urlsplit
import xml.etree.ElementTree as ET

from src import arxiv_client, s2_integration
from src.arxiv_discovery import ID_PATTERN, _entry_id
from src.storage import StorageError, TERMINAL

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_XML_BYTES = 2 * 1024 * 1024


def _require(condition, message):
    if not condition:
        raise StorageError("Invalid version history: " + message)


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(stamp.tzinfo is not None, "timestamp needs timezone")
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise StorageError("Invalid version history: timestamp") from None


def _identifier(identifier):
    match = ID_PATTERN.fullmatch(identifier) if isinstance(identifier, str) else None
    _require(match is not None and match.group("version") is not None,
             "expected exact versioned arXiv ID")
    return match.group("base"), int(match.group("version"))


def _entry_identity(value):
    try:
        return _entry_id(value)
    except (ValueError, TypeError, AttributeError):
        raise StorageError("Invalid version history: entry URL") from None


def parse_version_response(xml, versioned_id):
    """Validate one exact version, returning dates and the original XML evidence.

    Atom published describes v1; updated describes the version actually returned.
    Reject latest-version fallback instead of attributing its date to another vN.
    """
    base, number = _identifier(versioned_id)
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    _require(isinstance(xml, bytes) and len(xml) <= MAX_XML_BYTES, "XML size/type")
    # Only UTF-8 Atom, with no entity declarations; never expand a DTD.
    try:
        text = xml.decode("utf-8-sig")
        _require("<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(), "DTD")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise StorageError("Invalid version history: XML") from None
    _require(root.tag == ATOM + "feed", "Atom feed namespace")
    entries = root.findall(ATOM + "entry")
    _require(len(entries) == 1, "expected one entry")
    for link in root.findall(ATOM + "link"):
        if link.get("rel") == "self":
            try:
                query = parse_qs(urlsplit(link.get("href", "")).query)
            except ValueError:
                raise StorageError("Invalid version history: feed query URL") from None
            _require(query.get("id_list") == [versioned_id], "feed query identity")
    entry = entries[0]
    _require(len(entry.findall(ATOM + "id")) == 1
             and _entry_identity(entry.findtext(ATOM + "id")) == (base, number), "entry identity")
    for link in entry.findall(ATOM + "link"):
        if link.get("rel") == "alternate":
            _require(_entry_identity(link.get("href")) == (base, number), "alternate identity")
    dates = {}
    for field in ("published", "updated"):
        _require(len(entry.findall(ATOM + field)) == 1, "missing/duplicate " + field)
        raw = entry.findtext(ATOM + field)
        dates[field] = _instant(raw).isoformat().replace("+00:00", "Z")
    _require(_instant(dates["updated"]) >= _instant(dates["published"]), "date order")
    _require(number != 1 or dates["published"] == dates["updated"], "v1 date mismatch")
    return dict(versioned_id=versioned_id, published=dates["published"],
                date=dates["updated"], raw_xml=text)


def _verified_dates(metadata, records):
    _require(isinstance(records, list) and len(records) == len(metadata["versions"]),
             "incomplete version coverage")
    dates, published = [], None
    for version, record in zip(metadata["versions"], records):
        _require(isinstance(record, dict), "record type")
        parsed = parse_version_response(record.get("raw_xml"), version["versioned_id"])
        _require(all(record.get(k) == parsed[k] for k in parsed), "snapshot mismatch")
        _require(record.get("http_status") == 200 and type(record.get("attempts")) is int
                 and record["attempts"] > 0, "HTTP evidence")
        published = parsed["published"] if published is None else published
        _require(parsed["published"] == published, "conflicting submission dates")
        if version["date"] is not None:
            _require(_instant(version["date"]) == _instant(parsed["date"]),
                     "conflict with known version date")
        dates.append(parsed["date"])
    _require(dates and all(_instant(a) <= _instant(b) for a, b in zip(dates, dates[1:])),
             "revision order")
    _require(metadata["submission_date"] is None
             or _instant(metadata["submission_date"]) == _instant(dates[0]),
             "conflict with known submission date")
    return dates


def _checkpoint(store, mssv, arxiv_id):
    metadata = store.read_metadata(mssv, arxiv_id)
    manifest = store.read_manifest(mssv, arxiv_id)
    s2_integration.validate_checkpoint(store, mssv, arxiv_id)
    _require(metadata["version_status"] == manifest["version_status"] == "resolved"
             and manifest["discovery_status"] == "found", "unresolved discovery")
    _require({v["version"] for v in metadata["versions"]} == set(manifest["versions"])
             and bool(metadata["versions"]), "checkpoint version coverage")
    _require(manifest["metadata_status"] == metadata["status"], "checkpoint metadata status")
    _require(manifest["references_status"] == store.read_references(mssv, arxiv_id)["status"],
             "checkpoint references status")
    for name, record in manifest["versions"].items():
        if record["status"] in TERMINAL:
            store._verify_source(mssv, arxiv_id, name, record)
    return metadata


def _cached(metadata):
    history = metadata["provenance"]["version_history"]
    if history is None:
        return False
    _require(isinstance(history, dict), "history provenance dict")
    provider = history.get("provider")
    _require(provider in {"arxiv_atom_versioned", "kaggle_snapshot"}
             and history.get("query_identifier") == metadata["arxiv_id"],
             "unknown history provenance requires audit")
    _instant(history.get("retrieved_at"))
    if provider == "kaggle_snapshot":
        _require(all(v["date_status"] == "verified" for v in metadata["versions"])
                 and metadata["revised_dates"] is not None
                 and [v["date"] for v in metadata["versions"][1:]] == metadata["revised_dates"],
                 "kaggle history disagreements")
        return True
    dates = _verified_dates(metadata, history.get("snapshot"))
    _require(metadata["submission_date"] is not None
             and _instant(metadata["submission_date"]) == _instant(dates[0])
             and all(v["date_status"] == "verified" for v in metadata["versions"])
             and metadata["revised_dates"] is not None
             and [_instant(d) for d in metadata["revised_dates"]] == [_instant(d) for d in dates[1:]],
             "history cache disagrees with dates")
    return True


def _single_version_cached(metadata):
    if len(metadata["versions"]) != 1 or metadata["revised_dates"] != []:
        return False
    version = metadata["versions"][0]
    if version["date_status"] != "verified" or metadata["submission_date"] is None:
        return False
    arxiv = metadata["provenance"]["arxiv"]
    if not isinstance(arxiv, dict):
        return False
    source = arxiv.get("field_sources", {}).get("published", arxiv)
    snapshot = source.get("snapshot") if isinstance(source, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("status") != "found":
        return False
    return (snapshot.get("arxiv_id") == metadata["arxiv_id"]
            and snapshot.get("mssv") == metadata["mssv"]
            and source.get("query_identifier") == metadata["arxiv_id"]
            and _instant(snapshot.get("published")) == _instant(version["date"])
            == _instant(metadata["submission_date"]))


def enrich_version_history(store, mssv, arxiv_id, *, timeout=30.0, max_attempts=1,
                           user_agent=arxiv_client.USER_AGENT):
    """Fetch each confirmed vN via the existing shared arXiv HTTP transport.

    All dates are checked before a single save_metadata call. Failed, mismatched
    or partial retrieval never writes checkpoint JSON. 403/429 stops this call;
    a caller processing multiple papers must honor stopped=True. Complete
    verified history and a verified single-version discovery are reused without
    requests or writes. Pipeline invokes this when revision_history is enabled.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be positive and finite")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if (not isinstance(user_agent, str) or not user_agent.strip() or not user_agent.isascii()
            or "\r" in user_agent or "\n" in user_agent):
        raise ValueError("Invalid User-Agent")
    metadata = _checkpoint(store, mssv, arxiv_id)
    if _cached(metadata) or _single_version_cached(metadata):
        return dict(action="cached", status="complete", stopped=False, records=[], error=None)
    _require(metadata["status"] != "complete", "complete metadata is immutable")
    records = []
    for version in metadata["versions"]:
        identifier = version["versioned_id"]

        def read_xml(response):
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in arxiv_client.XML_CONTENT_TYPES:
                return dict(status="invalid_response", error="Expected Atom/XML")
            try:
                return dict(status="found", **parse_version_response(
                    response.read(MAX_XML_BYTES + 1), identifier))
            except StorageError as exc:
                return dict(status="invalid_response", error=str(exc))

        url = arxiv_client.API_URL + "?" + urlencode(
            dict(id_list=identifier, start=0, max_results=1))
        result = arxiv_client._request_arxiv(
            url, timeout=timeout, max_attempts=max_attempts, user_agent=user_agent,
            accept="application/atom+xml", read_response=read_xml)
        if result["status"] != "found":
            return dict(action="queried", status="partial" if records else "failed",
                        stopped=result.get("http_status") in {403, 429},
                        http_status=result.get("http_status"), records=records,
                        error=dict(type=result["status"], message=result.get("error"),
                                   versioned_id=identifier, attempts=result["attempts"]))
        records.append({k: result[k] for k in (
            "versioned_id", "published", "date", "raw_xml", "http_status", "attempts")})
    dates = _verified_dates(metadata, records)
    # Detect a changed checkpoint before persistence; still requires one writer.
    _require(_checkpoint(store, mssv, arxiv_id) == metadata, "checkpoint changed during fetch")
    value = copy.deepcopy(metadata)
    # Preserve the original v1 spelling/provenance if it denotes the same instant.
    value["submission_date"] = metadata["submission_date"] or dates[0]
    for index, version in enumerate(value["versions"]):
        version.update(date=version["date"] or dates[index], date_status="verified")
    value["revised_dates"] = [v["date"] for v in value["versions"][1:]]
    value["provenance"]["version_history"] = dict(
        provider="arxiv_atom_versioned", query_identifier=arxiv_id,
        retrieved_at=datetime.now(timezone.utc).isoformat(), snapshot=records)
    if s2_integration.metadata_cached(metadata):
        original = copy.deepcopy(metadata["provenance"]["semantic_scholar"])
        value = s2_integration.metadata_snapshot(value, original["snapshot"])
        # No new S2 request was made. Preserve its real retrieval timestamp.
        value["provenance"]["semantic_scholar"] = original
    else:
        value["missing_fields"] = [f for f in value["missing_fields"] if f != "version_history"]
    store.save_metadata(mssv, arxiv_id, value)
    return dict(action="queried", status="complete", stopped=False, records=records, error=None)


def paper_row(store, mssv, arxiv_id):
    """Project known fields into the final papers schema; no Parquet writer.

    Missing required metadata raises StorageError instead of turning unknown
    revision history into []. This projection does not certify publication
    completeness, sources, references or M1 acceptance.
    """
    value = _checkpoint(store, mssv, arxiv_id)
    _require(bool(value["title"]) and bool(value["authors"]) and value["submission_date"] is not None
             and value["revised_dates"] is not None, "papers fields still unknown")
    if len(value["versions"]) > 1 or value["provenance"]["version_history"] is not None:
        _require(_cached(value), "unverified revision history")
    else:
        _require(value["versions"][0]["date_status"] == "verified"
                 and value["versions"][0]["date"] == value["submission_date"]
                 and value["revised_dates"] == [], "unverified v1")
    return {key: copy.deepcopy(value[key]) for key in (
        "arxiv_id", "title", "authors", "submission_date", "revised_dates", "venue")}

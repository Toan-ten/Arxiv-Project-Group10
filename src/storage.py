"""Offline corpus persistence for the M1 v2 contract.

Single writer only. This module never calls a network client, downloads source,
or deletes/replaces source directories. JSON files are committed individually.
"""
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

from src.arxiv_source import SOURCE_URL
from src.id_ranges import ConfigError, arxiv_id_to_folder_key, load_config


CONTRACT_VERSION = 2
STAGES = {
    "not_attempted", "in_progress", "partial", "complete",
    "not_found", "failed", "interrupted",
}
SOURCE_ERRORS = {
    "timeout", "dns_error", "connection_error", "tls_error", "http_error",
    "rate_limited", "server_error", "archive_error", "unsafe_archive",
    "unsupported_source", "invalid_response", "size_limit", "storage_error",
    "output_exists", "validation_failed",
    "http_error_404_unverified", "version_not_found", "transient_error", "permanent_error",
}
DISCOVERY_STATES = SOURCE_ERRORS | {
    "not_attempted", "found", "not_found", "api_error", "invalid_xml",
}
TERMINAL = {"success", "no_tex_source", "withdrawn_version"}
VERSION_STATES = TERMINAL | SOURCE_ERRORS | {"pending", "in_progress", "interrupted"}
RETRYABLE = {
    "pending", "interrupted", "timeout", "dns_error", "connection_error",
    "rate_limited", "server_error", "transient_error",
}
PAPER_STATES = {
    "not_attempted", "in_progress", "partial", "failed", "interrupted",
    "not_found", "complete", "complete_with_no_tex", "complete_with_withdrawn",
}
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


class StorageError(ValueError):
    """Invalid input, identity, contract, or checkpoint."""


class IntegrityError(StorageError):
    """Existing output cannot safely be trusted or overwritten."""


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require(condition, message):
    if not condition:
        raise StorageError(message)


def _state(value, choices, field):
    _require(isinstance(value, str) and value in choices,
             f"Invalid {field}: expected a supported state string")


def _clone(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise StorageError("Expected finite, JSON-serializable data") from exc


def _keys(value, names):
    _require(isinstance(value, dict) and set(names) <= value.keys(),
             "Missing fields or invalid JSON object")


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _relative(value):
    _require(isinstance(value, str) and value and "\\" not in value,
             "Source paths must use relative POSIX notation")
    parts = value.split("/")
    _require(
        all(part not in {"", ".", ".."} and not part.endswith((" ", "."))
            and not any(ord(char) < 32 or char in ':*?"<>|' for char in part)
            and part.split(".")[0].upper() not in _RESERVED for part in parts),
        "Unsafe source path",
    )
    _require(not PurePosixPath(value).is_absolute(), "Absolute source path")
    return parts


def _version(value):
    _require(isinstance(value, str) and re.fullmatch(r"v[1-9][0-9]*", value),
             "Expected version name v1, v2, ...")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def _atomic_json(path, payload):
    """A failed write/replace leaves the previous JSON intact."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            # Directory fsync is not supported by every filesystem.
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
    finally:
        temporary.unlink(missing_ok=True)


class CorpusStorage:
    """Persist assigned papers under a caller-selected corpus root.

    Methods read/validate the current checkpoint before mutation. Use resume()
    after an interrupted session; a plan never performs a download.
    """

    def __init__(self, root, *, run_id=None):
        _require(run_id is None or isinstance(run_id, str) and run_id.strip(),
                 "run_id must be a nonempty string or None")
        self.root = Path(root).expanduser().resolve()
        self.run_id = run_id
        self.assignments = load_config()

    def _safe(self, path):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("Path is outside corpus root") from exc
        current = self.root
        for part in ("", *relative.parts):
            if part:
                current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise IntegrityError("Links/junctions are not allowed inside corpus")
        if not path.resolve().is_relative_to(self.root):
            raise IntegrityError("Path resolves outside corpus")
        return path

    def paper_path(self, mssv, arxiv_id):
        try:
            key = arxiv_id_to_folder_key(arxiv_id)
        except ConfigError as exc:
            raise StorageError(str(exc)) from exc
        _require(isinstance(mssv, str) and mssv in self.assignments, "Unknown MSSV")
        assigned = self.assignments[mssv]
        _require(assigned.start_arxiv_id <= arxiv_id <= assigned.end_arxiv_id,
                 "arXiv ID does not belong to this MSSV")
        return self._safe(self.root / key)

    def _path(self, mssv, arxiv_id, name):
        return self._safe(self.paper_path(mssv, arxiv_id) / name)

    def _read(self, path):
        self._safe(path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC

        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise IntegrityError(f"Cannot read valid checkpoint: {path.name}") from exc

        try:
            info = os.fstat(fd)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise IntegrityError("JSON must be a regular, unlinked file")
            with open(fd, "r", encoding="utf-8", closefd=False) as stream:
                value = json.load(stream, object_pairs_hook=_pairs,
                                  parse_constant=lambda value: _require(False, "Nonfinite JSON"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"Cannot read valid checkpoint: {path.name}") from exc
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        _require(isinstance(value, dict), "JSON root must be an object")
        return value

    def _identity(self, value, mssv, arxiv_id, *, owner=True):
        fields = ["contract_version", "arxiv_id"] + (["mssv"] if owner else [])
        _keys(value, fields)
        _require(type(value["contract_version"]) is int
                 and value["contract_version"] == CONTRACT_VERSION, "Wrong contract version")
        _require(value["arxiv_id"] == arxiv_id, "Wrong arXiv identity")
        if owner:
            _require(value["mssv"] == mssv, "Wrong ownership")
        self.paper_path(mssv, arxiv_id)

    def _versions(self, metadata, arxiv_id):
        values = metadata["versions"]
        _require(isinstance(values, list), "versions must be a list")
        names = []
        for item in values:
            _keys(item, ["version", "versioned_id", "date", "date_status"])
            name = _version(item["version"])
            _require(item["versioned_id"] == arxiv_id + name, "Wrong version identity")
            _state(item["date_status"], {"unknown", "verified"}, "version.date_status")
            _require(item["date"] is None or _valid_date(item["date"]), "Invalid version date")
            _require((item["date_status"] == "verified") == (item["date"] is not None),
                     "Date verification and value disagree")
            names.append(name)
        _require(names == [f"v{n}" for n in range(1, len(names) + 1)],
                 "Versions must be unique and contiguous from v1")
        _require(metadata["version_status"] != "resolved" or names,
                 "Resolved version list cannot be empty")
        _require(metadata["version_status"] == "resolved" or not names,
                 "Unknown version list must stay empty")
        return names

    def _metadata(self, value, mssv, arxiv_id):
        self._identity(value, mssv, arxiv_id)
        _keys(value, ["status", "title", "authors", "submission_date", "revised_dates",
                      "venue", "version_status", "versions", "missing_fields", "provenance"])
        _state(value["status"], STAGES, "metadata.status")
        _state(value["version_status"], {"resolved", "needs_verification", "not_applicable"},
               "metadata.version_status")
        for field in ("title", "venue"):
            _require(value[field] is None or isinstance(value[field], str), "Invalid text field")
        _require(value["authors"] is None or _strings(value["authors"]), "Invalid authors")
        _require(value["submission_date"] is None or _valid_date(value["submission_date"]),
                 "Invalid submission date")
        dates = value["revised_dates"]
        _require(dates is None or isinstance(dates, list) and all(_valid_date(x) for x in dates),
                 "Invalid revision dates")
        _require(_strings(value["missing_fields"]), "Invalid missing_fields")
        _keys(value["provenance"], ["arxiv", "semantic_scholar", "version_history"])
        _require(all(item is None or isinstance(item, dict)
                     for item in value["provenance"].values()), "Invalid metadata provenance")
        arxiv = value["provenance"]["arxiv"]
        if arxiv is not None:
            sources = arxiv.get("field_sources", {})
            _require(isinstance(sources, dict)
                     and set(sources) <= {"title", "authors", "published"},
                     "Invalid arXiv field sources")
            for source in [arxiv, *sources.values()]:
                _keys(source, ["query_identifier", "retrieved_at", "snapshot"])
                _keys(source["snapshot"], ["mssv", "arxiv_id", "status"])
                _state(source["snapshot"]["status"], DISCOVERY_STATES, "arxiv.snapshot.status")
                _require(source["query_identifier"] == arxiv_id
                         and source["snapshot"]["arxiv_id"] == arxiv_id
                         and source["snapshot"]["mssv"] == mssv
                         and _valid_date(source["retrieved_at"]), "Wrong discovery provenance")
        names = self._versions(value, arxiv_id)
        if value["status"] == "complete":
            _require(bool(value["title"]) and bool(value["authors"])
                     and value["submission_date"] is not None and dates is not None
                     and len(dates) == len(names) - 1 and not value["missing_fields"]
                     and bool(value["provenance"]["semantic_scholar"]),
                     "Incomplete M1 metadata cannot be complete")
            _require(all(x["date_status"] == "verified" for x in value["versions"]),
                     "Version dates still need verification")
            _require(value["submission_date"] == value["versions"][0]["date"]
                     and dates == [item["date"] for item in value["versions"][1:]],
                     "Metadata dates disagree with verified version dates")
            _require(len(names) == 1 or bool(value["provenance"]["version_history"]),
                     "Revision history needs provenance")

    def _references(self, value, mssv, arxiv_id):
        self._identity(value, mssv, arxiv_id, owner=False)
        _keys(value, ["status", "provenance", "coverage", "items"])
        _state(value["status"], STAGES, "references.status")
        _keys(value["provenance"], ["provider", "query_identifier", "s2_paper_id", "retrieved_at"])
        _require(value["provenance"]["provider"] == "semantic_scholar", "Wrong reference provider")
        coverage = value["coverage"]
        _keys(coverage, ["complete", "continuation", "reported_total", "retrieved_count"])
        _require(type(coverage["complete"]) is bool, "Invalid coverage flag")
        _require(isinstance(value["items"], list), "References must be a list")
        _require(type(coverage["retrieved_count"]) is int
                 and coverage["retrieved_count"] == len(value["items"]), "Wrong reference count")
        total = coverage["reported_total"]
        _require(total is None or type(total) is int and total >= len(value["items"]),
                 "Invalid reported reference total")
        for item in value["items"]:
            _keys(item, ["arxiv_id", "title", "authors", "submission_date", "publication_date",
                         "year", "venue", "s2_paper_id", "missing_fields", "raw"])
            for field in ("arxiv_id", "title", "venue", "s2_paper_id"):
                _require(item[field] is None or isinstance(item[field], str), "Invalid reference field")
            _require(item["authors"] is None or _strings(item["authors"]), "Invalid reference authors")
            for field in ("submission_date", "publication_date"):
                _require(item[field] is None or _valid_date(item[field]), "Invalid reference date")
            _require(item["year"] is None or type(item["year"]) is int, "Invalid reference year")
            _require(_strings(item["missing_fields"]) and isinstance(item["raw"], dict),
                     "Invalid reference provenance")
        _require(value["status"] == "complete" or not coverage["complete"],
                 "Noncomplete stage cannot claim complete coverage")
        if value["status"] == "complete":
            _require(coverage["complete"] and coverage["continuation"] is None
                     and (total is None or total == len(value["items"]))
                     and bool(value["provenance"]["query_identifier"])
                     and _valid_date(value["provenance"]["retrieved_at"]),
                     "Complete references need verified coverage and provenance")

    def _manifest(self, value, mssv, arxiv_id):
        self._identity(value, mssv, arxiv_id)
        _keys(value, ["status", "run_id", "discovery_status", "version_status",
                      "metadata_status", "references_status", "versions", "updated_at"])
        _state(value["status"], PAPER_STATES, "manifest.status")
        _require(value["run_id"] is None or isinstance(value["run_id"], str),
                 "Invalid run ID")
        _require(value["updated_at"] is None or _valid_date(value["updated_at"]),
                 "Invalid manifest timestamp")
        _state(value["discovery_status"], DISCOVERY_STATES, "manifest.discovery_status")
        _state(value["version_status"], {"resolved", "needs_verification", "not_applicable"},
               "manifest.version_status")
        _state(value["metadata_status"], STAGES, "manifest.metadata_status")
        _state(value["references_status"], STAGES, "manifest.references_status")
        _require(isinstance(value["versions"], dict), "Invalid version manifest")
        names = list(value["versions"])
        _require(set(names) == {f"v{n}" for n in range(1, len(names) + 1)},
                 "Invalid manifest version coverage")
        for name, record in value["versions"].items():
            _version(name)
            _keys(record, ["versioned_id", "status", "source_status", "files", "file_checksums",
                           "kept_bytes", "attempts", "error", "completed_at", "http_status",
                           "content_type", "source_url", "archive_bytes", "archive_sha256",
                           "extracted_bytes", "removed_files", "run_id"])
            _require(record["versioned_id"] == arxiv_id + name, "Wrong manifest version")
            _state(record["status"], VERSION_STATES, f"versions.{name}.status")
            if record["source_status"] is not None:
                _state(record["source_status"], TERMINAL | SOURCE_ERRORS,
                       f"versions.{name}.source_status")
            _require(_strings(record["files"]) and len(record["files"]) == len(set(record["files"])),
                     "Invalid file list")
            for filename in record["files"]:
                _relative(filename)
            _require(isinstance(record["file_checksums"], dict), "Invalid checksums")
            for filename, digest in record["file_checksums"].items():
                _relative(filename)
                _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                         "Invalid SHA-256")
            _require(type(record["attempts"]) is int and record["attempts"] >= 0, "Invalid attempts")
            for field in ("kept_bytes", "archive_bytes", "extracted_bytes", "removed_files"):
                _require(record[field] is None or type(record[field]) is int and record[field] >= 0,
                         "Invalid byte/file metric")
            _require(record["http_status"] is None
                     or type(record["http_status"]) is int and 100 <= record["http_status"] <= 599,
                     "Invalid HTTP status")
            _require(record["content_type"] is None or isinstance(record["content_type"], str),
                     "Invalid content type")
            _require(record["source_url"] == SOURCE_URL + arxiv_id + name, "Wrong source URL")
            _require(record["archive_sha256"] is None
                     or isinstance(record["archive_sha256"], str)
                     and re.fullmatch(r"[0-9a-f]{64}", record["archive_sha256"]),
                     "Invalid archive checksum")
            _require(record["run_id"] is None or isinstance(record["run_id"], str), "Invalid source run")
            _require(record["completed_at"] is None or _valid_date(record["completed_at"]),
                     "Invalid completion timestamp")
            if record["error"] is not None:
                _keys(record["error"], ["stage", "type", "message"])
                _require(all(isinstance(record["error"][field], str)
                             for field in ("stage", "type", "message")), "Invalid error record")

    def read_manifest(self, mssv, arxiv_id):
        value = self._read(self._path(mssv, arxiv_id, "manifest.json"))
        self._manifest(value, mssv, arxiv_id)
        return value

    def read_metadata(self, mssv, arxiv_id):
        value = self._read(self._path(mssv, arxiv_id, "metadata.json"))
        self._metadata(value, mssv, arxiv_id)
        return value

    def read_references(self, mssv, arxiv_id):
        value = self._read(self._path(mssv, arxiv_id, "references.json"))
        self._references(value, mssv, arxiv_id)
        return value

    def initialize_paper(self, mssv, arxiv_id):
        paper = self.paper_path(mssv, arxiv_id)
        if paper.exists() and any(paper.iterdir()):
            # Fail closed on incomplete initialization/orphaned source.
            manifest = self.read_manifest(mssv, arxiv_id)
            self.read_metadata(mssv, arxiv_id)
            self.read_references(mssv, arxiv_id)
            return manifest
        metadata = dict(
            contract_version=2, arxiv_id=arxiv_id, mssv=mssv, status="not_attempted",
            title=None, authors=None, submission_date=None, revised_dates=None, venue=None,
            version_status="needs_verification", versions=[], missing_fields=[],
            provenance=dict(arxiv=None, semantic_scholar=None, version_history=None),
        )
        references = dict(
            contract_version=2, arxiv_id=arxiv_id, status="not_attempted",
            provenance=dict(provider="semantic_scholar", query_identifier=None,
                            s2_paper_id=None, retrieved_at=None),
            coverage=dict(complete=False, continuation=None, reported_total=None, retrieved_count=0),
            items=[],
        )
        manifest = dict(
            contract_version=2, arxiv_id=arxiv_id, mssv=mssv, run_id=self.run_id,
            status="not_attempted", discovery_status="not_attempted",
            version_status="needs_verification", metadata_status="not_attempted",
            references_status="not_attempted", versions={}, updated_at=_now(),
        )
        paper.mkdir(parents=True, exist_ok=True)
        for name, value in [("metadata.json", metadata), ("references.json", references),
                            ("manifest.json", manifest)]:
            _atomic_json(self._path(mssv, arxiv_id, name), value)
        return manifest

    def _pending(self, arxiv_id, name):
        return dict(
            versioned_id=arxiv_id + name, status="pending", source_status=None,
            http_status=None, content_type=None, source_url=SOURCE_URL + arxiv_id + name,
            archive_bytes=None, archive_sha256=None, files=[], file_checksums={},
            kept_bytes=None, extracted_bytes=None, removed_files=None, attempts=0,
            run_id=None, completed_at=None, error=None,
        )

    def _sync(self, manifest, metadata, references):
        arxiv_id = manifest["arxiv_id"]
        names = self._versions(metadata, arxiv_id)
        _require(set(manifest["versions"]) <= set(names), "Discovery cannot drop known versions")
        for name in names:
            manifest["versions"].setdefault(name, self._pending(arxiv_id, name))
        manifest["version_status"] = metadata["version_status"]
        arxiv = metadata["provenance"]["arxiv"]
        if arxiv is not None:
            _keys(arxiv, ["snapshot"])
            manifest["discovery_status"] = arxiv["snapshot"]["status"]
        manifest["metadata_status"] = metadata["status"]
        manifest["references_status"] = references["status"]

    def _commit(self, manifest):
        mssv, arxiv_id = manifest["mssv"], manifest["arxiv_id"]
        metadata = self.read_metadata(mssv, arxiv_id)
        references = self.read_references(mssv, arxiv_id)
        self._sync(manifest, metadata, references)
        states = [x["status"] for x in manifest["versions"].values()]
        stages = [metadata["status"], references["status"]]
        if manifest["discovery_status"] == "not_found":
            status = "not_found"
        elif "in_progress" in states + stages:
            status = "in_progress"
        elif "interrupted" in states + stages:
            status = "interrupted"
        elif (manifest["discovery_status"] == "found"
              and manifest["version_status"] == "resolved" and states
              and all(x in TERMINAL for x in states)
              and stages == ["complete", "complete"]):
            for name, record in manifest["versions"].items():
                self._verify_source(mssv, arxiv_id, name, record)
            if "withdrawn_version" in states:
                status = "complete_with_withdrawn"
            elif "no_tex_source" in states:
                status = "complete_with_no_tex"
            else:
                status = "complete"
        elif (manifest["discovery_status"] == "found" or any(x in TERMINAL for x in states)
              or any(x in {"partial", "complete"} for x in stages)):
            status = "partial"
        elif any(x not in {"not_attempted", "pending"} for x in states + stages):
            status = "failed"
        else:
            status = "not_attempted"
        manifest["status"] = status
        if self.run_id is not None:
            manifest["run_id"] = self.run_id
        self._manifest(manifest, mssv, arxiv_id)
        path = self._path(mssv, arxiv_id, "manifest.json")
        if path.exists() and self._read(path) == manifest:
            return manifest
        manifest["updated_at"] = _now()
        _atomic_json(path, manifest)
        return manifest

    def save_metadata(self, mssv, arxiv_id, value):
        value = _clone(value)
        self._metadata(value, mssv, arxiv_id)
        manifest = self.read_manifest(mssv, arxiv_id)
        old = self.read_metadata(mssv, arxiv_id)
        _require(value["version_status"] == old["version_status"]
                 and self._versions(value, arxiv_id) == self._versions(old, arxiv_id),
                 "Use record_discovery to change version coverage")
        _require(old["status"] != "complete" or value == old,
                 "Do not overwrite complete metadata")
        _require(all(old[field] is None or value[field] is not None for field in
                     ("title", "authors", "submission_date", "revised_dates", "venue")),
                 "Do not erase previously known metadata with unknown values")
        _require(all(previous["date"] is None or current["date"] is not None
                     for previous, current in zip(old["versions"], value["versions"])),
                 "Do not erase known version dates")
        _atomic_json(self._path(mssv, arxiv_id, "metadata.json"), value)
        return self._commit(manifest)

    def save_references(self, mssv, arxiv_id, value):
        value = _clone(value)
        self._references(value, mssv, arxiv_id)
        manifest = self.read_manifest(mssv, arxiv_id)
        old = self.read_references(mssv, arxiv_id)
        _require(value["items"][:len(old["items"])] == old["items"],
                 "A reference snapshot must preserve every previously saved item")
        _require(old["status"] != "complete" or value == old,
                 "Do not overwrite complete references")
        _atomic_json(self._path(mssv, arxiv_id, "references.json"), value)
        return self._commit(manifest)

    def record_discovery(self, result):
        result = _clone(result)
        _keys(result, ["mssv", "arxiv_id", "status"])
        mssv, arxiv_id = result["mssv"], result["arxiv_id"]
        manifest = self.initialize_paper(mssv, arxiv_id)
        metadata = self.read_metadata(mssv, arxiv_id)
        provenance = dict(query_identifier=arxiv_id, retrieved_at=_now(), snapshot=result)
        if manifest["discovery_status"] == "found" and result["status"] != "found":
            raise StorageError("Do not replace confirmed discovery with an error/absence")
        if result["status"] == "found":
            _keys(result, ["version_status", "version_ids", "latest_version_id",
                           "title", "authors", "published", "updated", "missing_fields"])
            _state(result["version_status"], {"resolved", "needs_verification"},
                   "discovery.version_status")
            ids = result["version_ids"]
            _require(_strings(ids), "Invalid discovered IDs")
            names = [f"v{n}" for n in range(1, len(ids) + 1)]
            _require(ids == [arxiv_id + name for name in names], "Invalid discovered versions")
            _require((result["version_status"] == "resolved") == bool(ids),
                     "Discovery coverage is unresolved")
            _require(not ids or result["latest_version_id"] == ids[-1], "Wrong latest version")
            _require(set(manifest["versions"]) <= set(names), "Cannot drop discovered versions")
            _require(metadata["status"] != "complete", "Do not overwrite complete metadata")
            previous = {item["version"]: item for item in metadata["versions"]}
            revisions = metadata["revised_dates"]
            dates_known = revisions is not None and len(revisions) == len(ids) - 1
            versions = []
            for name in names:
                item = dict(version=name, versioned_id=arxiv_id + name,
                            date=None, date_status="unknown")
                if name in previous:
                    item.update(previous[name])
                if name == "v1" and _valid_date(result["published"]):
                    item.update(date=result["published"], date_status="verified")
                versions.append(item)
            previous_source = metadata["provenance"]["arxiv"]
            field_sources = {}
            for field, target in (("title", "title"), ("authors", "authors"),
                                  ("published", "submission_date")):
                supplied = _valid_date(result[field]) if field == "published" else bool(result[field])
                if supplied:
                    field_sources[field] = dict(provenance)
                elif previous_source is not None and metadata[target]:
                    # Old v2 checkpoints may have only the single arXiv snapshot.
                    source = previous_source.get("field_sources", {}).get(field, previous_source)
                    if source["snapshot"].get(field) == metadata[target]:
                        field_sources[field] = {
                            key: source[key] for key in ("query_identifier", "retrieved_at", "snapshot")
                        }
            provenance["field_sources"] = field_sources
            metadata.update(
                status="partial", title=result["title"] or metadata["title"],
                authors=result["authors"] or metadata["authors"],
                submission_date=(result["published"] if _valid_date(result["published"])
                                 else metadata["submission_date"]),
                revised_dates=[] if len(ids) == 1 else revisions if dates_known else None,
                version_status=result["version_status"],
                missing_fields=list(result["missing_fields"]),
                versions=versions,
            )
        elif result["status"] == "not_found":
            metadata.update(status="not_found", version_status="not_applicable")
        else:
            _state(result["status"], SOURCE_ERRORS | {"api_error", "invalid_xml"},
                   "discovery.status")
            metadata["status"] = "failed"
        metadata["provenance"]["arxiv"] = provenance
        self._metadata(metadata, mssv, arxiv_id)
        _atomic_json(self._path(mssv, arxiv_id, "metadata.json"), metadata)
        return self._commit(manifest)

    def refresh_discovery(self, result):
        """Append newly confirmed arXiv versions without replacing source or S2.

        Metadata is atomically written before the manifest. The existing
        resume reconciliation recovers an interruption between those writes.
        """
        result = _clone(result)
        _keys(result, ["mssv", "arxiv_id", "status", "http_status", "version_status", "version_ids", "latest_version_id"])
        mssv, arxiv_id = result["mssv"], result["arxiv_id"]
        _require(result["status"] == "found" and result["http_status"] == 200
                 and result["version_status"] == "resolved", "Unverified refresh")
        manifest = self.read_manifest(mssv, arxiv_id)
        metadata = self.read_metadata(mssv, arxiv_id)
        references = self.read_references(mssv, arxiv_id)
        _require(manifest["discovery_status"] == "found", "Refresh requires found discovery")
        self._sync(_clone(manifest), metadata, references)
        old_names = self._versions(metadata, arxiv_id)
        def dates_agree(old, fresh):
            if not old or not fresh:
                return True
            try:
                left = datetime.fromisoformat(old.replace("Z", "+00:00"))
                right = datetime.fromisoformat(fresh.replace("Z", "+00:00"))
                if len(old) == 10 or len(fresh) == 10:
                    return left.date() == right.date()
                return left.tzinfo is not None and right.tzinfo is not None and left == right
            except (ValueError, TypeError, AttributeError):
                return False
        _require(dates_agree(metadata["submission_date"], result.get("published")),
                 "Fresh discovery conflicts with known submission date")
        ids = result["version_ids"]
        _require(_strings(ids) and ids == [arxiv_id + f"v{i}" for i in range(1, len(ids) + 1)]
                 and bool(ids) and result["latest_version_id"] == ids[-1], "Invalid refresh versions")
        _require(len(ids) >= len(old_names), "Refresh cannot drop known versions")
        if len(ids) == len(old_names):
            previous_arxiv = metadata["provenance"]["arxiv"] or {}
            previous_history = metadata["provenance"]["version_history"]
            prior_snapshot = previous_arxiv.get("snapshot")
            metadata["provenance"]["arxiv"] = dict(
                query_identifier=arxiv_id,
                retrieved_at=_now(),
                snapshot=result,
                field_sources=previous_arxiv.get("field_sources", {}),
                previous_discovery=(dict(
                    query_identifier=previous_arxiv.get("query_identifier"),
                    retrieved_at=previous_arxiv.get("retrieved_at"),
                    snapshot=prior_snapshot,
                ) if isinstance(prior_snapshot, dict) else None),
                previous_version_history=previous_history,
            )
            self._metadata(metadata, mssv, arxiv_id)
            _atomic_json(self._path(mssv, arxiv_id, "metadata.json"), metadata)
            return manifest
        previous_arxiv = metadata["provenance"]["arxiv"]
        previous_history = metadata["provenance"]["version_history"]
        for i in range(len(old_names) + 1, len(ids) + 1):
            metadata["versions"].append(dict(version=f"v{i}", versioned_id=arxiv_id + f"v{i}",
                                             date=None, date_status="unknown"))
        metadata["status"] = "partial"
        metadata["revised_dates"] = None
        if "version_history" not in metadata["missing_fields"]:
            metadata["missing_fields"].append("version_history")
        metadata["provenance"]["version_history"] = None
        metadata["provenance"]["arxiv"] = dict(
            query_identifier=arxiv_id, retrieved_at=_now(), snapshot=result,
            field_sources=(previous_arxiv or {}).get("field_sources", {}),
            previous_discovery=previous_arxiv, previous_version_history=previous_history)
        self._metadata(metadata, mssv, arxiv_id)
        _atomic_json(self._path(mssv, arxiv_id, "metadata.json"), metadata)
        return self._commit(manifest)

    def version_destination(self, mssv, arxiv_id, version):
        _version(version)
        manifest = self.read_manifest(mssv, arxiv_id)
        _require(version in manifest["versions"], "Version was not discovered")
        return self._path(mssv, arxiv_id, version)

    def _snapshot(self, destination):
        files, checksums, total = [], {}, 0
        seen = set()
        try:
            d_lstat = os.lstat(destination)
            if stat.S_ISLNK(d_lstat.st_mode):
                raise IntegrityError("Version destination must not be a symbolic link")
            if not stat.S_ISDIR(d_lstat.st_mode):
                raise IntegrityError("Version destination must be a directory")
        except FileNotFoundError:
            return files, checksums, total
        except OSError as exc:
            raise IntegrityError(f"Cannot inspect version destination: {destination}") from exc

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC

        pending = [destination]
        while pending:
            parent = pending.pop()
            for path in sorted(parent.iterdir()):
                self._safe(path)
                relative = path.relative_to(destination).as_posix()
                _relative(relative)
                _require(relative.casefold() not in seen, "Case-colliding source paths")
                seen.add(relative.casefold())

                try:
                    lstat_res = os.lstat(path)
                except OSError as exc:
                    raise IntegrityError(f"Cannot inspect source path: {path}") from exc

                _require(not stat.S_ISLNK(lstat_res.st_mode), "Source entry must not be a symbolic link")
                if stat.S_ISDIR(lstat_res.st_mode):
                    pending.append(path)
                    continue

                _require(stat.S_ISREG(lstat_res.st_mode) and lstat_res.st_nlink == 1,
                         "Source must be a regular, unlinked file")
                _require(path.suffix.lower() in {".tex", ".bib", ".bbl"}, "Unexpected source file type")

                try:
                    fd = os.open(path, flags)
                except OSError as exc:
                    raise IntegrityError(f"Cannot open source file: {path}") from exc

                try:
                    fstat_info = os.fstat(fd)
                    _require(
                        stat.S_ISREG(fstat_info.st_mode)
                        and fstat_info.st_nlink == 1
                        and fstat_info.st_ino == lstat_res.st_ino,
                        "Source file descriptor validation failed",
                    )
                    digest = hashlib.sha256()
                    with open(fd, "rb", closefd=False) as stream:
                        for block in iter(lambda: stream.read(65536), b""):
                            digest.update(block)
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

                files.append(relative)
                checksums[relative] = digest.hexdigest()
                total += fstat_info.st_size
        return sorted(files), checksums, total

    def _verify_source(self, mssv, arxiv_id, version, record):
        try:
            _require(record["status"] in TERMINAL and record["source_status"] == record["status"],
                     "Source completion is not confirmed")
            destination = self._path(mssv, arxiv_id, _version(version))
            files, checksums, total = self._snapshot(destination)
            _require(files == record["files"] and checksums == record["file_checksums"]
                     and total == record["kept_bytes"], "Source files/checksums do not match")
            has_tex = any(PurePosixPath(x).suffix.lower() == ".tex" for x in files)
            _require(has_tex == (record["status"] == "success"), "Source status contradicts TeX files")
            _require(bool(files) or not destination.exists(), "Unexpected empty version directory")
            _require(record["completed_at"] is not None, "Missing source commit timestamp")
        except (OSError, StorageError) as exc:
            raise IntegrityError(str(exc)) from exc

    def should_skip_version(self, mssv, arxiv_id, version):
        manifest = self.read_manifest(mssv, arxiv_id)
        _require(_version(version) in manifest["versions"], "Unknown version")
        record = manifest["versions"][version]
        if record["status"] not in TERMINAL:
            return False
        try:
            self._verify_source(mssv, arxiv_id, version, record)
        except IntegrityError:
            return False
        return True

    def begin_version(self, mssv, arxiv_id, version):
        manifest = self.read_manifest(mssv, arxiv_id)
        destination = self.version_destination(mssv, arxiv_id, version)
        record = manifest["versions"][version]
        if record["status"] in TERMINAL:
            self._verify_source(mssv, arxiv_id, version, record)
            return None
        _require(record["status"] != "in_progress", "Call resume after an interrupted session")
        if destination.exists():
            raise IntegrityError("Existing source has no valid completion record; audit required")
        _require(record["status"] in RETRYABLE, "This source error requires audit before retry")
        record.update(status="in_progress", run_id=self.run_id, error=None)
        self._commit(manifest)
        return destination

    def record_source_result(self, result):
        result = _clone(result)
        _keys(result, ["mssv", "arxiv_id", "versioned_id", "status", "files", "error"])
        mssv, arxiv_id = result["mssv"], result["arxiv_id"]
        self.paper_path(mssv, arxiv_id)
        versioned = result["versioned_id"]
        _require(isinstance(versioned, str) and versioned.startswith(arxiv_id), "Wrong source ID")
        name = _version(versioned[len(arxiv_id):])
        manifest = self.read_manifest(mssv, arxiv_id)
        _require(name in manifest["versions"], "Unknown source version")
        record = manifest["versions"][name]
        if record["status"] in TERMINAL:
            self._verify_source(mssv, arxiv_id, name, record)
            return manifest
        _require(record["status"] == "in_progress", "begin_version must be committed first")
        status = result["status"]
        _state(status, TERMINAL | SOURCE_ERRORS, "source.status")
        _require(_strings(result["files"]), "Invalid downloader files")
        for filename in result["files"]:
            _relative(filename)
        attempts, size = result.get("attempts", 0), result.get("downloaded_bytes")
        _require(type(attempts) is int and attempts >= 0, "Invalid source attempts")
        _require(size is None or type(size) is int and size >= 0, "Invalid downloaded size")
        for field in ("extracted_bytes", "removed_files"):
            value = result.get(field)
            _require(value is None or type(value) is int and value >= 0, f"Invalid {field}")
        if status == "withdrawn_version":
            from src.withdrawal_evidence import confirmed_withdrawal
            evidence = result.get("withdrawal_evidence")
            detail = evidence.get("details") if isinstance(evidence, dict) else None
            _require(isinstance(evidence, dict)
                     and evidence.get("classification") == "withdrawn_version"
                     and evidence.get("original_http_status") == 404
                     and confirmed_withdrawal(detail, arxiv_id, name),
                     "withdrawn_version requires exact version-scoped arXiv evidence")
        record.update(
            status=status, source_status=status, http_status=result.get("http_status"),
            content_type=result.get("content_type"), attempts=attempts,
            archive_bytes=size if size else None, extracted_bytes=result.get("extracted_bytes"),
            removed_files=result.get("removed_files"), run_id=self.run_id,
            error=None if result["error"] is None else dict(
                stage="source", type=status, message=str(result["error"])),
        )
        if "withdrawal_evidence" in result:
            record["withdrawal_evidence"] = result["withdrawal_evidence"]
        if status in TERMINAL:
            try:
                files, hashes, total = self._snapshot(self._path(mssv, arxiv_id, name))
                _require(len(result["files"]) == len(set(result["files"]))
                         and files == sorted(result["files"]), "Downloader file list does not match")
                record.update(files=files, file_checksums=hashes,
                              kept_bytes=total, completed_at=_now())
                self._verify_source(mssv, arxiv_id, name, record)
            except (OSError, StorageError) as exc:
                record.update(status="validation_failed", completed_at=None, error=dict(
                    stage="storage", type="validation_failed", message=str(exc)))
                self._commit(manifest)
                raise IntegrityError(str(exc)) from exc
        return self._commit(manifest)

    def confirm_withdrawn_version(self, mssv, arxiv_id, version, evidence):
        """Promote a source 404 only after exact official-page evidence validates."""
        version = _version(version)
        manifest = self.read_manifest(mssv, arxiv_id)
        _require(version in manifest["versions"], "Unknown source version")
        record = manifest["versions"][version]
        _require(record["status"] in {"http_error", "http_error_404_unverified"}
                 and record["http_status"] == 404, "Only an unresolved source 404 can be reconciled")
        destination = self._path(mssv, arxiv_id, version)
        _require(not destination.exists(), "Withdrawn version has an existing source directory")
        from src.withdrawal_classifier import classify_version_outcome

        classification = classify_version_outcome(
            arxiv_id, version, http_status=404,
            content_type=record.get("content_type"),
            discovery_metadata=evidence, error_message=(record.get("error") or {}).get("message"),
            has_tex_files=False, file_list=[],
        )
        _require(classification["classification"] == "withdrawn_version",
                 "Official page evidence does not confirm withdrawal")
        record.update(
            status="withdrawn_version", source_status="withdrawn_version", error=None,
            withdrawal_evidence=classification, completed_at=_now(), files=[],
            file_checksums={}, kept_bytes=0,
        )
        return self._commit(manifest)

    def resume(self, mssv, arxiv_id):
        """Return skip/retry/blocked version sets; preserve all existing source."""
        manifest = self.read_manifest(mssv, arxiv_id)
        # Validate every mandatory JSON before changing any interrupted stage.
        stages = {
            "metadata": self.read_metadata(mssv, arxiv_id),
            "references": self.read_references(mssv, arxiv_id),
        }
        # Preflight cross-file coverage before persisting any recovery changes.
        # Newer metadata may add versions after an interrupted manifest commit.
        self._sync(manifest, stages["metadata"], stages["references"])
        for name, value in stages.items():
            if value["status"] == "in_progress":
                value["status"] = "interrupted"
                _atomic_json(self._path(mssv, arxiv_id, name + ".json"), value)
        self._sync(manifest, self.read_metadata(mssv, arxiv_id),
                   self.read_references(mssv, arxiv_id))
        plan = dict(skip=[], retry=[], verify_withdrawal=[], blocked={})
        for name, record in manifest["versions"].items():
            if record["status"] == "in_progress":
                record.update(status="interrupted", error=dict(
                    stage="source", type="interrupted", message="Previous session did not commit a result"))
            if record["status"] == "withdrawn_version":
                from src.withdrawal_evidence import confirmed_withdrawal

                evidence = record.get("withdrawal_evidence")
                details = evidence.get("details") if isinstance(evidence, dict) else None
                evidence_is_version_scoped = (
                    isinstance(evidence, dict)
                    and evidence.get("classification") == "withdrawn_version"
                    and evidence.get("original_http_status") == 404
                    and confirmed_withdrawal(details, arxiv_id, name)
                )
                if not evidence_is_version_scoped:
                    destination = self._path(mssv, arxiv_id, name)
                    safely_recoverable = (
                        record.get("http_status") == 404
                        and not destination.exists()
                        and not destination.is_symlink()
                        and not record.get("files")
                        and not record.get("file_checksums")
                        and record.get("kept_bytes") in (None, 0)
                    )
                    if safely_recoverable:
                        history = record.get("withdrawal_evidence_history", [])
                        if not isinstance(history, list):
                            history = [{"unparsed_previous_history": history}]
                        history.append({
                            "evidence": evidence,
                            "reviewed_at": _now(),
                            "review_result": "rejected_missing_version_scoped_proof",
                        })
                        record["withdrawal_evidence_history"] = history
                        record.pop("withdrawal_evidence", None)
                        record.update(
                            status="http_error_404_unverified",
                            source_status="http_error_404_unverified",
                            error=dict(
                                stage="source",
                                type="http_error_404_unverified",
                                message=("Previous withdrawn_version claim lacked valid exact-version evidence; "
                                         "prior evidence was preserved and the official version page must be checked"),
                            ),
                            completed_at=None,
                            files=[],
                            file_checksums={},
                            kept_bytes=None,
                            archive_bytes=None,
                            archive_sha256=None,
                            extracted_bytes=None,
                            removed_files=None,
                        )
                        plan["verify_withdrawal"].append(name)
                        continue
                    record.update(status="validation_failed", source_status="validation_failed", error=dict(
                        stage="storage", type="validation_failed",
                        message="Withdrawn-version evidence is invalid and its source state is not safe to recover automatically"))
            if record["status"] in TERMINAL:
                try:
                    self._verify_source(mssv, arxiv_id, name, record)
                except IntegrityError as exc:
                    record.update(status="validation_failed", error=dict(
                        stage="storage", type="validation_failed", message=str(exc)))
                else:
                    plan["skip"].append(name)
                    continue
            destination = self._path(mssv, arxiv_id, name)
            if (
                record["status"] in {"http_error", "http_error_404_unverified"}
                and record.get("http_status") == 404
                and not destination.exists()
            ):
                plan["verify_withdrawal"].append(name)
                continue

            if record["status"] in RETRYABLE and not destination.exists():
                plan["retry"].append(name)
            else:
                plan["blocked"][name] = (
                    "output_exists" if destination.exists() and record["status"] in RETRYABLE
                    else record["status"]
                )
        manifest = self._commit(manifest)
        return {**plan, "manifest": manifest}

    def audit_paper(self, mssv, arxiv_id):
        """Perform comprehensive storage contract and data integrity audit on a paper."""
        from src.storage_integrity import audit_paper_integrity
        return audit_paper_integrity(self, mssv, arxiv_id)

    def audit_cohort(self, pairs, *, cohort_number=None):
        """Perform comprehensive storage contract and data integrity audit on a cohort or paper list."""
        from src.storage_integrity import audit_cohort_integrity
        return audit_cohort_integrity(self, pairs, cohort_number=cohort_number)

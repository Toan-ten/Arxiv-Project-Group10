import gzip
import re
import stat
import tarfile
import tempfile
import zipfile
import zlib
from http.client import IncompleteRead
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from src import arxiv_client
from src.arxiv_discovery import ID_PATTERN


SOURCE_URL = "https://export.arxiv.org/src/"
CHUNK_SIZE = 64 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 10000
KEPT_SUFFIXES = {".tex", ".bib", ".bbl"}


class SourceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _copy_limited(source, target, limit):
    total = 0
    while chunk := source.read(min(CHUNK_SIZE, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            raise SourceError("size_limit", "Source exceeds the size limit")
        if target is not None:
            try:
                remaining = memoryview(chunk)
                while remaining:
                    written = target.write(remaining)
                    if not written:
                        raise OSError("Could not write source bytes")
                    remaining = remaining[written:]
            except OSError as exc:
                raise SourceError("storage_error", str(exc)) from exc
    return total


def _safe_path(name, is_directory, seen):
    if (not name or len(name) > 4096 or "\\" in name or any(c in name for c in '<>:"|?*')
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
        raise SourceError("unsafe_archive", f"Unsafe path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or (not path.parts and not is_directory):
        raise SourceError("unsafe_archive", f"Unsafe path: {name!r}")
    is_kept = (not is_directory) and (path.suffix.lower() in KEPT_SUFFIXES)
    for index, part in enumerate(path.parts):
        if (len(part) > 255 or part.endswith((".", " "))
                or re.fullmatch(r"CON(?:IN\$|OUT\$)?|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]",
                                part.split(".")[0].rstrip(" "), re.IGNORECASE)):
            raise SourceError("unsafe_archive", f"Unsafe path: {name!r}")
        if is_kept:
            prefix = PurePosixPath(*path.parts[:index + 1]).as_posix()
            directory = index < len(path.parts) - 1 or is_directory
            previous = seen.get(prefix.casefold())
            if previous is not None and (previous != (prefix, directory) or not directory):
                raise SourceError("unsafe_archive", f"Conflicting path: {name!r}")
            seen[prefix.casefold()] = (prefix, directory)
    return path


def _check_size(count, total):
    if count > MAX_MEMBERS or total > MAX_UNPACKED_BYTES:
        raise SourceError("size_limit", "Archive exceeds the member or unpacked-size limit")


def _save_member(source, destination, size):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:
        actual = _copy_limited(source, target, size)
    if actual != size:
        raise SourceError("archive_error", "Truncated archive member")


def _extract_tar(archive, payload_size, directory, metrics):
    files = []
    seen = {}
    total = 0
    for count, member in enumerate(archive, 1):
        _check_size(count, total)
        if not member.isdir() and (not member.isfile() or member.sparse is not None):
            raise SourceError("unsafe_archive", f"Links and special files are refused: {member.name}")
        path = _safe_path(member.name, member.isdir(), seen)
        if member.isdir():
            if member.size != 0:
                raise SourceError("archive_error", "Tar directory has unexpected data")
            continue
        if member.size < 0 or member.offset_data + member.size > payload_size:
            raise SourceError("archive_error", "Truncated or invalid tar member")
        total += member.size
        metrics["extracted_bytes"] += member.size
        _check_size(count, total)
        if path.suffix.lower() in KEPT_SUFFIXES:
            with archive.extractfile(member) as source:
                _save_member(source, directory / path, member.size)
            files.append(path.as_posix())
        else:
            metrics["removed_files"] += 1
    return files


def _extract_zip(payload, directory, metrics):
    files = []
    seen = {}
    total = 0
    with zipfile.ZipFile(payload) as archive:
        _check_size(len(archive.infolist()), 0)
        for member in archive.infolist():
            if member.flag_bits & 1:
                raise SourceError("unsupported_source", "Encrypted ZIP members are not supported")
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (mode == stat.S_IFDIR and not member.is_dir())):
                raise SourceError("unsafe_archive", f"Links and special files are refused: {member.filename}")
            path = _safe_path(member.orig_filename, member.is_dir(), seen)
            total += member.file_size
            if not member.is_dir():
                metrics["extracted_bytes"] += member.file_size
            _check_size(0, total)
            if member.is_dir():
                continue
            with archive.open(member) as source:
                if path.suffix.lower() in KEPT_SUFFIXES:
                    _save_member(source, directory / path, member.file_size)
                    files.append(path.as_posix())
                else:
                    metrics["removed_files"] += 1
                    if _copy_limited(source, None, member.file_size) != member.file_size:
                        raise SourceError("archive_error", "Truncated ZIP member")
    return files


def _extract_payload(payload, directory, metrics):
    with payload.open("rb") as source:
        head = source.read(CHUNK_SIZE)
    if not head:
        raise SourceError("invalid_response", "Empty source response")
    if head.startswith(b"PK"):
        return _extract_zip(payload, directory, metrics)
    try:
        archive = tarfile.open(payload, mode="r:")
    except tarfile.ReadError as exc:
        if head[257:262] == b"ustar":
            raise SourceError("archive_error", str(exc)) from exc
    else:
        with archive:
            return _extract_tar(archive, payload.stat().st_size, directory, metrics)

    stripped = head.lstrip()
    if stripped.startswith((b"%PDF-", b"%!PS-Adobe-")):
        metrics.update(extracted_bytes=payload.stat().st_size, removed_files=1)
        return []
    if stripped.lower().startswith((b"<!doctype html", b"<html", b"<?xml", b"<error")):
        raise SourceError("invalid_response", "Received HTML/XML instead of source")
    if b"\x00" not in head and re.search(
        rb"\\(?:documentclass\b|documentstyle\b|begin\s*\{\s*document\s*\}|bye\b)", head
    ):
        metrics.update(extracted_bytes=payload.stat().st_size, removed_files=0)
        with payload.open("rb") as source:
            _save_member(source, directory / "main.tex", payload.stat().st_size)
        return ["main.tex"]
    raise SourceError("unsupported_source", "Unrecognized source format; TeX availability is unknown")


def extract_source(
    source_path: str | Path,
    destination: str | Path,
    *,
    bbl_artifact_dir: str | Path | None = None,
    mssv: str | None = None,
    versioned_id: str | None = None,
) -> dict:
    destination = Path(destination).absolute()
    result = {"status": None, "files": [], "error": None}
    if destination.exists() or destination.is_symlink():
        return {**result, "status": "output_exists", "error": "Destination already exists"}
    try:
        payload = Path(source_path)
        if payload.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise SourceError("size_limit", "Source exceeds the download-size limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".arxiv-source-", dir=destination.parent) as work:
            directory = Path(work) / "files"
            directory.mkdir()
            with payload.open("rb") as source:
                compressed = source.read(2) == b"\x1f\x8b"
            if compressed:
                unpacked = Path(work) / "unpacked"
                with gzip.open(payload, "rb") as source, unpacked.open("wb") as target:
                    _copy_limited(source, target, MAX_UNPACKED_BYTES)
                payload = unpacked
            metrics = {"extracted_bytes": 0, "removed_files": 0}
            files = _extract_payload(payload, directory, metrics)
            bbl_policy = None
            if mssv is not None and versioned_id is not None:
                match = ID_PATTERN.fullmatch(versioned_id)
                if match is None or match.group("version") is None:
                    raise SourceError("invalid_response", "Invalid version identity for BBL filtering")
                from src.bbl_handling import BblConversionError, apply_bbl_source_policy

                try:
                    files, bbl_policy = apply_bbl_source_policy(
                        directory,
                        files,
                        artifact_root=bbl_artifact_dir,
                        mssv=mssv,
                        arxiv_id=match.group("base"),
                        version=f"v{match.group('version')}",
                    )
                except BblConversionError as exc:
                    raise SourceError("storage_error", str(exc)) from exc
            has_tex = any(PurePosixPath(f).suffix.lower() == ".tex" for f in files)
            status = "success" if has_tex else "no_tex_source"
            if files:
                if destination.exists() or destination.is_symlink():
                    raise SourceError("output_exists", "Destination already exists")
                directory.rename(destination)
            return {
                **result, **metrics, "status": status, "files": sorted(files),
                **({"bbl_policy": bbl_policy} if bbl_policy is not None else {}),
            }
    except SourceError as exc:
        return {**result, "status": exc.status, "error": str(exc)}
    except (tarfile.TarError, zipfile.BadZipFile, gzip.BadGzipFile,
            EOFError, zlib.error) as exc:
        return {**result, "status": "archive_error", "error": str(exc)}
    except NotImplementedError as exc:
        return {**result, "status": "unsupported_source", "error": str(exc)}
    except OSError as exc:
        return {**result, "status": "storage_error", "error": str(exc)}


def _valid_content_range(header_val: str | None, expected_start: int, total_expected: int) -> tuple[bool, int]:
    """Validate Content-Range: bytes <start>-<end>/<total> against expected offset and total."""
    if not header_val:
        return False, 0
    m = re.fullmatch(r"bytes\s+([0-9]+)-([0-9]+)/([0-9]+)", header_val.strip(), re.IGNORECASE)
    if not m:
        return False, 0
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if start != expected_start or total != total_expected or start > end or end >= total_expected:
        return False, 0
    return True, (end - start + 1)


def download_source(
    mssv: str,
    versioned_id: str,
    destination: str | Path,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    user_agent: str = arxiv_client.USER_AGENT,
    bbl_artifact_dir: str | Path | None = None,
) -> dict:
    if not isinstance(mssv, str) or not re.fullmatch(r"[0-9]+", mssv):
        raise ValueError("mssv must be a non-empty string of digits")
    match = ID_PATTERN.fullmatch(versioned_id) if isinstance(versioned_id, str) else None
    if match is None or match.group("version") is None:
        raise ValueError("versioned_id must include a version suffix, such as v1")
    result = {
        "mssv": mssv, "arxiv_id": match.group("base"), "versioned_id": versioned_id,
        "status": None, "http_status": None, "attempts": 0,
        "content_type": None, "downloaded_bytes": 0, "files": [], "error": None,
    }
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        return {**result, "status": "output_exists", "error": "Destination already exists"}
    try:
        with tempfile.TemporaryDirectory(prefix="arxiv-download-") as work:
            payload = Path(work) / "source"
            url = SOURCE_URL + quote(versioned_id, safe="/")

            def read_source(response):
                content_type = response.headers.get("Content-Type", "")
                details = {"content_type": content_type}
                if content_type.split(";", 1)[0].strip().lower() in {
                    "text/html", "application/xhtml+xml", "application/atom+xml",
                    "application/xml", "text/xml",
                }:
                    return {**details, "status": "invalid_response", "error": "Received HTML/XML instead of source"}
                length = response.headers.get("Content-Length")
                if length is not None and not re.fullmatch(r"[0-9]+", length):
                    return {**details, "status": "invalid_response", "error": "Invalid Content-Length"}
                if length is not None and int(length) > MAX_DOWNLOAD_BYTES:
                    return {**details, "status": "size_limit", "error": "Source exceeds the download-size limit"}
                initial_etag = response.headers.get("ETag")
                if initial_etag is not None:
                    initial_etag = initial_etag.strip()
                extra_attempts = 0
                try:
                    target = payload.open("wb", buffering=0)
                except OSError as exc:
                    return {**details, "status": "storage_error", "error": str(exc)}
                try:
                    with target:
                        size = _copy_limited(response, target, MAX_DOWNLOAD_BYTES)
                    if (
                        length is not None
                        and 0 < size < int(length)
                        and response.headers.get("Accept-Ranges", "").strip().lower() == "bytes"
                        and (initial_etag is None or not initial_etag.startswith("W/"))
                    ):
                        total_expected = int(length)
                        response.close()
                        with payload.open("ab", buffering=0) as append_target:
                            while size < total_expected and extra_attempts < 8:
                                extra_attempts += 1
                                expected_start = size

                                def _read_chunk(range_resp, start_off=expected_start):
                                    if getattr(range_resp, "status", None) != 206:
                                        return {"status": "range_mismatch", "chunk_bytes": 0}
                                    resp_etag = range_resp.headers.get("ETag")
                                    if initial_etag is not None and resp_etag is not None:
                                        if resp_etag.strip() != initial_etag:
                                            return {"status": "etag_mismatch", "chunk_bytes": 0}
                                    ok, expected_chunk = _valid_content_range(
                                        range_resp.headers.get("Content-Range"),
                                        start_off,
                                        total_expected,
                                    )
                                    if not ok:
                                        return {"status": "range_mismatch", "chunk_bytes": 0}
                                    chunk_read = _copy_limited(
                                        range_resp, append_target, MAX_DOWNLOAD_BYTES - start_off
                                    )
                                    if chunk_read <= 0 or chunk_read > expected_chunk:
                                        return {"status": "empty_or_overflow_range", "chunk_bytes": chunk_read}
                                    return {"status": "chunk_ok", "chunk_bytes": chunk_read}

                                range_res = arxiv_client._request_arxiv_range(
                                    url,
                                    start_byte=expected_start,
                                    total_expected=total_expected,
                                    timeout=timeout,
                                    user_agent=user_agent,
                                    attempt_number=1 + extra_attempts,
                                    read_chunk=_read_chunk,
                                    max_attempts=max_attempts,
                                )
                                extra_attempts += range_res.get("_extra_range_attempts", 0)
                                if range_res["status"] != "chunk_ok":
                                    if range_res.get("http_status") in (403, 429) or range_res["status"] in ("http_error", "rate_limited"):
                                        return {
                                            "content_type": None,
                                            "status": range_res["status"],
                                            "http_status": range_res["http_status"],
                                            "error": range_res.get("error"),
                                            "_extra_attempts": extra_attempts,
                                        }
                                    break
                                size = append_target.tell()
                                if size <= expected_start:
                                    break
                except SourceError as exc:
                    return {**details, "status": exc.status, "error": str(exc), "_extra_attempts": extra_attempts}
                if length is not None and size < int(length):
                    raise IncompleteRead(b"", int(length) - size)
                if length is not None and size > int(length):
                    return {**details, "status": "invalid_response", "error": "Body exceeds Content-Length", "_extra_attempts": extra_attempts}
                return {**details, "status": "downloaded", "downloaded_bytes": size, "_extra_attempts": extra_attempts}

            response = arxiv_client._request_arxiv(
                url, timeout=timeout,
                max_attempts=max_attempts, user_agent=user_agent,
                accept="application/octet-stream", read_response=read_source,
            )
            result.update(response)
            if result["status"] == "downloaded":
                result.update(extract_source(
                    payload,
                    destination,
                    bbl_artifact_dir=bbl_artifact_dir,
                    mssv=mssv,
                    versioned_id=versioned_id,
                ))
            return result
    except OSError as exc:
        return {**result, "status": "storage_error", "error": str(exc)}

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
KEPT_SUFFIXES = {".tex", ".bib"}


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
    for index, part in enumerate(path.parts):
        if (len(part) > 255 or part.endswith((".", " "))
                or re.fullmatch(r"CON(?:IN\$|OUT\$)?|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]",
                                part.split(".")[0].rstrip(" "), re.IGNORECASE)):
            raise SourceError("unsafe_archive", f"Unsafe path: {name!r}")
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


def _extract_tar(archive, payload_size, directory):
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
        _check_size(count, total)
        if path.suffix.lower() in KEPT_SUFFIXES:
            with archive.extractfile(member) as source:
                _save_member(source, directory / path, member.size)
            files.append(path.as_posix())
    return files


def _extract_zip(payload, directory):
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
            _check_size(0, total)
            if member.is_dir():
                continue
            with archive.open(member) as source:
                if path.suffix.lower() in KEPT_SUFFIXES:
                    _save_member(source, directory / path, member.file_size)
                    files.append(path.as_posix())
                else:
                    if _copy_limited(source, None, member.file_size) != member.file_size:
                        raise SourceError("archive_error", "Truncated ZIP member")
    return files


def _extract_payload(payload, directory):
    with payload.open("rb") as source:
        head = source.read(CHUNK_SIZE)
    if not head:
        raise SourceError("invalid_response", "Empty source response")
    if head.startswith(b"PK"):
        return _extract_zip(payload, directory)
    try:
        archive = tarfile.open(payload, mode="r:")
    except tarfile.ReadError as exc:
        if head[257:262] == b"ustar":
            raise SourceError("archive_error", str(exc)) from exc
    else:
        with archive:
            return _extract_tar(archive, payload.stat().st_size, directory)

    stripped = head.lstrip()
    if stripped.startswith((b"%PDF-", b"%!PS-Adobe-")):
        return []
    if stripped.lower().startswith((b"<!doctype html", b"<html", b"<?xml", b"<error")):
        raise SourceError("invalid_response", "Received HTML/XML instead of source")
    if b"\x00" not in head and re.search(
        rb"\\(?:documentclass\b|documentstyle\b|begin\s*\{\s*document\s*\}|bye\b)", head
    ):
        with payload.open("rb") as source:
            _save_member(source, directory / "main.tex", payload.stat().st_size)
        return ["main.tex"]
    raise SourceError("unsupported_source", "Unrecognized source format; TeX availability is unknown")


def extract_source(source_path: str | Path, destination: str | Path) -> dict:
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
            files = _extract_payload(payload, directory)
            has_tex = any(PurePosixPath(f).suffix.lower() == ".tex" for f in files)
            status = "success" if has_tex else "no_tex_source"
            if files:
                if destination.exists() or destination.is_symlink():
                    raise SourceError("output_exists", "Destination already exists")
                directory.rename(destination)
            return {**result, "status": status, "files": sorted(files)}
    except SourceError as exc:
        return {**result, "status": exc.status, "error": str(exc)}
    except (tarfile.TarError, zipfile.BadZipFile, gzip.BadGzipFile,
            EOFError, zlib.error) as exc:
        return {**result, "status": "archive_error", "error": str(exc)}
    except NotImplementedError as exc:
        return {**result, "status": "unsupported_source", "error": str(exc)}
    except OSError as exc:
        return {**result, "status": "storage_error", "error": str(exc)}


def download_source(
    mssv: str,
    versioned_id: str,
    destination: str | Path,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    user_agent: str = arxiv_client.USER_AGENT,
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
                try:
                    target = payload.open("wb", buffering=0)
                except OSError as exc:
                    return {**details, "status": "storage_error", "error": str(exc)}
                try:
                    with target:
                        size = _copy_limited(response, target, MAX_DOWNLOAD_BYTES)
                except SourceError as exc:
                    return {**details, "status": exc.status, "error": str(exc)}
                if length is not None and size < int(length):
                    raise IncompleteRead(b"", int(length) - size)
                if length is not None and size > int(length):
                    return {**details, "status": "invalid_response", "error": "Body exceeds Content-Length"}
                return {**details, "status": "downloaded", "downloaded_bytes": size}

            response = arxiv_client._request_arxiv(
                SOURCE_URL + quote(versioned_id, safe="/"), timeout=timeout,
                max_attempts=max_attempts, user_agent=user_agent,
                accept="application/octet-stream", read_response=read_source,
            )
            result.update(response)
            if result["status"] == "downloaded":
                result.update(extract_source(payload, destination))
            return result
    except OSError as exc:
        return {**result, "status": "storage_error", "error": str(exc)}

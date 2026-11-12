"""Thử chuyển đổi cục bộ tệp .bbl và lưu bằng chứng xử lý riêng."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from src.storage import _atomic_json, arxiv_id_to_folder_key


_ENTRY = re.compile(r"\\entry\b")
_COMMAND = re.compile(r"\\([A-Za-z@]+)")
_KEY = re.compile(r"[A-Za-z0-9_:.+/-]+\Z")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_BIBITEM_BYTES = re.compile(rb"\\bibitem\b")
_BIBLATEX_ONLY = re.compile(r"\\(?:bib[a-zA-Z]*|mkbib[a-zA-Z]*|blx[a-zA-Z]*)\b")
_MAX_CONVERSION_BYTES = 20 * 1024 * 1024
BBL_POLICY_VERSION = "bbl-exception-v1"

_REASON_DETAILS = {
    "rendered_thebibliography_is_not_reversible_locally": (
        "Tệp chứa danh sách đã định dạng; không thể suy ra chính xác các trường tác giả, tiêu đề và năm bằng phép chuyển đổi cục bộ."
    ),
    "no_supported_biblatex_entries": "Không tìm thấy mục BibLaTeX có cấu trúc được hỗ trợ.",
    "not_utf8": "Tệp không dùng UTF-8; giữ nguyên để tránh làm sai ký tự.",
    "bbl_size_limit": "Tệp vượt giới hạn 20 MiB của bộ chuyển đổi cục bộ.",
    "entry_has_no_recoverable_fields": "Mục không có trường dữ liệu có thể chuyển đổi.",
    "unbalanced_braces": "Nhóm ngoặc không cân bằng; không tạo tệp BibTeX suy đoán.",
    "unrecognized_entry_content": "Mục có nội dung không thuộc cấu trúc trường được hỗ trợ.",
}


class BblConversionError(ValueError):
    """Tệp .bbl không thuộc dạng có thể chuyển đổi chắc chắn tại chỗ."""


def _group(text: str, position: int) -> tuple[str, int]:
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != "{":
        raise BblConversionError("expected_braced_group")
    start = position + 1
    depth = 1
    position += 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:position], position + 1
        position += 1
    raise BblConversionError("unbalanced_braces")


def _strip_tex_comments(text: str) -> str:
    """Bỏ phần chú thích TeX trước khi nhận diện cấu trúc, giữ nguyên xuống dòng."""
    output = []
    in_comment = False
    backslashes = 0
    for char in text:
        if in_comment:
            if char in "\r\n":
                in_comment = False
                backslashes = 0
                output.append(char)
            continue
        if char == "%" and backslashes % 2 == 0:
            in_comment = True
            continue
        output.append(char)
        backslashes = backslashes + 1 if char == "\\" else 0
    return "".join(output)


def _parse_entry(text: str, start: int, end: int) -> tuple[str, str, dict[str, str]]:
    key, cursor = _group(text, start + len(r"\entry"))
    entry_type, cursor = _group(text, cursor)
    _options, cursor = _group(text, cursor)
    if not _KEY.fullmatch(key.strip()):
        raise BblConversionError("unsupported_entry_key")
    if not _IDENTIFIER.fullmatch(entry_type.strip()):
        raise BblConversionError("unsupported_entry_type")

    fields: dict[str, str] = {}
    body = text[cursor:end]
    offset = 0
    while offset < len(body):
        while offset < len(body) and body[offset].isspace():
            offset += 1
        if offset == len(body):
            break
        if body[offset] != "\\":
            raise BblConversionError("unrecognized_entry_content")
        command = _COMMAND.match(body, offset)
        if command is None or command.group(1) != "field":
            macro = command.group(1) if command else "unknown"
            raise BblConversionError(f"unsupported_entry_macro:{macro}")
        field_name, offset = _group(body, command.end())
        field_value, offset = _group(body, offset)
        field_name = field_name.strip()
        if not _IDENTIFIER.fullmatch(field_name):
            raise BblConversionError("unsupported_field_name")
        if field_name in fields:
            raise BblConversionError(f"duplicate_field:{field_name}")
        if _BIBLATEX_ONLY.search(field_value):
            raise BblConversionError(f"biblatex_macro_in_field:{field_name}")
        # BibTeX xem dấu phần trăm chưa escape là ký hiệu chú thích; từ chối sửa nội dung đó.
        if re.search(r"(?<!\\)%", field_value):
            raise BblConversionError(f"unescaped_percent_in_field:{field_name}")
        fields[field_name] = field_value

    if not fields:
        raise BblConversionError("entry_has_no_recoverable_fields")
    return key.strip(), entry_type.strip(), fields


def convert_structured_bbl(data: bytes) -> tuple[str, int]:
    """Chuyển phần BBL BibLaTeX chỉ gồm các trường ``\\field`` sang BibTeX.

    Hàm từ chối ``thebibliography`` và các macro tên/danh sách vì không thể
    khôi phục các trường có cấu trúc mà không suy đoán. Không dùng mạng.
    """
    try:
        text = _strip_tex_comments(data.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise BblConversionError("not_utf8") from exc

    matches = list(_ENTRY.finditer(text))
    if not matches:
        if re.search(r"\\begin\s*\{\s*thebibliography\s*\}", text):
            raise BblConversionError("rendered_thebibliography_is_not_reversible_locally")
        raise BblConversionError("no_supported_biblatex_entries")

    entries = []
    seen_keys = set()
    for match in matches:
        end_match = re.search(r"\\endentry\b", text[match.end():])
        if end_match is None:
            raise BblConversionError("missing_endentry")
        end = match.end() + end_match.start()
        key, entry_type, fields = _parse_entry(text, match.start(), end)
        if key in seen_keys:
            raise BblConversionError("duplicate_entry_key")
        seen_keys.add(key)
        lines = [f"@{entry_type}{{{key},"]
        ordered = sorted(fields.items())
        for index, (name, value) in enumerate(ordered):
            comma = "," if index < len(ordered) - 1 else ""
            lines.append(f"  {name} = {{{value}}}{comma}")
        lines.append("}")
        entries.append("\n".join(lines))
    return "\n\n".join(entries) + "\n", len(entries)


def _tex_has_bibitem(version_dir: Path, files: list[str]) -> tuple[bool | None, str | None]:
    for relative in files:
        if PurePosixPath(relative).suffix.lower() != ".tex":
            continue
        try:
            with (version_dir / relative).open("rb") as stream:
                overlap = b""
                in_comment = False
                backslashes = 0
                while block := stream.read(64 * 1024):
                    active = bytearray()
                    for byte in block:
                        if in_comment:
                            if byte == 10:
                                in_comment = False
                                backslashes = 0
                                active.append(byte)
                            continue
                        if byte == 37 and backslashes % 2 == 0:  # % bắt đầu chú thích TeX
                            in_comment = True
                            backslashes = 0
                            continue
                        active.append(byte)
                        backslashes = backslashes + 1 if byte == 92 else 0
                    combined = overlap + active
                    if _BIBITEM_BYTES.search(combined):
                        return True, None
                    overlap = combined[-16:]
        except OSError:
            return None, "tex_unreadable"
    return False, None


def _atomic_bytes(path: Path, data: bytes) -> None:
    """Create an immutable artifact, accepting an identical prior write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise BblConversionError("derived_output_conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Công bố bằng hard link là nguyên tử và không thay thế artifact đã có.
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise BblConversionError("derived_output_conflict")
        except OSError as exc:
            # Ổ Google Drive được gắn trong Colab không hỗ trợ hard link; dùng thao tác
            # đổi tên nguyên tử trong cùng thư mục sau khi kiểm tra lại đích bất biến.
            # M1 tuần tự hóa tiến trình ghi theo từng thành viên/thư mục gốc trước khi công bố.
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                    raise BblConversionError("derived_output_conflict") from exc
            else:
                try:
                    os.rename(temporary, path)
                except FileExistsError:
                    if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                        raise BblConversionError("derived_output_conflict") from exc
                except OSError as rename_exc:
                    raise BblConversionError("artifact_atomic_publish_failed") from rename_exc
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Artifact hoàn chỉnh đã được công bố; lượt thử lại sẽ xác minh artifact đó.
            pass
    except BaseException:
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise BblConversionError("artifact_path_escape") from exc


def _safe_artifact_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in path.parts):
        raise BblConversionError("artifact_path_escape")
    target = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise BblConversionError("artifact_path_is_symlink")
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BblConversionError("artifact_path_escape") from exc
    return target


def apply_bbl_source_policy(
    version_dir: Path,
    files: list[str],
    *,
    artifact_root: str | Path | None,
    mssv: str,
    arxiv_id: str,
    version: str,
) -> tuple[list[str], dict]:
    """Filter BBL files before publishing a source version.

    Removed BBL bytes and successful BibTeX conversions are stored under an
    immutable, version-scoped artifact index outside the source corpus.
    """
    bbl_files = sorted(
        name for name in files if PurePosixPath(name).suffix.lower() == ".bbl"
    )
    if not bbl_files:
        return files, {
            "policy_version": BBL_POLICY_VERSION,
            "status": "complete",
            "index_path": None,
            "index_sha256": None,
            "items": [],
        }
    if artifact_root is None:
        raise BblConversionError("bbl_artifact_dir_required")

    artifact_root = Path(artifact_root).expanduser().resolve()
    paper_key = arxiv_id_to_folder_key(arxiv_id)
    version_root = artifact_root / paper_key / version
    bib_files = [name for name in files if PurePosixPath(name).suffix.lower() == ".bib"]
    has_bibitem, tex_error = _tex_has_bibitem(version_dir, files)
    decisions = []
    removed = set()

    for name in bbl_files:
        source_path = version_dir / name
        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise BblConversionError("bbl_unreadable") from exc
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        item = {
            "bbl_path": name,
            "bbl_sha256": source_sha,
            "status": None,
            "reason": None,
            "source_evidence_path": None,
            "derived_bib_path": None,
            "derived_bib_sha256": None,
            "entry_count": None,
        }
        if tex_error:
            item.update(status="unresolved", reason=tex_error)
        elif bib_files:
            item.update(status="removed_bib_present", reason="same_version_bib_present")
        elif has_bibitem:
            item.update(status="removed_bibitem_in_tex", reason="active_bibitem_in_tex")
        else:
            try:
                if len(source_bytes) > _MAX_CONVERSION_BYTES:
                    raise BblConversionError("bbl_size_limit")
                bib_text, entry_count = convert_structured_bbl(source_bytes)
            except BblConversionError as exc:
                item.update(
                    status="retained_conversion_failed",
                    reason=str(exc),
                    reason_detail=_reason_detail(str(exc)),
                )
            else:
                bib_bytes = bib_text.encode("utf-8")
                bib_sha = hashlib.sha256(bib_bytes).hexdigest()
                source_evidence = version_root / "source_bbl" / name
                derived_bib = version_root / "derived_bib" / f"{name}.bib"
                _atomic_bytes(source_evidence, source_bytes)
                _atomic_bytes(derived_bib, bib_bytes)
                item.update(
                    status="converted_and_removed",
                    reason="converted_to_derived_bib",
                    source_evidence_path=_artifact_relative(artifact_root, source_evidence),
                    derived_bib_path=_artifact_relative(artifact_root, derived_bib),
                    derived_bib_sha256=bib_sha,
                    entry_count=entry_count,
                )

        if item["status"] in {"removed_bib_present", "removed_bibitem_in_tex"}:
            source_evidence = version_root / "source_bbl" / name
            _atomic_bytes(source_evidence, source_bytes)
            item["source_evidence_path"] = _artifact_relative(artifact_root, source_evidence)
        if item["status"] in {
            "removed_bib_present", "removed_bibitem_in_tex", "converted_and_removed",
        }:
            removed.add(name)
        decisions.append(item)

    if any(item["status"] == "unresolved" for item in decisions):
        raise BblConversionError("tex_unreadable")

    index = {
        "schema_version": 1,
        "policy_version": BBL_POLICY_VERSION,
        "mssv": mssv,
        "arxiv_id": arxiv_id,
        "version": version,
        "items": decisions,
    }
    index_bytes = (
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    index_path = version_root / "index.json"
    _atomic_bytes(index_path, index_bytes)
    index_sha = hashlib.sha256(index_bytes).hexdigest()
    for name in sorted(removed):
        try:
            (version_dir / name).unlink()
        except OSError as exc:
            raise BblConversionError("cannot_remove_filtered_bbl") from exc
    remaining = [name for name in files if name not in removed]
    return remaining, {
        "policy_version": BBL_POLICY_VERSION,
        "status": "complete",
        "index_path": _artifact_relative(artifact_root, index_path),
        "index_sha256": index_sha,
        "items": decisions,
    }


def validate_bbl_source_policy(
    version_dir: Path,
    files: list[str],
    policy: dict | None,
    *,
    artifact_root: str | Path | None,
    mssv: str,
    arxiv_id: str,
    version: str,
) -> dict:
    """Verify corpus BBL checksums and its external decision index."""
    if policy is None:
        return {"status": "legacy_unverified"}
    if (not isinstance(policy, dict)
            or policy.get("policy_version") != BBL_POLICY_VERSION
            or policy.get("status") != "complete"
            or not isinstance(policy.get("items"), list)):
        raise BblConversionError("invalid_bbl_policy_record")
    bbl_files = sorted(
        name for name in files if PurePosixPath(name).suffix.lower() == ".bbl"
    )
    decisions = policy["items"]
    if any(not isinstance(item, dict) for item in decisions):
        raise BblConversionError("invalid_bbl_policy_item")
    names = [item.get("bbl_path") for item in decisions]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise BblConversionError("invalid_bbl_policy_item")
    retained_names = sorted(
        item["bbl_path"] for item in decisions
        if item.get("status") == "retained_conversion_failed"
    )
    if retained_names != bbl_files:
        raise BblConversionError("bbl_policy_files_mismatch")
    if policy.get("index_path") is None:
        if decisions or policy.get("index_sha256") is not None:
            raise BblConversionError("missing_bbl_policy_index")
        return {"status": "verified", "items": 0}
    if artifact_root is None:
        raise BblConversionError("bbl_artifact_root_unconfigured")

    artifact_root = Path(artifact_root).expanduser().resolve()
    index_path = _safe_artifact_path(artifact_root, policy["index_path"])
    if index_path.is_symlink() or not index_path.is_file():
        raise BblConversionError("bbl_policy_index_missing")
    index_bytes = index_path.read_bytes()
    if hashlib.sha256(index_bytes).hexdigest() != policy.get("index_sha256"):
        raise BblConversionError("bbl_policy_index_checksum_mismatch")
    try:
        index = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BblConversionError("bbl_policy_index_invalid") from exc
    if (index.get("schema_version") != 1
            or index.get("policy_version") != BBL_POLICY_VERSION
            or index.get("mssv") != mssv
            or index.get("arxiv_id") != arxiv_id
            or index.get("version") != version
            or index.get("items") != decisions):
        raise BblConversionError("bbl_policy_index_mismatch")

    for item in decisions:
        name = item["bbl_path"]
        source_hash = item.get("bbl_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise BblConversionError("invalid_bbl_policy_checksum")
        status = item.get("status")
        if status == "retained_conversion_failed":
            if _sha256(version_dir / name) != source_hash:
                raise BblConversionError("retained_bbl_missing_or_changed")
            continue
        if status not in {
            "removed_bib_present", "removed_bibitem_in_tex", "converted_and_removed",
        }:
            raise BblConversionError("invalid_bbl_policy_status")
        evidence_rel = item.get("source_evidence_path")
        if not isinstance(evidence_rel, str):
            raise BblConversionError("bbl_source_evidence_missing")
        evidence_path = _safe_artifact_path(artifact_root, evidence_rel)
        if (evidence_path.is_symlink() or not evidence_path.is_file()
                or _sha256(evidence_path) != source_hash):
            raise BblConversionError("bbl_source_evidence_checksum_mismatch")
        if status == "removed_bib_present":
            if not any(PurePosixPath(file).suffix.lower() == ".bib" for file in files):
                raise BblConversionError("bib_present_decision_without_bib")
        elif status == "removed_bibitem_in_tex":
            if any(PurePosixPath(file).suffix.lower() == ".bib" for file in files):
                raise BblConversionError("bibitem_decision_conflicts_with_bib")
            has_bibitem, error = _tex_has_bibitem(version_dir, files)
            if error or not has_bibitem:
                raise BblConversionError("bibitem_decision_not_verified")
        else:
            if any(PurePosixPath(file).suffix.lower() == ".bib" for file in files):
                raise BblConversionError("converted_decision_conflicts_with_bib")
            has_bibitem, error = _tex_has_bibitem(version_dir, files)
            if error or has_bibitem:
                raise BblConversionError("converted_decision_conflicts_with_bibitem")
            derived_rel = item.get("derived_bib_path")
            derived_hash = item.get("derived_bib_sha256")
            derived_path = (
                _safe_artifact_path(artifact_root, derived_rel)
                if isinstance(derived_rel, str) else None
            )
            if (derived_path is None or derived_path.is_symlink() or not derived_path.is_file()
                    or _sha256(derived_path) != derived_hash
                    or type(item.get("entry_count")) is not int
                    or item["entry_count"] < 1):
                raise BblConversionError("derived_bib_checksum_mismatch")
            try:
                expected_bib, expected_count = convert_structured_bbl(evidence_path.read_bytes())
            except (OSError, BblConversionError) as exc:
                raise BblConversionError("derived_bib_source_no_longer_converts") from exc
            if (derived_path.read_bytes() != expected_bib.encode("utf-8")
                    or expected_count != item["entry_count"]):
                raise BblConversionError("derived_bib_content_mismatch")
    return {"status": "verified", "items": len(decisions)}


def audit_bbl_sources(
    store: Any,
    pairs: list[tuple[str, str]],
    *,
    output_dir: str | Path,
    report_path: str | Path,
    session_id: str,
) -> dict:
    """Thử chuyển .bbl đủ điều kiện và ghi kết quả ngoài corpus nguồn.

    Tệp .bbl và manifest lưu trữ không bị sửa. Tệp .bib tạo được chỉ là dẫn xuất,
    được lưu cùng thư mục báo cáo để không vi phạm storage contract v2.
    """
    output_dir = Path(output_dir).resolve()
    report_path = Path(report_path).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []

    for mssv, arxiv_id in pairs:
        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            items.append({"mssv": mssv, "arxiv_id": arxiv_id,
                          "status": "manifest_unavailable", "reason": type(exc).__name__})
            continue
        versions = manifest.get("versions", {})
        if manifest.get("discovery_status") == "not_found" or not versions:
            items.append({"mssv": mssv, "arxiv_id": arxiv_id, "version": None,
                          "status": "no_source_versions"})
            continue

        paper_dir = store.root / arxiv_id_to_folder_key(arxiv_id)
        for version, record in sorted(versions.items()):
            base = {"mssv": mssv, "arxiv_id": arxiv_id, "version": version}
            if not store.should_skip_version(mssv, arxiv_id, version):
                items.append({**base, "status": "source_integrity_unverified"})
                continue
            files = record.get("files", [])
            policy = record.get("bbl_policy")
            if policy is not None:
                try:
                    validation = validate_bbl_source_policy(
                        paper_dir / version,
                        files,
                        policy,
                        artifact_root=getattr(store, "bbl_artifact_root", None),
                        mssv=mssv,
                        arxiv_id=arxiv_id,
                        version=version,
                    )
                except (OSError, ValueError, TypeError) as exc:
                    items.append({
                        **base,
                        "status": "bbl_policy_artifact_invalid",
                        "reason": str(exc),
                    })
                    continue
                if not policy.get("items"):
                    items.append({
                        **base,
                        "status": "no_bbl",
                        "bbl_policy_status": validation["status"],
                    })
                    continue
                for decision in policy["items"]:
                    items.append({
                        **base,
                        **decision,
                        "status": decision["status"],
                        "bbl_policy_status": validation["status"],
                    })
                continue
            bbl_files = [name for name in files if PurePosixPath(name).suffix.lower() == ".bbl"]
            if not bbl_files:
                items.append({**base, "status": "no_bbl"})
                continue
            bib_files = [name for name in files if PurePosixPath(name).suffix.lower() == ".bib"]
            if bib_files:
                for name in bbl_files:
                    path = paper_dir / version / name
                    items.append({**base, "bbl_path": name, "bbl_sha256": _sha256(path),
                                  "status": "not_required_bib_present"})
                continue
            has_bibitem, tex_error = _tex_has_bibitem(paper_dir / version, files)
            if tex_error:
                for name in bbl_files:
                    path = paper_dir / version / name
                    items.append({**base, "bbl_path": name, "bbl_sha256": _sha256(path),
                                  "status": "conversion_not_attempted", "reason": tex_error})
                continue
            if has_bibitem:
                for name in bbl_files:
                    path = paper_dir / version / name
                    items.append({**base, "bbl_path": name, "bbl_sha256": _sha256(path),
                                  "status": "not_required_bibitem_in_tex"})
                continue

            for name in bbl_files:
                source_path = paper_dir / version / name
                source_sha = _sha256(source_path)
                item = {**base, "bbl_path": name, "bbl_sha256": source_sha,
                        "conversion_attempted": True}
                try:
                    if source_path.stat().st_size > _MAX_CONVERSION_BYTES:
                        raise BblConversionError("bbl_size_limit")
                    bib_text, entry_count = convert_structured_bbl(source_path.read_bytes())
                except (OSError, BblConversionError) as exc:
                    reason = str(exc) if isinstance(exc, BblConversionError) else "bbl_unreadable"
                    items.append({**item, "status": "not_converted_bbl_retained", "reason": reason,
                                  "reason_detail": _reason_detail(reason)})
                    continue

                bib_bytes = bib_text.encode("utf-8")
                bib_sha = hashlib.sha256(bib_bytes).hexdigest()
                target = output_dir / f"{arxiv_id.replace('.', '-')}_{version}_{source_sha[:12]}.bib"
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != bib_bytes:
                        items.append({**item, "status": "derived_output_conflict",
                                      "reason": "existing_derived_bib_differs"})
                        continue
                else:
                    with target.open("xb") as stream:
                        stream.write(bib_bytes)
                items.append({**item, "status": "converted_structured_fields",
                              "entry_count": entry_count,
                              "derived_bib_path": str(target),
                              "derived_bib_sha256": bib_sha,
                              "source_retained": True})

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    result = {
        "schema_version": 1,
        "session_id": session_id,
        "conversion_policy": "offline_structured_fields_only; unsupported BBL is retained with a reason",
        "source_corpus_modified": False,
        "output_dir": str(output_dir),
        "counts": counts,
        "items": items,
    }
    _atomic_json(report_path, result)
    result["report_path"] = str(report_path)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _reason_detail(reason: str) -> str:
    if reason.startswith("unsupported_entry_macro:"):
        return "Mục có cấu trúc tên hoặc danh sách không được bộ chuyển đổi cục bộ hỗ trợ; giữ nguyên .bbl."
    if reason.startswith(("duplicate_field:", "biblatex_macro_in_field:", "unescaped_percent_in_field:")):
        return "Trường chứa cấu trúc có thể đổi nghĩa khi chuyển sang BibTeX; giữ nguyên .bbl."
    return _REASON_DETAILS.get(
        reason,
        "Không thể khôi phục an toàn cấu trúc BibTeX từ tệp này; giữ nguyên .bbl.",
    )

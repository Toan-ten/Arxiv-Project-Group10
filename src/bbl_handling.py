"""Thử chuyển đổi cục bộ tệp .bbl và lưu bằng chứng xử lý riêng."""

from __future__ import annotations

import hashlib
import re
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
        # BibTeX treats an unescaped percent sign as a comment. Refuse to alter it.
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
